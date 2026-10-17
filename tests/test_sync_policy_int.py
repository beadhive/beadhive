"""Integration: bulk hive sync honors a pull-only contribution policy against REAL bd + Dolt
(bh-0azjq).

A real embedded-bd hive is wired in the contrib dual-remote shape: Dolt remote ``origin`` is the
writable fork's state store and Dolt remote ``upstream`` is the canonical upstream's. An
independent "maintainer" clone publishes a bead to the upstream store; the hive itself holds an
unpushed local bead. A forced bulk sync must then pull the maintainer's bead from ``upstream``
while writing NOTHING to either Dolt remote or to the fork's Git refs.

Marked ``integration`` (real Dolt) + self-skips without a ``bd`` binary on PATH.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from beadhive import config, sync_remote
from beadhive.run import run
from harness import beads
from harness.beads import skip_if_no_bd
from harness.hive import make_hive
from harness.world import git

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_TIMEOUT = 180


def _tree_digest(root: Path) -> str:
    """Content digest of every file under a filesystem Dolt remote — any write changes it."""
    h = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file()):
        h.update(str(path.relative_to(root)).encode())
        h.update(path.read_bytes())
    return h.hexdigest()


def _maintainer_publishes(hive, upstream_dolt: Path) -> str:
    """An independent clone bootstraps from the upstream store, files a bead, and pushes it."""
    sb = hive.world.sandboxes / "maintainer"
    git("clone", "-q", "-b", "main", str(hive.git_remote), str(sb))
    (sb / ".beads").mkdir(parents=True, exist_ok=True)
    (sb / ".beads" / "config.yaml").write_text(f'sync.remote: "file://{upstream_dolt}"\n')
    run(["bd", "bootstrap", "--yes"], cwd=str(sb), check=True, capture=True, timeout=_TIMEOUT)
    remotes = beads.bd_json("dolt", "remote", "list", cwd=sb) or []
    if not any(r.get("name") == "origin" for r in remotes):
        beads.add_file_remote(sb, upstream_dolt)
    bead_id = beads.create(sb, "upstream maintainer bead")
    beads.push(sb)
    return bead_id


def test_forced_bulk_sync_pulls_upstream_and_writes_no_remote(world):
    hive = make_hive(world, with_remotes=True)
    hive_id = f"github/{hive.org}/{hive.repo}"
    upstream_dolt = world.remotes / "gastownhall" / "beads"
    beads.add_file_remote(hive.main, upstream_dolt, name="upstream")
    beads.push(hive.main, "upstream")

    upstream_bead = _maintainer_publishes(hive, upstream_dolt)
    local_bead = beads.create(hive.main, "local planning bead — must never leak upstream")
    git("-C", str(hive.main), "add", "-A")
    git("-C", str(hive.main), "commit", "-qm", "chore: bd scaffolding")
    git("-C", str(hive.main), "push", "-q", "origin", "main")

    cfg = config.load()
    entry = next(e for e in cfg["managed_repos"] if e.get("repo") == hive.repo)
    entry.update(kind="external", contribution="pull", upstream="gastownhall/beads")
    config.save(cfg)

    before = (
        _tree_digest(upstream_dolt),
        _tree_digest(hive.dolt_remote),
        git("-C", str(hive.git_remote), "for-each-ref").stdout,
    )

    plan = sync_remote.sync_remote(dry_run=False, pull=True, push=True, force=True)
    forced = sync_remote.sync_remote(
        dry_run=False, pull=False, push=True, remote="upstream", force=True
    )

    assert plan.offending == [] and forced.offending == []
    assert plan.pull_sources == {hive_id: "upstream"}
    assert plan.dolt_pushed == [] and forced.dolt_pushed == []
    assert plan.pushed_branches == {} and forced.pushed_branches == {}
    after = (
        _tree_digest(upstream_dolt),
        _tree_digest(hive.dolt_remote),
        git("-C", str(hive.git_remote), "for-each-ref").stdout,
    )
    assert after == before, "a pull-only bulk sync wrote to a remote"
    pulled = beads.bd_json("show", upstream_bead, cwd=hive.main)
    pulled = pulled[0] if isinstance(pulled, list) and pulled else pulled
    assert pulled and pulled.get("id") == upstream_bead
    assert beads.bd_json("show", local_bead, cwd=hive.main)
