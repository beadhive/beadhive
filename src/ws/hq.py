"""Factory HQ — the one durable central store.

HQ is the aggregation primary (the cross-rig view that supersedes the disposable ``~/.ws/hub``)
that ALSO holds canonical hq-prefixed control-plane beads. A SINGLETON (kind=hq), registered
ONLY in the ws registry under the RESERVED SYNTHETIC IDENTITY ``local/factory/hq`` — LOCAL infra
like the hub/cache (no remote, never a git-workspace provider). It lives at ``config.hq_dir()``.

`ws hq init` stands it up: bd-init the store (prefix ``hq``), register the synthetic identity,
then move the aggregation role onto it (``bd repo add`` every registered rig + sync). The old
``~/.ws/hub`` is subsumed — rebuildable, no data migration (re-add + sync at the new location).
This module intentionally exposes only ``init``; the full ``ws hq`` operator surface is deferred.
"""

from __future__ import annotations

import typer

from . import config, hub, registry


def init():
    """Stand up the Factory HQ store and move aggregation onto it — the kind=hq singleton.

    Enforces the singleton (refuses a second HQ), then reuses ``hub.ensure_store`` to bd-init a
    durable git+bd store at ``config.hq_dir()`` with prefix ``hq``, registers the reserved
    synthetic identity, and reuses ``hub.sync`` to ``bd repo add`` every registered rig + sync
    (the aggregation role moves off the disposable hub to HQ)."""
    cfg = config.load()
    existing = registry.rig_of_kind(cfg, registry.HQ_KIND)
    if existing is not None:
        triplet = f"{existing['provider']}/{existing['org']}/{existing['repo']}"
        typer.echo(
            f"✗ HQ already exists (kind=hq is a singleton): {triplet} → {config.hq_dir()}.\n"
            "  Refusing to stand up a second HQ. Rebuild in place with `ws sync`.",
            err=True,
        )
        raise typer.Exit(1)

    # Create the durable store FIRST (prefix hq) — so a bd-init failure never leaves a dangling
    # registration — then register the synthetic identity in the ws registry.
    hq = hub.ensure_store(config.hq_dir(), registry.HQ_PREFIX)
    registry.register(
        registry.HQ_PROVIDER, registry.HQ_ORG, registry.HQ_REPO,
        registry.HQ_PREFIX, registry.HQ_KIND,
    )
    typer.echo(f"✓ Factory HQ store initialized at {hq} (prefix '{registry.HQ_PREFIX}', kind=hq)")

    # Aggregation moves onto HQ: hub.sync now resolves the target to HQ (it is registered), so
    # this bd repo add's every registered rig into HQ and syncs. Reuse over a parallel mechanism.
    failed = hub.sync()
    if failed:
        raise typer.Exit(1)
