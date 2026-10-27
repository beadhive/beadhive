"""Runner-neutral conformance cases for the bead tracker adapter port.

The shared definition of done for a ``TrackerAdapter`` implementation: an adapter conforms when
it passes :data:`TRACKER_CASES`. Each case is a :class:`~beadhive.testing.PortCase` with a
stable lowercase id that receives a :class:`TrackerFactory` and builds a FRESH subject from it,
so no scenario leaks into the next. A real adapter runs the same cases against an offline
stand-in of its tracker (its own :class:`~beadhive.testing.tracker.world.TrackerWorld`); the
in-memory fake runs them directly::

    @pytest.mark.parametrize("case", TRACKER_CASES, ids=lambda case: case.case_id)
    def test_my_adapter_conforms(case):
        run_tracker_case(my_factory, case)

No case reads a clock, sleeps or opens a socket: every timestamp is explicit, and a retry wait is
recorded by an injected ``sleep`` that never blocks.

Optional Protocols are probed with ``isinstance``. A writer the adapter does not expose is a
valid structural refusal. The ``resolver.*`` cases check amendment A1 when the adapter is an
``IssueResolver``; ``resolver.listing-fallback`` holds for every adapter and is the degraded path
a consumer takes without one (external-intake-module-adr.md section 5).
"""

from __future__ import annotations

import dataclasses
import enum
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from beadhive.modules.tracker.application import iterate, snapshot_ids
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    Actor,
    ActorKind,
    ChangeRequestUrl,
    Checkpoint,
    CommentQuery,
    CommentWriter,
    IdQuery,
    IdSubject,
    IssueKey,
    IssueQuery,
    IssueResolver,
    IssueState,
    LabelNotOwned,
    LabelOwnership,
    LabelWriter,
    NotVisible,
    Operation,
    OperationNotEnabled,
    OtherProject,
    OwnMarker,
    RateLimited,
    RetryBudget,
    TrackerAdapter,
    TrackerComment,
    TrackerIssue,
    TrackerReader,
    Tri,
    UrlNotRecognized,
    WriteGrant,
    WriteOutcomeUnknown,
)

from ..conformance import PortCase, assert_port_conforms
from .memory import MemoryTracker, without_resolver
from .world import MemoryWorld, TrackerWorld, at

_CONTRACTS_MODULE = "beadhive.modules.tracker.contracts"


@dataclass
class TrackerSubject:
    """One fresh adapter plus the world that controls and observes its tracker."""

    adapter: Any  # TrackerAdapter, optionally CommentWriter / LabelWriter / IssueResolver
    world: TrackerWorld


class TrackerFactory(Protocol):
    """Builds a fresh subject. ``enabled`` names the WRITE operations to enable; both reads are
    always enabled for conformance. ``page_size`` is the adapter's listing page size."""

    def __call__(
        self,
        *,
        enabled: frozenset[Operation] = ...,
        page_size: int = ...,
        budget: RetryBudget | None = ...,
    ) -> TrackerSubject: ...


class TrackerCase(PortCase[TrackerFactory]):
    """A semantic case shared by every ``TrackerAdapter`` implementation."""


OWNED = LabelOwnership(prefixes=("bh:",))
MARKER = OwnMarker(hive="acme/widgets", install="inst-1", outbox_id="ob-1")


# ---- helpers -------------------------------------------------------------------------------


def all_issues(adapter: TrackerReader, **query: Any) -> tuple[list[TrackerIssue], Checkpoint]:
    """Every issue of one pull, plus the last checkpoint it emitted."""
    items: list[TrackerIssue] = []
    checkpoint = None
    for page in iterate(lambda c: adapter.list_issues(IssueQuery(cursor=c, **query))):
        items.extend(page.items)
        checkpoint = page.checkpoint or checkpoint
    assert checkpoint is not None or not items, "a non-empty pull emitted no checkpoint"
    return items, checkpoint  # type: ignore[return-value]


def all_comments(
    adapter: TrackerReader, **query: Any
) -> tuple[list[TrackerComment], Checkpoint | None]:
    """Every comment of one pull, plus the last checkpoint it emitted."""
    items: list[TrackerComment] = []
    checkpoint = None
    for page in iterate(lambda c: adapter.list_comments(CommentQuery(cursor=c, **query))):
        items.extend(page.items)
        checkpoint = page.checkpoint or checkpoint
    return items, checkpoint


