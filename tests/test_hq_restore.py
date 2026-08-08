"""bh hq restore (bh-cmqp.1) — the consumer for what hq._take_backup writes.

The headline test is the ROUND TRIP: back up, DESTROY the store, restore, and assert the
content came back. Asserting that files exist would pass against a restore that silently does
nothing, which is the exact failure this bead exists to prevent.
"""

from __future__ import annotations

import json
import subprocess
import tarfile
from pathlib import Path

import pytest

from beadhive import hq_restore

STORE_REL = ".beads/embeddeddolt"


def _write_metadata(hq: Path, *, dolt_mode: str | None) -> None:
    """bd's own `.beads/metadata.json` — the FILESYSTEM FACT `store_locator.is_embedded_mode`
    reads (bh-areg.1), replacing the old `_bd_dolt_mode` probe mock. `dolt_mode=None` omits
    the file entirely (the "unknown" case)."""
    (hq / ".beads").mkdir(parents=True, exist_ok=True)
    if dolt_mode is None:
        return
    (hq / ".beads" / "metadata.json").write_text(json.dumps({"dolt_mode": dolt_mode}))


@pytest.fixture
def hq_dir(tmp_path, monkeypatch):
    """A fake HQ with a populated embedded store AND the metadata bd itself would have
    written for embedded mode — the fact `restore()` now checks instead of probing `bd dolt
    status` (bh-areg.1). Written up front (unlike the real store dir) so it survives a test
    that deliberately destroys the store to exercise recovery."""
    hq = tmp_path / "hq"
    store = hq / STORE_REL
    store.mkdir(parents=True)
    (store / "manifest").write_text("real store contents\n")
    (store / "nested").mkdir()
    (store / "nested" / "data").write_text("nested payload\n")
    _write_metadata(hq, dolt_mode="embedded")
    monkeypatch.setattr(hq_restore.config, "hq_dir", lambda: hq)
    return hq


@pytest.fixture
def backup_root(tmp_path, monkeypatch):
    root = tmp_path / "hq-backups"
    monkeypatch.setattr(hq_restore, "_backup_root", lambda cfg: root)
    return root


def _write_backup(root, label, *, tar_from=None, issues=None, native_from=None):
    d = root / label
    d.mkdir(parents=True)
    if tar_from is not None:
        with tarfile.open(d / "hq-embeddeddolt.tar.gz", "w:gz") as tf:
            tf.add(tar_from, arcname="embeddeddolt")
    if issues is not None:
        (d / "hq-issues.jsonl").write_text(
            "\n".join(json.dumps({"id": i, "title": f"issue {i}"}) for i in issues) + "\n"
        )
    if native_from is not None:
        native = d / hq_restore._native_dirname()
        native.mkdir(parents=True)
        (native / "payload").write_text(native_from)
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


def test_levels_reports_a_native_backup_alongside_jsonl(backup_root):
    """A non-embedded HQ's backup set (bh-areg.1): connection-oriented ``native``, no ``tar``
    at all — the shape ``hq._take_backup`` actually produces off embedded mode."""
    _write_backup(backup_root, "2026-08-01", native_from="dolt-native bytes", issues=["a"])
    (found,) = [s for s in hq_restore.list_backups({}) if s.label == "2026-08-01"]
    assert found.levels() == ["native", "jsonl"]
    assert found.full_fidelity == found.native


# ---- level selection ----------------------------------------------------------


def test_auto_prefers_tar(backup_root, hq_dir):
    _write_backup(backup_root, "2026-08-01", tar_from=hq_dir / STORE_REL, issues=["a"])
    assert hq_restore.resolve_level(hq_restore.list_backups({})[0], "auto") == "tar"


def test_auto_falls_back_to_jsonl_when_there_is_no_tar(backup_root):
    _write_backup(backup_root, "2026-08-01", issues=["a"])
    assert hq_restore.resolve_level(hq_restore.list_backups({})[0], "auto") == "jsonl"


def test_auto_skips_the_tar_when_the_engine_is_not_embedded(backup_root, hq_dir):
    """A tar is present, but a non-embedded engine does not read `.beads/embeddeddolt` — so
    that artifact is not usable and `auto` must take the JSONL floor (bh-kobw)."""
    _write_backup(backup_root, "2026-08-01", tar_from=hq_dir / STORE_REL, issues=["a"])
    backup = hq_restore.list_backups({})[0]

    assert hq_restore.resolve_level(backup, "auto", tar_usable=False) == "jsonl"


