"""Transport-neutral planning requests, results, and molecule DAG policy."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any


def _required(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must be non-empty")
    return normalized


class PlanningError(Exception):
    """A planning application operation could not satisfy its contract."""


@dataclass(frozen=True, slots=True)
class ValidationRequest:
    spec: dict[str, Any]
    config: Any


@dataclass(frozen=True, slots=True)
class ValidationResult:
    problems: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.problems


@dataclass(frozen=True, slots=True)
class FilingRequest:
    spec: dict[str, Any]
    workspace: Path
    actor: str
    config: Any


@dataclass(frozen=True, slots=True)
class FilingResult:
    epic_id: str
    issue_count: int
    root_count: int
    adopt_count: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "epic_id", _required(self.epic_id, "epic_id"))


@dataclass(frozen=True, slots=True)
class KickoffRequest:
    epic_id: str
    workspace: Path
    actor: str
    config: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "epic_id", _required(self.epic_id, "epic_id"))


@dataclass(frozen=True, slots=True)
class KickoffResult:
    epic_id: str
    resolved_gates: int
    already_approved: bool = False


@dataclass(frozen=True, slots=True)
class VerificationRequest:
    epic_id: str
    workspace: Path
    config: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "epic_id", _required(self.epic_id, "epic_id"))


@dataclass(frozen=True, slots=True)
class VerificationResult:
    epic_id: str
    problems: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.problems


@dataclass(frozen=True, slots=True)
class RepairRequest:
    epic_id: str
    workspace: Path
    actor: str
    config: Any

    def __post_init__(self) -> None:
        object.__setattr__(self, "epic_id", _required(self.epic_id, "epic_id"))


@dataclass(frozen=True, slots=True)
class RepairResult:
    epic_id: str
    fixes: tuple[str, ...] = ()
    problems: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MoleculeGraph:
    """Validated dependency order over planner-local issue handles."""

    order: tuple[str, ...]
    roots: tuple[str, ...]

    @classmethod
    def from_issues(cls, issues: Sequence[Mapping[str, Any]]) -> MoleculeGraph:
        handles = tuple(str(issue["handle"]) for issue in issues)
        if len(set(handles)) != len(handles):
            raise ValueError("molecule issue handles must be unique")
        known = set(handles)
        dependencies = {
            handle: tuple(str(dep) for dep in (issue.get("deps") or ()))
            for handle, issue in zip(handles, issues, strict=True)
        }
        unknown = sorted(
            {dep for deps in dependencies.values() for dep in deps if dep not in known}
        )
        if unknown:
            raise ValueError(f"molecule dependencies name unknown handles: {', '.join(unknown)}")

        indegree = {handle: len(dependencies[handle]) for handle in handles}
        ready = [handle for handle in handles if indegree[handle] == 0]
        ordered: list[str] = []
        while ready:
            current = ready.pop(0)
            ordered.append(current)
            for handle in handles:
                if current in dependencies[handle]:
                    indegree[handle] -= 1
                    if indegree[handle] == 0:
                        ready.append(handle)
        if len(ordered) != len(handles):
            raise ValueError("molecule dependency graph contains a cycle")
        return cls(tuple(ordered), tuple(handle for handle in handles if not dependencies[handle]))

    def ordered_issues(self, issues: Sequence[Mapping[str, Any]]) -> tuple[Mapping[str, Any], ...]:
        by_handle = {str(issue["handle"]): issue for issue in issues}
        return tuple(by_handle[handle] for handle in self.order)
