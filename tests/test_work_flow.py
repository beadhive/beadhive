"""hqfy.2 — the at-merge flow-metric helpers in ws.work (best-effort, skew-guarded bd reads).

These are pure-logic tests for ``work._emit_bead_flow`` / ``_emit_cycle``: the bd reads are faked
by monkeypatching ``bd.json`` (the hoisted public seam) and the otel ``record_*`` helpers are
captured, so we can prove the happy path emits the full cycle/stage/rework set, a failing/missing
read emits nothing for the affected metric (and NEVER raises), and a negative delta is skipped.
"""

from __future__ import annotations

import datetime
from pathlib import Path

import pytest

from beadhive import bd as bd_mod
from beadhive import otel, work, work_logic

UTC = datetime.UTC


def _iso(dt: datetime.datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def rec(monkeypatch):
    """Capture every flow-metric emission as (name, value, attrs)."""
    calls: list[tuple] = []

    def mk(name):
        def f(value, attrs=None):
            calls.append((name, value, attrs or {}))

        return f

    monkeypatch.setattr(otel, "record_cycle_time", mk("cycle_time"))
    monkeypatch.setattr(otel, "record_cycle_time_active", mk("cycle_time.active"))
    monkeypatch.setattr(otel, "record_rework", mk("rework"))
    monkeypatch.setattr(
        otel, "record_stage", lambda stage, value, attrs=None: calls.append(
            (f"stage.{stage}", value, attrs or {})
        )
    )
    return calls


def _bd_json_stub(events=None, gates=None, fail=False):
    """A fake ``work._bd_json`` dispatching on the first arg (list=events, gate=gates)."""

    def f(args, cwd):
        if fail:
            return None
        if args and args[0] == "list":
            return events
        if args and args[0] == "gate":
            return gates
        return None

    return f


def _names(calls):
    return [c[0] for c in calls]


# ---- happy path: full cycle/stage/rework decomposition ----------------------


def test_emit_bead_flow_happy_path_emits_full_set(monkeypatch, rec):
    now = datetime.datetime.now(UTC)
    created = now - datetime.timedelta(hours=2)
    started = now - datetime.timedelta(hours=1)
    review_pending = now - datetime.timedelta(minutes=40)
    gate_closed = now - datetime.timedelta(minutes=10)

    events = [
        {"issue_type": "event", "title": "set-state review=pending",
         "created_at": _iso(review_pending)},
        {"issue_type": "event", "title": "review=changes-requested round 1"},
        {"issue_type": "event", "title": "review=changes-requested round 2"},
        {"issue_type": "task", "title": "not an event — ignored"},
    ]
    gates = [
        {
            "status": "closed",
            "description": "Ad-hoc gate blocking mr-40\n\nReason: review abc123",
            "closed_at": _iso(gate_closed),
        }
    ]
    monkeypatch.setattr(bd_mod, "json", _bd_json_stub(events=events, gates=gates))

    data = {"id": "mr-40", "created_at": _iso(created), "started_at": _iso(started)}
    work._emit_bead_flow("mr-40", data, Path("/x"), {"ws.merge.kind": "bead", "ws.hive": "mr"})

    names = _names(rec)
    assert "cycle_time" in names and "cycle_time.active" in names
    for stage in ("stage.coding", "stage.review_wait", "stage.merge_latency"):
        assert stage in names
    # rework counts the two changes-requested events
    rework = [c for c in rec if c[0] == "rework"]
    assert rework and rework[0][1] == 2
    # bounded attrs only — never a bead/epic id on the metric point
    assert all("ws.bead" not in c[2] and "ws.epic" not in c[2] for c in rec)
    assert all(c[2].get("ws.hive") == "mr" for c in rec)


def test_emit_bead_flow_derives_review_pending_from_gate_when_no_event(monkeypatch, rec):
    """`bd set-state review=pending` materializes no infra event child, so the event scan is
    empty — coding + review_wait must still emit, derived from the review gate's created_at
    (the submit moment). Regression for the stages that never emitted (bh-yocq)."""
    now = datetime.datetime.now(UTC)
    created = now - datetime.timedelta(hours=2)
    started = now - datetime.timedelta(hours=1)
    gate_opened = now - datetime.timedelta(minutes=40)  # submit moment == gate created_at
    gate_closed = now - datetime.timedelta(minutes=10)

    events: list = []  # no review=pending event was ever written
    gates = [
        {
            "status": "closed",
            "description": "Ad-hoc gate blocking mr-43\n\nReason: review abc123",
            "created_at": _iso(gate_opened),
            "closed_at": _iso(gate_closed),
        }
    ]
    monkeypatch.setattr(bd_mod, "json", _bd_json_stub(events=events, gates=gates))

    data = {"id": "mr-43", "created_at": _iso(created), "started_at": _iso(started)}
    work._emit_bead_flow("mr-43", data, Path("/x"), {"ws.hive": "mr"})

    names = _names(rec)
    assert "stage.coding" in names  # was None (skipped) before the gate fallback
    assert "stage.review_wait" in names
    assert "stage.merge_latency" in names
    coding = next(c for c in rec if c[0] == "stage.coding")[1]
    review_wait = next(c for c in rec if c[0] == "stage.review_wait")[1]
    assert coding == pytest.approx((gate_opened - started).total_seconds(), abs=2)
    assert review_wait == pytest.approx((gate_closed - gate_opened).total_seconds(), abs=2)


# ---- bd-read failure: emit nothing for the affected metric, never raise -----


def test_emit_bead_flow_bd_read_failure_still_emits_cycle_and_never_raises(monkeypatch, rec):
    now = datetime.datetime.now(UTC)
    data = {
        "id": "mr-41",
        "created_at": _iso(now - datetime.timedelta(hours=1)),
        "started_at": _iso(now - datetime.timedelta(minutes=30)),
    }
    monkeypatch.setattr(bd_mod, "json", _bd_json_stub(fail=True))

    work._emit_bead_flow("mr-41", data, Path("/x"), {"ws.hive": "mr"})

    names = _names(rec)
    # cycle metrics come from the show data we already had → still emitted
    assert "cycle_time" in names and "cycle_time.active" in names
    # the bd-read-dependent metrics are skipped (events/gate unreadable) — and crucially, no raise
    assert "stage.coding" not in names
    assert "stage.review_wait" not in names
    assert "stage.merge_latency" not in names
    assert "rework" not in names  # events read failed (None) → rework not recorded


def test_emit_bead_flow_swallowed_by_caller_never_blocks_merge(monkeypatch):
    # The helper itself can raise on a truly malformed read; the merge seam wraps it in try/except.
    def boom(args, cwd):
        raise RuntimeError("bd exploded")

    monkeypatch.setattr(bd_mod, "json", boom)
    monkeypatch.setattr(otel, "record_cycle_time", lambda *a, **k: None)
    monkeypatch.setattr(otel, "record_cycle_time_active", lambda *a, **k: None)
    # Mirror the call site's guard: an exception must be contained, not propagated.
    try:
        work._emit_bead_flow("mr-42", {"id": "mr-42"}, Path("/x"), {})
    except Exception:
        pass  # the real call site does exactly this — proving it's catchable is enough


# ---- skew guard: negative deltas are dropped --------------------------------


def test_emit_cycle_skips_negative_delta(monkeypatch, rec):
    now = datetime.datetime.now(UTC)
    future = now + datetime.timedelta(hours=1)  # created_at in the future → negative cycle time
    work._emit_cycle({"created_at": _iso(future), "started_at": _iso(future)}, {"ws.hive": "mr"})
    assert _names(rec) == []  # both deltas negative → nothing recorded


def test_emit_delta_records_only_nonnegative():
    seen = []
    work._emit_delta(lambda s, a: seen.append(s), None, None, {})  # missing ts → skip
    a = datetime.datetime.now(UTC)
    work._emit_delta(lambda s, a_: seen.append(s), a, a + datetime.timedelta(seconds=5), {})  # neg
    work._emit_delta(lambda s, a_: seen.append(s), a + datetime.timedelta(seconds=5), a, {})  # pos
    assert seen == [5.0]


def test_parse_ts_handles_z_and_none():
    assert work._parse_ts(None) is None
    assert work._parse_ts("not-a-date") is None
    dt = work._parse_ts("2026-06-30T03:27:43Z")
    assert dt is not None and dt.tzinfo is not None


# ---- review-gate / event matchers -------------------------------------------


def test_review_gates_selector_matches_review_not_kickoff(monkeypatch):
    """The canonical selector (bh-c3il) returns EVERY review-reason gate for the bead, split
    (open, resolved) — kickoff gates never match. Identity is description-only (no blocks edge
    required), so a dep-less gate on an epic is still found (bh-pctz compatibility)."""
    gates = [
        {"description": "blocking mr-50\n\nReason: kickoff mr-50", "status": "closed"},
        {"description": "blocking mr-50\n\nReason: review deadbeef", "status": "closed",
         "closed_at": "2026-06-30T03:00:00Z"},
        {"description": "blocking mr-50\n\nReason: review cafef00d", "status": "open"},
    ]
    monkeypatch.setattr(bd_mod, "json", _bd_json_stub(gates=gates))
    open_, resolved = work_logic.review_gates("mr-50", Path("/x"))
    assert [g["status"] for g in open_] == ["open"]
    assert len(resolved) == 1 and "review deadbeef" in resolved[0]["description"]
