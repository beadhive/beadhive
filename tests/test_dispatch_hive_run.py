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


def _run(tmp_path, **kw):
    """A `_SpySpawnRun` with the boilerplate filled in."""
    kw.setdefault("hive_dir", tmp_path)
    kw.setdefault("hive", "acme/widgets")
    kw.setdefault("actor", "dev/x")
    kw.setdefault("sink_path", tmp_path / "sink.jsonl")
    return _SpySpawnRun(**kw)


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
    assert set(report.epics_in_flight) == {"bh-a", "bh-b"}


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


# ---- the ready read must not be truncated (bh-fruer, P0) -----------------------------------


def test_the_picker_asks_for_the_WHOLE_ready_set_not_bd_s_default_100(monkeypatch, tmp_path):
    """`bd ready` caps at 100 rows by default. Measured on this hive 2026-08-10: 20 epics
    visible, 48 actually ready. Without `--limit 0` every epic past position 100 is never
    dispatched and nothing reports it — the picker cannot even tell a truncated read from a
    short one. Assert the flag, because the failure is invisible from the outside.
    """
    seen: list[list[str]] = []

    def fake_json(args, cwd):  # noqa: ARG001
        seen.append(list(args))
        return []

    monkeypatch.setattr(bd_mod, "json", fake_json)
    dhr.kicked_off_ready_epics(tmp_path)

    assert seen == [["ready", "--limit", "0"]]
    # `bd.json` appends its own `--json`; passing a second one here was redundant.
    assert "--json" not in seen[0]


# ---- respawn backoff: permanent event beads are the cost of getting this wrong -------------


def test_backoff_delay_doubles_and_caps():
    assert dhr.backoff_delay(0) == 0.0
    assert dhr.backoff_delay(1) == dhr.BACKOFF_BASE_SECONDS
    assert dhr.backoff_delay(2) == dhr.BACKOFF_BASE_SECONDS * 2
    assert dhr.backoff_delay(3) == dhr.BACKOFF_BASE_SECONDS * 4
    assert dhr.backoff_delay(99) == dhr.BACKOFF_MAX_SECONDS


class _ExitedProc:
    def __init__(self, returncode: int):
        self.returncode = returncode
        self.pid = 1234


@async_test
async def test_a_halted_epic_is_not_respawned_on_the_very_next_pass(tmp_path):
    """A `blocked` bead awaiting human triage makes `bh work loop` escalate, halt and exit 1.
    Re-picking it every `poll_interval` (default 10s) is ~8,640 respawns a day against one stuck
    bead, and this hive has no compaction tier (`bd compact`/`bd flatten` forbidden until
    bh-3vs6c lands) so anything the child writes per cycle is PERMANENT."""
    run = _run(tmp_path, pick=lambda: ["bh-a"], lease=_FixedLease(held=True))
    run.children["bh-a"] = dhr._Child(epic="bh-a", proc=_ExitedProc(1))

    report = await run.run_pass()

    assert report.reaped == ("bh-a",)
    assert report.spawned == (), "the failed epic must not be respawned in the same pass"
    assert report.deferred == ("bh-a",), "and the skip must be legible, not silent"
    assert run.backoff["bh-a"][0] == 1


@async_test
async def test_backoff_expires_and_a_clean_exit_clears_the_history(tmp_path):
    run = _run(tmp_path, pick=lambda: ["bh-a"], lease=_FixedLease(held=True))

    # Arm the backoff, then pretend it has elapsed: the epic becomes pickable again.
    run.backoff["bh-a"] = (2, 0.0)
    report = await run.run_pass()
    assert report.spawned == ("bh-a",)

    # A clean (exit 0) child forgets the history entirely — backoff punishes a repeating
    # FAILURE, never a slow-but-healthy epic.
    run.children["bh-a"] = dhr._Child(epic="bh-a", proc=_ExitedProc(0))
    await run.run_pass()
    assert "bh-a" not in run.backoff


# ---- --dry-run / --seat-binary forwarded onto every spawned child (bh-3xl60) ----------------


class _CapturedExec:
    """Captures the argv `HiveDispatchRun._spawn` would exec, instead of really spawning."""

    def __init__(self):
        self.argv: list[str] | None = None

    async def __call__(self, *argv, **_kw):
        self.argv = list(argv)

        class _FakeProc:
            returncode = None
            pid = 1

        return _FakeProc()


def _argv_capturing_run(tmp_path, monkeypatch, **kw):
    captured = _CapturedExec()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", captured)
    kw.setdefault("hive_dir", tmp_path)
    kw.setdefault("hive", "acme/widgets")
    kw.setdefault("actor", "dev/x")
    kw.setdefault("sink_path", tmp_path / "sink.jsonl")
    kw.setdefault("bh_binary", "bh")
    return dhr.HiveDispatchRun(**kw), captured


@async_test
async def test_spawn_forwards_dry_run_onto_the_child_argv(tmp_path, monkeypatch):
    run, captured = _argv_capturing_run(tmp_path, monkeypatch, dry_run=True)
    await run._spawn("bh-epic-1")
    assert captured.argv == [
        "bh",
        "work",
        "loop",
        "bh-epic-1",
        "--json",
        "--hive",
        "acme/widgets",
        "--as",
        "dev/x",
        "--dry-run",
    ]


@async_test
async def test_spawn_forwards_seat_binary_onto_the_child_argv(tmp_path, monkeypatch):
    run, captured = _argv_capturing_run(tmp_path, monkeypatch, seat_binary="/path/to/stub_seat.py")
    await run._spawn("bh-epic-1")
    assert captured.argv[-2:] == ["--seat-binary", "/path/to/stub_seat.py"]


@async_test
async def test_spawn_forwards_neither_flag_by_default(tmp_path, monkeypatch):
    run, captured = _argv_capturing_run(tmp_path, monkeypatch)
    await run._spawn("bh-epic-1")
    assert "--dry-run" not in captured.argv
    assert "--seat-binary" not in captured.argv


def test_build_run_threads_dry_run_and_seat_binary(monkeypatch, tmp_path):
    """`bh host dispatch run --hive <hive> --dry-run --seat-binary <path>` — the CLI-facing
    constructor forwards both onto the `HiveDispatchRun` it assembles."""
    from beadhive import config, dispatch_log, registry

    monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: tmp_path)
    monkeypatch.setattr(registry, "entry_for_dir", lambda cfg, main: {})
    monkeypatch.setattr(config, "work_identity", lambda cfg, entry: {"name": "dev/x"})
    monkeypatch.setattr(dispatch_log, "ensure_sink_dir", lambda: None)
    monkeypatch.setattr(dispatch_log, "sink_path", lambda cfg, entry: tmp_path / "sink.jsonl")
    monkeypatch.setattr(localloop, "lease_keeper_for", lambda *a, **k: localloop.NullLeaseKeeper())

    run = dhr.build_run("acme/widgets", cfg={}, dry_run=True, seat_binary="/path/to/stub_seat.py")

    assert run.dry_run is True
    assert run.seat_binary == "/path/to/stub_seat.py"
