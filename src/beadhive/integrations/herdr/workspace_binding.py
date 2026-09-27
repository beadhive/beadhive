"""Herdr as a ``workspace.binding`` (bh-cb4jo.1, ADR bh-mr9tk.2 Option A).

Native Git stays the one ``worktree.manager``; Herdr only *presents* a worktree the manager
already created or attached. The binding uses exactly two Herdr primitives:

* bind — ``herdr worktree open --path <exact>``: idempotent (a repeated open returns the same
  ``workspace_id`` with ``already_open: true``, E34) and agnostic to whether the worktree was
  created or attached (E28, E29). It is also the one reconciliation primitive for every binding
  gap (a crash between create and bind, Herdr down at bind time — E33, E34, E41).
* release — ``herdr workspace close <id>``: runs before the native remove (E30), and is the only
  repair for an orphaned ``"<label> (deleted)"`` workspace (E31) — never a forced native remove.

The workspace id recorded on ``WorktreeHandle.bindings["herdr"]`` is a cache: it is always
re-derivable from ``herdr worktree list`` → ``open_workspace_id`` (E35). This module never
creates a plain ``workspace create --cwd`` workspace for a managed worktree (E48).

The command runner is injected (``run(*args) -> CompletedProcess | None``, the arguments after
``herdr --session <name>``), so unit tests drive it with a fake Herdr CLI. Like the rest of this
integration it imports no Typer, configuration, or Beadhive lifecycle module.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from beadhive_worktrees import WorkspaceBindingError, WorktreeHandle

PRESENTER = "herdr"
#: Herdr's label suffix for a workspace whose checkout vanished without a release (E31).
DELETED_SUFFIX = " (deleted)"
#: Label prefix every Beadhive-bound workspace carries (``bh:<hive>``).
BEADHIVE_LABEL_PREFIX = "bh:"

HerdrRunner = Callable[..., Any]


@dataclass(frozen=True, slots=True)
class HerdrWorkspaceOpened:
    """What one ``worktree open --path`` returned."""

    workspace_id: str
    root_pane_id: str
    already_open: bool
    path: str


@dataclass(frozen=True, slots=True)
class HerdrWorkspaceRecord:
    """One row of ``herdr workspace list`` narrowed to what reconciliation needs."""

    workspace_id: str
    label: str
    checkout_path: str

    @property
    def orphaned(self) -> bool:
        """E31: Herdr relabels a bound workspace whose checkout disappeared."""
        return self.label.endswith(DELETED_SUFFIX)

    @property
    def beadhive_owned(self) -> bool:
        return self.label.startswith(BEADHIVE_LABEL_PREFIX)


def _same_path(left: str | Path, right: str | Path) -> bool:
    try:
        return Path(left).resolve() == Path(right).resolve()
    except (OSError, RuntimeError, ValueError):
        return str(left) == str(right)


def _decoded(result: Any) -> Any:
    text = str(getattr(result, "stdout", "") or "").strip()
    if not text:
        text = str(getattr(result, "stderr", "") or "").strip()
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _payload(value: Any) -> Any:
    if isinstance(value, dict) and isinstance(value.get("result"), dict):
        return value["result"]
    return value


def _field(record: Any, *keys: str) -> str | None:
    if not isinstance(record, dict):
        return None
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


class HerdrWorkspaceBinding:
    """The Herdr ``workspace.binding`` provider for one Herdr session.

    ``label`` is applied only when ``open --path`` creates the workspace (Beadhive labels its
    workspaces ``bh:<hive>`` so roster ownership stays provable). ``last_opened`` keeps the most
    recent ``open`` response so a launch can split its agent pane from the bound root pane
    without a second round trip.
    """

    presenter = PRESENTER

    def __init__(self, run: HerdrRunner, *, label: str = "") -> None:
        self._run = run
        self._label = label
        self.last_opened: HerdrWorkspaceOpened | None = None

    # ---- the WorkspaceBinding port -------------------------------------------------------

    def bind(self, handle: WorktreeHandle) -> WorktreeHandle:
        """Bind ``handle.path`` with ``worktree open --path``; idempotent (E34)."""
        opened = self.open(handle.path, source=handle.main)
        return handle.with_binding(PRESENTER, opened.workspace_id)

    def release(self, handle: WorktreeHandle) -> None:
        """Close the worktree's workspace before the native remove (E30).

        The recorded reference is re-verified against Herdr's own inventory first: workspace ids
        are reused after a close, so a stale reference must never close another checkout's
        workspace. An empty recorded reference (bind intent recorded, crash before the id was)
        is re-derived from ``worktree list`` (E35). Nothing bound is a no-op.
        """
        for workspace_id in self._release_targets(handle):
            self.close(workspace_id)

    # ---- reconciliation primitives -------------------------------------------------------

    def open(self, path: str | Path, *, source: str | Path | None = None) -> HerdrWorkspaceOpened:
        """``herdr worktree open --cwd <main> --path <exact> --no-focus`` — bind or re-bind.

        ``--cwd`` names the repository's main checkout explicitly: Herdr otherwise resolves the
        repository from its active workspace and refuses when none is active
        (``invalid_request``), and it refuses a linked worktree as the source
        (``linked_worktree_source``). ``source`` defaults to ``path`` for a main checkout.
        """
        args = ["worktree", "open", "--cwd", str(source or path), "--path", str(path)]
        if self._label:
            args += ["--label", self._label]
        args.append("--no-focus")
        payload = self._ok(self._run(*args), "worktree open")
        workspace = payload.get("workspace") if isinstance(payload, dict) else None
        root_pane = payload.get("root_pane") if isinstance(payload, dict) else None
        worktree = payload.get("worktree") if isinstance(payload, dict) else None
        workspace_id = _field(workspace, "workspace_id", "id") or _field(
            worktree, "open_workspace_id"
        )
        if workspace_id is None:
            raise WorkspaceBindingError(
                PRESENTER, "worktree open response is missing workspace_id", code="malformed"
            )
        pane_id = _field(root_pane, "pane_id", "id")
        if pane_id is None:
            raise WorkspaceBindingError(
                PRESENTER, "worktree open response is missing root_pane.pane_id", code="malformed"
            )
        opened = HerdrWorkspaceOpened(
            workspace_id,
            pane_id,
            bool(isinstance(payload, dict) and payload.get("already_open")),
            str(path),
        )
        self.last_opened = opened
        return opened

    def derive(self, path: str | Path) -> str | None:
        """Re-derive the bound workspace id from ``herdr worktree list`` (E35)."""
        payload = self._ok(self._run("worktree", "list", "--cwd", str(path)), "worktree list")
        rows = payload.get("worktrees") if isinstance(payload, dict) else None
        for row in rows if isinstance(rows, list) else ():
            if isinstance(row, dict) and _same_path(str(row.get("path") or ""), path):
                return _field(row, "open_workspace_id")
        return None

    def workspaces(self) -> tuple[HerdrWorkspaceRecord, ...]:
        """Every workspace Herdr currently holds, with its label and bound checkout path."""
        payload = self._ok(self._run("workspace", "list"), "workspace list")
        rows = payload.get("workspaces") if isinstance(payload, dict) else None
        records = []
        for row in rows if isinstance(rows, list) else ():
            workspace_id = _field(row, "workspace_id", "id")
            if workspace_id is None:
                continue
            worktree = row.get("worktree") if isinstance(row, dict) else None
            records.append(
                HerdrWorkspaceRecord(
                    workspace_id,
                    _field(row, "label") or "",
                    _field(worktree, "checkout_path") or "",
                )
            )
        return tuple(records)

    def orphans(self) -> tuple[HerdrWorkspaceRecord, ...]:
        """Beadhive-labelled workspaces whose checkout was removed without a release (E31)."""
        return tuple(
            record
            for record in self.workspaces()
            if record.orphaned and record.beadhive_owned and not Path(record.checkout_path).exists()
        )

    def close(self, workspace_id: str) -> bool:
        """``herdr workspace close <id>``; ``True`` when closed, ``False`` if already gone."""
        result = self._run("workspace", "close", workspace_id)
        if result is not None and getattr(result, "returncode", 1) == 0:
            return True
        code, _detail = self._failure(result)
        if code == "workspace_not_found":
            return False
        self._ok(result, "workspace close")
        return False  # pragma: no cover - _ok always raises for a failed result

    # ---- internals -----------------------------------------------------------------------

    def _release_targets(self, handle: WorktreeHandle) -> tuple[str, ...]:
        if PRESENTER not in handle.bindings:
            return ()
        recorded = handle.bindings.get(PRESENTER) or ""
        records = self.workspaces()
        exact = tuple(
            record.workspace_id
            for record in records
            if record.checkout_path and _same_path(record.checkout_path, handle.path)
        )
        if recorded and recorded in exact:
            return (recorded, *(item for item in exact if item != recorded))
        if exact:
            return exact
        derived = self.derive(handle.path) if Path(handle.path).exists() else None
        return (derived,) if derived else ()

    def _failure(self, result: Any) -> tuple[str, str]:
        if result is None:
            return "unavailable", "herdr is not installed or did not start"
        decoded = _decoded(result)
        error = decoded.get("error") if isinstance(decoded, dict) else None
        if isinstance(error, dict):
            return str(error.get("code") or ""), str(error.get("message") or "")
        return "", str(decoded or f"exit {getattr(result, 'returncode', '?')}")

    def _ok(self, result: Any, action: str) -> Any:
        if result is None or getattr(result, "returncode", 1) != 0:
            code, detail = self._failure(result)
            raise WorkspaceBindingError(
                PRESENTER, f"herdr {action} failed: {detail or code or 'unknown error'}", code=code
            )
        decoded = _decoded(result)
        if isinstance(decoded, dict) and isinstance(decoded.get("error"), dict):
            code, detail = self._failure(result)
            raise WorkspaceBindingError(PRESENTER, f"herdr {action} failed: {detail}", code=code)
        return _payload(decoded)


__all__ = [
    "BEADHIVE_LABEL_PREFIX",
    "DELETED_SUFFIX",
    "PRESENTER",
    "HerdrWorkspaceBinding",
    "HerdrWorkspaceOpened",
    "HerdrWorkspaceRecord",
]
