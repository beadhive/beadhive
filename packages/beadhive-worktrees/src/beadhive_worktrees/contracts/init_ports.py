"""Outbound port for running one init-rule (or git-config stamp) command (bh-qdezo.6).

The one seam through which :mod:`beadhive_worktrees.policy.init_rules` reaches a real process —
package-local tests supply a fake here, never a real subprocess.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from ..domain.init_models import CommandOutcome


class CommandRunner(Protocol):
    """Run one external command in ``cwd`` and report its outcome."""

    def run(self, argv: list[str], *, cwd: Path) -> CommandOutcome: ...


__all__ = ["CommandRunner"]
