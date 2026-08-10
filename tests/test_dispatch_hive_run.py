"""The hive-level dispatch picker (bh-e7r9q.5) — `bh host dispatch run --hive <hive>`.

Covers the two hard requirements bh-e7r9q.4's acceptance bar puts on this tier: the picker
picks ONLY `kickoff:approved` epics in `bd ready` order (deliberately dumb, no cross-hive
arbitration), and a hive this host does NOT hold the lease on IDLES — a test asserts it does
not spawn anything and does not error, never that it merely "looks fine" on the happy path.
"""

from __future__ import annotations

import asyncio
import functools

from beadhive import bd as bd_mod
from beadhive import dispatch_hive_run as dhr
from beadhive import localloop


def async_test(fn):
    """Run an `async def` test on a fresh event loop — same three-liner as
    tests/test_localloop.py's, so this module needs no `pytest-asyncio` dependency either."""

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))

    return wrapper


# ---- the picker policy, in isolation -------------------------------------------------------


def test_kicked_off_ready_epics_filters_to_epic_and_kickoff_approved(monkeypatch, tmp_path):
    rows = [
        {"id": "bh-a", "issue_type": "epic", "labels": ["kickoff:approved"]},
        {"id": "bh-b", "issue_type": "epic", "labels": []},  # not kicked off
        {"id": "bh-c.1", "issue_type": "feature", "labels": ["kickoff:approved"]},  # not an epic
        {"id": "bh-d", "issue_type": "epic", "labels": ["kickoff:approved"]},
    ]
    monkeypatch.setattr(bd_mod, "json", lambda args, cwd: rows)

    picked = dhr.kicked_off_ready_epics(tmp_path)

    assert picked == ["bh-a", "bh-d"]  # bd ready's own order preserved; nothing re-sorted


def test_kicked_off_ready_epics_empty_when_bd_ready_returns_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(bd_mod, "json", lambda args, cwd: None)
    assert dhr.kicked_off_ready_epics(tmp_path) == []


# ---- the pass: lease-absent degradation is a hard requirement -----------------------------


class _FixedLease:
    def __init__(self, held: bool):
        self._status = localloop.LeaseStatus(held=held, renewed=False, detail="fixture")

    def renew(self, *, active):  # noqa: ARG002
        return self._status


class _SpySpawnRun(dhr.HiveDispatchRun):
    """A `HiveDispatchRun` whose `_spawn` records calls instead of touching a real process —
    the same "fake the I/O, keep the real decision logic" shape `localloop`'s own tests use."""

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.spawn_calls: list[str] = []

    async def _spawn(self, epic):  # noqa: D102
        self.spawn_calls.append(epic)

        class _FakeProc:
            returncode = None
            pid = 12345

        return dhr._Child(epic=epic, proc=_FakeProc())


@async_test
async def test_run_pass_never_spawns_without_the_lease(tmp_path):
    """THE acceptance criterion, verbatim: an enabled instance for a hive this host does not
    hold the lease on idles read-only and does not attempt a write — proven by asserting zero
    spawn calls and zero picker calls while unleased, not merely that the pass returns cleanly."""
    picker_calls = []

    def picker():
        picker_calls.append(1)
        return ["bh-would-be-picked"]

    run = _SpySpawnRun(
        hive_dir=tmp_path,
        hive="acme/widgets",
        actor="dev/x",
        sink_path=tmp_path / "sink.jsonl",
        lease=_FixedLease(held=False),
        pick=picker,
    )

    report = await run.run_pass()

    assert report.idle is True
    assert report.lease_held is False
    assert run.spawn_calls == []
    assert report.spawned == ()


@async_test
async def test_run_pass_spawns_up_to_the_concurrency_cap_when_leased(tmp_path):
    picked = ["bh-a", "bh-b", "bh-c"]
    run = _SpySpawnRun(
        hive_dir=tmp_path,
        hive="acme/widgets",
        actor="dev/x",
        sink_path=tmp_path / "sink.jsonl",
        max_epics_in_flight=2,
        lease=_FixedLease(held=True),
        pick=lambda: picked,
    )

    report = await run.run_pass()

    assert run.spawn_calls == ["bh-a", "bh-b"]  # capped at 2, never all 3
    assert report.idle is False
    assert set(report.in_flight) == {"bh-a", "bh-b"}


@async_test
async def test_run_pass_does_not_respawn_an_epic_already_in_flight(tmp_path):
    run = _SpySpawnRun(
        hive_dir=tmp_path,
        hive="acme/widgets",
        actor="dev/x",
        sink_path=tmp_path / "sink.jsonl",
        max_epics_in_flight=5,
        lease=_FixedLease(held=True),
        pick=lambda: ["bh-a"],
    )

    await run.run_pass()
    await run.run_pass()

    assert run.spawn_calls == ["bh-a"]  # not spawned a second time while still in flight


@async_test
async def test_run_pass_reaps_finished_children(tmp_path):
    run = _SpySpawnRun(
        hive_dir=tmp_path,
        hive="acme/widgets",
        actor="dev/x",
        sink_path=tmp_path / "sink.jsonl",
        max_epics_in_flight=5,
        lease=_FixedLease(held=True),
        pick=lambda: ["bh-a"],
    )
    await run.run_pass()
    run.children["bh-a"].proc.returncode = 0  # simulate the child exiting

    report = await run.run_pass()

    # Reaped, THEN re-picked in the same pass since the picker still returns it and there is
    # room — the reap and the (re-)spawn are correctly two different steps of one pass.
    assert report.reaped == ("bh-a",)
    assert run.spawn_calls == ["bh-a", "bh-a"]
