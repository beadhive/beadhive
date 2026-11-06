"""The ``beadhive.testing.tracker`` kit: MemoryTracker passes every case, and the cases bite.

Each deliberately broken ``MemoryTracker`` variant below (the in-repo successor of the spike's
textual mutation controls) must be rejected by the named case, so no case is vacuous. Nothing
here reads a clock, sleeps or opens a socket.
"""

from __future__ import annotations

import dataclasses
import functools
import socket
import time

import pytest

from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    ChangeRequestUrl,
    CommentQuery,
    CommentWriter,
    IdQuery,
    IdSubject,
    IssueCloser,
    IssueFieldWriter,
    IssueKey,
    IssueQuery,
    IssueResolver,
    IssueTypeWriter,
    LabelDefiner,
    LabelDefinition,
    LabelNotOwned,
    LabelOwnership,
    LabelPruner,
    LabelReader,
    LabelWriter,
    NotVisible,
    Operation,
    OperationNotEnabled,
    OtherProject,
    TrackerAdapter,
    UrlNotRecognized,
    WriteOutcomeUnknown,
    WriteReceipt,
)
from beadhive.testing import ConformanceFailure
from beadhive.testing.tracker import (
    MARKER,
    TRACKER_CASES,
    MemoryTracker,
    MemoryWorld,
    TrackerCase,
    TrackerSubject,
    assert_tracker_conforms,
    at,
    grant,
    memory_subject,
    run_tracker_case,
    without_resolver,
)
from beadhive.testing.tracker import memory as memory_module

_FACTORIES = {
    "memory": memory_subject,
    "memory-without-resolver": functools.partial(memory_subject, resolver=False),
}


@pytest.fixture(autouse=True)
def _offline_and_clockless(monkeypatch):
    def no_sleep(*_args, **_kwargs):
        raise AssertionError("a real sleep was attempted")

    def no_socket(*_args, **_kwargs):
        raise AssertionError("a real network connection was attempted")

    monkeypatch.setattr(time, "sleep", no_sleep)
    monkeypatch.setattr(socket.socket, "connect", no_socket)


@pytest.mark.parametrize("impl", sorted(_FACTORIES))
@pytest.mark.parametrize("case", TRACKER_CASES, ids=lambda case: case.case_id)
def test_memory_tracker_conforms(impl: str, case: TrackerCase) -> None:
    run_tracker_case(_FACTORIES[impl], case, subject=impl)


def test_case_set_covers_the_acceptance_scenarios() -> None:
    ids = {case.case_id for case in TRACKER_CASES}
    assert len(ids) == len(TRACKER_CASES) == 51
    assert {
        "issues.closed-included",
        "issues.checkpoint-incremental",
        "comments.deletion-needs-id-snapshot",
        "write.ambiguous-not-applied",
        "labels.read-lists-every-definition",
        "labels.define-only-owned",
        "labels.define-grant-matches-and-single-use",
        "labels.define-refused-when-not-enabled",
        "labels.define-is-idempotent",
        "labels.define-ambiguous-not-retried",
        "labels.define-ambiguous-not-applied",
        "labels.delete-only-owned",
        "labels.delete-grant-matches-and-single-use",
        "labels.delete-refused-when-not-enabled",
        "labels.delete-already-gone-succeeds",
        "close.refused-when-not-enabled",
        "close.grant-matches-and-single-use",
        "close.round-trip",
        "close.ambiguous-not-retried",
        "close.duplicate-of-canonical",
        "values.refused-when-not-enabled",
        "values.read-gated",
        "values.grant-matches-and-single-use",
        "values.type-round-trip",
        "values.fields-round-trip",
        "values.undefined-refused",
        "values.ambiguous-not-retried",
        "resolver.resolve-is-pure-and-canonical",
        "resolver.refusals",
        "resolver.get-reads-one-issue",
        "resolver.listing-fallback",
    } <= ids


def test_memory_tracker_implements_every_optional_protocol_and_the_view_hides_the_resolver() -> (
    None
):
    tracker = MemoryTracker(MemoryWorld())
    view = without_resolver(tracker)
    for adapter in (tracker, view):
        assert isinstance(adapter, TrackerAdapter)
        assert isinstance(adapter, CommentWriter) and isinstance(adapter, LabelWriter)
        assert isinstance(adapter, LabelReader) and isinstance(adapter, LabelDefiner)
        assert isinstance(adapter, LabelPruner) and isinstance(adapter, IssueCloser)
        assert isinstance(adapter, IssueTypeWriter) and isinstance(adapter, IssueFieldWriter)
    assert isinstance(tracker, IssueResolver) and not isinstance(view, IssueResolver)