def find_by_listing(adapter: TrackerReader, url: str) -> TrackerIssue | None:
    """The fallback without an ``IssueResolver``: page every issue (closed included) and match
    the canonical ``IssueKey.url``. Every refusal collapses into ``None`` (not found)."""
    items, _ = all_issues(adapter)
    return next((issue for issue in items if issue.key.url == url), None)


def grant(op: Operation, target: IssueKey, grant_id: str = "g-1") -> WriteGrant:
    return WriteGrant(grant_id=grant_id, operation=op, target=target)


_ALLOWED_LEAF_TYPES = (str, int, float, bool, type(None), datetime, timedelta, enum.Enum)


def assert_contract_only(value: Any, path: str = "value") -> None:
    """Every object an adapter returns is a contract dataclass, a primitive, or a container of
    them: no SDK model, HTTP object or raw payload crosses the port."""
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
    module = type(value).__module__
    assert module == _CONTRACTS_MODULE or module.startswith(f"{_CONTRACTS_MODULE}."), (
        f"{path}: {module}"
    )
    for item in dataclasses.fields(value):
        assert_contract_only(getattr(value, item.name), f"{path}.{item.name}")


def _refused(call: Any, error: type[Exception], message: str) -> Exception:
    try:
        call()
    except error as exc:
        return exc
    raise AssertionError(message)


# ---- cases: reading ------------------------------------------------------------------------


def _port_shape(factory: TrackerFactory) -> None:
    subject = factory()
    assert isinstance(subject.adapter, TrackerAdapter)  # TrackerReader + TrackerIdentity
    descriptor = subject.adapter.describe()
    assert descriptor.api_version == 1
    assert {Operation.ISSUES_READ, Operation.COMMENTS_READ} <= descriptor.supported
    assert_contract_only(descriptor)


def _closed_issues_included(factory: TrackerFactory) -> None:
    s = factory()
    s.world.add_issue("open one", at=at(2))
    closed = s.world.add_issue("closed one", at=at(3), state=IssueState.CLOSED)
    items, _ = all_issues(s.adapter)
    assert sorted(i.state.value for i in items) == ["closed", "open"]
    assert next(i for i in items if i.key == closed).closed_at is not None
    only_open, _ = all_issues(s.adapter, include_closed=False)
    assert [i.title for i in only_open] == ["open one"]


def _change_requests_excluded(factory: TrackerFactory) -> None:
    """Change requests are not work items, and neither is their conversation."""
    s = factory()
    issue = s.world.add_issue("an issue", at=at(2))
    change = s.world.add_change_request("a change request", at=at(3))
    s.world.add_comment(issue, "on the issue", at=at(4))
    s.world.add_comment(change, "on the change request", at=at(5))
    items, _ = all_issues(s.adapter)
    assert [i.title for i in items] == ["an issue"]
    comments, _ = all_comments(s.adapter)
    assert [c.body for c in comments] == ["on the issue"]


def _pagination_complete(factory: TrackerFactory) -> None:
    s = factory(page_size=2)
    keys = {s.world.add_issue(f"issue {n}", at=at(n)) for n in range(1, 6)}
    pages = list(iterate(lambda c: s.adapter.list_issues(IssueQuery(cursor=c))))
    assert len(pages) >= 3  # five items at two per page
    seen = [i.key for page in pages for i in page.items]
    assert len(seen) == len(set(seen)) == 5 and set(seen) == keys
    for page in pages:
        assert_contract_only(page)


def _checkpoint_incremental(factory: TrackerFactory) -> None:
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


def _checkpoint_mid_pull_resume(factory: TrackerFactory) -> None:
    """Persisting the checkpoint after page 1 and resuming from it loses nothing."""
    s = factory(page_size=2)
    for n in range(1, 6):
        s.world.add_issue(f"issue {n}", at=at(n))
    page1 = s.adapter.list_issues(IssueQuery())
    assert page1.next_cursor is not None and page1.checkpoint is not None
    rest, _ = all_issues(s.adapter, since=page1.checkpoint)
    union = {i.title for i in page1.items} | {i.title for i in rest}
    assert union == {f"issue {n}" for n in range(1, 6)}


