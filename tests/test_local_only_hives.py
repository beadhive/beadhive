"""Local-only hive registration (bh-sj2bf; docs/design/local-only-hives.md, bh-ndkos).

A hive whose bead database was never published carries ``managed_repos[].local_only``
``{owner_host, reason}``. Other frames skip it in ``hives-ready`` and stay eligible; only the
owner may lease or claim it. Hermetic: no gh, no bd, no remotes, no wall clock.
"""

from __future__ import annotations

import json

import pytest
import typer
from pydantic import ValidationError

from beadhive import config, guard, heartbeat_report, host_lease, registry
from beadhive import frame_eligibility as policy
from beadhive.modules.config.contracts import BeadhiveConfig, ManagedRepoEntry

OWNER = "6f1c0000-0000-4000-8000-000000000001"
OTHER = "6f1c0000-0000-4000-8000-000000000002"


def _local(prefix="tl", owner=OWNER, reason="push-denied", **extra):
    return {
        "provider": "github",
        "org": "acme",
        "repo": f"repo-{prefix}",
        "prefix": prefix,
        "kind": "prototype",
        "local_only": {"owner_host": owner, "reason": reason},
        **extra,
    }


def _plain(prefix="rp"):
    return {
        "provider": "github",
        "org": "acme",
        "repo": f"repo-{prefix}",
        "prefix": prefix,
        "kind": "prototype",
    }


# ---- R1: the field -------------------------------------------------------------------------


@pytest.mark.parametrize("reason", ["push-denied", "opt-in", "external", "migrated"])
def test_marker_validates_each_reason(reason):
    entry = ManagedRepoEntry.model_validate(_local(reason=reason))
    assert entry.local_only.owner_host == OWNER and entry.local_only.reason == reason


def test_absent_marker_is_replicated_and_unchanged():
    assert ManagedRepoEntry.model_validate(_plain()).local_only is None
    BeadhiveConfig(managed_repos=[_plain(), _local()])


@pytest.mark.parametrize(
    "marker",
    [
        {"owner_host": "", "reason": "opt-in"},
        {"owner_host": " padded ", "reason": "opt-in"},
        {"owner_host": OWNER, "reason": "replicated"},
        {"owner_host": OWNER},
        {"reason": "opt-in"},
        {"owner_host": OWNER, "reason": "opt-in", "enabled": False},
    ],
)
def test_marker_rejects_invalid_shapes(marker):
    with pytest.raises(ValidationError):
        ManagedRepoEntry.model_validate({**_plain(), "local_only": marker})


@pytest.mark.parametrize(
    "extra",
    [
        {"dolt_remote": {"url": "s3://acme-beads/beads/tl"}},
        {"stores": {"internal": {"remote": "tl-private"}}},
    ],
)
def test_marker_refused_beside_hq_remote_or_stores(extra):
    with pytest.raises(ValidationError, match="local_only cannot be combined"):
        ManagedRepoEntry.model_validate(_local(**extra))


def test_marker_is_not_register_owned():
    assert registry.LOCAL_ONLY_KEY not in registry.REGISTER_OWNED_KEYS


# ---- registry helpers ----------------------------------------------------------------------


def test_refusal_is_none_without_marker_or_for_the_owner():
    assert registry.local_only_refusal(_plain(), OTHER) is None
    assert registry.local_only_refusal(_local(), OWNER) is None


def test_refusal_names_owner_label_and_id(monkeypatch):
    monkeypatch.setattr(registry, "host_label", lambda hid: "studio" if hid == OWNER else hid)
    text = registry.local_only_refusal(_local(), OTHER)
    assert text == (
        f"hive tl is local-only on host studio ({OWNER}); only that host may lease or claim "
        "its beads"
    )


def test_refusal_fails_closed_without_a_host_id(monkeypatch):
    monkeypatch.setattr(registry, "host_label", lambda hid: hid)
    assert registry.local_only_refusal(_local(), None)
    assert registry.local_only_refusal(_local(), "")
    assert registry.local_only_blocks(_local(), None)


