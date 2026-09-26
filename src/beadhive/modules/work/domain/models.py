"""Transport-neutral requests and results for the bead work lifecycle."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


def _required(value: str, name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{name} must be non-empty")
    return normalized


def _selector(*values: object) -> str:
    """Return the first real selector while ignoring outer CLI default sentinels."""

    return next((value.strip() for value in values if isinstance(value, str) and value.strip()), "")


@dataclass(frozen=True, slots=True)
class AssignmentRequest:
    bead: str
    assignee: str
    actor: str = ""
    hive: str = ""
    preview: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "bead", _required(self.bead, "bead"))
        object.__setattr__(self, "assignee", _required(self.assignee, "assignee"))


@dataclass(frozen=True, slots=True)
class ClaimRequest:
    bead: str = ""
    actor: str = ""
    group: str = ""
    collapse: str = ""
    hive: str = ""
    preview: bool = False

    @property
    def subject(self) -> str:
        return _selector(self.bead, self.group, self.collapse)


@dataclass(frozen=True, slots=True)
class ScheduleRequest:
    epic: str
    hive: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "epic", _required(self.epic, "epic"))


@dataclass(frozen=True, slots=True)
class CheckRequest:
    bead: str
    hive: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "bead", _required(self.bead, "bead"))


@dataclass(frozen=True, slots=True)
class SubmissionRequest:
    bead: str = ""
    actor: str = ""
    hive: str = ""
    group: str = ""

    @property
    def subject(self) -> str:
        return _selector(self.bead, self.group)


@dataclass(frozen=True, slots=True)
class ReviewRequest:
    bead: str
    hive: str = ""
    run_validation: bool = False
    run_demo: bool = False
    fresh: bool = True
    views: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(self, "bead", _required(self.bead, "bead"))


@dataclass(frozen=True, slots=True)
class MergeRequest:
    bead: str = ""
    hive: str = ""
    remove_worktree: bool = False
    molecule: bool = False
    group: str = ""

    @property
    def subject(self) -> str:
        return _selector(self.bead, self.group)


@dataclass(frozen=True, slots=True)
class ResumeRequest:
    bead: str
    actor: str = ""
    hive: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "bead", _required(self.bead, "bead"))


@dataclass(frozen=True, slots=True)
class AbandonRequest:
    bead: str
    hive: str = ""
    remove_worktree: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "bead", _required(self.bead, "bead"))


@dataclass(frozen=True, slots=True)
class AssignmentResult:
    bead: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class ClaimResult:
    bead: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class ScheduleResult:
    epic: str
    plan: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class CheckResult:
    bead: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class SubmissionResult:
    bead: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class ReviewResult:
    bead: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class MergeResult:
    bead: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class ResumeResult:
    bead: str
    value: Any = None


@dataclass(frozen=True, slots=True)
class AbandonResult:
    bead: str
    value: Any = None
