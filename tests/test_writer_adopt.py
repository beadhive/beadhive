"""Placement-first coexistence adopt with an idempotent step 2 (bh-4c7p4, ADR §2, P-M2).

Fast, in-memory: the three ports (:class:`FenceData`, :class:`PlacementAuthority`,
:class:`EpochRef`) are faithful fakes of the remote head, HQ placement and ``refs/bh/epoch``,
so the ORDER, the idempotence after a crash at every point, and the loss semantics are proven
here. ``tests/test_writer_adopt_int.py`` runs the same core against real Dolt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import pytest

from beadhive import writer_adopt as wa

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


_UPDATE_WRITER = re.compile(
    r"UPDATE bh_writer SET frame = '(?P<frame>[^']*)', epoch = (?P<epoch>\d+), "
    r"revision = '(?P<rev>[0-9a-f]{64})' WHERE id = 1"
)


class FakeData:
    """The hive remote + one node's local main. ``push`` is a fast-forward-only CAS on the
    remote head, so a competing push in between is a genuine non-fast-forward."""

    def __init__(self, remote: Head, *, triggers: int = wa.FENCE_TRIGGER_COUNT):
        self.remote = remote
        self.local = remote.copy()
        self.base_version = 0
        self.version = 0
        self.triggers = triggers
        self.calls: list[str] = []
        self.unreachable = False
        self.after_push: list = []

    def sync_to_remote(self):
        self.calls.append("sync")
        if self.unreachable:
            raise wa.DataUnreachable("partitioned")
        self.local = self.remote.copy()
        self.base_version = self.version

    def writer(self):
        return self.local.writer

    def remote_writer(self):
        self.calls.append("remote_writer")
        return self.remote.writer

    def history_max_epoch(self):
        return self.local.history_max

    def trigger_count(self):
        return self.triggers

    def commit_bump(self, statements, message):
        self.calls.append("commit")
        m = _UPDATE_WRITER.fullmatch(statements[0])
        assert m, statements[0]
        frame, epoch, rev = m["frame"], int(m["epoch"]), m["rev"]
        assert statements[1] == f"DELETE FROM bh_write_mark WHERE epoch < {epoch}"
        assert statements[2] == f"UPDATE bh_epoch_live SET epoch = {epoch} WHERE id = 1"
        assert f"'adopt-{epoch}', {epoch}" in statements[3]
        assert message == f"bh: adopt {frame}@{epoch}"
        head = self.local
        assert head.writer is None or epoch > head.writer.epoch  # monotonic trigger
        head.writer = wa.WriterRow(frame, epoch, rev)
        head.marks = {k: v for k, v in head.marks.items() if v >= epoch}
        head.live = epoch
        head.marks[f"adopt-{epoch}"] = epoch
        head.history_max = max(head.history_max, epoch)
        head.bumps.append((frame, epoch, rev))

    def push(self):
        self.calls.append("push")
        if self.unreachable:
            raise wa.DataUnreachable("partitioned")
        if self.version != self.base_version:
            return False
        self.remote = self.local.copy()
        self.version += 1
        self.base_version = self.version
        for hook in self.after_push:
            hook()
        return True

    def foreign_push(self, head: Head):
        """Another frame publishes; this node's next push is non-fast-forward."""
        self.remote = head
        self.version += 1


class FakePlacement:
    def __init__(self, frame: str | None, epoch: int, log: list[str]):
        self.view = None if frame is None else wa.PlacementView(frame, epoch, f"tok{epoch}")
        self.log = log
        self.cas_calls = 0

    def read(self):
        return self.view

    def cas(self, frame, epoch, *, expected):
        self.cas_calls += 1
        self.log.append(f"placement:{epoch}")
        if expected != self.view:
            raise wa.PlacementLost("moved")
        self.view = wa.PlacementView(frame, epoch, f"tok{epoch}-{self.cas_calls}")
        return self.view


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

    def commit_bump(self, statements, message):
        self.log.append("data:commit")
        super().commit_bump(statements, message)

    def push(self):
        ok = super().push()
        self.log.append(f"data:push:{ok}")
        return ok


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


def test_bump_statements_quote_the_frame():
    stmts = wa.bump_statements("o'brien", 3, "ab")
    assert "frame = 'o''brien'" in stmts[0]
    assert stmts[3] == (
        "INSERT INTO bh_write_mark (id, epoch, tbl) VALUES ('adopt-3', 3, 'bh_writer')"
    )


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
    first = wa.run_step2(data, placement, frame=ME, epoch=222)
    second = wa.run_step2(data, placement, frame=ME, epoch=222)
    assert first.landed and first.revision
    assert second.landed and second.revision is None  # converged, no second commit
    assert len(data.remote.bumps) == 1


# ---- losses ---------------------------------------------------------------------------------


def test_a_lost_placement_cas_writes_nothing():
    data, placement, ref, log = _world()
    real_read = placement.read
    placement.read = lambda: wa.PlacementView(OLD, 221, "stale-token") if real_read() else None
    with pytest.raises(wa.PlacementLost):
        _adopt(data, placement, ref)
    assert ref.cas_calls == 0 and "data:commit" not in log


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
    result = wa.run_step2(data, placement, frame=ME, epoch=222)
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
