"""Placement-first coexistence adopt with an idempotent step 2 (bh-4c7p4, ADR §2, P-M2).

Fast, in-memory: the three ports (:class:`~beadhive_hq_storage.HiveFence`,
:class:`~beadhive_hq_storage.PlacementStore`, :class:`EpochRef`) are faithful fakes of the
remote head, HQ placement and ``refs/bh/epoch``, so the ORDER, the idempotence after a crash
at every point, and the loss semantics are proven here. ``tests/test_writer_adopt_int.py`` runs
the same core against real Dolt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from beadhive import writer_adopt as wa
from beadhive_hq_storage import (
    ABSENT,
    Absent,
    Applied,
    BumpRequest,
    Confirmed,
    Conflict,
    EpochFloor,
    GuardReport,
    InMemoryHqStorage,
    IntegrityViolation,
    NotConfirmed,
    PlacementStore,
    RecordKind,
    Revision,
    StorageUnavailable,
    SyncReport,
    Unknown,
    Versioned,
)

PREFIX = "fx"
ME = "frame-c"
OLD = "frame-a"


class Crash(BaseException):
    """An injected process death (BaseException: no ``except Exception`` may swallow it)."""


# ---- fakes --------------------------------------------------------------------------------


@dataclass
class Head:
    """One ``main``: the fence rows the bump touches, plus history."""

    writer: wa.WriterRow | None
    live: int = 0
    marks: dict[str, int] = field(default_factory=dict)
    history_max: int = 0
    bumps: list[tuple[str, int, str]] = field(default_factory=list)

    def copy(self) -> Head:
        return Head(self.writer, self.live, dict(self.marks), self.history_max, list(self.bumps))


class FakeData:
    """The hive remote + one node's local main, as a :class:`~beadhive_hq_storage.HiveFence`.
    ``push`` is a fast-forward-only CAS on the remote head, so a competing push in between is a
    genuine non-fast-forward (``Conflict``)."""

    def __init__(self, remote: Head, *, triggers: int = wa.FENCE_TRIGGER_COUNT):
        self.remote = remote
        self.local = remote.copy()
        self.base_version = 0
        self.version = 0
        self.triggers = triggers
        self.calls: list[str] = []
        self.unreachable = False
        self.after_push: list = []

    def _head(self) -> Revision:
        return Revision(f"v{self.version}")

    def sync_to_remote(self):
        self.calls.append("sync")
        if self.unreachable:
            raise wa.DataUnreachable("partitioned")
        self.local = self.remote.copy()
        self.base_version = self.version
        return SyncReport(self._head(), 0, 0, False)

    def writer(self):
        return self.local.writer

    def remote_writer(self):
        self.calls.append("remote_writer")
        return self.remote.writer

    def epoch_floor(self):
        w = self.local.writer
        return EpochFloor(history_max=self.local.history_max, writer=None if w is None else w.epoch)

    def guard_conformance(self):
        expected = wa.FENCE_TRIGGER_COUNT
        missing = tuple(f"bh_missing_{i}" for i in range(max(expected - self.triggers, 0)))
        return GuardReport(expected=expected, present=expected - len(missing), missing=missing)

    def commit_bump(self, bump: BumpRequest):
        self.calls.append("commit")
        assert isinstance(bump, BumpRequest)
        frame, epoch, rev = bump.frame, bump.epoch, bump.revision
        assert re.fullmatch(r"[0-9a-f]{64}", rev)
        head = self.local
        floor = max(head.history_max, head.writer.epoch if head.writer else 0)
        if epoch <= floor:
            raise IntegrityViolation(f"epoch {epoch} does not exceed the floor {floor}")
        head.writer = wa.WriterRow(frame, epoch, rev)
        head.marks = {k: v for k, v in head.marks.items() if v >= epoch}
        head.live = epoch
        head.marks[f"adopt-{epoch}"] = epoch
        head.history_max = max(head.history_max, epoch)
        head.bumps.append((frame, epoch, rev))
        return Applied(Revision(f"local-{epoch}"), bump.operation)

    def push(self):
        self.calls.append("push")
        if self.unreachable:
            raise wa.DataUnreachable("partitioned")
        if self.version != self.base_version:
            w = self.remote.writer
            body = {} if w is None else {"frame": w.frame, "epoch": w.epoch}
            return Conflict(Versioned("bh_writer", self._head(), body))
        self.remote = self.local.copy()
        self.version += 1
        self.base_version = self.version
        for hook in self.after_push:
            hook()
        return Applied(self._head(), "op")

    def lookup_bump(self, frame, epoch, *, operation):
        w = self.remote.writer
        if f"adopt-{epoch}" in self.remote.marks and w is not None and w.frame == frame:
            return Confirmed(operation, self._head(), None)
        return NotConfirmed(operation)

    def foreign_push(self, head: Head):
        """Another frame publishes; this node's next push is non-fast-forward."""
        self.remote = head
        self.version += 1


