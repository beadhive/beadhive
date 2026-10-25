"""Value records of the bead tracker adapter port. Standard library only.

Rules the shapes encode (tracker-adapter-port-adr.md section 3):

* **Tracker-neutral.** No field is named after one tracker's concept. Tracker-specific values
  travel as opaque strings (``IssueKey.identifier``, ``Checkpoint.token``, ``PageCursor.token``).
* **Raw transport, not a model.** A comment is delivered as the tracker reports it now (author,
  body, revision, the integration that posted it, the decoded own-message marker). Threads,
  revision history, ownership classification and the outbox belong to the consumer.
* **Writes are off unless enabled**, and every write call carries its own single-use
  :class:`WriteGrant` issued by an operator-approved gate.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Generic, TypeVar

PORT_API_VERSION = 1

T = TypeVar("T")


# -- identity of things ---------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectRef:
    """The one external project an adapter instance is bound to.

    ``tracker`` is a value (``"memory"``, a product name), not a field per tracker; ``locator``
    is the tracker's own project address, opaque to consumers."""

    tracker: str
    locator: str


@dataclass(frozen=True)
class IssueKey:
    """A work item's identity. Equality is ``(identifier, url)``.

    ``url`` is the canonical web URL and the consumers' join key. ``identifier`` is the tracker's
    human reference (``#12``, ``PROJ-12``) and is what an adapter routes writes by.
    ``internal_id`` and ``global_id`` are optional because some listings name the parent issue
    only by URL."""

    identifier: str
    url: str
    internal_id: str | None = field(default=None, compare=False)
    global_id: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class CommentKey:
    """A comment's identity: the tracker's id for it, plus an optional global id."""

    id: str
    global_id: str | None = field(default=None, compare=False)


class ActorKind(StrEnum):
    HUMAN = "human"
    BOT = "bot"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class ActorKey:
    """Stable principal identity: the tracker's immutable id plus its kind (never a handle)."""

    id: str
    kind: ActorKind


@dataclass(frozen=True)
class Actor:
    key: ActorKey
    display: str = field(default="", compare=False)  # a mutable handle; display only


@dataclass(frozen=True)
class IntegrationRef:
    """The integration a tracker says performed an action, when it records one."""

    id: str
    display: str = field(default="", compare=False)


@dataclass(frozen=True)
class OwnMarker:
    """The attribution an adapter embeds in every comment it posts, decoded on read.

    The encoding is adapter-owned; the port only promises the round trip. It is a loop-avoidance
    and attribution aid, not a security boundary. ``outbox_id`` lets a comment whose write
    outcome was unknown be found again."""

    hive: str
    install: str
    outbox_id: str


# -- raw records ----------------------------------------------------------------------------


class IssueState(StrEnum):
    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True, order=True)
class Revision:
    """Monotone per item. ``updated_at`` is the TRACKER's timestamp, never the local clock."""

    updated_at: datetime
    version: str | None = field(default=None, compare=False)


@dataclass(frozen=True)
class TrackerIssue:
    key: IssueKey
    title: str
    body: str
    state: IssueState
    labels: frozenset[str]
    author: Actor | None
    created_at: datetime
    revision: Revision
    closed_at: datetime | None = None
    closed_reason: str | None = None  # tracker vocabulary, passed through verbatim


@dataclass(frozen=True)
class TrackerComment:
    key: CommentKey
    issue: IssueKey
    author: Actor | None
    body: str  # with the adapter's own marker encoding removed
    created_at: datetime
    revision: Revision
    via: IntegrationRef | None = None
    marker: OwnMarker | None = None

    @property
    def edited(self) -> bool:
        return self.revision.updated_at > self.created_at


# -- reading: queries, pages, checkpoints, full id snapshots --------------------------------


@dataclass(frozen=True)
class Checkpoint:
    """Opaque high-water mark. Consumers persist ``token`` and never parse it.

    An item whose revision is newer than the checkpoint is returned by a later ``since=`` query.
    The filter is inclusive, so the boundary item may repeat (consumers upsert on
    ``(key, revision)``); it never silently skips. It is the tracker's own mark, never the
    local clock."""

    token: str


@dataclass(frozen=True)
class PageCursor:
    token: str  # opaque; whatever the adapter needs to fetch the next page


@dataclass(frozen=True)
class IssueQuery:
    since: Checkpoint | None = None
    cursor: PageCursor | None = None
    include_closed: bool = True  # closed work is in scope by default
    page_size: int | None = None


@dataclass(frozen=True)
class CommentQuery:
    since: Checkpoint | None = None
    cursor: PageCursor | None = None
    issue: IssueKey | None = None  # None: every comment in the project
    page_size: int | None = None


