"""Value objects for post-create worktree init-rule execution (bh-qdezo.6)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class CommandOutcome:
    """One :class:`~beadhive_worktrees.contracts.init_ports.CommandRunner` invocation's result.

    ``stdout`` is read only by the git-config stamp lookups; ``notes``/``warnings`` are
    diagnostic lines the port wants surfaced (e.g. cache-locality selection) — the policy
    never inspects their content, only relays them through its own ``report``/``warn``
    callbacks, in order, before evaluating ``missing``/``returncode``.
    """

    returncode: int
    missing: bool = False
    stdout: str = ""
    notes: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


__all__ = ["CommandOutcome"]