def test_default_enables_reads_only_and_a_disabled_read_is_refused_before_any_request() -> None:
    world = MemoryWorld()
    world.add_issue("one", at=at(2))
    report = MemoryTracker(world).probe_capabilities()
    assert {op for op, state in report.states.items() if state.enabled} == READ_OPERATIONS

    closed = MemoryTracker(world, enabled=frozenset())
    for call in (
        lambda: closed.list_issues(IssueQuery()),
        lambda: closed.list_comments(CommentQuery()),
        lambda: closed.list_ids(IdQuery(IdSubject.ISSUES)),
        lambda: closed.list_label_definitions(),
        lambda: closed.get(IssueKey("#1", world.issue_url(1))),
    ):
        with pytest.raises(OperationNotEnabled):
            call()
    assert world.read_requests() == 0
    assert not any(s.usable for s in closed.probe_capabilities().states.values())


def test_a_write_without_a_server_write_time_is_a_test_setup_error() -> None:
    world = MemoryWorld()
    issue = world.add_issue("one", at=at(2))
    tracker = MemoryTracker(world, enabled=READ_OPERATIONS | {Operation.COMMENT_CREATE})
    with pytest.raises(RuntimeError, match="set_write_time"):
        tracker.post_comment(
            issue, "hi", marker=MARKER, grant=grant(Operation.COMMENT_CREATE, issue)
        )


def test_get_refuses_a_change_request_reached_through_an_issue_url() -> None:
    world = MemoryWorld()
    change = world.add_change_request("cr", at=at(2))
    tracker = MemoryTracker(world)
    key = tracker.resolve(world.issue_url(int(change.internal_id or 0)))
    with pytest.raises(ChangeRequestUrl):
        tracker.get(key)


def test_a_transferred_issue_leaves_the_listing_and_answers_its_old_key_with_the_new_one() -> None:
    """``MemoryWorld.transfer_issue`` (the minimal model of a tracker transfer, bh-ukesf): the
    issue is no longer in this project's listings, but its old key still reads and answers with
    the canonical key of the other project, which this project's resolver then refuses."""
    world = MemoryWorld()
    kept = world.add_issue("stays", at=at(1))
    moved = world.add_issue("moves", at=at(2))
    tracker = MemoryTracker(world)

    new_key = world.transfer_issue(moved, to="acme/elsewhere", at=at(3))

    assert (
        new_key.url == "memory://acme/elsewhere/issues/2" and new_key.identifier == moved.identifier
    )
    assert [i.key for i in tracker.list_issues(IssueQuery()).items] == [kept]
    assert tracker.list_ids(IdQuery(IdSubject.ISSUES)).ids == (kept.url,)
    assert tracker.get(moved).key == new_key
    with pytest.raises(OtherProject):
        tracker.resolve(new_key.url)
    assert tracker.get(kept).key == kept  # an untouched issue still needs its exact key
    assert world.write_requests() == 0


def test_resolver_refusals_do_not_echo_the_pasted_url() -> None:
    world = MemoryWorld()
    tracker = MemoryTracker(world)
    pasted = "memory://acme/widgets/wiki/ghp_notarealtoken0000"
    with pytest.raises(UrlNotRecognized) as caught:
        tracker.resolve(pasted)
    assert "ghp_" not in str(caught.value)


def test_k1_default_is_applied_and_not_applied_stores_nothing() -> None:
    for applied, stored in ((True, 1), (False, 0)):
        world = MemoryWorld()
        issue = world.add_issue("one", at=at(2))
        world.set_write_time(at(3))
        tracker = MemoryTracker(world, enabled=READ_OPERATIONS | {Operation.COMMENT_CREATE})
        if applied:
            world.fail_next_write_ambiguously()
        else:
            world.fail_next_write_ambiguously(applied=False)
        with pytest.raises(WriteOutcomeUnknown):
            tracker.post_comment(
                issue, "hi", marker=MARKER, grant=grant(Operation.COMMENT_CREATE, issue)
            )
        assert (world.write_requests(), world.comment_count(issue)) == (1, stored)
        assert world.ambiguous_next_write is None  # one-shot


