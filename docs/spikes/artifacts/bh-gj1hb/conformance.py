"""Runner-neutral conformance cases for the bead tracker adapter port (spike bh-gj1hb).

Proposed home: ``beadhive.testing.tracker`` — the surface plugin packages may import
(``scripts/check_package_imports.py``), shaped like ``beadhive.testing.PortCase``: each case has a
stable lowercase id and receives a FRESH subject from a factory, so no scenario leaks into the
next. A real adapter runs the same cases against an offline stand-in of its tracker (a
``httpx.MockTransport`` server for GitHub); the in-memory fake runs them directly.

``TrackerWorld`` is the test-only control surface for the tracker's server-side state. It is
never part of the port: consumers cannot seed, edit or delete through it.

No case reads a clock or sleeps: every timestamp is explicit, and retry waits are recorded by an
injected ``sleep`` that never blocks.
"""

from __future__ import annotations

import dataclasses
import enum
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from tracker_contracts import (
    Actor,
    ActorKind,
    Checkpoint,
    CommentKey,
    CommentQuery,
    CommentWriter,
    IdQuery,
    IdSubject,
    IssueKey,
    IssueQuery,
    IssueState,
    LabelNotOwned,
    LabelOwnership,
    LabelWriter,
    Operation,
    OperationNotEnabled,
    OwnMarker,
    RateLimited,
    RetryBudget,
    TrackerAdapter,
    TrackerComment,
    TrackerIssue,
    TrackerReader,
    Tri,
    WriteGrant,
    WriteOutcomeUnknown,
    iterate,
    snapshot_ids,
)


def at(day: int, hour: int = 0) -> datetime:
    return datetime(2026, 1, day, hour, tzinfo=UTC)


class TrackerWorld(Protocol):
    sleeps: list[float]

    def add_issue(
        self,
        title: str,
        *,
        at: datetime,
        state: IssueState = IssueState.OPEN,
        labels: tuple[str, ...] = (),
    ) -> IssueKey: ...

    def update_issue(
        self, key: IssueKey, *, at: datetime, state: IssueState | None = None
    ) -> None: ...

    def add_change_request(self, title: str, *, at: datetime) -> IssueKey: ...

    def add_comment(
        self,
        issue: IssueKey,
        body: str,
        *,
        at: datetime,
        author: str = "alice",
        integration: str | None = None,
    ) -> CommentKey: ...

    def edit_comment(self, key: CommentKey, body: str, *, at: datetime) -> None: ...

    def delete_comment(self, key: CommentKey) -> None: ...

    def add_human_label(self, issue: IssueKey, label: str) -> None: ...

    def server_labels(self, issue: IssueKey) -> frozenset[str]: ...

    def comment_count(self, issue: IssueKey) -> int: ...

    def throttle(self, requests: int, retry_after_seconds: float) -> None: ...

    def fail_next_write_ambiguously(self) -> None: ...

    def write_requests(self) -> int: ...

    def set_write_time(self, at: datetime) -> None: ...


@dataclass
class Subject:
    adapter: Any  # TrackerReader + TrackerIdentity, optionally CommentWriter / LabelWriter
    world: TrackerWorld


Factory = Callable[..., Subject]
"""``factory(*, enabled=frozenset(), page_size=2, budget=RetryBudget()) -> Subject``"""


@dataclass(frozen=True)
class TrackerCase:
    case_id: str
    check: Callable[[Factory], None]

    def __post_init__(self) -> None:
        assert re.fullmatch(r"[a-z0-9]+(?:[._-][a-z0-9]+)*", self.case_id), self.case_id


OWNED = LabelOwnership(prefixes=("bh:",))
MARKER = OwnMarker(hive="acme/widgets", install="inst-1", outbox_id="ob-1")


# ---- helpers -------------------------------------------------------------------------------