def test_host_label_falls_back_to_the_id(monkeypatch):
    def boom():
        raise FileNotFoundError("no hq")

    monkeypatch.setattr(config, "hq_dir", boom)
    assert registry.host_label(OWNER) == OWNER


def test_marker_never_recorded_with_an_empty_owner():
    with pytest.raises(ValueError, match="never recorded empty"):
        registry.local_only_marker("  ", "opt-in")


FLOW = (
    "providers: [github]\n"
    "orgs:\n"
    "  acme: {code: ac, policy: personal}\n"
    "managed_repos:\n"
    '  - {"provider": "github", "org": "acme", "repo": "tool", "prefix": "tl", '
    '"kind": "prototype"}\n'
)


def _entry_json(org="acme", repo="tool"):
    entry = registry.find_entry(config.load(), "github", org, repo)
    return json.loads(json.dumps(entry))


def test_register_writes_the_marker_flow_style(world):
    registry.register(
        "github",
        "acme",
        "gadget",
        "gad",
        "prototype",
        local_only={"owner_host": OWNER, "reason": "push-denied"},
    )
    assert _entry_json(repo="gadget")["local_only"] == {
        "owner_host": OWNER,
        "reason": "push-denied",
    }
    line = next(x for x in config.config_path().read_text().splitlines() if "gadget" in x)
    assert f'"local_only": {{"owner_host": "{OWNER}", "reason": "push-denied"}}' in line


def test_force_reregister_carries_the_marker(world):
    registry.register(
        "github",
        "acme",
        "gadget",
        "gad",
        "prototype",
        local_only={"owner_host": OWNER, "reason": "opt-in"},
    )
    registry.register("github", "acme", "gadget", "gad", "personal")
    entry = _entry_json(repo="gadget")
    assert entry["kind"] == "personal"
    assert entry["local_only"] == {"owner_host": OWNER, "reason": "opt-in"}


def test_set_local_only_writes_and_deletes_preserving_other_keys(world):
    config.config_path().write_text(FLOW)
    registry.set_local_only("github", "acme", "tool", {"owner_host": OWNER, "reason": "migrated"})
    assert _entry_json()["local_only"] == {"owner_host": OWNER, "reason": "migrated"}
    registry.set_local_only("github", "acme", "tool", None)
    entry = _entry_json()
    assert "local_only" not in entry and entry["prefix"] == "tl"


def test_set_local_only_refuses_an_unregistered_hive(world):
    config.config_path().write_text(FLOW)
    with pytest.raises(LookupError):
        registry.set_local_only("github", "acme", "nope", None)


# ---- R4: hives-ready -----------------------------------------------------------------------


def _measure(monkeypatch, entries, host_id, ready_prefixes, caps=None):
    monkeypatch.setattr(heartbeat_report, "config_valid", lambda: True)
    seen = []

    def ready(entry):
        seen.append(entry["prefix"])
        return entry["prefix"] in ready_prefixes

    monkeypatch.setattr(heartbeat_report, "hive_ready", ready)
    checks = heartbeat_report.measure_conformance({"managed_repos": entries}, caps, host_id)
    return {c["id"]: c for c in checks}["hives-ready"], seen


def test_non_owner_skips_local_only_hive_with_evidence(monkeypatch):
    check, seen = _measure(monkeypatch, [_local(), _plain()], OTHER, {"rp"})
    assert check["status"] == "pass"
    assert seen == ["rp"]
    assert check["evidence"] == ("measured check passed; skipped (local-only on another host): tl")
    assert OWNER not in check["evidence"]


def test_non_owning_frame_stays_eligible_when_only_local_only_hives_exist(monkeypatch):
    check, seen = _measure(monkeypatch, [_local()], OTHER, set())
    assert check["status"] == "pass" and seen == []


def test_owner_still_requires_its_local_only_hive(monkeypatch):
    check, seen = _measure(monkeypatch, [_local()], OWNER, set())
    assert check["status"] == "fail" and seen == ["tl"]


def test_unreadable_host_id_fails_closed(monkeypatch):
    check, seen = _measure(monkeypatch, [_local()], None, set())
    assert check["status"] == "fail" and seen == ["tl"]


