"""Self-placement for `bh host lease adopt` under the fleet trust profiles (bh-vl0r3).

ADR ``hq-trust-profiles-and-capabilities-adr.md`` section 2, placement:

* ``open`` / ``trusted`` (or ``hq.placement_mode: self``): a frame places itself on a hive with
  no live writer session in ONE adopt — seed a never-placed record, CAS placement at the
  revision it read, run the fence bump — with no director credential and no expected revision.
* a live writer is refused naming the holder, when its tenure ends and when failover fires;
  under ``trusted`` the refusal prints the director command, under ``open`` it prints none.
* ``verified`` / ``attested``: self-placement is refused and the director command printed.
* two frames self-placing on one hive from the same read: exactly one winner.

Every clock is a fixed number (`now=` / a patched ``time.time``); nothing sleeps. HQ and the
hive are scratch bare repos under ``tmp_path`` (the same shape as ``test_host_lease_cli``);
nothing touches a real HQ placement row.
"""

from __future__ import annotations

import subprocess

import pytest
from typer.testing import CliRunner

from beadhive import (
    config,
    guard,
    host,
    host_adopt,
    host_fence,
    host_lease,
    hq_authority_enforce,
    hq_operator_settings,
    registry,
    self_placement,
    trust_profiles,
)
from beadhive.capabilities import PLACE
from beadhive.cli import app
from beadhive.host_lease_contracts import HostLease, now_stamp
from beadhive.self_placement import Placement, Session, decide

runner = CliRunner()

PREFIX = "aa"
HOST_A = "11111111-1111-4111-8111-111111111111"
HOST_B = "22222222-2222-4222-8222-222222222222"
T0 = 1_800_000_000.0
REV = "f" * 40


def _profile(name: str, *overrides) -> trust_profiles.ResolvedProfile:
    return trust_profiles.resolve(name, overrides, source="fleet")


def _lease(host_id=HOST_B, *, expires=T0 + 600.0, epoch=3) -> HostLease:
    return HostLease(host_id, "desk", epoch, now_stamp(T0 - 60), now_stamp(expires))


def _decide(profile, lease=None, *, session=None, director=False, revision=REV):
    return decide(
        profile,
        prefix=PREFIX,
        host_id=HOST_A,
        frame="frame-a",
        placement=Placement(lease, revision if lease is not None else ""),
        session=session,
        director_credential=director,
        now=T0,
    )


LIVE = Session(staleness_s=60.0, failover_after_s=3600.0)
STALE = Session(staleness_s=4000.0, failover_after_s=3600.0)


# =============================================================================================
# The decision (pure)
# =============================================================================================


@pytest.mark.parametrize("name", ["open", "trusted"])
def test_self_placing_profiles_seed_a_never_placed_hive(name):
    decision = _decide(_profile(name))
    assert decision.allowed and decision.mode == "self" and decision.action == "seed"
    assert decision.revision == ""  # the adopt CASes from "absent"


@pytest.mark.parametrize("name", ["open", "trusted"])
def test_released_expired_and_own_placements_are_self_placeable(name):
    released = HostLease("", "", 3, now_stamp(T0), now_stamp(T0))
    assert _decide(_profile(name), released).action == "place"
    assert _decide(_profile(name), _lease(expires=T0 - 1)).action == "place"
    assert _decide(_profile(name), _lease(HOST_A)).action == "resume"


@pytest.mark.parametrize("name", ["open", "trusted"])
def test_a_session_stale_past_failover_after_is_taken_over(name):
    decision = _decide(_profile(name), _lease(), session=STALE)
    assert decision.allowed and decision.takeover
    assert decision.revision == REV  # pinned to the placement it read
    assert decision.failover_at == now_stamp(T0 + 3600.0 - 4000.0)


def test_an_evictable_holder_is_not_a_live_writer():
    decision = _decide(_profile("trusted"), _lease(), session=Session(evictable=True))
    assert decision.allowed and decision.action == "evict" and not decision.takeover


def test_open_refuses_a_live_writer_with_tenure_and_failover_and_no_director_command():
    decision = _decide(_profile("open"), _lease(), session=LIVE)
    assert not decision.allowed and decision.action == "live_writer"
    assert HOST_B in decision.message
    assert now_stamp(T0 + 600.0) in decision.message  # its tenure ends
    assert now_stamp(T0 + 3600.0 - 60.0) in decision.message  # failover would fire
    assert decision.director_command == ""
    assert "hq placement" not in decision.message
    assert "no director fallback" in decision.message