def test_auto_reports_nothing_restorable_when_only_an_unusable_tar_exists(backup_root, hq_dir):
    _write_backup(backup_root, "2026-08-01", tar_from=hq_dir / STORE_REL)  # tar only

    assert hq_restore.resolve_level(hq_restore.list_backups({})[0], "auto", tar_usable=False) == ""


def test_auto_prefers_the_native_artifact_when_that_is_what_the_backup_holds(backup_root):
    """`auto` picks the full-fidelity level regardless of WHICH artifact format it is — the
    native artifact is never gated by `tar_usable` (it restores over the connection, so the
    CURRENT engine's mode doesn't matter)."""
    _write_backup(backup_root, "2026-08-01", native_from="x", issues=["a"])
    backup = hq_restore.list_backups({})[0]

    assert hq_restore.resolve_level(backup, "auto", tar_usable=False) == "tar"


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


def test_explicit_tar_level_is_refused_when_the_engine_is_not_embedded(backup_root, hq_dir):
    """Extracting into `.beads/embeddeddolt` under a non-embedded engine writes a directory
    nothing reads — and the old code would have reported that as a successful restore. Say why
    instead, and leave the live store alone (bh-kobw). Current mode is now a FILESYSTEM FACT
    (`.beads/metadata.json`'s `dolt_mode`), never a `bd dolt status` probe (bh-areg.1)."""
    _write_backup(backup_root, "2026-08-01", tar_from=hq_dir / STORE_REL)
    (hq_dir / STORE_REL / "manifest").write_text("LIVE\n")
    _write_metadata(hq_dir, dolt_mode="server")

    out = hq_restore.restore(
        {}, hq_restore.list_backups({})[0], level="tar", dry_run=False, confirm=True
    )

    assert not out.ok
    assert any("embedded" in a for a in out.actions)
    assert (hq_dir / STORE_REL / "manifest").read_text() == "LIVE\n"  # untouched


def test_explicit_tar_level_is_refused_when_the_engine_mode_is_unknown(backup_root, hq_dir):
    """Unreadable/missing metadata must NEVER be read as "assume embedded" (bh-u562.1 finding
    9's bug, restored honestly this time) — an unknown engine is not a usable tar target."""
    _write_backup(backup_root, "2026-08-01", tar_from=hq_dir / STORE_REL)
    (hq_dir / ".beads" / "metadata.json").unlink()  # no metadata at all

    out = hq_restore.restore(
        {}, hq_restore.list_backups({})[0], level="tar", dry_run=False, confirm=True
    )

    assert not out.ok
    assert any("embedded" in a for a in out.actions)


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


# ---- native round trip: connection-oriented, non-embedded HQ (bh-areg.1) ------


def test_native_round_trip_restores_over_the_connection(backup_root, tmp_path, monkeypatch):
    """The OTHER headline of this bead: a server-mode backup restores into a server-mode HQ —
    a real round trip through the `Engine.backup_restore` seam, not just a green gate."""
    hq = tmp_path / "hq"
    _write_metadata(hq, dolt_mode="server")  # a non-embedded HQ — no embeddeddolt at all
    monkeypatch.setattr(hq_restore.config, "hq_dir", lambda: hq)
    _write_backup(backup_root, "2026-08-01", native_from="real dolt-native payload")

    restored: list[tuple[str, str]] = []

    class _NativeEngine:
        def backup_restore(self, cwd, source, *, actor=""):
            restored.append((str(cwd), str(source)))
            return subprocess.CompletedProcess(["bd", "backup", "restore"], 0, "", "")

    monkeypatch.setattr(hq_restore.engine, "get_engine", lambda cfg: _NativeEngine())

    out = hq_restore.restore({}, hq_restore.list_backups({})[0], dry_run=False, confirm=True)

    assert out.ok, out.actions
    assert restored == [(str(hq), str(backup_root / "2026-08-01" / hq_restore._native_dirname()))]
    assert out.moved_aside is None  # no move-aside step for the connection-oriented level


