"""Reusable port contract scenarios and the differential state machine (ADR section 8).

The scenarios are defined once, as functions of an adapter's harness, and every adapter runs the
same bodies: this package's tests run them against the in-memory fake, and
``beadhive-hq-storage-dolt`` (``bh-br1cd``) runs them parametrized over ``[fake, dolt]``.

``pytest`` and ``hypothesis`` are imported only inside the functions that build fixtures, marks
and the state machine, never at module scope, so this module (and the package) stays importable
with the standard library alone. Neither is a runtime dependency; ``hypothesis`` is dev-only.

Adapter authors supply a :class:`ContractAdapter`: two factories building a fresh
:class:`HqHarness` and :class:`FenceHarness` per case. A harness exposes the port objects plus
the few test controls a scenario needs (provisioning, fault injection, a simulated competing
remote writer). A control an adapter cannot provide raises :class:`ScenarioSkipped`, and the case
skips rather than passing vacuously. ``clock`` is a :class:`ClockControl` only where the store
clock can be advanced (the fake); on Dolt it is ``None`` and staleness cases assert ordering,
never elapsed time. No scenario sleeps.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol

from . import (
    ABSENT,
    AppendLog,
    Applied,
    BumpRequest,
    CapabilityHost,
    CapabilityUnsupported,
    ChangeSet,
    CommittedHistory,
    Confirmed,
    Conflict,
    EpochRefStore,
    FenceSnapshot,
    HiveFence,
    InMemoryEpochRef,
    InMemoryHiveFence,
    InMemoryHqStorage,
    IntegrityViolation,
    InvalidRequest,
    Liveness,
    LivenessKind,
    NotConfirmed,
    NotProvisioned,
    OperationId,
    PlacementStore,
    ReclaimNote,
    ReclaimSet,
    RecordKind,
    Revision,
    StorageUnavailable,
    StreamKind,
    SyncReport,
    Unknown,
    UnknownReason,
    Versioned,
    VersionedRecords,
    WriterRow,
    adopt_sentinel,
    require_capability,
)

__all__ = [
    "BEADS_SEED",
    "LABELS_SEED",
    "DEV_PROFILE",
    "GATE_BUDGET",
    "GATE_PROFILE",
    "NIGHTLY_BUDGET",
    "NIGHTLY_PROFILE",
    "PROFILE_ENV",
    "SCENARIOS",
    "ClockControl",
    "ContractAdapter",
    "FakeFenceHarness",
    "FakeHqHarness",
    "FenceHarness",
    "HqHarness",
    "ManualClock",
    "Scenario",
    "ScenarioSkipped",
    "adapter_fixture",
    "build_state_machine",
    "fake_adapter",
    "load_hypothesis_profile",
    "no_shrink_phases",
    "operation_id",
    "parametrize_scenarios",
    "register_hypothesis_profiles",
    "run_scenario",
]

PROFILE_ENV = "BH_HYPOTHESIS_PROFILE"
"""Selects the hypothesis profile; unset means ``gate`` (deterministic). ``dev`` is opt-in."""
GATE_PROFILE = "gate"
NIGHTLY_PROFILE = "nightly"
DEV_PROFILE = "dev"

BEADS_SEED: Mapping[str, tuple[str, str]] = {
    "bd-1": ("in_progress", "frame-dead"),
    "bd-2": ("open", ""),
    "bd-3": ("in_progress", "frame-live"),
}
"""Bead rows (``id -> (status, assignee)``) every fence harness starts with."""

LABELS_SEED: frozenset[tuple[str, str]] = frozenset(
    {
        ("bd-1", "claim-frame:frame-dead"),
        ("bd-1", "recovery:stale"),
        ("bd-1", "area:fence"),
        ("bd-2", "claim-frame:frame-dead"),
        ("bd-3", "claim-frame:frame-live"),
    }
)
"""Label rows (``(bead id, label)``) every fence harness starts with."""

_T0 = datetime(2026, 1, 1, tzinfo=UTC)


def operation_id(n: int) -> OperationId:
    """A deterministic canonical-UUID operation id (distinct per ``n``)."""
    return OperationId(str(uuid.UUID(int=n + 1, version=4)))


class ScenarioSkipped(Exception):
    """An adapter cannot provide a control a scenario needs; the case skips."""


class ClockControl(Protocol):
    def advance(self, delta: timedelta) -> None: ...


class ManualClock:
    """A stdlib-only manual :class:`~beadhive_hq_storage.StorageClock` for the fake (the package
    cannot import root's ``tests/harness`` ``FakeClock``, which satisfies the same protocol)."""

    def __init__(self, start: datetime = _T0) -> None:
        if start.tzinfo is None:
            raise ValueError("ManualClock start must be timezone-aware")
        self._now = start

    def now(self) -> datetime:
        return self._now

    def advance(self, delta: timedelta | float) -> None:
        step = delta if isinstance(delta, timedelta) else timedelta(seconds=float(delta))
        if step < timedelta(0):
            raise ValueError("ManualClock never runs backwards")
        self._now += step


# --- harness protocols ----------------------------------------------------------------------


class HqHarness(Protocol):
    """One fresh HQ store under test plus its test controls."""

    records: VersionedRecords
    log: AppendLog
    liveness: Liveness
    history: CommittedHistory
    host: CapabilityHost
    placements: PlacementStore
    clock: ClockControl | None

    def provision(self, kind: RecordKind, key: str, body: Mapping[str, Any]) -> Revision: ...

    def inject_unknown(self, method: str, reason: UnknownReason, *, applied: bool) -> None: ...

    def inject_unavailable(self, method: str) -> None: ...

    def remote_publish(self, documents: Sequence[tuple[str, bytes]]) -> Revision: ...

    def set_remote_reachable(self, reachable: bool) -> None: ...


class FenceHarness(Protocol):
    """One fresh hive fence (cut over, seeded with :data:`BEADS_SEED`) plus its controls."""

    fence: HiveFence
    epoch_ref: EpochRefStore

    def snapshot_at(self, revision: Revision) -> FenceSnapshot: ...

    def parent_of(self, revision: Revision) -> Revision | None: ...

    def remote_bump(self, bump: BumpRequest) -> Revision: ...

    def inject_unknown(self, method: str, reason: UnknownReason, *, applied: bool) -> None: ...

    def inject_unavailable(self, method: str) -> None: ...

    def set_remote_reachable(self, reachable: bool) -> None: ...


@dataclass(frozen=True)
class ContractAdapter:
    """What an adapter package supplies to run the contract: an id and two factories.

    ``make_fence(cut_over)`` builds a fence that is cut over (``bh_writer`` at epoch 0) or not.
    ``clock_controlled`` declares that ``HqHarness.clock`` can be advanced."""

    id: str
    make_hq: Callable[[], HqHarness]
    make_fence: Callable[[bool], FenceHarness]
    clock_controlled: bool = False


# --- the fake's harness ---------------------------------------------------------------------


class FakeHqHarness:
    """:class:`HqHarness` over :class:`InMemoryHqStorage` and a :class:`ManualClock`."""

    def __init__(self, *, consistent_read: bool = True) -> None:
        self.clock = ManualClock()
        self.store = InMemoryHqStorage(self.clock, consistent_read=consistent_read)
        self.records = self.store
        self.log = self.store
        self.liveness = self.store
        self.history = self.store
        self.host = self.store
        self.placements = self.store.placements()

    def provision(self, kind: RecordKind, key: str, body: Mapping[str, Any]) -> Revision:
        return self.store.provision(kind, key, body)

    def inject_unknown(self, method: str, reason: UnknownReason, *, applied: bool) -> None:
        self.store.faults.arm_unknown(reason, applied=applied, on=method)

    def inject_unavailable(self, method: str) -> None:
        self.store.faults.arm_unavailable(on=method)

    def remote_publish(self, documents: Sequence[tuple[str, bytes]]) -> Revision:
        return self.store.remote_publish(documents)

    def set_remote_reachable(self, reachable: bool) -> None:
        self.store.set_reachable(reachable)


class FakeFenceHarness:
    """:class:`FenceHarness` over :class:`InMemoryHiveFence` and :class:`InMemoryEpochRef`."""

    def __init__(self, cut_over: bool = True) -> None:
        self.clock = ManualClock()
        self.fence_fake = InMemoryHiveFence(
            self.clock,
            writer=WriterRow(frame="", epoch=0) if cut_over else None,
            beads=BEADS_SEED,
            labels=LABELS_SEED,
        )
        self.fence = self.fence_fake
        self.epoch_ref = InMemoryEpochRef(("frame-dead", 1))

    def snapshot_at(self, revision: Revision) -> FenceSnapshot:
        return self.fence_fake.snapshot_at(revision)

    def parent_of(self, revision: Revision) -> Revision | None:
        return self.fence_fake.parent_of(revision)

    def remote_bump(self, bump: BumpRequest) -> Revision:
        return self.fence_fake.remote_bump(bump)

    def inject_unknown(self, method: str, reason: UnknownReason, *, applied: bool) -> None:
        self.fence_fake.faults.arm_unknown(reason, applied=applied, on=method)

    def inject_unavailable(self, method: str) -> None:
        self.fence_fake.faults.arm_unavailable(on=method)

    def set_remote_reachable(self, reachable: bool) -> None:
        self.fence_fake.set_reachable(reachable)


def fake_adapter() -> ContractAdapter:
    """The in-memory fake as a :class:`ContractAdapter` (the ``fake`` parameter id)."""
    return ContractAdapter(
        id="fake",
        make_hq=FakeHqHarness,
        make_fence=FakeFenceHarness,
        clock_controlled=True,
    )


# --- scenario registry ----------------------------------------------------------------------


@dataclass(frozen=True)
class Scenario:
    """One contract case. ``operations`` names the ADR operations it covers (the README table
    is generated from the same names); ``target`` picks the harness it runs on."""

    name: str
    target: Literal["hq", "fence", "adapter"]
    operations: tuple[str, ...]
    run: Callable[[Any], None] = field(repr=False)
    needs_clock: bool = False


_REGISTRY: list[Scenario] = []


def _scenario(
    target: Literal["hq", "fence", "adapter"], *operations: str, needs_clock: bool = False
) -> Callable[[Callable[[Any], None]], Callable[[Any], None]]:
    def register(fn: Callable[[Any], None]) -> Callable[[Any], None]:
        name = fn.__name__.removeprefix("_case_")
        _REGISTRY.append(Scenario(name, target, operations, fn, needs_clock))
        return fn

    return register


def run_scenario(scenario: Scenario, adapter: ContractAdapter) -> None:
    """Run one scenario against a fresh harness of ``adapter``. Raises ``ScenarioSkipped`` when
    the adapter cannot provide a needed control (``pytest.skip`` inside a pytest run)."""
    try:
        if scenario.needs_clock and not adapter.clock_controlled:
            raise ScenarioSkipped(f"{scenario.name} needs a controllable store clock")
        if scenario.target == "hq":
            scenario.run(adapter.make_hq())
        elif scenario.target == "fence":
            scenario.run(adapter.make_fence(True))
        else:
            scenario.run(adapter)
    except ScenarioSkipped as skipped:
        try:
            import pytest
        except ImportError:  # pragma: no cover - outside pytest the caller sees the exception
            raise skipped from None
        pytest.skip(str(skipped))


def _raises(error: type[BaseException], fn: Callable[[], object]) -> BaseException:
    try:
        fn()
    except error as caught:
        return caught
    raise AssertionError(f"expected {error.__name__}")


_REASONS: tuple[UnknownReason, ...] = ("deadline", "transport", "commit")


def _op() -> OperationId:
    return OperationId(str(uuid.uuid4()))


# --- VersionedRecords -----------------------------------------------------------------------


@_scenario("hq", "VersionedRecords.compare_and_set", "VersionedRecords.read")
def _case_cas_from_absent_creates_and_reads_back(h: HqHarness) -> None:
    op = _op()
    out = h.records.compare_and_set(
        RecordKind.AUTHORITY, "frame-a", {"grant": 1}, expected=ABSENT, operation=op
    )
    assert isinstance(out, Applied) and out.operation == op
    got = h.records.read(RecordKind.AUTHORITY, "frame-a")
    assert got == Versioned("frame-a", out.revision, {"grant": 1})


@_scenario("hq", "VersionedRecords.compare_and_set")
def _case_cas_issues_a_fresh_revision_every_write(h: HqHarness) -> None:
    first = h.records.compare_and_set(
        RecordKind.FAILOVER_POLICY, "hive", {"after_s": 30}, expected=ABSENT, operation=_op()
    )
    assert isinstance(first, Applied)
    second = h.records.compare_and_set(
        RecordKind.FAILOVER_POLICY,
        "hive",
        {"after_s": 30},
        expected=first.revision,
        operation=_op(),
    )
    assert isinstance(second, Applied)
    assert second.revision != first.revision  # same body, still a fresh token (condition 5)


@_scenario("hq", "VersionedRecords.compare_and_set")
def _case_cas_stale_expectation_conflicts_with_current(h: HqHarness) -> None:
    first = h.records.compare_and_set(
        RecordKind.AUTHORITY, "k", {"v": 1}, expected=ABSENT, operation=_op()
    )
    assert isinstance(first, Applied)
    second = h.records.compare_and_set(
        RecordKind.AUTHORITY, "k", {"v": 2}, expected=first.revision, operation=_op()
    )
    assert isinstance(second, Applied)
    lost = h.records.compare_and_set(
        RecordKind.AUTHORITY, "k", {"v": 3}, expected=first.revision, operation=_op()
    )
    assert lost == Conflict(Versioned("k", second.revision, {"v": 2}))
    assert h.records.read(RecordKind.AUTHORITY, "k") == Versioned("k", second.revision, {"v": 2})


@_scenario("hq", "VersionedRecords.compare_and_set")
def _case_cas_absent_expectation_conflicts_when_present(h: HqHarness) -> None:
    first = h.records.compare_and_set(
        RecordKind.AUTHORITY, "k", {"v": 1}, expected=ABSENT, operation=_op()
    )
    assert isinstance(first, Applied)
    again = h.records.compare_and_set(
        RecordKind.AUTHORITY, "k", {"v": 9}, expected=ABSENT, operation=_op()
    )
    assert again == Conflict(Versioned("k", first.revision, {"v": 1}))


@_scenario("hq", "VersionedRecords.compare_and_set")
def _case_cas_expected_revision_on_a_missing_record_conflicts(h: HqHarness) -> None:
    other = h.records.compare_and_set(
        RecordKind.AUTHORITY, "other", {"v": 1}, expected=ABSENT, operation=_op()
    )
    assert isinstance(other, Applied)
    out = h.records.compare_and_set(
        RecordKind.AUTHORITY, "missing", {"v": 1}, expected=other.revision, operation=_op()
    )
    assert out == Conflict(None)
    assert h.records.read(RecordKind.AUTHORITY, "missing") is None


@_scenario("hq", "VersionedRecords.compare_and_set", "provisioning")
def _case_update_only_kind_refuses_cas_from_absent(h: HqHarness) -> None:
    _raises(
        NotProvisioned,
        lambda: h.records.compare_and_set(
            RecordKind.PLACEMENT, "bh", {"frame": "f"}, expected=ABSENT, operation=_op()
        ),
    )
    assert h.records.read(RecordKind.PLACEMENT, "bh") is None
    seeded = h.provision(RecordKind.PLACEMENT, "bh", {"frame": "f", "epoch": 1, "cause": "seed"})
    out = h.records.compare_and_set(
        RecordKind.PLACEMENT,
        "bh",
        {"frame": "g", "epoch": 2, "cause": "adopt"},
        expected=seeded,
        operation=_op(),
    )
    assert isinstance(out, Applied) and out.revision != seeded


@_scenario("hq", "VersionedRecords.read_many")
def _case_read_many_filters_by_kind_and_keys(h: HqHarness) -> None:
    for key in ("b", "a", "c"):
        h.records.compare_and_set(
            RecordKind.AUTHORITY, key, {"k": key}, expected=ABSENT, operation=_op()
        )
    h.records.compare_and_set(
        RecordKind.FAILOVER_POLICY, "a", {"other": True}, expected=ABSENT, operation=_op()
    )
    assert [v.key for v in h.records.read_many(RecordKind.AUTHORITY)] == ["a", "b", "c"]
    picked = h.records.read_many(RecordKind.AUTHORITY, ["c", "a", "zz"])
    assert [(v.key, v.body) for v in picked] == [("a", {"k": "a"}), ("c", {"k": "c"})]


@_scenario("hq", "VersionedRecords.compare_and_set", "VersionedRecords.lookup", "Unknown")
def _case_cas_unknown_applied_resolves_confirmed(h: HqHarness) -> None:
    for reason in _REASONS:
        key = f"k-{reason}"
        op = _op()
        h.inject_unknown("compare_and_set", reason, applied=True)
        out = h.records.compare_and_set(
            RecordKind.AUTHORITY, key, {"r": reason}, expected=ABSENT, operation=op
        )
        assert out == Unknown(operation=op, expected=ABSENT, reason=reason)
        found = h.records.lookup(RecordKind.AUTHORITY, key, op)
        assert isinstance(found, Confirmed) and found.operation == op
        assert h.records.read(RecordKind.AUTHORITY, key) == Versioned(
            key, found.revision, {"r": reason}
        )


@_scenario("hq", "VersionedRecords.compare_and_set", "VersionedRecords.lookup", "Unknown")
def _case_cas_unknown_not_applied_keeps_original_expectation(h: HqHarness) -> None:
    base = h.records.compare_and_set(
        RecordKind.AUTHORITY, "k", {"v": 1}, expected=ABSENT, operation=_op()
    )
    assert isinstance(base, Applied)
    for reason in _REASONS:
        op = _op()
        h.inject_unknown("compare_and_set", reason, applied=False)
        out = h.records.compare_and_set(
            RecordKind.AUTHORITY, "k", {"v": 2}, expected=base.revision, operation=op
        )
        assert out == Unknown(operation=op, expected=base.revision, reason=reason)
        assert h.records.lookup(RecordKind.AUTHORITY, "k", op) == NotConfirmed(op)
        assert h.records.read(RecordKind.AUTHORITY, "k") == Versioned("k", base.revision, {"v": 1})


@_scenario("hq", "VersionedRecords.lookup")
def _case_lookup_reads_not_confirmed_once_superseded(h: HqHarness) -> None:
    op = _op()
    first = h.records.compare_and_set(
        RecordKind.AUTHORITY, "k", {"v": 1}, expected=ABSENT, operation=op
    )
    assert isinstance(first, Applied)
    assert isinstance(h.records.lookup(RecordKind.AUTHORITY, "k", op), Confirmed)
    h.records.compare_and_set(
        RecordKind.AUTHORITY, "k", {"v": 2}, expected=first.revision, operation=_op()
    )
    # Applied-then-superseded reads NotConfirmed: it is never proof of non-application.
    assert h.records.lookup(RecordKind.AUTHORITY, "k", op) == NotConfirmed(op)


@_scenario("hq", "VersionedRecords.compare_and_set", "StorageUnavailable")
def _case_cas_unavailable_means_nothing_applied(h: HqHarness) -> None:
    op = _op()
    h.inject_unavailable("compare_and_set")
    _raises(
        StorageUnavailable,
        lambda: h.records.compare_and_set(
            RecordKind.AUTHORITY, "k", {"v": 1}, expected=ABSENT, operation=op
        ),
    )
    assert h.records.read(RecordKind.AUTHORITY, "k") is None
    assert h.records.lookup(RecordKind.AUTHORITY, "k", op) == NotConfirmed(op)


@_scenario("hq", "VersionedRecords.compare_and_set", "AppendLog.append", "InvalidRequest")
def _case_malformed_requests_are_refused_before_io(h: HqHarness) -> None:
    def cas(key: str, body: Any, operation: str) -> object:
        return h.records.compare_and_set(
            RecordKind.AUTHORITY, key, body, expected=ABSENT, operation=OperationId(operation)
        )

    _raises(InvalidRequest, lambda: cas("k", {"v": 1}, "not-a-uuid"))
    _raises(InvalidRequest, lambda: cas("k", {"v": 1}, str(uuid.uuid4()).upper()))
    _raises(InvalidRequest, lambda: cas("", {"v": 1}, str(uuid.uuid4())))
    _raises(InvalidRequest, lambda: cas("k", {"v": object()}, str(uuid.uuid4())))
    _raises(InvalidRequest, lambda: cas("k", {"v": float("nan")}, str(uuid.uuid4())))
    _raises(InvalidRequest, lambda: cas("k", {"v": "x" * (1 << 20)}, str(uuid.uuid4())))
    _raises(
        InvalidRequest,
        lambda: h.log.append(StreamKind.INBOX, "k", [1], operation=_op()),  # type: ignore[arg-type]
    )
    assert h.records.read(RecordKind.AUTHORITY, "k") is None


# --- PlacementStore -------------------------------------------------------------------------


@_scenario("hq", "PlacementStore.read", "PlacementStore.compare_and_set")
def _case_placement_view_moves_frame_and_epoch_under_cas(h: HqHarness) -> None:
    assert h.placements.read("bh") is None
    h.provision(RecordKind.PLACEMENT, "bh", {"frame": "f1", "epoch": 1, "cause": "seed"})
    seen = h.placements.read("bh")
    assert seen is not None and (seen.frame, seen.epoch) == ("f1", 1)
    out = h.placements.compare_and_set(
        "bh", "f2", 2, expected=seen, cause="failover", operation=_op()
    )
    assert isinstance(out, Applied)
    now = h.placements.read("bh")
    assert now is not None and (now.frame, now.epoch, now.token) == ("f2", 2, out.revision)
    lost = h.placements.compare_and_set(
        "bh", "f3", 3, expected=seen, cause="failover", operation=_op()
    )
    assert isinstance(lost, Conflict) and lost.current is not None
    assert lost.current.revision == now.token


# --- AppendLog ------------------------------------------------------------------------------


@_scenario("hq", "AppendLog.append")
def _case_append_is_idempotent_for_the_same_operation(h: HqHarness) -> None:
    op = _op()
    first = h.log.append(StreamKind.INBOX, "f/1/1", {"beat": 1}, operation=op)
    again = h.log.append(StreamKind.INBOX, "f/1/1", {"beat": 1}, operation=op)
    assert isinstance(first, Applied) and again == first
    assert len(h.log.scan(StreamKind.INBOX, limit=10)) == 1


@_scenario("hq", "AppendLog.append")
def _case_append_replay_of_a_key_conflicts(h: HqHarness) -> None:
    op = _op()
    first = h.log.append(StreamKind.RECEIPT, "f/1/1", {"seen": 1}, operation=op)
    assert isinstance(first, Applied)
    expect = Conflict(Versioned("f/1/1", first.revision, {"seen": 1}))
    assert h.log.append(StreamKind.RECEIPT, "f/1/1", {"seen": 2}, operation=op) == expect
    assert h.log.append(StreamKind.RECEIPT, "f/1/1", {"seen": 1}, operation=_op()) == expect
    # The same key is independent per stream.
    assert isinstance(h.log.append(StreamKind.REPORT, "f/1/1", {"x": 1}, operation=_op()), Applied)


@_scenario("hq", "AppendLog.scan")
def _case_scan_orders_filters_and_pages(h: HqHarness) -> None:
    revs = []
    for i, key in enumerate(("c", "a", "b", "a2")):
        out = h.log.append(StreamKind.REPORT, key, {"i": i}, operation=_op())
        assert isinstance(out, Applied)
        revs.append(out.revision)
    assert [v.key for v in h.log.scan(StreamKind.REPORT, limit=10)] == ["c", "a", "b", "a2"]
    assert [v.revision for v in h.log.scan(StreamKind.REPORT, limit=2)] == revs[:2]
    after = h.log.scan(StreamKind.REPORT, after=revs[1], limit=10)
    assert [v.key for v in after] == ["b", "a2"]
    assert [v.body for v in h.log.scan(StreamKind.REPORT, key="a", limit=10)] == [{"i": 1}]
    assert h.log.scan(StreamKind.INBOX, limit=10) == []
    _raises(InvalidRequest, lambda: h.log.scan(StreamKind.REPORT, limit=0))


@_scenario("hq", "AppendLog.append", "AppendLog.lookup_append", "Unknown")
def _case_append_unknown_resolves_by_natural_key(h: HqHarness) -> None:
    applied_op, lost_op = _op(), _op()
    h.inject_unknown("append", "transport", applied=True)
    out = h.log.append(StreamKind.INBOX, "k1", {"v": 1}, operation=applied_op)
    assert out == Unknown(operation=applied_op, expected=ABSENT, reason="transport")
    assert isinstance(h.log.lookup_append(StreamKind.INBOX, "k1", applied_op), Confirmed)
    h.inject_unknown("append", "deadline", applied=False)
    out = h.log.append(StreamKind.INBOX, "k2", {"v": 2}, operation=lost_op)
    assert out == Unknown(operation=lost_op, expected=ABSENT, reason="deadline")
    assert h.log.lookup_append(StreamKind.INBOX, "k2", lost_op) == NotConfirmed(lost_op)
    assert [v.key for v in h.log.scan(StreamKind.INBOX, limit=10)] == ["k1"]


@_scenario("hq", "AppendLog.prune")
def _case_prune_keeps_everything_younger_than_the_window(h: HqHarness) -> None:
    for key in ("a", "b"):
        h.log.append(StreamKind.INBOX, key, {"k": key}, operation=_op())
    report = h.log.prune(StreamKind.INBOX, older_than=timedelta(days=365), operation=_op())
    assert (report.stream, report.removed, report.retained) == (StreamKind.INBOX, 0, 2)
    _raises(
        InvalidRequest,
        lambda: h.log.prune(StreamKind.INBOX, older_than=timedelta(seconds=-1), operation=_op()),
    )


@_scenario("hq", "AppendLog.prune", "StorageClock", needs_clock=True)
def _case_prune_measures_age_by_the_store_clock(h: HqHarness) -> None:
    assert h.clock is not None
    h.log.append(StreamKind.INBOX, "old", {"v": 1}, operation=_op())
    h.clock.advance(timedelta(seconds=100))
    h.log.append(StreamKind.INBOX, "new", {"v": 2}, operation=_op())
    h.clock.advance(timedelta(seconds=10))
    report = h.log.prune(StreamKind.INBOX, older_than=timedelta(seconds=60), operation=_op())
    assert (report.removed, report.retained) == (1, 1)
    assert [v.key for v in h.log.scan(StreamKind.INBOX, limit=10)] == ["new"]


# --- Liveness -------------------------------------------------------------------------------


@_scenario("hq", "Liveness.now", "Liveness.renew", "Liveness.stamped_at", "Liveness.ages")
def _case_renew_is_stamped_by_the_store_clock(h: HqHarness) -> None:
    before = h.liveness.now()
    stamp = h.liveness.renew(LivenessKind.SESSION, "frame-a", {"pid": 1})
    after = h.liveness.now()
    assert before.tzinfo is not None and before <= stamp <= after
    assert h.liveness.stamped_at(LivenessKind.SESSION, "frame-a") == stamp
    assert h.liveness.stamped_at(LivenessKind.SESSION, "nobody") is None
    ages = h.liveness.ages(LivenessKind.SESSION)
    assert set(ages) == {"frame-a"} and ages["frame-a"] >= timedelta(0)
    assert h.liveness.ages(LivenessKind.EVIDENCE) == {}


@_scenario("hq", "Liveness.ages", "Liveness.renew", "StorageClock", needs_clock=True)
def _case_ages_follow_the_injected_clock(h: HqHarness) -> None:
    assert h.clock is not None
    h.liveness.renew(LivenessKind.SESSION, "a")
    h.clock.advance(timedelta(seconds=30))
    h.liveness.renew(LivenessKind.SESSION, "b")
    h.clock.advance(timedelta(seconds=5))
    assert h.liveness.ages(LivenessKind.SESSION) == {
        "a": timedelta(seconds=35),
        "b": timedelta(seconds=5),
    }
    assert h.liveness.ages(LivenessKind.SESSION, ["b", "zz"]) == {"b": timedelta(seconds=5)}
    h.liveness.renew(LivenessKind.SESSION, "a")
    assert h.liveness.ages(LivenessKind.SESSION, ["a"]) == {"a": timedelta(0)}


# --- CommittedHistory -----------------------------------------------------------------------

_CFG = frozenset({"cfg"})


def _changes(*docs: tuple[str, bytes]) -> ChangeSet:
    return ChangeSet(documents=tuple(docs), tables=_CFG)


@_scenario("hq", "CommittedHistory.publish", "CommittedHistory.head", "CommittedHistory.read_at")
def _case_publish_returns_the_exact_committed_head(h: HqHarness) -> None:
    h0 = h.history.head()
    base = dict(h.history.read_at(h0))
    out = h.history.publish(
        _changes(("cfg/a", b"1"), ("cfg/b", b"2")),
        expected_head=h0,
        operation=_op(),
        author="op",
        message="publish",
    )
    assert isinstance(out, Applied)
    assert h.history.head() == out.revision != h0
    assert dict(h.history.read_at(out.revision)) == {**base, "cfg/a": b"1", "cfg/b": b"2"}
    assert dict(h.history.read_at(h0)) == base  # history is immutable


@_scenario("hq", "CommittedHistory.publish")
def _case_publish_conflicts_on_a_moved_or_absent_head(h: HqHarness) -> None:
    h0 = h.history.head()
    first = h.history.publish(
        _changes(("cfg/a", b"1")), expected_head=h0, operation=_op(), author="a", message="m"
    )
    assert isinstance(first, Applied)
    stale = h.history.publish(
        _changes(("cfg/a", b"2")), expected_head=h0, operation=_op(), author="a", message="m"
    )
    assert isinstance(stale, Conflict) and stale.current is not None
    assert stale.current.revision == first.revision
    absent = h.history.publish(
        _changes(("cfg/a", b"3")), expected_head=ABSENT, operation=_op(), author="a", message="m"
    )
    assert isinstance(absent, Conflict)
    assert h.history.head() == first.revision


@_scenario("hq", "CommittedHistory.publish", "CommittedHistory.lookup_publication", "Unknown")
def _case_publish_unknown_resolves_by_publication_witness(h: HqHarness) -> None:
    h0 = h.history.head()
    op = _op()
    h.inject_unknown("publish", "commit", applied=True)
    out = h.history.publish(
        _changes(("cfg/a", b"1")), expected_head=h0, operation=op, author="a", message="m"
    )
    assert out == Unknown(operation=op, expected=h0, reason="commit")
    found = h.history.lookup_publication(op, expected_head=h0)
    assert isinstance(found, Confirmed) and found.revision == h.history.head()
    h1 = h.history.head()
    lost = _op()
    h.inject_unknown("publish", "deadline", applied=False)
    out = h.history.publish(
        _changes(("cfg/a", b"2")), expected_head=h1, operation=lost, author="a", message="m"
    )
    assert out == Unknown(operation=lost, expected=h1, reason="deadline")
    assert h.history.lookup_publication(lost, expected_head=h1) == NotConfirmed(lost)
    assert h.history.head() == h1
    # The witness is bound to its expected parent.
    assert h.history.lookup_publication(op, expected_head=h1) == NotConfirmed(op)


@_scenario("hq", "CommittedHistory.publish", "InvalidRequest")
def _case_publish_refuses_malformed_change_sets(h: HqHarness) -> None:
    h0 = h.history.head()

    def publish(changes: ChangeSet, op: OperationId | None = None) -> object:
        return h.history.publish(
            changes, expected_head=h.history.head(), operation=op or _op(), author="a", message="m"
        )

    _raises(InvalidRequest, lambda: publish(ChangeSet((), _CFG)))
    _raises(InvalidRequest, lambda: publish(ChangeSet((("cfg/a", b"1"),), frozenset())))
    _raises(InvalidRequest, lambda: publish(_changes(("other/a", b"1"))))
    _raises(InvalidRequest, lambda: publish(_changes(("cfg/a", b"1"), ("cfg/a", b"2"))))
    assert h.history.head() == h0
    op = _op()
    assert isinstance(publish(_changes(("cfg/a", b"1")), op), Applied)
    _raises(InvalidRequest, lambda: publish(_changes(("cfg/a", b"2")), op))  # reused intent


@_scenario("hq", "CommittedHistory.consistent_read", "CapabilityUnsupported")
def _case_consistent_read_is_one_token_or_refused(h: HqHarness) -> None:
    try:
        reader = h.history.consistent_read()
    except CapabilityUnsupported:
        return  # e.g. Git: refused explicitly, never joined from independent reads
    with reader as token:
        assert token.revision == h.history.head()
        assert token.backend_identity and token.generation


@_scenario("hq", "CommittedHistory.fetch", "CommittedHistory.push")
def _case_fetch_and_push_report_divergence(h: HqHarness) -> None:
    def publish(content: bytes) -> None:
        out = h.history.publish(
            _changes(("cfg/a", content)),
            expected_head=h.history.head(),
            operation=_op(),
            author="a",
            message="m",
        )
        assert isinstance(out, Applied)

    report = h.history.fetch()
    assert (report.ahead, report.behind, report.diverged) == (0, 0, False)
    publish(b"1")
    report = h.history.fetch()
    assert (report.head, report.ahead, report.behind) == (h.history.head(), 1, 0)
    pushed = h.history.push()
    assert isinstance(pushed, Applied) and pushed.revision == h.history.head()
    assert h.history.fetch() == report.__class__(h.history.head(), 0, 0, False)
    h.remote_publish([("cfg/b", b"remote")])
    report = h.history.fetch()
    assert (report.ahead, report.behind, report.diverged) == (0, 1, False)
    assert report.head == h.history.head()  # fetch never moves the local head
    publish(b"2")
    report = h.history.fetch()
    assert (report.ahead, report.behind, report.diverged) == (1, 1, True)
    assert isinstance(h.history.push(), Conflict)


@_scenario("hq", "CommittedHistory.fetch", "CommittedHistory.push", "StorageUnavailable")
def _case_unreachable_remote_is_unavailable(h: HqHarness) -> None:
    h.set_remote_reachable(False)
    _raises(StorageUnavailable, h.history.fetch)
    _raises(StorageUnavailable, h.history.push)
    h.set_remote_reachable(True)
    assert h.history.fetch().diverged is False


@_scenario("hq", "CommittedHistory.push", "Unknown")
def _case_push_unknown_keeps_the_prior_remote_as_expectation(h: HqHarness) -> None:
    h.history.publish(
        _changes(("cfg/a", b"1")),
        expected_head=h.history.head(),
        operation=_op(),
        author="a",
        message="m",
    )
    h.inject_unknown("push", "transport", applied=True)
    out = h.history.push()
    assert isinstance(out, Unknown) and out.reason == "transport"
    assert h.history.fetch().ahead == 0  # it did land
    h.history.publish(
        _changes(("cfg/a", b"2")),
        expected_head=h.history.head(),
        operation=_op(),
        author="a",
        message="m",
    )
    h.inject_unknown("push", "deadline", applied=False)
    assert isinstance(h.history.push(), Unknown)
    assert h.history.fetch().ahead == 1  # it did not


# --- CapabilityHost -------------------------------------------------------------------------


class _NoSuchCapability(Protocol):
    def never(self) -> None: ...


@_scenario("hq", "CapabilityHost.capability", "require_capability", "CapabilityUnsupported")
def _case_missing_capability_is_none_and_required_raises(h: HqHarness) -> None:
    assert h.host.capability(_NoSuchCapability) is None
    _raises(CapabilityUnsupported, lambda: require_capability(h.host, _NoSuchCapability))


# --- HiveFence ------------------------------------------------------------------------------


def _bump(frame: str, epoch: int, *, reclaim: ReclaimSet | None = None) -> BumpRequest:
    return BumpRequest(
        frame=frame, epoch=epoch, revision=f"rev-{frame}-{epoch}", operation=_op(), reclaim=reclaim
    )


@_scenario("fence", "HiveFence.commit_bump", "HiveFence.writer", "ReclaimSet")
def _case_bump_is_one_commit_with_sentinel_and_reclaim(f: FenceHarness) -> None:
    before = f.fence.commit_bump(_bump("frame-a", 1))
    reclaim = ReclaimSet(dead_frame="frame-dead", bead_ids=frozenset({"bd-1", "bd-2"}))
    head = f.fence.commit_bump(_bump("frame-live", 2, reclaim=reclaim))
    assert f.parent_of(head) == before  # exactly one commit
    snap = f.snapshot_at(head)
    assert snap.writer == WriterRow("frame-live", 2, "rev-frame-live-2")
    assert snap.live_epoch == 2
    assert {mark for mark, _ in snap.marks} == {adopt_sentinel(2)}  # older marks retired
    assert snap.beads["bd-1"] == ("open", "")  # in_progress reverted inside the bump
    assert snap.beads["bd-2"] == BEADS_SEED["bd-2"]  # guarded on in_progress
    assert snap.beads["bd-3"] == BEADS_SEED["bd-3"]  # not in the set
    prior = f.snapshot_at(before)
    assert prior.beads["bd-1"] == BEADS_SEED["bd-1"]
    assert {mark for mark, _ in prior.marks} == {adopt_sentinel(1)}
    assert f.fence.writer() == snap.writer
    assert snap.labels == LABELS_SEED - {("bd-1", "claim-frame:frame-dead")}
    assert not snap.comments


_NOTE_AT = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)


