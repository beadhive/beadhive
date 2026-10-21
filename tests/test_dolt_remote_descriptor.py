"""HQ Dolt remote descriptors in fleet configuration (bh-b1scz.2).

Contract: docs/design/hq-dolt-remote-descriptor-adr.md (`dolt-remote-descriptor/v1`). A
descriptor is optional (absent = today's behaviour), fleet-owned, value-free on error, and
survives every registry write that does not own it.
"""

from __future__ import annotations

import pytest

from beadhive import config, config_partition, registry
from beadhive.hq_document_validation import DocumentValidationError, validate_settings_mapping
from beadhive.modules.config.contracts import (
    DOLT_REMOTE_DESCRIPTOR_CONTRACT,
    BeadhiveConfig,
    DoltRemoteDescriptor,
    ManagedRepoEntry,
    dolt_remote_location,
)

_BASE = {"provider": "github", "org": "acme", "repo": "api", "prefix": "ac-api", "kind": "personal"}
_SECRET = "AKIAEXAMPLESECRET"


def _fleet(*entries):
    return {"managed_repos": [dict(entry) for entry in entries]}


def _error(document, scope="fleet") -> DocumentValidationError:
    with pytest.raises(DocumentValidationError) as caught:
        validate_settings_mapping(document, scope=scope)
    return caught.value


def test_contract_id_and_defaults_are_stable():
    assert DOLT_REMOTE_DESCRIPTOR_CONTRACT == "dolt-remote-descriptor/v1"
    descriptor = DoltRemoteDescriptor(url="s3://bucket/beads/github/acme/api")
    assert (descriptor.name, descriptor.credentials) == ("origin", "")


def test_absent_descriptor_keeps_existing_configs_valid_and_unchanged():
    validate_settings_mapping(_fleet(_BASE), scope="fleet")
    assert ManagedRepoEntry(**_BASE).dolt_remote is None
    assert BeadhiveConfig(managed_repos=[_BASE]).managed_repos[0].dolt_remote is None


@pytest.mark.parametrize(
    "url",
    [
        "s3://acme-beads/beads/github/acme/api",
        "s3://acme-beads/db?endpoint=https://minio.local:9000&region=auto&path-style=true",
        "aws://[beads-table:acme-beads]/api",
        "file:///srv/dolt-remotes/api",
        "git+ssh://git@github.com/acme/api.git",
        "git+https://github.com/acme/api.git",
        "git+file:///srv/git/acme/api.git",
        "https://doltremoteapi.dolthub.com/acme/api",
        "gs://acme-beads/api",
        "future://host/whatever",
    ],
)
def test_valid_descriptor_urls_validate(url):
    validate_settings_mapping(_fleet(dict(_BASE, dolt_remote={"url": url})), scope="fleet")


@pytest.mark.parametrize(
    ("url", "code"),
    [
        ("", "dolt_remote_url_required"),
        ("acme/api", "dolt_remote_url_malformed"),
        (" s3://b/p", "dolt_remote_url_malformed"),
        ("s3://b/p#frag", "dolt_remote_url_malformed"),
        ("s3://bucket", "dolt_remote_url_malformed"),
        ("file://relative/path", "dolt_remote_url_malformed"),
        ("git+file://relative/api.git", "dolt_remote_url_malformed"),
        (f"s3://{_SECRET}:hunter2@bucket/db", "dolt_remote_embedded_credential"),
        (f"https://{_SECRET}@doltremoteapi.dolthub.com/a/b", "dolt_remote_embedded_credential"),
        (f"git+ssh://git:{_SECRET}@github.com/a/b.git", "dolt_remote_embedded_credential"),
        (f"s3://bucket/db?aws_secret_access_key={_SECRET}", "dolt_remote_embedded_credential"),
        (f"s3://bucket/db?X-Amz-Signature={_SECRET}", "dolt_remote_embedded_credential"),
        ("s3://bucket/db?acl=private", "dolt_remote_query_unsupported"),
        ("file:///srv/x?mode=1", "dolt_remote_query_unsupported"),
    ],
)
def test_bad_urls_fail_with_path_specific_value_free_errors(url, code):
    error = _error(_fleet(dict(_BASE, dolt_remote={"url": url})))
    assert error.path == "managed_repos.<item>.dolt_remote.url"
    assert error.code == f"validation_{code}"
    assert _SECRET not in str(error) and "hunter2" not in str(error)