def _issue_id_snapshot(factory: TrackerFactory) -> None:
    s = factory(page_size=2)
    for n in range(1, 4):
        s.world.add_issue(f"i{n}", at=at(n), state=IssueState.CLOSED if n == 2 else IssueState.OPEN)
    s.world.add_change_request("cr", at=at(5))
    snap = snapshot_ids(s.adapter, IdQuery(IdSubject.ISSUES))
    assert snap.complete and len(snap.ids) == 3  # closed included, change request excluded


def _issue_deletion_needs_snapshot(factory: TrackerFactory) -> None:
    s = factory(page_size=2)
    keys = [s.world.add_issue(f"i{n}", at=at(n)) for n in range(1, 4)]
    _, checkpoint = all_issues(s.adapter)
    s.world.delete_issue(keys[0])
    delta, _ = all_issues(s.adapter, since=checkpoint)
    assert keys[0] not in {i.key for i in delta}  # a since-pull cannot report it
    snap = snapshot_ids(s.adapter, IdQuery(IdSubject.ISSUES))
    assert snap.complete and snap.ids == {keys[1].url, keys[2].url}


def _comment_identity(factory: TrackerFactory) -> None:
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


def _comment_edit_is_new_revision(factory: TrackerFactory) -> None:
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


def _comment_deletion_needs_snapshot(factory: TrackerFactory) -> None:
    """Full id-set reconciliation: a deletion is visible only to a complete id snapshot."""
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


def _comment_checkpoint_safe_at_every_page(factory: TrackerFactory) -> None:
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


def _issue_scope(factory: TrackerFactory) -> None:
    s = factory()
    a = s.world.add_issue("a", at=at(2))
    b = s.world.add_issue("b", at=at(2))
    s.world.add_comment(a, "on a", at=at(3))
    s.world.add_comment(b, "on b", at=at(4))
    items, _ = all_comments(s.adapter, issue=a)
    assert [c.body for c in items] == ["on a"]
    ids = snapshot_ids(s.adapter, IdQuery(IdSubject.COMMENTS, issue=b))
    assert ids.complete and len(ids.ids) == 1


def _failed_snapshot_is_incomplete(factory: TrackerFactory) -> None:
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


def _capabilities_default_read_only(factory: TrackerFactory) -> None:
    s = factory()
    report = s.adapter.probe_capabilities()
    assert_contract_only(report)
    assert report.actor is not None and report.actor == s.adapter.whoami()
    for op in READ_OPERATIONS:
        assert report.states[op].usable
    for op in (Operation.COMMENT_CREATE, Operation.LABELS_CHANGE):
        state = report.states[op]
        assert not state.enabled and not state.usable  # every write is off by default
        assert state.probe in (Tri.YES, Tri.NO, Tri.UNKNOWN)
    assert s.world.write_requests() == 0  # probing never writes


# ---- cases: writes -------------------------------------------------------------------------


def _write_refused_when_not_enabled(factory: TrackerFactory) -> None:
    s = factory()  # no write enabled
    issue = s.world.add_issue("thread", at=at(2), labels=("bh:triage",))
    s.world.set_write_time(at(5))
    if isinstance(s.adapter, CommentWriter):  # an absent writer is the structural refusal
        _refused(
            lambda: s.adapter.post_comment(
                issue, "hi", marker=MARKER, grant=grant(Operation.COMMENT_CREATE, issue)
            ),
            OperationNotEnabled,
            "a comment was posted with comment.create disabled",
        )
    if isinstance(s.adapter, LabelWriter):
        _refused(
            lambda: s.adapter.change_labels(
                issue,
                add=frozenset({"bh:accepted"}),
                remove=frozenset(),
                ownership=OWNED,
                grant=grant(Operation.LABELS_CHANGE, issue),
            ),
            OperationNotEnabled,
            "labels were changed with labels.change disabled",
        )
    assert s.world.write_requests() == 0 and s.world.comment_count(issue) == 0
    assert s.world.server_labels(issue) == {"bh:triage"}


