"""``.bdlg`` archive I/O: a reproducible writer and a streaming, untrusted-input reader.

The bytes are specified by ``docs/design/beadlog-format-adr.md`` section 1. Import these names
from :mod:`beadlog` (``beadlog.__all__``), not from this subpackage's modules.
"""

from __future__ import annotations

from .errors import (
    ArchiveError,
    ArchiveLimitError,
    ArchiveTooLargeError,
    ArchiveWriteError,
    CorruptMemberError,
    EntryTooLargeError,
    EventIdMismatchError,
    IndexMismatchError,
    IntegrityError,
    InvalidMimetypeError,
    MalformedArchiveError,
    MemberSizeMismatchError,
    MissingMimetypeError,
    PartFormatError,
    PartHashMismatchError,
    PartOrderError,
    RecordCountMismatchError,
    TooManyEntriesError,
    UnsafePathError,
)
from .reader import (
    ArchiveReader,
    IndexEntry,
    ReadLimits,
    check_member_name,
    declared_entry_count,
    read_archive,
)
from .writer import (
    DEFAULT_MAX_PART_BYTES,
    MAX_PARTS_PER_GROUP,
    derive_capabilities,
    manifest_bytes,
    packaged_schemas,
    serialize_event,
    write_archive,
)

__all__ = [
    "DEFAULT_MAX_PART_BYTES",
    "MAX_PARTS_PER_GROUP",
    "ArchiveError",
    "ArchiveLimitError",
    "ArchiveReader",
    "ArchiveTooLargeError",
    "ArchiveWriteError",
    "CorruptMemberError",
    "EntryTooLargeError",
    "EventIdMismatchError",
    "IndexEntry",
    "IndexMismatchError",
    "IntegrityError",
    "InvalidMimetypeError",
    "MalformedArchiveError",
    "MemberSizeMismatchError",
    "MissingMimetypeError",
    "PartFormatError",
    "PartHashMismatchError",
    "PartOrderError",
    "ReadLimits",
    "RecordCountMismatchError",
    "TooManyEntriesError",
    "UnsafePathError",
    "check_member_name",
    "declared_entry_count",
    "derive_capabilities",
    "manifest_bytes",
    "packaged_schemas",
    "read_archive",
    "serialize_event",
    "write_archive",
]
