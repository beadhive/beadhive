"""psycopg connection setup for XTDB's pgwire (bh-w56ts E3 and the write/read patterns).

Four adapter facts make psycopg usable against XTDB 2.2:

- psycopg sends ``str`` with OID 0 (unknown) and XTDB refuses untyped parameters in DML, so
  ``str`` is registered with :class:`psycopg.types.string.StrDumper` (a typed ``text``).
- psycopg sends a small ``int`` as ``int2``, which creates a ``smallint`` column, so ``int`` is
  registered with ``Int8Dumper``.
- A whole document is one :class:`TransitParam`, sent as OID 16384 (``transit``) and written
  with ``INSERT INTO <table> RECORDS %s, %s, ...``: no hand-built SQL per document shape.
- ``SET fallback_output_format='transit'`` makes nested and union-typed columns arrive as
  transit text (OID 16384), which :class:`TransitLoader` decodes to typed Python values; under
  the default JSON output nested timestamps become strings and sets become lists.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping, Sequence
from typing import Any

import psycopg
from psycopg.adapt import Dumper, Loader
from psycopg.types.numeric import Int8Dumper
from psycopg.types.string import StrDumper

from ._transit import decode, encode_bytes

TRANSIT_OID = 16384
READ_FORMAT_SQL = "SET fallback_output_format='transit'"
DEFAULT_BATCH_SIZE = 500
# A loopback single-node default: the operator-started XTDB binds 127.0.0.1 (bh-cntr0).
DEFAULT_PARAMS: Mapping[str, Any] = {
    "host": "127.0.0.1",
    "port": 5432,
    "user": "xtdb",
    "dbname": "xtdb",
}
_TABLE = re.compile(r"^[a-z][a-z0-9_]*$")


class TransitParam:
    """One value (a document) sent as a single transit-JSON parameter."""

    __slots__ = ("value",)

    def __init__(self, value: Any) -> None:
        self.value = value

    def __repr__(self) -> str:
        return f"TransitParam({self.value!r})"


class TransitDumper(Dumper):
    """Dump a :class:`TransitParam` as OID 16384 transit-JSON text."""

    oid = TRANSIT_OID

    def dump(self, obj: Any) -> bytes:
        return encode_bytes(obj.value)


class TransitLoader(Loader):
    """Load an OID 16384 column into Python values (maps, frozensets, aware datetimes)."""

    def load(self, data: Any) -> Any:
        return decode(bytes(data).decode("utf-8"))


def configure_adapters(adapters: Any) -> None:
    """Register the dumpers and the transit loader on ``adapters`` (a connection's or a map)."""
    adapters.register_dumper(TransitParam, TransitDumper)
    adapters.register_dumper(str, StrDumper)
    adapters.register_dumper(int, Int8Dumper)
    adapters.register_loader(TRANSIT_OID, TransitLoader)


def configure_connection(connection: Any, *, transit_reads: bool = True) -> Any:
    """Prepare ``connection`` for XTDB: adapters, then transit output for reads.

    The ``SET`` runs outside any transaction (autocommit is switched on for that one statement
    and restored), because it is a session setting and XTDB rejects it inside a transaction.
    """
    configure_adapters(connection.adapters)
    if transit_reads:
        previous = connection.autocommit
        connection.autocommit = True
        try:
            connection.execute(READ_FORMAT_SQL)
        finally:
            connection.autocommit = previous
    return connection


def connect(
    conninfo: str = "",
    *,
    autocommit: bool = False,
    transit_reads: bool = True,
    connector: Callable[..., Any] | None = None,
    **params: Any,
) -> Any:
    """Open a configured connection to an XTDB node.

    ``conninfo`` is an optional libpq string; keyword ``params`` (``host``, ``port``, ``user``,
    ``dbname``, ``password``, ...) override :data:`DEFAULT_PARAMS`, which point at a loopback node.
    ``connector`` is ``psycopg.connect`` unless a test passes a fake.
    """
    if connector is None:
        connector = psycopg.connect
    merged = dict(params) if conninfo else {**DEFAULT_PARAMS, **params}
    connection = connector(conninfo, autocommit=autocommit, **merged)
    return configure_connection(connection, transit_reads=transit_reads)


def check_table(table: str) -> str:
    """``table`` when it is a plain lower-case identifier; table names are interpolated into SQL."""
    if not isinstance(table, str) or _TABLE.fullmatch(table) is None:
        raise ValueError(f"not a safe table name: {table!r}")
    return table


def insert_statement(table: str, count: int) -> str:
    """``INSERT INTO <table> RECORDS %s, %s, ...`` for ``count`` documents."""
    if count < 1:
        raise ValueError("an INSERT needs at least one document")
    return f"INSERT INTO {check_table(table)} RECORDS " + ", ".join(["%s"] * count)


def insert_records(
    connection: Any,
    table: str,
    documents: Iterable[Mapping[str, Any]],
    *,
    batch_size: int = DEFAULT_BATCH_SIZE,
) -> int:
    """INSERT ``documents`` into ``table`` in statements of ``batch_size``; returns the count.

    This issues statements on whatever transaction ``connection`` is in; the caller owns
    ``BEGIN``/``COMMIT`` (and any ``SYSTEM_TIME`` setting), so one XTDB transaction can hold a
    whole ``recorded_at`` group.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be at least 1")
    batch: list[Mapping[str, Any]] = []
    total = 0

    def flush() -> None:
        connection.execute(
            insert_statement(table, len(batch)), [TransitParam(doc) for doc in batch]
        )

    for document in documents:
        batch.append(document)
        if len(batch) == batch_size:
            flush()
            total += len(batch)
            batch = []
    if batch:
        flush()
        total += len(batch)
    return total


def transit_params(documents: Sequence[Mapping[str, Any]]) -> list[TransitParam]:
    """Wrap ``documents`` as transit parameters, one per document."""
    return [TransitParam(document) for document in documents]


__all__ = [
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_PARAMS",
    "READ_FORMAT_SQL",
    "TRANSIT_OID",
    "TransitDumper",
    "TransitLoader",
    "TransitParam",
    "check_table",
    "configure_adapters",
    "configure_connection",
    "connect",
    "insert_records",
    "insert_statement",
    "transit_params",
]
