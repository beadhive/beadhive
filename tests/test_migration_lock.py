"""Relocation lock = engine-mode lock + the HQ lease/primary model (bh-ksmt0.2)."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from beadhive import guard, migration_lock, storage_migrate


@pytest.fixture(autouse=True)
def _locks(tmp_path, monkeypatch):
    monkeypatch.setattr(storage_migrate, "_lock_dir", lambda cfg: tmp_path / "locks")
    monkeypatch.setattr(migration_lock, "holder_host_id", lambda: "host-a")


def _state(monkeypatch, holder):
    lease = SimpleNamespace(held_by=lambda h: h == holder)
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: ("bh", "host-a", lease))
    monkeypatch.setattr(
        guard,
        "_not_primary",
        lambda prefix, this, lease, **k: "" if lease.held_by(this) else "REFUSED",
    )


def test_reexports_the_engine_mode_lock():
    assert migration_lock.hive_migration_lock is storage_migrate.hive_migration_lock
    assert migration_lock.HiveLocked is storage_migrate.HiveLocked


def test_single_host_default_when_no_lease_in_force(monkeypatch):
    monkeypatch.setattr(guard, "primary_state", lambda *a, **k: None)
    with migration_lock.transaction_lock({}, "github/o/h") as host:
        assert host == "host-a"


def test_lease_holder_gets_lock_and_excludes_engine_migrator(monkeypatch):
    _state(monkeypatch, "host-a")
    with migration_lock.transaction_lock({}, "github/o/h") as host:
        assert host == "host-a"
        with pytest.raises(storage_migrate.HiveLocked):
            with storage_migrate.hive_migration_lock({}, "github/o/h"):
                pass  # pragma: no cover
    with migration_lock.transaction_lock({}, "github/o/h"):
        pass


def test_non_holder_is_refused_before_locking(monkeypatch):
    _state(monkeypatch, "host-b")
    with pytest.raises(migration_lock.NotLeaseHolder, match="REFUSED"):
        with migration_lock.transaction_lock({}, "github/o/h"):
            pass  # pragma: no cover
    with storage_migrate.hive_migration_lock({}, "github/o/h"):
        pass  # no lock was left behind
