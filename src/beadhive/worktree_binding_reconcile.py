"""Presentation-binding teardown, status, and repair for managed worktrees (bh-cb4jo.2).

The native ``worktree.manager`` owns worktree mechanics; a presenter (today only Herdr) is a
``workspace.binding`` recorded per worktree in :mod:`beadhive.worktree_bindings`. This module is
the root composition for everything that has to reconcile the two:

* **Teardown** — :func:`removal_bindings` composes the binding for every presenter a worktree has
  recorded, so ``WorktreeLifecycleService.remove`` releases it (``herdr workspace close``) before
  the native remove (E30). Every Beadhive removal path (``bh worktree rm``, SAFE prune, and the
  merge/abandon/retire teardowns that call them) goes through that one service call.
* **Status** — :func:`binding_states` verifies each recorded binding against the presenter's own
  inventory (``herdr worktree list`` → ``open_workspace_id``, E35) and names the gap: a
  present-but-unbound worktree (bind intent recorded, never bound — a crash between create and
  bind, E34, or Herdr down at bind time, E33), a bound-but-missing one (the recorded workspace is
  gone), a stale reference, or a live binding whose reference was never recorded.
* **Repair** — :func:`rebind` re-binds through the one reconciliation primitive
  (``herdr worktree open --path``, idempotent) and closes Beadhive-labelled orphaned
  ``"<label> (deleted)"`` workspaces with ``workspace close`` (E31) — never a forced native
  remove. ``bh plugin herdr launch`` runs the same re-bind automatically.

Only worktrees with a recorded binding are ever checked against Herdr, so hives that never use
a presenter pay nothing and never touch a Herdr process.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import run as _run
from . import worktree_bindings
from .integrations.herdr.transport import invoke_command
from .integrations.herdr.workspace_binding import PRESENTER as HERDR
from .integrations.herdr.workspace_binding import HerdrWorkspaceBinding
from .modules.worktrees import WorkspaceBindingError, WorkspaceBindingPort, WorktreeHandle

DEFAULT_SESSION = "default"
_HERDR_TIMEOUT_SECONDS = 30.0

#: Binding states that are a gap an operator (or the next launch) should repair.
GAP_STATES = frozenset({"unbound", "missing", "stale", "unrecorded"})


def herdr_binding(session: str, *, label: str = "") -> HerdrWorkspaceBinding:
    """The Herdr binding for one exact session (monkeypatch seam for tests)."""

    def run(*args: str) -> Any:
        return invoke_command(
            ["herdr", "--session", session, *args],
            runner=_run.run,
            timeout=_HERDR_TIMEOUT_SECONDS,
        )

    return HerdrWorkspaceBinding(run, label=label)


def removal_bindings(
    records: Mapping[str, worktree_bindings.BindingRecord],
) -> tuple[WorkspaceBindingPort, ...]:
    """The bindings a removal must release first: one per recorded presenter."""
    bindings: list[WorkspaceBindingPort] = []
    if (record := records.get(HERDR)) is not None:
        bindings.append(herdr_binding(record.session or DEFAULT_SESSION))
    return tuple(bindings)


@dataclass(frozen=True, slots=True)
class BindingState:
    """One presenter binding of one worktree, verified against the presenter's inventory.

    ``state`` is ``bound`` (no gap), ``unbound`` (present but never bound), ``missing`` (the
    recorded workspace is gone), ``stale`` (the presenter binds a different workspace),
    ``unrecorded`` (bound, but the reference was never recorded), or ``unverified`` (the
    presenter could not be reached; ``detail`` says why).
    """

    presenter: str
    session: str
    reference: str
    live: str
    state: str
    detail: str = ""

    @property
    def gap(self) -> bool:
        return self.state in GAP_STATES

    def as_dict(self) -> dict[str, str]:
        return {
            "session": self.session,
            "reference": self.reference,
            "live": self.live,
            "state": self.state,
            "detail": self.detail,
        }


def _classify(record: worktree_bindings.BindingRecord, live: str) -> str:
    if not record.reference:
        return "unrecorded" if live else "unbound"
    if not live:
        return "missing"
    return "bound" if live == record.reference else "stale"


def binding_states(
    path: str | Path,
    records: Mapping[str, worktree_bindings.BindingRecord] | None = None,
) -> tuple[BindingState, ...]:
    """Verify every recorded binding of the worktree at ``path`` (no record → no Herdr call)."""
    records = worktree_bindings.STORE.read(path) if records is None else records
    states: list[BindingState] = []
    for presenter, record in sorted(records.items()):
        session = record.session or DEFAULT_SESSION
        if presenter != HERDR:
            states.append(
                BindingState(presenter, session, record.reference, "", "unverified", "unknown")
            )
            continue
        try:
            live = herdr_binding(session).derive(path) or ""
        except WorkspaceBindingError as exc:
            states.append(
                BindingState(presenter, session, record.reference, "", "unverified", exc.detail)
            )
            continue
        states.append(
            BindingState(presenter, session, record.reference, live, _classify(record, live))
        )
    return tuple(states)


@dataclass
class RebindReport:
    """What one ``rebind`` pass did."""

    rebound: list[dict[str, str]] = field(default_factory=list)
    closed: list[dict[str, str]] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed

    def as_dict(self) -> dict[str, object]:
        return {
            "op": "rebind",
            "rebound": self.rebound,
            "closed": self.closed,
            "failed": self.failed,
            "ok": self.ok,
        }


@dataclass(frozen=True, slots=True)
class RebindTarget:
    """One worktree to (re-)bind: its exact path, main checkout, and ``bh:<hive>`` label."""

    path: Path
    main: Path
    hive: str


def rebind(
    targets: Iterable[RebindTarget],
    *,
    session: str = "",
    explicit: bool = False,
    binding_for: Callable[..., HerdrWorkspaceBinding] | None = None,
) -> RebindReport:
    """Repair binding gaps with ``open --path`` and close orphaned ``(deleted)`` workspaces.

    Without ``explicit`` only worktrees that already carry a recorded Herdr binding are
    (re-)bound — the repair never binds a worktree nobody asked to present. ``explicit`` (one
    named worktree) binds it even without a record. Every re-bind records the intent first and
    the reference after, exactly like launch. Orphans are closed per session touched.
    """
    binding_for = binding_for or herdr_binding
    store = worktree_bindings.STORE
    report = RebindReport()
    sessions: set[str] = {session} if session else set()
    for target in targets:
        record = store.read(target.path).get(HERDR)
        if record is None and not explicit:
            continue
        chosen = session or (record.session if record else "") or DEFAULT_SESSION
        sessions.add(chosen)
        binding = binding_for(chosen, label=f"bh:{target.hive}")
        store.record_intent(target.path, HERDR, chosen)
        try:
            opened = binding.open(target.path, source=target.main)
        except WorkspaceBindingError as exc:
            report.failed.append({"path": str(target.path), "session": chosen, "error": exc.detail})
            continue
        store.record(target.path, HERDR, chosen, opened.workspace_id)
        previous = record.reference if record else ""
        report.rebound.append(
            {
                "path": str(target.path),
                "session": chosen,
                "workspace": opened.workspace_id,
                "previous": previous,
                "already_open": "true" if opened.already_open else "false",
            }
        )
    for chosen in sorted(sessions or {DEFAULT_SESSION}):
        binding = binding_for(chosen)
        try:
            orphans = binding.orphans()
            for orphan in orphans:
                if binding.close(orphan.workspace_id):
                    report.closed.append(
                        {
                            "session": chosen,
                            "workspace": orphan.workspace_id,
                            "label": orphan.label,
                            "path": orphan.checkout_path,
                        }
                    )
        except WorkspaceBindingError as exc:
            report.failed.append({"session": chosen, "error": exc.detail})
    return report


def recorded_handle(main: Path, path: Path, branch: str = "") -> tuple[WorktreeHandle, tuple]:
    """A removal handle carrying the recorded references, plus the bindings to release."""
    records = worktree_bindings.STORE.read(path)
    handle = worktree_bindings.STORE.handle(main, path, branch, records=records)
    return handle, removal_bindings(records)


__all__ = [
    "DEFAULT_SESSION",
    "GAP_STATES",
    "BindingState",
    "RebindReport",
    "RebindTarget",
    "binding_states",
    "herdr_binding",
    "rebind",
    "recorded_handle",
    "removal_bindings",
]
