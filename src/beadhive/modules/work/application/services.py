"""Typed application boundary for bead work lifecycle commands."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from ..contracts import (
    BeadStore,
    ExecutionPort,
    IdentityProvider,
    ValidationEvidenceStore,
    WorkNotifier,
    WorktreeLifecyclePort,
)
from ..domain import (
    AbandonRequest,
    AbandonResult,
    ApprovalRequest,
    ApprovalResult,
    AssignmentRequest,
    AssignmentResult,
    BounceRequest,
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


class WorkLifecycleService:
    """Coordinate lifecycle use cases over explicit effect-owning ports."""

    def __init__(
        self,
        *,
        beads: BeadStore,
        worktrees: WorktreeLifecyclePort,
        execution: ExecutionPort,
        evidence: ValidationEvidenceStore,
        identity: IdentityProvider,
        notifier: WorkNotifier,
    ) -> None:
        self._beads = beads
        self._worktrees = worktrees
        self._execution = execution
        self._evidence = evidence
        self._identity = identity
        self._notifier = notifier

    def assign(self, request: AssignmentRequest) -> AssignmentResult:
        request = self._resolved(request, "assign")
        return self._complete("assign", request.bead, self._beads.assign(request))

    def claim(self, request: ClaimRequest) -> ClaimResult:
        request = self._resolved(request, "claim", subject=request.subject)
        return self._complete("claim", request.subject, self._worktrees.claim(request))

    def schedule(self, request: ScheduleRequest) -> ScheduleResult:
        return self._complete("schedule", request.epic, self._beads.schedule(request))

    def check(self, request: CheckRequest) -> CheckResult:
        return self._complete("check", request.bead, self._execution.check(request))

    def submit(self, request: SubmissionRequest) -> SubmissionResult:
        request = self._resolved(request, "submit", subject=request.subject)
        return self._complete("submit", request.subject, self._execution.submit(request))

    def review(self, request: ReviewRequest) -> ReviewResult:
        return self._complete("review", request.bead, self._evidence.review(request))

    def approve(self, request: ApprovalRequest) -> ApprovalResult:
        request = self._resolved(request, "approve")
        return self._complete("approve", request.bead, self._beads.approve(request))

    def bounce(self, request: BounceRequest) -> ApprovalResult:
        request = self._resolved(request, "bounce")
        return self._complete("bounce", request.bead, self._beads.bounce(request))

    def merge(self, request: MergeRequest) -> MergeResult:
        return self._complete("merge", request.subject, self._execution.merge(request))

    def resume(self, request: ResumeRequest) -> ResumeResult:
        request = self._resolved(request, "resume")
        return self._complete("resume", request.bead, self._worktrees.resume(request))

    def abandon(self, request: AbandonRequest) -> AbandonResult:
        return self._complete("abandon", request.bead, self._beads.abandon(request))

    def _resolved(self, request: Any, action: str, *, subject: str = "") -> Any:
        actor = self._identity.resolve(
            request.actor,
            bead=subject or request.bead,
            hive=request.hive,
            action=action,
        )
        return replace(request, actor=actor)

    def _complete(self, action: str, subject: str, result: Any) -> Any:
        result_subject = getattr(result, "bead", getattr(result, "epic", ""))
        if result_subject != subject:
            raise ValueError(f"{action} result changed the requested bead identity")
        self._notifier.completed(action, subject, result)
        return result
