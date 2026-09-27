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
)
from ..domain import (
    CheckRequest,
    CheckResult,
    MergeRequest,
    MergeResult,
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
        execution: ExecutionPort,
        evidence: ValidationEvidenceStore,
        identity: IdentityProvider,
        notifier: WorkNotifier,
    ) -> None:
        self._beads = beads
        self._execution = execution
        self._evidence = evidence
        self._identity = identity
        self._notifier = notifier

    def schedule(self, request: ScheduleRequest) -> ScheduleResult:
        return self._complete("schedule", request.epic, self._beads.schedule(request))

    def check(self, request: CheckRequest) -> CheckResult:
        return self._complete("check", request.bead, self._execution.check(request))

    def submit(self, request: SubmissionRequest) -> SubmissionResult:
        request = self._resolved(request, "submit", subject=request.subject)
        return self._complete("submit", request.subject, self._execution.submit(request))

    def review(self, request: ReviewRequest) -> ReviewResult:
        return self._complete("review", request.bead, self._evidence.review(request))

    def merge(self, request: MergeRequest) -> MergeResult:
        return self._complete("merge", request.subject, self._execution.merge(request))

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
