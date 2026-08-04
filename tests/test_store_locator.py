"""store_locator.py — the ONE place a hive's embedded-store path and dolt mode are derived
(bh-areg.1), as FILESYSTEM FACTS never a `bd dolt status` mode probe.
"""

from __future__ import annotations

import json

from beadhive import store_locator

# ---- embedded_store_dir / has_embedded_store -----------------------------------


def test_embedded_store_dir_is_a_pure_path_join(tmp_path):
    assert store_locator.embedded_store_dir(tmp_path) == tmp_path / ".beads" / "embeddeddolt"


def test_has_embedded_store_true_when_the_directory_exists(tmp_path):
    (tmp_path / ".beads" / "embeddeddolt").mkdir(parents=True)
    assert store_locator.has_embedded_store(tmp_path) is True


def test_has_embedded_store_false_when_absent(tmp_path):
    assert store_locator.has_embedded_store(tmp_path) is False


def test_has_embedded_store_false_when_beads_exists_but_embeddeddolt_does_not(tmp_path):
    """A server-mode hive (owned/shared/external) has `.beads/` but no `embeddeddolt/`
    (bh-u562.1 finding 8) — the fact must be about the SPECIFIC directory, not `.beads/`."""
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads" / "dolt-server.port").write_text("3308\n")
    assert store_locator.has_embedded_store(tmp_path) is False


# ---- embedded_database_dir / dolt_database ---------------------------------------
#
# bh-z9h7: these two facts were briefly BOTH called `embedded_store_dir`, in two modules,
# returning paths one directory apart. The distinction is what these tests are for — asserting
# only "returns a Path" is exactly what failed to catch it.


def test_embedded_database_dir_is_the_parents_child_named_for_the_database(tmp_path):
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads" / "metadata.json").write_text(json.dumps({"dolt_database": "beads"}))

    parent = store_locator.embedded_store_dir(tmp_path)
    db_dir = store_locator.embedded_database_dir(tmp_path)

    assert db_dir == parent / "beads"
    assert db_dir.name == "beads"  # the per-database path ENDS in the database name...
    assert parent.name == "embeddeddolt"  # ...and the bare parent does not
    assert db_dir.parent == parent


def test_embedded_database_dir_reads_the_database_from_metadata_not_the_directory(tmp_path):
    """Two candidate database dirs, mirroring the real repo's own embeddeddolt/ (beads + bh) —
    a naive "glob the one subdirectory" would be ambiguous here. metadata.json is bd's own
    record of which one it opens."""
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads" / "metadata.json").write_text(json.dumps({"dolt_database": "beads"}))
    (tmp_path / ".beads" / "embeddeddolt" / "beads").mkdir(parents=True)
    (tmp_path / ".beads" / "embeddeddolt" / "bh").mkdir(parents=True)

    assert store_locator.embedded_database_dir(tmp_path) == (
        tmp_path / ".beads" / "embeddeddolt" / "beads"
    )


def test_embedded_database_dir_explicit_database_wins(tmp_path):
    assert store_locator.embedded_database_dir(tmp_path, database="fallback") == (
        tmp_path / ".beads" / "embeddeddolt" / "fallback"
    )


def test_dolt_database_falls_back_to_the_given_name_then_the_hive_dir_name(tmp_path):
    hive = tmp_path / "myhive"
    hive.mkdir()
    assert store_locator.dolt_database(hive, "prefix") == "prefix"
    assert store_locator.dolt_database(hive) == "myhive"


# ---- dolt_mode / is_embedded_mode ------------------------------------------------


def _write_metadata(hive, **fields):
    (hive / ".beads").mkdir(parents=True, exist_ok=True)
    (hive / ".beads" / "metadata.json").write_text(json.dumps(fields))


def test_dolt_mode_reads_the_persisted_value(tmp_path):
    _write_metadata(tmp_path, dolt_mode="embedded")
    assert store_locator.dolt_mode(tmp_path) == "embedded"


def test_dolt_mode_none_when_metadata_is_missing(tmp_path):
    assert store_locator.dolt_mode(tmp_path) is None


