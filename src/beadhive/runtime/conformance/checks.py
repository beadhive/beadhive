"""The conformance checks: one function per ADR invariant, each raising
:class:`ConformanceFailure` that names the invariant and its fix class.

Every check takes a :class:`RuntimeEnv`, builds its own runtime from the caller's factory and
uses a fresh bead and a fresh session id per attempt (session ids are write-once).
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...gate_resolution import GateResolution
from .. import RoleHandle, RoleOutcome, Runtime
from .hive import OUTCOME_PREFIX, DisposableHive
from .state import BeadState

GC_FIXABLE = "gc-fixable"
STRUCTURAL = "structural"

TERMINAL = frozenset({"done", "blocked", "handoff", "failed"})
ROLE = "developer"


@dataclass(frozen=True)
class Invariant:
    """One rule of gas-city-runtime-seam-adr.md that a runtime must keep."""

    id: str
    adr: str
    rule: str
    fix_class: str  # GC_FIXABLE: fix the runtime and re-run; STRUCTURAL: Mode 2 (ADR Decision 6)


INVARIANTS: dict[str, Invariant] = {
    inv.id: inv
    for inv in (
        Invariant(
            "handle-shape",
            "Decision 4: shape of schedule()",
            "schedule() returns a RoleHandle for the bead it was given",
            GC_FIXABLE,
        ),
        Invariant(
            "observe-transitions",
            "Decision 2 and 4: observe() transitions",
            "observe() answers running while the session is live, then a terminal outcome "
            "(done, blocked, handoff or failed), and never a bare exit 0 as success",
            GC_FIXABLE,
        ),
        Invariant(
            "sink-round-trip",
            "Decision 2: outcome sink round trip",
            "a SeatRun written through the outcome sink reads back, for its own session, as the "
            "classification the same line has on stdout",
            GC_FIXABLE,
        ),
        Invariant(
            "stale-session-rejected",
            "Decision 2: a stale session cannot answer for a new one",
            "a record for another session_id never answers observe() for this one "
            "(the outcome sink is GC-fixable per ADR Decision 6)",
            GC_FIXABLE,
        ),
        Invariant(
            "gate-idempotent",
            "Decision 3: at-least-once delivery",
            "on_gate_resolved is idempotent, and never re-dispatches (re-dispatch is BH-owned)",
            GC_FIXABLE,
        ),
        Invariant(
            "same-session-id",
            "Decision 4: restart keeps the session_id",
            "a session restarted by the runtime answers observe() for the SAME session_id",
            STRUCTURAL,
        ),
        Invariant(
            "no-lifecycle-writes",
            "Decision 4: process liveness only",
            "the runtime never claims, assigns, changes status, writes a label, writes metadata "
            "other than the outcome sink's, or resolves a gate",
            STRUCTURAL,
        ),
    )
}


class ConformanceFailure(AssertionError):
    """A violated invariant. The message names it, its ADR location and fix class."""

    def __init__(self, invariant_id: str, detail: str):
        self.invariant = INVARIANTS[invariant_id]
        self.detail = detail
        inv = self.invariant
        verdict = (
            "GC-fixable: fix the runtime and re-run the kit"
            if inv.fix_class == GC_FIXABLE
            else "structural: Mode 2 (delegated molecule) per ADR Decision 6"
        )
        super().__init__(
            f"[{inv.id}] violates {inv.adr}. Rule: {inv.rule}. {verdict}. Detail: {detail}"
        )


@dataclass(frozen=True)
class RuntimeEnv:
    """What a runtime factory is given: the disposable hive, the seat command to launch for
    ``schedule()`` (a ``work.dispatch.seat_command`` template) and a workspace plus instructions
    file to pass to it."""

    hive: DisposableHive
    seat_command: str
    workspace: Path
    instructions: Path
    timeout: float = 60.0
    #: Injectable time source and sleeper for the kit's polling loops (tests pass fakes).
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep

    @property
    def hive_kind(self) -> str:
        """``"bd"`` (a real beads store), ``"file"`` (JSON stand-in) or ``"custom"``."""
        return getattr(self.hive, "kind", "custom")


RuntimeFactory = Callable[[RuntimeEnv], Runtime]


def new_session_id() -> str:
    return f"conformance-{uuid.uuid4().hex[:12]}"


def _close(runtime: object) -> None:
    close = getattr(runtime, "close", None)
    if callable(close):
        close()


def _schedule(runtime: Runtime, env: RuntimeEnv, bead: str, session_id: str) -> RoleHandle:
    return runtime.schedule(
        bead,
        ROLE,
        workspace=str(env.workspace),
        instructions=str(env.instructions),
        session_id=session_id,
    )


def _await_terminal(runtime: Runtime, handle: RoleHandle, env: RuntimeEnv) -> RoleOutcome:
    deadline = env.clock() + env.timeout
    while True:
        seen = runtime.observe(handle)
        if seen.status != "running":
            return seen
        if env.clock() > deadline:
            raise ConformanceFailure(
                "observe-transitions",
                f"observe() still answered running {env.timeout:.0f}s after the turn was released",
            )
        env.sleep(0.05)


def _check_handle(handle: object, bead: str, session_id: str) -> None:
    if not isinstance(handle, RoleHandle):
        raise ConformanceFailure(
            "handle-shape", f"schedule() returned {type(handle).__name__}, not a RoleHandle"
        )
    if handle.bead_id != bead:
        raise ConformanceFailure(
            "handle-shape", f"handle.bead_id is {handle.bead_id!r}, expected {bead!r}"
        )
    if handle.session_id != session_id:
        raise ConformanceFailure(
            "same-session-id",
            f"schedule() was given session_id {session_id!r} but the handle carries "
            f"{handle.session_id!r}",
        )


def _lifecycle_diff(before: BeadState, after: BeadState, sessions: set[str]) -> list[str]:
    """What changed on the bead other than the outcome sink's own key for a known session."""
    allowed = {OUTCOME_PREFIX + s for s in sessions}
    changes: list[str] = []
    for name in ("status", "assignee"):
        if getattr(before, name) != getattr(after, name):
            changes.append(f"{name} {getattr(before, name)!r} -> {getattr(after, name)!r}")
    if before.gates != after.gates:
        changes.append(f"gates {before.gates} -> {after.gates}")
    if before.labels != after.labels:
        added, removed = sorted(after.labels - before.labels), sorted(before.labels - after.labels)
        changes.append(f"labels added {added} removed {removed}")
    for key in sorted(set(before.metadata) | set(after.metadata)):
        if key in allowed:
            continue
        if before.metadata.get(key) != after.metadata.get(key):
            changes.append(f"metadata key {key!r} changed")
    return changes


