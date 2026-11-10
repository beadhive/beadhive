"""The root half of the one-process outbox batch (``bh-pc9rf``): ``beadhive.intake_services``.

Pins what only the composition root adds: the flock lock's batch scope (one host check and epoch
read reused for at most ``batch_recheck_seconds``, measured on an injected fake clock, never by
waiting), the file-backed outbox id index (atomic, merged, rebuilt, and a cache that can never
fail a send), and the default wiring of both. No real clock, no sleep, no socket, no tracker.
"""

from __future__ import annotations

import json

import pytest

from beadhive import guard, intake_services, private_paths
from beadhive.adapters.intake import FileOutboxIndex
from beadhive.adapters.intake.outbox_index import INDEX_FILE
from beadhive.modules.intake import IntakeRefused, IntakeStoreError, RefusalReason
from beadhive.modules.intake.contracts import BatchLock, OutboxIndex


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class Calls:
    def __init__(self) -> None:
        self.primary = 0
        self.epoch = 0
        self.refuse = False

    def check_primary(self) -> None:
        self.primary += 1
        if self.refuse:
            raise IntakeRefused(RefusalReason.NOT_PRIMARY, "not this host")

    def read_epoch(self) -> int:
        self.epoch += 1
        return 40 + self.epoch


def _lock(tmp_path, calls: Calls, *, recheck: float = 60.0, clock=None):
    return intake_services.FlockIntakeLock(
        tmp_path / "locks",
        primary=calls.check_primary,
        epoch=calls.read_epoch,
        recheck_seconds=recheck,
        clock=clock or FakeClock(),
    )


# -- the lock's batch scope ----------------------------------------------------------------------


def test_the_flock_lock_is_a_batch_lock(tmp_path) -> None:
    assert isinstance(_lock(tmp_path, Calls()), BatchLock)


def test_outside_a_batch_every_hold_checks_the_host_and_reads_the_epoch(tmp_path) -> None:
    calls = Calls()
    lock = _lock(tmp_path, calls)
    epochs = []
    for key in ("a", "b", "a"):
        with lock.hold(key) as epoch:
            epochs.append(epoch)
    assert (calls.primary, calls.epoch) == (3, 3) and epochs == [41, 42, 43]


def test_a_batch_reuses_one_check_until_it_is_recheck_seconds_old(tmp_path) -> None:
    calls, clock = Calls(), FakeClock()
    lock = _lock(tmp_path, calls, recheck=60.0, clock=clock)
    epochs = []
    with lock.batch():
        for key in ("a", "b", "c"):
            with lock.hold(key) as epoch:
                epochs.append(epoch)
            clock.now += 10.0
        assert (calls.primary, calls.epoch) == (1, 1) and epochs == [41, 41, 41]
        clock.now += 40.0  # 70 s after the check: taken again
        with lock.hold("d") as epoch:
            epochs.append(epoch)
        assert (calls.primary, calls.epoch) == (2, 2) and epochs[-1] == 42
        with lock.hold("e") as epoch:  # and reused again from the new check
            epochs.append(epoch)
    assert (calls.primary, calls.epoch) == (2, 2)
    with lock.hold("a"):  # the scope is gone: a single hold checks afresh
        pass
    assert (calls.primary, calls.epoch) == (3, 3)


def test_each_hold_of_a_batch_still_takes_its_own_flock(tmp_path) -> None:
    calls = Calls()
    lock = _lock(tmp_path, calls)
    with lock.batch(), lock.hold("a"):
        with pytest.raises(IntakeRefused) as busy:
            with lock.hold("a"):
                pass
        assert busy.value.reason is RefusalReason.INTAKE_BUSY
    assert calls.primary == 1


def test_recheck_zero_checks_on_every_hold_even_in_a_batch(tmp_path) -> None:
    calls = Calls()
    lock = _lock(tmp_path, calls, recheck=0)
    with lock.batch():
        for key in ("a", "b", "c"):
            with lock.hold(key):
                pass
    assert (calls.primary, calls.epoch) == (3, 3)


def test_a_refused_host_check_is_never_reused(tmp_path) -> None:
    calls = Calls()
    lock = _lock(tmp_path, calls)
    with lock.batch():
        calls.refuse = True
        for _ in range(2):
            with pytest.raises(IntakeRefused) as refused:
                with lock.hold("a"):
                    pass
            assert refused.value.reason is RefusalReason.NOT_PRIMARY
    assert calls.primary == 2 and calls.epoch == 0


