"""The "a fleet config revision was published in this process" signal (``bh-pc9rf``).

The SQL and Git fleet-config stores publish directly (fleet mode, beadyard, seed, release
prepare-config), bypassing ``config_store``'s writers, yet this process may hold a memoized
effective config (``config_store.process_load_memo``). The stores call :func:`published`; the
config store subscribes its cache clear. A leaf module with no imports, so neither store gains an
import edge to ``config_store`` (which would close import cycles).
"""

from __future__ import annotations

from collections.abc import Callable

_listeners: list[Callable[[], None]] = []


def subscribe(listener: Callable[[], None]) -> None:
    """Call ``listener`` after every publish in this process (idempotent per listener)."""
    if listener not in _listeners:
        _listeners.append(listener)


def published() -> None:
    """A store published (or attempted to publish) a revision: drop every in-process memo."""
    for listener in tuple(_listeners):
        listener()


__all__ = ["published", "subscribe"]
