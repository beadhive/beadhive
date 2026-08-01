"""bh hq restore (bh-cmqp.1) — the consumer for what hq._take_backup writes.

The headline test is the ROUND TRIP: back up, DESTROY the store, restore, and assert the
content came back. Asserting that files exist would pass against a restore that silently does
nothing, which is the exact failure this bead exists to prevent.
"""

from __future__ import annotations

import json
import subprocess
import tarfile

import pytest

from beadhive import hq_restore

STORE_REL = ".beads/embeddeddolt"


@pytest.fixture
def hq_dir(tmp_path, monkeypatch):
    """A fake HQ with a populated embedded store."""
    hq = tmp_path / "hq"
    store = hq / STORE_REL
    store.mkdir(parents=True)
    (store / "manifest").write_text("real store contents\n")
    (store / "nested").mkdir()
    (store / "nested" / "data").write_text("nested payload\n")
    monkeypatch.setattr(hq_restore.config, "hq_dir", lambda: hq)
    return hq


@pytest.fixture
def backup_root(tmp_path, monkeypatch):
    root = tmp_path / "hq-backups"
    monkeypatch.setattr(hq_restore, "_backup_root", lambda cfg: root)
    return root


def _write_backup(root, label, *, tar_from=None, issues=None):
    d = root / label
    d.mkdir(parents=True)
    if tar_from is not None:
        with tarfile.open(d / "hq-embeddeddolt.tar.gz", "w:gz") as tf:
            tf.add(tar_from, arcname="embeddeddolt")
    if issues is not None:
        (d / "hq-issues.jsonl").write_text(
            "\n".join(json.dumps({"id": i, "title": f"issue {i}"}) for i in issues) + "\n"
        )
    return d


# ---- discovery ----------------------------------------------------------------


def test_no_backups_lists_empty(backup_root):
    assert hq_restore.list_backups({}) == []


def test_backups_are_listed_newest_first(backup_root, hq_dir):
    _write_backup(backup_root, "2026-07-01", issues=["a"])
    _write_backup(backup_root, "2026-08-01", issues=["b"])
    labels = [s.label for s in hq_restore.list_backups({})]
    assert labels == ["2026-08-01", "2026-07-01"]


def test_a_directory_with_no_levels_is_not_listed(backup_root):
    (backup_root / "2026-08-01").mkdir(parents=True)
    assert hq_restore.list_backups({}) == []


def test_levels_reports_what_the_directory_actually_holds(backup_root, hq_dir):
    _write_backup(backup_root, "2026-08-01", tar_from=hq_dir / STORE_REL, issues=["a"])
    (only_jsonl,) = [s for s in hq_restore.list_backups({}) if s.label == "2026-08-01"]
    assert only_jsonl.levels() == ["tar", "jsonl"]


# ---- level selection ----------------------------------------------------------


def test_auto_prefers_tar(backup_root, hq_dir):
    _write_backup(backup_root, "2026-08-01", tar_from=hq_dir / STORE_REL, issues=["a"])
    assert hq_restore.resolve_level(hq_restore.list_backups({})[0], "auto") == "tar"


def test_auto_falls_back_to_jsonl_when_there_is_no_tar(backup_root):
    _write_backup(backup_root, "2026-08-01", issues=["a"])
    assert hq_restore.resolve_level(hq_restore.list_backups({})[0], "auto") == "jsonl"


# ---- safety -------------------------------------------------------------------


def test_dry_run_mutates_nothing(backup_root, hq_dir):
    _write_backup(backup_root, "2026-08-01", tar_from=hq_dir / STORE_REL)
    (hq_dir / STORE_REL / "manifest").write_text("LIVE\n")

    out = hq_restore.restore({}, hq_restore.list_backups({})[0], dry_run=True)

    assert out.ok and out.dry_run
    assert (hq_dir / STORE_REL / "manifest").read_text() == "LIVE\n"
    assert out.moved_aside is None


def test_a_real_restore_refuses_without_confirm(backup_root, hq_dir):
    _write_backup(backup_root, "2026-08-01", tar_from=hq_dir / STORE_REL)
    (hq_dir / STORE_REL / "manifest").write_text("LIVE\n")

    out = hq_restore.restore({}, hq_restore.list_backups({})[0], dry_run=False, confirm=False)

    assert not out.ok
    assert any("--confirm" in a for a in out.actions)
    assert (hq_dir / STORE_REL / "manifest").read_text() == "LIVE\n"


