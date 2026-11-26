"""The loop probe: an opt-in kit tier that drives the candidate runtime through BeadHive's REAL
dispatcher loop (bh-lxv3v).

The static checks in :mod:`.checks` call ``schedule`` / ``observe`` / ``on_gate_resolved``
directly. They prove the runtime keeps the ADR invariants, not that ``bh work loop`` can drive it.
This tier closes that gap. It seeds one epic and one ready child in the disposable
:class:`~.bdhive.BdHive`, builds a :class:`beadhive.localloop.LocalLoop` over the candidate
(``runtime=`` and its ``on_gate_resolved``, exactly as ``bh work loop`` wires them), and runs
real passes over one turn:

1. **dispatch** — the loop claims the child and calls ``schedule()`` once;
2. **in flight** — a pass while the turn is held: ``observe()`` answers ``running``, so the bead
   stays in flight, its claim is heartbeated, and ``bd reclaim`` leaves it alone;
3. **harvest** — once the turn is released, the loop harvests ``done`` and writes no cause. The
   stand-in seat must have started exactly once, under the session id the loop passed.

Around the turn the bead is diffed as in ``no-lifecycle-writes``, with the loop's OWN writes
attributed to the loop: the claim (taken before the "before" snapshot), the heartbeat (not in the
snapshot), and the ``bh.runtime.handle`` document an adoptable runtime's turn carries
(:mod:`beadhive.runtime_handle`). A non-adoptable runtime gets no handle from the loop, so a
handle key on its bead is the runtime's write.

Failures are CLASSED: a runtime fault names its ADR invariant and is GC-fixable or structural; a
fault in BeadHive's own loop (it did not dispatch a ready bead, a heartbeat failed, it wrote a
cause for a done turn, it left its handle behind, ``run_pass`` raised outside the runtime) is
``bh-loop`` with fix class :data:`~.checks.BH_SIDE`, so it is never reported as GC-fixable. An
exception raised while the runtime's own method was executing stays a runtime crash.

The loop runs against the scratch store only: for the probe's duration ``BH_HOME`` and
``BH_CONFIG`` point at a throwaway config that selects the ``cli`` beads route for it, and the
caller's shared-server variables are unset (both restored afterwards). Needs a :class:`BdHive`
(``bd`` on ``PATH``); on any other hive the probe fails as ``bh-loop``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from collections.abc import Callable, Iterator
from pathlib import Path

from .bdhive import ACTOR, BdHive
from .checks import (
    ConformanceFailure,
    RuntimeEnv,
    RuntimeFactory,
    _close,
    _lifecycle_diff,
)
from .seat import _SHARED_SERVER_ENV
from .state import BeadState

__all__ = ["LOOP_CHECKS", "check_loop_drives_one_turn"]

#: The bead metadata key the loop persists for an adoptable runtime's turn
#: (``beadhive.runtime_handle.METADATA_KEY``, repeated so the kit does not import it eagerly).
HANDLE_KEY = "bh.runtime.handle"

#: Between harvest passes. A pass already costs several bd round trips.
_POLL = 0.25


def _probe_config(schema_version: int) -> str:
    return (
        f"schema_version: {schema_version}\n"
        "providers: [github]\n"
        "managed_repos: []\n"
        "work:\n"
        "  beads:\n"
        "    route: cli\n"
    )


@contextlib.contextmanager
def _loop_environment(root: Path) -> Iterator[None]:
    """Point BeadHive's config at a throwaway file whose beads route is ``cli`` (the scratch
    store has no Beads service) and unset the shared-server variables, which would point the
    loop's ``bd`` calls at a server that is not the scratch store's. Restored on exit."""
    from ...modules.config.contracts import SCHEMA_VERSION

    root.mkdir(parents=True, exist_ok=True)
    config_file = root / "config.yaml"
    config_file.write_text(_probe_config(SCHEMA_VERSION), encoding="utf-8")
    names = ("BH_HOME", "BH_CONFIG", *sorted(_SHARED_SERVER_ENV))
    saved = {name: os.environ.get(name) for name in names}
    os.environ["BH_HOME"] = str(root)
    os.environ["BH_CONFIG"] = str(config_file)
    for name in _SHARED_SERVER_ENV:
        os.environ.pop(name, None)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def _bh_side(detail: str) -> ConformanceFailure:
    return ConformanceFailure("bh-loop", detail)


def _created_id(out: str) -> str:
    data = json.loads(out)
    data = data[0] if isinstance(data, list) else data
    return str(data["id"])


def _seed(hive: BdHive) -> tuple[str, str]:
    """One in-progress epic with one ready child task: what the loop's dispatch row needs."""
    try:
        epic = _created_id(hive.bd("create", "loop probe epic", "-t", "epic", "--json"))
        child = _created_id(
            hive.bd("create", "loop probe turn", "-t", "task", "--parent", epic, "--json")
        )
        hive.bd("update", epic, "--status", "in_progress")
    except Exception as exc:  # noqa: BLE001 - the probe's own setup is BH's side
        raise _bh_side(f"could not seed the probe molecule in the scratch store: {exc}") from exc
    return epic, child