class FakePlacement:
    """HQ placement as a :class:`~beadhive_hq_storage.PlacementStore`: a stale expectation is a
    ``Conflict``, a won CAS rewrites the token."""

    def __init__(self, frame: str | None, epoch: int, log: list[str]):
        self.view = None if frame is None else wa.PlacementView(frame, epoch, f"tok{epoch}")
        self.log = log
        self.cas_calls = 0
        self.causes: list[str] = []

    def read(self, prefix):
        assert prefix == PREFIX
        return self.view

    def compare_and_set(self, prefix, frame, epoch, *, expected, cause, operation):
        assert prefix == PREFIX
        self.cas_calls += 1
        self.causes.append(cause)
        self.log.append(f"placement:{epoch}")
        if (None if isinstance(expected, Absent) else expected) != self.view:
            v = self.view
            body = {} if v is None else {"frame": v.frame, "epoch": v.epoch}
            return Conflict(None if v is None else Versioned(prefix, Revision(v.token), body))
        self.view = wa.PlacementView(frame, epoch, f"tok{epoch}-{self.cas_calls}")
        return Applied(Revision(self.view.token), operation)

    def lookup(self, prefix, operation):
        assert prefix == PREFIX
        return NotConfirmed(operation)


class FakeRef:
    def __init__(self, frame: str | None, epoch: int, log: list[str]):
        self.view = None if frame is None else wa.RefView(frame, epoch, f"sha{epoch}")
        self.log = log
        self.cas_calls = 0

    def read(self):
        return self.view

    def cas(self, frame, epoch, *, expected):
        self.cas_calls += 1
        self.log.append(f"ref:{epoch}")
        if expected != self.view:
            raise RuntimeError("! [rejected] stale info")
        self.view = wa.RefView(frame, epoch, f"sha{epoch}-{self.cas_calls}")
        return self.view


class OrderedData(FakeData):
    def __init__(self, remote, log, **kw):
        super().__init__(remote, **kw)
        self.log = log

    def commit_bump(self, bump):
        self.log.append("data:commit")
        return super().commit_bump(bump)

    def push(self):
        out = super().push()
        self.log.append(f"data:push:{isinstance(out, Applied)}")
        return out


def _world(*, writer_epoch=221, placement_epoch=221, ref_epoch=221, history_max=None, ref=True):
    log: list[str] = []
    head = Head(
        wa.WriterRow(OLD, writer_epoch, "r0"),
        live=writer_epoch,
        marks={"m1": writer_epoch},
        history_max=history_max if history_max is not None else writer_epoch,
    )
    data = OrderedData(head, log)
    placement = FakePlacement(OLD, placement_epoch, log)
    epoch_ref = FakeRef(OLD, ref_epoch, log) if ref else FakeRef(None, 0, log)
    return data, placement, epoch_ref, log


def _adopt(data, placement, ref, **kw):
    return wa.coexistence_adopt(data, placement, ref, prefix=PREFIX, frame=ME, **kw)


# ---- the epoch formula ---------------------------------------------------------------------


def test_next_epoch_is_one_past_every_carrier():
    assert wa.next_epoch(ref=5, placement=7, writer=6, history_max=3) == 8
    assert wa.next_epoch(ref=9, placement=7, writer=6, history_max=3) == 10
    assert wa.next_epoch() == 1


def test_a_dropped_and_recreated_bh_writer_cannot_regress_the_epoch():
    """bh_writer was dropped and recreated at 1, placement/ref restored low: only history still
    remembers 230. The adopt must land above it."""
    data, placement, ref, _ = _world(
        writer_epoch=1, placement_epoch=2, ref_epoch=2, history_max=230
    )
    out = _adopt(data, placement, ref)
    assert out.epoch == 231
    assert data.remote.writer.epoch == 231


# ---- the order and the bump ----------------------------------------------------------------


