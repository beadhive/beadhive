"""``bh work import``: the import operation (I4) on ``MemoryTracker`` and ``MemoryReportStore``.

Refusals R0 to R8 of external-intake-module-adr.md section 5 each have a case with their distinct
code, on both the resolver path (``MemoryTracker`` implements ``IssueResolver``) and the fallback
that pages ``list_issues`` (``without_resolver``), where R3 to R7 collapse into the documented
``NOT_FOUND_IN_PROJECT``. Every case asserts what the ADR demands of a refusal: zero store writes,
and zero tracker write requests (import holds no ``WriteGrant`` and calls no writer). Explicit
``at=`` tracker timestamps, an in-process lock and a deterministic store: no case reads the host
clock, sleeps or opens a socket.
"""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules.intake import (
    INTAKE_UNTRIAGED,
    ORIGIN_GITHUB,
    Disposition,
    DispositionRequest,
    ImportKind,
    ImportRequest,
    IntakeRefused,
    IntakeService,
    IntakeStoreError,
    IssueImporter,
    IssueStatus,
    RefusalReason,
    ReportDraft,
    SourceSnapshot,
)
from beadhive.modules.tracker.contracts import (
    AuthenticationFailed,
    IssueKey,
    IssueState,
    Operation,
    TransientFailure,
)
from beadhive.testing.intake import URL, InProcessIntakeLock, MemoryReportStore, memory_store
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at, without_resolver

ACTOR = "dev/importer"
BINDING_KEY = "memory:acme/widgets"


class _Labels:
    def __init__(self, *bad: str) -> None:
        self.bad = set(bad)

    def violations(self, labels):
        return tuple(f"unknown label {label}" for label in labels if label in self.bad)


class _ReadOnly:
    """A tracker that exposes ONLY the read half of the port: an import that reached a writer (or
    anything else) would raise ``AttributeError``."""

    def __init__(self, inner) -> None:
        self._inner = inner

    @property
    def world(self) -> MemoryWorld:
        return self._inner.world

    def describe(self):
        return self._inner.describe()

    def list_issues(self, query):
        return self._inner.list_issues(query)


class _ReadOnlyResolving(_ReadOnly):
    """The read half plus the optional ``IssueResolver`` pair."""

    def resolve(self, url):
        return self._inner.resolve(url)

    def get(self, key):
        return self._inner.get(key)


@dataclasses.dataclass
class Rig:
    world: MemoryWorld
    tracker: object
    store: MemoryReportStore
    lock: InProcessIntakeLock
    service: IntakeService

    def issue(self, title: str = "Login is broken", *, day: int = 1, **kwargs) -> IssueKey:
        return self.world.add_issue(title, at=at(day), **kwargs)

    def import_(self, reference: str, *, dry_run: bool = False):
        return self.service.import_issue(ImportRequest(reference, ACTOR, dry_run=dry_run))

    def refused(self, reference: str, *, dry_run: bool = False) -> IntakeRefused:
        with pytest.raises(IntakeRefused) as refused:
            self.import_(reference, dry_run=dry_run)
        return refused.value

    def assert_untouched(self) -> None:
        """What every refusal and every dry run leaves: nothing written, nothing sent."""
        assert self.store.writes == 0
        assert self.store.bound_reports() == ()
        assert self.world.write_requests() == 0
        assert self.lock.holds == [] or all(held == BINDING_KEY for held in self.lock.holds)


def _rig(*, resolver: bool = True, enabled=None, labels=None, default_type="bug") -> Rig:
    world = MemoryWorld()
    memory = MemoryTracker(world, **({} if enabled is None else {"enabled": enabled}))
    tracker = _ReadOnlyResolving(memory) if resolver else _ReadOnly(without_resolver(memory))
    store, lock = memory_store(), InProcessIntakeLock()
    importer = IssueImporter(
        store, labels or _Labels(), tracker, lock, origin=ORIGIN_GITHUB, default_type=default_type
    )
    return Rig(world, tracker, store, lock, IntakeService(store, _Labels(), importer=importer))


@pytest.fixture
def rig() -> Rig:
    return _rig()


@pytest.fixture(params=[True, False], ids=["resolver", "fallback"])
def either(request) -> Rig:
    """The same scenario on a tracker with and without the optional ``IssueResolver``."""
    return _rig(resolver=request.param)