def test_default_host_id_is_this_hosts(monkeypatch):
    monkeypatch.setattr(registry, "this_host_id", lambda: OTHER)
    monkeypatch.setattr(heartbeat_report, "config_valid", lambda: True)
    monkeypatch.setattr(heartbeat_report, "hive_ready", lambda e: False)
    checks = heartbeat_report.measure_conformance({"managed_repos": [_local()]}, None)
    assert {c["id"]: c for c in checks}["hives-ready"]["status"] == "pass"


def test_hive_both_local_only_and_policy_excluded_is_listed_once(monkeypatch):
    arm = {"frame_policy": {"requires": {"arch": "arm64"}}}
    entries = [_local(**arm), _plain("ab") | arm]
    caps = {"arch": "x86_64", "harnesses": [], "max_sessions": 1}
    check, _ = _measure(monkeypatch, entries, OTHER, set(), caps)
    assert check["evidence"] == (
        "measured check passed; skipped (frame_policy excludes this frame): ab; "
        "skipped (local-only on another host): tl"
    )


def test_evidence_truncates_long_prefix_lists():
    names = [f"hive{i:04d}" for i in range(300)]
    text = heartbeat_report._skip_evidence([("local-only on another host", names)])
    assert len(text) <= heartbeat_report.EVIDENCE_CAP
    assert text.endswith(f"(+{300 - text.count('hive')})")
    assert ", … (+" in text


# ---- R5: eligibility, leases and claims ----------------------------------------------------


def test_lease_adopt_refused_for_non_owner_even_forced(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "require_eligible", lambda *a, **k: None)  # legacy host
    monkeypatch.setattr(config, "load", lambda: {"managed_repos": [_local()]})
    monkeypatch.setattr(registry, "host_label", lambda hid: "studio")
    reads = []
    monkeypatch.setattr(host_lease, "_read", lambda *a, **k: reads.append(a) or ("", None))
    for force in (False, True):
        with pytest.raises(host_lease.HostLeaseRejected, match="local-only on host studio"):
            host_lease.adopt("origin", "tl", host_id=OTHER, label="x", cwd=tmp_path, force=force)
        with pytest.raises(host_lease.HostLeaseRejected):
            host_lease.takeover("origin", "tl", host_id=OTHER, label="x", cwd=tmp_path, force=force)
    assert reads == []  # refused before HQ is read or written


def test_lease_adopt_allowed_for_the_owner(monkeypatch, tmp_path):
    monkeypatch.setattr(policy, "require_eligible", lambda *a, **k: None)
    monkeypatch.setattr(config, "load", lambda: {"managed_repos": [_local()]})
    host_lease.refuse_local_only("tl", OWNER)  # does not raise
    host_lease.refuse_local_only("rp", OTHER)  # unknown prefix: nothing to refuse


def test_legacy_guard_refuses_a_non_owner_claim(monkeypatch):
    monkeypatch.setattr(guard, "writer_state", lambda *a, **k: None)
    monkeypatch.setattr(policy, "require_intake", lambda *a, **k: None)
    monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: "/nowhere")
    monkeypatch.setattr(registry, "entry_for_dir", lambda cfg, d: _local())
    monkeypatch.setattr(registry, "this_host_id", lambda: OTHER)
    monkeypatch.setattr(registry, "host_label", lambda hid: "studio")
    with pytest.raises(typer.Exit):
        guard.guard_primary("tl", cfg={"managed_repos": [_local()]})


def test_legacy_guard_admits_the_owner(monkeypatch):
    monkeypatch.setattr(guard, "writer_state", lambda *a, **k: None)
    monkeypatch.setattr(policy, "require_intake", lambda *a, **k: None)
    monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: "/nowhere")
    monkeypatch.setattr(registry, "entry_for_dir", lambda cfg, d: _local())
    monkeypatch.setattr(registry, "this_host_id", lambda: OWNER)
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: None)
    guard.guard_primary("tl", cfg={"managed_repos": [_local()]})
