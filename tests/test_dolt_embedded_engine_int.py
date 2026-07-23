"""Integration: safety._scan_dolt_ref / sync_remote / doctor fleet health against a REAL
embedded-Dolt bd repo (bh-fl26) — not a mock of the ``Engine`` protocol.

bd's embedded (in-process, no server) engine — the default/most common backend — stores its
database entirely outside the wrapping git repo (``.beads/embeddeddolt``), so
``refs/dolt/data`` never exists for it. Before this bead, ``safety._scan_dolt_ref`` only
checked that outer-repo git ref, so every embedded-engine hive silently reported Dolt state
``"absent"`` (never "unpushed") regardless of real local commits — ``bh doctor``'s "unpushed
dolt state" count and ``bh hive sync-remote --all``'s classification never caught it
(confirmed empirically on beadhive/beadhive itself, see the bead).

This test builds a REAL embedded-bd hive with a ``file://`` Dolt remote (the same
``harness.hive.make_hive`` + ``harness.beads`` seam other AGF integration tests use — a real
``bd`` process, real Dolt sql-server, real commits), proves the detection gap is closed
*before* any push, then proves the fix's actual point empirically: a live (non-dry-run)
``sync_remote`` push makes the state genuinely leave the machine — a SECOND real
embedded-bd clone can ``bd bootstrap`` + see the pushed bead.

Note on "clean once pushed": bd exposes no read-only "peek the remote commit" primitive (no
``bd dolt fetch``) for this engine, so ``safety.scan`` cannot *predict* ahead/behind for it
without mutating (see the bead's design notes) — a post-push ``safety.scan`` on the same
clone still honestly reports ``"unknown"`` (can't verify without another push attempt), not a
fabricated ``"clean"``. This test instead proves the stronger, more meaningful claim: the
data really did leave the machine, by pulling it into an independent real clone.

Marked ``integration`` (slower — spins up real Dolt sql-servers) + self-skips without a
``bd`` binary on PATH, per this repo's marker convention (``justfile``: ``just test``
excludes "integration"; ``just render-int`` / ``uv run pytest -m integration`` run it).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beadhive import doctor, metadata, safety, sync_remote
from beadhive.run import run
from harness import beads
from harness.beads import skip_if_no_bd
from harness.hive import make_hive
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_BOOTSTRAP_TIMEOUT = 180


def _bootstrap_second_clone(hive) -> Path:
    """A second, independent real embedded-bd clone wired at the SAME shared remotes,
    mirroring ``RemoteSandboxModality.develop``'s sandbox-clone recipe: fresh git clone (no
    ``.beads``, which is gitignored) + ``sync.remote`` config + ``bd bootstrap``."""
    sb = hive.world.sandboxes / "second-clone"
    git("clone", "-q", "-b", "main", str(hive.git_remote), str(sb))
    (sb / ".beads").mkdir(parents=True, exist_ok=True)
    (sb / ".beads" / "config.yaml").write_text(f'sync.remote: "file://{hive.dolt_remote}"\n')
    run(
        ["bd", "bootstrap", "--yes"],
        cwd=str(sb),
        check=True,
        capture=True,
        timeout=_BOOTSTRAP_TIMEOUT,
    )
    return Path(sb)


def test_embedded_dolt_engine_detected_and_pushed_for_real(world):
    """End-to-end regression proof for bh-fl26 against a real embedded-engine bd repo."""
    hive_id = None

    # ------------------------------------------------------------------ hive A (embedded bd)
    # make_hive(with_remotes=True) already wires a file:// dolt remote AND pushes the fresh
    # (empty) bead state once — so the very next local commit is genuinely unpushed.
    hive = make_hive(world, with_remotes=True)
    hive_id = f"github/{hive.org}/{hive.repo}"

    bead_id = beads.create(hive.main, "unpushed dolt state")
    assert bead_id.startswith(hive.prefix + "-"), bead_id

    # bd's own first-write scaffolding (.claude/, .agents/, an appended .gitignore entry) is
    # git-tracked housekeeping unrelated to what this test is isolating — commit AND push it
    # so the git branch is clean+pushed again, leaving Dolt state as the ONLY unpushed signal.
    git("-C", str(hive.main), "add", "-A")
    git("-C", str(hive.main), "commit", "-qm", "chore: bd scaffolding")
    git("-C", str(hive.main), "push", "-q", "origin", "main")

    # --- 1. BEFORE any push: must NOT silently read "absent"/"clean" (the bug) -------------
    record = safety.scan(hive.main)
    assert record.dolt_ref.status == "unknown", (
        "embedded engine + configured Dolt remote must report 'unknown' (needs an attempt), "
        f"got {record.dolt_ref.status!r} — was it mistaken for 'absent'/'clean' again?"
    )

    sync_record = sync_remote.assess_hive(hive_id, hive.main)
    assert sync_record.status == sync_remote.SyncStatus.UNPUSHED_DOLT
    assert sync_record.dolt_status == "unknown"

    # `bh doctor`'s fleet-wide "unpushed dolt state" count also catches it (real path: a
    # real metadata.measure() scan, not a synthetic RepoMetadata literal).
    rec = metadata.measure(hive.main)
    health = doctor._data_fleet_health({hive_id: rec}, {hive_id})
    assert health["dolt_unpushed"] == 1

    # --- 2. Live sync_remote actually pushes it (real Engine.push_state -> bd dolt push) ---
    plan = sync_remote.sync_remote(dry_run=False)
    assert plan.offending == []
    assert hive_id in plan.dolt_pushed

    # --- 3. Cross-host proof: the state genuinely left the machine -------------------------
    # A second, independent real embedded-bd clone bootstraps from the SAME shared file://
    # dolt remote and must see the bead pushed in step 2 — this is the strongest available
    # proof (bd has no read-only remote-diff primitive to check this any other way).
    second_clone = _bootstrap_second_clone(hive)
    pulled = beads.bd_json("show", bead_id, cwd=second_clone)
    if isinstance(pulled, list):
        pulled = pulled[0] if pulled else None
    assert pulled is not None and pulled.get("id") == bead_id, (
        f"pushed bead {bead_id!r} did not propagate to a second real clone via the shared "
        "Dolt remote — the fix's live push did not actually make the data durable"
    )
