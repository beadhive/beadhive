"""Compatibility identity for the canonical operation kernel.

The implementation moved to :mod:`beadhive.kernel.operations`.  Replacing this module entry
with that module object preserves historical public and monkeypatch identity without creating a
second registry.
"""

from __future__ import annotations

import sys

from .kernel import operations as _operations

sys.modules[__name__] = _operations
