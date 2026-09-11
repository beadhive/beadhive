"""Compatibility facade for the state capability's snapshot/replay contract."""

from .modules.state.domain import stream as _stream
from .modules.state.domain.stream import *  # noqa: F403

__all__ = _stream.__all__
