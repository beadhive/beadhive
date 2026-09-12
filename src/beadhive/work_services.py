"""Live compatibility adapters for :mod:`beadhive.modules.work`.

The capability stays transport- and infrastructure-neutral.  This outer composition layer binds
its ports to the established ``work.py`` collaborators on every call, so monkeypatch seams remain
live and no process-global service caches mutable test or operator state.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from .modules.work import (
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
    WorkLifecycleService,
)

Operation = Callable[[Any], Any]
IdentityResolver = Callable[[str, str, str, str], str]
CompletionNotifier = Callable[[str, str, Any], None]


def _unbound(request: Any) -> Any:
    raise RuntimeError(f"work lifecycle adapter is not bound for {type(request).__name__}")


class CallbackBeadStore:
    """Bind bead authority operations to live legacy callbacks."""

    def __init__(
        self,
        *,
        assign: Operation = _unbound,
        schedule: Operation = _unbound,
        approve: Operation = _unbound,
        bounce: Operation = _unbound,
        abandon: Operation = _unbound,
    ) -> None:
        self._assign = assign
        self._schedule = schedule
        self._approve = approve
        self._bounce = bounce
        self._abandon = abandon

    def assign(self, request: AssignmentRequest) -> AssignmentResult:
        return AssignmentResult(request.bead, self._assign(request))

    def schedule(self, request: ScheduleRequest) -> ScheduleResult:
        value = self._schedule(request)
        plan = value if isinstance(value, Mapping) else {}
        return ScheduleResult(request.epic, plan)

    def approve(self, request: ApprovalRequest) -> ApprovalResult:
        return ApprovalResult(request.bead, self._approve(request))

    def bounce(self, request: BounceRequest) -> ApprovalResult:
        return ApprovalResult(request.bead, self._bounce(request))

    def abandon(self, request: AbandonRequest) -> AbandonResult:
        return AbandonResult(request.bead, self._abandon(request))


class CallbackWorktreeLifecycle:
    """Bind claim/resume while the legacy adapter consumes the worktrees capability."""

    def __init__(self, *, claim: Operation = _unbound, resume: Operation = _unbound) -> None:
        self._claim = claim
        self._resume = resume

    def claim(self, request: ClaimRequest) -> ClaimResult:
        return ClaimResult(request.subject, self._claim(request))

    def resume(self, request: ResumeRequest) -> ResumeResult:
        return ResumeResult(request.bead, self._resume(request))


class CallbackExecution:
    """Bind validation/submission/integration execution to existing implementations."""

    def __init__(
        self,
        *,
        check: Operation = _unbound,
        submit: Operation = _unbound,
        merge: Operation = _unbound,
    ) -> None:
        self._check = check
        self._submit = submit
        self._merge = merge

    def check(self, request: CheckRequest) -> CheckResult:
        return CheckResult(request.bead, self._check(request))

    def submit(self, request: SubmissionRequest) -> SubmissionResult:
        return SubmissionResult(request.subject, self._submit(request))

    def merge(self, request: MergeRequest) -> MergeResult:
        return MergeResult(request.subject, self._merge(request))


class CallbackValidationEvidence:
    def __init__(self, review: Operation = _unbound) -> None:
        self._review = review

    def review(self, request: ReviewRequest) -> ReviewResult:
        return ReviewResult(request.bead, self._review(request))


class CallbackIdentityProvider:
    def __init__(self, resolve: IdentityResolver | None = None) -> None:
        self._resolve = resolve or (lambda actor, _bead, _hive, _action: actor)

    def resolve(self, actor: str, *, bead: str, hive: str, action: str) -> str:
        return self._resolve(actor, bead, hive, action)


class CallbackWorkNotifier:
    def __init__(self, completed: CompletionNotifier | None = None) -> None:
        self._completed = completed or (lambda _action, _subject, _result: None)

    def completed(self, action: str, subject: str, result: Any) -> None:
        self._completed(action, subject, result)


def work_lifecycle_service(
    *,
    assign: Operation = _unbound,
    claim: Operation = _unbound,
    schedule: Operation = _unbound,
    check: Operation = _unbound,
    submit: Operation = _unbound,
    review: Operation = _unbound,
    approve: Operation = _unbound,
    bounce: Operation = _unbound,
    merge: Operation = _unbound,
    resume: Operation = _unbound,
    abandon: Operation = _unbound,
    resolve_identity: IdentityResolver | None = None,
    notify: CompletionNotifier | None = None,
) -> WorkLifecycleService:
    """Construct one uncached service with the currently live compatibility callbacks."""

    return WorkLifecycleService(
        beads=CallbackBeadStore(
            assign=assign,
            schedule=schedule,
            approve=approve,
            bounce=bounce,
            abandon=abandon,
        ),
        worktrees=CallbackWorktreeLifecycle(claim=claim, resume=resume),
        execution=CallbackExecution(check=check, submit=submit, merge=merge),
        evidence=CallbackValidationEvidence(review),
        identity=CallbackIdentityProvider(resolve_identity),
        notifier=CallbackWorkNotifier(notify),
    )
