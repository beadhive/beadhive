"""Root composition of the HQ storage port's Dolt adapter (hq-storage-port-adr section 7).

The adapter package (``beadhive_hq_storage_dolt``) never imports PyMySQL or ``beadhive``: this
module injects root's transport and fence engines into it.

* :func:`hq_connection_factory` closes over :func:`beadhive.hq_sql_transport.connect`, one
  ``HqSqlConnection`` binding per adapter :class:`~beadhive_hq_storage_dolt.Role`, so the bounded
  operation deadline, no-reconnect and verified-TLS behaviour stay exactly as they are and PyMySQL
  stays lazily imported here.
* :func:`dolt_hive_fence` binds a hive's ``DoltCliEngine`` / ``BdServerEngine`` with the
  complete 44-trigger guard set from :mod:`beadhive.fence_schema`.
* :func:`placement_storage` is the director's store (bh-sqfvq, the port's first production
  consumer, :mod:`beadhive.hq_sql_placement`): one ``placement_writer`` binding for its reads,
  its placement and failover-policy writes and the session ages it fails over on, every session
  principal-checked, over root's runtime tables (``runtime_tables=True``, bh-xnn5p).
* :func:`config_history` is the HQ config publisher's ``CommittedHistory`` (bh-x7xtc,
  :mod:`beadhive.hq_sql_config`): the adapter's ``DoltConfigHistory`` over the committed
  ``hq_config_*`` tables, its ``reader`` and ``publisher`` bindings principal-checked.
* :func:`runtime_statements` is the HQ runtime database's Dolt-only statement surface over a
  connection root opened itself (bh-xnn5p): the runtime authority, the trusted receiver, the
  operator and the session rows keep their own transport and transactions, and the adapter
  owns every statement.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from typing import Any

from beadhive_hq_storage_dolt import (
    ConnectionFactory,
    DoltConfigHistory,
    DoltHiveFence,
    DoltHqStorage,
    FenceStatementEngine,
    HqRuntimeStatements,
    Role,
)

from . import fence_schema

__all__ = [
    "config_history",
    "dolt_hive_fence",
    "dolt_hq_storage",
    "hq_connection_factory",
    "is_transport_deadline",
    "placement_storage",
    "runtime_statements",
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


def placement_storage(
    binding: Mapping[str, Any],
    *,
    broker: Any = None,
    connect: Callable[[Mapping[str, Any]], Any] | None = None,
) -> DoltHqStorage:
    """The director's HQ store over one ``placement_writer`` binding (``HqSqlConnection``).

    Reads (``Role.READER``) and the placement and failover-policy writes
    (``Role.PLACEMENT_WRITER``) both open that binding, and every session first checks the
    connection is its ``user`` on its ``database``'s ``main`` branch, as the director always has.
    The store maps ``FAILOVER_POLICY`` onto ``hq_live_failover_policy`` and session liveness onto
    the per-incarnation tables (``runtime_tables``). ``connect(binding)`` replaces the transport
    (a test's own connection); it is called once per adapter session."""
    if connect is None:
        factory = hq_connection_factory(
            {Role.READER.value: binding, Role.PLACEMENT_WRITER.value: binding}, broker=broker
        )
    else:

        def factory(_role: Role) -> Any:
            return connect(binding)

    principal = (str(binding.get("user", "")), str(binding.get("database", "")))
    return DoltHqStorage(
        factory,
        deadline=is_transport_deadline,
        principals={Role.READER: principal, Role.PLACEMENT_WRITER: principal},
        runtime_tables=True,
    )


def config_history(settings: Mapping[str, Any], *, broker: Any = None) -> DoltConfigHistory:
    """The HQ config ``CommittedHistory`` over root's ``hq.sql.config`` settings.

    ``Role.READER`` opens the ``reader`` binding (head, lookup) and ``Role.PUBLISHER`` the
    ``publisher`` binding (publish, fetch, push); each session first checks the connection is
    that binding's ``user`` on its ``database``'s ``main`` branch, as root's ``_identity`` does.
    A role without a binding is ``StorageUnavailable`` before anything is sent."""
    bindings = {
        role.value: settings[role.value]
        for role in (Role.READER, Role.PUBLISHER)
        if settings.get(role.value)
    }
    principals = {
        Role(name): (str(binding.get("user", "")), str(binding.get("database", "")))
        for name, binding in bindings.items()
    }
    return DoltConfigHistory(
        hq_connection_factory(bindings, broker=broker),
        deadline=is_transport_deadline,
        principals=principals,
    )


def runtime_statements(cursor: Any) -> HqRuntimeStatements:
    """The runtime database's statements over ``cursor`` (a surface passes through as is)."""
    return HqRuntimeStatements.of(cursor)


def dolt_hive_fence(engine: FenceStatementEngine, **options: Any) -> DoltHiveFence:
    """A hive's ``HiveFence`` over its fence engine, expecting the complete guard install."""
    return DoltHiveFence(engine, guards=tuple(fence_schema.trigger_names()), **options)
