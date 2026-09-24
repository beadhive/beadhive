#!/usr/bin/env python3
"""Validate the fail-closed inventory of tests that read docs at runtime."""

from __future__ import annotations

from pathlib import Path

from beadhive.kernel.plugins import DiagnosticSeverity
from beadhive_pants.verify import doc_readers as _doc_readers
from beadhive_pants.verify import verify_proven_manifest

ROOT = Path(__file__).parents[1]
MANIFEST = ROOT / "scripts" / "pants_proven_tests.json"
PLUGIN_MANIFEST = (
    ROOT / "packages" / "beadhive-pants" / "src" / "beadhive_pants" / "data" / "proven_tests.json"
)


def doc_readers() -> set[str]:
    """Compatibility wrapper for callers of the former script implementation."""
    return _doc_readers(ROOT)


def main() -> int:
    diagnostic = verify_proven_manifest(ROOT)
    print(f"pants-proven: {diagnostic.detail}")
    return int(diagnostic.severity is DiagnosticSeverity.ERROR)


if __name__ == "__main__":
    raise SystemExit(main())