def _assert_no_lifecycle_writes(
    env: RuntimeEnv, bead: str, before: BeadState, sessions: set[str]
) -> None:
    changes = _lifecycle_diff(before, env.hive.snapshot(bead), sessions)
    if changes:
        raise ConformanceFailure("no-lifecycle-writes", "; ".join(changes))


def _full_turn(runtime: Runtime, env: RuntimeEnv, bead: str, session_id: str) -> RoleOutcome:
    handle = _schedule(runtime, env, bead, session_id)
    _check_handle(handle, bead, session_id)
    first = runtime.observe(handle)
    if not isinstance(first, RoleOutcome) or first.status != "running":
        raise ConformanceFailure(
            "observe-transitions",
            f"observe() before the turn finished answered {first!r}, expected status running",
        )
    env.hive.release_turn(bead, session_id)
    final = _await_terminal(runtime, handle, env)
    if final.status not in TERMINAL:
        raise ConformanceFailure(
            "observe-transitions",
            f"terminal status {final.status!r} is not one of {sorted(TERMINAL)}",
        )
    if final.status != "done":
        raise ConformanceFailure(
            "observe-transitions",
            f"the stand-in seat reported done but observe() answered {final.status!r} "
            f"({final.summary!r})",
        )
    return final


# ---- the checks ------------------------------------------------------------------------------


