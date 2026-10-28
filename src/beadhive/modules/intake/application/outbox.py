"""Translation between the outbox's intake-owned records and the tracker port (resolution X2).

The pure machine lives in :mod:`..domain.outbox`; this is the only tracker-typed code of the
outbox, and it is translation only: it never calls a writer or a reader. The supervised sender
that holds ``IntakeLock``, persists O9 before the request and calls ``post_comment`` /
``change_labels`` exactly once is :mod:`.sender` (``bh-6wj9y`` R6, I9a and I9b).

* :func:`write_grant` turns an O5 approval into the port's single-use ``WriteGrant``, scoped to
  one operation and the record's own issue.
* :func:`capabilities` reads ``probe_capabilities`` into the usable set O5 checks.
* :func:`send_outcome` classifies what a write raised: only the port's proof-of-no-write
  classes are a refusal (O11); ``WriteOutcomeUnknown``, a transient failure or anything else is
  unknown (O12), never retried.
* :func:`receipt` maps a ``WriteReceipt`` (O10); :func:`marked_comments` maps a listing to the
  marked comments reconciliation reads (O14, O15, O19).
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from enum import StrEnum

from beadhive.modules.tracker.contracts import (
    AuthenticationFailed,
    CapabilityReport,
    IssueKey,
    NotVisible,
    Operation,
    OperationNotEnabled,
    OwnMarker,
    PayloadRejected,
    PermissionDenied,
    RateLimited,
    TargetStateRefused,
    TrackerComment,
    WriteGrant,
    WriteReceipt,
)

from ..domain import (
    Approval,
    Capabilities,
    IntakeRefused,
    MarkedComment,
    OpKind,
    OwnIdentity,
    Receipt,
    RefusalReason,
    SendPlan,
    ThreadActor,
)
from ..domain.outbox import OutboxRecord
from .threads import thread_actor

#: The port's errors that prove nothing was written (``bh-6wj9y`` E11; ``LabelNotOwned`` is an
#: ``OperationNotEnabled``). A rate limit refused the request before it was applied.
PROOF_OF_NO_WRITE: tuple[type[Exception], ...] = (
    OperationNotEnabled,
    AuthenticationFailed,
    PermissionDenied,
    NotVisible,
    TargetStateRefused,
    PayloadRejected,
    RateLimited,
)


class SendOutcome(StrEnum):
    REFUSED = "refused"  # O11: failed, the grant consumed
    UNKNOWN = "unknown"  # O12: may exist; only reconciliation resolves it


def send_outcome(error: BaseException) -> SendOutcome:
    """O11 for a proof of no write, O12 for everything else (never a retry)."""
    return SendOutcome.REFUSED if isinstance(error, PROOF_OF_NO_WRITE) else SendOutcome.UNKNOWN


def refusal_name(error: BaseException) -> str:
    """The ``failed`` reason recorded for an O11 refusal: the error class, never its text."""
    return type(error).__name__


def operation(kind: OpKind) -> Operation:
    return Operation(kind.value)


def issue_key(record: OutboxRecord) -> IssueKey:
    return IssueKey(record.binding.identifier, record.binding.url)


def write_grant(record: OutboxRecord, kind: OpKind, approval: Approval) -> WriteGrant:
    """The single-use grant an O5 approval minted, for exactly one operation and target."""
    if approval.grant_id not in {a.grant_id for o in record.operations for a in o.approvals}:
        raise IntakeRefused(
            RefusalReason.GRANT_UNUSABLE, f"{approval.grant_id} is not an approval of this record"
        )
    return WriteGrant(approval.grant_id, operation(kind), issue_key(record))


def plan_grant(record: OutboxRecord, plan: SendPlan) -> WriteGrant:
    """The grant an O9 plan hands the writer, once."""
    return WriteGrant(plan.grant_id, operation(plan.kind), issue_key(record))


def own_marker(identity: OwnIdentity, outbox_id: str) -> OwnMarker:
    """The marker every attempt of one comment operation carries (the same ``outbox_id``)."""
    return OwnMarker(identity.hive, identity.install, outbox_id)


def capabilities(report: CapabilityReport) -> Capabilities:
    """The operations ``probe_capabilities`` reports usable (supported, enabled, not ``NO``)."""
    return Capabilities(frozenset(op.value for op, state in report.states.items() if state.usable))


def receipt(answer: WriteReceipt) -> Receipt:
    """O10's receipt: the remote comment key, or the labels after."""
    comment = answer.comment
    return Receipt(
        comment_id=None if comment is None else comment.key.id,
        global_id=None if comment is None else comment.key.global_id,
        labels_after=answer.labels_after,
        actor=None if comment is None else thread_actor(comment.author),
    )


def marked_comments(
    comments: Iterable[TrackerComment],
    identity: OwnIdentity,
    known_actors: Collection[ThreadActor],
) -> tuple[MarkedComment, ...]:
    """Every listed comment whose decoded marker names this hive and install, with whether its
    author is a known actor (a marker alone is never sufficient)."""
    known = {(a.id, a.kind) for a in known_actors}
    found: list[MarkedComment] = []
    for comment in comments:
        marker = comment.marker
        if marker is None or (marker.hive, marker.install) != (identity.hive, identity.install):
            continue
        actor = thread_actor(comment.author)
        found.append(
            MarkedComment(
                comment.key.id,
                marker.outbox_id,
                actor,
                actor is not None and (actor.id, actor.kind) in known,
                comment.key.global_id,
            )
        )
    return tuple(found)


__all__ = [
    "PROOF_OF_NO_WRITE",
    "SendOutcome",
    "capabilities",
    "issue_key",
    "marked_comments",
    "operation",
    "own_marker",
    "plan_grant",
    "receipt",
    "refusal_name",
    "send_outcome",
    "write_grant",
]
