"""The in-data fence adapter registry — the M1 data switch (bh-4c7p4, bh-12hev, bh-uz46l).

``(prefix, hive_dir) -> FenceData | None``. ``None`` means "no in-data fence adapter for this
hive": adopt takes the legacy fence-first path and the write guard keeps the lease gate.

The built-in default is "no adapter" for every hive, so a process that never loads the product
adapter fails safe: it treats every hive as legacy. The product adapter (M1, ``bh-uz46l``)
registers itself as the default when :mod:`beadhive.fence_data` is imported
(:func:`register_default_resolver`, idempotent); every process entrypoint that can reach the
guard, lease or adopt paths imports it once before use (``fence_data.register()``). That
resolver answers an adapter ONLY for a hive whose local data already carries ``bh_writer``, so
the coexistence path stays dormant until a hive's data switches it on.

A leaf module with no ``beadhive`` imports at all (the adapter is typed ``Any`` here; its
protocol is :class:`beadhive.writer_adopt.FenceData`), so :mod:`beadhive.writer_adopt` (adopt),
:mod:`beadhive.guard` (the hot-path write gate) and :mod:`beadhive.fence_data` (the adapter)
can all depend on it without any of them importing another.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

__all__ = [
    "FenceDataResolver",
    "default_resolver",
    "fence_data_for",
    "register_default_resolver",
    "set_fence_data_resolver",
]

FenceDataResolver = Callable[[str, Path], Any]


def _no_fence_data(_prefix: str, _hive_dir: Path) -> Any:
    return None


_default_resolver: FenceDataResolver = _no_fence_data
_fence_data_resolver: FenceDataResolver = _no_fence_data


def register_default_resolver(resolver: FenceDataResolver) -> None:
    """Make ``resolver`` the default (the product adapter registers here at import).

    Idempotent. The active resolver follows the new default unless an explicit override from
    :func:`set_fence_data_resolver` is in place."""
    global _default_resolver, _fence_data_resolver
    if _fence_data_resolver is _default_resolver:
        _fence_data_resolver = resolver
    _default_resolver = resolver


def default_resolver() -> FenceDataResolver:
    """The registered default (the built-in "no adapter" until the product one registers)."""
    return _default_resolver


def set_fence_data_resolver(resolver: FenceDataResolver | None) -> None:
    """Override the in-data fence adapter; ``None`` restores the registered default."""
    global _fence_data_resolver
    _fence_data_resolver = resolver or _default_resolver


def fence_data_for(prefix: str, hive_dir: Path) -> Any:
    """The hive's :class:`~beadhive.writer_adopt.FenceData`, or ``None`` (legacy / not cut over)."""
    return _fence_data_resolver(prefix, Path(hive_dir))