def _note(bead: str, n: int) -> ReclaimNote:
    return ReclaimNote(bead, f"c-{n}", "ops/adopt@frame-live", f"reclaimed {bead}", _NOTE_AT)


@_scenario("fence", "HiveFence.commit_bump", "ReclaimSet", "ReclaimNote")
def _case_bump_reclaim_marks_resumable_and_leaves_the_audit_note(f: FenceHarness) -> None:
    before = f.fence.commit_bump(_bump("frame-a", 1))
    reclaim = ReclaimSet(
        dead_frame="frame-dead",
        bead_ids=frozenset({"bd-1", "bd-2"}),
        resumable=frozenset({"bd-1", "bd-2"}),
        notes=(_note("bd-1", 1), _note("bd-2", 2)),
    )
    head = f.fence.commit_bump(_bump("frame-live", 2, reclaim=reclaim))
    assert f.parent_of(head) == before  # still exactly one commit
    snap = f.snapshot_at(head)
    assert snap.beads["bd-1"] == ("open", "")
    # claim-frame cleared and every recovery:* replaced by recovery:resumable; other labels kept
    assert {label for bead, label in snap.labels if bead == "bd-1"} == {
        "area:fence",
        "recovery:resumable",
    }
    # bd-2 is not in_progress: none of its writes apply (labels, note)
    assert {row for row in snap.labels if row[0] != "bd-1"} == {
        row for row in LABELS_SEED if row[0] != "bd-1"
    }
    assert snap.comments == frozenset(
        {("c-1", "bd-1", "ops/adopt@frame-live", "reclaimed bd-1", "2026-01-02 03:04:05")}
    )
    assert f.snapshot_at(before).comments == frozenset()