# -- the happy path: a held, untriaged report --


def test_import_lands_a_held_untriaged_report_with_the_github_origin(either: Rig) -> None:
    key = either.issue()

    outcome = either.import_(key.url)

    assert outcome.kind is ImportKind.CREATED and not outcome.dry_run
    report = outcome.binding.report
    view = either.store.get(report)
    assert view is not None
    assert INTAKE_UNTRIAGED in view.labels and ORIGIN_GITHUB in view.labels
    assert view.external_ref == key.url == outcome.binding.url
    assert view.binding is not None and view.binding.project == "acme/widgets"
    assert (view.issue_type, view.title, view.status) == ("bug", "Login is broken", "open")
    # held (i3): an open intake-hold gate blocks it, so it is never ready work
    assert [hold.blocks for hold in either.store.holds()] == [(report,)]
    assert either.store.ready_ids() == ()
    assert view.snapshot is not None and view.snapshot.state is IssueStatus.OPEN
    assert either.world.write_requests() == 0  # no tracker write


def test_the_create_order_is_binding_then_hold_then_origin_then_intake(rig: Rig) -> None:
    key = rig.issue()

    report = rig.import_(key.url).binding.report

    assert rig.store.events()[0][0] == "open_hold"  # the hold comes right after the create
    assert [(dim, value) for dim, value, _ in rig.store.history(report)] == [
        ("origin", "github"),
        ("intake", "untriaged"),
    ]


def test_importing_the_same_url_twice_updates_and_never_duplicates(either: Rig) -> None:
    key = either.issue()

    first = either.import_(key.url)
    writes = either.store.writes
    second = either.import_(key.url)

    assert (first.kind, second.kind) == (ImportKind.CREATED, ImportKind.UNCHANGED)
    assert second.binding.report == first.binding.report
    assert either.store.writes == writes  # an unchanged revision writes nothing at all
    assert len(either.store.find_by_external_ref(key.url)) == 1
    assert len(either.store.bound_reports()) == 1


def test_every_pasted_spelling_of_the_url_keys_the_same_report(rig: Rig) -> None:
    key = rig.issue()
    first = rig.import_(key.url).binding.report

    for spelling in rig.world.url_variants(key):
        outcome = rig.import_(spelling)
        assert (outcome.kind, outcome.binding.report) == (ImportKind.UNCHANGED, first)

    assert len(rig.store.bound_reports()) == 1


def test_the_fallback_canonicalises_only_the_generic_url_noise() -> None:
    rig = _rig(resolver=False)
    key = rig.issue()
    first = rig.import_(key.url).binding.report

    for spelling in (f"{key.url}/", f"{key.url}?view=full", f"{key.url}#c1"):
        assert rig.import_(spelling).binding.report == first
    assert len(rig.store.bound_reports()) == 1


def test_a_closed_issue_imports_as_an_open_held_report(either: Rig) -> None:
    key = either.issue("Old crash", state=IssueState.CLOSED)

    outcome = either.import_(key.url)

    view = either.store.get(outcome.binding.report)
    assert view is not None and view.status == "open"  # the bead is internal triage, not the issue
    assert view.snapshot is not None and view.snapshot.state is IssueStatus.CLOSED
    assert INTAKE_UNTRIAGED in view.labels
    assert either.store.ready_ids() == () and len(either.store.holds()) == 1


def test_an_upstream_close_or_edit_is_a_snapshot_update_only(rig: Rig) -> None:
    key = rig.issue()
    report = rig.import_(key.url).binding.report
    rig.store.set_state(report, "priority", "p1", reason="triage note", actor="dev/triage")
    before = rig.store.get(report)
    holds = rig.store.holds()
    rig.world.update_issue(key, at=at(5), state=IssueState.CLOSED)
    writes = rig.store.writes

    outcome = rig.import_(key.url)

    assert outcome.kind is ImportKind.UPDATED and outcome.repaired == ()
    after = rig.store.get(report)
    assert after is not None and before is not None
    assert after.snapshot is not None and after.snapshot.state is IssueStatus.CLOSED
    assert rig.store.writes == writes + 1  # exactly the snapshot
    assert (after.title, after.labels, after.status, after.issue_type) == (
        before.title,
        before.labels,
        before.status,
        before.issue_type,
    )
    assert rig.store.holds() == holds


