"""Registry capture wrapping the engine's one registry implementation (bh-ksmt0.2)."""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from beadhive import registry_snapshot as rs
from beadhive.dolt_remote import RegistryEntry, RegistryKind, RegistrySnapshot

CLOCK = lambda: datetime(2026, 10, 19, tzinfo=UTC)  # noqa: E731


def snapshot(origin="file:///a"):
    return RegistrySnapshot(
        remotes=(RegistryEntry(RegistryKind.REMOTE, "origin", origin),),
        backups=(RegistryEntry(RegistryKind.BACKUP, "default", "file:///bk"),),
    )


class FakeEngine:
    def __init__(self, snap=None, ok=True):
        self._read = SimpleNamespace(ok=ok, error="" if ok else "boom", snapshot=snap)
        self.restored = None

    def read_registry(self, cwd):
        return self._read

    def restore_registry(self, cwd, snap, *, extra_env=None):
        self.restored = snap
        return SimpleNamespace(ok=True)


def cap(snap=None, clock=CLOCK, **kw):
    return rs.capture(
        Path("/x"),
        hive="h",
        engine=FakeEngine(snap or snapshot()),
        clock=clock,
        sync_remote=lambda d: "git+ssh://r/x",
        **kw,
    )


def test_capture_wraps_engine_snapshot_with_descriptor_and_sync_remote():
    c = cap(hq_descriptor={"name": "origin", "url": "file:///a"})
    assert c.snapshot == snapshot() and c.sync_remote == "git+ssh://r/x"
    assert c.taken_at.startswith("2026-10-19")


def test_failed_read_refuses_rather_than_empty():
    with pytest.raises(rs.RegistryUnreadable, match="boom"):
        rs.capture(
            Path("/x"), hive="h", engine=FakeEngine(ok=False), clock=CLOCK, sync_remote=lambda d: ""
        )


def test_write_is_private_atomic_roundtrips_raw_and_detects_tamper(tmp_path):
    c = cap()
    path = tmp_path / "d" / "registry.json"
    c.write(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not list(path.parent.glob(".cap-*"))
    loaded = rs.RegistryCapture.load(path)
    assert loaded.snapshot.to_raw() == c.snapshot.to_raw()
    doc = json.loads(path.read_text())
    doc["registry"]["remotes"][0]["url"] = "file:///evil"
    path.write_text(json.dumps(doc))
    with pytest.raises(ValueError, match="digest"):
        rs.RegistryCapture.load(path)


def test_digest_ignores_time_and_diff_is_redacted():
    a = cap()
    b = cap(clock=lambda: datetime(2030, 1, 1, tzinfo=UTC))
    assert a.digest() == b.digest() and a.diff(b) == ()
    moved = cap(snapshot("s3://u:pw@h/b"))
    assert any("origin" in line for line in moved.diff(a))
    assert "pw" not in json.dumps(moved.redacted()) and "pw" not in " ".join(moved.diff(a))


def test_restore_delegates_to_engine():
    eng = FakeEngine()
    c = cap()
    c.restore(eng, Path("/x"))
    assert eng.restored == c.snapshot
