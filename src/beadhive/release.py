"""`bh release` — the release plane's read-only views over the advisory merge order (bh-k2j8).

Today one verb: `bh release order`, which renders the strategy-preferred merge sequence the merger
consults instead of FCFS. It is strictly advisory and read-only — it reads the gated-ready set
(`bd ready --gated`, the beads whose review gate cleared) and orders it through the same scorer that
sorts `bh work ready --gated` (`release_order`), so the two never disagree about the sequence. The
hard counterpart — the `release-hold:` gate that blocks a `release:breaking` bead until a releaser
clears it — lives in plan/guard/work, not here.
"""

from __future__ import annotations

import typer

from . import bd, config, registry
from . import release_order as ro

app = typer.Typer(no_args_is_help=True, help="Release plane: advisory merge-order views.")

_HIVE = typer.Option("", "--hive", help="target hive (default: cwd's hive)")


def _impact_tag(bead: dict) -> str:
    """A compact `release:/wave:` tag for a bead's order line ('unclassified' when unlabeled)."""
    impact = ro.release_impact(bead)
    if not impact:
        return "unclassified"
    wave = ro.wave_name(bead)
    return f"{impact}" + (f" (wave:{wave})" if wave else "")


@app.command("order")
def order(hive: str = _HIVE):
    """Show the strategy-preferred merge sequence over the gated-ready set — read-only, advisory.

    Consults the same scorer that sorts `bh work ready --gated` (`release.strategy` /
    `release.fix_churn_budget`), so the merger sees the order it would merge in. Unclassified ready
    beads (no `release:` label) list after the ordered ones. Empty when nothing is gated-ready."""
    cfg = config.load()
    cwd = registry.hive_dir_for(cfg, hive)
    entry = registry.entry_for_dir(cfg, cwd)
    strategy = config.release_strategy(cfg, entry)
    budget = config.release_fix_churn_budget(cfg, entry)

    beads = bd.json(["ready", "--gated", "--limit", "0"], cwd) or []
    by_id = {str(b.get("id") or ""): b for b in beads}
    sequence = ro.merge_sequence(beads, strategy=strategy, fix_churn_budget=budget)

    typer.echo(f"release order — strategy: {strategy}, fix_churn_budget: {budget}")
    if not sequence:
        typer.echo("  (nothing gated-ready to order)")
        return
    for n, bead_id in enumerate(sequence, 1):
        typer.echo(f"  {n}. {bead_id}  [{_impact_tag(by_id.get(bead_id, {}))}]")
