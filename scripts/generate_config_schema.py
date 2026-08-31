#!/usr/bin/env python3
"""Publish or check the official configuration JSON Schema artifact."""

from __future__ import annotations

import argparse
from pathlib import Path

from beadhive.modules.config.application.schema_artifacts import (
    generate_config_json_schema_bytes,
    plugin_fragment_artifacts,
)

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT = ROOT / "docs/schemas/wire/v1.4.0/config-v1.schema.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="refuse checked-artifact drift")
    args = parser.parse_args()
    artifacts = [(ARTIFACT, generate_config_json_schema_bytes())]
    artifacts.extend(
        (
            ROOT
            / "docs/schemas/wire/v1.4.0"
            / f"plugin-config-{fragment.plugin_id}-v1.schema.json",
            payload,
        )
        for fragment, payload in plugin_fragment_artifacts()
    )
    if args.check:
        stale = [
            path.relative_to(ROOT)
            for path, expected in artifacts
            if not path.is_file() or path.read_bytes() != expected
        ]
        if stale:
            parser.error(f"checked config artifacts are stale: {', '.join(map(str, stale))}")
        return 0
    for path, expected in artifacts:
        path.write_bytes(expected)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
