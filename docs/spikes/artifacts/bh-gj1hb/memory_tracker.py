"""In-memory implementation of the bead tracker adapter port (spike bh-gj1hb).

Proposed home: ``beadhive.testing.tracker.MemoryTracker`` — the fake every consumer test (the
intake module, CLI wiring) substitutes for a real adapter, so no consumer test ever needs the
GitHub plugin package, ``httpx`` or a network. It is also the port's second implementation:
it passes the same ``conformance.CASES`` as the GitHub adapter prototype.

Deterministic: no clock (every timestamp is supplied), and a rate-limit wait calls the injected
``sleep`` (recorded, never blocking).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from tracker_contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    Actor,
    ActorKey,
    ActorKind,
    CapabilityReport,
    CapabilityState,
    Checkpoint,
    CommentKey,
    CommentQuery,
    IdPage,
    IdQuery,
    IdSubject,
    IntegrationRef,
    IssueKey,
    IssueQuery,
    IssueState,
    LabelNotOwned,
    LabelOwnership,
    NotVisible,
    Operation,
    OperationNotEnabled,
    OwnMarker,
    Page,
    PageCursor,
    ProjectRef,
    RateLimited,
    RetryBudget,
    Revision,
    TrackerComment,
    TrackerDescriptor,
    TrackerIssue,
    Tri,
    WriteGrant,
    WriteOutcomeUnknown,
    WriteReceipt,
)

PREFIX = "memory:"


@dataclass
class _Issue:
    key: IssueKey
    title: str
    state: IssueState
    labels: set[str]
    created: datetime
    updated: datetime
    change_request: bool = False


@dataclass
class _Comment:
    key: CommentKey
    issue: IssueKey
    body: str
    author: Actor
    created: datetime
    updated: datetime
    via: IntegrationRef | None = None
    marker: OwnMarker | None = None


@dataclass
class MemoryWorld:
    """Server-side state plus the test controls of ``conformance.TrackerWorld``."""

    locator: str = "acme/widgets"
    issues: dict[str, _Issue] = field(default_factory=dict)
    comments: dict[str, _Comment] = field(default_factory=dict)
    sleeps: list[float] = field(default_factory=list)
    writes: int = 0
    throttled: int = 0
    retry_after: float = 0.0
    ambiguous_next_write: bool = False
    write_time: datetime | None = None
    _ids: int = 0

    def _next(self) -> int:
        self._ids += 1
        return self._ids

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    @staticmethod
    def actor(name: str, *, bot: bool = False) -> Actor:
        kind = ActorKind.BOT if bot else ActorKind.HUMAN
        return Actor(ActorKey(f"{kind.value}-{name}", kind), display=name)

    # -- TrackerWorld -------------------------------------------------------------------------

    def add_issue(self, title, *, at, state=IssueState.OPEN, labels=()) -> IssueKey:
        n = self._next()
        key = IssueKey(f"#{n}", f"memory://{self.locator}/{n}", internal_id=str(n))
        self.issues[key.identifier] = _Issue(key, title, state, set(labels), at, at)
        return key

    def update_issue(self, key, *, at, state=None) -> None:
        issue = self.issues[key.identifier]
        issue.updated = at
        issue.state = state or issue.state

    def add_change_request(self, title, *, at) -> IssueKey:
        key = self.add_issue(title, at=at)
        self.issues[key.identifier].change_request = True
        return key

    def add_comment(self, issue, body, *, at, author="alice", integration=None) -> CommentKey:
        key = CommentKey(f"c{self._next()}")
        via = IntegrationRef(integration, integration) if integration else None
        who = self.actor(author, bot=integration is not None)
        self.comments[key.id] = _Comment(key, issue, body, who, at, at, via=via)
        return key

    def edit_comment(self, key, body, *, at) -> None:
        self.comments[key.id].body = body
        self.comments[key.id].updated = at

    def delete_comment(self, key) -> None:
        del self.comments[key.id]

    def add_human_label(self, issue, label) -> None:
        self.issues[issue.identifier].labels.add(label)

    def server_labels(self, issue) -> frozenset[str]:
        return frozenset(self.issues[issue.identifier].labels)

    def comment_count(self, issue) -> int:
        return sum(1 for c in self.comments.values() if c.issue == issue)

    def throttle(self, requests, retry_after_seconds) -> None:
        self.throttled, self.retry_after = requests, retry_after_seconds

    def fail_next_write_ambiguously(self) -> None:
        self.ambiguous_next_write = True

    def write_requests(self) -> int:
        return self.writes

    def set_write_time(self, at) -> None:
        self.write_time = at


def _stamp(moment: datetime) -> Checkpoint:
    return Checkpoint(PREFIX + moment.isoformat())


def _since(checkpoint: Checkpoint | None) -> datetime | None:
    if checkpoint is None:
        return None
    if not checkpoint.token.startswith(PREFIX):
        raise ValueError("a checkpoint from another adapter")
    return datetime.fromisoformat(checkpoint.token.removeprefix(PREFIX))


class MemoryTracker:
    """Implements ``TrackerReader``, ``TrackerIdentity``, ``CommentWriter``, ``LabelWriter``."""

    def __init__(
        self,
        world: MemoryWorld,
        *,
        enabled: frozenset[Operation] = frozenset(),
        page_size: int = 50,
        budget: RetryBudget | None = None,
        sleep: Callable[[float], None] | None = None,
        me: Actor | None = None,
    ) -> None:
        self.world = world
        self._enabled = READ_OPERATIONS | (enabled & WRITE_OPERATIONS)
        self._page_size = page_size
        self._budget = budget or RetryBudget()
        self._sleep = sleep or world.sleep
        self._me = me or MemoryWorld.actor("maintainer")
        self._used_grants: set[str] = set()

    # -- request gate: the fake's model of a remote call and its rate limit -------------------

    def _request(self) -> None:
        waited, attempt = 0.0, 0
        while self.world.throttled:
            self.world.throttled -= 1
            wait = self.world.retry_after
            budget = self._budget
            if (
                attempt >= budget.max_retries
                or wait > budget.max_single_wait.total_seconds()
                or waited + wait > budget.max_total_wait.total_seconds()
            ):
                raise RateLimited("throttled", retry_after=timedelta(seconds=wait), kind="throttle")
            self._sleep(wait)
            waited, attempt = waited + wait, attempt + 1

    def _window(self, rows: list, cursor: PageCursor | None, size: int | None):
        offset = int(cursor.token) if cursor else 0
        size = size or self._page_size
        window = rows[offset : offset + size]
        more = offset + size < len(rows)
        return window, (PageCursor(str(offset + size)) if more else None)

    # -- TrackerReader ------------------------------------------------------------------------

    def describe(self) -> TrackerDescriptor:
        return TrackerDescriptor(
            ProjectRef("memory", self.world.locator),
            api_version=1,
            supported=READ_OPERATIONS | WRITE_OPERATIONS,
        )

    def _issue(self, row: _Issue) -> TrackerIssue:
        closed = row.state is IssueState.CLOSED
        return TrackerIssue(
            key=row.key,
            title=row.title,
            body="",
            state=row.state,
            labels=frozenset(row.labels),
            author=None,
            created_at=row.created,
            revision=Revision(row.updated),
            closed_at=row.updated if closed else None,
        )

    def _issues(self, since: datetime | None, include_closed: bool) -> list[_Issue]:
        rows = [
            r
            for r in self.world.issues.values()
            if not r.change_request
            and (include_closed or r.state is IssueState.OPEN)
            and (since is None or r.updated >= since)
        ]
        return sorted(rows, key=lambda r: (r.updated, r.key.identifier))

    def list_issues(self, query: IssueQuery) -> Page[TrackerIssue]:
        self._request()
        rows = self._issues(_since(query.since), query.include_closed)
        window, nxt = self._window(rows, query.cursor, query.page_size)
        checkpoint = _stamp(window[-1].updated) if window else query.since
        return Page(tuple(self._issue(r) for r in window), nxt, checkpoint)

    def _comment(self, row: _Comment) -> TrackerComment:
        return TrackerComment(
            key=row.key,
            issue=row.issue,
            author=row.author,
            body=row.body,
            created_at=row.created,
            revision=Revision(row.updated),
            via=row.via,
            marker=row.marker,
        )

    def _comments(self, since: datetime | None, issue: IssueKey | None) -> list[_Comment]:
        rows = [
            c
            for c in self.world.comments.values()
            if (issue is None or c.issue == issue)
            and (since is None or c.updated >= since)
            and not self.world.issues[c.issue.identifier].change_request
        ]
        return sorted(rows, key=lambda c: (c.updated, c.key.id))

    def list_comments(self, query: CommentQuery) -> Page[TrackerComment]:
        self._request()
        rows = self._comments(_since(query.since), query.issue)
        window, nxt = self._window(rows, query.cursor, query.page_size)
        checkpoint = _stamp(window[-1].updated) if window else query.since
        return Page(tuple(self._comment(c) for c in window), nxt, checkpoint)

    def list_ids(self, query: IdQuery) -> IdPage:
        self._request()
        if query.subject is IdSubject.ISSUES:
            ids = [r.key.url for r in self._issues(None, include_closed=True)]
        else:
            ids = [c.key.id for c in self._comments(None, query.issue)]
        window, nxt = self._window(ids, query.cursor, query.page_size)
        return IdPage(tuple(window), nxt)

    # -- TrackerIdentity ----------------------------------------------------------------------

    def whoami(self) -> Actor | None:
        return self._me

    def probe_capabilities(self) -> CapabilityReport:
        states = {
            op: CapabilityState(op, supported=True, enabled=op in self._enabled, probe=Tri.YES)
            for op in READ_OPERATIONS | WRITE_OPERATIONS
        }
        return CapabilityReport(actor=self._me, credential_source=None, states=states)

    # -- writes -------------------------------------------------------------------------------

    def _authorise(self, op: Operation, issue: IssueKey, grant: WriteGrant) -> None:
        if op not in self._enabled:
            raise OperationNotEnabled(f"{op.value} is not enabled for this tracker")
        if grant.operation is not op or grant.target != issue:
            raise OperationNotEnabled(f"grant {grant.grant_id} does not cover {op.value}")
        if grant.grant_id in self._used_grants:
            raise OperationNotEnabled(f"grant {grant.grant_id} was already used")
        if issue.identifier not in self.world.issues:
            raise NotVisible(f"{issue.identifier} is not visible")
        self._used_grants.add(grant.grant_id)

    def _when(self) -> datetime:
        assert self.world.write_time is not None, "a test must set the server's write time"
        return self.world.write_time

    def post_comment(
        self, issue: IssueKey, body: str, *, marker: OwnMarker, grant: WriteGrant
    ) -> WriteReceipt:
        self._authorise(Operation.COMMENT_CREATE, issue, grant)
        self._request()
        self.world.writes += 1
        key = CommentKey(f"c{self.world._next()}")
        when = self._when()
        row = _Comment(key, issue, body, self._me, when, when, marker=marker)
        self.world.comments[key.id] = row
        if self.world.ambiguous_next_write:  # applied, but the caller cannot know
            self.world.ambiguous_next_write = False
            raise WriteOutcomeUnknown("the tracker did not confirm the comment")
        return WriteReceipt(grant.grant_id, grant.operation, issue, comment=self._comment(row))

    def change_labels(
        self,
        issue: IssueKey,
        *,
        add: frozenset[str],
        remove: frozenset[str],
        ownership: LabelOwnership,
        grant: WriteGrant,
    ) -> WriteReceipt:
        foreign = sorted(label for label in add | remove if not ownership.owns(label))
        if foreign:
            raise LabelNotOwned(f"labels outside the owned set: {', '.join(foreign)}")
        self._authorise(Operation.LABELS_CHANGE, issue, grant)
        self._request()
        self.world.writes += 1
        labels = self.world.issues[issue.identifier].labels
        labels.difference_update(remove)
        labels.update(add)
        return WriteReceipt(grant.grant_id, grant.operation, issue, labels_after=frozenset(labels))
