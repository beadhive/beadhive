"""The review cohort's ``bd`` ports (bh-bwnys.1; moved out of the root shell by bh-o3xuf).

Beads 1.3 has no gate lookup/resolve route and no state-dimension route, so
:class:`beadhive_core.ReviewCommands`' ``GateOperations`` and ``StateOperations`` ports are always
served over ``bd`` — ``work.gate.lookup`` / ``work.gate.resolve`` and ``work.state.update``.
"""

from __future__ import annotations

from typing import Any

from beadhive_core import Gate, GateLookupFailed, GateResolveFailed, StateUpdateFailed

from .transport import BdTransport, err_line, names_bead

__all__ = ["CliGateOperations", "CliStateOperations"]


class CliGateOperations:
    """``GateOperations`` over the named ``work.gate.lookup`` / ``work.gate.resolve`` routes.

    Lookup is the same ``bd gate list --limit 0 --all`` read the review-gate selector has always
    used (``--limit 0`` defeats bd's 50-row window, bh-pwi2), narrowed with the anchored
    :func:`~beadhive_bd_cli.transport.names_bead` match (bh-1vvdp). Unlike that selector, a failed
    read raises instead of reading as "no gates", so approve and bounce fail closed on it.
    """

    def __init__(self, bd: BdTransport, main: Any) -> None:
        self._bd = bd
        self._main = main

    def gates_for(self, bead: str) -> list[Gate]:
        rows = self._bd.json(["gate", "list", "--limit", "0", "--all"], self._main)
        if not isinstance(rows, list):
            raise GateLookupFailed("`bd gate list` failed or returned no JSON list")
        return [
            Gate(
                id=str(row.get("id") or ""),
                status=str(row.get("status") or ""),
                description=str(row.get("description") or ""),
                reason=str(row.get("reason") or ""),
                await_type=str(row.get("await_type") or ""),
            )
            for row in rows
            if isinstance(row, dict) and names_bead(row.get("description"), bead)
        ]

    def resolve(self, gate_id: str, *, reason: str, actor: str) -> None:
        args = ["gate", "resolve", gate_id, "--reason", reason]
        result = self._bd.run(args, self._main, actor=actor)
        if result.returncode != 0:
            raise GateResolveFailed(gate_id, result.returncode, err_line(result))


class CliStateOperations:
    """``StateOperations`` over the named ``work.state.update`` route (``bd set-state``)."""

    def __init__(self, bd: BdTransport, main: Any) -> None:
        self._bd = bd
        self._main = main

    def set_state(self, bead: str, dimension: str, value: str, *, reason: str, actor: str) -> None:
        args = ["set-state", bead, f"{dimension}={value}", "--reason", reason]
        result = self._bd.run(args, self._main, actor=actor)
        if result.returncode != 0:
            raise StateUpdateFailed(result.returncode, err_line(result))
