"""The in-data fence adapter registry — the M1 data switch (bh-4c7p4, bh-12hev).

``(prefix, hive_dir) -> FenceData | None``. ``None`` means "no in-data fence adapter for this
hive": adopt takes the legacy fence-first path and the write guard keeps the lease gate. The
product adapter is M1's (``bh-uz46l``); until it registers one, every hive answers ``None`` and
the coexistence behaviour is dormant until a hive's data switches it on.

A leaf module with no ``beadhive`` imports at all (the adapter is typed ``Any`` here; its
protocol is :class:`beadhive.writer_adopt.FenceData`), so both :mod:`beadhive.writer_adopt`
(adopt) and :mod:`beadhive.guard` (the hot-path write gate) can consult it without either
importing the other.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = ["FenceDataResolver", "fence_data_for", "set_fence_data_resolver"]

FenceDataResolver = Callable[[str, Path], Any]


def _no_fence_data(_prefix: str, _hive_dir: Path) -> Any:
    return None


_fence_data_resolver: FenceDataResolver = _no_fence_data


def set_fence_data_resolver(resolver: FenceDataResolver | None) -> None:
    """Register the in-data fence adapter (M1); ``None`` restores the dormant default."""
    global _fence_data_resolver
    _fence_data_resolver = resolver or _no_fence_data


def fence_data_for(prefix: str, hive_dir: Path) -> Any:
    """The hive's :class:`~beadhive.writer_adopt.FenceData`, or ``None`` (legacy / dormant)."""
    return _fence_data_resolver(prefix, Path(hive_dir))