def all_issues(adapter: TrackerReader, **query: Any) -> tuple[list[TrackerIssue], Checkpoint]:
    items: list[TrackerIssue] = []
    checkpoint = None
    for page in iterate(lambda c: adapter.list_issues(IssueQuery(cursor=c, **query))):
        items.extend(page.items)
        checkpoint = page.checkpoint or checkpoint
    assert checkpoint is not None or not items
    return items, checkpoint  # type: ignore[return-value]


def all_comments(
    adapter: TrackerReader, **query: Any
) -> tuple[list[TrackerComment], Checkpoint | None]:
    items: list[TrackerComment] = []
    checkpoint = None
    for page in iterate(lambda c: adapter.list_comments(CommentQuery(cursor=c, **query))):
        items.extend(page.items)
        checkpoint = page.checkpoint or checkpoint
    return items, checkpoint


def grant(op: Operation, target: IssueKey, grant_id: str = "g-1") -> WriteGrant:
    return WriteGrant(grant_id=grant_id, operation=op, target=target)


_ALLOWED_LEAF_TYPES = (str, int, float, bool, type(None), datetime, timedelta, enum.Enum)


def assert_contract_only(value: Any, path: str = "value") -> None:
    """Every object an adapter returns is a contract dataclass, a primitive, or a container
    of them: no SDK model, ``httpx`` object or raw dict crosses the port."""
    if isinstance(value, _ALLOWED_LEAF_TYPES):
        return
    if isinstance(value, (tuple, frozenset, list, set)):
        for index, item in enumerate(value):
            assert_contract_only(item, f"{path}[{index}]")
        return
    if isinstance(value, dict):  # Mapping[Operation, CapabilityState] only
        for key, item in value.items():
            assert_contract_only(key, f"{path}.key")
            assert_contract_only(item, f"{path}[{key!r}]")
        return
    assert dataclasses.is_dataclass(value), f"{path}: non-contract type {type(value).__name__}"
    assert type(value).__module__ == "tracker_contracts", f"{path}: {type(value).__module__}"
    for item in dataclasses.fields(value):
        assert_contract_only(getattr(value, item.name), f"{path}.{item.name}")


# ---- cases: reading ------------------------------------------------------------------------


def _port_shape(factory: Factory) -> None:
    subject = factory()
    assert isinstance(subject.adapter, TrackerAdapter)  # TrackerReader + TrackerIdentity
    descriptor = subject.adapter.describe()
    assert {Operation.ISSUES_READ, Operation.COMMENTS_READ} <= descriptor.supported
    assert_contract_only(descriptor)


def _closed_issues_included(factory: Factory) -> None:
    s = factory()
    s.world.add_issue("open one", at=at(2))
    s.world.add_issue("closed one", at=at(3), state=IssueState.CLOSED)
    items, _ = all_issues(s.adapter)
    assert sorted(i.state.value for i in items) == ["closed", "open"]
    only_open, _ = all_issues(s.adapter, include_closed=False)
    assert [i.title for i in only_open] == ["open one"]


def _change_requests_excluded(factory: Factory) -> None:
    """Pull/merge requests are not work items, and neither is their conversation."""
    s = factory()
    issue = s.world.add_issue("an issue", at=at(2))
    change = s.world.add_change_request("a change request", at=at(3))
    s.world.add_comment(issue, "on the issue", at=at(4))
    s.world.add_comment(change, "on the change request", at=at(5))
    items, _ = all_issues(s.adapter)
    assert [i.title for i in items] == ["an issue"]
    comments, _ = all_comments(s.adapter)
    assert [c.body for c in comments] == ["on the issue"]


def _pagination_complete(factory: Factory) -> None:
    s = factory(page_size=2)
    keys = {s.world.add_issue(f"issue {n}", at=at(n)) for n in range(1, 6)}
    pages = list(iterate(lambda c: s.adapter.list_issues(IssueQuery(cursor=c))))
    assert len(pages) >= 3  # five items at two per page
    seen = [i.key for page in pages for i in page.items]
    assert len(seen) == len(set(seen)) == 5 and set(seen) == keys
    for page in pages:
        assert_contract_only(page)