def test_bad_name_and_label_fail_on_their_own_paths():
    error = _error(_fleet(dict(_BASE, dolt_remote={"url": "s3://b/p", "name": "1x"})))
    assert (error.path, error.code) == (
        "managed_repos.<item>.dolt_remote.name",
        "validation_dolt_remote_name_invalid",
    )
    error = _error(_fleet(dict(_BASE, dolt_remote={"url": "s3://b/p", "credentials": "Bad!"})))
    assert error.code == "validation_dolt_remote_credentials_label_invalid"


def test_unknown_descriptor_key_is_rejected():
    error = _error(_fleet(dict(_BASE, dolt_remote={"url": "s3://b/p", "secret": _SECRET})))
    assert _SECRET not in str(error)


def test_two_hives_cannot_share_an_object_store_location():
    other = dict(_BASE, repo="web", prefix="ac-web")
    error = _error(
        _fleet(
            dict(_BASE, dolt_remote={"url": "s3://Acme-Beads/beads/api/"}),
            dict(other, dolt_remote={"url": "s3://acme-beads/beads/api"}),
        )
    )
    assert (error.path, error.code) == ("managed_repos", "validation_dolt_remote_path_shared")


def test_distinct_paths_and_distinct_endpoints_are_distinct_locations():
    other = dict(_BASE, repo="web", prefix="ac-web")
    validate_settings_mapping(
        _fleet(
            dict(_BASE, dolt_remote={"url": "s3://b/beads/api"}),
            dict(other, dolt_remote={"url": "s3://b/beads/api?endpoint=https://r2.example"}),
        ),
        scope="fleet",
    )
    # Git remotes address a repository, not a storage path: not location-checked.
    assert dolt_remote_location("git+ssh://git@github.com/a/b.git") is None
    assert dolt_remote_location("file:///srv/x/") == "file:///srv/x"


def test_host_credential_profiles_validate_and_never_hold_values():
    validate_settings_mapping(
        {
            "dolt": {
                "remote_credentials": {
                    "prod": {"source": "aws-profile", "aws_profile": "beads-prod"},
                    "vault": {
                        "source": "fnox",
                        "access_key_id": {"config_path": "/etc/f.toml", "profile": "p", "key": "K"},
                        "secret_access_key": {
                            "config_path": "/etc/f.toml",
                            "profile": "p",
                            "key": "S",
                        },
                    },
                }
            }
        },
        scope="host",
    )
    error = _error({"dolt": {"remote_credentials": {"prod": {"secret": _SECRET}}}}, scope="host")
    assert _SECRET not in str(error)
    _error({"dolt": {"remote_credentials": {"prod": {"source": "fnox"}}}}, scope="host")


def test_new_leaves_have_the_right_partition_owner():
    assert config_partition.partition_of("managed_repos.dolt_remote.url") == "fleet"
    assert config_partition.partition_of("managed_repos.dolt_remote.credentials") == "fleet"
    assert config_partition.partition_of("dolt.remote_credentials") == "host"
    assert config_partition.partition_of("dolt.remote_credentials.prod.source") == "host"
    assert not config_partition.is_host_overridable("managed_repos.dolt_remote.url")


# ---- registry writes preserve fields they do not own -------------------------------------


def _write_fleet(text: str):
    host_cfg = config.load_host()
    host_cfg.pop("providers", None)
    if not host_cfg.get("managed_repos"):
        host_cfg.pop("managed_repos", None)
    config.save(host_cfg)
    path = config.fleet_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return path


