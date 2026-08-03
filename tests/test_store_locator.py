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
