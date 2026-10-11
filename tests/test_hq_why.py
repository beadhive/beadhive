"""``bh hq why <hive>`` — the first refusing layer and the one command that fixes it (bh-4pki5).

Every branch the acceptance names is covered on a fixture: config unavailable, capability
<name> refused by predicate <p>, not placed (holder, cause), the fence naming another frame,
and ok — each with its command, in text and ``--json``. The collaborators (registry, host
identity, forward marker, writer fence, eligibility decision, leases) are replaced at the
module attribute the layers read, so no HQ, config or clock is touched.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import pytest
import typer
from typer.testing import CliRunner

from beadhive import (
    capabilities,
    cli,
    config,
    frame_eligibility,
    guard,
    host,
    hq_why,
    hq_why_layer_core,
    registry,
)
from beadhive.frame_eligibility import EligibilityDecision

PREFIX = "tt"
THIS_HOST = "11111111-1111-4111-8111-111111111111"
OTHER_HOST = "22222222-2222-4222-8222-222222222222"
FRAME = "frame-this-01"
CFG = {"managed_repos": []}


@dataclass(frozen=True)
class FakeLease:
    """A host lease whose expiry is a fixed answer, never a clock reading."""

    host_id: str
    expired: bool = False
    label: str = "builder"
    epoch: int = 7
    expires_at: str = "2026-10-10T00:00:00Z"

    @property
    def is_tombstone(self) -> bool:
        return not self.host_id

    def is_expired(self) -> bool:
        return self.is_tombstone or self.expired

    def held_by(self, host_id: str) -> bool:
        return bool(host_id) and host_id == self.host_id

    def describe(self) -> str:
        if self.is_tombstone:
            return f"released (no holder; epoch {self.epoch})"
        return f"{self.label} ({self.host_id}), epoch {self.epoch}, expires {self.expires_at}"


@dataclass(frozen=True)
class FakeMarker:
    prefix: str
    state: str
    frame: str = "frame-primary"
    reason: str = ""


@dataclass(frozen=True)
class FakeWriter:
    frame: str
    epoch: int


def decision(**failing: bool) -> EligibilityDecision:
    """A full fourteen-predicate decision with the named predicates set as given."""
    return EligibilityDecision(
        tuple((name, failing.get(name, True)) for name in capabilities.FRAME_PREDICATES)
    )


@pytest.fixture
def world(tmp_path, monkeypatch):
    """A registered, legacy (unenrolled, never-adopted, not cut-over) hive: everything allows.

    Tests flip one collaborator to reach one branch."""
    entry = {"provider": "github", "org": "o", "repo": "r", "prefix": PREFIX}
    monkeypatch.setattr(registry, "hive_dir_for", lambda _cfg, _hive: tmp_path / "hive")
    monkeypatch.setattr(registry, "entry_for_dir", lambda _cfg, _dir: entry)
    monkeypatch.setattr(host, "host_id", lambda: THIS_HOST)
    monkeypatch.setattr(host, "enrolled_frame_id", lambda: FRAME)
    monkeypatch.setattr(guard, "forward_marker", lambda *_a, **_k: None)
    monkeypatch.setattr(guard, "writer_state", lambda *_a, **_k: None)
    monkeypatch.setattr(frame_eligibility, "decision_for", lambda *_a, **_k: None)
    monkeypatch.setattr(guard, "primary_state", lambda *_a, **_k: None)
    monkeypatch.setattr(config, "load", lambda: CFG)
    return monkeypatch


def why(capability: str = "publish", hive: str = PREFIX) -> hq_why.WhyReport:
    return hq_why.walk(hq_why.WhyContext(hive, capability))


def statuses(report: hq_why.WhyReport) -> dict[str, str]:
    return {step.layer: step.status for step in report.walked}


# ---- ok ---------------------------------------------------------------------------------


def test_legacy_single_host_hive_is_ok_with_a_next_command(world):
    report = why()
    assert report.allowed
    assert report.verdict.code == "ok"
    assert report.verdict.command == f"{config.BINARY_ALIAS} host eligible --hive {PREFIX}"
    assert report.host == THIS_HOST
    assert statuses(report) == {
        "config": "ok",
        "forward": "skipped",
        "capability": "ok",
        "placement": "skipped",
        "fence": "skipped",
        "tenure": "skipped",  # bh-ofszz's provider: no elected-tenure cache on this host
        "profile": "skipped",  # bh-bc2rj's provider: no trust-profile override refused
    }


def test_enrolled_frame_holding_the_lease_is_ok(world):
    world.setattr(frame_eligibility, "decision_for", lambda *_a, **_k: decision())
    world.setattr(
        frame_eligibility,
        "authoritative_primary",
        lambda *_a, **_k: (PREFIX, THIS_HOST, FakeLease(THIS_HOST)),
    )
    report = why()
    assert report.allowed and report.verdict.code == "ok"
    assert statuses(report)["placement"] == "ok"


# ---- config unavailable -----------------------------------------------------------------


def test_config_snapshot_that_does_not_load_is_config_unavailable(world):
    def boom():
        raise ValueError("committed configuration unavailable")

    world.setattr(config, "load", boom)
    report = why()
    assert not report.allowed
    assert (report.verdict.layer, report.verdict.code) == ("config", "config_unavailable")
    assert "did not load" in report.verdict.summary
    assert report.verdict.command == f"{config.BINARY_ALIAS} doctor"
    assert report.host == THIS_HOST  # named even though the walk stopped first
    assert [step.layer for step in report.walked] == ["config"]


def test_unregistered_hive_is_config_unavailable(world):
    world.setattr(registry, "entry_for_dir", lambda _cfg, _dir: None)
    report = why(hive="nope")
    assert report.verdict.code == "config_unavailable"
    assert report.verdict.command == f"{config.BINARY_ALIAS} hive list"
    assert report.hive == "nope"


def test_unresolvable_hive_is_config_unavailable(world):
    def unresolved(_cfg, _hive):
        raise ValueError("no hive matches 'nope'")

    world.setattr(registry, "hive_dir_for", unresolved)
    report = why(hive="nope")
    assert report.verdict.code == "config_unavailable"
    assert report.verdict.detail["error"] == "no hive matches 'nope'"


# ---- forward ----------------------------------------------------------------------------


def test_forwarding_checkout_is_decided_by_the_primary(world):
    world.setattr(guard, "forward_marker", lambda *_a, **_k: FakeMarker(PREFIX, "forwarding"))
    report = why()
    assert report.allowed
    assert (report.verdict.layer, report.verdict.status) == ("forward", "decided")
    assert [step.layer for step in report.walked] == ["config", "forward"]


def test_refused_forwarding_points_at_forward_point(world):
    marker = FakeMarker(PREFIX, "refused", reason="primary epoch moved")
    world.setattr(guard, "forward_marker", lambda *_a, **_k: marker)
    report = why()
    assert report.verdict.code == "forward_refused"
    assert report.verdict.command == f"{config.BINARY_ALIAS} hive forward point {PREFIX}"
    assert "primary epoch moved" in report.verdict.summary


# ---- capability refused by predicate ----------------------------------------------------


def test_capability_refused_names_the_capability_predicate_and_fix(world):
    world.setattr(frame_eligibility, "decision_for", lambda *_a, **_k: decision(not_cordoned=False))
    report = why("author")
    assert (report.verdict.layer, report.verdict.code) == ("capability", "capability_refused")
    assert report.verdict.summary == "capability author refused by predicate not_cordoned"
    assert report.verdict.command == f"{config.BINARY_ALIAS} host resume plan {FRAME}"
    assert report.verdict.detail["refused_by"] == ["not_cordoned"]


def test_capability_layer_reuses_the_per_capability_narrowing(world):
    """A stale heartbeat refuses ``publish`` but not ``author`` (bh-b3ebg's decision)."""
    world.setattr(
        frame_eligibility,
        "decision_for",
        lambda *_a, **_k: decision(authenticated_fresh_heartbeat=False),
    )
    world.setattr(
        frame_eligibility,
        "authoritative_primary",
        lambda *_a, **_k: (PREFIX, THIS_HOST, FakeLease(THIS_HOST)),
    )
    assert why("author").allowed
    refused = why("publish")
    assert refused.verdict.summary.endswith("predicate authenticated_fresh_heartbeat")
    assert refused.verdict.command == f"{config.BINARY_ALIAS} host daemon status"
    report = refused.verdict.detail["capabilities"]
    assert report["author"]["allowed"] and not report["publish"]["allowed"]


def test_every_frame_predicate_has_a_fix_command():
    assert set(hq_why_layer_core.PREDICATE_FIXES) == set(capabilities.FRAME_PREDICATES)


def test_unknown_predicate_falls_back_to_host_eligible():
    fix = hq_why_layer_core.predicate_fix("hive_catalog_available", frame="f", host="h", hive="x")
    assert fix == f"{config.BINARY_ALIAS} host eligible h --hive x"


# ---- not placed -------------------------------------------------------------------------


def test_enrolled_frame_with_a_live_foreign_lease_is_not_placed(world):
    world.setattr(frame_eligibility, "decision_for", lambda *_a, **_k: decision())
    world.setattr(
        frame_eligibility,
        "authoritative_primary",
        lambda *_a, **_k: (PREFIX, THIS_HOST, FakeLease(OTHER_HOST)),
    )
    report = why()
    assert (report.verdict.layer, report.verdict.code) == ("placement", "not_placed")
    assert report.verdict.detail["holder"] == OTHER_HOST
    assert report.verdict.detail["cause"].startswith("held by builder")
    assert report.verdict.command == f"{config.BINARY_ALIAS} hq placement show {PREFIX}"


def test_enrolled_frame_with_no_lease_adopts_here(world):
    world.setattr(frame_eligibility, "decision_for", lambda *_a, **_k: decision())
    world.setattr(
        frame_eligibility, "authoritative_primary", lambda *_a, **_k: (PREFIX, THIS_HOST, None)
    )
    report = why()
    assert report.verdict.code == "not_placed"
    assert report.verdict.detail == {
        "holder": "",
        "cause": "nobody holds the hive lease",
        "expires_at": "",
        "epoch": 0,
        "source": guard.SOURCE_HQ,
        "this_host": THIS_HOST,
    }
    assert report.verdict.command == f"{config.BINARY_ALIAS} host adopt {PREFIX}"


def test_unreadable_authoritative_lease_is_not_placed(world):
    world.setattr(frame_eligibility, "decision_for", lambda *_a, **_k: decision())

    def unreadable(*_a, **_k):
        raise frame_eligibility.EligibilityError(
            "frame ineligible: authoritative_hive_lease_available"
        )

    world.setattr(frame_eligibility, "authoritative_primary", unreadable)
    report = why()
    assert report.verdict.code == "not_placed"
    assert "authoritative_hive_lease_available" in report.verdict.summary


def test_legacy_live_foreign_lease_is_not_placed(world):
    lease = FakeLease(OTHER_HOST)
    world.setattr(guard, "primary_state", lambda *_a, **_k: (PREFIX, THIS_HOST, lease))
    world.setattr(guard, "_refresh_expired", lambda _p, cached: (cached, guard.SOURCE_CACHED))
    report = why()
    assert report.verdict.code == "not_placed"
    assert report.verdict.detail["source"] == guard.SOURCE_CACHED
    assert report.verdict.command == (f"{config.BINARY_ALIAS} host list --lease-hive {PREFIX}")


def test_legacy_expired_lease_names_the_cause_and_adopt(world):
    lease = FakeLease(OTHER_HOST, expired=True)
    world.setattr(guard, "primary_state", lambda *_a, **_k: (PREFIX, THIS_HOST, lease))
    world.setattr(guard, "_refresh_expired", lambda _p, cached: (cached, guard.SOURCE_HQ))
    report = why()
    assert report.verdict.detail["cause"].startswith("the hive lease EXPIRED")
    assert report.verdict.command == f"{config.BINARY_ALIAS} host adopt {PREFIX}"


def test_legacy_stale_cache_refreshed_in_this_hosts_favour_is_ok(world):
    lease = FakeLease(OTHER_HOST, expired=True)
    world.setattr(guard, "primary_state", lambda *_a, **_k: (PREFIX, THIS_HOST, lease))
    world.setattr(
        guard, "_refresh_expired", lambda _p, _cached: (FakeLease(THIS_HOST), guard.SOURCE_HQ)
    )
    assert why().allowed


# ---- writer fence -----------------------------------------------------------------------


def test_fence_naming_another_frame_refuses_and_skips_lease_layers(world):
    state = (PREFIX, FRAME, FakeWriter("frame-other-02", 12))
    world.setattr(guard, "writer_state", lambda *_a, **_k: state)
    world.setattr(frame_eligibility, "decision_for", lambda *_a, **_k: decision(not_cordoned=False))
    report = why()
    assert (report.verdict.layer, report.verdict.code) == ("fence", "fence_foreign")
    assert "frame-other-02 at epoch 12" in report.verdict.summary
    assert report.verdict.command == f"{config.BINARY_ALIAS} host adopt {PREFIX}"
    assert statuses(report)["capability"] == "skipped"
    assert statuses(report)["placement"] == "skipped"


def test_fence_naming_this_frame_is_ok(world):
    world.setattr(guard, "writer_state", lambda *_a, **_k: (PREFIX, FRAME, FakeWriter(FRAME, 3)))
    report = why()
    assert report.allowed and statuses(report)["fence"] == "ok"


def test_unreadable_fence_fails_closed(world):
    def unreadable(*_a, **_k):
        raise guard.WriterUnreadable("tt: cannot read the local bh_writer: boom")

    world.setattr(guard, "writer_state", unreadable)
    report = why()
    assert report.verdict.code == "fence_unreadable"
    assert statuses(report)["capability"] == "skipped"


# ---- the walk itself --------------------------------------------------------------------


def test_a_layer_that_raises_is_reported_as_refusing(world):
    def broken(*_a, **_k):
        raise RuntimeError("HQ clone corrupt")

    world.setattr(frame_eligibility, "decision_for", broken)
    report = why()
    assert (report.verdict.layer, report.verdict.code) == ("capability", "layer_error")
    assert not report.allowed


def test_core_layers_are_discovered_in_adr_order():
    names = [layer.name for layer in hq_why.layers()]
    assert names[:5] == ["config", "forward", "capability", "placement", "fence"]


def test_a_provider_registers_a_layer_between_the_built_ins(world):
    """Later beads (frame-ref merge conflicts, profile overrides) add a layer by registering
    it; the walk visits it in order without any edit to the walk or the core layers."""

    def conflict(_ctx):
        return hq_why.Verdict(
            "frame-refs", hq_why.REFUSED, "frame_ref_conflict", "two frames claim tt-1", "bh x"
        )

    layer = hq_why.register_layer(hq_why.Layer("frame-refs", 35, conflict))
    try:
        report = why()
        assert [step.layer for step in report.walked] == [
            "config",
            "forward",
            "capability",
            "placement",
            "frame-refs",
        ]
        assert report.verdict.code == "frame_ref_conflict"
        with pytest.raises(hq_why.DuplicateLayer):
            hq_why.register_layer(hq_why.Layer("frame-refs", 35, conflict))
        assert hq_why.register_layer(layer) is layer  # re-registering the same object is fine
    finally:
        hq_why.unregister_layer("frame-refs")


def test_verdict_rejects_an_unknown_status():
    with pytest.raises(ValueError):
        hq_why.Verdict("x", "maybe", "c", "s")


# ---- CLI ----------------------------------------------------------------------------------


def test_cli_json_carries_the_decision_and_every_layer(world):
    world.setattr(
        frame_eligibility, "decision_for", lambda *_a, **_k: decision(release_matches=False)
    )
    result = CliRunner().invoke(cli.app, ["hq", "why", PREFIX, "--json"])
    assert result.exit_code == 1, result.output
    payload = json.loads(result.stdout)
    assert payload["allowed"] is False
    assert payload["layer"] == "capability"
    assert payload["code"] == "capability_refused"
    assert payload["summary"] == "capability publish refused by predicate release_matches"
    assert payload["command"] == f"{config.BINARY_ALIAS} host release-upgrade plan {FRAME}"
    assert payload["host"] == THIS_HOST
    assert [row["layer"] for row in payload["layers"]] == [
        "config",
        "forward",
        "capability",
    ]


def test_cli_text_prints_the_layer_and_next_command(world):
    result = CliRunner().invoke(cli.app, ["hq", "why", PREFIX, "-c", "author"])
    assert result.exit_code == 0, result.output
    assert f"✓ {PREFIX} · author" in result.stdout
    assert f"next: {config.BINARY_ALIAS} host eligible --hive {PREFIX}" in result.stdout


def test_cli_rejects_a_non_write_capability(world):
    result = CliRunner().invoke(cli.app, ["hq", "why", PREFIX, "-c", "admin"])
    assert result.exit_code == 2


def test_help_never_loads_config_or_hq(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(cli, "_init_telemetry_best_effort", lambda: calls.append("load"))
    monkeypatch.setattr(cli, "_migrate_hive_keys_best_effort", lambda: calls.append("migrate"))
    monkeypatch.setattr(cli, "_migrate_home_best_effort", lambda: calls.append("home"))
    monkeypatch.setattr(config, "load", lambda: calls.append("config") or {})
    monkeypatch.setattr(config, "hq_dir", lambda: calls.append("hq") or None)
    monkeypatch.setattr(cli.sys, "argv", ["bh", "hq", "why", "--help"])

    result = CliRunner().invoke(cli.app, ["hq", "why", "--help"])

    assert result.exit_code == 0, result.output
    assert "Usage" in result.stdout
    assert calls == []


# ---- the guard's refusals point here ----------------------------------------------------


def test_guard_capability_refusal_ends_with_the_why_pointer(world, capsys):
    world.setattr(
        guard, "writer_state", lambda *_a, **_k: (PREFIX, FRAME, FakeWriter("frame-other", 2))
    )
    with pytest.raises(typer.Exit):
        guard.guard_capability("author", PREFIX, cfg=CFG)
    err = capsys.readouterr().err.rstrip("\n")
    assert err.endswith(f"{config.BINARY_ALIAS} hq why {PREFIX}")


def test_guard_primary_refusal_ends_with_the_why_pointer(world, capsys):
    def ineligible(*_a, **_k):
        raise frame_eligibility.EligibilityError("frame x ineligible: not_cordoned")

    world.setattr(frame_eligibility, "require_intake", ineligible)
    with pytest.raises(typer.Exit):
        guard.guard_primary(PREFIX, cfg=CFG)
    err = capsys.readouterr().err.rstrip("\n")
    assert err.splitlines()[0] == "frame x ineligible: not_cordoned"
    assert err.endswith(f"{config.BINARY_ALIAS} hq why {PREFIX}")
