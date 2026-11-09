"""Pure rules of the many-to-one report to work link (external-intake-module-adr.md section 6).

Standard library only. A bound report ``tracks`` a work bead (``tracks`` edge, report to work, in
the one operational store); several reports may track one work bead. A reproduction bead may also
investigate a report (bh-u3a53), a separate relation that never carries delivery. These are the
decisions the link service takes before it writes anything, so every refusal leaves the store
untouched, and the two derived reads: the ``delivery`` value a link writes and the work progress of
a tracked bead.
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


def _refuse_public(scope: StoreScope, bead: str) -> None:
    if _public(scope, bead):
        raise IntakeRefused(
            RefusalReason.PUBLIC_STORE_ID,
            f"{bead} belongs to a public store; a link names beads of this hive's own "
            "internal store only (no edge crosses stores)",
        )


def _refuse_report(report_id: str, report: BeadView | None) -> None:
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


def _check_pair(
    *,
    scope: StoreScope,
    report_id: str,
    report: BeadView | None,
    work_id: str,
    work: BeadView | None,
    verb: str,
) -> None:
    """The refusals a ``tracks`` link and an investigation share, in the ADR's order: a public id
    first (no edge crosses stores), then self, then the report, then the target. ``verb`` words
    the self-link refusal (``track`` / ``investigate``)."""
    for bead in (report_id, work_id):
        _refuse_public(scope, bead)
    if report_id == work_id:
        raise IntakeRefused(RefusalReason.SELF_LINK, f"{report_id} cannot {verb} itself")
    _refuse_report(report_id, report)
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
    _check_pair(
        scope=scope,
        report_id=report_id,
        report=report,
        work_id=work_id,
        work=work,
        verb="track",
    )
    if tracked == work_id:
        return LinkKind.ALREADY_LINKED
    if tracked and not replace:
        raise IntakeRefused(
            RefusalReason.ALREADY_LINKED_ELSEWHERE,
            f"{report_id} already tracks {tracked}; pass --replace to move it to {work_id}",
        )
    return LinkKind.RELINKED if tracked else LinkKind.LINKED


def check_investigate(
    *,
    scope: StoreScope,
    report_id: str,
    report: BeadView | None,
    work_id: str,
    work: BeadView | None,
    tracked: str | None,
    investigated_by: Iterable[str],
) -> LinkKind:
    """Whether ``work_id`` may investigate ``report_id`` (bh-u3a53): ``linked`` (a new
    investigation), ``already_linked``, or an :class:`IntakeRefused` with nothing written.

    The refusals of a ``tracks`` link apply in the same order (a reproduction bead is ordinary
    work of this store, never a report), then one of its own: the work the report already
    ``tracks`` is its delivery, so investigating it as well is refused (``TRACKED_WORK``). Several
    reproduction beads may investigate one report, and one bead several reports; there is no
    ``--replace``. The report's ``intake`` value does not matter: reproduction is a triage step."""
    _check_pair(
        scope=scope,
        report_id=report_id,
        report=report,
        work_id=work_id,
        work=work,
        verb="investigate",
    )
    if tracked == work_id:
        raise IntakeRefused(
            RefusalReason.TRACKED_WORK,
            f"{report_id} already tracks {work_id}, so that work is its delivery and its "
            "milestones reach the reporter; a reproduction task is a separate bead (use "
            "`--new-work`)",
        )
    if work_id in tuple(investigated_by):
        return LinkKind.ALREADY_LINKED
    return LinkKind.LINKED


def check_investigable(*, scope: StoreScope, report_id: str, report: BeadView | None) -> None:
    """The report-side refusals of :func:`check_investigate`, decided before ``--new-work``
    creates the reproduction bead so a refused call creates nothing: a public id, an unknown
    report, one with no binding, or a closed one."""
    _refuse_public(scope, report_id)
    _refuse_report(report_id, report)


def delivery_at_link(
    labels: Iterable[str] | None,
    *,
    edge_written: bool,
    unlinked_work: str,
    work: str,
    duplicate: bool = False,
) -> str | None:
    """The ``delivery`` value to write when linking to ``work``, or ``None`` to write nothing.

    A ``duplicate`` link (a ``duplicate-of`` outcome, bh-5kpwc) writes nothing, ever: the report
    shares its canonical's work but its delivery is the canonical report's. The caller clears a
    value the report already carries.

    A new edge means the report now waits on ``work``: no value becomes ``pending``, ``pending``
    stays, and a milestone value (``merged``, ``released``, ``reverted``) belonged to the work the
    report tracked before, so it goes back to ``pending`` unless the report is being re-linked to
    that very work (``unlinked_work``, the fact an unlink left), which it still describes. An edge
    that already existed only repairs a missing value."""
    if duplicate:
        return None
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
    "check_investigable",
    "check_investigate",
    "check_link",
    "delivery_at_link",
    "in_operational_store",
    "work_progress_of",
]
