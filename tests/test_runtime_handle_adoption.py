"""Persisted runtime handles and restart adoption (bh-fwm82, ADR Decision 4).

A runtime whose sessions outlive the loop (``adoptable = True``) gets one ``bh.runtime.handle``
document per bead, written by the LOOP after ``schedule()``. A restarted loop adopts the live
ones instead of rescheduling, reaping or releasing them; a turn that ended while no loop ran is
harvested; and lease expiry still wins. Everything here runs against a stateful fake ``bd`` (bead
status, assignee, lease, metadata) and a fake remote runtime whose ``observe()`` is the real
outcome-sink reader, so a session's record is read back exactly as Gas City's would be. Time is
two injected clocks — a monotonic one for the wall-time cap and a wall clock for leases and
``scheduled_at`` — never a sleep.
"""

from __future__ import annotations

import asyncio
import functools
import json
from datetime import UTC, datetime, timedelta

import pytest

from beadhive import bd as bd_mod
from beadhive import beads_routing, localloop, outcome, runtime_handle
from beadhive.runtime import RoleHandle, RoleOutcome, ScheduleRefused

ACTOR = "disp/loop"
LEASE = timedelta(minutes=5)
T0 = datetime(2026, 11, 25, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _bd_cli_fallback_route(monkeypatch):
    monkeypatch.setattr(
        beads_routing, "configured_route", lambda *_a, **_k: beads_routing.ROUTE_API_CLI_FALLBACK
    )


def async_test(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


class _CP:
    def __init__(self, returncode, stdout="", stderr=""):
        self.returncode, self.stdout, self.stderr = returncode, stdout, stderr


def _iso(when: datetime) -> str:
    return when.isoformat().replace("+00:00", "Z")


class FakeHive:
    """A stateful fake `bd`: bead rows with status / assignee / lease / metadata, and the verbs a
    pass uses — reclaim reverting EXPIRED leases only, heartbeat renewing one, the metadata merge
    and unset routes, the claim release, and cause events."""

    def __init__(self, beads=()):
        self.now = T0
        #: `bd reclaim --older-than`: how long past expiry a claim stays before reclaim reverts
        #: it. Zero unless a test models real bd's grace window (10m by default).
        self.grace = timedelta(0)
        self.beads: dict[str, dict] = {}
        for bead in beads:
            self.add(bead)
        self.calls: list[list[str]] = []

    def add(self, bead, *, status="open", assignee=""):
        self.beads[bead] = {
            "id": bead,
            "parent": "epic-1",
            "status": status,
            "issue_type": "task",
            "labels": [],
            "assignee": assignee,
            "metadata": {},
        }

    def claim(self, bead):
        row = self.beads[bead]
        row.update(status="in_progress", assignee=ACTOR, lease_expires_at=_iso(self.now + LEASE))

    def handle(self, bead) -> runtime_handle.PersistedHandle | None:
        return runtime_handle.from_row(self.beads[bead])

    def record(self, bead, session_id, status="done", summary="finished"):
        """The seat's own outcome-sink write (`bh role --outcome-sink bead`), append-only."""
        key = outcome.metadata_key(session_id)
        self.beads[bead]["metadata"].setdefault(
            key,
            {
                "outcome": {"status": status, "summary": summary, "bead_id": bead},
                "session_id": session_id,
                "cost_usd": 0.25,
                "usage": {},
                "packs": [],
            },
        )

    def __call__(self, cmd, **kw):
        args = list(cmd[1:])
        while args and args[0] in ("-C", "--actor"):
            args = args[2:]
        args = [a for a in args if a != "--json"]
        self.calls.append(args)
        sub = args[0] if args else ""
        if sub == "gate" and args[1:2] == ["list"]:
            return _CP(0, "[]")
        if sub == "gate":
            return _CP(0, json.dumps({"checked": 0, "resolved": 0, "escalated": 0, "errors": 0}))
        if sub == "reclaim":
            return self._reclaim()
        if sub == "heartbeat":
            row = self.beads.get(args[1])
            if row is None or row["status"] != "in_progress":
                return _CP(1, "", "not in progress")
            row["lease_expires_at"] = _iso(self.now + LEASE)
            return _CP(0, json.dumps({"id": args[1], "status": "heartbeat"}))
        if sub == "show":
            row = self.beads.get(args[1])
            return _CP(0, json.dumps(row if row else {"id": args[1], "status": "in_progress"}))
        if sub == "list" and "--parent" in args:
            parent = args[args.index("--parent") + 1]
            rows = [dict(r) for r in self.beads.values()] if parent == "epic-1" else []
            return _CP(0, json.dumps(rows))
        if sub == "ready":
            return _CP(0, "[]")
        if sub == "update":
            return self._update(args, kw.get("text_input"))
        return _CP(0, "")

    def _reclaim(self):
        reclaimed = []
        for row in self.beads.values():
            expires = row.get("lease_expires_at")
            cutoff = _iso(self.now - self.grace)
            if row["status"] == "in_progress" and expires and expires <= cutoff:
                reclaimed.append({"id": row["id"], "previous_owner": row["assignee"]})
                row.update(status="open", assignee="")
                row.pop("lease_expires_at", None)
        return _CP(0, json.dumps({"count": len(reclaimed), "reclaimed": reclaimed}))

    def _update(self, args, text_input):
        row = self.beads[args[1]]
        if "--metadata" in args:
            row["metadata"].update(json.loads(text_input))
        if "--unset-metadata" in args:
            for i, a in enumerate(args):
                if a == "--unset-metadata":
                    row["metadata"].pop(args[i + 1], None)
        if "--status" in args:
            row["status"] = args[args.index("--status") + 1]
            row["assignee"] = args[args.index("--assignee") + 1] if "--assignee" in args else ""
        return _CP(0, "")

    # ---- what the loop wrote --------------------------------------------------------------

    def written_states(self):
        return [(c[1], c[2]) for c in self.calls if c and c[0] == "set-state"]

    def set_state_reason(self, bead):
        call = next(c for c in self.calls if c[:2] == ["set-state", bead])
        return call[call.index("--reason") + 1]

    def released(self, bead):
        return any(c[:2] == ["update", bead] and "--status" in c for c in self.calls)

    def handle_writes(self, bead=None):
        return [
            c
            for c in self.calls
            if c[:1] == ["update"]
            and (bead is None or c[1] == bead)
            and ("--metadata" in c or "--unset-metadata" in c)
        ]


@pytest.fixture
def hive(monkeypatch):
    fake = FakeHive()
    monkeypatch.setattr(bd_mod, "_run", fake)
    return fake


class RemoteRuntime:
    """A Gas-City-shaped runtime: sessions are not children of the loop, so they live in a
    store shared across "restarts" (a new runtime instance over the same `sessions`), and
    ``observe()`` is the real outcome-sink reader over the fake hive. It never writes a bead."""

    name = "gascity"
    adoptable = True

    def __init__(self, hive_dir, sessions: dict[str, str]):
        self.hive_dir = hive_dir
        self.sessions = sessions  # session_id -> bead
        self.scheduled: list[dict] = []
        self.observed: list[RoleHandle] = []
        self.closed = 0

    def schedule(self, bead_id, role, *, workspace, instructions, session_id, model=None):
        self.scheduled.append({"bead": bead_id, "role": role, "session_id": session_id})
        self.sessions[session_id] = bead_id
        return RoleHandle(bead_id=bead_id, session_id=session_id)

    def observe(self, handle):
        self.observed.append(handle)
        return outcome.observe(handle, cwd=self.hive_dir)

    def on_gate_resolved(self, resolution):
        pass

    def close(self):
        self.closed += 1


class CancellableRemote(RemoteRuntime):
    def __init__(self, hive_dir, sessions):
        super().__init__(hive_dir, sessions)
        self.cancelled: list[RoleHandle] = []
        self.cancel_error: Exception | None = None

    def cancel(self, handle):
        self.cancelled.append(handle)
        if self.cancel_error is not None:
            raise self.cancel_error
        return RoleOutcome(status="failed", session_id=handle.session_id, cancel_rung="hard")


class Clock:
    def __init__(self, start=1000.0):
        self.now = start

    def __call__(self):
        return self.now


def _ws(tmp_path, bead):
    path = tmp_path / "wt" / bead
    path.mkdir(parents=True, exist_ok=True)
    return str(path)


def _claimer(hive, tmp_path, beads):
    pending = list(beads)

    def claim():
        while pending:
            bead = pending.pop(0)
            if hive.beads[bead]["status"] == "open":
                hive.claim(bead)
                return localloop.ClaimResult(claimed=bead, worktree=_ws(tmp_path, bead))
        return localloop.ClaimResult(reason="empty_queue")

    return claim


def _loop(tmp_path, hive, rt, *, clock=None, claim=None, **kw):
    kw.setdefault("caps", localloop.Caps(max_concurrency=2))
    return localloop.LocalLoop(
        hive_dir=tmp_path,
        epic="epic-1",
        actor=ACTOR,
        runtime=rt,
        workspace_for=lambda bead: _ws(tmp_path, bead),
        routing=lambda _bead, _role: None,
        instructions=lambda _a, bead, _r: f"brief-{bead}.md",
        claim=claim or (lambda: localloop.ClaimResult(reason="empty_queue")),
        clock=clock or Clock(),
        wall_clock=lambda: hive.now,
        **kw,
    )


@pytest.fixture
def no_orphan_scan(monkeypatch):
    def refuse(*_a, **_k):
        raise AssertionError("an adoptable runtime's sessions are adopted, never orphan-reaped")

    monkeypatch.setattr(localloop, "find_orphan_seats", refuse)


# ---- the handle document ----------------------------------------------------------------------


@async_test
async def test_the_loop_writes_one_handle_document_after_schedule(tmp_path, hive):
    hive.add("b1")
    sessions: dict[str, str] = {}
    rt = RemoteRuntime(tmp_path, sessions)
    loop = _loop(tmp_path, hive, rt, claim=_claimer(hive, tmp_path, ["b1"]))

    report = await loop.run_pass()

    assert report.dispatched == ("b1",)
    (sid,) = sessions
    assert hive.beads["b1"]["metadata"] == {
        "bh.runtime.handle": {
            "runtime": "gascity",
            "session_id": sid,
            "role": "developer",
            "action": "dispatch",
            "scheduled_at": "2026-11-25T12:00:00Z",
        }
    }, "exactly one key, the documented shape, wall-clock scheduled_at"
    assert len(hive.handle_writes("b1")) == 1


def test_a_malformed_handle_document_is_no_handle():
    assert runtime_handle.parse({"bh.runtime.handle": "not json"}, "b1") is None
    assert runtime_handle.parse({"bh.runtime.handle": {"runtime": "gascity"}}, "b1") is None
    bad_time = {f: "x" for f in ("runtime", "session_id", "role", "action", "scheduled_at")}
    assert runtime_handle.parse({"bh.runtime.handle": bad_time}, "b1") is None
    good = {**bad_time, "scheduled_at": "2026-11-25T12:00:00Z"}
    parsed = runtime_handle.parse({"bh.runtime.handle": json.dumps(good)}, "b1")
    assert parsed is not None and parsed.scheduled_at == T0
    assert parsed.age_seconds(T0 - timedelta(seconds=30)) == 0.0, "clock skew is never negative"


# ---- acceptance 1: restart adopts the live, harvests the terminal ------------------------------


@async_test
async def test_a_restarted_loop_adopts_live_handles_and_harvests_terminal_ones(
    tmp_path, hive, no_orphan_scan
):
    hive.add("b1")
    hive.add("b2")
    sessions: dict[str, str] = {}
    first_rt = CancellableRemote(tmp_path, sessions)
    first = _loop(tmp_path, hive, first_rt, claim=_claimer(hive, tmp_path, ["b1", "b2"]))
    assert (await first.run_pass()).dispatched == ("b1", "b2")
    sid = {bead: s for s, bead in sessions.items()}

    # A plain stop (SIGTERM, a restart) leaves an adoptable runtime's sessions alone.
    await first.shutdown()
    assert first_rt.cancelled == [], "no session is cancelled"
    assert hive.written_states() == [], "no cause is written"
    assert not hive.released("b1") and not hive.released("b2"), "no claim is released"
    assert first_rt.closed == 1, "the runtime itself is still closed"
    assert hive.handle("b1") is not None and hive.handle("b2") is not None

    # b2's session finishes while no loop is running; b1's is still going. Leases are valid.
    hive.now += timedelta(minutes=2)
    hive.record("b2", sid["b2"], status="blocked", summary="needs a human")

    second_rt = CancellableRemote(tmp_path, sessions)
    second = _loop(tmp_path, hive, second_rt, clock=Clock(start=50.0))
    calls_before = len(hive.calls)
    report = await second.run_pass()

    assert report.reclaimed == ()
    assert report.adopted == ("b1",)
    assert second_rt.scheduled == [], "adoption is not a second schedule()"
    assert set(second.in_flight) == {"b1"}
    assert second.in_flight["b1"].handle == RoleHandle("b1", sid["b1"])
    assert (second.in_flight["b1"].role, second.in_flight["b1"].action) == ("developer", "dispatch")
    assert "b1" in report.heartbeats, "the adopted claim is renewed in the same pass"
    assert report.orphans_reaped == ()
    assert report.harvested == (("b2", "blocked"),)
    assert report.causes == (("b2", localloop.CAUSE_BLOCKED),)
    assert hive.set_state_reason("b2") == (
        f"exit None; action dispatch; session {sid['b2']}; cost 0.25; needs a human"
    )
    assert hive.handle("b2") is None, "a harvested turn's handle is dropped"
    assert hive.handle("b1") is not None, "the adopted turn's handle stays"
    assert not any(c[:1] == ["update"] and "--status" in c for c in hive.calls[calls_before:])
    assert report.as_dict()["adopted"] == ["b1"]

    # The adopted session later finishes and is harvested like any other.
    hive.record("b1", sid["b1"])
    later = await second.run_pass()
    assert later.harvested == (("b1", "done"),)
    assert second_rt.scheduled == []
    assert hive.handle("b1") is None


@async_test
async def test_adoption_rebuilds_the_wall_time_cap_from_scheduled_at(
    tmp_path, hive, no_orphan_scan
):
    """The cap keeps counting from the ORIGINAL schedule across a restart: neither reset by the
    new monotonic clock nor fired at once (bh-03rvd review, carried)."""
    hive.add("b1")
    sessions: dict[str, str] = {}
    caps = localloop.Caps(max_concurrency=1, max_run_seconds=600.0)
    first = _loop(
        tmp_path,
        hive,
        CancellableRemote(tmp_path, sessions),
        caps=caps,
        claim=_claimer(hive, tmp_path, ["b1"]),
    )
    await first.run_pass()
    await first.shutdown()

    hive.now += timedelta(minutes=4)  # 240 s of the 600 s budget used while the loop was down
    clock = Clock(start=7.0)
    rt = CancellableRemote(tmp_path, sessions)
    second = _loop(tmp_path, hive, rt, caps=caps, clock=clock)
    await second.run_pass()
    assert second.in_flight["b1"].age(clock()) == 240.0

    clock.now += 359.0
    hive.now += timedelta(seconds=359)
    assert (await second.run_pass()).cancelled == (), "not reset, not fired at once"
    clock.now += 1.0
    hive.now += timedelta(seconds=1)
    capped = await second.run_pass()
    assert capped.cancelled == (("b1", "hard"),)
    assert hive.released("b1") and hive.handle("b1") is None


@async_test
async def test_adoption_skips_another_runtime_another_actor_and_unhandled_beads(
    tmp_path, hive, no_orphan_scan
):
    for bead in ("other-runtime", "other-actor", "no-handle"):
        hive.add(bead)
        hive.claim(bead)
    document = {
        "runtime": "gascity",
        "session_id": "s-x",
        "role": "developer",
        "action": "dispatch",
        "scheduled_at": _iso(T0),
    }
    hive.beads["other-runtime"]["metadata"]["bh.runtime.handle"] = {**document, "runtime": "k8s"}
    hive.beads["other-actor"]["metadata"]["bh.runtime.handle"] = document
    hive.beads["other-actor"]["assignee"] = "disp/someone-else"
    rt = RemoteRuntime(tmp_path, {})

    report = await _loop(tmp_path, hive, rt).run_pass()

    assert report.adopted == () and rt.observed == []


class EventHive(FakeHive):
    """FakeHive whose `set-state` also leaves the event child `bd set-state` creates, listed by
    `bd list --parent <bead>` the way a real store lists it (title, and the reason in the
    description)."""

    def __init__(self, beads=()):
        super().__init__(beads)
        self.events: dict[str, list[dict]] = {}

    def __call__(self, cmd, **kw):
        args = list(cmd[1:])
        while args and args[0] in ("-C", "--actor"):
            args = args[2:]
        args = [a for a in args if a != "--json"]
        if args[:1] == ["list"] and "--parent" in args:
            parent = args[args.index("--parent") + 1]
            if parent != "epic-1":
                self.calls.append(args)
                return _CP(0, json.dumps(self.events.get(parent, [])))
        if args[:1] == ["set-state"]:
            bead, (dim, value) = args[1], args[2].split("=", 1)
            reason = args[args.index("--reason") + 1]
            rows = self.events.setdefault(bead, [])
            rows.append(
                {
                    "id": f"{bead}.{len(rows) + 1}",
                    "issue_type": "event",
                    "status": "closed",
                    "title": f"State change: {dim} → {value}",
                    "description": f"Set {dim} to {value}\n\nReason: {reason}",
                    "created_at": _iso(self.now),
                }
            )
        return super().__call__(cmd, **kw)


@async_test
async def test_a_restart_never_writes_a_harvested_turns_cause_twice(
    tmp_path, monkeypatch, no_orphan_scan
):
    """Carry-forward from the bh-fwm82 review: `_forget_handle` is best-effort. When dropping the
    handle fails after the cause was written, the bead stays `in_progress` with a handle and a
    terminal record, and a restart must not write that permanent cause event a second time. A
    turn that finished while no loop ran is still harvested, once."""
    hive = EventHive()
    monkeypatch.setattr(bd_mod, "_run", hive)
    hive.add("b1")
    hive.add("b2")
    sessions: dict[str, str] = {}
    first = _loop(
        tmp_path,
        hive,
        RemoteRuntime(tmp_path, sessions),
        claim=_claimer(hive, tmp_path, ["b1", "b2"]),
    )
    assert (await first.run_pass()).dispatched == ("b1", "b2")
    sid = {bead: s for s, bead in sessions.items()}
    hive.record("b1", sid["b1"], status="blocked", summary="needs a human")

    real_clear = runtime_handle.clear

    def failing_clear(*_a, **_kw):
        raise runtime_handle.HandleWriteError("bd update --unset-metadata failed")

    monkeypatch.setattr(runtime_handle, "clear", failing_clear)
    report = await first.run_pass()
    assert report.causes == (("b1", localloop.CAUSE_BLOCKED),)
    assert hive.handle("b1") is not None, "the removal failed; the handle is still there"
    monkeypatch.setattr(runtime_handle, "clear", real_clear)
    await first.shutdown()
    hive.record("b2", sid["b2"], status="handoff", summary="over to review")

    second = _loop(tmp_path, hive, RemoteRuntime(tmp_path, sessions))
    restart = await second.run_pass()

    assert restart.harvested == (("b2", "handoff"),), "only the unharvested turn is harvested"
    assert restart.causes == (("b2", localloop.CAUSE_HANDOFF),)
    assert hive.written_states() == [
        ("b1", f"{localloop.DISPATCH_DIMENSION}={localloop.CAUSE_BLOCKED}"),
        ("b2", f"{localloop.DISPATCH_DIMENSION}={localloop.CAUSE_HANDOFF}"),
    ], "one permanent cause event per turn"
    assert hive.handle("b1") is None and hive.handle("b2") is None
    assert second.in_flight == {}


def test_cause_already_recorded_matches_the_turns_own_reason_only():
    reason = "exit None; action dispatch; session s-1; cost 0.25; needs a human"
    event = {
        "issue_type": "event",
        "title": f"State change: dispatch → {localloop.CAUSE_BLOCKED}",
        "description": f"Set dispatch to {localloop.CAUSE_BLOCKED}\n\nReason: {reason}",
    }
    assert localloop.cause_already_recorded([event], localloop.CAUSE_BLOCKED, reason)
    assert not localloop.cause_already_recorded([event], localloop.CAUSE_HANDOFF, reason)
    other_turn = reason.replace("s-1", "s-2")
    assert not localloop.cause_already_recorded([event], localloop.CAUSE_BLOCKED, other_turn)
    assert not localloop.cause_already_recorded([], localloop.CAUSE_BLOCKED, reason)

    # Scoped to the persisted handle's session: a match names THAT session, or it is not this
    # turn's (`s-1` is not a prefix match for `s-10`).
    assert localloop.cause_already_recorded(
        [event], localloop.CAUSE_BLOCKED, reason, session_id="s-1"
    )
    assert not localloop.cause_already_recorded(
        [event], localloop.CAUSE_BLOCKED, reason, session_id="s-10"
    )


def test_a_reason_naming_no_session_never_suppresses_the_handles_turn_cause():
    """A failure with no envelope quotes only its detail, so two turns can fail with the same
    reason; an earlier turn's event must not swallow the new turn's cause."""
    reason = "exit None; action dispatch; runtime gascity observe() raised 3 times in a row: boom"
    event = {
        "issue_type": "event",
        "title": f"State change: dispatch → {localloop.CAUSE_FAILED}",
        "description": f"Set dispatch to {localloop.CAUSE_FAILED}\n\nReason: {reason}",
    }
    assert localloop.cause_already_recorded([event], localloop.CAUSE_FAILED, reason)
    assert not localloop.cause_already_recorded(
        [event], localloop.CAUSE_FAILED, reason, session_id="s-2"
    )


# ---- acceptance 2: lease expiry wins -----------------------------------------------------------


@async_test
async def test_lease_expiry_wins_and_the_old_sessions_late_record_is_ignored(
    tmp_path, hive, no_orphan_scan
):
    hive.add("b1")
    sessions: dict[str, str] = {}
    first = _loop(
        tmp_path, hive, RemoteRuntime(tmp_path, sessions), claim=_claimer(hive, tmp_path, ["b1"])
    )
    await first.run_pass()
    (old_sid,) = sessions
    # The loop dies without a word; nothing heartbeats the claim and its lease runs out.
    hive.now += LEASE + timedelta(minutes=1)

    rt = RemoteRuntime(tmp_path, sessions)
    second = _loop(tmp_path, hive, rt, claim=_claimer(hive, tmp_path, ["b1"]))
    report = await second.run_pass()

    assert report.reclaimed == ("b1",), "bd reclaim reverted the expired claim"
    assert report.adopted == (), "a reclaimed bead is not adopted"
    assert RoleHandle("b1", old_sid) not in rt.observed
    assert report.dispatched == ("b1",), "re-dispatched as a fresh turn"
    new_sid = rt.scheduled[0]["session_id"]
    assert new_sid != old_sid
    assert hive.handle("b1").session_id == new_sid

    # The OLD session finally writes its record. It never answers for the new turn.
    hive.record("b1", old_sid, status="blocked", summary="late")
    later = await second.run_pass()
    assert later.harvested == () and later.causes == ()
    assert set(second.in_flight) == {"b1"}
    assert rt.observed[-1] == RoleHandle("b1", new_sid)


@async_test
async def test_an_expired_lease_inside_reclaims_grace_window_is_not_adopted(
    tmp_path, hive, no_orphan_scan
):
    """Lease expiry wins by `lease_expires_at` (operator decision 2026-11-26), not by what
    `bd reclaim` has reverted: inside the grace window the claim is still `in_progress` and
    still this actor's, and the restarted loop leaves it alone for reclaim."""
    from structlog.testing import capture_logs

    from beadhive import log

    hive.grace = timedelta(minutes=10)
    hive.add("b1")
    hive.add("b2")
    sessions: dict[str, str] = {}
    first = _loop(
        tmp_path,
        hive,
        CancellableRemote(tmp_path, sessions),
        claim=_claimer(hive, tmp_path, ["b1", "b2"]),
    )
    assert (await first.run_pass()).dispatched == ("b1", "b2")
    sid = {bead: s for s, bead in sessions.items()}
    await first.shutdown()

    # One minute past expiry: b1's session is still running, b2's finished while no loop ran.
    hive.now += LEASE + timedelta(minutes=1)
    hive.record("b2", sid["b2"], status="blocked", summary="needs a human")
    expired = {bead: hive.beads[bead]["lease_expires_at"] for bead in ("b1", "b2")}

    rt = CancellableRemote(tmp_path, sessions)
    second = _loop(tmp_path, hive, rt)
    calls_before = len(hive.calls)
    log.get_logger("warmup")
    with capture_logs() as captured:
        report = await second.run_pass()

    assert report.reclaimed == (), "inside the grace window bd reclaim reverts nothing"
    assert report.adopted == (), "an expired lease is not adopted"
    assert rt.observed == [] and rt.scheduled == [] and rt.cancelled == []
    assert second.in_flight == {}
    assert report.heartbeats == (), "an expired lease is not heartbeated back to life"
    assert report.harvested == () and report.causes == ()
    assert hive.written_states() == [], "no cause is written"
    new_calls = hive.calls[calls_before:]
    assert not any(c[:1] == ["heartbeat"] for c in new_calls)
    assert not any(c[:1] == ["update"] for c in new_calls), "no release, no handle write"
    for bead in ("b1", "b2"):
        row = hive.beads[bead]
        assert (row["status"], row["assignee"]) == ("in_progress", ACTOR), "left for reclaim"
        assert row["lease_expires_at"] == expired[bead], "the lease is not renewed"
        assert hive.handle(bead).session_id == sid[bead], "the handle is left as it is"
    refused = [e for e in captured if e["event"] == "session_not_adopted_lease_expired"]
    assert [(e["bead"], e["runtime"], e["session_id"]) for e in refused] == [
        ("b1", "gascity", sid["b1"]),
        ("b2", "gascity", sid["b2"]),
    ]

    # Past the grace window, the same loop's per-pass reclaim reverts both claims.
    hive.now += hive.grace
    assert (await second.run_pass()).reclaimed == ("b1", "b2")


@async_test
async def test_a_live_lease_is_still_adopted_when_reclaim_models_a_grace_window(
    tmp_path, hive, no_orphan_scan
):
    hive.grace = timedelta(minutes=10)
    hive.add("b1")
    sessions: dict[str, str] = {}
    first = _loop(
        tmp_path, hive, RemoteRuntime(tmp_path, sessions), claim=_claimer(hive, tmp_path, ["b1"])
    )
    await first.run_pass()
    (sid,) = sessions
    await first.shutdown()

    hive.now += LEASE - timedelta(seconds=1)  # one second of lease left
    report = await _loop(tmp_path, hive, RemoteRuntime(tmp_path, sessions)).run_pass()

    assert report.adopted == ("b1",)
    assert "b1" in report.heartbeats
    assert hive.handle("b1").session_id == sid


# ---- shutdown -----------------------------------------------------------------------------------


@async_test
async def test_lease_loss_cancels_adoptable_sessions_it_can_stop(tmp_path, hive):
    hive.add("b1")
    hive.claim("b1")
    rt = CancellableRemote(tmp_path, {})
    loop = _loop(tmp_path, hive, rt)
    await loop._spawn_for("b1", action="dispatch", role="developer", report=localloop.PassReport())

    await loop.shutdown(cause=localloop.CAUSE_LEASE_LOST, reason="lost", keep_adoptable=False)

    assert [h.bead_id for h in rt.cancelled] == ["b1"]
    assert hive.released("b1") and hive.handle("b1") is None


@async_test
async def test_lease_loss_leaves_a_session_it_cannot_stop_to_its_lease(tmp_path, hive):
    hive.add("b1")
    hive.claim("b1")
    rt = RemoteRuntime(tmp_path, {})
    loop = _loop(tmp_path, hive, rt)
    await loop._spawn_for("b1", action="dispatch", role="developer", report=localloop.PassReport())

    await loop.shutdown(cause=localloop.CAUSE_LEASE_LOST, reason="lost", keep_adoptable=False)

    assert not hive.released("b1"), "releasing would let a second session start"
    assert hive.written_states() == [] and hive.handle("b1") is not None
    assert loop.in_flight == {} and rt.closed == 1


class _FlakyCancel(localloop.LocalRuntime):
    """A non-adoptable runtime whose cancel raises for one bead, and which counts close()."""

    def __init__(self, fail_for):
        super().__init__()
        self.fail_for = fail_for
        self.cancelled: list[str] = []
        self.closes = 0

    def schedule(self, bead_id, role, *, workspace, instructions, session_id, model=None, **_kw):
        return RoleHandle(bead_id=bead_id, session_id=session_id)

    def observe(self, handle):
        return RoleOutcome(status="running")

    def cancel(self, handle):
        self.cancelled.append(handle.bead_id)
        if handle.bead_id == self.fail_for:
            raise ConnectionError("runtime unreachable")
        return RoleOutcome(status="failed", cancel_rung="cooperative")

    def close(self):
        self.closes += 1


@async_test
async def test_one_failing_cancel_does_not_strand_the_rest_of_shutdown(tmp_path, hive):
    for bead in ("b1", "b2"):
        hive.add(bead)
        hive.claim(bead)
    rt = _FlakyCancel(fail_for="b1")
    loop = _loop(tmp_path, hive, rt)
    for bead in ("b1", "b2"):
        await loop._spawn_for(
            bead, action="dispatch", role="developer", report=localloop.PassReport()
        )

    await loop.shutdown()

    assert rt.cancelled == ["b1", "b2"], "every seat is attempted"
    assert hive.released("b1") and hive.released("b2")
    assert hive.set_state_reason("b1") == "loop shutting down (rung error, session unknown)"
    assert rt.closes == 1 and loop.in_flight == {}
    assert hive.handle_writes() == [], "not adoptable: no handle metadata"


class _ClosableFake:
    name = "closable"

    def __init__(self, close_error=None):
        self.closes = 0
        self.close_error = close_error

    def schedule(self, bead_id, role, *, workspace, instructions, session_id, model=None):
        return RoleHandle(bead_id=bead_id, session_id=session_id)

    def observe(self, handle):
        return RoleOutcome(status="running")

    def on_gate_resolved(self, resolution):
        pass

    def close(self):
        self.closes += 1
        if self.close_error is not None:
            raise self.close_error


@async_test
async def test_shutdown_closes_a_passed_in_closable_runtime(tmp_path, hive):
    rt = _ClosableFake()
    loop = _loop(tmp_path, hive, rt)
    await loop.shutdown()
    await loop.shutdown()
    assert rt.closes == 2, "close() is called on every shutdown; the runtime makes it idempotent"


@async_test
async def test_a_close_failure_never_masks_the_error_that_stopped_the_loop(tmp_path, hive):
    rt = _ClosableFake(close_error=RuntimeError("close blew up"))
    loop = _loop(tmp_path, hive, rt)

    async def boom():
        raise KeyError("the real failure")

    loop.run_pass = boom
    with pytest.raises(KeyError, match="the real failure"):
        await loop.run(max_passes=1)
    assert rt.closes == 1


@async_test
async def test_a_close_failure_on_a_clean_exit_propagates(tmp_path, hive):
    rt = _ClosableFake(close_error=RuntimeError("close blew up"))
    loop = _loop(tmp_path, hive, rt)
    with pytest.raises(RuntimeError, match="close blew up"):
        await loop.run(max_passes=1)


# ---- the transient-error policy -----------------------------------------------------------------


class _FlakyObserve(RemoteRuntime):
    def __init__(self, hive_dir, failures):
        super().__init__(hive_dir, {})
        self.failures = list(failures)

    def observe(self, handle):
        self.observed.append(handle)
        if self.failures:
            exc = self.failures.pop(0)
            if exc is not None:
                raise exc
        return outcome.observe(handle, cwd=self.hive_dir)


@async_test
async def test_a_transient_observe_error_is_retried_next_pass(tmp_path, hive):
    hive.add("b1")
    hive.claim("b1")
    rt = _FlakyObserve(tmp_path, [ConnectionError("blip"), None, ConnectionError("blip")])
    loop = _loop(tmp_path, hive, rt, runtime_error_limit=2)
    await loop._spawn_for("b1", action="dispatch", role="developer", report=localloop.PassReport())

    for _ in range(3):  # raise, clean (resets the count), raise: never two in a row
        report = localloop.PassReport()
        await loop._harvest(report)
        assert report.harvested == () and "b1" in loop.in_flight

    assert hive.written_states() == []


@async_test
async def test_observe_failing_past_the_limit_fails_the_bead(tmp_path, hive):
    hive.add("b1")
    hive.claim("b1")
    rt = _FlakyObserve(tmp_path, [ConnectionError("down")] * 3)
    loop = _loop(tmp_path, hive, rt, runtime_error_limit=3)
    await loop._spawn_for("b1", action="dispatch", role="developer", report=localloop.PassReport())

    reports = []
    for _ in range(3):
        reports.append(localloop.PassReport())
        await loop._harvest(reports[-1])

    assert [r.harvested for r in reports] == [(), (), (("b1", "incomplete"),)]
    assert reports[-1].causes == (("b1", localloop.CAUSE_FAILED),)
    assert hive.set_state_reason("b1") == (
        "exit None; action dispatch; runtime gascity observe() raised 3 times in a row: down"
    )
    assert loop.in_flight == {} and hive.handle("b1") is None


class _FlakyEverything(CancellableRemote):
    """observe() always raises (until the list runs out), and so does cancel()."""

    def __init__(self, hive_dir, failures):
        super().__init__(hive_dir, {})
        self.failures = list(failures)
        self.cancel_error = ConnectionError("unreachable")

    def observe(self, handle):
        self.observed.append(handle)
        if self.failures:
            raise self.failures.pop(0)
        return outcome.observe(handle, cwd=self.hive_dir)


@async_test
async def test_the_cap_keeps_the_failed_answer_that_reaches_the_error_limit(tmp_path, hive):
    """Carry-forward from the bh-fwm82 review: the wall-time cap observes before it cancels. When
    ITS observe() is the raise that reaches the limit, the `failed` answer is settled there, not
    dropped for the harvest to observe again (which also reset the count, so the bead failed
    only after twice the budget). The limit counts raises, wherever they come from."""
    hive.add("b1")
    hive.claim("b1")
    clock = Clock()
    rt = _FlakyEverything(tmp_path, [ConnectionError("down")] * 6)
    loop = _loop(
        tmp_path,
        hive,
        rt,
        clock=clock,
        caps=localloop.Caps(max_run_seconds=10.0),
        runtime_error_limit=3,
    )
    await loop._spawn_for("b1", action="dispatch", role="developer", report=localloop.PassReport())
    clock.now += 11

    first = localloop.PassReport()
    await loop._enforce_wall_time(first)  # raise 1 (running), then cancel raises: retried later
    await loop._harvest(first)  # raise 2
    assert first.harvested == () and "b1" in loop.in_flight

    second = localloop.PassReport()
    await loop._enforce_wall_time(second)  # raise 3: the limit, answered failed, settled here
    await loop._harvest(second)

    assert len(rt.observed) == 3, "failed after exactly three consecutive raises"
    assert second.harvested == (("b1", "incomplete"),)
    assert second.causes == (("b1", localloop.CAUSE_FAILED),)
    assert hive.set_state_reason("b1") == (
        "exit None; action dispatch; runtime gascity observe() raised 3 times in a row: down"
    )
    assert loop.in_flight == {} and hive.handle("b1") is None
    assert second.cancelled == ()


@async_test
async def test_a_raising_cancel_under_the_cap_is_retried_then_left_running(tmp_path, hive):
    """An adoptable session the cap cannot stop stays in flight (no second session for the bead):
    cancel is retried while the error budget lasts, then the cap is given up, once."""
    hive.add("b1")
    hive.claim("b1")
    clock = Clock()
    rt = CancellableRemote(tmp_path, {})
    rt.cancel_error = ConnectionError("unreachable")
    loop = _loop(
        tmp_path,
        hive,
        rt,
        clock=clock,
        caps=localloop.Caps(max_run_seconds=10.0),
        runtime_error_limit=2,
    )
    await loop._spawn_for("b1", action="dispatch", role="developer", report=localloop.PassReport())
    clock.now += 11

    for _ in range(4):
        report = localloop.PassReport()
        await loop._enforce_wall_time(report)
        assert report.cancelled == ()

    assert len(rt.cancelled) == 2, "retried up to the limit, then not again"
    assert "b1" in loop.in_flight and not hive.released("b1")
    assert hive.written_states() == []


@async_test
async def test_an_adoptable_runtime_without_cancel_keeps_a_capped_session_in_flight(tmp_path, hive):
    hive.add("b1")
    hive.claim("b1")
    clock = Clock()
    rt = RemoteRuntime(tmp_path, {})
    loop = _loop(tmp_path, hive, rt, clock=clock, caps=localloop.Caps(max_run_seconds=10.0))
    await loop._spawn_for("b1", action="dispatch", role="developer", report=localloop.PassReport())
    clock.now += 11

    report = localloop.PassReport()
    await loop._enforce_wall_time(report)

    assert report.cancelled == () and "b1" in loop.in_flight
    assert not hive.released("b1"), "releasing would let a second session start"


# ---- the typed ScheduleRefused check ------------------------------------------------------------


class _PayloadValueError(ValueError):
    """A third-party ValueError that happens to carry status/reason/payload attributes."""

    status = "blocked"
    reason = ""
    payload = ["not", "a", "mapping"]


class _Refusing(RemoteRuntime):
    def __init__(self, hive_dir, exc):
        super().__init__(hive_dir, {})
        self.exc = exc

    def schedule(self, bead_id, role, *, workspace, instructions, session_id, model=None):
        raise self.exc


@async_test
async def test_only_a_real_schedule_refused_is_read_for_status_and_payload(tmp_path, hive):
    hive.add("b1")
    loop = _loop(tmp_path, hive, _Refusing(tmp_path, _PayloadValueError("pool exhausted")))
    report = localloop.PassReport()

    await loop._spawn_for("b1", action="dispatch", role="developer", report=report)

    assert report.causes == (("b1", localloop.CAUSE_FAILED),)
    assert report.routing == ()
    assert hive.set_state_reason("b1") == ("runtime gascity refused to schedule b1: pool exhausted")
    assert hive.handle_writes() == [], "nothing scheduled, so nothing persisted"

    typed = ScheduleRefused("no", status="blocked", reason="quota", payload={"bead": "b1"})
    loop = _loop(tmp_path, hive, _Refusing(tmp_path, typed))
    report = localloop.PassReport()
    await loop._spawn_for("b1", action="dispatch", role="developer", report=report)
    assert report.causes == (("b1", localloop.CAUSE_BLOCKED),)
    assert report.routing == (("b1", {"bead": "b1"}),)


# ---- acceptance 4: the reclaim guard -------------------------------------------------------------


def _row(**kw):
    row = {
        "id": "b1",
        "status": "in_progress",
        "assignee": ACTOR,
        "lease_expires_at": _iso(T0 + LEASE),
        "metadata": {
            "bh.runtime.handle": {
                "runtime": "gascity",
                "session_id": "s-1",
                "role": "developer",
                "action": "dispatch",
                "scheduled_at": _iso(T0),
            }
        },
    }
    row.update(kw)
    return row


def test_reclaim_refuses_a_live_handle_naming_the_runtime():
    message = runtime_handle.reclaim_refusal(_row(), now=T0 + timedelta(minutes=1))
    assert message is not None
    assert "'gascity' runtime handle" in message and "session s-1" in message
    assert "second 'gascity' session" in message


def test_reclaim_proceeds_once_the_handle_is_terminal_or_the_lease_expired():
    later = T0 + timedelta(minutes=1)
    assert runtime_handle.reclaim_refusal(_row(metadata={}), now=later) is None
    assert runtime_handle.reclaim_refusal(_row(status="open"), now=later) is None
    assert runtime_handle.reclaim_refusal(_row(), now=T0 + LEASE) is None, "lease expiry wins"
    finished = _row()
    finished["metadata"]["bh.outcome.s-1"] = {
        "outcome": {"status": "done", "summary": "ok"},
        "session_id": "s-1",
        "cost_usd": 0.0,
        "usage": {},
        "packs": [],
    }
    assert runtime_handle.reclaim_refusal(finished, now=later) is None
    stale = _row()
    stale["metadata"]["bh.outcome.s-0"] = finished["metadata"]["bh.outcome.s-1"] | {
        "session_id": "s-0"
    }
    assert runtime_handle.reclaim_refusal(stale, now=later) is not None, (
        "another session's record never answers for this one"
    )


def test_a_row_without_a_readable_lease_is_not_expired():
    row = _row()
    row.pop("lease_expires_at")
    assert runtime_handle.reclaim_refusal(row, now=T0 + timedelta(days=1)) is not None
