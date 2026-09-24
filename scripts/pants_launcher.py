"""Compatibility shim for :mod:`beadhive_pants.launcher`."""

from __future__ import annotations

from beadhive_pants.compat import install_legacy_script

install_legacy_script(__name__, "beadhive_pants.launcher")
