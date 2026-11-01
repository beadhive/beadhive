"""XTDB loader library for BeadLog: connection, layout (b), transit and type discipline.

A **library** package (``docs/design/package-class-library-vs-plugin-adr.md``): it depends on
``beadlog`` (imported through its public ``__all__`` only) and ``psycopg``, and never imports
``beadhive``. The design is ``docs/design/beadlog-format-adr.md`` amendment A6, with the evidence
in the ``bh-w56ts`` (document shape), ``bh-3jp2g`` (bitemporal load) and ``bh-cntr0``
(operations) spikes. This package is the store-specific foundation every loader and reader call
uses; the op mapping, the monotone initial load and the as-of reader are built on it.

- connection: :func:`connect` / :func:`configure_connection` (typed ``str`` and ``int`` dumpers,
  the transit dumper for OID 16384, transit reads), :func:`insert_records`;
- transit: :func:`encode` / :func:`decode` for the subset a bead document uses (sets, typed
  instants, escaped strings), :class:`Instant`;
- layout (b): :class:`Layout`, :func:`bead_row`, :func:`label_row`, :func:`edge_row`,
  :func:`comment_row`, :func:`state_rows`, :func:`record_rows`, :func:`decode_bead_row`;
- type discipline: :class:`ColumnTypes` (one type per column, coercion or refusal);
- operations: :func:`check_version` (``>=2.2.0,<2.3``), :func:`find_dangling_edges`;
- as-of reader: :func:`read_state` / :func:`read_history` (events with ``provenance: observed``,
  ``source.system: xtdb``) and :func:`write_baseline` (a ``.bdlg`` for ``reconcile``).
"""

from __future__ import annotations

from ._connect import (
    DEFAULT_BATCH_SIZE,
    DEFAULT_PARAMS,
    READ_FORMAT_SQL,
    TRANSIT_OID,
    TransitDumper,
    TransitLoader,
    TransitParam,
    check_table,
    configure_adapters,
    configure_connection,
    connect,
    insert_records,
    insert_statement,
    transit_params,
)
from ._dangling import (
    DANGLING_EDGE_AS_OF_SQL,
    DANGLING_EDGE_SQL,
    EXTERNAL_PREFIX,
    DanglingEdge,
    DanglingReport,
    find_dangling_edges,
)
from ._layout import (
    BEAD_COMMENTS,
    BEAD_EDGES,
    BEAD_LABELS,
    BEADS,
    GROUP_SEPARATOR,
    ID_SEPARATOR,
    RECORD_TYPE_COLUMN,
    RENAMED_COLUMNS,
    TABLES,
    BeadRows,
    DecodedBead,
    Layout,
    LayoutError,
    ReservedColumnError,
    UnsupportedFieldError,
    bead_row,
    comment_row,
    decode_bead_row,
    edge_row,
    label_row,
    record_rows,
    rename_reserved,
    split_key,
    state_rows,
)
from ._reader import (
    GENERATOR_NAME,
    SOURCE_SYSTEM,
    ReaderError,
    ReadNote,
    ReadResult,
    group_dirs,
    read_history,
    read_state,
    write_baseline,
)
from ._transit import (
    Instant,
    Keyword,
    Symbol,
    Tagged,
    TransitError,
    decode,
    encode,
    encode_bytes,
    escape_string,
    unescape_string,
)
from ._types import KNOWN_COLUMNS, ColumnKind, ColumnTypeError, ColumnTypes, kind_of
from ._version import (
    EXCLUSIVE_MAXIMUM,
    MINIMUM,
    SUPPORTED_RANGE,
    VERSION_SQL,
    UnsupportedXtdbVersion,
    XtdbVersion,
    check_version,
    parse_version,
    require_supported_version,
)

DISTRIBUTION = "beadlog-xtdb"


def distribution_name() -> str:
    """Return this package's distribution name."""
    return DISTRIBUTION


__all__ = [
    "BEADS",
    "BEAD_COMMENTS",
    "BEAD_EDGES",
    "BEAD_LABELS",
    "DANGLING_EDGE_AS_OF_SQL",
    "DANGLING_EDGE_SQL",
    "DEFAULT_BATCH_SIZE",
    "DEFAULT_PARAMS",
    "DISTRIBUTION",
    "EXCLUSIVE_MAXIMUM",
    "EXTERNAL_PREFIX",
    "GENERATOR_NAME",
    "GROUP_SEPARATOR",
    "ID_SEPARATOR",
    "KNOWN_COLUMNS",
    "MINIMUM",
    "READ_FORMAT_SQL",
    "RECORD_TYPE_COLUMN",
    "RENAMED_COLUMNS",
    "SOURCE_SYSTEM",
    "SUPPORTED_RANGE",
    "TABLES",
    "TRANSIT_OID",
    "VERSION_SQL",
    "BeadRows",
    "ColumnKind",
    "ColumnTypeError",
    "ColumnTypes",
    "DanglingEdge",
    "DanglingReport",
    "DecodedBead",
    "Instant",
    "Keyword",
    "Layout",
    "LayoutError",
    "ReadNote",
    "ReadResult",
    "ReaderError",
    "ReservedColumnError",
    "Symbol",
    "Tagged",
    "TransitDumper",
    "TransitError",
    "TransitLoader",
    "TransitParam",
    "UnsupportedFieldError",
    "UnsupportedXtdbVersion",
    "XtdbVersion",
    "bead_row",
    "check_table",
    "check_version",
    "comment_row",
    "configure_adapters",
    "configure_connection",
    "connect",
    "decode",
    "decode_bead_row",
    "distribution_name",
    "edge_row",
    "encode",
    "encode_bytes",
    "escape_string",
    "find_dangling_edges",
    "group_dirs",
    "insert_records",
    "insert_statement",
    "kind_of",
    "label_row",
    "parse_version",
    "read_history",
    "read_state",
    "record_rows",
    "rename_reserved",
    "require_supported_version",
    "split_key",
    "state_rows",
    "transit_params",
    "unescape_string",
    "write_baseline",
]