def test_trusted_refuses_a_live_writer_and_prints_the_director_command():
    decision = _decide(_profile("trusted"), _lease(), session=LIVE)
    assert not decision.allowed and decision.action == "live_writer"
    assert HOST_B in decision.message and now_stamp(T0 + 600.0) in decision.message
    assert decision.director_command == (
        f"bh hq placement place {PREFIX} --frame frame-a --expected-revision {REV} --confirm"
    )
    assert decision.director_command in decision.message


def test_an_unobserved_session_is_live_until_its_tenure_ends():
    decision = _decide(_profile("trusted"), _lease(), session=None)
    assert not decision.allowed and decision.failover_at == ""
    assert "cannot fire before then" in decision.message


def test_failover_before_tenure_end_is_named_as_the_earlier_event():
    decision = _decide(_profile("open"), _lease(expires=T0 + 7200.0), session=LIVE)
    assert f"failover fires at {now_stamp(T0 + 3540.0)}" in decision.message


@pytest.mark.parametrize("name", ["verified", "attested"])
def test_restricting_profiles_refuse_self_placement_with_the_director_command(name):
    for lease in (None, _lease(expires=T0 - 1)):
        decision = _decide(_profile(name), lease)
        assert not decision.allowed and decision.action == "director_only"
        assert "bh hq placement place aa --frame frame-a" in decision.message
        assert f"under the {name} trust profile" in decision.message


def test_the_director_credential_keeps_place_under_verified():
    decision = _decide(_profile("verified"), None, director=True)
    assert decision.allowed and decision.mode == "director"


def test_placement_mode_director_tightens_trusted_to_the_director():
    tightened = _profile(
        "trusted",
        trust_profiles.Override("placement", "hq.placement_mode", "director", "host.yaml"),
    )
    decision = _decide(tightened, None)
    assert not decision.allowed and decision.action == "director_only"


def test_placement_mode_self_on_a_default_host_selects_self_placement_with_fallback():
    """``hq.placement_mode: self`` maps to ``self-or-director``: on ``open`` it tightens to
    the director fallback; on an unsigned ``verified`` it is refused (no self-placement)."""
    override = trust_profiles.Override("placement", "hq.placement_mode", "self", "host.yaml")
    open_self = _profile("open", override)
    refusal = _decide(open_self, _lease(), session=LIVE)
    assert refusal.director_command  # the override added the director fallback
    assert not _decide(_profile("verified", override), None).allowed


def test_no_learned_profile_keeps_the_0_24_adopt():
    decision = _decide(trust_profiles.resolve(None), _lease(), session=LIVE)
    assert decision.allowed and decision.mode == "legacy"


# =============================================================================================
# The CLI: one-step adopt on a real git HQ and hive remote
# =============================================================================================


def _git(args, cwd):
    return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True, check=False)


def _bare(tmp_path, name):
    path = tmp_path / name
    _git(["init", "--bare", "-q", str(path)], tmp_path)
    return path


def _clone_with_origin(path, origin):
    path.mkdir(parents=True)
    _git(["init", "-q"], path)
    _git(["config", "user.email", "t@example.invalid"], path)
    _git(["config", "user.name", "t"], path)
    _git(["remote", "add", "origin", str(origin)], path)
    return path


@pytest.fixture
def hive(tmp_path, monkeypatch):
    hq_dir = _clone_with_origin(config.hq_dir(), _bare(tmp_path, "hq.git"))
    workspace = tmp_path / "workspace"
    monkeypatch.setattr(registry, "workspace_root", lambda: str(workspace))
    hive_dir = _clone_with_origin(workspace / "github" / "o" / "r", _bare(tmp_path, "r.git"))
    config.config_path().write_text(
        "schema_version: 1\n"
        "providers: [github]\n"
        f"managed_repos:\n  - provider: github\n    org: o\n    repo: r\n    prefix: {PREFIX}\n"
        "exclude:\n  orgs: []\n  repos: []\n"
        "otel:\n  enabled: false\n  protocol: grpc\n"
    )
    monkeypatch.delenv(hq_operator_settings.ENV, raising=False)
    monkeypatch.setattr(host_lease.time, "time", lambda: T0)  # one fixed clock for every read
    return {"hq_dir": hq_dir, "hive_dir": hive_dir}


