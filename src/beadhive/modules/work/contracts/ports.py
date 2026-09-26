"""Outbound ports for bead work lifecycle effects."""

from __future__ import annotations

from typing import Any, Protocol

from ..domain import (
    AbandonRequest,
    AbandonResult,
    AssignmentRequest,
    AssignmentResult,
    CheckRequest,
    CheckResult,
    ClaimRequest,
    ClaimResult,
    MergeRequest,
    MergeResult,
    ResumeRequest,
    ResumeResult,
    ReviewRequest,
    ReviewResult,
    ScheduleRequest,
    ScheduleResult,
    SubmissionRequest,
    SubmissionResult,
)


class BeadStore(Protocol):
    """Persist and query lifecycle authority held by the bead store."""

    def assign(self, request: AssignmentRequest) -> AssignmentResult: ...

    def schedule(self, request: ScheduleRequest) -> ScheduleResult: ...

    def abandon(self, request: AbandonRequest) -> AbandonResult: ...


class WorktreeLifecyclePort(Protocol):
    """Provision or reattach worktrees without exposing Git/filesystem mechanics."""

    def claim(self, request: ClaimRequest) -> ClaimResult: ...

    def resume(self, request: ResumeRequest) -> ResumeResult: ...


class ExecutionPort(Protocol):
    """Execute validation, submission, and serialized integration effects."""

    def check(self, request: CheckRequest) -> CheckResult: ...

    def submit(self, request: SubmissionRequest) -> SubmissionResult: ...

    def merge(self, request: MergeRequest) -> MergeResult: ...


class ValidationEvidenceStore(Protocol):
    """Assemble review evidence through the configured validation boundary."""

    def review(self, request: ReviewRequest) -> ReviewResult: ...


class IdentityProvider(Protocol):
    """Resolve a lifecycle actor without leaking environment/config policy inward."""

    def resolve(self, actor: str, *, bead: str, hive: str, action: str) -> str: ...


class WorkNotifier(Protocol):
    """Publish a completed application result to telemetry or notifications."""

    def completed(self, action: str, subject: str, result: Any) -> None: ...