@_scenario("fence", "HiveFence.commit_bump", "ReclaimSet", "InvalidRequest")
def _case_bump_refuses_a_malformed_reclaim(f: FenceHarness) -> None:
    f.fence.commit_bump(_bump("frame-a", 1))
    beads = frozenset({"bd-1"})
    bad = (
        ReclaimSet("frame-dead", beads, resumable=frozenset({"bd-3"})),
        ReclaimSet("frame-dead", beads, notes=(_note("bd-3", 1),)),
        ReclaimSet("frame-dead", beads, notes=(_note("bd-1", 1), _note("bd-1", 1))),
        ReclaimSet(
            "frame-dead",
            beads,
            notes=(ReclaimNote("bd-1", "c-1", "a", "t", datetime(2026, 1, 1)),),  # noqa: DTZ001
        ),
    )
    for reclaim in bad:
        _raises(
            InvalidRequest,
            lambda reclaim=reclaim: f.fence.commit_bump(_bump("frame-b", 2, reclaim=reclaim)),
        )
    assert f.fence.writer() == WriterRow("frame-a", 1, "rev-frame-a-1")


@_scenario("fence", "HiveFence.commit_bump", "HiveFence.epoch_floor", "IntegrityViolation")
def _case_bump_never_regresses_below_the_floor(f: FenceHarness) -> None:
    assert f.fence.epoch_floor().writer == 0
    f.fence.commit_bump(_bump("frame-a", 3))
    floor = f.fence.epoch_floor()
    assert (floor.history_max, floor.writer) == (3, 3)
    for epoch in (1, 3):
        _raises(IntegrityViolation, lambda epoch=epoch: f.fence.commit_bump(_bump("x", epoch)))
    assert f.fence.writer() == WriterRow("frame-a", 3, "rev-frame-a-3")