def _as_host(monkeypatch, host_id=HOST_A):
    monkeypatch.setattr(host, "load", lambda: {"host_id": host_id, "label": "fixture-host"})
    result = runner.invoke(app, ["host", "init", "--role", "executor"])
    assert result.exit_code == 0, result.output


def _fleet(monkeypatch, name, session=None):
    monkeypatch.setattr(hq_authority_enforce, "trust_profile", lambda cfg=None: _profile(name))
    monkeypatch.setattr(
        self_placement, "holder_session", lambda holder, *, hq_dir, at=None: session
    )


def _held_by_b(hive):
    host_lease.adopt(
        "origin", PREFIX, host_id=HOST_B, label="desk", cwd=hive["hq_dir"], ttl=600.0, at=T0 - 60
    )


def _adopt():
    return runner.invoke(app, ["host", "lease", "adopt", PREFIX])


def test_trusted_adopt_on_a_never_placed_hive_seeds_places_and_fences_in_one_step(
    hive, monkeypatch
):
    _as_host(monkeypatch)
    _fleet(monkeypatch, "trusted")

    result = _adopt()

    assert result.exit_code == 0, result.output
    assert "self-placed under the trusted trust profile: seeded" in result.output
    lease = host_lease.read("origin", PREFIX, cwd=hive["hq_dir"])
    assert lease.host_id == HOST_A and lease.epoch == 1
    _sha, fence = host_fence.read_fence("origin", cwd=hive["hive_dir"])
    assert (fence.host_id, fence.epoch) == (HOST_A, 1)


def test_trusted_adopt_takes_over_a_session_stale_writer_in_one_step(hive, monkeypatch):
    _held_by_b(hive)
    _as_host(monkeypatch)
    _fleet(monkeypatch, "trusted", session=STALE)

    result = _adopt()

    assert result.exit_code == 0, result.output
    assert "session was stale past failover_after" in result.output
    lease = host_lease.read("origin", PREFIX, cwd=hive["hq_dir"])
    assert lease.host_id == HOST_A and lease.epoch == 2  # a fresh epoch, a fresh record


@pytest.mark.parametrize(("name", "director"), [("trusted", True), ("open", False)])
def test_a_live_writer_is_refused_naming_holder_tenure_and_failover(
    hive, monkeypatch, name, director
):
    _held_by_b(hive)
    _as_host(monkeypatch)
    _fleet(monkeypatch, name, session=LIVE)

    result = _adopt()

    assert result.exit_code == 1
    assert HOST_B in result.output
    assert now_stamp(T0 + 540.0) in result.output  # B's tenure ends (adopted T0-60, ttl 600)
    assert now_stamp(T0 + 3540.0) in result.output  # failover would fire
    assert ("bh hq placement place" in result.output) is director
    assert f"bh hq why {PREFIX}" in result.output
    assert host_lease.read("origin", PREFIX, cwd=hive["hq_dir"]).host_id == HOST_B


@pytest.mark.parametrize("name", ["verified", "attested"])
def test_restricting_profiles_refuse_the_adopt_and_print_the_director_command(
    hive, monkeypatch, name
):
    _as_host(monkeypatch)
    _fleet(monkeypatch, name)

    result = _adopt()

    assert result.exit_code == 1
    assert "self-placement is refused" in result.output
    assert f"bh hq placement place {PREFIX} --frame" in result.output
    assert host_lease.read("origin", PREFIX, cwd=hive["hq_dir"]) is None  # nothing written
    assert host_fence.read_fence("origin", cwd=hive["hive_dir"])[1] is None


def test_place_is_wired_through_guard_capability(hive, monkeypatch):
    _as_host(monkeypatch)
    _fleet(monkeypatch, "open")
    decision = guard.guard_capability(PLACE, PREFIX, verb="host lease adopt")
    assert decision.allowed and decision.action == "seed"


# =============================================================================================
# The CAS race: two frames, one hive, exactly one winner
# =============================================================================================


def _self_adopt(hive, host_id, decision):
    return host_adopt.adopt(
        prefix=PREFIX,
        hive_remote="origin",
        hq_remote="origin",
        hive_cwd=hive["hive_dir"],
        hq_cwd=hive["hq_dir"],
        host_id=host_id,
        label=host_id[:4],
        ttl=600.0,
        expected_placement=decision.revision,
    )