def _raised_in(runtime: object, exc: BaseException) -> bool:
    """Was *exc* raised while one of *runtime*'s own methods was executing?"""
    tb = exc.__traceback__
    while tb is not None:
        if tb.tb_frame.f_locals.get("self") is runtime:
            return True
        tb = tb.tb_next
    return False


async def _pass(loop, runtime):
    try:
        return await loop.run_pass()
    except ConformanceFailure:
        raise
    except Exception as exc:
        if _raised_in(runtime, exc):
            raise  # the runner reports a runtime crash as GC-fixable
        raise _bh_side(f"LocalLoop.run_pass raised {type(exc).__name__}: {exc}") from exc


def _assert_no_runtime_writes(
    before: BeadState, after: BeadState, session_id: str, *, adoptable: bool, stage: str
) -> None:
    loop_keys = frozenset({HANDLE_KEY}) if adoptable else frozenset()
    changes = _lifecycle_diff(before, after, {session_id}, loop_keys=loop_keys)
    if changes:
        raise ConformanceFailure("no-lifecycle-writes", f"{stage}: " + "; ".join(changes))


async def _drive(env: RuntimeEnv, hive: BdHive, runtime, epic: str, bead: str) -> None:
    from ... import localloop
    from .. import is_adoptable

    adoptable = is_adoptable(runtime)
    claimed: dict[str, BeadState] = {}

    def claim() -> localloop.ClaimResult:
        if claimed:
            return localloop.ClaimResult(reason="empty_queue")
        hive.bd("update", bead, "--claim")
        # The "before" state is taken AFTER the claim: the claim is the loop's write.
        claimed["before"] = hive.snapshot(bead)
        return localloop.ClaimResult(claimed=bead, worktree=str(env.workspace), actor=ACTOR)

    loop = localloop.LocalLoop(
        hive_dir=hive.store,
        epic=epic,
        actor=ACTOR,
        caps=localloop.Caps(max_concurrency=1),
        runtime=runtime,
        on_gate_resolved=runtime.on_gate_resolved,
        claim=claim,
        workspace_for=lambda _bead: str(env.workspace),
        routing=lambda _bead, _role: None,
        instructions=lambda _action, _bead, _role: str(env.instructions),
    )
    try:
        await _turn(env, hive, loop, runtime, bead, claimed, adoptable=adoptable)
    finally:
        await loop.shutdown()


