#!/usr/bin/env python3
"""List, and with ``--apply`` clear, the ``delivery`` value a rejected report should not carry.

Before bh-5kpwc a ``duplicate-of`` triage outcome linked the duplicate to its canonical report's
work through the ordinary link, which wrote ``delivery:pending`` for it (so the label projection
published ``bh:delivery/pending`` and a milestone could draft for the closed duplicate). A
duplicate's delivery is the canonical report's. This sweeps ONE hive's bound reports for the ones
fielded by ``reject`` (a ``duplicate-of`` ends in one) that still carry a ``delivery`` value.

Without ``--apply`` it only lists them (the dry run). With ``--apply`` it clears each value from
the bead's label cache (``bd set-state`` has no unset, so this is a raw label removal) and writes
nothing else: no GitHub write and no outbox record. Run ``bh work labels sync --include-closed``
afterwards to draft the removal of the now-stale ``bh:delivery/*`` label on each issue; an
operator still reviews and approves that batch.

Usage::

    uv run python scripts/repair_duplicate_delivery.py --repo /path/to/hive
    uv run python scripts/repair_duplicate_delivery.py --repo /path/to/hive --apply
    uv run python scripts/repair_duplicate_delivery.py --repo /path/to/hive --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from beadhive import identity, intake_services


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("--repo", default=".", help="the hive checkout to sweep (default: .)")
    parser.add_argument("--apply", action="store_true", help="clear each value (default: list)")
    parser.add_argument("--as", dest="actor", default="", help="the actor recorded on each clear")
    parser.add_argument("--json", dest="as_json", action="store_true", help="emit one document")
    args = parser.parse_args(argv)

    sweep = intake_services.sweep_delivery(
        Path(args.repo),
        repair=args.apply,
        actor=identity.resolve_actor(args.actor) if args.apply else "",
        cli_only=True,
    )
    if args.as_json:
        payload = {
            "applied": args.apply,
            "findings": [{"report": f.report, "delivery": f.delivery} for f in sweep.findings],
            "cleared": [f.report for f in sweep.cleared],
            "failed": [{"report": f.report, "error": why} for f, why in sweep.failed],
        }
        print(json.dumps(payload, sort_keys=True))
    else:
        if sweep.clean:
            print("no rejected report carries a delivery value")
        for finding in sweep.findings:
            print(f"  {finding.report}\tdelivery:{finding.delivery}")
        for finding in sweep.cleared:
            print(f"✓ cleared delivery:{finding.delivery} from {finding.report}")
        for finding, why in sweep.failed:
            print(f"✗ {finding.report}: {why}", file=sys.stderr)
        if sweep.findings and not args.apply:
            print(f"dry run: {len(sweep.findings)} report(s); re-run with --apply to clear them")
        if sweep.cleared:
            print("next: `bh work labels sync --include-closed` drafts the label removals")
    return 1 if sweep.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