def _decisions(hive):
    """Both frames decide from the SAME read, before either writes."""
    profile = _profile("trusted")
    return [
        self_placement.evaluate(PREFIX, host_id=frame, hq_dir=hive["hq_dir"], profile=profile)
        for frame in (HOST_A, HOST_B)
    ]


def test_two_frames_self_placing_from_one_read_produce_exactly_one_winner(hive, monkeypatch):
    _as_host(monkeypatch)
    first, second = _decisions(hive)
    assert first.action == second.action == "seed" and first.revision == second.revision == ""

    outcomes = []
    for frame, decision in ((HOST_A, first), (HOST_B, second)):
        try:
            outcomes.append(_self_adopt(hive, frame, decision))
        except (host_adopt.PlacementLost, host_fence.FenceRejected, host_lease.HostLeaseRejected):
            outcomes.append(None)

    assert [o is not None for o in outcomes] == [True, False]
    lease = host_lease.read("origin", PREFIX, cwd=hive["hq_dir"])
    _sha, fence = host_fence.read_fence("origin", cwd=hive["hive_dir"])
    assert (lease.host_id, lease.epoch) == (HOST_A, 1)
    assert (fence.host_id, fence.epoch) == (HOST_A, 1)  # the loser moved nothing


def test_interleaved_at_the_fence_cas_the_frame_that_lands_first_is_the_only_winner(
    hive, monkeypatch
):
    """Frame A passes its placement pin, then — before A's fence CAS — frame B runs its whole
    self-placement. A's fence CAS then expects the value it read and loses."""
    _as_host(monkeypatch)
    first, second = _decisions(hive)
    real_install = host_fence.install_fence
    results = {}

    def install_fence(remote, fence, *, expected, cwd):
        if fence.host_id == HOST_A and "b" not in results:
            results["b"] = _self_adopt(hive, HOST_B, second)  # B lands in full first
        return real_install(remote, fence, expected=expected, cwd=cwd)

    monkeypatch.setattr(host_fence, "install_fence", install_fence)

    with pytest.raises(host_fence.FenceRejected):
        _self_adopt(hive, HOST_A, first)

    assert results["b"].lease.host_id == HOST_B
    lease = host_lease.read("origin", PREFIX, cwd=hive["hq_dir"])
    _sha, fence = host_fence.read_fence("origin", cwd=hive["hive_dir"])
    assert lease.host_id == fence.host_id == HOST_B and lease.epoch == fence.epoch == 1


def test_a_placement_that_moved_since_the_decision_is_lost_before_anything_is_written(
    hive, monkeypatch
):
    _as_host(monkeypatch)
    decision = _decisions(hive)[0]
    _held_by_b(hive)  # placement moves after the read

    with pytest.raises(host_adopt.PlacementLost, match="placement moved"):
        _self_adopt(hive, HOST_A, decision)
    assert host_fence.read_fence("origin", cwd=hive["hive_dir"])[1] is None


# =============================================================================================
# bh hq why names the self-adopt command
# =============================================================================================


def _why_placement(monkeypatch, decision):
    from beadhive import hq_why_layer_core
    from beadhive.hq_why import WhyContext

    monkeypatch.setattr(self_placement, "evaluate", lambda *a, **k: decision)
    ctx = WhyContext(PREFIX, "publish")
    ctx.memo.update({"host_id": HOST_A, "entry": {"prefix": PREFIX}, "decision": None})
    return hq_why_layer_core._not_placed(ctx, _lease(), "held by B", "test")


def test_hq_why_names_the_self_adopt_command_when_self_placement_is_allowed(monkeypatch):
    allowed = _decide(_profile("trusted"), _lease(), session=STALE)
    verdict = _why_placement(monkeypatch, allowed)
    assert verdict.command == f"bh host lease adopt {PREFIX}"
    assert "trusted trust profile lets this frame place itself" in verdict.summary
    assert verdict.detail["self_placement"]["action"] == "takeover"


def test_hq_why_names_the_director_command_when_self_placement_is_refused(monkeypatch):
    refused = _decide(_profile("trusted"), _lease(), session=LIVE)
    verdict = _why_placement(monkeypatch, refused)
    assert verdict.command == refused.director_command
    assert "live writer session" in verdict.summary