def test_a_reimport_never_resets_triage(rig: Rig) -> None:
    key = rig.issue()
    report = rig.import_(key.url).binding.report
    rig.service.dispose(DispositionRequest(report, Disposition.ACCEPT, ACTOR))
    accepted = rig.store.get(report)
    rig.world.update_issue(key, at=at(7))

    outcome = rig.import_(key.url)

    assert outcome.kind is ImportKind.UPDATED
    after = rig.store.get(report)
    assert after is not None and accepted is not None
    assert "intake:accepted" in after.labels and INTAKE_UNTRIAGED not in after.labels
    assert after.labels == accepted.labels  # no stamp was repeated


def test_a_stale_observation_does_not_roll_the_snapshot_back(rig: Rig) -> None:
    key = rig.issue(day=9)
    report = rig.import_(key.url).binding.report
    stored = rig.store.get(report)
    assert stored is not None and stored.snapshot is not None
    newer = dataclasses.replace(stored.snapshot, revision="2026-02-01T00:00:00+00:00")
    rig.store.record_snapshot(report, newer, actor=ACTOR)
    writes = rig.store.writes

    outcome = rig.import_(
        key.url
    )  # the tracker's revision (Jan 9) is older than the stored (Feb 1)

    assert outcome.kind is ImportKind.UNCHANGED and rig.store.writes == writes


def test_the_configured_default_type_files_the_report() -> None:
    rig = _rig(default_type="feature")
    key = rig.issue()

    report = rig.import_(key.url).binding.report

    view = rig.store.get(report)
    assert view is not None and view.issue_type == "feature"


# -- repair: a found report is completed for the missing piece only --


def _bound(store: MemoryReportStore, key: IssueKey, *, snapshot: bool = True) -> str:
    """A bound report as an interrupted earlier import left it: created (hold and binding), but
    not yet stamped."""
    from beadhive.modules.intake import IssueBinding

    return store.create(
        ReportDraft(
            "Login is broken",
            "bug",
            ORIGIN_GITHUB,
            ACTOR,
            binding=IssueBinding("", "memory", "acme/widgets", key.url, key.identifier),
            snapshot=SourceSnapshot("t", "b", IssueStatus.OPEN, "2026-01-01T00:00:00+00:00")
            if snapshot
            else None,
        )
    )


def test_an_interrupted_import_is_repaired_for_the_missing_stamps_only(rig: Rig) -> None:
    key = rig.issue(day=1)
    report = _bound(rig.store, key)  # created and held, never stamped
    rig.world.update_issue(key, at=at(1))  # same revision as the stored snapshot

    outcome = rig.import_(key.url)

    assert (outcome.kind, outcome.binding.report) == (ImportKind.UPDATED, report)
    assert outcome.repaired == ("origin", "intake")
    view = rig.store.get(report)
    assert view is not None and {ORIGIN_GITHUB, INTAKE_UNTRIAGED} <= set(view.labels)
    assert len(rig.store.holds()) == 1  # the hold it was born with is not opened twice
    assert rig.import_(key.url).kind is ImportKind.UNCHANGED


def test_a_missing_hold_is_reopened_and_a_present_disposition_is_kept(rig: Rig) -> None:
    key = rig.issue()
    report = rig.import_(key.url).binding.report
    rig.service.dispose(DispositionRequest(report, Disposition.ACCEPT, ACTOR))
    rig.store.lose_hold(rig.store.holds()[0].gate)  # a person deleted the blocking edge by hand
    assert report in rig.store.ready_ids()

    outcome = rig.import_(key.url)

    assert outcome.repaired == ("hold",)
    assert rig.store.ready_ids() == () and any(report in h.blocks for h in rig.store.holds())
    view = rig.store.get(report)
    assert view is not None and "intake:accepted" in view.labels  # never overwritten


def test_a_missing_snapshot_is_recorded(rig: Rig) -> None:
    key = rig.issue()
    report = _bound(rig.store, key, snapshot=False)
    rig.store.set_state(report, "origin", "github", reason="x", actor=ACTOR)
    rig.store.set_state(report, "intake", "untriaged", reason="x", actor=ACTOR)

    outcome = rig.import_(key.url)

    assert outcome.repaired == ("source",)
    view = rig.store.get(report)
    assert view is not None and view.snapshot is not None