@_scenario(
    "adapter",
    "HiveFence.commit_bump",
    "HiveFence.writer",
    "HiveFence.epoch_floor",
    "NotProvisioned",
)
def _case_bump_refused_when_not_cut_over(adapter: ContractAdapter) -> None:
    f = adapter.make_fence(False)
    assert f.fence.writer() is None
    assert f.fence.epoch_floor().writer is None
    _raises(NotProvisioned, lambda: f.fence.commit_bump(_bump("frame-a", 1)))


@_scenario("fence", "HiveFence.commit_bump", "StorageUnavailable")
def _case_bump_failure_leaves_nothing_applied(f: FenceHarness) -> None:
    f.fence.commit_bump(_bump("frame-a", 1))
    writer = f.fence.writer()
    f.inject_unavailable("commit_bump")
    _raises(StorageUnavailable, lambda: f.fence.commit_bump(_bump("frame-b", 2)))
    assert f.fence.writer() == writer
    assert f.fence.epoch_floor().history_max == 1


@_scenario("fence", "HiveFence.push", "HiveFence.lookup_bump", "HiveFence.remote_writer")
def _case_push_then_lookup_bump_finds_the_sentinel(f: FenceHarness) -> None:
    bump = _bump("frame-a", 1)
    f.fence.commit_bump(bump)
    assert f.fence.lookup_bump("frame-a", 1, operation=bump.operation) == NotConfirmed(
        bump.operation
    )
    pushed = f.fence.push()
    assert isinstance(pushed, Applied)
    found = f.fence.lookup_bump("frame-a", 1, operation=bump.operation)
    assert isinstance(found, Confirmed) and found.revision == pushed.revision
    assert f.fence.remote_writer() == WriterRow("frame-a", 1, "rev-frame-a-1")
    other = _op()
    assert f.fence.lookup_bump("frame-b", 1, operation=other) == NotConfirmed(other)
    f.fence.commit_bump(_bump("frame-b", 2))
    assert isinstance(f.fence.push(), Applied)
    # A later bump retired the sentinel: superseded reads NotConfirmed.
    assert f.fence.lookup_bump("frame-a", 1, operation=bump.operation) == NotConfirmed(
        bump.operation
    )