def test_requesting_a_level_the_backup_lacks_fails_cleanly(backup_root, hq_dir):
    _write_backup(backup_root, "2026-08-01", issues=["a"])  # jsonl only
    out = hq_restore.restore({}, hq_restore.list_backups({})[0], level="tar", dry_run=True)
    assert not out.ok


# ---- THE round trip -----------------------------------------------------------


def test_tar_round_trip_survives_the_store_being_destroyed(backup_root, hq_dir):
    """Back up -> DESTROY -> restore -> content matches. The whole point of the bead."""
    store = hq_dir / STORE_REL
    _write_backup(backup_root, "2026-08-01", tar_from=store)

    # Destroy it the way a corruption/wipe would.
    import shutil

    shutil.rmtree(store)
    assert not store.exists()

    out = hq_restore.restore({}, hq_restore.list_backups({})[0], dry_run=False, confirm=True)

    assert out.ok, out.actions
    assert (store / "manifest").read_text() == "real store contents\n"
    assert (store / "nested" / "data").read_text() == "nested payload\n"


def test_tar_restore_keeps_the_previous_store_rather_than_deleting_it(backup_root, hq_dir):
    store = hq_dir / STORE_REL
    _write_backup(backup_root, "2026-08-01", tar_from=store)
    (store / "manifest").write_text("SUPERSEDED\n")

    out = hq_restore.restore({}, hq_restore.list_backups({})[0], dry_run=False, confirm=True)

    assert out.ok
    assert out.moved_aside is not None
    from pathlib import Path

    assert (Path(out.moved_aside) / "manifest").read_text() == "SUPERSEDED\n"
    assert (store / "manifest").read_text() == "real store contents\n"


def test_a_failed_extract_rolls_the_previous_store_back(backup_root, hq_dir):
    """A restore that dies mid-extract must not leave the operator with neither store."""
    store = hq_dir / STORE_REL
    d = backup_root / "2026-08-01"
    d.mkdir(parents=True)
    (d / "hq-embeddeddolt.tar.gz").write_bytes(b"not a valid gzip stream")
    (store / "manifest").write_text("LIVE\n")

    out = hq_restore.restore({}, hq_restore.list_backups({})[0], dry_run=False, confirm=True)

    assert not out.ok
    assert (store / "manifest").read_text() == "LIVE\n"  # rolled back
    assert out.moved_aside is None


def test_jsonl_restore_works_with_no_readable_store(backup_root, hq_dir, monkeypatch):
    """The format-independent floor: it must survive the case the tarball does not — a Dolt
    store too broken to read — so it stands a store up rather than assuming one."""
    import shutil

    shutil.rmtree(hq_dir / STORE_REL)
    _write_backup(backup_root, "2026-08-01", issues=["bh-1", "bh-2"])

    ensured, imported = [], []
    monkeypatch.setattr(hq_restore.hub, "ensure_store", lambda d, p: ensured.append((d, p)))
    monkeypatch.setattr(
        hq_restore.engine,
        "get_engine",
        lambda cfg: type(
            "E", (), {"import_jsonl": lambda self, cwd, args: (
                imported.append(args) or subprocess.CompletedProcess(args, 0)
            )}
        )(),
    )

    out = hq_restore.restore({}, hq_restore.list_backups({})[0], dry_run=False, confirm=True)

    assert out.ok, out.actions
    assert ensured, "must stand up a store when none is readable"
    assert imported and imported[0][0].endswith("hq-issues.jsonl")


def test_jsonl_restore_reports_an_import_failure(backup_root, hq_dir, monkeypatch):
    _write_backup(backup_root, "2026-08-01", issues=["bh-1"])
    monkeypatch.setattr(hq_restore.hub, "ensure_store", lambda d, p: None)
    monkeypatch.setattr(
        hq_restore.engine,
        "get_engine",
        lambda cfg: type(
            "E", (), {"import_jsonl": lambda self, cwd, args: subprocess.CompletedProcess(
                args, 1, stdout="", stderr="boom"
            )}
        )(),
    )

    out = hq_restore.restore({}, hq_restore.list_backups({})[0], dry_run=False, confirm=True)
    assert not out.ok