_FLEET_WITH_DESCRIPTOR = """\
orgs: {}
# One entry per managed hive.
managed_repos:
  - {"provider": "github", "org": "acme", "repo": "api", "prefix": "ac-api", "kind": "personal", "dolt_remote": {"url": "s3://acme-beads/beads/api", "credentials": "prod"}}
  - {"provider": "github", "org": "acme", "repo": "web", "prefix": "ac-web", "kind": "personal"}
"""  # noqa: E501 - one flow mapping per line is the registry layout under test


def test_reregistration_and_prefix_repair_preserve_the_descriptor(world):
    path = _write_fleet(_FLEET_WITH_DESCRIPTOR)

    registry.register("github", "acme", "api", "ac-api2", "personal")

    text = path.read_text()
    assert "# One entry per managed hive." in text
    entry = registry.find_entry(config.load(), "github", "acme", "api")
    assert str(entry["prefix"]) == "ac-api2"
    assert dict(entry["dolt_remote"]) == {
        "url": "s3://acme-beads/beads/api",
        "credentials": "prod",
    }
    # Flow style survives: still one mapping per line, and the untouched entry is byte-equal.
    lines = [line for line in text.splitlines() if line.startswith("  - {")]
    assert len(lines) == 2 and any('"dolt_remote"' in line for line in lines)
    assert '"repo": "web", "prefix": "ac-web"' in text


def test_hive_set_writes_a_descriptor_and_refuses_an_embedded_secret(world):
    path = _write_fleet(_FLEET_WITH_DESCRIPTOR)

    ok = config.set_hive_value("ac-web", "dolt_remote.url", "s3://acme-beads/beads/web")
    assert ok["ok"], ok
    entry = registry.find_entry(config.load(), "github", "acme", "web")
    assert str(entry["dolt_remote"]["url"]) == "s3://acme-beads/beads/web"

    before = path.read_text()
    refused = config.set_hive_value("ac-web", "dolt_remote.url", f"s3://{_SECRET}:x@b/web")
    assert not refused["ok"]
    rendered = " ".join(problem["message"] for problem in refused["problems"])
    assert "dolt_remote.url" in rendered and "embedded_credential" in rendered
    assert _SECRET not in rendered
    assert path.read_text() == before


def test_hive_set_refuses_a_shared_location(world):
    _write_fleet(_FLEET_WITH_DESCRIPTOR)

    refused = config.set_hive_value("ac-web", "dolt_remote.url", "s3://acme-beads/beads/api")

    assert not refused["ok"]
    assert "path_shared" in " ".join(problem["message"] for problem in refused["problems"])


@pytest.mark.parametrize("scope", [None, config.SCOPE_FLEET])
@pytest.mark.parametrize("hive_id", ["ac-api", "github/acme/api"])
def test_hive_alias_get_reads_the_published_descriptor(world, scope, hive_id):
    path = _write_fleet(_FLEET_WITH_DESCRIPTOR)
    before = path.read_bytes()
    result = config.get_value(f"hives.{hive_id}.dolt_remote", scope=scope)
    assert result == {
        "ok": True,
        "problems": [],
        "value": {"url": "s3://acme-beads/beads/api", "credentials": "prod"},
    }
    assert (
        config.get_value(f"hives.{hive_id}.dolt_remote.credentials", scope=scope)["value"] == "prod"
    )
    assert path.read_bytes() == before


def test_hive_alias_get_preserves_host_scope_and_missing_field_errors(world):
    from typer.testing import CliRunner

    from beadhive.cli import app

    _write_fleet(_FLEET_WITH_DESCRIPTOR)
    assert not config.get_value("hives.ac-api.dolt_remote", scope=config.SCOPE_HOST)["ok"]
    assert not config.get_value("hives.ac-web.dolt_remote", scope=config.SCOPE_FLEET)["ok"]
    runner = CliRunner()
    found = runner.invoke(app, ["config", "get", "hives.ac-api.dolt_remote", "--scope", "fleet"])
    assert found.exit_code == 0 and "s3://acme-beads/beads/api" in found.stdout
    missing = runner.invoke(app, ["config", "get", "hives.missing.dolt_remote", "--scope", "fleet"])
    assert missing.exit_code == 1 and "no hive matching" in missing.output
