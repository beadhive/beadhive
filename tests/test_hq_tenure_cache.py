"""The elected-tenure cache (trust-profiles ADR section 3, bh-ofszz; absorbs bh-qrsec, bh-kjxe9).

Every clock here is fake: the refresher loop runs under a fake timer that advances the fake
clock, and the retry backoff is recorded rather than slept.
"""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest

from beadhive import (
    config,
    config_store,
    frame_eligibility,
    hq_sql_config,
    hq_tenure_cache,
    hq_tenure_refresh,
    hq_trust_profile,
    trust_profiles,
)
from beadhive.capabilities import AUTHOR, CLAIM, EXECUTE, PLACE, PUBLISH
from beadhive.frame_eligibility import EligibilityDecision, EligibilityError
from beadhive.hq_tenure_cache import HiveDecisions, TenureRefresher
from beadhive.modules.config.domain.ports import FleetConfigDocument, FleetConfigSnapshot
from beadhive.trust_profiles import ATTESTED, OPEN, TRUSTED, VERIFIED

T0 = 1_800_000_000.0
FRAME = "frame-1"
PREFIX = "bh"
SRC = Path(__file__).resolve().parents[1] / "src" / "beadhive"


class Clock:
    def __init__(self, now: float = T0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeTimer:
    """``wait(seconds)`` for :meth:`TenureRefresher.run`: advances the fake clock instead of
    sleeping, runs a hook at each wake, and stops after `until` fake seconds."""

    def __init__(self, clock: Clock, until: float, on_wake=None):
        self.clock, self.until, self.on_wake = clock, until, on_wake
        self.waits: list[float] = []

    def __call__(self, seconds: float) -> bool:
        self.waits.append(seconds)
        self.clock.advance(seconds)
        if self.on_wake is not None:
            self.on_wake(self.clock.now)
        return self.clock.now >= self.until


@pytest.fixture(autouse=True)
def _isolated(monkeypatch):
    monkeypatch.delenv(hq_tenure_cache.ENV, raising=False)
    hq_tenure_cache.reset()
    hq_trust_profile.reset_cache()
    sleeps: list[float] = []
    monkeypatch.setattr(hq_tenure_cache, "sleep", sleeps.append)
    yield sleeps
    hq_tenure_cache.reset()
    hq_trust_profile.reset_cache()


def _host(tmp_path, **hq) -> dict:
    return {
        "host": {"frame_id": FRAME, "roles": ["executor"]},
        "hq": {
            "sql": {
                "enabled": True,
                "reader": {"host": "hq.example.test", "port": 3308},
                "floor_path": str(tmp_path / "floor.json"),
            },
            **hq,
        },
    }


def _floor(host: dict, head: str, sequence: int = 1) -> None:
    Path(host["hq"]["sql"]["floor_path"]).write_text(
        json.dumps({"backend": "b", "generation": "g", "sequence": sequence, "head": head})
    )


def _snapshot(head: str = "h1", body: str = "hq:\n  mode: dolt-server\n") -> FleetConfigSnapshot:
    return FleetConfigSnapshot(
        backend_identity="sql:b",
        commit_revision=head,
        generation="g",
        fetched_at=T0,
        valid_until=T0 + 30,
        documents=(FleetConfigDocument("fleet.yaml", body),),
    )


def _holds(predicates) -> bool:
    """Whether a cached decision (its predicates) allows."""
    return EligibilityDecision(predicates).allowed


def _learn(profile: str) -> None:
    hq_trust_profile.record(profile, head="h", via="test")
    hq_trust_profile.reset_cache()


def _policy(profile: str, host: dict | None = None) -> hq_tenure_cache.TenurePolicy:
    return hq_tenure_cache.current_policy(
        host or {"host": {"roles": ["executor"]}}, profile=trust_profiles.resolve(profile)
    )


class Live:
    """A live snapshot reader that counts HQ reads and can be made unreachable or refusing."""

    def __init__(self, host: dict, head: str = "h1"):
        self.host, self.head, self.calls = host, head, 0
        self.error: Exception | None = None

    def __call__(self, *_args) -> FleetConfigSnapshot:
        self.calls += 1
        if self.error is not None:
            raise self.error
        _floor(self.host, self.head)
        return _snapshot(self.head)


# ---- tenure per profile ----------------------------------------------------------------------


def test_tenure_per_profile_from_the_resolved_profile():
    executor = 3600.0
    assert _policy(OPEN).tenure_s == trust_profiles.OPEN_TENURE_CEILING_S == 86400.0
    assert _policy(TRUSTED).tenure_s == executor
    assert _policy(VERIFIED).tenure_s == executor
    attested = _policy(ATTESTED)
    assert attested.tenure_s == 900.0  # the heartbeat TTL
    assert not attested.offline_grace and attested.fresh_eligibility
    # The refresh interval defaults to a quarter of min(failover_after, tenure).
    assert _policy(VERIFIED).refresh_s == executor / 4
    assert attested.refresh_s == 900.0 / 4
    assert _policy(OPEN).refresh_s == executor / 4


def test_hq_tenure_bounds_open_and_tightens_verified_and_tenure_refresh_overrides():
    host = {"host": {"roles": ["executor"]}, "hq": {"tenure": 7200, "tenure_refresh": 60}}
    open_policy = hq_tenure_cache.current_policy(
        host, profile=hq_trust_profile.resolved(host, profile=OPEN)
    )
    assert open_policy.tenure_s == 7200.0 and open_policy.refresh_s == 60.0
    verified = hq_tenure_cache.current_policy(
        {"hq": {"tenure": 600}}, profile=hq_trust_profile.resolved({"hq": {"tenure": 600}})
    )
    assert verified.tenure_s == 600.0 and verified.refresh_s == 150.0
    # A refresh longer than the tenure is clamped to it.
    clamped = hq_tenure_cache.current_policy(
        {"hq": {"tenure": 600, "tenure_refresh": 9000}},
        profile=hq_trust_profile.resolved({"hq": {"tenure": 600, "tenure_refresh": 9000}}),
    )
    assert clamped.refresh_s == 600.0


# ---- the command read --------------------------------------------------------------------------


def test_fresh_host_reads_hq_once_per_process_and_seeds_the_cache(tmp_path):
    host = _host(tmp_path)
    live = Live(host)
    # Eleven ordinary reads (bh-qrsec measured eleven loads per `bh bd show`) cost one HQ read.
    snapshots = [hq_tenure_cache.read_snapshot(host, live, now=T0 + i) for i in range(11)]
    assert live.calls == 1
    assert {item.commit_revision for item in snapshots} == {"h1"}
    record = hq_tenure_cache.load()
    assert record is not None and record.source == "command" and record.refreshed_at == T0
    path = hq_tenure_cache.path()
    assert path is not None and os.stat(path).st_mode & 0o777 == 0o600
    # A new process (empty memo) reads the record: no HQ connection at all.
    hq_tenure_cache.reset()
    assert hq_tenure_cache.read_snapshot(host, live, now=T0 + 60).commit_revision == "h1"
    assert live.calls == 1


def test_a_floor_another_process_advanced_or_another_binding_reads_live(tmp_path):
    host = _host(tmp_path)
    live = Live(host)
    hq_tenure_cache.read_snapshot(host, live, now=T0)
    hq_tenure_cache.reset()
    _floor(host, "h2", sequence=2)  # another process verified a newer revision
    live.head = "h2"
    assert hq_tenure_cache.read_snapshot(host, live, now=T0 + 1).commit_revision == "h2"
    assert live.calls == 2
    hq_tenure_cache.reset()
    other = _host(tmp_path)
    other["hq"]["sql"]["reader"]["host"] = "elsewhere.example.test"
    other_live = Live(other, head="h2")
    hq_tenure_cache.read_snapshot(other, other_live, now=T0 + 2)
    assert other_live.calls == 1  # a record from another binding is never served


def test_a_stale_record_without_a_daemon_reads_live_once_and_reseeds(tmp_path):
    host = _host(tmp_path)
    live = Live(host)
    policy = _policy(VERIFIED)
    hq_tenure_cache.read_snapshot(host, live, now=T0, policy=policy)
    hq_tenure_cache.reset()
    later = T0 + policy.stale_after + 1
    hq_tenure_cache.read_snapshot(host, live, now=later, policy=policy)
    assert live.calls == 2
    assert hq_tenure_cache.load().refreshed_at == later


def test_cache_off_restores_the_per_process_read(tmp_path, monkeypatch):
    host = _host(tmp_path, tenure_cache=False)
    live = Live(host)
    for _ in range(3):
        hq_tenure_cache.read_snapshot(host, live, now=T0)
    assert live.calls == 3
    monkeypatch.setenv(hq_tenure_cache.ENV, "off")
    host = _host(tmp_path)
    hq_tenure_cache.read_snapshot(host, live, now=T0)
    assert live.calls == 4 and hq_tenure_cache.load() is None


def test_a_doubtful_record_is_never_served(tmp_path):
    host = _host(tmp_path)
    live = Live(host)
    hq_tenure_cache.read_snapshot(host, live, now=T0)
    path = hq_tenure_cache.path()
    os.chmod(path, 0o644)  # group/world readable: not ours alone
    hq_tenure_cache.reset()
    assert hq_tenure_cache.load() is None
    os.chmod(path, 0o600)
    payload = json.loads(path.read_text())
    payload["snapshot"]["documents"][0][1] = "hq:\n  mode: tampered\n"
    path.write_text(json.dumps(payload))
    hq_tenure_cache.reset()
    assert hq_tenure_cache.load() is None  # digest mismatch


# ---- HQ unreachable (bh-kjxe9) -----------------------------------------------------------------


def test_a_transient_reader_failure_is_retried_with_bounded_backoff(tmp_path, _isolated):
    host = _host(tmp_path)
    live = Live(host)
    attempts = []

    def flaky():
        attempts.append(1)
        if len(attempts) < 3:
            raise hq_sql_config.SqlConfigUnavailable("verified HQ config connection unavailable")
        return live()

    assert hq_tenure_cache.read_snapshot(host, flaky, now=T0).commit_revision == "h1"
    assert len(attempts) == 3 and _isolated == [0.25, 0.5]


def test_no_cache_and_hq_unreachable_names_the_endpoint_and_the_retry_outcome(tmp_path):
    host = _host(tmp_path)
    live = Live(host)
    live.error = hq_sql_config.SqlConfigUnavailable("verified HQ config connection unavailable")
    with pytest.raises(hq_sql_config.SqlConfigUnavailable) as raised:
        hq_tenure_cache.read_snapshot(host, live, now=T0)
    message = str(raised.value)
    assert "hq.example.test:3308" in message and "after 3 attempts" in message
    assert "no elected-tenure cache" in message
    assert live.calls == 3


def test_a_refusing_hq_fails_closed_at_once_even_with_a_cache(tmp_path, _isolated):
    host = _host(tmp_path)
    live = Live(host)
    policy = _policy(VERIFIED)
    hq_tenure_cache.read_snapshot(host, live, now=T0, policy=policy)
    hq_tenure_cache.reset()
    live.error = hq_sql_config.SqlConfigError("HQ configuration rollback or amended head detected")
    with pytest.raises(hq_sql_config.SqlConfigError, match="rollback"):
        hq_tenure_cache.read_snapshot(host, live, now=T0 + policy.stale_after + 1, policy=policy)
    assert live.calls == 2 and _isolated == []  # not retried, not served from the cache


def test_unreachable_hq_serves_the_cache_within_tenure_warning_once_per_hour(tmp_path):
    host = _host(tmp_path)
    live = Live(host)
    policy = _policy(OPEN)  # a 24 h tenure: room for more than one hourly warning
    hq_tenure_cache.read_snapshot(host, live, now=T0, policy=policy)
    live.error = hq_sql_config.SqlConfigUnavailable("verified HQ config connection unavailable")
    warnings: list[str] = []
    original = hq_tenure_cache.warn_unreachable

    def capture(*args, **kwargs):
        return original(*args, emit=warnings.append, **{k: v for k, v in kwargs.items()})

    hq_tenure_cache.warn_unreachable = capture
    try:
        start = T0 + policy.stale_after + 1
        for offset in (0, 600, 1800, 3600 + 1):
            hq_tenure_cache.reset()
            served = hq_tenure_cache.read_snapshot(host, live, now=start + offset, policy=policy)
            assert served.commit_revision == "h1"
    finally:
        hq_tenure_cache.warn_unreachable = original
    assert len(warnings) == 2  # at start, and again an hour later
    assert warnings[0].startswith("⚠ HQ unreachable since ") and "tenure ends at" in warnings[0]


def test_the_daemon_reporting_hq_unreachable_spares_commands_the_live_read(tmp_path):
    host = _host(tmp_path)
    live = Live(host)
    clock = Clock()
    refresher = TenureRefresher(host_cfg=lambda: host, load_live=lambda _h: live(), clock=clock)
    refresher.refresh()
    live.error = hq_sql_config.SqlConfigUnavailable("verified HQ config connection unavailable")
    policy = _policy(VERIFIED)
    clock.advance(policy.stale_after + 1)
    outcome = refresher.refresh()
    assert not outcome.ok and hq_tenure_cache.load().unreachable_since == clock.now
    calls = live.calls
    hq_tenure_cache.read_snapshot(host, live, now=clock.now + 1, policy=policy)
    assert live.calls == calls  # the daemon already knows: no synchronous HQ read


# ---- lapse rules (table-driven) ------------------------------------------------------------------

LAPSE_TABLE = [
    # profile,  capability, continues after the lapse
    (OPEN, AUTHOR, True),
    (OPEN, CLAIM, True),
    (OPEN, EXECUTE, False),
    (OPEN, PUBLISH, False),
    (OPEN, PLACE, False),
    (TRUSTED, AUTHOR, True),
    (TRUSTED, CLAIM, True),
    (TRUSTED, EXECUTE, False),
    (TRUSTED, PUBLISH, False),
    (TRUSTED, PLACE, False),
    (VERIFIED, AUTHOR, False),
    (VERIFIED, CLAIM, False),
    (VERIFIED, EXECUTE, False),
    (VERIFIED, PUBLISH, False),
    (VERIFIED, PLACE, False),
    (ATTESTED, AUTHOR, False),
    (ATTESTED, CLAIM, False),
    (ATTESTED, EXECUTE, False),
    (ATTESTED, PUBLISH, False),
    (ATTESTED, PLACE, False),
]


def _record_with_decisions(host, clock, *, allowed=True):
    live = Live(host)
    refresher = TenureRefresher(
        host_cfg=lambda: host,
        load_live=lambda _h: live(),
        decide=lambda _h, snapshot, now: {
            PREFIX: HiveDecisions(
                FRAME,
                now,
                snapshot.commit_revision,
                {
                    capability: (("authority_available", True), ("not_cordoned", allowed))
                    for capability in (AUTHOR, CLAIM, EXECUTE, PUBLISH, PLACE)
                },
            )
        },
        clock=clock,
    )
    refresher.refresh()
    return live, refresher


@pytest.mark.parametrize(("profile", "capability", "continues"), LAPSE_TABLE)
def test_after_the_lapse_only_author_and_claim_continue_under_open_and_trusted(
    tmp_path, profile, capability, continues
):
    host = _host(tmp_path)
    clock = Clock()
    _record_with_decisions(host, clock)
    policy = _policy(profile)
    clock.advance(policy.tenure_s + 1)  # HQ unreachable for the whole tenure
    decision = EligibilityDecision(
        hq_tenure_cache.offline_decision(
            FRAME, PREFIX, capability, host, now=clock.now, policy=policy
        )
    )
    assert decision.allowed is continues
    if not continues:
        assert decision.reason == "tenure_current"


@pytest.mark.parametrize("profile", [OPEN, TRUSTED, VERIFIED])
@pytest.mark.parametrize("capability", [AUTHOR, CLAIM, EXECUTE, PUBLISH])
def test_within_tenure_every_write_capability_works_offline_with_grace(
    tmp_path, profile, capability
):
    host = _host(tmp_path)
    clock = Clock()
    _record_with_decisions(host, clock)
    policy = _policy(profile)
    clock.advance(policy.tenure_s - 1)
    assert _holds(
        hq_tenure_cache.offline_decision(
            FRAME, PREFIX, capability, host, now=clock.now, policy=policy
        )
    )


def test_attested_has_no_offline_grace(tmp_path):
    host = _host(tmp_path)
    clock = Clock()
    _record_with_decisions(host, clock)
    policy = _policy(ATTESTED)
    clock.advance(1)
    for capability in (AUTHOR, CLAIM, EXECUTE, PUBLISH):
        decision = hq_tenure_cache.offline_decision(
            FRAME, PREFIX, capability, host, now=clock.now, policy=policy
        )
        assert not _holds(decision)


@pytest.mark.parametrize(
    ("profile", "served"), [(OPEN, True), (TRUSTED, True), (VERIFIED, False), (ATTESTED, False)]
)
def test_the_snapshot_after_the_lapse_follows_the_author_rule(tmp_path, profile, served):
    host = _host(tmp_path)
    live = Live(host)
    policy = _policy(profile)
    hq_tenure_cache.read_snapshot(host, live, now=T0, policy=policy)
    live.error = hq_sql_config.SqlConfigUnavailable("verified HQ config connection unavailable")
    hq_tenure_cache.reset()
    later = T0 + policy.tenure_s + 1
    if served:
        assert hq_tenure_cache.read_snapshot(host, live, now=later, policy=policy)
    else:
        with pytest.raises(hq_sql_config.SqlConfigUnavailable, match="hq.example.test:3308"):
            hq_tenure_cache.read_snapshot(host, live, now=later, policy=policy)


def test_open_tenure_is_bounded_by_its_24h_ceiling(tmp_path):
    host = _host(tmp_path)
    clock = Clock()
    _record_with_decisions(host, clock)
    policy = _policy(OPEN)
    clock.advance(24 * 3600 - 1)  # far beyond failover_after: still within the open ceiling
    assert _holds(
        hq_tenure_cache.offline_decision(FRAME, PREFIX, PUBLISH, host, now=clock.now, policy=policy)
    )
    clock.advance(2)  # the ceiling lapses: the open after-lapse rules apply
    assert not _holds(
        hq_tenure_cache.offline_decision(FRAME, PREFIX, PUBLISH, host, now=clock.now, policy=policy)
    )
    assert _holds(
        hq_tenure_cache.offline_decision(FRAME, PREFIX, AUTHOR, host, now=clock.now, policy=policy)
    )


# ---- the daemon refresh loop -------------------------------------------------------------------


def test_the_daemon_refreshes_on_the_timer_and_a_cordon_lands_within_one_interval(tmp_path):
    host = _host(tmp_path)
    clock = Clock()
    cordoned = {"value": False}
    live = Live(host)

    def decide(_host_cfg, snapshot, now):
        return {
            PREFIX: HiveDecisions(
                FRAME,
                now,
                snapshot.commit_revision,
                {AUTHOR: (("authority_available", True), ("not_cordoned", not cordoned["value"]))},
            )
        }

    refresher = TenureRefresher(
        host_cfg=lambda: host, load_live=lambda _h: live(), decide=decide, clock=clock
    )
    policy = _policy(VERIFIED)
    published_at: list[float] = []
    honoured_at: list[float] = []

    def on_wake(now):
        if not published_at and now >= T0 + 1000:
            cordoned["value"] = True  # the operator cordons the frame at HQ
            published_at.append(now)
        if published_at and not honoured_at:
            decision = hq_tenure_cache.cached_decision(
                FRAME, PREFIX, AUTHOR, host, now=now, policy=policy
            )
            if decision is not None and not _holds(decision):
                honoured_at.append(now)

    timer = FakeTimer(clock, until=T0 + 4 * policy.refresh_s, on_wake=on_wake)
    refresher.run(timer, poll_s=60)
    assert refresher.refreshes == 4  # t0, then every refresh interval until the timer stops
    assert honoured_at and honoured_at[0] - published_at[0] <= policy.refresh_s
    assert set(timer.waits) == {60}


def test_a_successful_heartbeat_refreshes_before_the_timer(tmp_path):
    host = _host(tmp_path)
    clock = Clock()
    live = Live(host)
    refresher = TenureRefresher(host_cfg=lambda: host, load_live=lambda _h: live(), clock=clock)
    assert refresher.tick().reason == "timer"
    clock.advance(30)
    assert refresher.tick() is None  # not due
    hq_tenure_cache.mark_heartbeat(clock.now)
    outcome = refresher.tick()
    assert outcome is not None and outcome.reason == "heartbeat" and live.calls == 2
    assert refresher.tick() is None


def test_build_refresher_needs_sql_hq_and_the_cache_on(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "load_host", lambda: {"hq": {"sql": {"enabled": False}}})
    assert hq_tenure_refresh.build_refresher() is None
    monkeypatch.setattr(
        config, "load_host", lambda: {"hq": {"sql": {"enabled": True}, "tenure_cache": False}}
    )
    assert hq_tenure_refresh.build_refresher() is None
    monkeypatch.setattr(config, "load_host", lambda: {"hq": {"sql": {"enabled": True}}})
    assert isinstance(hq_tenure_refresh.build_refresher(), TenureRefresher)


def test_session_renewer_reports_each_landed_renewal():
    from beadhive.hq_sql_session import SessionRenewer

    renewer = SessionRenewer.__new__(SessionRenewer)
    outcomes = iter([None, RuntimeError("blip"), None])

    def tick():
        value = next(outcomes)
        if isinstance(value, Exception):
            raise value

    renewer.tick = tick
    landed, failed, stops = [], [], iter([False, False, True])
    renewer.run(
        interval_s=1.0,
        stop=lambda _wait: next(stops),
        on_error=failed.append,
        on_success=lambda: landed.append(1),
    )
    assert len(landed) == 2 and len(failed) == 1


def test_the_heartbeat_sender_marks_a_landed_beat(monkeypatch):
    from beadhive import heartbeat_report, heartbeat_sender

    monkeypatch.setattr(heartbeat_report, "send_cached_beat", lambda free_sessions: "digest")
    assert heartbeat_sender._beat(0) == "digest"
    assert hq_tenure_cache.last_heartbeat() is not None


# ---- the eligibility boundary --------------------------------------------------------------------


def _wire_boundary(monkeypatch, host, verdicts):
    """`config.load_host` and a counting live `decision_for` returning `verdicts[-1]`."""
    monkeypatch.setattr(config, "load_host", lambda: host)
    reads = []

    def decision_for(host_id, hive=None, **_kwargs):
        reads.append(host_id)
        return verdicts[-1]

    monkeypatch.setattr(frame_eligibility, "decision_for", decision_for)
    return reads


ALLOWED = EligibilityDecision((("authority_available", True), ("not_cordoned", True)))
CORDONED = EligibilityDecision((("authority_available", True), ("not_cordoned", False)))
UNREACHABLE = EligibilityDecision((("authority_available", False),))
HIVE = {"prefix": PREFIX, "repo": "beadhive"}


def test_author_reads_the_daemon_decision_with_no_hq_read(tmp_path, monkeypatch):
    host = _host(tmp_path)
    clock = Clock()
    monkeypatch.setattr(hq_tenure_cache, "clock", clock)
    _learn(VERIFIED)
    _record_with_decisions(host, clock)
    reads = _wire_boundary(monkeypatch, host, [CORDONED])
    clock.advance(10)
    for capability in (AUTHOR, CLAIM):
        frame_eligibility.require_eligible(FRAME, HIVE, capability=capability)
    assert reads == []  # served by the cache; the live verdict (cordoned) was never read
    with pytest.raises(EligibilityError, match="not_cordoned"):
        frame_eligibility.require_eligible(FRAME, HIVE, capability=EXECUTE)
    assert reads == [FRAME]  # execute keeps its live read


def test_attested_keeps_the_fresh_reread_so_a_cordon_lands_on_the_next_command(
    tmp_path, monkeypatch
):
    host = _host(tmp_path)
    clock = Clock()
    monkeypatch.setattr(hq_tenure_cache, "clock", clock)
    _learn(ATTESTED)
    _record_with_decisions(host, clock)
    verdicts = [ALLOWED]
    reads = _wire_boundary(monkeypatch, host, verdicts)
    clock.advance(10)
    frame_eligibility.require_eligible(FRAME, HIVE, capability=AUTHOR)
    verdicts.append(CORDONED)  # the operator cordons the frame at HQ
    with pytest.raises(EligibilityError, match="not_cordoned"):
        frame_eligibility.require_eligible(FRAME, HIVE, capability=AUTHOR)
    assert reads == [FRAME, FRAME]


def test_an_unreachable_live_read_falls_back_to_the_cached_decision(tmp_path, monkeypatch):
    host = _host(tmp_path)
    clock = Clock()
    monkeypatch.setattr(hq_tenure_cache, "clock", clock)
    _learn(TRUSTED)
    _record_with_decisions(host, clock)
    reads = _wire_boundary(monkeypatch, host, [UNREACHABLE])
    policy = _policy(TRUSTED, host)
    clock.advance(policy.tenure_s - 1)  # within tenure, the record no longer fresh
    assert frame_eligibility.require_eligible(FRAME, HIVE, capability=EXECUTE).allowed
    clock.advance(2)  # lapsed: execute refuses, author continues under trusted
    with pytest.raises(EligibilityError, match="tenure_current"):
        frame_eligibility.require_eligible(FRAME, HIVE, capability=EXECUTE)
    assert frame_eligibility.require_eligible(FRAME, HIVE, capability=AUTHOR).allowed
    assert len(reads) == 3


def test_an_explicit_clock_or_another_frame_always_reads_live(tmp_path, monkeypatch):
    host = _host(tmp_path)
    clock = Clock()
    monkeypatch.setattr(hq_tenure_cache, "clock", clock)
    _learn(VERIFIED)
    _record_with_decisions(host, clock)
    reads = _wire_boundary(monkeypatch, host, [ALLOWED])
    frame_eligibility.require_eligible(FRAME, HIVE, capability=AUTHOR, at=T0)
    frame_eligibility.require_eligible("frame-2", HIVE, capability=AUTHOR)
    frame_eligibility.require_eligible(FRAME, HIVE)
    assert reads == [FRAME, "frame-2", FRAME]


# ---- config_store: ordinary reads use the cache, publication never does ------------------------


def _sql_home(tmp_path, monkeypatch):
    monkeypatch.setenv("BH_HOME", str(tmp_path))
    host = config.config_path()
    host.parent.mkdir(parents=True, exist_ok=True)
    binding = {
        "enabled": True,
        "reader": {
            "host": "sql.example.test",
            "database": "beadhive_hq_config",
            "user": "reader",
            "server_name": "sql.example.test",
            "ca_file": str(tmp_path / "ca.pem"),
            "credential": {
                "config_path": str(tmp_path / "fnox.toml"),
                "profile": "test",
                "key": "SQL_READER",
            },
        },
        "floor_path": str(tmp_path / "floor.json"),
        "backend_identity": "fixture",
        "generation": "fixture-generation",
        "initial_revision": "a" * 32,
    }
    host.write_text(json.dumps({"hq": {"sql": binding}}))
    config_store.clear_load_cache()
    return {"hq": {"sql": binding}}


def test_eleven_effective_loads_cost_one_hq_read(tmp_path, monkeypatch):
    from beadhive import hq_control_plane

    host = _sql_home(tmp_path, monkeypatch)
    reads = []

    def attach(*, bootstrap):
        reads.append(1)
        _floor(host, "h1")
        return None, _snapshot("h1", "hq:\n  mode: dolt-server\nwork:\n  validate_cmd: central\n")

    monkeypatch.setattr(hq_control_plane, "attach_fleet_config", attach)
    for _ in range(11):
        assert config.load()["work"]["validate_cmd"] == "central"
        assert config.load_fleet()["work"]["validate_cmd"] == "central"
    assert reads == [1]
    # A fleet mutation (publication) reads HQ live even with a current cache.
    with config_store.fleet_mutation(config._facade()):
        pass
    assert reads == [1, 1]


ALLOWED_IMPORTERS = {
    "config_store",
    "frame_eligibility",
    "heartbeat_sender",
    "host_daemon",
    "hq_tenure_cache",
    "hq_tenure_refresh",
    "hq_why_layer_tenure",
}
#: Modules that own the placement CAS, state pushes and authority writes: none may consult the
#: cache (ADR section 3, "Not cached").
NEVER_CACHED = (
    "hq_sql_placement",
    "hq_placement_ops",
    "hq_placement_cli",
    "host_lease",
    "director_failover",
    "hq_authority_cli",
    "hq_authority_fleet_mode",
    "hq_sql_operator",
    "hq_seed",
    "hq_migrate_cli",
    "molecule",
    "work",
    "work_assignment",
    "push_state",
)


def _imports_tenure_cache(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {alias.name for alias in node.names}
            if (node.module or "").endswith("hq_tenure_cache") or "hq_tenure_cache" in names:
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name.endswith("hq_tenure_cache") for alias in node.names):
                return True
    return False


def test_architecture_no_cache_for_placement_pushes_or_authority_writes():
    importers = {path.stem for path in SRC.rglob("*.py") if _imports_tenure_cache(path)}
    assert importers <= ALLOWED_IMPORTERS, importers - ALLOWED_IMPORTERS
    assert not importers & set(NEVER_CACHED)
    # Inside config_store only the ordinary effective reads take the cached path: every
    # publication, seed, backend switch and readback reads HQ live.
    tree = ast.parse((SRC / "config_store.py").read_text(encoding="utf-8"))
    cached = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and any(
            isinstance(call, ast.Call) and getattr(call.func, "id", "") == "_effective_sql_snapshot"
            for call in ast.walk(node)
        )
    }
    assert cached == {"load_fleet", "_load_sql"}


