"""The bead tracker adapter port (spike bh-gj1hb) — a runnable stand-in for the future
``src/beadhive/modules/tracker/contracts/`` package. Standard library only.

Nothing under ``src/`` imports this file. It is the CONTRACT the spike proposes: every name a
consumer (the external-intake module, prerequisite 3) may depend on, and every name an adapter
(the GitHub plugin package, a Linear or Jira adapter, the in-memory fake) must produce.

Rules the shapes encode:

* **Tracker-neutral.** No field is named after a GitHub concept (number, node id, html_url,
  login, pull request, scopes). Tracker-specific identifiers travel as opaque strings
  (``IssueKey.identifier``, ``Checkpoint.token``, ``PageCursor.token``).
* **Raw transport, not a model.** A comment is delivered as the tracker reports it *now*
  (author, body, revision, the integration that posted it, the decoded own-message marker).
  Threads, revision history, ownership classification and the outbox belong to the intake
  module; this port never stores anything.
* **Small capability interfaces** (the bd ``IssueTracker`` lesson): ``TrackerReader`` and
  ``TrackerIdentity`` are required; ``CommentWriter`` and ``LabelWriter`` are optional.
* **Writes are off unless enabled, and each call carries its own operator grant.** The adapter
  refuses a write before sending any request when the operation is not enabled, the grant names
  another operation or target, or the grant was already used.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Generic, Protocol, TypeVar, runtime_checkable

PORT_API_VERSION = 1

T = TypeVar("T")


# --------------------------------------------------------------------------------------------
# Identity of things
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class ProjectRef:
    """The one external project an adapter instance is bound to.

    ``tracker`` is a value (``"github"``, ``"jira"``, ``"linear"``, ``"memory"``), not a field
    per tracker; ``locator`` is the tracker's own project address (``owner/repo``, a project key,
    a team key), opaque to consumers."""

    tracker: str
    locator: str


@dataclass(frozen=True)
class IssueKey:
    """A work item's identity. Equality is ``(identifier, url)``.

    ``url`` is the canonical web URL: the intake module's join key (``external_ref``, operator
    decision D1) and the bd projection's ``external_ref``. ``identifier`` is the tracker's human
    reference (``#12``, ``PROJ-12``, ``ENG-12``) and is what an adapter routes writes by.
    ``internal_id`` / ``global_id`` are optional because some listings name the parent issue
    only by URL (GitHub's repository-wide comment list does)."""

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
    """Stable principal identity: the tracker's immutable id plus its kind (never a login)."""

    id: str
    kind: ActorKind


@dataclass(frozen=True)
class Actor:
    key: ActorKey
    display: str = field(default="", compare=False)  # a mutable handle; display only


@dataclass(frozen=True)
class IntegrationRef:
    """The app/integration a tracker says performed an action, when it records one."""

    id: str
    display: str = field(default="", compare=False)


@dataclass(frozen=True)
class OwnMarker:
    """The attribution an adapter embeds in every comment it posts, decoded on read.

    The encoding is adapter-owned (a trailing HTML comment on GitHub, a comment property on
    Jira, ...). The port only promises the round trip. It is a loop-avoidance aid, not a
    security boundary (spike bh-mqvdq E4)."""

    hive: str
    install: str
    outbox_id: str


# --------------------------------------------------------------------------------------------
# Raw records
# --------------------------------------------------------------------------------------------


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


# --------------------------------------------------------------------------------------------
# Reading: queries, pages, checkpoints, full id snapshots
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Checkpoint:
    """Opaque high-water mark. Consumers persist ``token`` and never parse it.

    Contract: an item whose revision is newer than the checkpoint is returned by a later
    ``since=`` query. The filter is inclusive, so the boundary item may repeat (consumers upsert
    on ``(key, revision)``); it never silently skips."""

    token: str


@dataclass(frozen=True)
class PageCursor:
    token: str  # opaque; for GitHub it is the server's verbatim ``rel="next"`` URL


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
    """One page. ``checkpoint`` is safe to persist once this page and every earlier page of
    the same pull are durably ingested. An adapter that can order by revision (oldest first)
    returns it on every page; one that cannot returns ``None`` until the final page."""

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


# --------------------------------------------------------------------------------------------
# Capabilities, identity, write grants
# --------------------------------------------------------------------------------------------


class Operation(StrEnum):
    ISSUES_READ = "issues.read"
    COMMENTS_READ = "comments.read"
    COMMENT_CREATE = "comment.create"
    LABELS_CHANGE = "labels.change"


READ_OPERATIONS = frozenset({Operation.ISSUES_READ, Operation.COMMENTS_READ})
WRITE_OPERATIONS = frozenset({Operation.COMMENT_CREATE, Operation.LABELS_CHANGE})


class Tri(StrEnum):
    """Read-only evidence about a capability (spike bh-mqvdq E3): YES means "not refuted"."""

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
    credential_source: str | None  # provenance such as "env:GITHUB_TOKEN"; never a value
    states: Mapping[Operation, CapabilityState]


@dataclass(frozen=True)
class TrackerDescriptor:
    project: ProjectRef
    api_version: int
    supported: frozenset[Operation]


@dataclass(frozen=True)
class WriteGrant:
    """One operator-approved write. Issued by the gate (the intake outbox), checked here."""

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


# --------------------------------------------------------------------------------------------
# Errors (every message already scrubbed of credentials)
# --------------------------------------------------------------------------------------------


class TrackerError(Exception):
    retryable = False


class AuthenticationFailed(TrackerError):
    """The credential is invalid, revoked or expired."""


class PermissionDenied(TrackerError):
    def __init__(self, message: str, *, operation: Operation | None, needed: str | None = None):
        super().__init__(message)
        self.operation = operation
        self.needed = needed  # the tracker's own name for the missing grant, when it says


class NotVisible(TrackerError):
    """Missing, or hidden from this credential (trackers often report forbidden as missing)."""


class TargetStateRefused(TrackerError):
    """Archived project, locked thread, comments disabled: a state problem, not a credential."""


class PayloadRejected(TrackerError):
    """Authorised, but the content was refused (too long, invalid label): the write is permitted."""


class RateLimited(TrackerError):
    retryable = True

    def __init__(self, message: str, *, retry_after: timedelta | None, kind: str):
        super().__init__(message)
        self.retry_after = retry_after
        self.kind = kind  # "quota" (a per-window allowance) or "throttle" (a burst limit)


class TransientFailure(TrackerError):
    retryable = True


class WriteOutcomeUnknown(TrackerError):
    """A write may or may not have happened (timeout, 5xx). Never retried by the adapter; the
    outbox reconciles by listing comments and matching ``OwnMarker.outbox_id``."""


class OperationNotEnabled(TrackerError):
    """Refused before any request: not enabled, wrong grant, reused grant, unsupported."""


class LabelNotOwned(OperationNotEnabled):
    """Refused before any request: a label outside ``LabelOwnership``."""


# --------------------------------------------------------------------------------------------
# The port
# --------------------------------------------------------------------------------------------


@runtime_checkable
class TrackerReader(Protocol):
    def describe(self) -> TrackerDescriptor: ...

    def list_issues(self, query: IssueQuery) -> Page[TrackerIssue]: ...

    def list_comments(self, query: CommentQuery) -> Page[TrackerComment]: ...

    def list_ids(self, query: IdQuery) -> IdPage: ...


@runtime_checkable
class TrackerIdentity(Protocol):
    def whoami(self) -> Actor | None: ...

    def probe_capabilities(self) -> CapabilityReport: ...  # read-only requests only


@runtime_checkable
class TrackerAdapter(TrackerReader, TrackerIdentity, Protocol):
    """The bead tracker adapter port: the required part every adapter implements. Writers are
    discovered separately (``isinstance(adapter, CommentWriter)``) and still need ``enabled``."""


@runtime_checkable
class CommentWriter(Protocol):
    def post_comment(
        self, issue: IssueKey, body: str, *, marker: OwnMarker, grant: WriteGrant
    ) -> WriteReceipt: ...


@runtime_checkable
class LabelWriter(Protocol):
    def change_labels(
        self,
        issue: IssueKey,
        *,
        add: frozenset[str],
        remove: frozenset[str],
        ownership: LabelOwnership,
        grant: WriteGrant,
    ) -> WriteReceipt: ...


# --------------------------------------------------------------------------------------------
# Tracker-neutral helpers (home: modules/tracker/application, not the adapter)
# --------------------------------------------------------------------------------------------


def iterate(fetch: Callable[[PageCursor | None], Page[T]]) -> Iterator[Page[T]]:
    """Follow ``next_cursor`` until the last page."""
    cursor: PageCursor | None = None
    while True:
        page = fetch(cursor)
        yield page
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def snapshot_ids(reader: TrackerReader, query: IdQuery) -> IdSnapshot:
    """Collect a full id set. Any error leaves ``complete=False``: never infer a deletion."""
    ids: set[str] = set()
    cursor = query.cursor
    try:
        while True:
            page = reader.list_ids(
                IdQuery(query.subject, query.issue, cursor=cursor, page_size=query.page_size)
            )
            ids.update(page.ids)
            if page.next_cursor is None:
                return IdSnapshot(frozenset(ids), complete=True)
            cursor = page.next_cursor
    except TrackerError:
        return IdSnapshot(frozenset(ids), complete=False)


__all__ = [
    "PORT_API_VERSION",
    "READ_OPERATIONS",
    "WRITE_OPERATIONS",
    "Actor",
    "ActorKey",
    "ActorKind",
    "AuthenticationFailed",
    "CapabilityReport",
    "CapabilityState",
    "Checkpoint",
    "CommentKey",
    "CommentQuery",
    "CommentWriter",
    "IdPage",
    "IdQuery",
    "IdSnapshot",
    "IdSubject",
    "IntegrationRef",
    "IssueKey",
    "IssueQuery",
    "IssueState",
    "LabelNotOwned",
    "LabelOwnership",
    "LabelWriter",
    "NotVisible",
    "Operation",
    "OperationNotEnabled",
    "OwnMarker",
    "Page",
    "PageCursor",
    "PayloadRejected",
    "PermissionDenied",
    "ProjectRef",
    "RateBudget",
    "RateLimited",
    "RetryBudget",
    "Revision",
    "TargetStateRefused",
    "TrackerAdapter",
    "TrackerComment",
    "TrackerDescriptor",
    "TrackerError",
    "TrackerIdentity",
    "TrackerIssue",
    "TrackerReader",
    "TransientFailure",
    "Tri",
    "WriteGrant",
    "WriteOutcomeUnknown",
    "WriteReceipt",
    "iterate",
    "snapshot_ids",
]
