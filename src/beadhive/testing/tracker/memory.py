"""In-memory implementation of the bead tracker adapter port.

:class:`MemoryTracker` is the substitute every consumer test uses instead of a real adapter, so
no consumer test needs a plugin package, an HTTP client or a network. It is also the port's
second implementation: it passes the same :data:`~beadhive.testing.tracker.cases.TRACKER_CASES`
a real adapter must pass. It implements the required :class:`TrackerAdapter` plus every optional
Protocol (``CommentWriter``, ``LabelWriter``, ``LabelReader``, ``LabelDefiner``,
``LabelPruner``, ``IssueCloser``, ``IssueResolver``, ``MarkerCodec``);
:func:`without_resolver` hides the resolver to exercise a consumer's fallback.

Deterministic: no clock (every timestamp is supplied by the world), and a rate-limit wait calls
the injected ``sleep`` (recorded by default, never blocking).
"""

from __future__ import annotations

import re
from collections.abc import Callable
from datetime import datetime, timedelta

from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    Actor,
    CapabilityReport,
    CapabilityState,
    ChangeRequestUrl,
    Checkpoint,
    CloseReason,
    CommentKey,
    CommentQuery,
    IdPage,
    IdQuery,
    IdSubject,
    IssueKey,
    IssueQuery,
    IssueState,
    LabelDefinition,
    LabelNotOwned,
    LabelOwnership,
    NotVisible,
    Operation,
    OperationNotEnabled,
    OtherProject,
    OwnMarker,
    Page,
    PageCursor,
    ProjectRef,
    RateLimited,
    RetryBudget,
    Revision,
    TrackerAdapter,
    TrackerComment,
    TrackerDescriptor,
    TrackerIssue,
    Tri,
    UrlNotRecognized,
    WriteGrant,
    WriteOutcomeUnknown,
    WriteReceipt,
)

from .world import MemoryWorld, _Comment, _Issue

_CHECKPOINT_PREFIX = "memory:"
_URL = re.compile(
    r"memory://(?P<locator>[^?#]+?)/(?P<kind>issues|changes)/(?P<number>[0-9]+)/?(?:[?#].*)?",
    re.DOTALL,
)


def _stamp(moment: datetime) -> Checkpoint:
    return Checkpoint(_CHECKPOINT_PREFIX + moment.isoformat())


def _since(checkpoint: Checkpoint | None) -> datetime | None:
    if checkpoint is None:
        return None
    if not checkpoint.token.startswith(_CHECKPOINT_PREFIX):
        raise ValueError("a checkpoint from another adapter")
    return datetime.fromisoformat(checkpoint.token.removeprefix(_CHECKPOINT_PREFIX))


#: The footer line :meth:`MemoryTracker.marker_line` renders: the same shape as the first-party
#: GitHub adapter's ``bh:v1`` line, so a consumer test sees realistic posted bytes.
_MARKER_FORMAT = "<!-- bh:v1 hive={hive} install={install} outbox={outbox} -->"


#: The label namespace the adapter owns when it defines repository labels (it takes no
#: ``LabelOwnership`` per call, unlike ``change_labels``).
_DEFAULT_OWNERSHIP = LabelOwnership(prefixes=("bh:",))


