#!/usr/bin/env python3
"""Render the canonical Python operation declaration as language-neutral JSON."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from beadhive.operation_catalog import document

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "schemas" / "wire" / "v1.5.0" / "operation-catalog-v1.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args(argv)
    rendered = json.dumps(document(), indent=2, sort_keys=True) + "\n"
    if args.check:
        if not TARGET.is_file() or TARGET.read_text(encoding="utf-8") != rendered:
            parser.error(f"{TARGET.relative_to(ROOT)} is stale; render it without --check")
        return 0
    TARGET.write_text(rendered, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