def check_schedule_handle_shape(env: RuntimeEnv, factory: RuntimeFactory) -> None:
    runtime = factory(env)
    try:
        if not isinstance(runtime, Runtime):
            raise ConformanceFailure(
                "handle-shape", "the factory did not return an object implementing Runtime"
            )
        if not getattr(runtime, "name", ""):
            raise ConformanceFailure("handle-shape", "the runtime has no name")
        bead, session_id = env.hive.create_bead(), new_session_id()
        handle = _schedule(runtime, env, bead, session_id)
        _check_handle(handle, bead, session_id)
        env.hive.release_turn(bead, session_id)
        _await_terminal(runtime, handle, env)
    finally:
        _close(runtime)


def check_observe_running_to_terminal(env: RuntimeEnv, factory: RuntimeFactory) -> None:
    runtime = factory(env)
    try:
        bead, session_id = env.hive.create_bead(), new_session_id()
        final = _full_turn(runtime, env, bead, session_id)
        again = runtime.observe(RoleHandle(bead_id=bead, session_id=session_id))
        if again.status != final.status:
            raise ConformanceFailure(
                "observe-transitions",
                f"a second observe() of a finished session changed answer: "
                f"{final.status!r} then {again.status!r}",
            )
    finally:
        _close(runtime)


_SINK_LINES = {
    "done": ("done", "shipped"),
    "blocked": ("blocked", "needs a decision"),
    "handoff": ("handoff", "over to review"),
}


def check_outcome_sink_round_trip(env: RuntimeEnv, factory: RuntimeFactory | None = None) -> None:
    """Independent of the runtime: the sink as Gas City uses it (``bh role --outcome-sink
    bead --session-id``) round-trips every seat verdict and an empty run classifies failed."""
    import json

    from ... import outcome

    for expected, summary in _SINK_LINES.values():
        bead, session_id = env.hive.create_bead(), new_session_id()
        line = json.dumps(
            {
                "outcome": {"status": expected, "summary": summary, "bead_id": bead},
                "session_id": session_id,
                "cost_usd": 0.0,
                "usage": {},
                "packs": [],
            }
        )
        if not env.hive.record_outcome(bead, session_id, line):
            raise ConformanceFailure("sink-round-trip", "a fresh session's record was refused")
        found = outcome.from_metadata(env.hive.snapshot(bead).metadata, session_id, bead_id=bead)
        if found is None or found.status != expected or found.summary != summary:
            raise ConformanceFailure(
                "sink-round-trip", f"wrote a {expected!r} SeatRun and read back {found!r}"
            )
        if env.hive.record_outcome(bead, session_id, line.replace(summary, "rewritten")):
            raise ConformanceFailure(
                "sink-round-trip", "the sink overwrote an existing record for the same session"
            )
    bead, session_id = env.hive.create_bead(), new_session_id()
    env.hive.record_outcome(bead, session_id, "")
    found = outcome.from_metadata(env.hive.snapshot(bead).metadata, session_id, bead_id=bead)
    if found is None or found.status != "failed":
        raise ConformanceFailure(
            "sink-round-trip", f"an empty run (a bare exit 0) classified as {found!r}, not failed"
        )