# ---- daemon wiring -------------------------------------------------------------------------------


def test_the_daemon_component_runs_the_refresher_in_a_thread_until_shutdown():
    import asyncio

    ran = []

    class Refresher:
        def run(self, wait):
            ran.append("started")
            while not wait(60):  # woken by shutdown, never by elapsed time
                pass
            ran.append("stopped")

    component = hq_tenure_refresh.TenureLoop(Refresher()).component()
    assert component.name == "hq-tenure-cache"

    async def lifecycle():
        async with component.lifespan(None):
            while not ran:
                await asyncio.sleep(0)

    asyncio.run(lifecycle())
    assert ran == ["started", "stopped"]


def test_the_daemon_builds_no_tenure_loop_without_sql_hq(monkeypatch):
    from beadhive import host_daemon

    monkeypatch.setattr(config, "load_host", lambda: {"hq": {"sql": {"enabled": False}}})
    assert host_daemon._tenure_loop() is None
    monkeypatch.setattr(config, "load_host", lambda: {"hq": {"sql": {"enabled": True}}})
    assert isinstance(host_daemon._tenure_loop(), hq_tenure_refresh.TenureLoop)


# ---- bh hq why --------------------------------------------------------------------------------


def test_hq_why_reports_the_cache_and_refuses_publish_after_the_lapse(tmp_path, monkeypatch):
    from beadhive import hq_why
    from beadhive.hq_why_layer_tenure import check_tenure

    host = _host(tmp_path)
    clock = Clock()
    monkeypatch.setattr(hq_tenure_cache, "clock", clock)
    monkeypatch.setattr(config, "load_host", lambda: host)
    _learn(TRUSTED)
    assert check_tenure(hq_why.WhyContext(PREFIX, PUBLISH)).status == hq_why.SKIPPED
    live, refresher = _record_with_decisions(host, clock)
    verdict = check_tenure(hq_why.WhyContext(PREFIX, PUBLISH))
    assert verdict.status == hq_why.OK and "revision h1" in verdict.summary
    live.error = hq_sql_config.SqlConfigUnavailable("verified HQ config connection unavailable")
    clock.advance(_policy(TRUSTED, host).tenure_s + 1)
    refresher.refresh()  # the daemon finds HQ unreachable past the tenure
    refused = check_tenure(hq_why.WhyContext(PREFIX, PUBLISH))
    assert (refused.status, refused.code) == (hq_why.REFUSED, "tenure_lapsed")
    assert refused.command.endswith("doctor")
    assert check_tenure(hq_why.WhyContext(PREFIX, AUTHOR)).status == hq_why.OK
    assert "tenure" in [layer.name for layer in hq_why.layers()]