def test_the_production_lock_reads_its_bound_from_config(tmp_path, monkeypatch) -> None:
    checks: list[str] = []
    monkeypatch.setattr(guard, "guard_primary", lambda hive, *, cfg, verb: checks.append(hive))
    monkeypatch.setattr(guard, "live_epoch", lambda hive, *, cfg: 5)
    monkeypatch.setattr(private_paths, "ensure_repo_private_root", lambda d: tmp_path / ".bh")
    cfg = {"intake": {"external": {"outbox": {"batch_recheck_seconds": 0}}}}
    lock = intake_services.intake_lock(cfg, "/hive", hive="acme")
    with lock.batch():
        for key in ("a", "b"):
            with lock.hold(key):
                pass
    assert checks == ["acme", "acme"]  # 0: every hold re-checks

    checks.clear()
    lock = intake_services.intake_lock({}, "/hive", hive="acme")
    with lock.batch():
        for key in ("a", "b"):
            with lock.hold(key):
                pass
    assert checks == ["acme", "acme"]  # the default is 0 too: reuse is opt-in

    checks.clear()
    cfg = {"intake": {"external": {"outbox": {"batch_recheck_seconds": 300}}}}
    lock = intake_services.intake_lock(cfg, "/hive", hive="acme")
    with lock.batch():
        for key in ("a", "b"):
            with lock.hold(key):
                pass
    assert checks == ["acme"]  # opted in: one check reused


# -- the one-evaluation form, ``admit`` (production since bh-25fvb) ---------------------------


class Admits:
    """``guard.primary_epoch`` as the lock sees it: the host check and the epoch in one call."""

    def __init__(self) -> None:
        self.calls = 0
        self.refuse = False

    def __call__(self) -> int:
        self.calls += 1
        if self.refuse:
            raise IntakeRefused(RefusalReason.NOT_PRIMARY, "not this host")
        return 40 + self.calls


def _admit_lock(tmp_path, admit: Admits, *, recheck: float = 60.0, clock=None):
    return intake_services.FlockIntakeLock(
        tmp_path / "locks", admit=admit, recheck_seconds=recheck, clock=clock or FakeClock()
    )


def test_the_lock_needs_admit_or_both_halves(tmp_path) -> None:
    with pytest.raises(TypeError, match="admit, or both primary and epoch"):
        intake_services.FlockIntakeLock(tmp_path / "locks", primary=lambda: None)
    with pytest.raises(TypeError):
        intake_services.FlockIntakeLock(tmp_path / "locks", epoch=lambda: 1)
    with pytest.raises(TypeError):
        intake_services.FlockIntakeLock(tmp_path / "locks")


def test_admit_runs_once_per_hold_and_its_epoch_is_yielded(tmp_path) -> None:
    admit = Admits()
    lock = _admit_lock(tmp_path, admit)
    epochs = []
    for key in ("a", "b", "a"):
        with lock.hold(key) as epoch:
            epochs.append(epoch)
    assert admit.calls == 3 and epochs == [41, 42, 43]


def test_admit_is_reused_in_a_batch_until_recheck_seconds_old(tmp_path) -> None:
    admit, clock = Admits(), FakeClock()
    lock = _admit_lock(tmp_path, admit, recheck=60.0, clock=clock)
    epochs = []
    with lock.batch():
        for key in ("a", "b"):
            with lock.hold(key) as epoch:
                epochs.append(epoch)
            clock.now += 30.0
        with lock.hold("c") as epoch:  # 60 s after the check: taken again
            epochs.append(epoch)
    assert admit.calls == 2 and epochs == [41, 41, 42]
    with lock.hold("a") as epoch:  # the scope is gone: a single hold admits afresh
        assert epoch == 43


def test_admit_with_recheck_zero_runs_on_every_hold_of_a_batch(tmp_path) -> None:
    admit = Admits()
    lock = _admit_lock(tmp_path, admit, recheck=0)
    with lock.batch():
        for key in ("a", "b", "c"):
            with lock.hold(key):
                pass
    assert admit.calls == 3


def test_a_refused_admit_takes_no_flock_and_is_never_reused(tmp_path) -> None:
    admit = Admits()
    lock = _admit_lock(tmp_path, admit)
    with lock.batch():
        admit.refuse = True
        for _ in range(2):
            with pytest.raises(IntakeRefused) as refused:
                with lock.hold("a"):
                    pytest.fail("a refused host check must not yield")
            assert refused.value.reason is RefusalReason.NOT_PRIMARY
        assert admit.calls == 2
        admit.refuse = False
        with lock.hold("a") as epoch:  # nothing was left holding the binding's flock
            assert epoch == 43


def test_admit_runs_before_the_flock_and_a_busy_binding_still_refuses(tmp_path) -> None:
    admit = Admits()
    lock = _admit_lock(tmp_path, admit)
    with lock.hold("a"):
        with pytest.raises(IntakeRefused) as busy:
            with lock.hold("a"):
                pass
        assert busy.value.reason is RefusalReason.INTAKE_BUSY
    assert admit.calls == 2