def _checkpoint_incremental(factory: Factory) -> None:
    s = factory(page_size=2)
    s.world.add_issue("untouched", at=at(1))
    first = s.world.add_issue("first", at=at(2))
    s.world.add_issue("boundary", at=at(3))
    _, checkpoint = all_issues(s.adapter)
    s.world.update_issue(first, at=at(10), state=IssueState.CLOSED)
    s.world.add_issue("third", at=at(11))
    s.world.add_issue("same second", at=at(3))  # timestamps are coarse: ties are real
    delta, after = all_issues(s.adapter, since=checkpoint)
    titles = {i.title for i in delta}
    assert {"first", "third", "same second"} <= titles  # never skips a newer or tied revision
    assert titles <= {"first", "third", "same second", "boundary"}  # the boundary may repeat
    assert next(i for i in delta if i.key == first).state is IssueState.CLOSED
    assert after != checkpoint
    again, _ = all_issues(s.adapter, since=after)
    assert {i.title for i in again} <= {"third"}  # only the boundary item, nothing older


def _checkpoint_mid_pull_resume(factory: Factory) -> None:
    """Persisting the checkpoint after page 1 and resuming from it loses nothing."""
    s = factory(page_size=2)
    for n in range(1, 6):
        s.world.add_issue(f"issue {n}", at=at(n))
    page1 = s.adapter.list_issues(IssueQuery())
    assert page1.next_cursor is not None and page1.checkpoint is not None
    rest, _ = all_issues(s.adapter, since=page1.checkpoint)
    union = {i.title for i in page1.items} | {i.title for i in rest}
    assert union == {f"issue {n}" for n in range(1, 6)}


def _comment_identity(factory: Factory) -> None:
    s = factory()
    issue = s.world.add_issue("thread", at=at(2))
    s.world.add_comment(issue, "from a person", at=at(3), author="alice")
    s.world.add_comment(issue, "from an app", at=at(4), author="ci-app", integration="ci-app")
    items, _ = all_comments(s.adapter)
    human, bot = sorted(items, key=lambda c: c.created_at)
    assert human.issue == issue and human.author is not None
    assert human.author.key.kind is ActorKind.HUMAN and human.via is None
    assert bot.author is not None and bot.author.key.kind is ActorKind.BOT
    assert bot.via is not None
    assert human.author.key != bot.author.key
    assert human.marker is None
    for comment in items:
        assert_contract_only(comment)


def _comment_edit_is_new_revision(factory: Factory) -> None:
    s = factory()
    issue = s.world.add_issue("thread", at=at(2))
    key = s.world.add_comment(issue, "v1", at=at(3))
    items, checkpoint = all_comments(s.adapter)
    before = next(c for c in items if c.key == key)
    assert not before.edited
    s.world.edit_comment(key, "v2", at=at(9))
    delta, _ = all_comments(s.adapter, since=checkpoint)
    after = next(c for c in delta if c.key == key)
    assert after.body == "v2" and after.edited and after.revision > before.revision


def _comment_deletion_needs_snapshot(factory: Factory) -> None:
    s = factory(page_size=2)
    issue = s.world.add_issue("thread", at=at(2))
    keys = [s.world.add_comment(issue, f"c{n}", at=at(3 + n)) for n in range(3)]
    _, checkpoint = all_comments(s.adapter)
    s.world.delete_comment(keys[1])
    delta, _ = all_comments(s.adapter, since=checkpoint)
    assert keys[1] not in {c.key for c in delta}  # a since-pull cannot report it
    snapshot = snapshot_ids(s.adapter, IdQuery(IdSubject.COMMENTS))
    assert snapshot.complete
    assert snapshot.ids == {keys[0].id, keys[2].id}


