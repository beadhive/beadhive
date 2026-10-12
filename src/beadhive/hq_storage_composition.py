"""Root composition of the HQ storage port's Dolt adapter (hq-storage-port-adr section 7).

The adapter package (``beadhive_hq_storage_dolt``) never imports PyMySQL or ``beadhive``: this
module injects root's transport and fence engines into it.

* :func:`hq_connection_factory` closes over :func:`beadhive.hq_sql_transport.connect`, one
  ``HqSqlConnection`` binding per adapter :class:`~beadhive_hq_storage_dolt.Role`, so the bounded
  operation deadline, no-reconnect and verified-TLS behaviour stay exactly as they are and PyMySQL
  stays lazily imported here.
* :func:`dolt_hive_fence` binds a hive's ``DoltCliEngine`` / ``BdServerEngine`` with the
  complete 44-trigger guard set from :mod:`beadhive.fence_schema`.

No consumer is migrated here; the follow-on beads of ``bh-xxkf7`` rebuild them over the port.
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

from beadhive_hq_storage_dolt import (
    ConnectionFactory,
    DoltHiveFence,
    DoltHqStorage,
    FenceStatementEngine,
    Role,
)

from . import fence_schema

__all__ = [
    "dolt_hive_fence",
    "dolt_hq_storage",
    "hq_connection_factory",
    "is_transport_deadline",
]


def is_transport_deadline(exc: BaseException) -> bool:
    """Whether ``exc`` is the transport's operation-deadline refusal (no numeric code)."""
    from .hq_sql_transport import SqlTransportError

    return isinstance(exc, SqlTransportError) and "deadline" in str(exc)


def hq_connection_factory(
    bindings: Mapping[str, Mapping[str, Any]], *, broker: Any = None
) -> ConnectionFactory:
    """A ``Callable[[Role], Connection]`` over ``hq_sql_transport.connect``.

    ``bindings`` maps a role name (``operator``, ``placement_writer``, ``publisher``, ``frame``,
    ``reader``) to its ``HqSqlConnection`` settings. Each call opens one fresh connection under
    that binding's own ``operation_timeout`` deadline; a missing binding or a refused connection
    raises before anything is sent, which the adapter reports as ``StorageUnavailable``."""

    def connect(role: Role) -> Any:
        from .hq_sql_transport import FnoxBroker, SqlTransportError
        from .hq_sql_transport import connect as transport_connect

        binding = bindings.get(Role(role).value)
        if not binding:
            raise SqlTransportError(f"HQ {Role(role).value} SQL binding unavailable")
        deadline = time.monotonic() + float(binding.get("operation_timeout", 15))
        return transport_connect(dict(binding), broker or FnoxBroker(), deadline=deadline)

    return connect


def dolt_hq_storage(
    bindings: Mapping[str, Mapping[str, Any]], *, broker: Any = None, **options: Any
) -> DoltHqStorage:
    """The Dolt HQ store over root's bounded transport."""
    return DoltHqStorage(
        hq_connection_factory(bindings, broker=broker), deadline=is_transport_deadline, **options
    )


def dolt_hive_fence(engine: FenceStatementEngine, **options: Any) -> DoltHiveFence:
    """A hive's ``HiveFence`` over its fence engine, expecting the complete guard install."""
    return DoltHiveFence(engine, guards=tuple(fence_schema.trigger_names()), **options)