def test_native_restore_reports_a_bd_failure(backup_root, tmp_path, monkeypatch):
    hq = tmp_path / "hq"
    _write_metadata(hq, dolt_mode="server")
    monkeypatch.setattr(hq_restore.config, "hq_dir", lambda: hq)
    _write_backup(backup_root, "2026-08-01", native_from="x")

    class _FailingEngine:
        def backup_restore(self, cwd, source, *, actor=""):
            return subprocess.CompletedProcess(["bd", "backup", "restore"], 1, "", "boom")

    monkeypatch.setattr(hq_restore.engine, "get_engine", lambda cfg: _FailingEngine())

    out = hq_restore.restore({}, hq_restore.list_backups({})[0], dry_run=False, confirm=True)

    assert not out.ok
    assert any("bd backup restore failed" in a for a in out.actions)


def test_native_level_is_usable_even_though_the_current_engine_is_embedded(
    backup_root, hq_dir, monkeypatch
):
    """The connection-oriented artifact restores over the connection, so it is never gated by
    `tar_usable` the way the `.tar.gz` artifact is — this is what makes `auto` correct
    regardless of which mode originally produced the backup."""
    _write_backup(backup_root, "2026-08-01", native_from="x")  # no tar — a non-embedded backup

    restored = []

    class _NativeEngine:
        def backup_restore(self, cwd, source, *, actor=""):
            restored.append(str(source))
            return subprocess.CompletedProcess(["bd", "backup", "restore"], 0, "", "")

    monkeypatch.setattr(hq_restore.engine, "get_engine", lambda cfg: _NativeEngine())

    out = hq_restore.restore({}, hq_restore.list_backups({})[0], dry_run=False, confirm=True)

    assert out.ok, out.actions
    assert restored


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
            "E",
            (),
            {
                "import_jsonl": lambda self, cwd, args: (
                    imported.append(args) or subprocess.CompletedProcess(args, 0)
                )
            },
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
            "E",
            (),
            {
                "import_jsonl": lambda self, cwd, args: subprocess.CompletedProcess(
                    args, 1, stdout="", stderr="boom"
                )
            },
        )(),
    )

    out = hq_restore.restore({}, hq_restore.list_backups({})[0], dry_run=False, confirm=True)
    assert not out.ok


# ---- bh-5009a: the relocation must not strand a pre-existing pre-push backup ----


def test_artifact_names_match_the_writer_side():
    """`hq_restore` mirrors `hq`'s artifact-name constants rather than importing them (that
    module is heavy and this one is on the recovery path). This is what keeps the copies from
    drifting apart the next time one of them is renamed."""
    from beadhive import hq as hq_mod

    assert hq_restore._JSONL_NAME == hq_mod._JSONL_NAME
    assert hq_restore._TAR_NAME == hq_mod._TAR_NAME
    assert hq_restore._native_dirname() == hq_mod._DOLT_NATIVE_DIRNAME


def test_current_artifact_names_are_discovered(backup_root, hq_dir):
    d = backup_root / "2026-08-08T165333Z"
    d.mkdir(parents=True)
    (d / hq_restore._JSONL_NAME).write_text(json.dumps({"id": "hq-1"}) + "\n")

    sets = hq_restore.list_backups({})

    assert [s.label for s in sets] == ["2026-08-08T165333Z"]
    assert sets[0].jsonl is not None and sets[0].jsonl.name == "issues.jsonl"


def test_a_set_in_the_pre_relocation_location_is_still_discovered(
    backup_root, hq_dir, tmp_path, monkeypatch
):
    """A backup taken before bh-5009a is still the only pre-push copy of that HQ. `list_backups`
    spans both roots, sorted by set NAME so the two interleave chronologically rather than
    grouping by root."""
    from beadhive import backup as backup_mod

    legacy = tmp_path / "legacy-hq-backups"
    monkeypatch.setattr(backup_mod, "legacy_hq_root", lambda cfg=None: legacy)
    _write_backup(legacy, "2026-07-01", issues=["old"])  # legacy location AND legacy filenames
    d = backup_root / "2026-08-08T165333Z"
    d.mkdir(parents=True)
    (d / hq_restore._JSONL_NAME).write_text(json.dumps({"id": "hq-new"}) + "\n")

    sets = hq_restore.list_backups({})

    assert [s.label for s in sets] == ["2026-08-08T165333Z", "2026-07-01"]
    assert sets[1].jsonl is not None and sets[1].jsonl.name == "hq-issues.jsonl"
