"""Compatibility facade for the canonical configuration contracts.

New code imports :mod:`beadhive.modules.config.contracts`. Existing callers keep this module
until the migration ledger proves that the public import and monkeypatch surface can be removed.
"""

from .modules.config import contracts as _contracts
from .modules.config.contracts import *  # noqa: F403
from .modules.config.contracts import _DEFAULT_WORKTREE_INIT  # noqa: F401

__all__ = _contracts.__all__
