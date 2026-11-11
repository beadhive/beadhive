"""The read-only scan of a closed report's end state (bh-cd03d, bh-c0x0i).

Two invalid states of the 0.35.10 lifecycle (``docs/INTAKE-LIFECYCLE.md``) had no finder:

* a report CLOSED in the store whose bound issue is still open on the tracker, found with the ONE
  issue listing the backfill makes (and no listing at all when no closed report is bound to the
  tracker project); and
* a report closed while its reporter was never told the current delivery milestone, because the
  completion comment was abandoned: :func:`~..domain.delivery.notification_behind` is true and the
  report's outbox holds an abandoned comment record.

Nothing here writes, drafts or sends. A report whose bound issue was closed by a SENT
``issue.close``
on any record (a reply plus ``backfill-close``, the way the 0.35.9 gaps were closed by hand) was
told, even though ``notified`` still reads ``replied``, and is never listed as behind.
"""

from __future__ import annotations

from beadhive.modules.tracker.contracts import TrackerAdapter, TrackerError

from ..contracts import BeadView, ReportStore
from ..domain import (
    ClosedOpenIssue,
    EndStateFindings,
    IntakeRefused,
    NotificationBehind,
    OpKind,
    OpState,
    OutboxDocument,
    OutboxEvent,
    OutboxRecord,
    ProposalStatus,
    RefusalReason,
    close_reason_for,
    delivery_of,
    notification_behind,
    notified_of,
)
from .labels import issue_index

#: The records whose comment tells the reporter a delivery milestone.
_MILESTONES = frozenset({OutboxEvent.MERGED, OutboxEvent.RELEASED, OutboxEvent.CORRECTED})
#: A close (or comment) that is still on its way: drafted, held, approved or in flight.
_LIVE = frozenset(
    {OpState.DRAFTED, OpState.HELD, OpState.APPROVED, OpState.SENDING, OpState.UNKNOWN}
)


def told_by_a_sent_close(doc: OutboxDocument) -> bool:
    """True when ANY record's ``issue.close`` was sent: its comment went first (O1), so the
    reporter heard from us whatever ``notified`` says."""
    return any(
        (close := r.op(OpKind.CLOSE)) is not None and close.state is OpState.SENT
        for r in doc.records.values()
    )


def abandoned_milestone(doc: OutboxDocument) -> OutboxRecord | None:
    """The latest milestone record whose comment was abandoned and that no live milestone comment
    replaces, or ``None``."""
    comments = [
        (r, c)
        for r in doc.records.values()
        if r.event in _MILESTONES and (c := r.op(OpKind.COMMENT)) is not None
    ]
    if any(c.state in _LIVE for _, c in comments):
        return None  # a redraft is on its way: not stranded
    gone = sorted((r for r, c in comments if c.state is OpState.ABANDONED), key=_seq)
    return gone[-1] if gone else None


def _seq(record: OutboxRecord) -> int:
    return record.created_seq


def notification_gap(view: BeadView, doc: OutboxDocument | None) -> NotificationBehind | None:
    """The finding for one CLOSED report, or ``None`` when the reporter was told (or never owed a
    milestone comment)."""
    delivery = delivery_of(view.labels)
    notified = notified_of(view.labels)
    if doc is None or not notification_behind(delivery, notified):
        return None
    if told_by_a_sent_close(doc):
        return None
    record = abandoned_milestone(doc)
    if record is None:
        return None
    return NotificationBehind(view.id, delivery or "", notified or "", record.outbox_id)


def scan_end_states(
    store: ReportStore, tracker: TrackerAdapter, *, check_open: bool = True
) -> EndStateFindings:
    """Scan the store's closed, bound reports of ``tracker``'s project. Read-only; the issue
    listing is made once, and only when a closed report could need it."""
    project = tracker.describe().project
    closed = [
        v
        for v in store.bound_reports()
        if v.status == "closed"
        and v.binding is not None
        and (v.binding.tracker, v.binding.project) == (project.tracker, project.locator)
    ]
    if not closed:
        return EndStateFindings()
    docs = {v.id: store.read_outbox(v.id) for v in closed}
    behind = tuple(found for v in closed if (found := notification_gap(v, docs[v.id])) is not None)
    if not check_open:
        return EndStateFindings(behind=behind)
    try:
        live = issue_index(tracker, include_closed=True)
    except TrackerError as exc:
        raise IntakeRefused(
            RefusalReason.TRACKER_ERROR, f"the issue listing failed: {type(exc).__name__}"
        ) from exc
    still_open = []
    for view in sorted(closed, key=lambda v: v.id):
        binding = view.binding
        assert binding is not None
        issue = live.get(binding.url)
        if issue is None or issue.state.value != "open":
            continue  # closed already, or not in the listing: nothing provable
        still_open.append(_open_finding(store, view, docs[view.id]))
    return EndStateFindings(tuple(still_open), behind, listed=True)


def _open_finding(
    store: ReportStore, view: BeadView, doc: OutboxDocument | None
) -> ClosedOpenIssue:
    binding = view.binding
    assert binding is not None
    delivery = delivery_of(view.labels) or ""
    for record in sorted((doc.records if doc else {}).values(), key=_seq, reverse=True):
        close = record.op(OpKind.CLOSE)
        if close is not None and close.state in _LIVE:
            return ClosedOpenIssue(view.id, binding.url, "outbox send", delivery, record.outbox_id)
    proposal = store.read_proposal(view.id)
    unlinked = doc is not None and doc.facts.unlinked is not None
    by_triage = (
        proposal is not None
        and proposal.status is ProposalStatus.APPLIED
        and close_reason_for(proposal.disposition) is not None
    )
    tracked = delivery in {"merged", "released"} and not unlinked
    repair = "backfill-close" if by_triage or tracked else "outbox draft"
    return ClosedOpenIssue(view.id, binding.url, repair, delivery)