def test_order_is_placement_then_ref_then_data():
    data, placement, ref, log = _world()
    out = _adopt(data, placement, ref)
    assert log == ["placement:222", "ref:222", "data:commit", "data:push:True"]
    assert (out.epoch, out.resumed) == (222, False)
    assert placement.view.epoch == ref.view.epoch == data.remote.writer.epoch == 222
    assert ref.view.frame == placement.view.frame == data.remote.writer.frame == ME


def test_every_bump_inserts_the_sentinel_and_a_fresh_revision():
    data, placement, ref, _ = _world()
    _adopt(data, placement, ref)
    head = data.remote
    assert head.live == 222
    assert head.marks == {"adopt-222": 222}  # older marks retired, sentinel inserted
    assert head.writer.revision not in ("r0", "")
    assert re.fullmatch(r"[0-9a-f]{64}", head.writer.revision)


def test_fresh_revision_never_repeats_for_the_same_record():
    revisions = {wa.fresh_revision(ME, 7) for _ in range(50)}
    assert len(revisions) == 50


def test_the_bump_is_typed_never_sql():
    """bh-jhgum: step 2 hands the fence a typed BumpRequest (frame, epoch, fresh revision, its
    own operation id); the adapter owns the statements."""
    data, placement, ref, _ = _world()
    seen: list[BumpRequest] = []
    commit = data.commit_bump

    def spy(bump):
        seen.append(bump)
        return commit(bump)

    data.commit_bump = spy
    _adopt(data, placement, ref)
    (bump,) = seen
    assert (bump.frame, bump.epoch, bump.reclaim) == (ME, 222, None)
    assert bump.revision == data.remote.writer.revision


def test_a_bump_at_or_below_the_floor_fails_closed():
    """IntegrityViolation from the store is never retried and never reported as incomplete."""
    data, placement, _ref, _ = _world()
    data.local.history_max = data.remote.history_max = 400  # the floor moved under the plan
    placement.view = wa.PlacementView(ME, 222, "tok")
    with pytest.raises(IntegrityViolation):
        wa.run_step2(data, placement, prefix=PREFIX, frame=ME, epoch=222)
    assert data.calls.count("commit") == 1 and "push" not in data.calls


@pytest.mark.parametrize("applied", [True, False], ids=["applied", "not-applied"])
def test_an_unknown_push_is_resolved_by_lookup_bump_never_reissued(applied):
    data, placement, _ref, _ = _world()
    placement.view = wa.PlacementView(ME, 222, "tok")
    push = data.push

    def unknown_push():
        if applied:
            push()
        data.calls.append("unknown")
        return Unknown("op", Revision("v0"), "transport")

    data.push = unknown_push
    result = wa.run_step2(data, placement, prefix=PREFIX, frame=ME, epoch=222)
    assert data.calls.count("commit") == 1  # one bump, never re-issued
    if applied:
        assert result.outcome is wa.Step2Outcome.LANDED and result.revision is not None
    else:
        assert result.outcome is wa.Step2Outcome.UNREACHABLE
        assert "push outcome unknown (transport)" in result.detail


@pytest.mark.parametrize("applied", [True, False], ids=["applied", "not-applied"])
def test_an_unknown_bump_commit_is_pushed_and_resolved_by_lookup_bump_never_reissued(applied):
    """bh-xxkf7.6: the commit point (or the head read after it) failed, so the bump may exist
    locally. Step 2 pushes (publishing it if it exists) and asks lookup_bump under the bump's
    own operation; it never commits the bump a second time."""
    data, placement, _ref, _ = _world()
    placement.view = wa.PlacementView(ME, 222, "tok")
    commit = data.commit_bump
    seen: list[BumpRequest] = []

    def unknown_commit(bump):
        seen.append(bump)
        if applied:
            commit(bump)
        else:
            data.calls.append("commit")
        return Unknown(bump.operation, Revision("v0"), "transport")

    lookups: list = []
    lookup = data.lookup_bump

    def spy_lookup(frame, epoch, *, operation):
        lookups.append(operation)
        return lookup(frame, epoch, operation=operation)

    data.commit_bump = unknown_commit
    data.lookup_bump = spy_lookup
    result = wa.run_step2(data, placement, prefix=PREFIX, frame=ME, epoch=222)
    assert data.calls.count("commit") == 1  # one bump, never re-issued
    assert lookups == [seen[0].operation]  # resolved under the bump's own operation
    if applied:
        assert result.outcome is wa.Step2Outcome.LANDED and result.revision is not None
        assert data.remote.writer == wa.WriterRow(ME, 222, seen[0].revision)
    else:
        assert result.outcome is wa.Step2Outcome.UNREACHABLE
        assert "bump commit outcome unknown (transport)" in result.detail
        assert data.remote.writer.frame == OLD


