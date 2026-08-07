"""``hosts/<host_id>.yaml`` — the fleet's roster in HQ (bh-ytbb.3).

Covers the acceptance bar directly:
  * the manifest schema covers label/os/arch/role/capacity/harnesses/identity.
  * `role` is a closed set — one round-trip test per value (executor,
    transient, viewer).
  * the identity mechanism (ssh alias / insteadOf / core.sshCommand / none) round-trips.
  * a malformed manifest fails loudly on read, naming the offending key.

The autouse `_sandbox_bh_home` fixture (tests/conftest.py) isolates `BH_HOME` per test; every
test below also passes its own throwaway `hq_dir` (a `tmp_path` subdir) explicitly — never
`config.hq_dir()` — so this suite can never touch a real HQ store.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from beadhive import hosts


def _manifest(**overrides) -> hosts.HostManifest:
    fields = {
        "host_id": "11111111-1111-4111-8111-111111111111",
        "label": "test-host",
        "os": "darwin",
        "arch": "arm64",
        "role": "viewer",
        "identity": hosts.IdentityMechanism(kind="none", value=""),
    }
    fields.update(overrides)
    return hosts.HostManifest(**fields)


# ---- schema shape --------------------------------------------------------------


def test_manifest_requires_the_documented_fields():
    with pytest.raises(ValidationError):
        hosts.HostManifest(host_id="h1")  # missing label/os/arch/role/identity


def test_manifest_rejects_unknown_top_level_key():
    with pytest.raises(ValidationError):
        _manifest(bogus="nope")


def test_capacity_and_harnesses_default_to_empty_and_accept_free_form_data():
    bare = _manifest()
    assert bare.capacity == {}
    assert bare.harnesses == {}

    loaded = _manifest(
        capacity={"weekly_token_budget": 100, "max_concurrent_sessions": 2},
        harnesses={"claude": {"note": "placeholder"}},
    )
    assert loaded.capacity["weekly_token_budget"] == 100
    assert loaded.harnesses["claude"]["note"] == "placeholder"


# ---- role: closed set, one round-trip per value --------------------------------


@pytest.mark.parametrize("role", hosts.HOST_ROLES)
def test_each_role_value_round_trips_through_write_and_read(tmp_path, role):
    hq_dir = tmp_path / "hq"
    manifest = _manifest(host_id=f"host-{role}", role=role)

    written = hosts.save(hq_dir, manifest)

    assert written == hosts.manifest_path(hq_dir, manifest.host_id)
    loaded = hosts.load(hq_dir, manifest.host_id)
    assert loaded == manifest
    assert loaded.role == role


def test_role_outside_the_closed_set_is_rejected_at_construction():
    with pytest.raises(ValidationError):
        _manifest(role="super-admin")


def test_host_roles_constant_matches_the_three_documented_values():
    assert hosts.HOST_ROLES == ("executor", "transient", "viewer")


# ---- identity mechanism ---------------------------------------------------------


@pytest.mark.parametrize(
    "kind,value",
    [
        ("none", ""),
        ("ssh_alias", "github-operator"),
        ("insteadOf", "url.git@github.com-operator:.insteadOf=git@github.com:"),
        ("core_sshCommand", "ssh -i ~/.ssh/operator_ed25519"),
    ],
)
def test_identity_mechanism_round_trips_for_each_kind(tmp_path, kind, value):
    hq_dir = tmp_path / "hq"
    manifest = _manifest(
        host_id=f"host-{kind}", identity=hosts.IdentityMechanism(kind=kind, value=value)
    )

    hosts.save(hq_dir, manifest)
    loaded = hosts.load(hq_dir, manifest.host_id)

    assert loaded.identity.kind == kind
    assert loaded.identity.value == value


def test_identity_mechanism_kind_outside_the_closed_set_is_rejected():
    with pytest.raises(ValidationError):
        hosts.IdentityMechanism(kind="magic", value="")


# ---- read: missing + malformed ---------------------------------------------------


def test_load_raises_file_not_found_when_no_manifest_exists(tmp_path):
    hq_dir = tmp_path / "hq"

    with pytest.raises(FileNotFoundError, match="no-such-host"):
        hosts.load(hq_dir, "no-such-host")


def test_load_fails_loudly_naming_the_offending_key_on_a_bad_role(tmp_path):
    hq_dir = tmp_path / "hq"
    manifest_dir = hosts.hosts_dir(hq_dir)
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "bad-role.yaml").write_text(
        "host_id: bad-role\n"
        "label: broken\n"
        "os: linux\n"
        "arch: x86_64\n"
        "role: super-admin\n"
        "identity:\n"
        "  kind: none\n"
        "  value: ''\n"
    )

    with pytest.raises(hosts.ManifestError, match="role") as exc_info:
        hosts.load(hq_dir, "bad-role")

    assert "role" in str(exc_info.value)


def test_load_fails_loudly_naming_the_offending_key_on_an_unknown_key(tmp_path):
    hq_dir = tmp_path / "hq"
    manifest_dir = hosts.hosts_dir(hq_dir)
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "extra-key.yaml").write_text(
        "host_id: extra-key\n"
        "label: broken\n"
        "os: linux\n"
        "arch: x86_64\n"
        "role: viewer\n"
        "identity:\n"
        "  kind: none\n"
        "  value: ''\n"
        "totally_unknown_field: surprise\n"
    )

    with pytest.raises(hosts.ManifestError, match="totally_unknown_field"):
        hosts.load(hq_dir, "extra-key")


def test_load_fails_loudly_naming_the_offending_key_on_a_missing_required_field(tmp_path):
    hq_dir = tmp_path / "hq"
    manifest_dir = hosts.hosts_dir(hq_dir)
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "no-arch.yaml").write_text(
        "host_id: no-arch\n"
        "label: broken\n"
        "os: linux\n"
        "role: viewer\n"
        "identity:\n"
        "  kind: none\n"
        "  value: ''\n"
    )

    with pytest.raises(hosts.ManifestError, match="arch"):
        hosts.load(hq_dir, "no-arch")


def test_load_fails_loudly_naming_the_offending_nested_key_on_a_bad_identity_kind(tmp_path):
    hq_dir = tmp_path / "hq"
    manifest_dir = hosts.hosts_dir(hq_dir)
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "bad-identity.yaml").write_text(
        "host_id: bad-identity\n"
        "label: broken\n"
        "os: linux\n"
        "arch: x86_64\n"
        "role: viewer\n"
        "identity:\n"
        "  kind: carrier-pigeon\n"
        "  value: ''\n"
    )

    with pytest.raises(hosts.ManifestError, match="identity"):
        hosts.load(hq_dir, "bad-identity")


# ---- path helpers -----------------------------------------------------------------


def test_manifest_path_is_hosts_dir_slash_host_id_yaml(tmp_path):
    hq_dir = tmp_path / "hq"
    assert hosts.manifest_path(hq_dir, "abc") == hq_dir / "hosts" / "abc.yaml"
    assert hosts.hosts_dir(hq_dir) == hq_dir / "hosts"


def test_save_creates_the_hosts_directory_if_missing(tmp_path):
    hq_dir = tmp_path / "hq"
    assert not hosts.hosts_dir(hq_dir).exists()

    hosts.save(hq_dir, _manifest())

    assert hosts.hosts_dir(hq_dir).is_dir()
    assert hosts.manifest_path(hq_dir, "11111111-1111-4111-8111-111111111111").exists()


# ---- bh-7ztwe: the rename must not strand an already-registered host ----------
#
# The role vocabulary was renamed because `worker` named the ONE role that can do no work, and
# three independent readers in one day assumed the opposite (v0.8.0 shipped docs saying so).
# An HQ manifest carries the role STRING, so the rename lands behind aliases: the failure mode
# it must not have is a v0.8.1 clone refusing to parse manifests v0.8.0 wrote.


def _write_manifest(hq_dir, host_id, role):
    (hosts.hosts_dir(hq_dir) / f"{host_id}.yaml").write_text(
        f"host_id: {host_id}\nlabel: box\nos: linux\narch: x86_64\nrole: {role}\n"
        "identity:\n  kind: none\n  value: ''\n"
    )


@pytest.mark.parametrize(
    "deprecated,current",
    [("primary-default", "executor"), ("adopt-on-demand", "transient"), ("worker", "viewer")],
)
def test_a_manifest_written_by_v0_8_0_still_parses(tmp_path, deprecated, current):
    """The load path, which is the one that strands hosts: `role:` comes off disk as the old
    word and must come out of the model as the new one."""
    hq_dir = tmp_path / "hq"
    hosts.hosts_dir(hq_dir).mkdir(parents=True)
    _write_manifest(hq_dir, "old-host", deprecated)

    assert hosts.load(hq_dir, "old-host").role == current


def test_an_unknown_role_is_still_rejected(tmp_path):
    """Aliasing resolves KNOWN old spellings; it must not become a hole that lets any string
    through — a typo has to fail validation, not land on a silent default."""
    hq_dir = tmp_path / "hq"
    hosts.hosts_dir(hq_dir).mkdir(parents=True)
    _write_manifest(hq_dir, "typo", "wokrer")

    with pytest.raises(hosts.ManifestError, match="role"):
        hosts.load(hq_dir, "typo")


def test_canonical_role_passes_an_unknown_value_through_untouched():
    assert hosts.canonical_role("nonsense") == "nonsense"
    assert hosts.canonical_role("executor") == "executor"
