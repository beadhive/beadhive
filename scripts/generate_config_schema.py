#!/usr/bin/env python3
"""Publish or check the official configuration JSON Schema artifact."""

from __future__ import annotations

import argparse
from pathlib import Path

from beadhive.contract_release import RELEASE_VERSION, render_release, write_release

ROOT = Path(__file__).resolve().parents[1]
ARTIFACT_ROOT = ROOT / f"src/beadhive/schemas/contracts/v{RELEASE_VERSION}/artifacts"
ARTIFACT = ARTIFACT_ROOT / "config-v1.schema.json"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="refuse checked-artifact drift")
    args = parser.parse_args()
    artifacts = [
        (ARTIFACT_ROOT.parent / relative, payload)
        for relative, payload in render_release().items()
        if relative == Path("artifacts/config-v1.schema.json")
        or (relative.parent == Path("artifacts") and relative.name.startswith("plugin-config-"))
    ]
    if args.check:
        stale = [
            path.relative_to(ROOT)
            for path, expected in artifacts
            if not path.is_file() or path.read_bytes() != expected
        ]
        if stale:
            parser.error(f"checked config artifacts are stale: {', '.join(map(str, stale))}")
        return 0
    # Publish the whole current bundle atomically, keeping inventory hashes coupled.
    write_release(ARTIFACT_ROOT.parent)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