def test_without_the_ref_no_ref_cas_is_attempted():
    data, placement, ref, log = _world(ref=False)
    out = _adopt(data, placement, ref)
    assert ref.cas_calls == 0 and out.ref is None
    assert log == ["placement:222", "data:commit", "data:push:True"]


def test_a_legacy_hive_is_not_touched():
    data, placement, ref, log = _world()
    data.remote.writer = None
    with pytest.raises(wa.NotCutOver):
        _adopt(data, placement, ref)
    assert data.calls == ["remote_writer"]  # no sync: local main left exactly as it was
    assert log == []


# ---- idempotent re-run after a crash at every point ---------------------------------------


def _crash_after_placement(data, placement, ref):
    def boom(_view):
        raise Crash("after placement")

    return {"on_placed": boom}


def _crash_after_ref(data, placement, ref):
    real = ref.cas

    def cas(*a, **kw):
        real(*a, **kw)
        raise Crash("after ref")

    ref.cas = cas
    return {}


def _crash_before_push(data, placement, ref):
    def boom():
        raise Crash("after commit, before push")

    return {"before_push": boom}


def _crash_after_push(data, placement, ref):
    def boom():
        data.after_push.clear()
        raise Crash("after push")

    data.after_push.append(boom)
    return {}


@pytest.mark.parametrize(
    "inject",
    [_crash_after_placement, _crash_after_ref, _crash_before_push, _crash_after_push],
    ids=["after-placement", "after-ref", "before-push", "after-push"],
)
def test_rerun_after_a_crash_converges_without_a_second_bump(inject):
    data, placement, ref, log = _world()
    kw = inject(data, placement, ref)
    with pytest.raises(Crash):
        _adopt(data, placement, ref, **kw)
    if hasattr(ref, "cas") and "cas" in vars(ref):
        del ref.cas  # the "process" is gone; the re-run uses the real CAS
    out = _adopt(data, placement, ref)
    assert out.epoch == 222
    assert placement.cas_calls == 1  # placement was never moved again
    assert placement.view.epoch == ref.view.epoch == data.remote.writer.epoch == 222
    assert [b[1] for b in data.remote.bumps] == [222]  # exactly one bump on the remote
    assert data.remote.marks == {"adopt-222": 222}


def test_rerun_of_a_complete_adopt_is_a_no_op():
    data, placement, ref, log = _world()
    _adopt(data, placement, ref)
    log.clear()
    out = _adopt(data, placement, ref)
    assert out.resumed and out.epoch == 222
    assert log == []  # nothing written anywhere


def test_step2_is_idempotent_on_its_own():
    data, placement, ref, _ = _world()
    placement.view = wa.PlacementView(ME, 222, "t")
    first = wa.run_step2(data, placement, prefix=PREFIX, frame=ME, epoch=222)
    second = wa.run_step2(data, placement, prefix=PREFIX, frame=ME, epoch=222)
    assert first.landed and first.revision
    assert second.landed and second.revision is None  # converged, no second commit
    assert len(data.remote.bumps) == 1


# ---- losses ---------------------------------------------------------------------------------


def test_a_lost_placement_cas_writes_nothing():
    data, placement, ref, log = _world()
    real_read = placement.read
    placement.read = lambda prefix: (
        wa.PlacementView(OLD, 221, "stale-token") if real_read(prefix) else None
    )
    with pytest.raises(wa.PlacementLost):
        _adopt(data, placement, ref)
    assert ref.cas_calls == 0 and "data:commit" not in log


def test_the_fakes_are_placement_stores():
    _data, placement, _ref, _ = _world()
    assert isinstance(placement, PlacementStore)


@pytest.mark.parametrize("failover", [None, True, False], ids=["read", "failover", "planned"])
def test_the_placement_cas_records_the_adopt_kind_as_its_cause(failover):
    """bh-xxkf7.4: the port CAS carries a cause, the director's vocabulary. The displaced
    placement names another frame, so the read kind is a failover."""
    from beadhive import hq_sql_placement as sp

    assert (wa.PLACEMENT_CAUSE_FAILOVER, wa.PLACEMENT_CAUSE_PLANNED) == (
        sp.CAUSE_FAILOVER,
        sp.CAUSE_PLANNED,
    )
    data, placement, ref, _ = _world()
    _adopt(data, placement, ref, failover=failover)
    want = wa.PLACEMENT_CAUSE_PLANNED if failover is False else wa.PLACEMENT_CAUSE_FAILOVER
    assert placement.causes == [want]


