#!/usr/bin/env python3
"""Render or check the canonical semantic telemetry event-envelope v1 schema."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from beadhive.kernel.telemetry import event_envelope_schema

ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "docs" / "schemas" / "telemetry-event-envelope-v1.schema.json"


def rendered_bytes() -> bytes:
    return (json.dumps(event_envelope_schema(), indent=2, sort_keys=True) + "\n").encode()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="fail when checked bytes drift")
    args = parser.parse_args()
    expected = rendered_bytes()
    if args.check:
        if not DESTINATION.exists() or DESTINATION.read_bytes() != expected:
            print("telemetry schema drift: run uv run python scripts/render_telemetry_schema.py")
            return 1
        print(f"telemetry schema: checked {DESTINATION.relative_to(ROOT)}")
        return 0
    DESTINATION.write_bytes(expected)
    print(f"telemetry schema: wrote {DESTINATION.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
