"""BeadLog: the bead history interchange and plan format (``.bdlg``).

A stdlib-only **library** package (``docs/design/package-class-library-vs-plugin-adr.md``): it
never imports ``beadhive``, and root and other packages import it only through this ``__all__``.
The bytes are specified by ``docs/design/beadlog-format-adr.md`` and the JSON Schemas under
``docs/schemas/beadlog/v0.1/``; this package implements the domain over them:

- events: :class:`BeadEvent` and its parts (:class:`BeadRef`, :class:`Source`,
  :class:`EventKind`, ...), parsed from and written to JSON objects;
- ids: :func:`canonical_json` (RFC 8785 over the admissible domain), :func:`event_id`,
  :func:`mint_event`;
- order: :func:`ordering_key` ``(occurred_at, recorded_at, id)`` and archive part order;
- merge: :func:`union_merge` (commutative, associative, idempotent; tombstones absorbing);
- fold: :func:`fold` to per-bead :class:`BeadState` at a valid-time or bitemporal cut;
- diff: :func:`diff` (proposed events absent from a baseline plus per-bead deltas);
- manifest: :class:`Manifest`, :class:`Capabilities` and the capability vocabularies.

- archive: :func:`write_archive` (reproducible ``.bdlg`` writer), :class:`ArchiveReader`
  (streaming, untrusted-input reader), :func:`read_archive`, :class:`ReadLimits` and the
  specific :class:`ArchiveError` family (see ``beadlog.archive``);
- testing: ``beadlog.testing``, the loader-conformance kit (:func:`conformance_cases`,
  :class:`InMemoryStore`, :func:`load_archive`).
"""

from __future__ import annotations

from ._diff import BeadDelta, LogDiff, changed_facets, diff
from ._fold import BeadState, Comment, checkpoint_key, fold
from ._ids import IDENTITY_MEMBERS, event_id, identity_projection, mint_event
from ._json import MAX_SAFE_INTEGER, AdmissibleValueError, canonical_bytes, canonical_json
from ._manifest import (
    FILE_EXTENSION,
    FORMAT_VERSION,
    KNOWN_CAPABILITIES,
    MEDIA_TYPE,
    TIME_SEMANTICS,
    Capabilities,
    Capability,
    Generator,
    Group,
    LoaderCapability,
    Manifest,
    ManifestError,
    UnsupportedCapabilityError,
    check_format_version,
)
from ._merge import IdCollisionError, apply_tombstones, erasure, union_merge
from ._model import (
    BeadEvent,
    BeadRef,
    Edge,
    EventFormatError,
    EventKind,
    Evidence,
    Precision,
    Provenance,
    Source,
)
from ._order import OrderKey, order_key, ordering_key, part_order, sort_events
from ._time import TimestampError, check_timestamp, format_timestamp, instant
from .archive import (
    DEFAULT_MAX_PART_BYTES,
    ArchiveError,
    ArchiveLimitError,
    ArchiveReader,
    ArchiveTooLargeError,
    ArchiveWriteError,
    CorruptMemberError,
    EntryTooLargeError,
    EventIdMismatchError,
    IndexEntry,
    IndexMismatchError,
    IntegrityError,
    InvalidMimetypeError,
    MalformedArchiveError,
    MemberSizeMismatchError,
    MissingMimetypeError,
    PartFormatError,
    PartHashMismatchError,
    PartOrderError,
    ReadLimits,
    RecordCountMismatchError,
    TooManyEntriesError,
    UnsafePathError,
    check_member_name,
    read_archive,
    write_archive,
)
from .testing import (
    ConformanceCase,
    ConformanceFailure,
    ConformanceStore,
    InMemoryStore,
    assert_conformant,
    conformance_cases,
    load_archive,
    run_conformance,
)

DISTRIBUTION = "beadlog"


def distribution_name() -> str:
    """Return this package's distribution name."""
    return DISTRIBUTION


__all__ = [
    "DEFAULT_MAX_PART_BYTES",
    "DISTRIBUTION",
    "FILE_EXTENSION",
    "FORMAT_VERSION",
    "IDENTITY_MEMBERS",
    "KNOWN_CAPABILITIES",
    "MAX_SAFE_INTEGER",
    "MEDIA_TYPE",
    "TIME_SEMANTICS",
    "AdmissibleValueError",
    "ArchiveError",
    "ArchiveLimitError",
    "ArchiveReader",
    "ArchiveTooLargeError",
    "ArchiveWriteError",
    "BeadDelta",
    "BeadEvent",
    "BeadRef",
    "BeadState",
    "Capabilities",
    "Capability",
    "Comment",
    "ConformanceCase",
    "ConformanceFailure",
    "ConformanceStore",
    "CorruptMemberError",
    "Edge",
    "EntryTooLargeError",
    "EventFormatError",
    "EventIdMismatchError",
    "EventKind",
    "Evidence",
    "Generator",
    "Group",
    "IdCollisionError",
    "InMemoryStore",
    "IndexEntry",
    "IndexMismatchError",
    "IntegrityError",
    "InvalidMimetypeError",
    "LoaderCapability",
    "LogDiff",
    "MalformedArchiveError",
    "Manifest",
    "ManifestError",
    "MemberSizeMismatchError",
    "MissingMimetypeError",
    "OrderKey",
    "PartFormatError",
    "PartHashMismatchError",
    "PartOrderError",
    "Precision",
    "Provenance",
    "ReadLimits",
    "RecordCountMismatchError",
    "Source",
    "TimestampError",
    "TooManyEntriesError",
    "UnsafePathError",
    "UnsupportedCapabilityError",
    "apply_tombstones",
    "assert_conformant",
    "canonical_bytes",
    "canonical_json",
    "changed_facets",
    "check_format_version",
    "check_member_name",
    "check_timestamp",
    "checkpoint_key",
    "conformance_cases",
    "diff",
    "distribution_name",
    "erasure",
    "event_id",
    "fold",
    "format_timestamp",
    "identity_projection",
    "instant",
    "load_archive",
    "mint_event",
    "order_key",
    "ordering_key",
    "part_order",
    "read_archive",
    "run_conformance",
    "sort_events",
    "union_merge",
    "write_archive",
]
