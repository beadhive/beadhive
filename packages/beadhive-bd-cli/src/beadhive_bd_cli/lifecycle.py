"""The lifecycle cohort's ``bd`` routes (bh-sy36q.1; moved out of the root shell by bh-o3xuf).

* :class:`CliIssues` — ``work.issue.get`` / ``work.issue.update``'s CLI-compatibility route,
  selected by the shell only when no capable Beads session opens and its ``work.beads.route``
  allows the ``bd`` route.
* :class:`CliLeases` — ``work.lease.acquire`` / ``work.lease.release``: the renewable claim lease
  Beads 1.3's ``issues.claim`` does not grant, always over ``bd``.
* :class:`CliStateReads` — ``work.state.get`` (``bd state``), always over ``bd``.

Each implements the matching :mod:`beadhive_core` port over the caller's
:class:`~beadhive_bd_cli.transport.BdTransport`.
"""

from __future__ import annotations

from typing import Any

from beadhive_core import WriteFailed

from . import reads
from .transport import BdTransport

__all__ = ["CliIssues", "CliLeases", "CliStateReads"]


class CliIssues:
    """``work.issue.get`` / ``work.issue.update`` over ``bd``."""

    route = "cli-compatibility"

    def __init__(self, bd: BdTransport, main: Any) -> None:
        self._bd = bd
        self._main = main

    def get(self, bead: str) -> dict | None:
        return reads.show(self._bd, bead, self._main)

    def assign(self, bead: str, assignee: str, *, actor: str, read: Any) -> None:
        result = self._bd.run(["assign", bead, assignee], self._main, actor=actor)
        if result.returncode != 0:  # bd streamed its own error
            raise WriteFailed(result.returncode)


class CliLeases:
    """``work.lease.acquire`` (``bd update --claim``) / ``work.lease.release`` (reopen +
    unassign) — the renewable claim lease v1.3's ``issues.claim`` does not grant."""

    def __init__(self, bd: BdTransport, main: Any) -> None:
        self._bd = bd
        self._main = main

    def acquire(self, bead: str, *, actor: str) -> None:
        result = self._bd.run(["update", bead, "--claim"], self._main, actor=actor)
        if result.returncode != 0:
            raise WriteFailed(result.returncode)

    def release(self, bead: str, *, actor: str) -> None:
        args = ["update", bead, "--status", "open", "--assignee", ""]
        result = self._bd.run(args, self._main, actor=actor)
        if result.returncode != 0:
            raise WriteFailed(result.returncode)


class CliStateReads:
    """``work.state.get`` (``bd state``): '' when unset."""

    def __init__(self, bd: BdTransport, main: Any) -> None:
        self._bd = bd
        self._main = main

    def get_state(self, bead: str, dimension: str) -> str:
        return reads.state(self._bd, bead, dimension, self._main)
