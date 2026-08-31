#!/usr/bin/env python3
"""Publish or check the official configuration JSON Schema artifact."""

from __future__ import annotations

import argparse
from pathlib import Path

from beadhive.modules.config.application.schema_artifacts import (
    generate_config_json_schema_bytes,
)

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "docs/schemas/wire/v1.4.0/config-v1.schema.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="refuse checked-artifact drift")
    args = parser.parse_args()
    expected = generate_config_json_schema_bytes()
    if args.check:
        if not ARTIFACT.is_file() or ARTIFACT.read_bytes() != expected:
            parser.error(f"{ARTIFACT.relative_to(ROOT)} is stale; regenerate it")
        return 0
    ARTIFACT.write_bytes(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