class IdSubject(StrEnum):
    ISSUES = "issues"
    COMMENTS = "comments"


@dataclass(frozen=True)
class IdQuery:
    """A full id-set listing, used to find deletions (a ``since`` filter cannot report them)."""

    subject: IdSubject
    issue: IssueKey | None = None
    cursor: PageCursor | None = None
    page_size: int | None = None


@dataclass(frozen=True)
class RateBudget:
    remaining: int | None
    resets_at: datetime | None


@dataclass(frozen=True)
class Page(Generic[T]):
    """One page. ``checkpoint`` is safe to persist once this page and every earlier page of the
    same pull are durably ingested. An adapter that can order by revision (oldest first) returns
    it on every page; one that cannot returns ``None`` until the final page."""

    items: tuple[T, ...]
    next_cursor: PageCursor | None
    checkpoint: Checkpoint | None
    budget: RateBudget | None = None


@dataclass(frozen=True)
class IdPage:
    """Issue ids are ``IssueKey.url`` (the join key); comment ids are ``CommentKey.id``."""

    ids: tuple[str, ...]
    next_cursor: PageCursor | None


@dataclass(frozen=True)
class IdSnapshot:
    """A full id set. Only a ``complete`` snapshot may be used to infer deletions."""

    ids: frozenset[str]
    complete: bool


# -- capabilities, identity, write grants ---------------------------------------------------


class Operation(StrEnum):
    ISSUES_READ = "issues.read"
    COMMENTS_READ = "comments.read"
    COMMENT_CREATE = "comment.create"
    LABELS_CHANGE = "labels.change"


READ_OPERATIONS = frozenset({Operation.ISSUES_READ, Operation.COMMENTS_READ})
WRITE_OPERATIONS = frozenset({Operation.COMMENT_CREATE, Operation.LABELS_CHANGE})


class Tri(StrEnum):
    """Read-only evidence about a capability: ``YES`` means only "not refuted", ``NO`` is
    definitive, ``UNKNOWN`` means a read could not tell (the write itself becomes the probe)."""

    YES = "yes"
    NO = "no"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class CapabilityState:
    operation: Operation
    supported: bool  # the adapter implements it
    enabled: bool  # the operator turned it on; False by default for every write
    probe: Tri  # what a read-only probe could establish
    reason: str = ""  # one scrubbed line

    @property
    def usable(self) -> bool:
        return self.supported and self.enabled and self.probe is not Tri.NO


@dataclass(frozen=True)
class CapabilityReport:
    actor: Actor | None  # None: anonymous
    credential_source: str | None  # provenance such as "env:NAME"; never a value
    states: Mapping[Operation, CapabilityState]


@dataclass(frozen=True)
class TrackerDescriptor:
    project: ProjectRef
    api_version: int
    supported: frozenset[Operation]


@dataclass(frozen=True)
class WriteGrant:
    """One operator-approved write, scoped to one operation and one target, usable once."""

    grant_id: str
    operation: Operation
    target: IssueKey


@dataclass(frozen=True)
class LabelOwnership:
    """The labels Beadhive owns on this tracker; nothing outside them is ever touched."""

    prefixes: tuple[str, ...]

    def owns(self, label: str) -> bool:
        return any(label.startswith(prefix) for prefix in self.prefixes)


@dataclass(frozen=True)
class WriteReceipt:
    grant_id: str
    operation: Operation
    target: IssueKey
    comment: TrackerComment | None = None
    labels_after: frozenset[str] | None = None


@dataclass(frozen=True)
class RetryBudget:
    """Adapter-side retry bounds (configurable); a waiting adapter never exceeds them."""

    max_retries: int = 3
    max_single_wait: timedelta = timedelta(seconds=60)
    max_total_wait: timedelta = timedelta(seconds=120)


__all__ = [
    "PORT_API_VERSION",
    "READ_OPERATIONS",
    "WRITE_OPERATIONS",
    "Actor",
    "ActorKey",
    "ActorKind",
    "CapabilityReport",
    "CapabilityState",
    "Checkpoint",
    "CommentKey",
    "CommentQuery",
    "IdPage",
    "IdQuery",
    "IdSnapshot",
    "IdSubject",
    "IntegrationRef",
    "IssueKey",
    "IssueQuery",
    "IssueState",
    "LabelOwnership",
    "Operation",
    "OwnMarker",
    "Page",
    "PageCursor",
    "ProjectRef",
    "RateBudget",
    "RetryBudget",
    "Revision",
    "TrackerComment",
    "TrackerDescriptor",
    "TrackerIssue",
    "Tri",
    "WriteGrant",
    "WriteReceipt",
]