def test_a_define_that_changes_nothing_sends_no_write_and_keeps_a_pending_ambiguity() -> None:
    """A no-op define is converged, not a request: it does not consume the one-shot K1 flag."""
    world = MemoryWorld()
    world.seed_label_definition(LabelDefinition("bh:accepted", "0e8a16", "Accepted"))
    tracker = MemoryTracker(world, enabled=READ_OPERATIONS | {Operation.LABELS_DEFINE})
    project = tracker.describe().project
    world.fail_next_write_ambiguously(applied=False)
    receipt = tracker.define_labels(
        (LabelDefinition("bh:accepted", "0e8a16", "Accepted"),),
        grant=grant(Operation.LABELS_DEFINE, project),
    )
    assert receipt.target == project and world.write_requests() == 0
    assert world.ambiguous_next_write is False  # still armed for the next real write


# ---- mutation controls: each broken variant is rejected by its named case -------------------


class _ExclusiveSince(MemoryTracker):
    def _issues(self, since, include_closed):
        rows = super()._issues(since, include_closed)
        return [r for r in rows if since is None or r.updated > since]


class _HidesClosed(MemoryTracker):
    def _issues(self, since, include_closed):
        return super()._issues(since, include_closed=False)


class _KeepsChangeRequestComments(MemoryTracker):
    def _comments(self, since, issue):
        rows = [
            c
            for c in self.world.comments.values()
            if (issue is None or c.issue == issue) and (since is None or c.updated >= since)
        ]
        return sorted(rows, key=lambda c: (c.updated, c.key.id))


class _ReusableGrants(MemoryTracker):
    def _authorise(self, op, issue, grant):
        super()._authorise(op, issue, grant)
        self._used_grants.clear()


class _IgnoresEnabled(MemoryTracker):
    def _require(self, operation):
        return None


class _RetriesAmbiguousWrite(MemoryTracker):
    def post_comment(self, issue, body, *, marker, grant):
        try:
            return super().post_comment(issue, body, marker=marker, grant=grant)
        except WriteOutcomeUnknown:
            self._used_grants.discard(grant.grant_id)
            return super().post_comment(issue, body, marker=marker, grant=grant)


class _AppliesAnyway(MemoryTracker):
    def post_comment(self, issue, body, *, marker, grant):
        if self.world.ambiguous_next_write is False:
            self.world.ambiguous_next_write = True
        return super().post_comment(issue, body, marker=marker, grant=grant)


class _RefundsAmbiguousGrant(MemoryTracker):
    def post_comment(self, issue, body, *, marker, grant):
        try:
            return super().post_comment(issue, body, marker=marker, grant=grant)
        except WriteOutcomeUnknown:
            self._used_grants.discard(grant.grant_id)
            raise


class _NoLabelOwnership(MemoryTracker):
    def change_labels(self, issue, *, add, remove, ownership, grant):
        everything = LabelOwnership(prefixes=("",))
        return super().change_labels(
            issue, add=add, remove=remove, ownership=everything, grant=grant
        )


class _NoSingleWaitCap(MemoryTracker):
    def _request(self, *, write=False):
        budget = self._budget
        self._budget = type(budget)(
            budget.max_retries, budget.max_total_wait, budget.max_total_wait
        )
        try:
            super()._request(write=write)
        finally:
            self._budget = budget


class _AcceptsForeignUrls(MemoryTracker):
    def resolve(self, url):
        return super().resolve(url.replace("someone-else/elsewhere", self.world.locator))


class _KeepsFragments(MemoryTracker):
    def resolve(self, url):
        key = super().resolve(url)
        return IssueKey(key.identifier, url, internal_id=key.internal_id)


class _ResolveSendsARequest(MemoryTracker):
    def resolve(self, url):
        self._request()
        return super().resolve(url)


class _GetSkipsTheRead(MemoryTracker):
    """Answers ``get`` from a local copy without the one read the contract promises."""

    def _request(self, *, write=False):
        if getattr(self, "_in_get", False):
            return None
        return super()._request(write=write)

    def get(self, key):
        self._in_get = True
        try:
            return super().get(key)
        finally:
            self._in_get = False


class _SnapshotListsNothing(MemoryTracker):
    def list_ids(self, query):
        page = super().list_ids(query)
        return type(page)(page.ids[:1], page.next_cursor)


