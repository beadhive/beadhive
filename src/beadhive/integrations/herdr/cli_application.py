"""Deprecated module-name facade for Herdr application services.

Policy and orchestration live in :mod:`application_services`.  The module-object alias preserves
the frozen monkeypatch surface until the compatibility ledger permits its removal.
"""

from __future__ import annotations

import sys

from . import application_services as _implementation

sys.modules[__name__] = _implementation