async def _turn(env, hive, loop, runtime, bead, claimed, *, adoptable: bool) -> None:
    # 1. dispatch: the loop claims the ready child and schedules it, once.
    first = await _pass(loop, runtime)
    if bead not in first.dispatched:
        refused = [cause for b, cause in first.causes if b == bead]
        if refused:
            raise ConformanceFailure(
                "loop-session-started",
                f"schedule() refused the loop's turn for {bead}; the loop recorded cause "
                f"{refused[0]!r}",
            )
        row = first.decision.row if first.decision else None
        raise _bh_side(f"the loop did not schedule the ready bead {bead} (decision row {row!r})")
    if bead not in loop.in_flight or "before" not in claimed:
        raise _bh_side(f"the loop reported {bead} dispatched but does not hold it in flight")
    session_id = loop.in_flight[bead].handle.session_id
    before = claimed["before"]

    # 2. in flight: the turn is held, so observe() must answer running.
    second = await _pass(loop, runtime)
    harvested = [label for b, label in second.harvested if b == bead]
    if harvested:
        raise ConformanceFailure(
            "observe-transitions",
            f"the loop harvested {bead} as {harvested[0]!r} before the turn was released: "
            "observe() answered terminal while the seat was still holding its turn",
        )
    mid = hive.snapshot(bead)
    _assert_no_runtime_writes(before, mid, session_id, adoptable=adoptable, stage="in flight")
    if bead in second.reclaimed or bead not in second.heartbeats:
        raise _bh_side(
            f"{bead} was in flight but the loop's pass did not keep its claim "
            f"(heartbeats {list(second.heartbeats)}, reclaimed {list(second.reclaimed)})"
        )
    if adoptable and HANDLE_KEY not in mid.metadata:
        raise _bh_side(f"the loop did not persist {HANDLE_KEY} for the adoptable runtime's turn")

    # 3. harvest: release the turn; the loop must harvest done and write no cause.
    hive.release_turn(bead, session_id)
    deadline = env.clock() + env.timeout
    final = None
    while final is None:
        report = await _pass(loop, runtime)
        labels = [label for b, label in report.harvested if b == bead]
        if labels:
            final = (labels[0], report)
            break
        if env.clock() > deadline:
            break
        env.sleep(_POLL)
    starts = hive.seat_starts(bead)
    if final is None:
        if not starts:
            raise ConformanceFailure(
                "loop-session-started",
                f"the loop scheduled session {session_id!r} for {bead}, and schedule() returned "
                "a handle, but no seat session ever started",
            )
        raise ConformanceFailure(
            "observe-transitions",
            f"observe() still answered running {env.timeout:.0f}s after the loop's turn was "
            "released",
        )
    if starts != [session_id]:
        raise ConformanceFailure(
            "loop-session-started",
            f"the seat sessions started for {bead} were {starts}, expected exactly the loop's "
            f"session {session_id!r}",
        )
    label, report = final
    if label != "done":
        raise ConformanceFailure(
            "observe-transitions",
            f"the stand-in seat reported done but the loop harvested {label!r}",
        )
    if any(b == bead for b, _cause in report.causes):
        raise _bh_side(f"the loop wrote a cause for {bead}'s done turn: {list(report.causes)}")
    after = hive.snapshot(bead)
    _assert_no_runtime_writes(before, after, session_id, adoptable=adoptable, stage="harvested")
    if HANDLE_KEY in after.metadata:
        raise _bh_side(f"the loop left {HANDLE_KEY} on {bead} after harvesting its turn")


def check_loop_drives_one_turn(env: RuntimeEnv, factory: RuntimeFactory) -> None:
    """BeadHive's real loop drives one turn through the runtime: one schedule() that starts
    the seat under the loop's session id, running while held (claim kept), done harvested with
    no cause, and no lifecycle write the loop did not make itself."""
    hive = env.hive
    if not isinstance(hive, BdHive):
        raise _bh_side(
            f"the loop probe needs a BdHive (a real scratch bd store); got a {env.hive_kind!r} "
            "hive. Put bd on PATH, or pass a BdHive"
        )
    with _loop_environment(hive.root / "loop-probe"):
        epic, bead = _seed(hive)
        runtime = factory(env)
        try:
            asyncio.run(_drive(env, hive, runtime, epic, bead))
        finally:
            _close(runtime)  # idempotent: the loop's shutdown closed it already


LOOP_CHECKS: dict[str, Callable[[RuntimeEnv, RuntimeFactory], None]] = {
    "loop_drives_one_turn": check_loop_drives_one_turn,
}