class MemoryTracker:
    """Implements ``TrackerAdapter``, ``CommentWriter``, ``LabelWriter``, ``LabelReader``,
    ``LabelDefiner``, ``LabelPruner``, ``IssueCloser``, ``IssueResolver`` and ``MarkerCodec``.

    ``enabled`` is the resolved set of enabled operations, as a composition root would hand it to
    a real adapter. The default enables the three reads and no write. A disabled operation is
    refused with ``OperationNotEnabled`` before any request. ``ownership`` is the label namespace
    ``define_labels`` may touch (default ``bh:``).
    """

    def __init__(
        self,
        world: MemoryWorld,
        *,
        enabled: frozenset[Operation] = READ_OPERATIONS,
        page_size: int = 50,
        budget: RetryBudget | None = None,
        sleep: Callable[[float], None] | None = None,
        me: Actor | None = None,
        ownership: LabelOwnership = _DEFAULT_OWNERSHIP,
    ) -> None:
        self.world = world
        self._enabled = frozenset(enabled)
        self._page_size = page_size
        self._budget = budget or RetryBudget()
        self._sleep = sleep or world.sleep
        self._me = me or MemoryWorld.actor("maintainer")
        self._ownership = ownership
        self._used_grants: set[str] = set()

    # -- request gate: the fake's model of a remote call and its rate limit -------------------

    def _require(self, operation: Operation) -> None:
        if operation not in self._enabled:
            raise OperationNotEnabled(f"{operation.value} is not enabled for this tracker")

    def _request(self, *, write: bool = False) -> None:
        if not write:
            self.world.reads += 1
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

    def _window(
        self, rows: list, cursor: PageCursor | None, size: int | None
    ) -> tuple[list, PageCursor | None]:
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
            closed_reason=row.closed_reason if closed else None,
        )

    def _issues(self, since: datetime | None, include_closed: bool) -> list[_Issue]:
        rows = [
            r
            for r in self.world.issues.values()
            if not r.change_request
            and not r.transferred
            and (include_closed or r.state is IssueState.OPEN)
            and (since is None or r.updated >= since)
        ]
        return sorted(rows, key=lambda r: (r.updated, r.key.identifier))

    def list_issues(self, query: IssueQuery) -> Page[TrackerIssue]:
        self._require(Operation.ISSUES_READ)
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
        self._require(Operation.COMMENTS_READ)
        self._request()
        rows = self._comments(_since(query.since), query.issue)
        window, nxt = self._window(rows, query.cursor, query.page_size)
        checkpoint = _stamp(window[-1].updated) if window else query.since
        return Page(tuple(self._comment(c) for c in window), nxt, checkpoint)

    def list_ids(self, query: IdQuery) -> IdPage:
        if query.subject is IdSubject.ISSUES:
            self._require(Operation.ISSUES_READ)
        else:
            self._require(Operation.COMMENTS_READ)
        self._request()
        if query.subject is IdSubject.ISSUES:
            ids = [r.key.url for r in self._issues(None, include_closed=True)]
        else:
            ids = [c.key.id for c in self._comments(None, query.issue)]
        window, nxt = self._window(ids, query.cursor, query.page_size)
        return IdPage(tuple(window), nxt)

    # -- LabelReader --------------------------------------------------------------------------

    def list_label_definitions(self) -> tuple[LabelDefinition, ...]:
        """One read: every label definition of the repository, owned or not, ordered by name."""
        self._require(Operation.LABELS_READ)
        self._request()
        return self.world.label_definitions()

    # -- IssueResolver (amendment A1) ---------------------------------------------------------

    def resolve(self, url: str) -> IssueKey:
        """Pure: canonicalise ``url`` without a request. The URL is never echoed in a refusal."""
        match = _URL.fullmatch(url.strip())
        if match is None:
            raise UrlNotRecognized("not an issue URL of the memory tracker")
        if match["locator"] != self.world.locator:
            raise OtherProject("the URL names an issue of another project")
        if match["kind"] == "changes":
            raise ChangeRequestUrl("the URL names a change request, not an issue")
        number = match["number"]
        return IssueKey(f"#{number}", self.world.issue_url(int(number)), internal_id=number)

    def get(self, key: IssueKey) -> TrackerIssue:
        """One read: the issue ``key`` names, closed or open."""
        self._require(Operation.ISSUES_READ)
        self._request()
        row = self.world.issues.get(key.identifier)
        if row is None:
            raise NotVisible(f"{key.identifier} is not visible")
        if row.change_request:
            raise ChangeRequestUrl(f"{key.identifier} is a change request, not an issue")
        if row.key != key and not row.transferred:
            raise NotVisible(f"{key.identifier} is not visible")
        return self._issue(row)

    # -- TrackerIdentity ----------------------------------------------------------------------

    def whoami(self) -> Actor | None:
        return self._me

    def probe_capabilities(self) -> CapabilityReport:
        states = {
            op: CapabilityState(op, supported=True, enabled=op in self._enabled, probe=Tri.YES)
            for op in READ_OPERATIONS | WRITE_OPERATIONS
        }
        return CapabilityReport(actor=self._me, credential_source=None, states=states)

    # -- MarkerCodec (pure) -------------------------------------------------------------------

    def marker_line(self, marker: OwnMarker) -> str:
        """The footer line this fake "appends" (it stores the marker structurally instead)."""
        for value in (marker.hive, marker.install, marker.outbox_id):
            if not value or any(c.isspace() for c in value) or "--" in value:
                raise ValueError("marker fields must be non-empty tokens without spaces or '--'")
        return _MARKER_FORMAT.format(
            hive=marker.hive, install=marker.install, outbox=marker.outbox_id
        )

    # -- writes -------------------------------------------------------------------------------

    def _authorise(self, op: Operation, target: IssueKey | ProjectRef, grant: WriteGrant) -> None:
        self._require(op)
        if grant.operation is not op or grant.target != target:
            raise OperationNotEnabled(f"grant {grant.grant_id} does not cover {op.value}")
        if grant.grant_id in self._used_grants:
            raise OperationNotEnabled(f"grant {grant.grant_id} was already used")
        if isinstance(target, IssueKey) and target.identifier not in self.world.issues:
            raise NotVisible(f"{target.identifier} is not visible")
        self._used_grants.add(grant.grant_id)

    def _when(self) -> datetime:
        if self.world.write_time is None:
            raise RuntimeError("a test must set the server's write time (set_write_time)")
        return self.world.write_time

    def post_comment(
        self, issue: IssueKey, body: str, *, marker: OwnMarker, grant: WriteGrant
    ) -> WriteReceipt:
        self._authorise(Operation.COMMENT_CREATE, issue, grant)
        self._request(write=True)
        self.world.writes += 1
        ambiguous, self.world.ambiguous_next_write = self.world.ambiguous_next_write, None
        if ambiguous is False:  # cut off before the tracker stored anything (K1)
            raise WriteOutcomeUnknown("the tracker did not confirm the comment")
        key = CommentKey(f"c{self.world._next()}")
        when = self._when()
        row = _Comment(key, issue, body, self._me, when, when, marker=marker)
        self.world.comments[key.id] = row
        if ambiguous:  # applied, but the caller cannot know
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
        self._request(write=True)
        self.world.writes += 1
        ambiguous, self.world.ambiguous_next_write = self.world.ambiguous_next_write, None
        if ambiguous is False:  # cut off before the tracker applied anything (K1)
            raise WriteOutcomeUnknown("the tracker did not confirm the label change")
        labels = self.world.issues[issue.identifier].labels
        labels.difference_update(remove)
        labels.update(add)
        if ambiguous:  # applied, but the caller cannot know
            raise WriteOutcomeUnknown("the tracker did not confirm the label change")
        return WriteReceipt(grant.grant_id, grant.operation, issue, labels_after=frozenset(labels))

    def close_issue(
        self, issue: IssueKey, *, reason: CloseReason, grant: WriteGrant
    ) -> WriteReceipt:
        """Close ``issue`` with ``reason`` under an ``issue.close`` grant. Only the state changes
        (never a label); an issue already closed is closed again with the new reason."""
        if not isinstance(reason, CloseReason):
            raise ValueError("a close needs a CloseReason")
        self._authorise(Operation.ISSUE_CLOSE, issue, grant)
        self._request(write=True)
        self.world.writes += 1
        ambiguous, self.world.ambiguous_next_write = self.world.ambiguous_next_write, None
        if ambiguous is False:  # cut off before the tracker applied anything (K1)
            raise WriteOutcomeUnknown("the tracker did not confirm the close")
        row = self.world.issues[issue.identifier]
        row.state, row.closed_reason, row.updated = IssueState.CLOSED, reason.value, self._when()
        if ambiguous:  # applied, but the caller cannot know
            raise WriteOutcomeUnknown("the tracker did not confirm the close")
        return WriteReceipt(
            grant.grant_id,
            grant.operation,
            issue,
            state_after=IssueState.CLOSED,
            closed_reason=reason.value,
        )

    def define_labels(
        self, definitions: tuple[LabelDefinition, ...], *, grant: WriteGrant
    ) -> WriteReceipt:
        """Create or update repository label definitions under a ``ProjectRef`` grant.

        Owned names only, checked for the whole batch before anything is written. Idempotent: a
        definition the repository already holds identically is a no-op, and a batch that changes
        nothing sends no write. An existing owned name with another color or description is
        updated in place."""
        foreign = sorted(d.name for d in definitions if not self._ownership.owns(d.name))
        if foreign:
            raise LabelNotOwned(f"labels outside the owned set: {', '.join(foreign)}")
        project = self.describe().project
        self._authorise(Operation.LABELS_DEFINE, project, grant)
        self._request(write=True)
        changed = [d for d in definitions if self.world.label_defs.get(d.name) != d]
        if changed:
            self.world.writes += 1
            ambiguous, self.world.ambiguous_next_write = self.world.ambiguous_next_write, None
            if ambiguous is False:  # cut off before the tracker applied anything (K1)
                raise WriteOutcomeUnknown("the tracker did not confirm the label definitions")
            for definition in changed:
                self.world.label_defs[definition.name] = definition
            if ambiguous:  # applied, but the caller cannot know
                raise WriteOutcomeUnknown("the tracker did not confirm the label definitions")
        return WriteReceipt(grant.grant_id, grant.operation, project)

    def delete_labels(self, names: tuple[str, ...], *, grant: WriteGrant) -> WriteReceipt:
        """Delete repository label definitions under a ``labels.define`` ``ProjectRef`` grant.

        Owned names only, checked for the whole batch before anything is written. As on GitHub,
        deleting a definition also takes the label off every issue carrying it. A name the
        repository does not hold is already deleted; a batch that deletes nothing sends no
        write."""
        foreign = sorted(name for name in names if not self._ownership.owns(name))
        if foreign:
            raise LabelNotOwned(f"labels outside the owned set: {', '.join(foreign)}")
        project = self.describe().project
        self._authorise(Operation.LABELS_DEFINE, project, grant)
        self._request(write=True)
        gone = [name for name in names if name in self.world.label_defs]
        if gone:
            self.world.writes += 1
            for name in gone:
                del self.world.label_defs[name]
                for issue in self.world.issues.values():
                    issue.labels.discard(name)
        return WriteReceipt(grant.grant_id, grant.operation, project)


