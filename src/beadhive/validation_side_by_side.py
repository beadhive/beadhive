"""Run one gate's pending attest keys side by side through the local broker engine (bh-qhrl6).

:func:`run_group` submits every key as ONE gate group (``JobSpec.group``) and runs each granted
key on its own thread, all in one shared checkout. The checkout is created and provisioned once,
when the first grant arrives (its root is the one the engine pinned the gate to), by the caller's
:data:`WorkspaceFactory`: today's clean-checkout location without execution roots, a
``run_directory`` in the granted root with them. Its setup (the verify init rules plus
``work.validation_broker.setup``) is the gate's one per-tree setup job; it runs under that first
grant, before any key starts.

**Failure policy: the group finishes.** A red key does not cancel its siblings: every started
key runs to completion and records its own verdict, and the caller applies its existing
aggregate (and with it the rollback / bounce rules of the boundary) once, after the group. The
keys run concurrently, so finishing costs at most the longest key already running, and each
recorded verdict is proof a later boundary reuses or carries instead of re-running. The existing
opt-in ``BH_VALIDATION_FAIL_FAST`` keeps its meaning: after the first blocking result no further
key *starts* (queued keys are cancelled and reported not run); keys already running still finish,
so no verdict is left half-written.

**Engine failure falls back, loudly.** A key the local engine cannot run (refused at submit, an
engine error, a lost lease, a setup or root mismatch) is returned in
:attr:`GroupResult.fallback`; the caller runs those keys afterwards on today's sequential slots
path. After the first engine error no further key starts here.

Everything that touches the world is a port: the broker, the workspace factory and its
``execute``, the clock. Threads copy the caller's context (``contextvars``), so a scoped config
(``identity.config_scope``) reaches every key.
"""

from __future__ import annotations

import contextlib
import contextvars
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Protocol

from beadhive_validation_broker import (
    BrokerPort,
    Clock,
    Grant,
    GrantRefused,
    JobHandle,
    JobSpec,
    LeaseLost,
    Outcome,
    Owner,
    PriorityClass,
    Reservation,
    Usage,
)

__all__ = [
    "GroupKey",
    "GroupResult",
    "KeyOutcome",
    "KeyWorkspace",
    "WorkspaceFactory",
    "group_priority",
    "run_group",
]

#: Lifecycle phases a developer is waiting on; everything else is a land.
_CHECK_PHASES = frozenset({"check", "submit", "review", "validation"})


def group_priority(phase: str | None) -> PriorityClass:
    """The admission class of a gate at ``phase``: check/submit before merge/land."""
    if (phase or "") in _CHECK_PHASES:
        return PriorityClass.CHECK_SUBMIT
    return PriorityClass.MERGE_LAND


@dataclass(frozen=True)
class GroupKey:
    """One attest key of the gate: what to run and what it reserves."""

    name: str
    command: str
    reservation: Reservation
    expected_seconds: float
    #: Whether an UNKNOWN (75) result blocks: only a required key's does.
    required: bool = True

    def blocks(self, rc: int) -> bool:
        return rc != 0 and not (rc == 75 and not self.required)


@dataclass(frozen=True)
class KeyOutcome:
    """A key that ran (``rc``) or did not start under fail-fast (``rc is None``)."""

    name: str
    rc: int | None
    wall_s: float = 0.0
    root: str | None = None


@dataclass
class GroupResult:
    outcomes: dict[str, KeyOutcome] = field(default_factory=dict)
    #: Keys for today's sequential slots path, with why (in submission order).
    fallback: dict[str, str] = field(default_factory=dict)
    wall_s: float = 0.0
    stopped_after: str = ""


class KeyWorkspace(Protocol):
    """The shared checkout one gate's keys run in."""

    #: The execution root it lives in (``Grant.root``), or ``None`` for today's location.
    root: str | None

    def execute(self, key: GroupKey, grant: Grant) -> int:
        """Run ``key`` in the shared checkout with ``grant``'s environment; its exit code."""
        ...


