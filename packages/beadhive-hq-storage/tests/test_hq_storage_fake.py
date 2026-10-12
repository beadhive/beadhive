"""Fake-only behaviour the shared contract does not cover: the injected clock, fault plans,
test controls and protocol conformance."""

from __future__ import annotations

import copy
import pickle
from datetime import UTC, datetime, timedelta

import pytest

from beadhive_hq_storage import (
    ABSENT,
    Absent,
    AppendLog,
    Applied,
    BumpRequest,
    CapabilityHost,
    CapabilityUnsupported,
    CommittedHistory,
    Conflict,
    EpochRefStore,
    HiveFence,
    InMemoryEpochRef,
    InMemoryHiveFence,
    InMemoryHqStorage,
    IntegrityViolation,
    InvalidRequest,
    Liveness,
    LivenessKind,
    NotProvisioned,
    PlacementStore,
    RecordKind,
    RefView,
    Revision,
    StorageClock,
    StorageUnavailable,
    StreamKind,
    Unknown,
    VersionedRecords,
    new_operation_id,
    require_capability,
)
from beadhive_hq_storage.contract import ManualClock, operation_id


@pytest.fixture
def clock() -> ManualClock:
    return ManualClock()


@pytest.fixture
def store(clock: ManualClock) -> InMemoryHqStorage:
    return InMemoryHqStorage(clock)


def test_fakes_satisfy_every_protocol(clock: ManualClock, store: InMemoryHqStorage) -> None:
    for protocol in (VersionedRecords, AppendLog, Liveness, CommittedHistory, CapabilityHost):
        assert isinstance(store, protocol)
    assert isinstance(store.placements(), PlacementStore)
    assert isinstance(InMemoryHiveFence(clock), HiveFence)
    assert isinstance(InMemoryEpochRef(), EpochRefStore)
    assert isinstance(clock, StorageClock)


def test_every_stamp_comes_from_the_injected_clock(
    clock: ManualClock, store: InMemoryHqStorage
) -> None:
    clock.advance(timedelta(days=3650))  # far from wall time: any wall read would show
    assert store.now() == clock.now()
    assert store.renew(LivenessKind.SESSION, "f") == clock.now()
    op = new_operation_id()
    store.append(StreamKind.RECEIPT, "k", {"v": 1}, operation=op)
    assert store.lookup_append(StreamKind.RECEIPT, "k", op).recorded_at == clock.now()  # type: ignore[union-attr]
    clock.advance(timedelta(minutes=2))
    assert store.ages(LivenessKind.SESSION) == {"f": timedelta(minutes=2)}


def test_a_naive_clock_is_an_integrity_violation() -> None:
    class Naive:
        def now(self) -> datetime:
            return datetime(2026, 1, 1)

    with pytest.raises(IntegrityViolation):
        InMemoryHqStorage(Naive())


def test_manual_clock_never_runs_backwards(clock: ManualClock) -> None:
    clock.advance(1.5)
    assert clock.now() == datetime(2026, 1, 1, 0, 0, 1, 500000, tzinfo=UTC)
    with pytest.raises(ValueError):
        clock.advance(timedelta(seconds=-1))
    with pytest.raises(ValueError):
        ManualClock(datetime(2026, 1, 1))


def test_fault_plan_is_fifo_and_method_scoped(store: InMemoryHqStorage) -> None:
    store.faults.arm_unknown("deadline", applied=True, on="append")
    store.faults.arm_unavailable(on="compare_and_set")
    assert store.faults.pending == 2
    with pytest.raises(StorageUnavailable):
        store.compare_and_set(
            RecordKind.AUTHORITY, "k", {}, expected=ABSENT, operation=new_operation_id()
        )
    store.renew(LivenessKind.SESSION, "s")  # an Unknown never applies to a non-outcome call
    out = store.append(StreamKind.INBOX, "k", {}, operation=new_operation_id())
    assert isinstance(out, Unknown) and out.reason == "deadline"
    assert store.faults.pending == 0
    store.faults.arm_unknown(applied=False)
    store.faults.clear()
    assert store.faults.pending == 0
    with pytest.raises(ValueError):
        store.faults.arm_unknown("maybe", applied=True)  # type: ignore[arg-type]


def test_stored_bodies_are_private_copies(store: InMemoryHqStorage) -> None:
    body = {"nested": {"v": [1, 2]}}
    store.compare_and_set(
        RecordKind.AUTHORITY, "k", body, expected=ABSENT, operation=new_operation_id()
    )
    original = copy.deepcopy(body)
    body["nested"]["v"].append(3)  # type: ignore[index, union-attr]
    assert store.read(RecordKind.AUTHORITY, "k").body == original  # type: ignore[union-attr]