def test_a_closed_report_is_never_given_a_new_hold(rig: Rig) -> None:
    key = rig.issue()
    report = rig.import_(key.url).binding.report
    rig.service.dispose(DispositionRequest(report, Disposition.REJECT, ACTOR, reason="not ours"))
    assert rig.store.holds() == ()
    rig.world.update_issue(key, at=at(3))

    outcome = rig.import_(key.url)

    assert outcome.kind is ImportKind.UPDATED and outcome.repaired == ()
    assert rig.store.holds() == ()  # a closed report is never ready, so it needs no hold


# -- dry run: classification only, zero store writes --


def test_a_dry_run_reports_what_it_would_do_and_writes_nothing(either: Rig) -> None:
    key = either.issue()

    would_create = either.import_(key.url, dry_run=True)

    assert (would_create.kind, would_create.dry_run) == (ImportKind.CREATED, True)
    assert would_create.binding.report == "" and would_create.binding.url == key.url
    either.assert_untouched()
    assert either.lock.holds == []  # a dry run takes no lock: it only reads

    report = either.import_(key.url).binding.report
    writes = either.store.writes
    assert either.import_(key.url, dry_run=True).kind is ImportKind.UNCHANGED
    either.world.update_issue(key, at=at(4))
    would_update = either.import_(key.url, dry_run=True)
    assert (would_update.kind, would_update.binding.report) == (ImportKind.UPDATED, report)
    assert either.store.writes == writes


def test_a_dry_run_names_the_repairs_without_making_them(rig: Rig) -> None:
    key = rig.issue()
    report = _bound(rig.store, key)
    writes = rig.store.writes

    outcome = rig.import_(key.url, dry_run=True)

    assert (outcome.kind, outcome.repaired) == (ImportKind.UPDATED, ("origin", "intake"))
    assert outcome.binding.report == report and rig.store.writes == writes


@pytest.mark.parametrize("dry_run", [False, True], ids=["import", "dry-run"])
def test_every_refusal_is_the_same_for_a_dry_run(rig: Rig, dry_run: bool) -> None:
    assert rig.refused("pub-12a", dry_run=dry_run).reason is RefusalReason.PUBLIC_STORE_ID
    assert rig.refused(rig.world.foreign_issue_url(), dry_run=dry_run).reason is (
        RefusalReason.OTHER_PROJECT
    )
    rig.assert_untouched()


# -- R0: intake off, no tracker bound, or issues.read not usable --


def test_r0_no_bound_tracker_is_intake_disabled_and_writes_nothing() -> None:
    store = memory_store()
    service = IntakeService(store, _Labels())  # no importer: the root bound no tracker

    with pytest.raises(IntakeRefused) as refused:
        service.import_issue(ImportRequest(URL, ACTOR))

    assert refused.value.reason is RefusalReason.INTAKE_DISABLED
    assert "tracker" in refused.value.detail and store.writes == 0


@pytest.mark.parametrize("resolver", [True, False], ids=["resolver", "fallback"])
def test_r0_issues_read_not_enabled_is_intake_disabled(resolver: bool) -> None:
    rig = _rig(resolver=resolver, enabled=frozenset({Operation.COMMENTS_READ}))
    key = rig.issue()

    refused = rig.refused(key.url)

    assert refused.reason is RefusalReason.INTAKE_DISABLED
    assert "issues.read" in refused.detail
    rig.assert_untouched()


# -- R1, R2: refused from the text alone, before any tracker request --


def test_r1_a_public_store_id_is_refused_without_a_tracker_request(either: Rig) -> None:
    either.issue()

    refused = either.refused("pub-12a")

    assert refused.reason is RefusalReason.PUBLIC_STORE_ID
    assert "issue URL" in refused.detail
    either.assert_untouched()
    assert either.world.read_requests() == 0


@pytest.mark.parametrize("text", ["acme-12a", "bh-wjvq9", "#12", "12", "not a url", ""])
def test_r2_anything_that_is_not_a_url_is_refused_without_a_tracker_request(
    either: Rig, text: str
) -> None:
    refused = either.refused(text)

    assert refused.reason is RefusalReason.NOT_A_URL
    either.assert_untouched()
    assert either.world.read_requests() == 0