def check_stale_session_rejected(env: RuntimeEnv, factory: RuntimeFactory) -> None:
    """Runtime-only: a finished attempt on a bead must not answer for the next attempt's
    (fresh) session. BH's own sink reader is tested in BH's tests, not here."""
    old, new = new_session_id(), new_session_id()
    runtime = factory(env)
    try:
        bead = env.hive.create_bead()
        _full_turn(runtime, env, bead, old)
        # Attempt two on the same bead, with a fresh session, held open.
        handle = _schedule(runtime, env, bead, new)
        _check_handle(handle, bead, new)
        seen = runtime.observe(handle)
        if seen.status != "running":
            raise ConformanceFailure(
                "stale-session-rejected",
                f"a fresh session {new!r} on a bead whose earlier session {old!r} finished "
                f"answered {seen.status!r} instead of running",
            )
        env.hive.release_turn(bead, new)
        _await_terminal(runtime, handle, env)
    finally:
        _close(runtime)


def check_gate_resolution_idempotent(env: RuntimeEnv, factory: RuntimeFactory) -> None:
    runtime = factory(env)
    try:
        bead, session_id = env.hive.create_bead(), new_session_id()
        _full_turn(runtime, env, bead, session_id)
        before = env.hive.snapshot(bead)
        starts = list(env.hive.seat_starts(bead))
        resolutions = (
            GateResolution(gate_id="gate-1", bead_id=bead, verdict="approved"),
            GateResolution(
                gate_id="gate-2", bead_id=bead, verdict="changes_requested", feedback_ref="gate-2"
            ),
        )
        for resolution in resolutions:
            for _ in range(2):  # delivery is at-least-once
                try:
                    runtime.on_gate_resolved(resolution)
                except Exception as exc:
                    raise ConformanceFailure(
                        "gate-idempotent",
                        f"on_gate_resolved({resolution.verdict}) raised {exc!r} on delivery",
                    ) from exc
        _assert_no_lifecycle_writes(env, bead, before, {session_id})
        if env.hive.seat_starts(bead) != starts:
            raise ConformanceFailure(
                "gate-idempotent",
                f"on_gate_resolved started a seat: starts {starts} -> {env.hive.seat_starts(bead)}",
            )
    finally:
        _close(runtime)


def check_restarted_session_keeps_session_id(env: RuntimeEnv, factory: RuntimeFactory) -> None:
    """The runtime restarts a session for the SAME session_id (what Gas City does to a crashed
    session): the new handle and the observed outcome are still that session's."""
    runtime = factory(env)
    try:
        bead, session_id = env.hive.create_bead(), new_session_id()
        _full_turn(runtime, env, bead, session_id)
        handle = _schedule(runtime, env, bead, session_id)
        _check_handle(handle, bead, session_id)
        again = _await_terminal(runtime, handle, env)
        if again.status not in TERMINAL:
            raise ConformanceFailure(
                "same-session-id",
                f"the restarted session {session_id!r} answered {again.status!r}",
            )
        if not set(env.hive.seat_starts(bead)) <= {session_id}:
            raise ConformanceFailure(
                "same-session-id",
                f"seat sessions started for the bead were {env.hive.seat_starts(bead)}, "
                f"expected only {session_id!r}",
            )
    finally:
        _close(runtime)


def check_no_lifecycle_writes(env: RuntimeEnv, factory: RuntimeFactory) -> None:
    runtime = factory(env)
    try:
        bead, session_id = env.hive.create_bead(), new_session_id()
        before = env.hive.snapshot(bead)
        _full_turn(runtime, env, bead, session_id)
        _assert_no_lifecycle_writes(env, bead, before, {session_id})
    finally:
        _close(runtime)


CHECKS: dict[str, Callable[[RuntimeEnv, RuntimeFactory], None]] = {
    "schedule_handle_shape": check_schedule_handle_shape,
    "observe_running_to_terminal": check_observe_running_to_terminal,
    "outcome_sink_round_trip": check_outcome_sink_round_trip,
    "stale_session_rejected": check_stale_session_rejected,
    "gate_resolution_idempotent": check_gate_resolution_idempotent,
    "restarted_session_keeps_session_id": check_restarted_session_keeps_session_id,
    "no_lifecycle_writes": check_no_lifecycle_writes,
}