class _WithoutResolver:
    """Delegates every port operation except the optional ``IssueResolver`` pair."""

    def __init__(self, inner: MemoryTracker) -> None:
        self._inner = inner

    @property
    def world(self) -> MemoryWorld:
        return self._inner.world

    def describe(self) -> TrackerDescriptor:
        return self._inner.describe()

    def list_issues(self, query: IssueQuery) -> Page[TrackerIssue]:
        return self._inner.list_issues(query)

    def list_comments(self, query: CommentQuery) -> Page[TrackerComment]:
        return self._inner.list_comments(query)

    def list_ids(self, query: IdQuery) -> IdPage:
        return self._inner.list_ids(query)

    def whoami(self) -> Actor | None:
        return self._inner.whoami()

    def probe_capabilities(self) -> CapabilityReport:
        return self._inner.probe_capabilities()

    def marker_line(self, marker: OwnMarker) -> str:
        return self._inner.marker_line(marker)

    def list_label_definitions(self) -> tuple[LabelDefinition, ...]:
        return self._inner.list_label_definitions()

    def define_labels(
        self, definitions: tuple[LabelDefinition, ...], *, grant: WriteGrant
    ) -> WriteReceipt:
        return self._inner.define_labels(definitions, grant=grant)

    def delete_labels(self, names: tuple[str, ...], *, grant: WriteGrant) -> WriteReceipt:
        return self._inner.delete_labels(names, grant=grant)

    def post_comment(
        self, issue: IssueKey, body: str, *, marker: OwnMarker, grant: WriteGrant
    ) -> WriteReceipt:
        return self._inner.post_comment(issue, body, marker=marker, grant=grant)

    def change_labels(
        self,
        issue: IssueKey,
        *,
        add: frozenset[str],
        remove: frozenset[str],
        ownership: LabelOwnership,
        grant: WriteGrant,
    ) -> WriteReceipt:
        return self._inner.change_labels(
            issue, add=add, remove=remove, ownership=ownership, grant=grant
        )

    def close_issue(
        self, issue: IssueKey, *, reason: CloseReason, grant: WriteGrant
    ) -> WriteReceipt:
        return self._inner.close_issue(issue, reason=reason, grant=grant)


def without_resolver(tracker: MemoryTracker) -> TrackerAdapter:
    """``tracker`` without the optional ``IssueResolver``: an adapter that predates amendment A1
    (or cannot resolve URLs), so a consumer's list-and-match fallback can be tested."""
    return _WithoutResolver(tracker)


__all__ = ["MemoryTracker", "without_resolver"]
