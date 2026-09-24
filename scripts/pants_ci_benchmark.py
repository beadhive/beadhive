#!/usr/bin/env python3
"""Compatibility shim for :mod:`beadhive_pants.benchmark`."""

from __future__ import annotations

from beadhive_pants.compat import install_legacy_script

_implementation = install_legacy_script(__name__, "beadhive_pants.benchmark")
if __name__ == "__main__":
    raise SystemExit(_implementation.main())
