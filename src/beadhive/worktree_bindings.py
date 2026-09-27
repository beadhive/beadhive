"""Durable, per-worktree record of presentation bindings (bh-cb4jo).

``WorktreeHandle.bindings`` is an in-memory cache; this module is where it survives between
``bh`` processes. Each linked worktree keeps its own record in Git-owned per-worktree config
(the same home as the init-rule fingerprint, ``worktree_verify``), so the record follows the
worktree across ``git worktree move`` and disappears with that worktree incarnation:

* ``beadhive.bindingsession.<presenter>`` — the presenter session the binding lives in. Written
  *before* the bind is attempted: it is the durable "this worktree should be bound" intent, so a
  crash between create and bind (E34) or a presenter outage at bind time (E33) leaves a visible,
  repairable gap instead of silence.
* ``beadhive.binding.<presenter>`` — the binding reference (for Herdr, the ``workspace_id``),
  written once the bind succeeded.

The reference is only ever a cache of a fact the presenter can re-derive (``herdr worktree list``
→ ``open_workspace_id``, E35); losing it costs one idempotent re-bind, never data.

Git is invoked directly with :mod:`subprocess` (not through ``beadhive.run``) on purpose: a
binding read must stay a cheap, side-effect-free local probe that never touches the operator's
command runner seams, and every failure (not a Git checkout, Git missing) reads as "no record".
"""

from __future__ import annotations

import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .modules.worktrees import WorktreeHandle

_REFERENCE_KEY = "beadhive.binding.{presenter}"
_SESSION_KEY = "beadhive.bindingsession.{presenter}"
_TIMEOUT_SECONDS = 10.0

GitRunner = Callable[[list[str]], Any]


def _default_git(args: list[str]) -> Any:
    try:
        return subprocess.run(
            args, check=False, capture_output=True, text=True, timeout=_TIMEOUT_SECONDS
        )
    except (OSError, subprocess.SubprocessError):
        return None


@dataclass(frozen=True, slots=True)
class BindingRecord:
    """One presenter's recorded binding for one worktree.

    ``reference`` is empty while the bind is still pending (intent recorded, no reference yet).
    """

    presenter: str
    session: str
    reference: str = ""

    @property
    def pending(self) -> bool:
        return not self.reference


class BindingStore:
    """Read and write binding records in a worktree's per-worktree Git config."""

    def __init__(self, git: GitRunner | None = None) -> None:
        self._git = git or _default_git

    def read(self, path: str | Path) -> dict[str, BindingRecord]:
        """Every presenter binding recorded for the worktree at ``path`` (empty if none)."""
        if not Path(path).is_dir():
            return {}
        result = self._git(
            [
                "git",
                "-C",
                str(path),
                "config",
                "--worktree",
                "--get-regexp",
                r"^beadhive\.binding(session)?\.",
            ]
        )
        if result is None or getattr(result, "returncode", 1) != 0:
            return {}
        sessions: dict[str, str] = {}
        references: dict[str, str] = {}
        for line in str(getattr(result, "stdout", "") or "").splitlines():
            key, _sep, value = line.strip().partition(" ")
            section, _dot, presenter = key.rpartition(".")
            if not presenter:
                continue
            if section == "beadhive.bindingsession":
                sessions[presenter] = value.strip()
            elif section == "beadhive.binding":
                references[presenter] = value.strip()
        return {
            presenter: BindingRecord(
                presenter, sessions.get(presenter, ""), references.get(presenter, "")
            )
            for presenter in sorted(set(sessions) | set(references))
        }

    def record_intent(self, path: str | Path, presenter: str, session: str) -> bool:
        """Durably note that ``presenter`` should be bound, before the bind is attempted."""
        return self._write(
            path, {_SESSION_KEY.format(presenter=presenter): session}
        ) and self._unset(path, _REFERENCE_KEY.format(presenter=presenter))

    def record(self, path: str | Path, presenter: str, session: str, reference: str) -> bool:
        """Record a successful bind's reference."""
        return self._write(
            path,
            {
                _SESSION_KEY.format(presenter=presenter): session,
                _REFERENCE_KEY.format(presenter=presenter): reference,
            },
        )

    def clear(self, path: str | Path, presenter: str) -> bool:
        """Forget a presenter binding entirely (after a release with no remove to follow)."""
        return self._unset(path, _REFERENCE_KEY.format(presenter=presenter)) and self._unset(
            path, _SESSION_KEY.format(presenter=presenter)
        )

    def main_checkout(self, path: str | Path) -> Path | None:
        """The main checkout that owns the (possibly linked) worktree at ``path``."""
        result = self._git(
            ["git", "-C", str(path), "rev-parse", "--path-format=absolute", "--git-common-dir"]
        )
        if result is None or getattr(result, "returncode", 1) != 0:
            return None
        common = Path(str(getattr(result, "stdout", "") or "").strip())
        return common.parent if common.name == ".git" else None

    def handle(
        self,
        main: Path,
        path: Path,
        branch: str = "",
        *,
        records: Mapping[str, BindingRecord] | None = None,
    ) -> WorktreeHandle:
        """A handle for the worktree at ``path`` carrying its recorded binding references.

        A pending record contributes an empty reference: the presenter binding must then
        re-derive it from its own inventory rather than trust a cache that was never written.
        """
        records = self.read(path) if records is None else records
        handle = WorktreeHandle(path.name, main, path, branch)
        for presenter, record in records.items():
            handle = handle.with_binding(presenter, record.reference)
        return handle

    def _write(self, path: str | Path, values: Mapping[str, str]) -> bool:
        enabled = self._git(["git", "-C", str(path), "config", "extensions.worktreeConfig", "true"])
        if enabled is None or getattr(enabled, "returncode", 1) != 0:
            return False
        for key, value in values.items():
            written = self._git(["git", "-C", str(path), "config", "--worktree", key, value])
            if written is None or getattr(written, "returncode", 1) != 0:
                return False
        return True

    def _unset(self, path: str | Path, key: str) -> bool:
        result = self._git(["git", "-C", str(path), "config", "--worktree", "--unset-all", key])
        # Exit 5 is Git's "no such key" — already absent is the desired end state.
        return result is not None and getattr(result, "returncode", 1) in (0, 5)


#: The process-wide store; tests replace it with an in-memory fake.
STORE = BindingStore()


__all__ = ["STORE", "BindingRecord", "BindingStore"]
