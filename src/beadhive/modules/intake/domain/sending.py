"""The records the supervised sender speaks across ``IntakePort`` (``bh-6wj9y`` R6, R11).

The outbox operations of the port (``draft_reply``, ``outbox``, ``render``, ``approve``,
``send``, ``reconcile``, ``discard``, ``retry``) take and return these intake-owned values; the
records they carry are the domain's own :class:`~.outbox.OutboxRecord` and
:class:`~.outbox.Rendered`. No tracker type, bd argv or store path appears here. Standard
library only.
"""

from __future__ import annotations

from dataclasses import dataclass

from .outbox import CloseReason, OpKind, OpState, OutboxRecord, Rendered


@dataclass(frozen=True)
class ReplyDraft:
    """O1 for an operator reply: ``text`` is posted (with the marker) to ``report``'s issue.
    ``close`` asks for an ``issue.close`` operation on the same record, sent after the comment;
    it is drafted only while the hive's close gate is on (``OutboxPolicy.close_enabled``)."""

    report: str
    text: str
    actor: str
    close: CloseReason | None = None


@dataclass(frozen=True)
class OutboxQuery:
    """Which records ``outbox`` lists: one report's, or every report's that has an outbox.
    ``open_only`` keeps the records with a non-terminal operation (the communication backlog)."""

    report: str = ""
    open_only: bool = False


@dataclass(frozen=True)
class OutboxEntry:
    report: str
    record: OutboxRecord


@dataclass(frozen=True)
class OutboxListing:
    entries: tuple[OutboxEntry, ...] = ()


@dataclass(frozen=True)
class RenderedRecord:
    """The dry run of a record: every operation rendered exactly as it would be sent."""

    report: str
    record: OutboxRecord
    renders: tuple[Rendered, ...]

    def of(self, kind: OpKind) -> Rendered | None:
        return next((r for r in self.renders if r.kind is kind), None)


@dataclass(frozen=True)
class OutboxApprove:
    """O5 (with O4 for each ``publish``): the operator approves the digest they reviewed, the full
    value or at least its 12-hex prefix. ``kind`` selects the operation: ``comment.create`` (the
    default), ``labels.change`` or ``issue.close``; each is its own approval and mints its own
    grant."""

    outbox_id: str
    digest: str
    actor: str
    publish: tuple[str, ...] = ()
    kind: OpKind = OpKind.COMMENT


@dataclass(frozen=True)
class OutboxResult:
    """What one outbox operation did to one record.

    ``state`` is the operation's state afterwards; ``rules`` the transitions applied, in order
    (``O13`` recoveries included). ``rendered`` and ``grant_id`` are an approval's dry run and the
    single-use grant it minted; ``comment_id`` is a receipt's remote comment; ``notified`` the
    ``notified`` value written for a sent comment; ``complete`` whether a reconcile listing was
    complete (only a complete one can count an absence); ``detail`` says what to do next."""

    report: str
    outbox_id: str
    kind: OpKind = OpKind.COMMENT
    state: OpState | None = None
    rules: tuple[str, ...] = ()
    rendered: Rendered | None = None
    grant_id: str = ""
    comment_id: str = ""
    notified: str = ""
    complete: bool = True
    detail: str = ""


@dataclass(frozen=True)
class CloseBackfill:
    """Draft the missing closes (``bh-pr5ll``): for every report already closed in the store
    whose bound issue is still open and whose applied triage outcome ends the thread, an
    ``issue.close`` on its latest reply record. ``dry_run`` reads and plans and writes nothing."""

    actor: str
    dry_run: bool = False


@dataclass(frozen=True)
class CloseBackfillItem:
    """One candidate report: the reply record that carries (or would carry) the close and its
    reason, or why it was skipped (``detail``). ``drafted`` is set only when a close was written.
    ``reply_text`` is set when the report never got its reply and the backfill drafts (or, on a
    dry run, would draft) that reply too, from the applied proposal."""

    report: str
    outbox_id: str = ""
    reason: CloseReason | None = None
    drafted: bool = False
    detail: str = ""
    reply_text: str = ""

    @property
    def skipped(self) -> bool:
        return bool(self.detail)


@dataclass(frozen=True)
class CloseBackfillOutcome:
    items: tuple[CloseBackfillItem, ...] = ()
    dry_run: bool = False

    @property
    def drafted(self) -> tuple[CloseBackfillItem, ...]:
        return tuple(i for i in self.items if i.drafted)

    @property
    def planned(self) -> tuple[CloseBackfillItem, ...]:
        return tuple(i for i in self.items if not i.skipped and not i.drafted)

    @property
    def skipped(self) -> tuple[CloseBackfillItem, ...]:
        return tuple(i for i in self.items if i.skipped)


__all__ = [
    "CloseBackfill",
    "CloseBackfillItem",
    "CloseBackfillOutcome",
    "OutboxApprove",
    "OutboxEntry",
    "OutboxListing",
    "OutboxQuery",
    "OutboxResult",
    "RenderedRecord",
    "ReplyDraft",
]