class _Clock:
    def now(self):
        return datetime(2026, 10, 12, tzinfo=UTC)


class _Spy:
    """A :class:`~beadhive_hq_storage.PlacementStore` that counts what the adopt asks of it."""

    def __init__(self, inner):
        self.inner, self.cas, self.lookups = inner, [], []
        self.lookup_fails = False

    def read(self, prefix):
        return self.inner.read(prefix)

    def compare_and_set(self, prefix, frame, epoch, *, expected, cause, operation):
        self.cas.append((expected, operation))
        return self.inner.compare_and_set(
            prefix, frame, epoch, expected=expected, cause=cause, operation=operation
        )

    def lookup(self, prefix, operation):
        self.lookups.append(operation)
        if self.lookup_fails:
            raise StorageUnavailable("lookup partitioned")
        return self.inner.lookup(prefix, operation)


def _port_placement():
    store = InMemoryHqStorage(_Clock())
    store.provision(RecordKind.PLACEMENT, PREFIX, {"frame": OLD, "epoch": 221, "cause": "seed"})
    return store, _Spy(store.placements())


def test_the_adopt_drives_the_port_placement_store():
    data, _fake, ref, _ = _world()
    store, placement = _port_placement()
    out = _adopt(data, placement, ref)
    now = store.read(RecordKind.PLACEMENT, PREFIX)
    assert out.epoch == 222 and out.placement == wa.PlacementView(ME, 222, now.revision)
    assert now.body == {"frame": ME, "epoch": 222, "cause": wa.PLACEMENT_CAUSE_FAILOVER}
    assert data.remote.writer.epoch == 222 and len(placement.cas) == 1


def test_a_port_conflict_is_a_lost_placement_that_writes_nothing():
    data, _fake, ref, log = _world()
    store, placement = _port_placement()
    real_read = placement.read
    placement.read = lambda prefix: wa.PlacementView(OLD, 221, Revision("stale"))
    with pytest.raises(wa.PlacementLost, match="placement moved since it was read"):
        _adopt(data, placement, ref)
    placement.read = real_read
    assert len(placement.cas) == 1 and placement.lookups == []
    assert ref.cas_calls == 0 and "data:commit" not in log
    assert store.placements().read(PREFIX).epoch == 221


def test_an_unknown_placement_cas_confirmed_by_lookup_is_placed_never_reissued():
    data, _fake, ref, _ = _world()
    store, placement = _port_placement()
    store.faults.arm_unknown("commit", applied=True, on="compare_and_set")
    out = _adopt(data, placement, ref)
    ((expected, operation),) = placement.cas  # one CAS, never re-issued
    assert placement.lookups == [operation]  # resolved under the CAS's own operation
    assert expected.epoch == 221
    now = store.read(RecordKind.PLACEMENT, PREFIX)
    assert out.epoch == 222 and out.placement.token == now.revision
    assert data.remote.writer.epoch == 222 and ref.view.epoch == 222


@pytest.mark.parametrize("lookup_fails", [False, True], ids=["not-confirmed", "lookup-failed"])
def test_an_unconfirmed_placement_cas_is_unknown_and_never_reissued(lookup_fails):
    data, _fake, ref, log = _world()
    store, placement = _port_placement()
    placement.lookup_fails = lookup_fails
    store.faults.arm_unknown("transport", applied=False, on="compare_and_set")
    with pytest.raises(wa.PlacementUnknown) as excinfo:
        _adopt(data, placement, ref)
    ((_expected, operation),) = placement.cas  # one CAS, never re-issued, never re-minted
    assert placement.lookups == [operation]
    assert excinfo.value.operation == operation and excinfo.value.reason == "transport"
    assert wa.recovery_command(PREFIX) in str(excinfo.value)
    assert ref.cas_calls == 0 and "data:commit" not in log  # nothing past placement
    assert store.placements().read(PREFIX).epoch == 221
    # The re-run is a NEW decision from a fresh read: it places 222 under a new operation.
    out = _adopt(data, placement, ref)
    assert out.epoch == 222 and len(placement.cas) == 2 and placement.cas[1][1] != operation


def test_a_never_placed_hive_expects_absent():
    data, placement, ref, _ = _world()
    placement.view = None
    seen = []
    real = placement.compare_and_set

    def spy(prefix, frame, epoch, **kw):
        seen.append(kw["expected"])
        return real(prefix, frame, epoch, **kw)

    placement.compare_and_set = spy
    _adopt(data, placement, ref)
    assert seen == [ABSENT]


