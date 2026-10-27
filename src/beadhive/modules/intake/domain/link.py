"""Pure rules of the many-to-one report to work link (external-intake-module-adr.md section 6).

Standard library only. A bound report ``tracks`` a work bead (``tracks`` edge, report to work, in
the one operational store); several reports may track one work bead. These are the decisions the
link service takes before it writes anything, so every refusal leaves the store untouched, and the
two derived reads: the ``delivery`` value a link writes and the work progress of a tracked bead.
"""

from __future__ import annotations

from collections.abc import Iterable

from .errors import IntakeRefused, RefusalReason
from .models import BeadView, LinkKind, StoreScope, WorkProgress
from .references import ReferenceShape, classify_reference
from .vocabulary import INTAKE_UNTRIAGED, delivery_of

#: The label ``bh work submit`` leaves while a bead waits for review.
REVIEW_PENDING_LABEL = "review:pending"


def in_operational_store(scope: StoreScope, bead: str) -> bool:
    """Whether ``bead`` carries this store's own prefix: the only beads a link may name. A bead of
    another hive carries another prefix, so it is never looked up in this store at all."""
    return bead.startswith(f"{scope.operational_prefix}-")


def _public(scope: StoreScope, bead: str) -> bool:
    shape = classify_reference(bead, public_prefixes=scope.public_prefixes)
    return shape is ReferenceShape.PUBLIC_STORE_ID


def check_link(
    *,
    scope: StoreScope,
    report_id: str,
    report: BeadView | None,
    work_id: str,
    work: BeadView | None,
    tracked: str | None,
    replace: bool,
) -> LinkKind:
    """The kind of link to make, or an :class:`IntakeRefused` (nothing written) naming the fix.

    ``report`` and ``work`` are the beads as read (``None``: not found in this store), ``tracked``
    the work ``report`` already tracks. The order is the ADR's: a public id first (no edge crosses
    stores), then self, then the report, then the target. A bound report is never work (D-a): a
    target that is itself bound is refused, which also refuses a chain of reports."""
    for bead in (report_id, work_id):
        if _public(scope, bead):
            raise IntakeRefused(
                RefusalReason.PUBLIC_STORE_ID,
                f"{bead} belongs to a public store; a link names beads of this hive's own "
                "internal store only (no edge crosses stores)",
            )
    if report_id == work_id:
        raise IntakeRefused(RefusalReason.SELF_LINK, f"{report_id} cannot track itself")
    if report is None:
        raise IntakeRefused(RefusalReason.NOT_FOUND, f"{report_id} not found")
    if report.binding is None:
        raise IntakeRefused(
            RefusalReason.NOT_A_REPORT,
            f"{report_id} is not a report bound to an external issue; only an imported report "
            "is linked (a report filed with `bh report` is ordinary backlog once accepted)",
        )
    if report.status == "closed":
        raise IntakeRefused(
            RefusalReason.REPORT_CLOSED,
            f"{report_id} is closed; link a report while it is open (its edge outlives the close)",
        )
    if not in_operational_store(scope, work_id) or work is None:
        raise IntakeRefused(
            RefusalReason.CROSS_STORE_LINK,
            f"{work_id} is not a bead of this hive's operational store; a link never crosses "
            "stores",
        )
    if work.binding is not None:
        raise IntakeRefused(
            RefusalReason.TARGET_IS_REPORT,
            f"{work_id} is itself a report bound to an external issue and a bound report is never "
            "work; link to the work bead, or create one with `accept --new-work`",
        )
    if INTAKE_UNTRIAGED in work.labels:
        raise IntakeRefused(
            RefusalReason.TARGET_UNTRIAGED,
            f"{work_id} is an untriaged intake bead; triage it first, or link to canonical work",
        )
    if tracked == work_id:
        return LinkKind.ALREADY_LINKED
    if tracked and not replace:
        raise IntakeRefused(
            RefusalReason.ALREADY_LINKED_ELSEWHERE,
            f"{report_id} already tracks {tracked}; pass --replace to move it to {work_id}",
        )
    return LinkKind.RELINKED if tracked else LinkKind.LINKED


def delivery_at_link(
    labels: Iterable[str] | None, *, edge_written: bool, unlinked_work: str, work: str
) -> str | None:
    """The ``delivery`` value to write when linking to ``work``, or ``None`` to write nothing.

    A new edge means the report now waits on ``work``: no value becomes ``pending``, ``pending``
    stays, and a milestone value (``merged``, ``released``, ``reverted``) belonged to the work the
    report tracked before, so it goes back to ``pending`` unless the report is being re-linked to
    that very work (``unlinked_work``, the fact an unlink left), which it still describes. An edge
    that already existed only repairs a missing value."""
    current = delivery_of(labels)
    if current is None:
        return "pending"
    if not edge_written or current == "pending":
        return None
    return None if unlinked_work == work else "pending"


def work_progress_of(status: str, labels: Iterable[str] | None) -> WorkProgress:
    """The progress of a tracked work bead from its status and ``review:`` label."""
    if status == "closed":
        return WorkProgress.CLOSED
    if REVIEW_PENDING_LABEL in tuple(labels or ()):
        return WorkProgress.IN_REVIEW
    if status in {"in_progress", "hooked"}:
        return WorkProgress.IN_PROGRESS
    return WorkProgress.OPEN


__all__ = [
    "REVIEW_PENDING_LABEL",
    "check_link",
    "delivery_at_link",
    "in_operational_store",
    "work_progress_of",
]