def _comment_checkpoint_safe_at_every_page(factory: Factory) -> None:
    """Persisting ANY emitted page checkpoint and resuming from it loses nothing, even when an
    old comment was edited (per-issue listings come in creation order on some trackers)."""
    for scoped in (False, True):
        s = factory(page_size=2)
        issue = s.world.add_issue("thread", at=at(2))
        keys = [s.world.add_comment(issue, f"c{n}", at=at(3 + n)) for n in range(5)]
        s.world.edit_comment(keys[0], "c0 edited", at=at(20))
        scope = {"issue": issue} if scoped else {}
        delivered: set[str] = set()
        adapter = s.adapter

        def fetch(cursor, adapter=adapter, scope=scope):
            return adapter.list_comments(CommentQuery(cursor=cursor, **scope))

        for page in iterate(fetch):
            delivered |= {c.key.id for c in page.items}
            if page.checkpoint is None:
                continue
            rest, _ = all_comments(s.adapter, since=page.checkpoint, **scope)
            missing = {k.id for k in keys} - delivered - {c.key.id for c in rest}
            assert not missing, f"resuming from a page checkpoint misses {missing} ({scope})"


def _issue_scope(factory: Factory) -> None:
    s = factory()
    a = s.world.add_issue("a", at=at(2))
    b = s.world.add_issue("b", at=at(2))
    s.world.add_comment(a, "on a", at=at(3))
    s.world.add_comment(b, "on b", at=at(4))
    items, _ = all_comments(s.adapter, issue=a)
    assert [c.body for c in items] == ["on a"]
    ids = snapshot_ids(s.adapter, IdQuery(IdSubject.COMMENTS, issue=b))
    assert ids.complete and len(ids.ids) == 1


def _issue_id_snapshot(factory: Factory) -> None:
    s = factory(page_size=2)
    for n in range(1, 4):
        s.world.add_issue(f"i{n}", at=at(n), state=IssueState.CLOSED if n == 2 else IssueState.OPEN)
    s.world.add_change_request("cr", at=at(5))
    snap = snapshot_ids(s.adapter, IdQuery(IdSubject.ISSUES))
    assert snap.complete and len(snap.ids) == 3  # closed included, change request excluded


def _failed_snapshot_is_incomplete(factory: Factory) -> None:
    """A listing that fails part-way raises a port error, so no deletion is ever inferred."""
    s = factory(page_size=2, budget=RetryBudget(max_retries=0))
    issue = s.world.add_issue("thread", at=at(2))
    for n in range(3):
        s.world.add_comment(issue, f"c{n}", at=at(3 + n))
    first = s.adapter.list_ids(IdQuery(IdSubject.COMMENTS))
    assert first.next_cursor is not None
    s.world.throttle(requests=1, retry_after_seconds=30)
    rest = snapshot_ids(s.adapter, IdQuery(IdSubject.COMMENTS, cursor=first.next_cursor))
    assert not rest.complete


# ---- cases: identity and capability --------------------------------------------------------


def _capabilities_default_read_only(factory: Factory) -> None:
    s = factory()
    report = s.adapter.probe_capabilities()
    assert_contract_only(report)
    assert report.actor is not None and report.actor == s.adapter.whoami()
    for op in (Operation.ISSUES_READ, Operation.COMMENTS_READ):
        assert report.states[op].usable
    for op in (Operation.COMMENT_CREATE, Operation.LABELS_CHANGE):
        state = report.states[op]
        assert not state.enabled and not state.usable  # every write is off by default
        assert state.probe in (Tri.YES, Tri.NO, Tri.UNKNOWN)
    assert s.world.write_requests() == 0  # probing never writes


# ---- cases: writes -------------------------------------------------------------------------


def _write_refused_when_not_enabled(factory: Factory) -> None:
    s = factory()  # nothing enabled
    issue = s.world.add_issue("thread", at=at(2))
    assert isinstance(s.adapter, CommentWriter)
    try:
        s.adapter.post_comment(
            issue, "hi", marker=MARKER, grant=grant(Operation.COMMENT_CREATE, issue)
        )
    except OperationNotEnabled:
        pass
    else:
        raise AssertionError("a write ran with the operation disabled")
    assert s.world.write_requests() == 0 and s.world.comment_count(issue) == 0


