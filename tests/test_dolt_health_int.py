"""Integration: `dolt_health.probe_embedded_schema_version` against a REAL bd-initialized Dolt
store — the empirical regression proof for the trap this bead's design work uncovered.

`bd dolt status --json` reports `"schema_version": 1` for EVERY store this probe would ever
see (bd's own `JSONSchemaVersion` CLI-envelope constant — `cmd/bd/output.go`), regardless of how
many real migrations that store is actually at. A probe built on that field would always compare
1 against 1 and always pass — a preflight that green-lights a load which then fails at open
time, exactly the regression this bead's acceptance criteria (AC4) forbid. This test proves the
probe this bead ships does NOT fall into that trap: it reads the real `schema_migrations` table
via a direct `dolt sql`, and that number is provably NOT 1 (a fresh bd store already carries
dozens of migrations) and provably NOT the same as `bd dolt status`'s own decoy field.

Marked `integration` (spins up a real `bd init`) + self-skips without `bd`/`dolt` on PATH, per
this repo's marker convention (`justfile`: `just test` excludes "integration").
"""

from __future__ import annotations

import json
import shutil
import uuid

import pytest

from beadhive import dolt_health
from beadhive.run import run

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(shutil.which("bd") is None, reason="bd not installed"),
    pytest.mark.skipif(shutil.which("dolt") is None, reason="dolt not installed"),
]


def test_real_bd_store_schema_version_is_not_the_json_envelope_decoy(tmp_path):
    prefix = f"schemaprobeint{uuid.uuid4().hex[:8]}"
    init = run(
        ["bd", "init", "--prefix", prefix, "--non-interactive"],
        check=True,
        capture=True,
        cwd=str(tmp_path),
        timeout=60,
    )
    assert init.returncode == 0, init.stderr

    db_dir = dolt_health.embedded_store_dir(tmp_path, database=prefix)
    probed = dolt_health.probe_embedded_schema_version(db_dir)

    assert probed.version is not None, probed.detail
    # The decoy this bead's own trap section names: bd dolt status --json's own
    # "schema_version" field is bd's CLI JSON envelope version, hardcoded 1, unrelated to real
    # migration count. A fresh bd store already carries far more than one migration.
    assert probed.version > 1

    status = run(
        ["bd", "-C", str(tmp_path), "dolt", "status", "--json"],
        check=True,
        capture=True,
        timeout=30,
    )
    decoy = json.loads(status.stdout)["schema_version"]
    assert decoy == 1
    assert probed.version != decoy


def test_local_bd_schema_version_matches_a_fresh_store_directly(tmp_path, monkeypatch):
    """`local_bd_schema_version`'s own scratch probe (used when the cache is cold) should agree
    with a separately, independently bd-init'd store's real version — both are "a fresh store
    under THIS bd binary", so they must land on the same LatestVersion()."""
    monkeypatch.setattr(dolt_health.config, "cache_dir", lambda: tmp_path / "cache")

    local = dolt_health.local_bd_schema_version()
    assert local.version is not None, local.detail

    prefix = f"schemaprobeint{uuid.uuid4().hex[:8]}"
    control_dir = tmp_path / "control"
    control_dir.mkdir()
    run(
        ["bd", "init", "--prefix", prefix, "--non-interactive"],
        check=True,
        capture=True,
        cwd=str(control_dir),
        timeout=60,
    )
    control = dolt_health.probe_embedded_schema_version(
        dolt_health.embedded_store_dir(control_dir, database=prefix)
    )
    assert control.version == local.version
