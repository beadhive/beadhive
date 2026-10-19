"""S3 transport capabilities and host credentials without exposing secrets (bh-b1scz.4)."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from beadhive import config, dolt_remote, dolt_remote_credentials, engine
from beadhive.dolt_remote import RemoteFamily, RemoteOp, SupportLevel
from beadhive.hq_sql_transport import SqlTransportError
from beadhive.run import run
from harness.beads import embedded_env, skip_if_no_bd

SECRET = "wJalrXUtnFEMI-fixture-secret"
KEY_ID = "AKIAFIXTUREKEYID"


def _resolve(url, label="", cfg=None, environ=None, home=None, broker=None):
    return dolt_remote_credentials.resolve(
        dolt_remote.classify(url),
        label,
        cfg or {},
        environ=environ or {},
        home=home or Path("/nonexistent-home"),
        broker=broker,
    )


# ---- capabilities ----------------------------------------------------------------------


@pytest.mark.parametrize("url", ["s3://acme-beads/beads/api", "aws://[tbl:acme-beads]/api"])
def test_documented_aws_forms_are_s3_capable(url):
    target = dolt_remote.classify(url)
    assert target.capabilities.object_store and target.capabilities.credentials == "aws"
    assert target.supported and target.allows(RemoteOp.PUSH)
    assert not target.capabilities.read_only_diff  # ahead/behind is honestly unknowable


def test_custom_endpoint_is_implemented_after_its_end_to_end_proof():
    # bh-82gpi: proven against an S3-compatible endpoint (moto server mode); server mode only.
    target = dolt_remote.classify("s3://b/db?endpoint=https://s3.example:9000&path-style=true")
    assert target.family == RemoteFamily.S3
    assert target.capabilities.support == SupportLevel.IMPLEMENTED
    assert target.allows(RemoteOp.ADD) and target.allows(RemoteOp.PUSH)
    assert target.capabilities.credentials == "aws"
    embedded = dolt_remote.for_store_mode(target, embedded=True)
    assert not embedded.allows(RemoteOp.PUSH)


def test_embedded_store_cannot_drive_s3_but_can_drive_aws():
    s3 = dolt_remote.for_store_mode(dolt_remote.classify("s3://b/db"), embedded=True)
    assert s3.capabilities.support == SupportLevel.UNSUPPORTED and not s3.allows(RemoteOp.ADD)
    assert "embedded" in s3.capabilities.note
    aws = dolt_remote.for_store_mode(dolt_remote.classify("aws://[t:b]/db"), embedded=True)
    assert aws.supported
    server = dolt_remote.for_store_mode(dolt_remote.classify("s3://b/db"), embedded=False)
    assert server.supported


def test_unknown_scheme_is_a_typed_unsupported_capability():
    target = dolt_remote.classify("dolthub://acme/api")
    assert target.family == RemoteFamily.UNKNOWN
    with pytest.raises(dolt_remote.UnsupportedRemoteOperation) as caught:
        raise dolt_remote.UnsupportedRemoteOperation(target, RemoteOp.ADD)
    assert caught.value.op == RemoteOp.ADD and "unknown" in str(caught.value)


# ---- credential resolution -------------------------------------------------------------


def test_git_and_file_remotes_need_no_cloud_credentials():
    for url in ("git+ssh://git@github.com/a/b.git", "file:///srv/r"):
        assert _resolve(url).source == "not-required"


def test_ambient_keys_resolve_without_copying_values():
    got = _resolve(
        "s3://b/db", environ={"AWS_ACCESS_KEY_ID": KEY_ID, "AWS_SECRET_ACCESS_KEY": SECRET}
    )
    assert got.ok and got.source == "ambient:AWS_ACCESS_KEY_ID" and dict(got.env) == {}


def test_half_set_keys_name_the_variables_never_the_values():
    got = _resolve("s3://b/db", environ={"AWS_ACCESS_KEY_ID": KEY_ID})
    assert not got.ok
    assert "AWS_SECRET_ACCESS_KEY is not" in got.missing and KEY_ID not in got.missing


def test_missing_credentials_fail_before_anything_runs(tmp_path):
    got = _resolve("aws://[t:b]/db", environ={}, home=tmp_path)
    assert not got.ok and "no AWS credentials found" in got.missing


def test_profile_variable_and_shared_files_count_as_ambient(tmp_path):
    assert _resolve("s3://b/db", environ={"AWS_PROFILE": "beads"}).source == "ambient:AWS_PROFILE"
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "credentials").write_text("[default]\n")
    assert _resolve("s3://b/db", home=tmp_path).source == "ambient:~/.aws/credentials"


def test_unknown_label_names_the_missing_host_profile():
    got = _resolve("s3://b/db", label="prod")
    assert not got.ok and "dolt.remote_credentials.prod" in got.missing


def test_aws_profile_source_sets_only_the_profile_name(tmp_path):
    cfg = {"dolt": {"remote_credentials": {"prod": {"source": "aws-profile", "aws_profile": "bp"}}}}
    assert not _resolve("s3://b/db", "prod", cfg, home=tmp_path).ok
    (tmp_path / ".aws").mkdir()
    (tmp_path / ".aws" / "config").write_text("[profile bp]\n")
    got = _resolve("s3://b/db", "prod", cfg, home=tmp_path)
    assert got.ok and dict(got.env) == {"AWS_PROFILE": "bp"}


def test_instance_role_is_trusted_without_a_local_probe():
    cfg = {"dolt": {"remote_credentials": {"ec2": {"source": "instance-role"}}}}
    assert _resolve("s3://b/db", "ec2", cfg).source == "instance-role"


class FakeBroker:
    def __init__(self, fail=False):
        self.fail, self.calls = fail, []

    def get(self, reference, *, deadline):
        self.calls.append(reference["key"])
        if self.fail:
            raise SqlTransportError("fnox credential unavailable")
        return {"KID": KEY_ID, "SEC": SECRET}[reference["key"]]


_FNOX = {
    "dolt": {
        "remote_credentials": {
            "vault": {
                "source": "fnox",
                "access_key_id": {"config_path": "/etc/f.toml", "profile": "p", "key": "KID"},
                "secret_access_key": {"config_path": "/etc/f.toml", "profile": "p", "key": "SEC"},
            }
        }
    }
}


def test_fnox_values_reach_only_the_child_env_and_never_repr():
    got = _resolve("s3://b/db", "vault", _FNOX, broker=FakeBroker())
    assert got.ok and dict(got.env) == {
        "AWS_ACCESS_KEY_ID": KEY_ID,
        "AWS_SECRET_ACCESS_KEY": SECRET,
    }
    assert SECRET not in repr(got) and KEY_ID not in repr(got) and SECRET not in str(got)


def test_fnox_failure_is_value_free():
    got = _resolve("s3://b/db", "vault", _FNOX, broker=FakeBroker(fail=True))
    assert not got.ok and "unavailable" in got.missing and dict(got.env) == {}


# ---- redaction across surfaces -----------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "rendered"),
    [
        (
            f"https://{SECRET}@doltremoteapi.dolthub.com/a/b",
            "https://***@doltremoteapi.dolthub.com/a/b",
        ),
        (
            f"s3://b/db?endpoint=https://m:9000&token={SECRET}",
            "s3://b/db?endpoint=https://m:9000&token=***",
        ),
        ("git+ssh://git@github.com/a/b.git", "git+ssh://***@github.com/a/b.git"),
        (f"s3://b/db {SECRET}", "***"),
        ("s3://b/db", "s3://b/db"),
    ],
)
def test_one_redaction_rule_for_every_rendered_url(url, rendered):
    assert dolt_remote.redact_url(url) == rendered


def test_exceptions_and_engine_refusals_are_redacted(monkeypatch):
    target = dolt_remote.classify(f"https://{SECRET}@doltremoteapi.dolthub.com/a/b")
    assert SECRET not in str(dolt_remote.UnsupportedRemoteOperation(target, RemoteOp.CLONE))

    eng = engine.BdEngine()
    registered = json.dumps([{"name": "origin", "url": f"https://{SECRET}@host.example/a/b"}])
    monkeypatch.setattr(
        eng,
        "invoke",
        lambda args, **kw: subprocess.CompletedProcess(args, 0, stdout=registered, stderr=""),
    )
    refused = eng.add_remote("/hive", "origin", "s3://b/db")
    assert refused.returncode == 1 and SECRET not in refused.stderr


def test_config_display_cannot_carry_an_embedded_secret(world):
    host_cfg = config.load_host()
    host_cfg.pop("providers", None)
    host_cfg.pop("managed_repos", None)
    config.save(host_cfg)
    path = config.fleet_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "managed_repos:\n"
        '  - {"provider": "github", "org": "a", "repo": "b", "prefix": "ab", '
        f'"dolt_remote": {{"url": "s3://{KEY_ID}:{SECRET}@bucket/db"}}}}\n'
    )
    with pytest.raises(config.ConfigError) as caught:
        config.load()
    assert SECRET not in str(caught.value) and KEY_ID not in str(caught.value)
    assert "dolt_remote.url" in str(caught.value)


# ---- the exact URL forms the pinned bd/Dolt accept (real binary, no network) ------------------


@pytest.fixture
def embedded_bd(tmp_path, monkeypatch):
    for key in set(os.environ) - set(embedded_env()):
        monkeypatch.delenv(key, raising=False)
    repo = tmp_path / "hive"
    repo.mkdir()
    run(["git", "init", "-q", "-b", "main"], cwd=str(repo), check=True, capture=True)
    run(["bd", "init", "--prefix", "px", "--quiet"], cwd=str(repo), check=True, capture=True)
    # Offline by construction: any AWS call goes to a closed local port, never the internet.
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", KEY_ID)
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", SECRET)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")
    monkeypatch.setenv("AWS_ENDPOINT_URL", "http://127.0.0.1:9")
    monkeypatch.setenv("AWS_MAX_ATTEMPTS", "1")
    return repo


def _push(repo: Path, name: str) -> str:
    res = run(
        ["bd", "dolt", "push", "--remote", name],
        cwd=str(repo),
        check=False,
        capture=True,
        timeout=120,
    )
    assert res.returncode != 0
    return res.stdout + res.stderr


@pytest.mark.integration
@skip_if_no_bd
def test_pinned_embedded_bd_speaks_aws_but_not_s3(embedded_bd):
    eng = engine.BdEngine()
    assert eng.add_remote(embedded_bd, "awsr", "aws://[bh-table:bh-bucket]/px").returncode == 0
    assert eng.add_remote(embedded_bd, "s3r", "s3://bh-bucket/beads/px").returncode == 0
    # aws:// is understood: the failure is the (refused) DynamoDB call, not the scheme.
    assert "DynamoDB" in _push(embedded_bd, "awsr")
    # s3:// is registered but bd 1.3.0's embedded engine cannot open it — for_store_mode's rule.
    assert "unknown url scheme: 's3'" in _push(embedded_bd, "s3r")


class _Broker:
    def get(self, reference, *, deadline):
        return {"k": KEY_ID, "s": SECRET}[reference["key"]]


def _server_cfg(label="host"):
    ref = {"config_path": "/etc/fnox.toml", "profile": "default"}
    return {
        "dolt": {
            "server_credentials": label,
            "remote_credentials": {
                "host": {
                    "source": "fnox",
                    "access_key_id": {**ref, "key": "k"},
                    "secret_access_key": {**ref, "key": "s"},
                }
            },
        }
    }


def test_server_env_is_empty_by_default():
    res = dolt_remote_credentials.server_env({})
    assert res.ok and not res.env


def test_server_env_resolves_the_configured_source_without_leaking():
    res = dolt_remote_credentials.server_env(_server_cfg(), broker=_Broker())
    assert res.ok and res.env == {"AWS_ACCESS_KEY_ID": KEY_ID, "AWS_SECRET_ACCESS_KEY": SECRET}
    assert SECRET not in repr(res)


def test_server_env_fails_closed_naming_what_is_missing():
    res = dolt_remote_credentials.server_env(_server_cfg("absent"), broker=_Broker())
    assert not res.ok and "absent" in res.missing and SECRET not in res.missing
