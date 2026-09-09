#!/usr/bin/env python3
"""Render the deterministic public transport projection inventory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from beadhive.transport_inventory import document

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "design" / "transport-projection-inventory-v1.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail when the artifact would change")
    args = parser.parse_args(argv)
    rendered = json.dumps(document(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not TARGET.exists() or TARGET.read_text() != rendered:
            parser.error(f"{TARGET.relative_to(ROOT)} is stale; render it without --check")
        return 0
    TARGET.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