# -- R3 to R7: the resolver's distinct codes, and the fallback's one collapse --


def test_r3_a_url_the_grammar_does_not_recognise_is_unknown(rig: Rig) -> None:
    refused = rig.refused(rig.world.unrecognized_url())

    assert refused.reason is RefusalReason.UNKNOWN_URL
    rig.assert_untouched()
    assert rig.world.read_requests() == 0  # resolve is pure


def test_r4_an_issue_of_another_project_is_refused_without_a_request(rig: Rig) -> None:
    refused = rig.refused(rig.world.foreign_issue_url())

    assert refused.reason is RefusalReason.OTHER_PROJECT
    rig.assert_untouched()
    assert rig.world.read_requests() == 0  # resolve is pure


def test_r5_a_change_request_url_is_refused_without_a_request(rig: Rig) -> None:
    pull = rig.world.add_change_request("Fix login", at=at(2))

    refused = rig.refused(pull.url)

    assert refused.reason is RefusalReason.CHANGE_REQUEST
    rig.assert_untouched()
    assert rig.world.read_requests() == 0


def test_r5_an_issues_url_the_tracker_answers_as_a_change_request_is_refused(rig: Rig) -> None:
    pull = rig.world.add_change_request("Fix login", at=at(2))
    as_issue = pull.url.replace("/changes/", "/issues/")

    refused = rig.refused(as_issue)

    assert refused.reason is RefusalReason.CHANGE_REQUEST
    rig.assert_untouched()
    assert rig.world.read_requests() == 1  # the one read that told


def test_r6_an_issue_missing_or_hidden_from_the_token_is_not_visible(rig: Rig) -> None:
    key = rig.issue()
    gone = rig.world.issue_url(99)

    refused = rig.refused(gone)

    assert key.url != gone and refused.reason is RefusalReason.NOT_VISIBLE
    rig.assert_untouched()


def test_r7_a_transferred_issue_is_other_project_after_the_read(rig: Rig) -> None:
    key = rig.issue()
    rig.world.transfer_issue(key, to="acme/elsewhere", at=at(3))

    refused = rig.refused(key.url)

    assert refused.reason is RefusalReason.OTHER_PROJECT
    assert "transferred" in refused.detail  # distinct from R4's wording
    rig.assert_untouched()


def test_r7_a_transfer_after_import_is_refused_and_the_report_is_untouched(rig: Rig) -> None:
    key = rig.issue()
    report = rig.import_(key.url).binding.report
    writes = rig.store.writes
    rig.world.transfer_issue(key, to="acme/elsewhere", at=at(3))

    assert rig.refused(key.url).reason is RefusalReason.OTHER_PROJECT

    assert rig.store.writes == writes and rig.store.get(report) is not None


@pytest.fixture
def fallback() -> Rig:
    return _rig(resolver=False)


def test_without_a_resolver_r3_to_r7_collapse_into_not_found_in_project(fallback: Rig) -> None:
    world = fallback.world
    transferred = fallback.issue("Moved")
    world.transfer_issue(transferred, to="acme/elsewhere", at=at(3))
    pull = world.add_change_request("Fix login", at=at(2))
    cases = {
        "R3": world.unrecognized_url(),
        "R4": world.foreign_issue_url(),
        "R5": pull.url,
        "R5b": pull.url.replace("/changes/", "/issues/"),
        "R6": world.issue_url(99),
        "R7": transferred.url,
    }

    for label, url in cases.items():
        refused = fallback.refused(url)
        assert refused.reason is RefusalReason.NOT_FOUND_IN_PROJECT, label
    fallback.assert_untouched()


def test_the_fallback_pages_every_page_and_finds_a_closed_issue() -> None:
    world = MemoryWorld()
    tracker = _ReadOnly(without_resolver(MemoryTracker(world, page_size=2)))
    store, lock = memory_store(), InProcessIntakeLock()
    service = IntakeService(
        store, _Labels(), importer=IssueImporter(store, _Labels(), tracker, lock)
    )
    keys = [world.add_issue(f"issue {n}", at=at(n + 1)) for n in range(5)]
    last = world.add_issue("closed last", at=at(9), state=IssueState.CLOSED)

    outcome = service.import_issue(ImportRequest(last.url, ACTOR))

    assert outcome.kind is ImportKind.CREATED and len(keys) == 5
    assert world.read_requests() == 3  # three pages of two, found on the last
    assert world.write_requests() == 0


