"""The one call root's lazy provider row makes (``bootstrap/beadlog_store.py``).

:func:`build` receives the already-validated ``plugins.beadlog-xtdb`` fragment and the credential
port, and returns an :class:`~beadhive_beadlog_xtdb.store.XtdbBeadlogStore`, which implements
``beadlog.store@1`` (``BeadlogStore``). Building connects nothing, resolves no password and
imports neither ``beadlog_xtdb`` nor psycopg; root maps any failure here to a typed refusal naming
only the exception class, and every message raised here is static anyway.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .settings import XtdbSettings
from .store import XtdbBeadlogStore

__all__ = ["build"]


def build(
    settings: Mapping[str, object], password_source: Callable[[], str | None]
) -> XtdbBeadlogStore:
    """Return the XTDB store for the validated fragment ``settings``."""
    if not callable(password_source):
        raise TypeError("password_source must be callable")
    return XtdbBeadlogStore(XtdbSettings.from_mapping(settings), password_source)
