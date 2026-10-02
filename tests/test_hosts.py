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


def test_remote_only_hives_round_trip_as_host_local_placement_intent(tmp_path):
    hq_dir = tmp_path / "hq"
    manifest = _manifest(remote_only_hives=["hl", "orca"])

    hosts.save(hq_dir, manifest)

    assert hosts.load(hq_dir, manifest.host_id).remote_only_hives == ["hl", "orca"]


def test_mutated_roster_candidate_cannot_replace_prior_file(tmp_path):
    manifest = _manifest()
    path = hosts.save(tmp_path, manifest)
    prior = path.read_bytes()
    manifest.role = "invalid-role"
    with pytest.raises(hosts.ManifestError, match="role"):
        hosts.save(tmp_path, manifest)
    assert path.read_bytes() == prior


def test_roster_read_rejects_mismatched_path_identity(tmp_path):
    manifest = _manifest()
    path = hosts.save(tmp_path, manifest)
    other = hosts.manifest_path(tmp_path, "other-host")
    other.write_bytes(path.read_bytes())
    with pytest.raises(hosts.ManifestError, match="path identity mismatch"):
        hosts.load(tmp_path, "other-host")


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


def test_neutral_manifest_reexport_preserves_structured_deprecated_role_warning():
    import io
    import json

    from beadhive import log
    from beadhive.host_manifest_contracts import HostManifest

    assert hosts.HostManifest is HostManifest
    output = io.StringIO()
    log.configure(level="WARNING", fmt="json", stream=output)
    try:
        assert hosts.canonical_role("worker") == "viewer"
    finally:
        log.configure()
    warning = json.loads(output.getvalue().splitlines()[-1])
    assert warning["event"] == "deprecated_host_role"
    assert warning["deprecated"] == "worker"
    assert warning["replacement"] == "viewer"
    assert "bh host init --role viewer --force" in warning["reason"]


# Captured from the factory HQ on 2026-09-30, before frame membership fields.
def test_real_pre_frame_hq_manifest_loads_unchanged_as_active(tmp_path):
    from pathlib import Path

    source = Path(__file__).parent / "fixtures/hosts/pre-frame-factory.yaml"
    target = hosts.manifest_path(tmp_path, "6ae345b9-81a8-4c9b-8661-c5a4420fc12d")
    target.parent.mkdir(parents=True)
    target.write_bytes(source.read_bytes())
    loaded = hosts.load(tmp_path, target.stem)
    assert loaded.state == "active"
    assert loaded.frame_id is None
    assert loaded.label == "beadhive-factory"
    assert loaded.capacity == loaded.harnesses == {}
    assert target.read_bytes() == source.read_bytes()


@pytest.mark.parametrize("state", hosts.FRAME_STATES)
def test_frame_membership_round_trips_every_state(tmp_path, state):
    manifest = _manifest(
        frame_id="frame-01",
        state=state,
        instance_ref="vm-123",
        release={"id": "v0.20.2", "digest": "sha256:abc"},
        capabilities={
            "isolation": "kvm",
            "trust_zone": "self-hosted",
            "arch": "x86_64",
            "harnesses": ["claude", "codex"],
            "max_sessions": 2,
        },
    )
    hosts.save(tmp_path, manifest)
    assert hosts.load(tmp_path, manifest.host_id) == manifest


@pytest.mark.parametrize("isolation", hosts.FRAME_ISOLATIONS)
@pytest.mark.parametrize("trust_zone", hosts.FRAME_TRUST_ZONES)
def test_frame_capability_enums(isolation, trust_zone):
    caps = hosts.FrameCapabilities(
        isolation=isolation, trust_zone=trust_zone, arch="aarch64", harnesses=[], max_sessions=0
    )
    assert caps.isolation == isolation
    assert caps.trust_zone == trust_zone


def test_new_frame_is_written_pending_but_ordinary_host_stays_active(tmp_path):
    frame = _manifest(frame_id="frame-new")
    assert frame.state == "pending"
    hosts.save(tmp_path, frame)
    assert hosts.load(tmp_path, frame.host_id).state == "pending"
    assert "state: pending" in hosts.manifest_path(tmp_path, frame.host_id).read_text()
    assert _manifest().state == "active"


@pytest.mark.parametrize(
    "field,value", [("state", "stale"), ("state", "eligible"), ("stale", True), ("eligible", True)]
)
def test_derived_or_unknown_frame_state_is_not_stored(field, value):
    with pytest.raises(ValidationError):
        _manifest(**{field: value})


@pytest.mark.parametrize(
    "extra", [{"bogus": 1}, {"isolation": "vm"}, {"trust_zone": "public"}, {"max_sessions": -1}]
)
def test_capabilities_reject_unknown_fields_and_invalid_values(extra):
    with pytest.raises(ValidationError):
        hosts.FrameCapabilities.model_validate(
            {
                "isolation": "container",
                "trust_zone": "vendor-hosted",
                "arch": "x86_64",
                "harnesses": [],
                "max_sessions": 1,
                **extra,
            }
        )


def test_release_rejects_unknown_fields():
    with pytest.raises(ValidationError):
        hosts.FrameRelease(id="v1", digest="abc", bogus=True)


def test_frame_contract_names_and_closed_enums():
    assert hosts.FRAME_STATES == (
        "pending",
        "active",
        "draining",
        "drained",
        "parked",
        "quarantined",
        "retired",
    )
    assert hosts.FRAME_ISOLATIONS == ("kvm", "microvm", "container")
    assert hosts.FRAME_TRUST_ZONES == ("self-hosted", "vendor-hosted")
    assert set(hosts.FrameRelease.model_fields) == {"id", "digest"}
    assert set(hosts.FrameCapabilities.model_fields) == {
        "isolation",
        "trust_zone",
        "arch",
        "harnesses",
        "max_sessions",
    }


def test_reading_historical_frame_without_state_is_active(tmp_path):
    manifest = _manifest(frame_id="existing-frame")
    data = manifest.model_dump(mode="json")
    del data["state"]
    target = hosts.manifest_path(tmp_path, manifest.host_id)
    target.parent.mkdir(parents=True)
    import json

    target.write_text(json.dumps(data))
    assert hosts.load(tmp_path, manifest.host_id).state == "active"