def _grant_must_match_and_is_single_use(factory: Factory) -> None:
    s = factory(enabled=frozenset({Operation.COMMENT_CREATE}))
    issue = s.world.add_issue("thread", at=at(2))
    other = s.world.add_issue("other", at=at(2))
    wrong = [
        grant(Operation.COMMENT_CREATE, other),  # another target
        grant(Operation.LABELS_CHANGE, issue),  # another operation
    ]
    for bad in wrong:
        try:
            s.adapter.post_comment(issue, "hi", marker=MARKER, grant=bad)
        except OperationNotEnabled:
            continue
        raise AssertionError(f"a mismatched grant was accepted: {bad}")
    good = grant(Operation.COMMENT_CREATE, issue, "g-once")
    s.world.set_write_time(at(5))
    s.adapter.post_comment(issue, "hi", marker=MARKER, grant=good)
    try:
        s.adapter.post_comment(issue, "hi again", marker=MARKER, grant=good)
    except OperationNotEnabled:
        pass
    else:
        raise AssertionError("a grant was used twice")
    assert s.world.write_requests() == 1 and s.world.comment_count(issue) == 1


def _comment_round_trip(factory: Factory) -> None:
    s = factory(enabled=frozenset({Operation.COMMENT_CREATE}))
    issue = s.world.add_issue("thread", at=at(2))
    s.world.set_write_time(at(6))
    receipt = s.adapter.post_comment(
        issue, "We reproduced this.", marker=MARKER, grant=grant(Operation.COMMENT_CREATE, issue)
    )
    assert_contract_only(receipt)
    assert receipt.comment is not None and receipt.grant_id == "g-1"
    listed, _ = all_comments(s.adapter, issue=issue)
    mine = next(c for c in listed if c.key == receipt.comment.key)
    assert mine.body == "We reproduced this."  # the marker encoding is not in the body
    assert mine.marker == MARKER  # ... but decodes back
    me: Actor | None = s.adapter.whoami()
    assert me is not None and mine.author is not None and mine.author.key == me.key


def _ambiguous_write_not_retried(factory: Factory) -> None:
    s = factory(enabled=frozenset({Operation.COMMENT_CREATE}))
    issue = s.world.add_issue("thread", at=at(2))
    s.world.set_write_time(at(6))
    s.world.fail_next_write_ambiguously()
    try:
        s.adapter.post_comment(
            issue, "hello", marker=MARKER, grant=grant(Operation.COMMENT_CREATE, issue)
        )
    except WriteOutcomeUnknown:
        pass
    else:
        raise AssertionError("an ambiguous write outcome was not surfaced")
    assert s.world.write_requests() == 1  # never retried by the adapter
    listed, _ = all_comments(s.adapter, issue=issue)  # the outbox reconciles by marker
    assert [c.marker for c in listed] == [MARKER]


def _labels_only_owned(factory: Factory) -> None:
    s = factory(enabled=frozenset({Operation.LABELS_CHANGE}))
    issue = s.world.add_issue("thread", at=at(2), labels=("bug", "bh:triage"))
    s.world.add_human_label(issue, "needs-love")
    assert isinstance(s.adapter, LabelWriter)
    for add, remove in ((frozenset({"wontfix"}), frozenset()), (frozenset(), frozenset({"bug"}))):
        try:
            s.adapter.change_labels(
                issue,
                add=add,
                remove=remove,
                ownership=OWNED,
                grant=grant(Operation.LABELS_CHANGE, issue, f"g-{len(add)}"),
            )
        except LabelNotOwned:
            continue
        raise AssertionError("a label outside the owned set was changed")
    assert s.world.write_requests() == 0
    receipt = s.adapter.change_labels(
        issue,
        add=frozenset({"bh:accepted"}),
        remove=frozenset({"bh:triage"}),
        ownership=OWNED,
        grant=grant(Operation.LABELS_CHANGE, issue, "g-ok"),
    )
    expected = {"bug", "needs-love", "bh:accepted"}
    assert receipt.labels_after == expected == s.world.server_labels(issue)