#: Creates, provisions (the per-tree setup job) and finally removes the shared checkout for the
#: first grant. Raising means the gate cannot run here: its keys fall back.
WorkspaceFactory = Callable[[Grant], AbstractContextManager[KeyWorkspace]]


class _Shared:
    """The lazily created workspace and the group's stop/trip state, under one lock."""

    def __init__(self, factory: WorkspaceFactory) -> None:
        self.lock = threading.Lock()
        self.factory = factory
        self.stack = contextlib.ExitStack()
        self.workspace: KeyWorkspace | None = None
        self.workspace_error: str = ""
        self.stopped_after = ""
        self.tripped = ""
        self.started: set[str] = set()

    def refusal(self) -> str:
        """Why no further key may start (the group stopped or tripped), or ''."""
        with self.lock:
            if self.stopped_after:
                return f"fail-fast after {self.stopped_after}"
            return self.tripped

    def start(self, name: str) -> str:
        """Mark ``name`` started unless the group stopped or tripped; returns why not, or ''."""
        with self.lock:
            why_not = (
                f"fail-fast after {self.stopped_after}" if self.stopped_after else self.tripped
            )
            if not why_not:
                self.started.add(name)
            return why_not

    def ensure(self, grant: Grant) -> KeyWorkspace | None:
        with self.lock:
            if self.workspace is None and not self.workspace_error:
                try:
                    self.workspace = self.stack.enter_context(self.factory(grant))
                except Exception as exc:  # noqa: BLE001 - no shared checkout: every key falls back
                    self.workspace_error = f"shared checkout setup failed: {exc}"[:300]
            return self.workspace


def _spec(key: GroupKey, group: str, priority: PriorityClass, owner: Owner) -> JobSpec:
    return JobSpec(
        job_id=f"{group}/{key.name}",
        command=(key.command,),
        reservation=key.reservation,
        priority=priority,
        expected_duration_s=key.expected_seconds,
        group=group,
        owner=owner,
        key=key.name,
    )


def _quietly(action: Callable[[], object]) -> None:
    try:
        action()
    except Exception:  # noqa: BLE001 - report/cancel are accounting; the verdict stands
        pass