def test_absent_is_a_singleton() -> None:
    assert Absent() is ABSENT and repr(ABSENT) == "ABSENT"
    assert pickle.loads(pickle.dumps(ABSENT)) is ABSENT


def test_operation_ids_are_canonical_and_distinct() -> None:
    assert operation_id(1) != operation_id(2)
    assert new_operation_id() != new_operation_id()


def test_consistent_read_can_be_refused(clock: ManualClock) -> None:
    with pytest.raises(CapabilityUnsupported):
        InMemoryHqStorage(clock, consistent_read=False).consistent_read()


def test_registered_capability_is_returned(store: InMemoryHqStorage) -> None:
    class DoltHistoryLike:
        pass

    impl = DoltHistoryLike()
    store.register_capability(DoltHistoryLike, impl)
    assert require_capability(store, DoltHistoryLike) is impl


def test_the_fake_fence_is_a_capability_host_with_no_dolt_capability(clock: ManualClock) -> None:
    """A Dolt-only consumer (``fence_audit``, ``fence_orphan``, ``fence_cutover``) given the
    fake is refused before any I/O (bh-3kl1k)."""

    class DoltMergeLike:
        pass

    fence = InMemoryHiveFence(clock)
    assert isinstance(fence, CapabilityHost)
    assert fence.capability(DoltMergeLike) is None
    with pytest.raises(CapabilityUnsupported, match="DoltMergeLike"):
        require_capability(fence, DoltMergeLike)
    impl = DoltMergeLike()
    fence.register_capability(DoltMergeLike, impl)
    assert require_capability(fence, DoltMergeLike) is impl


def test_read_at_an_unknown_revision_is_not_provisioned(store: InMemoryHqStorage) -> None:
    with pytest.raises(NotProvisioned):
        store.read_at(Revision("0" * 64))


def test_scan_cursor_survives_prune_but_foreign_cursor_is_refused(
    clock: ManualClock, store: InMemoryHqStorage
) -> None:
    first = store.append(StreamKind.INBOX, "a", {}, operation=new_operation_id())
    clock.advance(10)
    store.append(StreamKind.INBOX, "b", {}, operation=new_operation_id())
    store.prune(StreamKind.INBOX, older_than=timedelta(seconds=5), operation=new_operation_id())
    assert isinstance(first, Applied)
    assert [v.key for v in store.scan(StreamKind.INBOX, after=first.revision, limit=5)] == ["b"]
    with pytest.raises(InvalidRequest):
        store.scan(StreamKind.REPORT, after=first.revision, limit=5)


def test_publish_unavailable_and_reachability(store: InMemoryHqStorage) -> None:
    from beadhive_hq_storage import ChangeSet

    store.faults.arm_unavailable(on="publish")
    head = store.head()
    with pytest.raises(StorageUnavailable):
        store.publish(
            ChangeSet((("cfg/a", b"1"),), frozenset({"cfg"})),
            expected_head=head,
            operation=new_operation_id(),
            author="a",
            message="m",
        )
    assert store.head() == head


def test_fence_guard_report_names_missing_guards(clock: ManualClock) -> None:
    fence = InMemoryHiveFence(clock, guards=("g1", "g2", "g3"), missing_guards={"g2", "zz"})
    report = fence.guard_conformance()
    assert (report.expected, report.present, report.missing) == (3, 2, ("g2",))


def test_fence_bump_refuses_malformed_requests(clock: ManualClock) -> None:
    fence = InMemoryHiveFence(clock)
    for bad in (
        BumpRequest("", 1, "r", new_operation_id()),
        BumpRequest("f", 0, "r", new_operation_id()),
        BumpRequest("f", 1, "r", "nope"),  # type: ignore[arg-type]
    ):
        with pytest.raises(InvalidRequest):
            fence.commit_bump(bad)
    with pytest.raises(InvalidRequest):
        fence.commit_bump("UPDATE bh_writer SET epoch = 9")  # type: ignore[arg-type]


def test_absent_epoch_ref_conflicts_on_cas() -> None:
    ref = InMemoryEpochRef()
    assert ref.read() is None
    stale = RefView("f", 1, Revision("abc"))
    assert ref.compare_and_set("f", 2, expected=stale) == Conflict(None)
    ref = InMemoryEpochRef(("f", 1))
    ref.faults.arm_unknown("transport", applied=True)
    seen = ref.read()
    assert seen is not None
    out = ref.compare_and_set("g", 2, expected=seen)
    assert isinstance(out, Unknown) and out.expected == seen.sha
    assert ref.read().frame == "g"  # type: ignore[union-attr]