def test_dolt_mode_none_when_beads_directory_is_missing_entirely(tmp_path):
    assert store_locator.dolt_mode(tmp_path) is None


def test_dolt_mode_none_when_metadata_is_malformed(tmp_path):
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads" / "metadata.json").write_text("not json {")
    assert store_locator.dolt_mode(tmp_path) is None


def test_dolt_mode_none_when_the_key_is_absent(tmp_path):
    _write_metadata(tmp_path, backend="dolt")  # no dolt_mode key at all
    assert store_locator.dolt_mode(tmp_path) is None


def test_is_embedded_mode_true_only_for_the_literal_embedded_value(tmp_path):
    _write_metadata(tmp_path, dolt_mode="embedded")
    assert store_locator.is_embedded_mode(tmp_path) is True


def test_is_embedded_mode_false_for_server_mode(tmp_path):
    """Measured against a real bd binary: owned, shared, AND external modes all persist
    `dolt_mode: "server"` — bd does not distinguish them at this layer, which is fine since
    this only ever needs "embedded, or not"."""
    _write_metadata(tmp_path, dolt_mode="server")
    assert store_locator.is_embedded_mode(tmp_path) is False


def test_is_embedded_mode_false_when_unknown_never_assumes_embedded(tmp_path):
    """The binding constraint this whole module exists to satisfy: unknown must never read as
    embedded (bh-u562.1 finding 9's `_bd_dolt_mode() is None` bug, not repeated here)."""
    assert store_locator.is_embedded_mode(tmp_path) is False


# ---- ensure_server_mode_persisted (bh-areg.7) -------------------------------------


def test_ensure_server_mode_persisted_no_op_when_already_server(tmp_path):
    """The measured common case: a fresh `bd init --shared-server`/`bd bootstrap` already
    persisted `dolt_mode: "server"` on its own — nothing to write, nothing to report."""
    _write_metadata(tmp_path, dolt_mode="server", dolt_database="widget")
    before = (tmp_path / ".beads" / "metadata.json").read_text()

    changed = store_locator.ensure_server_mode_persisted(tmp_path)

    assert changed is False
    assert (tmp_path / ".beads" / "metadata.json").read_text() == before  # byte-for-byte


def test_ensure_server_mode_persisted_writes_when_missing(tmp_path):
    """The defensive path: dolt_mode absent entirely — write it, preserving other keys."""
    _write_metadata(tmp_path, dolt_database="widget")

    changed = store_locator.ensure_server_mode_persisted(tmp_path)

    assert changed is True
    data = json.loads((tmp_path / ".beads" / "metadata.json").read_text())
    assert data["dolt_mode"] == "server"
    assert data["dolt_database"] == "widget"  # other keys untouched


def test_ensure_server_mode_persisted_writes_when_stale_embedded(tmp_path):
    """The exact drift bh-areg.4 measured for `--reinit-local` (not expected for a fresh
    init/bootstrap, but never trusted silently): dolt_mode stuck at "embedded"."""
    _write_metadata(tmp_path, dolt_mode="embedded")

    changed = store_locator.ensure_server_mode_persisted(tmp_path)

    assert changed is True
    assert store_locator.dolt_mode(tmp_path) == "server"


def test_ensure_server_mode_persisted_never_manufactures_a_beads_dir(tmp_path):
    """No `.beads/` at all (bd init never actually ran, e.g. a fully-mocked caller) — this
    must never create one; there is no store here to persist a mode for."""
    changed = store_locator.ensure_server_mode_persisted(tmp_path)

    assert changed is False
    assert not (tmp_path / ".beads").exists()


def test_ensure_server_mode_persisted_survives_malformed_metadata(tmp_path):
    (tmp_path / ".beads").mkdir()
    (tmp_path / ".beads" / "metadata.json").write_text("not json {")

    changed = store_locator.ensure_server_mode_persisted(tmp_path)

    assert changed is True
    assert json.loads((tmp_path / ".beads" / "metadata.json").read_text())["dolt_mode"] == "server"
