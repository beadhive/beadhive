"""The ``beadlog.store`` password port backed by the host's fnox secret broker.

A store plugin's fragment names a fnox lookup (``credential``: ``config_path``, ``profile``,
``key``), the same host-local reference HQ's SQL transport uses
(:class:`beadhive.hq_sql_transport.FnoxBroker`). :func:`fnox_password_source` turns it into the
:data:`~beadhive.modules.beadlog_store.contracts.PasswordSource` the composition root hands to the
store: nothing is resolved until the store connects, the value is returned to the store only, and
a broker failure is a :class:`~beadhive.modules.beadlog_store.contracts.BeadlogStoreError` with the
broker's static message (never a value).
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from .bootstrap.beadlog_store import credential_configured
from .modules.beadlog_store.contracts import BeadlogStoreError, PasswordSource

#: How long one fnox lookup may take before the store refuses (the broker is local).
FNOX_DEADLINE_SECONDS = 15.0


def fnox_password_source(
    settings: Mapping[str, Any],
    *,
    broker: Any = None,
    deadline_seconds: float = FNOX_DEADLINE_SECONDS,
) -> PasswordSource:
    """The store's password port: ``None`` when no fnox lookup is configured, otherwise a call
    to the host's fnox broker made only when invoked."""
    if not credential_configured(settings):
        return lambda: None
    reference = dict(settings["credential"])

    def resolve() -> str | None:
        from .hq_sql_transport import FnoxBroker, SqlTransportError  # lazy: only on connect

        try:
            value = (broker or FnoxBroker()).get(
                reference, deadline=time.monotonic() + deadline_seconds
            )
        except SqlTransportError as exc:  # the broker's messages are static, never a value
            raise BeadlogStoreError(f"the XTDB password could not be resolved: {exc}") from None
        return value or None

    return resolve


__all__ = ["FNOX_DEADLINE_SECONDS", "fnox_password_source"]