def test_the_production_lock_admits_through_primary_epoch(tmp_path, monkeypatch) -> None:
    seen: list[tuple] = []

    def primary_epoch(hive, *, cfg, verb):
        seen.append((hive, verb))
        return 5

    def unused(*_a, **_k):
        raise AssertionError("the production lock takes the host check and epoch in one call")

    monkeypatch.setattr(guard, "primary_epoch", primary_epoch)
    monkeypatch.setattr(guard, "guard_primary", unused)
    monkeypatch.setattr(guard, "live_epoch", unused)
    monkeypatch.setattr(private_paths, "ensure_repo_private_root", lambda d: tmp_path / ".bh")
    lock = intake_services.intake_lock({}, "/hive", hive="acme", verb="work labels")
    with lock.hold("a") as epoch:
        assert epoch == 5
    assert seen == [("acme", "work labels")]


def test_the_production_lock_maps_a_refused_host_to_not_primary(tmp_path, monkeypatch) -> None:
    import typer

    def refuse(hive, *, cfg, verb):
        raise typer.Exit(1)

    monkeypatch.setattr(guard, "primary_epoch", refuse)
    monkeypatch.setattr(private_paths, "ensure_repo_private_root", lambda d: tmp_path / ".bh")
    lock = intake_services.intake_lock({}, "/hive", hive="acme", verb="work outbox")
    with pytest.raises(IntakeRefused) as refused:
        with lock.hold("a"):
            pytest.fail("a refused host check must not yield")
    assert refused.value.reason is RefusalReason.NOT_PRIMARY
    assert "bh work outbox" in str(refused.value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, 0.0), (0, 0.0), (45, 45.0), (300, 300.0), (-1, 0.0), (True, 0.0), ("10", 0.0)],
)
def test_batch_recheck_seconds_reads_a_non_negative_number(value, expected) -> None:
    outbox = {} if value is None else {"batch_recheck_seconds": value}
    cfg = {"intake": {"external": {"outbox": outbox}}}
    assert intake_services.batch_recheck_seconds(cfg) == expected
    assert intake_services.batch_recheck_seconds({}) == 0.0


# -- the file index ------------------------------------------------------------------------------


def test_the_file_index_is_an_outbox_index(tmp_path) -> None:
    assert isinstance(FileOutboxIndex(tmp_path / INDEX_FILE), OutboxIndex)


def test_the_file_index_merges_adds_and_replaces_on_rebuild(tmp_path) -> None:
    path = tmp_path / "intake" / INDEX_FILE
    index = FileOutboxIndex(path)
    assert index.report_of("ob1_a") is None  # no file yet: a miss, never an error

    index.add({"ob1_a": "acme-1"})
    FileOutboxIndex(path).add({"ob1_b": "acme-2"})  # another writer, the same file
    assert (index.report_of("ob1_a"), index.report_of("ob1_b")) == ("acme-1", "acme-2")

    index.rebuild({"ob1_b": "acme-2", "ob1_c": "acme-3"})
    assert index.report_of("ob1_a") is None and index.report_of("ob1_c") == "acme-3"
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored == {"schema": 1, "records": {"ob1_b": "acme-2", "ob1_c": "acme-3"}}
    assert sorted(p.name for p in path.parent.iterdir()) == [f".{INDEX_FILE}.lock", INDEX_FILE]


def test_an_unchanged_update_does_not_rewrite(tmp_path) -> None:
    path = tmp_path / INDEX_FILE
    index = FileOutboxIndex(path)
    index.add({"ob1_a": "acme-1"})
    before = path.stat().st_ino
    index.add({"ob1_a": "acme-1"})
    index.rebuild({"ob1_a": "acme-1"})
    assert path.stat().st_ino == before  # never replaced


@pytest.mark.parametrize(
    "content",
    ["{", "[]", json.dumps({"schema": 9, "records": {}}), json.dumps({"schema": 1})],
)
def test_a_malformed_index_reads_empty_and_is_rebuilt(tmp_path, content) -> None:
    path = tmp_path / INDEX_FILE
    path.write_text(content, encoding="utf-8")
    index = FileOutboxIndex(path)
    assert index.report_of("ob1_a") is None
    index.rebuild({"ob1_a": "acme-1"})
    assert index.report_of("ob1_a") == "acme-1"


def test_an_index_that_cannot_be_resolved_is_no_index(tmp_path) -> None:
    def unresolved():
        raise IntakeStoreError("no private .bh/ state", operation="outbox_index")

    index = FileOutboxIndex(unresolved)
    index.add({"ob1_a": "acme-1"})
    index.rebuild({"ob1_a": "acme-1"})
    assert index.report_of("ob1_a") is None


def test_an_index_that_cannot_be_written_never_fails_the_caller(tmp_path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    index = FileOutboxIndex(blocker / INDEX_FILE)  # its parent is a file
    index.add({"ob1_a": "acme-1"})
    assert index.report_of("ob1_a") is None


def test_the_root_index_lives_in_the_hives_private_intake_state(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(private_paths, "ensure_repo_private_root", lambda d: tmp_path / ".bh")
    index = intake_services.outbox_index("/hive")
    index.add({"ob1_a": "acme-1"})
    assert (tmp_path / ".bh" / "intake" / INDEX_FILE).is_file()
    assert intake_services.outbox_index("/hive").report_of("ob1_a") == "acme-1"