def _grant_must_match_and_is_single_use(factory: TrackerFactory) -> None:
    s = factory(enabled=frozenset({Operation.COMMENT_CREATE}))
    assert isinstance(s.adapter, CommentWriter), "comment.create is enabled but has no writer"
    issue = s.world.add_issue("thread", at=at(2))
    other = s.world.add_issue("other", at=at(2))
    wrong = [
        grant(Operation.COMMENT_CREATE, other),  # another target
        grant(Operation.LABELS_CHANGE, issue),  # another operation
    ]
    for bad in wrong:
        _refused(
            lambda bad=bad: s.adapter.post_comment(issue, "hi", marker=MARKER, grant=bad),
            OperationNotEnabled,
            f"a mismatched grant was accepted: {bad}",
        )
    good = grant(Operation.COMMENT_CREATE, issue, "g-once")
    s.world.set_write_time(at(5))
    s.adapter.post_comment(issue, "hi", marker=MARKER, grant=good)
    _refused(
        lambda: s.adapter.post_comment(issue, "hi again", marker=MARKER, grant=good),
        OperationNotEnabled,
        "a grant was used twice",
    )
    assert s.world.write_requests() == 1 and s.world.comment_count(issue) == 1


def _comment_round_trip(factory: TrackerFactory) -> None:
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


def _ambiguous_write_not_retried(factory: TrackerFactory) -> None:
    """Applied, but the caller cannot know: surfaced, never retried, found again by marker."""
    s = factory(enabled=frozenset({Operation.COMMENT_CREATE}))
    issue = s.world.add_issue("thread", at=at(2))
    s.world.set_write_time(at(6))
    s.world.fail_next_write_ambiguously()
    _refused(
        lambda: s.adapter.post_comment(
            issue, "hello", marker=MARKER, grant=grant(Operation.COMMENT_CREATE, issue)
        ),
        WriteOutcomeUnknown,
        "an ambiguous write outcome was not surfaced",
    )
    assert s.world.write_requests() == 1  # never retried by the adapter
    listed, _ = all_comments(s.adapter, issue=issue)  # the outbox reconciles by marker
    assert [c.marker for c in listed] == [MARKER]


def _ambiguous_write_not_applied(factory: TrackerFactory) -> None:
    """Kit extension K1: cut off before being applied. Surfaced as unknown, never retried, the
    grant is spent, and a complete snapshot shows no comment; a fresh grant then succeeds."""
    s = factory(enabled=frozenset({Operation.COMMENT_CREATE}))
    issue = s.world.add_issue("thread", at=at(2))
    s.world.set_write_time(at(6))
    s.world.fail_next_write_ambiguously(applied=False)
    first = grant(Operation.COMMENT_CREATE, issue, "g-first")
    _refused(
        lambda: s.adapter.post_comment(issue, "hello", marker=MARKER, grant=first),
        WriteOutcomeUnknown,
        "an ambiguous write outcome was not surfaced",
    )
    assert s.world.write_requests() == 1 and s.world.comment_count(issue) == 0
    listed, _ = all_comments(s.adapter, issue=issue)
    assert [c.marker for c in listed] == []
    snapshot = snapshot_ids(s.adapter, IdQuery(IdSubject.COMMENTS, issue=issue))
    assert snapshot.complete and snapshot.ids == frozenset()
    _refused(
        lambda: s.adapter.post_comment(issue, "hello", marker=MARKER, grant=first),
        OperationNotEnabled,
        "the grant of an ambiguous write was reused",
    )
    retry = grant(Operation.COMMENT_CREATE, issue, "g-retry")
    s.adapter.post_comment(issue, "hello", marker=MARKER, grant=retry)
    assert s.world.write_requests() == 2 and s.world.comment_count(issue) == 1


def _labels_only_owned(factory: TrackerFactory) -> None:
    s = factory(enabled=frozenset({Operation.LABELS_CHANGE}))
    issue = s.world.add_issue("thread", at=at(2), labels=("bug", "bh:triage"))
    s.world.add_human_label(issue, "needs-love")
    assert isinstance(s.adapter, LabelWriter), "labels.change is enabled but has no writer"
    for add, remove in ((frozenset({"wontfix"}), frozenset()), (frozenset(), frozenset({"bug"}))):
        _refused(
            lambda add=add, remove=remove: s.adapter.change_labels(
                issue,
                add=add,
                remove=remove,
                ownership=OWNED,
                grant=grant(Operation.LABELS_CHANGE, issue, f"g-{len(add)}"),
            ),
            LabelNotOwned,
            "a label outside the owned set was changed",
        )
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