class _DefinesForeignLabels(MemoryTracker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ownership = LabelOwnership(prefixes=("",))


class _DefinesPartially(MemoryTracker):
    """Applies each definition as it goes, so a foreign name leaves the owned ones behind."""

    def define_labels(self, definitions, *, grant):
        for n, definition in enumerate(definitions):
            if not self._ownership.owns(definition.name):
                raise LabelNotOwned(f"outside the owned set: {definition.name}")
            one = dataclasses.replace(grant, grant_id=f"{grant.grant_id}-{n}")
            super().define_labels((definition,), grant=one)
        return WriteReceipt(grant.grant_id, grant.operation, self.describe().project)


class _DefineIgnoresGrantTarget(MemoryTracker):
    def define_labels(self, definitions, *, grant):
        widened = dataclasses.replace(grant, target=self.describe().project)
        return super().define_labels(definitions, grant=widened)


class _DefineIgnoresEnabled(MemoryTracker):
    def _require(self, operation):
        if operation is not Operation.LABELS_DEFINE:
            super()._require(operation)


class _DefineIsNotIdempotent(MemoryTracker):
    def define_labels(self, definitions, *, grant):
        before = dict(self.world.label_defs)
        receipt = super().define_labels(definitions, grant=grant)
        if self.world.label_defs == before:
            self.world.writes += 1  # sends the write again for a definition it already holds
        return receipt


class _DefineNeverUpdates(MemoryTracker):
    def define_labels(self, definitions, *, grant):
        fresh = tuple(d for d in definitions if d.name not in self.world.label_defs)
        return super().define_labels(fresh, grant=grant)


class _DefineRetriesAmbiguousWrite(MemoryTracker):
    def define_labels(self, definitions, *, grant):
        try:
            return super().define_labels(definitions, grant=grant)
        except WriteOutcomeUnknown:
            self._used_grants.discard(grant.grant_id)
            return super().define_labels(definitions, grant=grant)


class _DefineAppliesAnyway(MemoryTracker):
    def define_labels(self, definitions, *, grant):
        if self.world.ambiguous_next_write is False:
            self.world.ambiguous_next_write = True
        return super().define_labels(definitions, grant=grant)


class _DefineRefundsAmbiguousGrant(MemoryTracker):
    def define_labels(self, definitions, *, grant):
        try:
            return super().define_labels(definitions, grant=grant)
        except WriteOutcomeUnknown:
            self._used_grants.discard(grant.grant_id)
            raise


class _DeletesForeignLabels(MemoryTracker):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._ownership = LabelOwnership(prefixes=("",))


class _DeletesPartially(MemoryTracker):
    """Deletes each name as it goes, so a foreign name leaves the owned ones deleted."""

    def delete_labels(self, names, *, grant):
        for n, name in enumerate(names):
            if not self._ownership.owns(name):
                raise LabelNotOwned(f"outside the owned set: {name}")
            one = dataclasses.replace(grant, grant_id=f"{grant.grant_id}-{n}")
            super().delete_labels((name,), grant=one)
        return WriteReceipt(grant.grant_id, grant.operation, self.describe().project)


class _DeleteIgnoresGrantTarget(MemoryTracker):
    def delete_labels(self, names, *, grant):
        widened = dataclasses.replace(grant, target=self.describe().project)
        return super().delete_labels(names, grant=widened)


class _DeleteIgnoresEnabled(MemoryTracker):
    def delete_labels(self, names, *, grant):
        self._enabled = self._enabled | {Operation.LABELS_DEFINE}  # as if it were always on
        return super().delete_labels(names, grant=grant)


class _DeleteOfAbsentRaises(MemoryTracker):
    def delete_labels(self, names, *, grant):
        missing = [name for name in names if name not in self.world.label_defs]
        if missing:
            raise NotVisible(f"no such label: {', '.join(missing)}")
        return super().delete_labels(names, grant=grant)


class _DeleteOfAbsentSendsAWrite(MemoryTracker):
    def delete_labels(self, names, *, grant):
        receipt = super().delete_labels(names, grant=grant)
        if not any(name in self.world.label_defs for name in names):
            self.world.writes += 1  # sends the delete again for a label that is already gone
        return receipt


class _DeleteOfAbsentKeepsGrant(MemoryTracker):
    def delete_labels(self, names, *, grant):
        if not any(name in self.world.label_defs for name in names):
            return WriteReceipt(grant.grant_id, grant.operation, self.describe().project)
        return super().delete_labels(names, grant=grant)


class _ListsOnlyOwnedDefinitions(MemoryTracker):
    def list_label_definitions(self):
        return tuple(d for d in super().list_label_definitions() if self._ownership.owns(d.name))


class _ProbeEnablesLabelsDefine(MemoryTracker):
    def probe_capabilities(self):
        report = super().probe_capabilities()
        states = dict(report.states)
        states[Operation.LABELS_DEFINE] = dataclasses.replace(
            states[Operation.LABELS_DEFINE], enabled=True
        )
        return dataclasses.replace(report, states=states)


def _variant_factory(cls: type[MemoryTracker]):
    def factory(*, enabled=frozenset(), page_size=2, budget=None) -> TrackerSubject:
        world = MemoryWorld()
        adapter = cls(world, enabled=READ_OPERATIONS | enabled, page_size=page_size, budget=budget)
        return TrackerSubject(adapter, world)

    return factory


_CASES = {case.case_id: case for case in TRACKER_CASES}


@pytest.mark.parametrize(
    ("variant", "case_id"),
    [
        (_ExclusiveSince, "issues.checkpoint-incremental"),
        (_HidesClosed, "issues.closed-included"),
        (_KeepsChangeRequestComments, "issues.change-requests-excluded"),
        (_ReusableGrants, "write.grant-matches-and-single-use"),
        (_IgnoresEnabled, "write.refused-when-not-enabled"),
        (_RetriesAmbiguousWrite, "write.ambiguous-not-retried"),
        (_AppliesAnyway, "write.ambiguous-not-applied"),
        (_RefundsAmbiguousGrant, "write.ambiguous-not-applied"),
        (_NoLabelOwnership, "labels.only-owned"),
        (_NoSingleWaitCap, "rate-limit.wait-cap"),
        (_AcceptsForeignUrls, "resolver.refusals"),
        (_KeepsFragments, "resolver.resolve-is-pure-and-canonical"),
        (_ResolveSendsARequest, "resolver.resolve-is-pure-and-canonical"),
        (_GetSkipsTheRead, "resolver.get-reads-one-issue"),
        (_SnapshotListsNothing, "comments.deletion-needs-id-snapshot"),
        (_ProbeEnablesLabelsDefine, "capability.default-read-only"),
        (_ListsOnlyOwnedDefinitions, "labels.read-lists-every-definition"),
        (_DefinesForeignLabels, "labels.define-only-owned"),
        (_DefinesPartially, "labels.define-only-owned"),
        (_DefineIgnoresGrantTarget, "labels.define-grant-matches-and-single-use"),
        (_ReusableGrants, "labels.define-grant-matches-and-single-use"),
        (_DefineIgnoresEnabled, "labels.define-refused-when-not-enabled"),
        (_IgnoresEnabled, "labels.define-refused-when-not-enabled"),
        (_DefineIsNotIdempotent, "labels.define-is-idempotent"),
        (_DefineNeverUpdates, "labels.define-is-idempotent"),
        (_DefineRetriesAmbiguousWrite, "labels.define-ambiguous-not-retried"),
        (_DefineRefundsAmbiguousGrant, "labels.define-ambiguous-not-retried"),
        (_DefineAppliesAnyway, "labels.define-ambiguous-not-applied"),
        (_DefineRefundsAmbiguousGrant, "labels.define-ambiguous-not-applied"),
        (_DeletesForeignLabels, "labels.delete-only-owned"),
        (_DeletesPartially, "labels.delete-only-owned"),
        (_DeleteIgnoresGrantTarget, "labels.delete-grant-matches-and-single-use"),
        (_ReusableGrants, "labels.delete-grant-matches-and-single-use"),
        (_DeleteIgnoresEnabled, "labels.delete-refused-when-not-enabled"),
        (_IgnoresEnabled, "labels.delete-refused-when-not-enabled"),
        (_DeleteOfAbsentRaises, "labels.delete-already-gone-succeeds"),
        (_DeleteOfAbsentSendsAWrite, "labels.delete-already-gone-succeeds"),
        (_DeleteOfAbsentKeepsGrant, "labels.delete-already-gone-succeeds"),
    ],
    ids=lambda value: value if isinstance(value, str) else value.__name__.lstrip("_"),
)
def test_broken_variant_is_rejected_by_its_case(variant: type[MemoryTracker], case_id: str) -> None:
    with pytest.raises(ConformanceFailure) as caught:
        run_tracker_case(_variant_factory(variant), _CASES[case_id], subject=variant.__name__)
    assert caught.value.case_id == case_id


def test_whole_suite_runner_names_the_first_failing_case() -> None:
    with pytest.raises(ConformanceFailure, match=r"semantic-case=write\.refused-when-not-enabled"):
        assert_tracker_conforms(_variant_factory(_IgnoresEnabled), subject="ignores-enabled")


def test_kit_module_exposes_no_private_world_rows() -> None:
    assert set(memory_module.__all__) == {"MemoryTracker", "without_resolver"}