@_scenario("fence", "HiveFence.push", "HiveFence.sync_to_remote", "HiveFence.remote_writer")
def _case_non_fast_forward_push_conflicts_and_sync_reports_divergence(f: FenceHarness) -> None:
    f.remote_bump(_bump("frame-other", 1))
    local = f.fence.commit_bump(_bump("frame-a", 1))
    out = f.fence.push()
    assert isinstance(out, Conflict)
    assert f.fence.remote_writer() == WriterRow("frame-other", 1, "rev-frame-other-1")
    report = f.fence.sync_to_remote()
    assert (report.ahead, report.behind, report.diverged) == (1, 1, True)
    assert f.fence.writer() == WriterRow("frame-other", 1, "rev-frame-other-1")
    assert report.head != local
    assert f.fence.sync_to_remote() == report.__class__(report.head, 0, 0, False)
    f.set_remote_reachable(False)
    _raises(StorageUnavailable, f.fence.sync_to_remote)
    _raises(StorageUnavailable, f.fence.push)


@_scenario("fence", "HiveFence.push", "HiveFence.lookup_bump", "Unknown")
def _case_push_unknown_resolves_through_lookup_bump(f: FenceHarness) -> None:
    landed = _bump("frame-a", 1)
    f.fence.commit_bump(landed)
    f.inject_unknown("push", "commit", applied=True)
    assert isinstance(f.fence.push(), Unknown)
    assert isinstance(f.fence.lookup_bump("frame-a", 1, operation=landed.operation), Confirmed)
    lost = _bump("frame-a", 2)
    f.fence.commit_bump(lost)
    f.inject_unknown("push", "deadline", applied=False)
    out = f.fence.push()
    assert isinstance(out, Unknown) and out.reason == "deadline"
    assert f.fence.lookup_bump("frame-a", 2, operation=lost.operation) == NotConfirmed(
        lost.operation
    )