def _rate_limit_within_budget(factory: TrackerFactory) -> None:
    s = factory(budget=RetryBudget(max_retries=3, max_single_wait=timedelta(seconds=30)))
    s.world.add_issue("one", at=at(2))
    s.world.throttle(requests=2, retry_after_seconds=7)
    items, _ = all_issues(s.adapter)
    assert [i.title for i in items] == ["one"]
    assert s.world.sleeps == [7.0, 7.0]  # waited exactly as told, on the injected sleep


def _rate_limit_beyond_budget(factory: TrackerFactory) -> None:
    s = factory(budget=RetryBudget(max_retries=1))
    s.world.add_issue("one", at=at(2))
    s.world.throttle(requests=5, retry_after_seconds=7)
    exc = _refused(
        lambda: s.adapter.list_issues(IssueQuery()),
        RateLimited,
        "the retry budget was not enforced",
    )
    assert isinstance(exc, RateLimited)
    assert exc.retry_after == timedelta(seconds=7) and exc.retryable
    assert s.world.sleeps == [7.0]


def _rate_limit_wait_cap(factory: TrackerFactory) -> None:
    """A wait longer than the budget's single-wait cap is surfaced, never slept."""
    budget = RetryBudget(
        max_retries=3, max_single_wait=timedelta(seconds=10), max_total_wait=timedelta(days=1)
    )
    s = factory(budget=budget)
    s.world.add_issue("one", at=at(2))
    s.world.throttle(requests=1, retry_after_seconds=3600)
    exc = _refused(
        lambda: s.adapter.list_issues(IssueQuery()), RateLimited, "an hour-long wait was accepted"
    )
    assert isinstance(exc, RateLimited) and exc.retry_after == timedelta(seconds=3600)
    assert s.world.sleeps == []


# ---- cases: the optional IssueResolver (amendment A1) --------------------------------------


def _resolver_resolve_is_pure_and_canonical(factory: TrackerFactory) -> None:
    s = factory()
    if not isinstance(s.adapter, IssueResolver):
        return  # optional; the listing fallback case covers adapters without one
    key = s.world.add_issue("an issue", at=at(2))
    before = s.world.read_requests()
    assert s.adapter.resolve(key.url) == key
    for variant in s.world.url_variants(key):
        resolved = s.adapter.resolve(variant)
        assert resolved == key and resolved.url == key.url, variant
    assert s.world.read_requests() == before  # resolve sends no request
    assert s.world.write_requests() == 0


def _resolver_refusals(factory: TrackerFactory) -> None:
    """Each pasted URL that is not an issue of this project gets its own refusal, no request."""
    s = factory()
    if not isinstance(s.adapter, IssueResolver):
        return
    change = s.world.add_change_request("a change request", at=at(3))
    before = s.world.read_requests()
    _refused(
        lambda: s.adapter.resolve(s.world.unrecognized_url()),
        UrlNotRecognized,
        "an unrecognised URL was resolved",
    )
    _refused(
        lambda: s.adapter.resolve(s.world.foreign_issue_url()),
        OtherProject,
        "another project's issue URL was resolved",
    )
    assert s.world.read_requests() == before  # the pure refusals send no request
    try:
        key = s.adapter.resolve(change.url)
    except ChangeRequestUrl:
        return  # refused purely from the URL's shape
    _refused(lambda: s.adapter.get(key), ChangeRequestUrl, "a change request was got as an issue")


def _resolver_get_reads_one_issue(factory: TrackerFactory) -> None:
    s = factory()
    if not isinstance(s.adapter, IssueResolver):
        return
    open_key = s.world.add_issue("open one", at=at(2), labels=("bug",))
    closed_key = s.world.add_issue("closed one", at=at(3), state=IssueState.CLOSED)
    gone = s.world.add_issue("deleted", at=at(4))
    listed = {i.key: i for i in all_issues(s.adapter)[0]}
    s.world.delete_issue(gone)
    before = s.world.read_requests()
    issue = s.adapter.get(s.adapter.resolve(open_key.url))
    assert s.world.read_requests() == before + 1  # one read
    assert_contract_only(issue)
    assert issue == listed[open_key]
    closed = s.adapter.get(closed_key)
    assert closed.state is IssueState.CLOSED and closed == listed[closed_key]
    _refused(lambda: s.adapter.get(gone), NotVisible, "a deleted issue was got")
    assert s.world.write_requests() == 0