def run_group(
    broker: BrokerPort,
    keys: Sequence[GroupKey],
    *,
    group: str,
    priority: PriorityClass,
    owner: Owner,
    workspace: WorkspaceFactory,
    report: Callable[[KeyOutcome], None],
    fail_fast: bool = False,
    clock: Clock = time.monotonic,
    warn: Callable[[str], None] = lambda _message: None,
) -> GroupResult:
    """Run ``keys`` concurrently as gate ``group``; see the module docstring for the policy.

    ``report`` is called once per key that ran or was stopped, as it finishes (from the key's
    thread). Returns when every key has finished and the shared checkout is removed.
    """
    started = clock()
    result = GroupResult()
    shared = _Shared(workspace)
    handles: dict[str, JobHandle] = {}
    errors: list[BaseException] = []
    outcome_lock = threading.Lock()

    def cancel_unstarted(reason: str) -> None:
        with shared.lock:
            waiting = [h for name, h in handles.items() if name not in shared.started]
        for handle in waiting:  # never a running key's lease: it keeps its grant to the end
            _quietly(lambda h=handle: broker.cancel(h, reason))

    def trip(reason: str) -> None:
        with shared.lock:
            first = not shared.tripped
            shared.tripped = shared.tripped or reason
        if first:
            warn(
                f"⚠ validation broker: {reason}; keys not yet started fall back to the "
                "sequential slots path"
            )
            cancel_unstarted(reason)

    def fall_back(name: str, reason: str) -> None:
        with outcome_lock:
            result.fallback[name] = reason

    def finish(outcome: KeyOutcome) -> None:
        with outcome_lock:
            result.outcomes[outcome.name] = outcome
        report(outcome)

    def stop_after(name: str) -> None:
        with shared.lock:
            if shared.stopped_after:
                return
            shared.stopped_after = name
        cancel_unstarted(f"fail-fast after {name}")

    def blocked() -> str:
        with shared.lock:
            return shared.stopped_after

    for key in keys:
        if shared.tripped:
            fall_back(key.name, shared.tripped)
            continue
        try:
            handles[key.name] = broker.submit(_spec(key, group, priority, owner))
        except GrantRefused as exc:
            fall_back(key.name, f"refused by the local engine: {exc}")
        except Exception as exc:  # noqa: BLE001 - an engine error: nothing more starts here
            trip(f"the local engine failed ({type(exc).__name__}: {exc})")
            fall_back(key.name, shared.tripped)

    def work(key: GroupKey, handle: JobHandle) -> None:
        try:
            grant = broker.wait_grant(handle)
        except LeaseLost as exc:
            if blocked():
                finish(KeyOutcome(key.name, None))
            else:
                fall_back(key.name, shared.tripped or f"lease lost: {exc}")
            return
        except Exception as exc:  # noqa: BLE001 - an engine error: this key falls back
            trip(f"the local engine failed ({type(exc).__name__}: {exc})")
            fall_back(key.name, shared.tripped)
            return
        space = None
        why_not = shared.refusal()
        if not why_not:
            space = shared.ensure(grant)
            if space is None:
                trip(shared.workspace_error)
            why_not = shared.start(key.name)
        if not why_not and space is not None and space.root != grant.root:
            why_not = f"granted root {grant.root} is not the shared checkout's root {space.root}"
        if why_not:
            _quietly(lambda: broker.cancel(handle, why_not))
            if blocked():
                finish(KeyOutcome(key.name, None))
            else:
                fall_back(key.name, why_not)
            return
        assert space is not None
        began = clock()
        rc, completed = 1, False
        try:
            rc = space.execute(key, grant)
            completed = True
        finally:
            wall = clock() - began
            if completed and fail_fast and key.blocks(rc):
                stop_after(key.name)  # before the lease is released, so no queued key slips in
            _quietly(lambda: broker.report(handle, Outcome(rc), Usage(wall_s=wall)))
        finish(KeyOutcome(key.name, rc, wall, grant.root))

    threads = []
    with shared.stack:
        for key in keys:
            handle = handles.get(key.name)
            if handle is None:
                continue
            context = contextvars.copy_context()
            thread = threading.Thread(
                target=_run_recording,
                args=(context, errors, work, key, handle),
                name=f"attest-{key.name}",
                daemon=True,
            )
            threads.append(thread)
            thread.start()
        for thread in threads:
            thread.join()
    if errors:
        raise errors[0]
    result.wall_s = clock() - started
    result.stopped_after = shared.stopped_after
    order = {key.name: index for index, key in enumerate(keys)}
    result.fallback = dict(sorted(result.fallback.items(), key=lambda item: order[item[0]]))
    return result


def _run_recording(
    context: contextvars.Context, errors: list[BaseException], work, key, handle
) -> None:
    """Run ``work`` in ``context``; an exception is kept for the caller's thread to re-raise."""
    try:
        context.run(work, key, handle)
    except BaseException as exc:  # noqa: BLE001 - re-raised by run_group once every key joined
        errors.append(exc)


def summary_lines(result: GroupResult, engine: str = "local") -> Iterator[str]:
    """The group's wall-time report: per-key walls (slowest first) and the group wall."""
    ran = [o for o in result.outcomes.values() if o.rc is not None]
    total = sum(o.wall_s for o in ran)
    yield (
        f"  side-by-side group ({engine} engine): {len(ran)} key(s), group wall "
        f"{result.wall_s:.3f}s, sum of key walls {total:.3f}s"
    )
    for outcome in sorted(ran, key=lambda o: -o.wall_s):
        where = f" on {outcome.root}" if outcome.root else ""
        yield f"    {outcome.name}: {outcome.wall_s:.3f}s{where}"
    for name, reason in result.fallback.items():
        yield f"    {name}: not run side by side ({reason}) — sequential slots path"