@_scenario("fence", "HiveFence.guard_conformance")
def _case_guard_conformance_reports_counts(f: FenceHarness) -> None:
    report = f.fence.guard_conformance()
    assert report.expected >= 1
    assert report.present == report.expected - len(report.missing)


# --- EpochRefStore --------------------------------------------------------------------------


@_scenario("fence", "EpochRefStore.read", "EpochRefStore.compare_and_set")
def _case_epoch_ref_cas_issues_a_fresh_sha(f: FenceHarness) -> None:
    seen = f.epoch_ref.read()
    assert seen is not None
    out = f.epoch_ref.compare_and_set("frame-a", seen.epoch + 1, expected=seen)
    assert isinstance(out, Applied) and out.revision != seen.sha
    now = f.epoch_ref.read()
    assert now is not None and (now.frame, now.epoch, now.sha) == (
        "frame-a",
        seen.epoch + 1,
        out.revision,
    )
    lost = f.epoch_ref.compare_and_set("frame-b", seen.epoch + 2, expected=seen)
    assert isinstance(lost, Conflict) and lost.current is not None
    assert lost.current.revision == now.sha


SCENARIOS: tuple[Scenario, ...] = tuple(_REGISTRY)


# --- pytest wiring --------------------------------------------------------------------------


def adapter_fixture(adapters: Mapping[str, Callable[[], ContractAdapter]]) -> Any:
    """A pytest fixture named ``adapter``, parametrized over ``adapters`` (id -> factory).

    Assign it at module scope: ``adapter = adapter_fixture({"fake": fake_adapter})``. A factory
    may call ``pytest.skip`` itself (the ``dolt`` parameter self-skips without a binary)."""
    import pytest

    ids = list(adapters)

    @pytest.fixture(params=ids, ids=ids, name="adapter")
    def _adapter(request: Any) -> ContractAdapter:
        return adapters[request.param]()

    return _adapter


