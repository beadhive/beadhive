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

from beadhive import dolt_health, store_locator
from beadhive.run import run
from harness.beads import embedded_env

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
        env=embedded_env(),
    )
    assert init.returncode == 0, init.stderr

    db_dir = store_locator.embedded_database_dir(tmp_path, database=prefix)
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
        env=embedded_env(),
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
        env=embedded_env(),
    )
    control = dolt_health.probe_embedded_schema_version(
        store_locator.embedded_database_dir(control_dir, database=prefix)
    )
    assert control.version == local.version


# ---- probe_embedded_lineage against a REAL dolt binary (bh-s9cdk) ---------------------------


def _dolt(args, cwd):
    res = run(["dolt", *args], check=False, capture=True, cwd=str(cwd), timeout=60)
    assert res.returncode == 0, f"dolt {args}: {res.stderr}"
    return res


def test_real_dolt_merge_base_reports_no_common_ancestor_for_two_unrelated_histories(tmp_path):
    """The exact repro bh-s9cdk measured: two independently `dolt init`'d stores, each pushed
    to the same remote (the second with `-f`, simulating two hosts each publishing their own
    rebuilt lineage), share no common ancestor — a real `dolt merge-base` proves it, and
    `probe_embedded_lineage` must classify it as split-brain, never as an ordinary diverged/
    behind state."""
    remote = tmp_path / "remote"
    remote_url = f"file://{remote}"

    host1 = tmp_path / "host1"
    host1.mkdir()
    _dolt(["init", "--name", "h1", "--email", "h1@test.com"], host1)
    _dolt(["sql", "-q", "create table t (id int primary key)"], host1)
    _dolt(["add", "-A"], host1)
    _dolt(["commit", "-m", "host1 init"], host1)
    _dolt(["remote", "add", "origin", remote_url], host1)
    _dolt(["push", "origin", "main"], host1)

    host2 = tmp_path / "host2"
    _dolt(["clone", remote_url, str(host2)], tmp_path)

    # A SECOND, unrelated lineage force-pushed over the same remote — the split-brain trigger.
    host1_fresh = tmp_path / "host1fresh"
    host1_fresh.mkdir()
    _dolt(["init", "--name", "h1", "--email", "h1@test.com"], host1_fresh)
    _dolt(["sql", "-q", "create table t (id int primary key)"], host1_fresh)
    _dolt(["add", "-A"], host1_fresh)
    _dolt(["commit", "-m", "unrelated fresh history"], host1_fresh)
    _dolt(["remote", "add", "origin", remote_url], host1_fresh)
    _dolt(["push", "-f", "origin", "main"], host1_fresh)

    # host2 fetches the now-unrelated remote main into its own remote-tracking branch.
    _dolt(["fetch", "origin"], host2)

    result = dolt_health.probe_embedded_lineage(host2)

    assert result.status == dolt_health.LINEAGE_SPLIT_BRAIN, result.detail
    assert "no common ancestor" in result.detail.lower()


def test_real_dolt_merge_base_reports_a_common_ancestor_for_a_normal_fast_forward(tmp_path):
    """Sanity control: two clones of the SAME lineage, one ahead by a real commit, must NOT be
    classified split-brain — a real common ancestor exists and `dolt merge-base` finds it."""
    remote = tmp_path / "remote"
    remote_url = f"file://{remote}"

    host1 = tmp_path / "host1"
    host1.mkdir()
    _dolt(["init", "--name", "h1", "--email", "h1@test.com"], host1)
    _dolt(["sql", "-q", "create table t (id int primary key)"], host1)
    _dolt(["add", "-A"], host1)
    _dolt(["commit", "-m", "init"], host1)
    _dolt(["remote", "add", "origin", remote_url], host1)
    _dolt(["push", "origin", "main"], host1)

    host2 = tmp_path / "host2"
    _dolt(["clone", remote_url, str(host2)], tmp_path)

    _dolt(["sql", "-q", "insert into t values (1)"], host1)
    _dolt(["add", "-A"], host1)
    _dolt(["commit", "-m", "host1 change"], host1)
    _dolt(["push", "origin", "main"], host1)

    _dolt(["fetch", "origin"], host2)

    result = dolt_health.probe_embedded_lineage(host2)

    assert result.status == dolt_health.LINEAGE_COMMON_ANCESTOR, result.detail
