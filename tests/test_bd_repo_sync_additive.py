"""Empirical de-risk gate —.

Claim: ``bd repo sync`` is ADDITIVE — a native bead in a primary bd DB SURVIVES a sync that
imports beads from a configured source rig.  The unified-store architecture (HQ as aggregation
primary that also holds canonical native beads) depends on this being true.

EMPIRICAL RESULT: PASS
  The native bead (prim-* prefix) survives ``bd repo sync``; the source bead (src-* prefix)
  is imported alongside it.  Sync is additive, not destructive — the write-guard in guard.py
  was mis-annotated as "wiped on sync"; the real hazard is that a hub-native bead is a
  *permanent orphan* with no source-rig home (see guard.py annotation update in this commit).

Test design:
  - NOT marked ``integration``: runs under ``just check`` (fast gate) when ``bd`` is on PATH.
  - Marked ``skip_if_no_bd``: self-skips on machines without the binary, so the suite stays
    green in CI environments that lack bd.
  - All I/O is in pytest's ``tmp_path`` — zero production writes.
"""

from __future__ import annotations

import json
import os

from beadhive.run import run
from harness.beads import skip_if_no_bd

# Self-skips when bd is not installed; NOT @pytest.mark.integration so the test runs under
# `just check` (marker "not integration") and provides an empirical result on every validate.
pytestmark = skip_if_no_bd

_BD_NI = {"BD_NON_INTERACTIVE": "1"}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bd_env() -> dict:
    """Subprocess env with BD_NON_INTERACTIVE set (suppresses prompts/hooks output)."""
    return {**os.environ, **_BD_NI}


def _bd_init(path, prefix: str):
    """``bd init`` via cwd (not -C): init IS what creates the .beads dir."""
    return run(
        ["bd", "init", "--prefix", prefix, "--skip-agents", "--skip-hooks", "--quiet"],
        cwd=str(path),
        check=False,
        capture=True,
        env=_bd_env(),
    )


def _bd(path, *args):
    """``bd -C <path> <args>`` — all other bd subcommands can use -C."""
    return run(
        ["bd", "-C", str(path), *[str(a) for a in args]],
        check=False,
        capture=True,
        env=_bd_env(),
    )


def _bd_json(path, *args):
    """Run ``bd -C <path> <args> --json`` and return the parsed JSON (or None on error)."""
    res = _bd(path, *args, "--json")
    if res.returncode != 0:
        return None
    try:
        return json.loads(res.stdout or "null")
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# The gate test
# ---------------------------------------------------------------------------


def test_bd_repo_sync_is_additive(tmp_path):
    """EMPIRICAL: native bead in the primary DB survives ``bd repo sync``.

    Setup
    -----
    source_rig  — bd init prefix=src; create one bead; export to issues.jsonl
    primary_db  — bd init prefix=prim; create one NATIVE bead

    Action
    ------
    ``bd repo add <source_rig>`` → ``bd repo sync``  (both run in primary_db)

    Assertions
    ----------
    (a) native bead (prim-*) still present in primary_db after sync   — SURVIVES
    (b) source bead (src-*) is present in primary_db after sync       — IMPORTED
    (c) source bead id retains its original prefix                    — PREFIX PRESERVED

    PASS → unified-store architecture is safe (HQ DB can aggregate AND hold native beads).
    FAIL → fallback to two-store model required; report to coordinator with observed ids.
    """
    source_rig = tmp_path / "source_rig"
    primary_db = tmp_path / "primary_db"
    source_rig.mkdir()
    primary_db.mkdir()

    # ------------------------------------------------------------------ source rig
    src_init = _bd_init(source_rig, prefix="src")
    assert src_init.returncode == 0, f"source bd init failed:\n{src_init.stderr}"

    src_q = _bd(source_rig, "q", "source bead")
    assert src_q.returncode == 0, f"source bd q failed:\n{src_q.stderr}"
    src_bead_id = (src_q.stdout or "").strip().splitlines()[-1].strip()
    assert src_bead_id.startswith("src-"), (
        f"source bead id has unexpected prefix: {src_bead_id!r}"
    )

    # Export source beads to issues.jsonl so bd repo sync can read them.
    jsonl_path = source_rig / ".beads" / "issues.jsonl"
    export = _bd(source_rig, "export", "-o", str(jsonl_path))
    assert export.returncode == 0, f"bd export failed:\n{export.stderr}"
    assert jsonl_path.exists(), "bd export did not create issues.jsonl"

    # ------------------------------------------------------------------ primary db
    prim_init = _bd_init(primary_db, prefix="prim")
    assert prim_init.returncode == 0, f"primary bd init failed:\n{prim_init.stderr}"

    prim_q = _bd(primary_db, "q", "native bead")
    assert prim_q.returncode == 0, f"primary bd q failed:\n{prim_q.stderr}"
    native_bead_id = (prim_q.stdout or "").strip().splitlines()[-1].strip()
    assert native_bead_id.startswith("prim-"), (
        f"native bead id has unexpected prefix: {native_bead_id!r}"
    )

    # ------------------------------------------------------------------ aggregate
    add = _bd(primary_db, "repo", "add", str(source_rig))
    assert add.returncode == 0, f"bd repo add failed:\n{add.stderr}"

    sync = _bd(primary_db, "repo", "sync")
    assert sync.returncode == 0, f"bd repo sync failed:\n{sync.stderr}"

    # ------------------------------------------------------------------ assertions
    all_beads = _bd_json(primary_db, "list", "--all") or []
    all_ids = {b["id"] for b in all_beads}

    # (a) native bead SURVIVES — the unified-store architecture is safe
    assert native_bead_id in all_ids, (
        f"FAIL (gate): native bead {native_bead_id!r} was WIPED by bd repo sync — "
        f"unified-store architecture is NOT safe; fall back to two-store model.\n"
        f"All beads after sync: {sorted(all_ids)}"
    )

    # (b) source bead is IMPORTED into the primary DB
    assert src_bead_id in all_ids, (
        f"FAIL (gate): source bead {src_bead_id!r} was NOT imported by bd repo sync.\n"
        f"All beads after sync: {sorted(all_ids)}"
    )

    # (c) imported bead retains its original prefix (not remapped to prim-*)
    imported = next((b for b in all_beads if b["id"] == src_bead_id), None)
    assert imported is not None
    assert imported["id"].startswith("src-"), (
        f"FAIL: imported bead id {imported['id']!r} does not start with 'src-' — "
        f"prefix was remapped during import"
    )