def parametrize_scenarios(*, scenarios: Sequence[Scenario] | None = None) -> Any:
    """``pytest.mark.parametrize("scenario", SCENARIOS)`` with the scenario names as ids."""
    import pytest

    chosen = tuple(SCENARIOS if scenarios is None else scenarios)
    return pytest.mark.parametrize("scenario", chosen, ids=[s.name for s in chosen])


# --- hypothesis profiles --------------------------------------------------------------------

GATE_BUDGET: Mapping[str, int] = {"max_examples": 25, "stateful_step_count": 20}
"""The gate's small, derandomized budget (ADR section 8)."""
NIGHTLY_BUDGET: Mapping[str, int] = {"max_examples": 500, "stateful_step_count": 40}
"""About 20x the gate's examples and twice its steps, still derandomized; shrinking on."""


def register_hypothesis_profiles() -> None:
    """Register ``gate``, ``nightly`` and ``dev`` (idempotent). Called from ``conftest.py``."""
    from hypothesis import HealthCheck, Phase, settings

    common: dict[str, Any] = {
        "derandomize": True,
        "database": None,
        "deadline": None,
        "suppress_health_check": [HealthCheck.too_slow],
    }
    settings.register_profile(GATE_PROFILE, **common, **GATE_BUDGET)
    settings.register_profile(NIGHTLY_PROFILE, **common, **NIGHTLY_BUDGET, phases=tuple(Phase))
    settings.register_profile(DEV_PROFILE)


def load_hypothesis_profile(default: str = GATE_PROFILE) -> str:
    """Load the profile named by :data:`PROFILE_ENV` (else ``default``) and return its name.

    The gate is the default so an unconfigured run is reproducible from the tree alone; set
    ``BH_HYPOTHESIS_PROFILE=dev`` locally to explore with hypothesis' defaults."""
    from hypothesis import settings

    register_hypothesis_profiles()
    name = os.environ.get(PROFILE_ENV, "").strip() or default
    settings.load_profile(name)
    return name


def no_shrink_phases() -> tuple[Any, ...]:
    """The gate's phases for a real-Dolt machine: everything except ``shrink`` (ADR section 8)."""
    from hypothesis import Phase

    return tuple(p for p in Phase if p is not Phase.shrink)


# --- the differential state machine ---------------------------------------------------------


class _Side:
    """One adapter under the machine, plus its revision-equality pattern."""

    def __init__(self, adapter: ContractAdapter) -> None:
        self.adapter = adapter
        self.hq = adapter.make_hq()
        self.fence = adapter.make_fence(True)
        self._tags: dict[str, int] = {}
        self.seen: dict[tuple[str, str], list[Revision]] = {}
        self.bumps: list[BumpRequest] = []

    def tag(self, revision: object) -> object:
        if revision is ABSENT or revision is None:
            return revision
        return self._tags.setdefault(str(revision), len(self._tags))

    def shape(self, value: object) -> object:
        """Project a result onto what both adapters must agree on."""
        if isinstance(value, BaseException):
            return ("error", type(value).__name__)
        if isinstance(value, Applied):
            return ("applied", self.tag(value.revision))
        if isinstance(value, Conflict):
            cur = value.current
            return (
                "conflict",
                None if cur is None else (cur.key, self.tag(cur.revision), cur.body),
            )
        if isinstance(value, Unknown):
            return ("unknown", value.reason, self.tag(value.expected))
        if isinstance(value, Confirmed):
            return ("confirmed", self.tag(value.revision))
        if isinstance(value, NotConfirmed):
            return ("not-confirmed",)
        if isinstance(value, SyncReport):  # the head is a revision: compare its tag (bh-br1cd)
            return ("sync", self.tag(value.head), value.ahead, value.behind, value.diverged)
        if isinstance(value, str):  # a revision (commit_bump)
            return ("revision", self.tag(value))
        return value


def _attempt(fn: Callable[[], object]) -> object:
    try:
        return fn()
    except (
        StorageUnavailable,
        NotProvisioned,
        InvalidRequest,
        IntegrityViolation,
        CapabilityUnsupported,
    ) as error:
        return error


