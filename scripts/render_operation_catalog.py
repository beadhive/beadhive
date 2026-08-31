#!/usr/bin/env python3
"""Render the canonical Python operation declaration as language-neutral JSON."""

from __future__ import annotations

import json
from pathlib import Path

from beadhive.operation_catalog import document

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "schemas" / "wire" / "v1.2.0" / "operation-catalog-v1.json"


def main() -> int:
    TARGET.write_text(json.dumps(document(), indent=2, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