# ---- cases: rate limits --------------------------------------------------------------------


def _rate_limit_within_budget(factory: Factory) -> None:
    s = factory(budget=RetryBudget(max_retries=3, max_single_wait=timedelta(seconds=30)))
    s.world.add_issue("one", at=at(2))
    s.world.throttle(requests=2, retry_after_seconds=7)
    items, _ = all_issues(s.adapter)
    assert [i.title for i in items] == ["one"]
    assert s.world.sleeps == [7.0, 7.0]  # waited exactly as told, on the injected sleep


def _rate_limit_beyond_budget(factory: Factory) -> None:
    s = factory(budget=RetryBudget(max_retries=1))
    s.world.add_issue("one", at=at(2))
    s.world.throttle(requests=5, retry_after_seconds=7)
    try:
        s.adapter.list_issues(IssueQuery())
    except RateLimited as exc:
        assert exc.retry_after == timedelta(seconds=7) and exc.retryable
    else:
        raise AssertionError("the retry budget was not enforced")
    assert s.world.sleeps == [7.0]


def _rate_limit_wait_cap(factory: Factory) -> None:
    """A wait longer than the budget's single-wait cap is surfaced, never slept."""
    budget = RetryBudget(
        max_retries=3, max_single_wait=timedelta(seconds=10), max_total_wait=timedelta(days=1)
    )
    s = factory(budget=budget)
    s.world.add_issue("one", at=at(2))
    s.world.throttle(requests=1, retry_after_seconds=3600)
    try:
        s.adapter.list_issues(IssueQuery())
    except RateLimited as exc:
        assert exc.retry_after == timedelta(seconds=3600)
    else:
        raise AssertionError("an hour-long wait was accepted")
    assert s.world.sleeps == []


CASES: tuple[TrackerCase, ...] = (
    TrackerCase("port.shape", _port_shape),
    TrackerCase("issues.closed-included", _closed_issues_included),
    TrackerCase("issues.change-requests-excluded", _change_requests_excluded),
    TrackerCase("issues.pagination-complete", _pagination_complete),
    TrackerCase("issues.checkpoint-incremental", _checkpoint_incremental),
    TrackerCase("issues.checkpoint-mid-pull-resume", _checkpoint_mid_pull_resume),
    TrackerCase("issues.id-snapshot", _issue_id_snapshot),
    TrackerCase("comments.identity", _comment_identity),
    TrackerCase("comments.edit-is-new-revision", _comment_edit_is_new_revision),
    TrackerCase("comments.deletion-needs-id-snapshot", _comment_deletion_needs_snapshot),
    TrackerCase("comments.issue-scope", _issue_scope),
    TrackerCase("ids.failed-snapshot-is-incomplete", _failed_snapshot_is_incomplete),
    TrackerCase("comments.checkpoint-safe-at-every-page", _comment_checkpoint_safe_at_every_page),
    TrackerCase("capability.default-read-only", _capabilities_default_read_only),
    TrackerCase("write.refused-when-not-enabled", _write_refused_when_not_enabled),
    TrackerCase("write.grant-matches-and-single-use", _grant_must_match_and_is_single_use),
    TrackerCase("write.comment-round-trip", _comment_round_trip),
    TrackerCase("write.ambiguous-not-retried", _ambiguous_write_not_retried),
    TrackerCase("labels.only-owned", _labels_only_owned),
    TrackerCase("rate-limit.within-budget", _rate_limit_within_budget),
    TrackerCase("rate-limit.beyond-budget", _rate_limit_beyond_budget),
    TrackerCase("rate-limit.wait-cap", _rate_limit_wait_cap),
)