def build_state_machine(a: ContractAdapter, b: ContractAdapter) -> Any:
    """A hypothesis ``RuleBasedStateMachine`` driving ``a`` and ``b`` with the same commands.

    Commands: CAS with a fresh, absent or stale expectation; append (with operation reuse to hit
    idempotency and replay); prune; renew; publish; push; fetch; a competing remote writer; bump
    (including a floor violation and a reclaim); fence push and sync; lookups; and injected
    unknown outcomes (applied and not). After every step it compares outcome kinds, bodies, the
    revision-EQUALITY pattern (never raw revision text), witness lookups and head movement.
    Clock-dependent values are projected out unless both sides are clock-controlled, which is
    also the only case with an ``advance_clock`` rule (the fake-against-fake self-check)."""
    from hypothesis import strategies as st
    from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

    clocked = a.clock_controlled and b.clock_controlled
    keys = st.sampled_from(("k0", "k1", "k2"))
    kinds = st.sampled_from(tuple(RecordKind))
    streams = st.sampled_from(tuple(StreamKind))
    live_kinds = st.sampled_from(tuple(LivenessKind))
    values = st.integers(min_value=0, max_value=2)
    reasons = st.sampled_from(_REASONS)
    faultable = st.sampled_from(("compare_and_set", "append", "publish", "push"))

    class DifferentialMachine(RuleBasedStateMachine):
        def __init__(self) -> None:
            super().__init__()
            self.sides = (_Side(a), _Side(b))
            self.ops: list[OperationId] = []
            self.n = 0
            for side in self.sides:
                seeded = side.hq.provision(
                    RecordKind.PLACEMENT, "k0", {"frame": "f0", "epoch": 1, "cause": "seed"}
                )
                side.seen[(RecordKind.PLACEMENT.value, "k0")] = [seeded]
                side.tag(seeded)

        # -- helpers ----------------------------------------------------------------------

        def _op(self) -> OperationId:
            self.n += 1
            op = operation_id(self.n)
            self.ops.append(op)
            return op

        def _both(self, call: Callable[[_Side], object]) -> tuple[object, object]:
            results = tuple(_attempt(lambda side=side: call(side)) for side in self.sides)
            left, right = (side.shape(r) for side, r in zip(self.sides, results, strict=True))
            assert left == right, f"{a.id} and {b.id} disagree: {left!r} != {right!r}"
            return results  # type: ignore[return-value]

        # -- VersionedRecords -------------------------------------------------------------

        @rule(
            kind=kinds,
            key=keys,
            value=values,
            mode=st.sampled_from(("current", "absent", "stale")),
        )
        def cas(self, kind: RecordKind, key: str, value: int, mode: str) -> None:
            op = self._op()

            def call(side: _Side) -> object:
                seen = side.seen.setdefault((kind.value, key), [])
                current = side.hq.records.read(kind, key)
                expected: Revision | Any
                if mode == "absent" or (mode == "current" and current is None):
                    expected = ABSENT
                elif mode == "current":
                    expected = current.revision  # type: ignore[union-attr]
                else:
                    expected = seen[0] if seen else ABSENT
                out = side.hq.records.compare_and_set(
                    kind, key, {"v": value}, expected=expected, operation=op
                )
                if isinstance(out, Applied):
                    seen.append(out.revision)
                return out

            self._both(call)

        @rule(kind=kinds, key=keys, pick=st.integers(min_value=0, max_value=50))
        def lookup(self, kind: RecordKind, key: str, pick: int) -> None:
            if not self.ops:
                return
            op = self.ops[-1 - pick % len(self.ops)]
            self._both(lambda side: side.hq.records.lookup(kind, key, op))

        @rule(method=faultable, reason=reasons, applied=st.booleans())
        def inject_unknown(self, method: str, reason: UnknownReason, applied: bool) -> None:
            for side in self.sides:
                side.hq.inject_unknown(method, reason, applied=applied)

        # -- AppendLog --------------------------------------------------------------------

        @rule(stream=streams, key=keys, value=values, op_pick=values)
        def append(self, stream: StreamKind, key: str, value: int, op_pick: int) -> None:
            op = operation_id(1_000_000 + op_pick)
            self._both(lambda side: side.hq.log.append(stream, key, {"v": value}, operation=op))
            self._both(lambda side: side.hq.log.lookup_append(stream, key, op))

        @rule(stream=streams, seconds=st.sampled_from((0, 30, 3600)))
        def prune(self, stream: StreamKind, seconds: int) -> None:
            window = timedelta(seconds=seconds) if clocked else timedelta(days=365)
            op = self._op()
            left, right = self._both(
                lambda side: side.hq.log.prune(stream, older_than=window, operation=op)
            )
            if not isinstance(left, BaseException):
                assert (left.removed, left.retained) == (right.removed, right.retained)  # type: ignore[union-attr]

        # -- Liveness ---------------------------------------------------------------------

        @rule(kind=live_kinds, key=keys)
        def renew(self, kind: LivenessKind, key: str) -> None:
            for side in self.sides:
                side.hq.liveness.renew(kind, key, {"by": key})

        @precondition(lambda self: clocked)
        @rule(seconds=st.sampled_from((1, 29, 31, 3601)))
        def advance_clock(self, seconds: int) -> None:
            for side in self.sides:
                assert side.hq.clock is not None
                side.hq.clock.advance(timedelta(seconds=seconds))

        # -- CommittedHistory -------------------------------------------------------------

        @rule(doc=st.sampled_from(("cfg/a", "cfg/b")), value=values, stale=st.booleans())
        def publish(self, doc: str, value: int, stale: bool) -> None:
            op = self._op()

            def call(side: _Side) -> object:
                seen = side.seen.setdefault(("history", "head"), [side.hq.history.head()])
                expected = seen[0] if stale else side.hq.history.head()
                out = side.hq.history.publish(
                    _changes((doc, str(value).encode())),
                    expected_head=expected,
                    operation=op,
                    author="machine",
                    message="step",
                )
                side.last_publish = (op, expected)  # type: ignore[attr-defined]
                return out

            self._both(call)
            self._both(
                lambda side: side.hq.history.lookup_publication(
                    side.last_publish[0],  # type: ignore[attr-defined]
                    expected_head=side.last_publish[1],  # type: ignore[attr-defined]
                )
            )

        @rule()
        def push(self) -> None:
            self._both(lambda side: side.hq.history.push())

        @rule()
        def fetch(self) -> None:
            left, right = self._both(lambda side: side.hq.history.fetch())
            for side, report in zip(self.sides, (left, right), strict=True):
                side.last_fetch = (  # type: ignore[attr-defined]
                    side.tag(report.head),  # type: ignore[union-attr]
                    report.ahead,  # type: ignore[union-attr]
                    report.behind,  # type: ignore[union-attr]
                    report.diverged,  # type: ignore[union-attr]
                )
            assert self.sides[0].last_fetch == self.sides[1].last_fetch  # type: ignore[attr-defined]

        @rule(value=values)
        def competing_remote_publish(self, value: int) -> None:
            for side in self.sides:
                side.tag(side.hq.remote_publish([("cfg/r", str(value).encode())]))

        # -- HiveFence --------------------------------------------------------------------

        @rule(
            frame=st.sampled_from(("frame-a", "frame-b")),
            delta=st.sampled_from((0, 1, 2)),
            reclaim=st.booleans(),
        )
        def bump(self, frame: str, delta: int, reclaim: bool) -> None:
            op = self._op()

            def call(side: _Side) -> object:
                floor = side.fence.fence.epoch_floor()
                epoch = max(floor.history_max, floor.writer or 0) + delta
                bump = BumpRequest(
                    frame=frame,
                    epoch=epoch,
                    revision=f"rev-{frame}-{epoch}",
                    operation=op,
                    reclaim=ReclaimSet(
                        "frame-dead",
                        frozenset({"bd-1", "bd-2"}),
                        resumable=frozenset({"bd-1"}),
                        notes=(
                            ReclaimNote("bd-1", f"c-{op}", "ops/adopt@" + frame, "reclaimed", _T0),
                        ),
                    )
                    if reclaim
                    else None,
                )
                side.bumps.append(bump)
                return side.fence.fence.commit_bump(bump)

            self._both(call)

        @rule()
        def fence_push(self) -> None:
            self._both(lambda side: side.fence.fence.push())

        @rule()
        def fence_sync(self) -> None:
            left, right = self._both(lambda side: side.fence.fence.sync_to_remote())
            if not isinstance(left, BaseException):
                l_view = (left.ahead, left.behind, left.diverged)  # type: ignore[union-attr]
                r_view = (right.ahead, right.behind, right.diverged)  # type: ignore[union-attr]
                assert l_view == r_view

        @rule(frame=st.sampled_from(("frame-a", "frame-other")))
        def competing_remote_bump(self, frame: str) -> None:
            op = self._op()

            def call(side: _Side) -> object:
                remote = side.fence.fence.remote_writer()
                floor = side.fence.fence.epoch_floor()
                epoch = max(floor.history_max, remote.epoch if remote else 0) + 1
                return side.fence.remote_bump(BumpRequest(frame, epoch, f"rev-{frame}-{epoch}", op))

            self._both(call)

        @rule(pick=st.integers(min_value=0, max_value=50))
        def lookup_bump(self, pick: int) -> None:
            if not self.sides[0].bumps:
                return
            index = -1 - pick % len(self.sides[0].bumps)

            def call(side: _Side) -> object:
                bump = side.bumps[index]
                return side.fence.fence.lookup_bump(
                    bump.frame, bump.epoch, operation=bump.operation
                )

            self._both(call)

        @rule(reason=reasons, applied=st.booleans())
        def inject_fence_unknown(self, reason: UnknownReason, applied: bool) -> None:
            for side in self.sides:
                side.fence.inject_unknown("push", reason, applied=applied)

        # -- invariants -------------------------------------------------------------------

        @invariant()
        def observable_state_agrees(self) -> None:
            views = []
            for side in self.sides:
                hq = side.hq
                records = tuple(
                    (kind.value, v.key, side.tag(v.revision), v.body)
                    for kind in RecordKind
                    for v in hq.records.read_many(kind)
                )
                streams_view = tuple(
                    (stream.value, v.key, side.tag(v.revision), v.body)
                    for stream in StreamKind
                    for v in hq.log.scan(stream, limit=10_000)
                )
                live = tuple(
                    (kind.value, key, age if clocked else None)
                    for kind in LivenessKind
                    for key, age in hq.liveness.ages(kind).items()
                )
                head = hq.history.head()
                history = (side.tag(head), tuple(hq.history.read_at(head)))
                fence = side.fence.fence
                floor = fence.epoch_floor()
                fence_view = (
                    fence.writer(),
                    (floor.history_max, floor.writer),
                    fence.remote_writer(),
                    fence.guard_conformance(),
                )
                views.append((records, streams_view, live, history, fence_view))
            assert views[0] == views[1], f"{a.id} and {b.id} diverged in observable state"

    DifferentialMachine.__name__ = f"DifferentialMachine_{a.id}_{b.id}"
    return DifferentialMachine