def _resolver_listing_fallback(factory: TrackerFactory) -> None:
    """Every adapter: the list-and-match fallback finds open and closed issues by canonical URL,
    and anything else is simply "not found". With a resolver, both paths agree."""
    s = factory(page_size=2)
    keys = [
        s.world.add_issue(f"i{n}", at=at(n), state=IssueState.CLOSED if n % 2 else IssueState.OPEN)
        for n in range(1, 5)
    ]
    change = s.world.add_change_request("cr", at=at(6))
    for key in keys:
        found = find_by_listing(s.adapter, key.url)
        assert found is not None and found.key == key
    for url in (change.url, s.world.foreign_issue_url(), s.world.unrecognized_url()):
        assert find_by_listing(s.adapter, url) is None  # R3 to R6 collapse into not found
    if isinstance(s.adapter, IssueResolver):
        for key in keys:
            assert s.adapter.get(s.adapter.resolve(key.url)) == find_by_listing(s.adapter, key.url)
    assert s.world.write_requests() == 0


TRACKER_CASES: tuple[TrackerCase, ...] = (
    TrackerCase("port.shape", _port_shape),
    TrackerCase("issues.closed-included", _closed_issues_included),
    TrackerCase("issues.change-requests-excluded", _change_requests_excluded),
    TrackerCase("issues.pagination-complete", _pagination_complete),
    TrackerCase("issues.checkpoint-incremental", _checkpoint_incremental),
    TrackerCase("issues.checkpoint-mid-pull-resume", _checkpoint_mid_pull_resume),
    TrackerCase("issues.id-snapshot", _issue_id_snapshot),
    TrackerCase("issues.deletion-needs-id-snapshot", _issue_deletion_needs_snapshot),
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
    TrackerCase("write.ambiguous-not-applied", _ambiguous_write_not_applied),
    TrackerCase("labels.only-owned", _labels_only_owned),
    TrackerCase("rate-limit.within-budget", _rate_limit_within_budget),
    TrackerCase("rate-limit.beyond-budget", _rate_limit_beyond_budget),
    TrackerCase("rate-limit.wait-cap", _rate_limit_wait_cap),
    TrackerCase("resolver.resolve-is-pure-and-canonical", _resolver_resolve_is_pure_and_canonical),
    TrackerCase("resolver.refusals", _resolver_refusals),
    TrackerCase("resolver.get-reads-one-issue", _resolver_get_reads_one_issue),
    TrackerCase("resolver.listing-fallback", _resolver_listing_fallback),
)


# ---- runners and the in-memory factory -----------------------------------------------------


def run_tracker_case(
    factory: TrackerFactory, case: TrackerCase, *, subject: str = "tracker adapter"
) -> None:
    """Run one case; a failure raises ``ConformanceFailure`` naming the case id."""
    assert_port_conforms(lambda: factory, (case,), subject=subject)


def assert_tracker_conforms(
    factory: TrackerFactory,
    cases: Sequence[TrackerCase] = TRACKER_CASES,
    *,
    subject: str = "tracker adapter",
) -> None:
    """Run every case against fresh subjects from ``factory``."""
    assert_port_conforms(lambda: factory, cases, subject=subject)


def memory_subject(
    *,
    enabled: frozenset[Operation] = frozenset(),
    page_size: int = 2,
    budget: RetryBudget | None = None,
    resolver: bool = True,
) -> TrackerSubject:
    """A fresh :class:`MemoryTracker` over a fresh :class:`MemoryWorld` (both reads enabled,
    plus the write operations named in ``enabled``). ``resolver=False`` hides the optional
    ``IssueResolver``. This is a :class:`TrackerFactory`."""
    world = MemoryWorld()
    tracker = MemoryTracker(
        world, enabled=READ_OPERATIONS | enabled, page_size=page_size, budget=budget
    )
    return TrackerSubject(tracker if resolver else without_resolver(tracker), world)


__all__ = [
    "MARKER",
    "OWNED",
    "TRACKER_CASES",
    "TrackerCase",
    "TrackerFactory",
    "TrackerSubject",
    "all_comments",
    "all_issues",
    "assert_contract_only",
    "assert_tracker_conforms",
    "find_by_listing",
    "grant",
    "memory_subject",
    "run_tracker_case",
]
