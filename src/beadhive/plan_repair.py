"""Compatibility facade for the planning capability's repair operation.

Repair is implemented by :func:`beadhive.plan.repair_epic`.  These names remain
import-compatible for callers and patch points that used the historical split module; dependency
flow is one-way and no longer forms plan↔plan_repair.
"""

from dataclasses import dataclass, field

from . import plan

PlanError = plan.PlanError
repair = plan.repair


@dataclass
class RepairResult:
    """Compatibility result shape retained for direct legacy callers."""

    fixes: list[str] = field(default_factory=list)
    problems: list[str] = field(default_factory=list)


def repair_epic(epic_id: str, cfg, cwd, actor: str) -> RepairResult:
    """Delegate to :func:`beadhive.plan.repair_epic` without recreating its policy."""
    result = plan.repair_epic(epic_id, cfg, cwd, actor)
    return RepairResult(list(result.fixes), list(result.problems))


__all__ = ["PlanError", "RepairResult", "repair", "repair_epic"]