def test_a_lost_ref_cas_is_a_lost_adopt_never_retried():
    data, placement, ref, log = _world()
    real_read = ref.read
    ref.read = lambda: wa.RefView(OLD, 221, "stale-sha") if real_read() else None
    with pytest.raises(wa.EpochRefLost) as excinfo:
        _adopt(data, placement, ref)
    assert ref.cas_calls == 1  # one attempt, never retried with the same expectation
    assert "data:commit" not in log
    assert excinfo.value.report is not None
    assert wa.recovery_command(PREFIX) in str(excinfo.value)


def test_a_non_fast_forward_loops_back_to_the_data_check():
    data, placement, ref, log = _world()

    def race():
        # The old writer publishes a plain write (same epoch) under the bump.
        head = data.remote.copy()
        head.marks["late-write"] = 221
        data.foreign_push(head)

    out = _adopt(data, placement, ref, before_push=race)
    assert out.step2.attempts == 2
    assert log.count("data:commit") == 2
    assert data.remote.writer.epoch == 222 and data.remote.marks == {"adopt-222": 222}


def test_superseded_in_data_is_lost_not_incomplete():
    data, placement, ref, _ = _world()

    def overtaken():
        head = data.remote.copy()
        head.writer = wa.WriterRow("frame-b", 223, "rb")
        data.foreign_push(head)

    with pytest.raises(wa.AdoptLost):
        _adopt(data, placement, ref, before_push=overtaken)


def test_guard_incomplete_reports_adopt_incomplete_with_recovery():
    data, placement, ref, log = _world()
    data.triggers = 42
    with pytest.raises(wa.AdoptIncomplete) as excinfo:
        _adopt(data, placement, ref)
    msg = str(excinfo.value)
    assert "adopt incomplete (placement_ahead)" in msg
    assert "re-run `bh host lease adopt fx` here" in msg
    assert "Never roll placement back" in msg
    assert "data:commit" not in log
    # Recovery: the guard is completed (M1's install), the same command resumes at 222.
    data.triggers = wa.FENCE_TRIGGER_COUNT
    out = _adopt(data, placement, ref)
    assert out.resumed and out.epoch == 222 and placement.cas_calls == 1


def test_unreachable_data_mid_step2_is_incomplete():
    data, placement, ref, _ = _world()

    def partition():
        data.unreachable = True

    with pytest.raises(wa.AdoptIncomplete):
        _adopt(data, placement, ref, before_push=partition)
    data.unreachable = False
    assert _adopt(data, placement, ref).epoch == 222


def test_placement_re_placed_higher_elsewhere_is_never_resumed():
    """The recovery that re-places higher wins; the stale adopter does not roll it back."""
    data, placement, ref, _ = _world()
    data.triggers = 0
    with pytest.raises(wa.AdoptIncomplete):
        _adopt(data, placement, ref)
    placement.view = wa.PlacementView("frame-b", 223, "director")
    data.triggers = wa.FENCE_TRIGGER_COUNT
    result = wa.run_step2(data, placement, prefix=PREFIX, frame=ME, epoch=222)
    assert result.outcome is wa.Step2Outcome.PLACEMENT_MOVED


# ---- reporting and configuration ------------------------------------------------------------


def test_adopt_report_only_when_placement_is_ahead():
    w = wa.WriterRow(OLD, 221)
    assert wa.adopt_report(PREFIX, wa.PlacementView(OLD, 221), w) is None
    assert wa.adopt_report(PREFIX, None, w) is None
    assert wa.adopt_report(PREFIX, wa.PlacementView(ME, 222), None) is None
    report = wa.adopt_report(PREFIX, wa.PlacementView(ME, 222), w)
    assert report is not None
    other = report.describe(host_id="someone-else")
    assert "frame-c@222" in other and "re-places higher" in other
    assert "bh host lease adopt fx" in report.describe(host_id=ME)


def test_step2_attempts_is_configurable_and_refused_below_one(monkeypatch):
    assert wa.step2_attempts() == wa.DEFAULT_STEP2_ATTEMPTS
    monkeypatch.setenv(wa.STEP2_ATTEMPTS_ENV, "9")
    assert wa.step2_attempts() == 9
    assert wa.step2_attempts(2) == 2
    with pytest.raises(ValueError):
        wa.step2_attempts(0)
