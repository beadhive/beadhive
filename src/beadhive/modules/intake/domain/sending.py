"""The records the supervised sender speaks across ``IntakePort`` (``bh-6wj9y`` R6, R11).

The outbox operations of the port (``draft_reply``, ``outbox``, ``render``, ``approve``,
``send``, ``reconcile``, ``discard``, ``retry``) take and return these intake-owned values; the
records they carry are the domain's own :class:`~.outbox.OutboxRecord` and
:class:`~.outbox.Rendered`. No tracker type, bd argv or store path appears here. Standard
library only.
"""

from __future__ import annotations

from dataclasses import dataclass

from .outbox import OpKind, OpState, OutboxRecord, Rendered


@dataclass(frozen=True)
class ReplyDraft:
    """O1 for an operator reply: ``text`` is posted (with the marker) to ``report``'s issue."""

    report: str
    text: str
    actor: str


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
    value or at least its 12-hex prefix. Only ``comment.create`` is sendable in this build."""

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


__all__ = [
    "OutboxApprove",
    "OutboxEntry",
    "OutboxListing",
    "OutboxQuery",
    "OutboxResult",
    "RenderedRecord",
    "ReplyDraft",
]