# -- R8: a duplicate binding is refused and names the ids --


def test_r8_two_beads_carrying_the_url_are_refused_naming_both(either: Rig) -> None:
    key = either.issue()
    first, second = _bound(either.store, key), _bound(either.store, key)
    writes = either.store.writes

    refused = either.refused(key.url)

    assert refused.reason is RefusalReason.DUPLICATE_BINDING
    assert first in refused.detail and second in refused.detail
    assert either.store.writes == writes  # nothing written, nothing auto-closed
    assert {view.status for view in either.store.bound_reports()} == {"open"}
    assert either.refused(key.url, dry_run=True).reason is RefusalReason.DUPLICATE_BINDING


def test_r8_a_racing_create_on_a_replica_is_refused_without_closing_anything(rig: Rig) -> None:
    key = rig.issue()
    real_create = rig.store.create

    def racing(draft):
        real_create(draft)  # another host's create lands first
        return real_create(draft)

    rig.store.create = racing

    refused = rig.refused(key.url)

    assert refused.reason is RefusalReason.DUPLICATE_BINDING
    assert {view.status for view in rig.store.bound_reports()} == {"open"}


# -- the rest of the contract --


def test_the_binding_is_held_under_the_lock_for_a_mutating_import(rig: Rig) -> None:
    key = rig.issue()

    rig.import_(key.url)

    assert rig.lock.holds == [BINDING_KEY] and not rig.lock.held(BINDING_KEY)


def test_a_binding_held_elsewhere_is_busy_and_nothing_is_written(rig: Rig) -> None:
    key = rig.issue()

    with rig.lock.hold(BINDING_KEY):
        refused = rig.refused(key.url)

    assert refused.reason is RefusalReason.INTAKE_BUSY
    assert rig.store.writes == 0 and rig.store.bound_reports() == ()


def test_an_issue_that_would_carry_invalid_labels_is_refused_before_the_create() -> None:
    rig = _rig(labels=_Labels(INTAKE_UNTRIAGED))
    key = rig.issue()

    refused = rig.refused(key.url)

    assert refused.reason is RefusalReason.INVALID_LABELS
    assert rig.store.writes == 0


def test_a_misconfigured_default_type_is_refused_before_the_create() -> None:
    rig = _rig(default_type="epic")
    key = rig.issue()

    assert rig.refused(key.url).reason is RefusalReason.INVALID_TYPE
    assert rig.store.writes == 0


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (AuthenticationFailed("bad credential"), "AuthenticationFailed"),
        (TransientFailure("503"), "TransientFailure"),
    ],
)
def test_a_tracker_failure_is_a_tracker_error_and_writes_nothing(
    failure: Exception, expected: str
) -> None:
    rig = _rig(resolver=False)
    key = rig.issue()

    def broken(query):
        raise failure

    rig.tracker.list_issues = broken

    refused = rig.refused(key.url)

    assert refused.reason is RefusalReason.TRACKER_ERROR and expected in refused.detail
    rig.assert_untouched()


def test_a_failed_stamp_names_the_half_imported_report_and_leaves_it_held(rig: Rig) -> None:
    key = rig.issue()
    real = rig.store.set_state

    def failing(bead, dimension, value, *, reason, actor):
        if dimension == "intake":
            raise IntakeStoreError("bd said no", operation="set_state")
        real(bead, dimension, value, reason=reason, actor=actor)

    rig.store.set_state = failing

    with pytest.raises(IntakeStoreError) as failed:
        rig.import_(key.url)

    assert failed.value.operation == "stamp:intake" and failed.value.bead
    assert rig.store.ready_ids() == ()  # born held: never offered as work
    rig.store.set_state = real
    assert rig.import_(key.url).repaired == ("intake",)  # and the next import completes it


def test_import_never_calls_a_tracker_writer(rig: Rig) -> None:
    """The tracker handed to the importer exposes only reads; the world confirms no write."""
    key = rig.issue()

    rig.import_(key.url)
    rig.import_(key.url)
    rig.import_(key.url, dry_run=True)

    assert not hasattr(rig.tracker, "post_comment") and not hasattr(rig.tracker, "change_labels")
    assert rig.world.write_requests() == 0
