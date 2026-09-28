"""In-memory doubles for this package's ports, for consumers that test at the port boundary.

A higher layer that composes the worktree capability substitutes these at the port rather than
patching the package's (or its own adapter's) internals: :class:`InMemoryBeadStateLookup` stands
in for a bead store behind ``BeadStateLookup``, and :class:`InMemoryWorkspaceBinding` is a
reference ``workspace.binding`` provider whose presenter state is plain data (it is also the
subject the binding conformance kit uses to prove its own cases).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..domain import WorkspaceBindingError, WorktreeHandle


class InMemoryBeadStateLookup:
    """A ``BeadStateLookup`` over a fixed set of bead records, recording every read.

    ``issues`` maps a bead id to its record (``status``, ``close_reason``, ``dependencies``,
    ``labels``, ...). ``readable=False`` models a store that cannot be read at all: every read
    answers ``None``, exactly like the argv adapter when ``bd`` fails. ``probe`` answers the
    records themselves, so an empty mapping models a store that answers with zero issues.
    """

    def __init__(
        self, issues: Mapping[str, Mapping[str, Any]] | None = None, *, readable: bool = True
    ) -> None:
        self.issues: dict[str, dict[str, Any]] = {
            bead_id: {"id": bead_id, **record} for bead_id, record in (issues or {}).items()
        }
        self.readable = readable
        self.probes: list[Path] = []
        self.shows: list[tuple[str, Path]] = []
        self.listings: list[Path] = []

    def probe(self, main: Path) -> list[Any] | None:
        self.probes.append(Path(main))
        return list(self.issues.values()) if self.readable else None

    def show(self, bead_id: str, main: Path) -> dict[str, Any] | None:
        self.shows.append((bead_id, Path(main)))
        if not self.readable:
            return None
        record = self.issues.get(bead_id)
        return dict(record) if record is not None else None

    def all_issues(self, main: Path) -> list[Any] | None:
        self.listings.append(Path(main))
        return [dict(record) for record in self.issues.values()] if self.readable else None


@dataclass
class InMemoryPresenter:
    """Presenter-side state behind :class:`InMemoryWorkspaceBinding`: live workspaces by id.

    Ids are reused after a close (the lowest free ``w<n>``), like Herdr's, so a stale cached
    reference can name another checkout's workspace.
    """

    workspaces: dict[str, Path] = field(default_factory=dict)
    available: bool = True

    def open(self, path: Path) -> str:
        for reference, bound in self.workspaces.items():
            if bound == path:
                return reference
        number = 1
        while f"w{number}" in self.workspaces:
            number += 1
        reference = f"w{number}"
        self.workspaces[reference] = path
        return reference

    def derive(self, path: Path) -> str | None:
        return next((ref for ref, bound in self.workspaces.items() if bound == path), None)

    def presentations(self, path: Path) -> int:
        return sum(1 for bound in self.workspaces.values() if bound == path)


class InMemoryWorkspaceBinding:
    """Reference ``workspace.binding`` provider over an :class:`InMemoryPresenter`."""

    def __init__(self, presenter: InMemoryPresenter | None = None, *, name: str = "memory"):
        self.state = presenter or InMemoryPresenter()
        self.presenter = name
        self.calls: list[tuple[str, Path]] = []

    def bind(self, handle: WorktreeHandle) -> WorktreeHandle:
        self.calls.append(("bind", handle.path))
        self._require_available("bind")
        return handle.with_binding(self.presenter, self.state.open(handle.path))

    def release(self, handle: WorktreeHandle) -> None:
        self.calls.append(("release", handle.path))
        if self.presenter not in handle.bindings:
            return
        self._require_available("release")
        # The cached reference is only a cache: close what the presenter holds for this path.
        for reference in self._targets(handle.path):
            del self.state.workspaces[reference]

    def _targets(self, path: Path) -> Iterable[str]:
        return [ref for ref, bound in self.state.workspaces.items() if bound == path]

    def _require_available(self, action: str) -> None:
        if not self.state.available:
            raise WorkspaceBindingError(
                self.presenter, f"{self.presenter} is down; could not {action}", code="down"
            )


__all__ = ["InMemoryBeadStateLookup", "InMemoryPresenter", "InMemoryWorkspaceBinding"]
