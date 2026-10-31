"""Distinct, specific errors for archive I/O (format ADR section 1).

Every archive read is treated as untrusted input, so each refusal has its own type: a caller can
tell a tampered part from a zip-slip name from a decompression bomb. All derive from
:class:`ArchiveError`; an unknown required capability raises
:class:`beadlog.UnsupportedCapabilityError` (a ``ManifestError``) as for any other manifest read.
"""

from __future__ import annotations


class ArchiveError(ValueError):
    """Base of every archive read or write failure."""


class MalformedArchiveError(ArchiveError):
    """Not a ZIP, or a ZIP whose layout, manifest or member set breaks the format."""


class MissingMimetypeError(MalformedArchiveError):
    """The first entry is not ``mimetype`` (absent, or present but not first)."""


class InvalidMimetypeError(MalformedArchiveError):
    """``mimetype`` is present and first but is compressed, carries extra fields or names a
    different media type."""


class UnsafePathError(MalformedArchiveError):
    """A member name that could escape an extraction root (zip-slip): absolute, ``..``/``.``
    segments, backslashes, NUL, drive letters, empty segments or duplicate names."""


class ArchiveLimitError(ArchiveError):
    """A reader limit was exceeded (decompression bomb or entry flood)."""


class EntryTooLargeError(ArchiveLimitError):
    """A member's decompressed size (declared or streamed) exceeds the reader limit."""


class ArchiveTooLargeError(ArchiveLimitError):
    """The sum of decompressed member sizes exceeds the reader limit."""


class TooManyEntriesError(ArchiveLimitError):
    """More entries than the reader limit."""


class IntegrityError(ArchiveError):
    """A member's bytes disagree with what the manifest recorded."""


class PartHashMismatchError(IntegrityError):
    """A member's SHA-256 differs from the manifest (a tampered or corrupted part)."""


class MemberSizeMismatchError(IntegrityError):
    """A member's byte count differs from the manifest."""


class CorruptMemberError(IntegrityError):
    """A member's compressed stream is damaged (bad CRC-32, truncated or undecodable)."""


class EventIdMismatchError(IntegrityError):
    """An event's id differs from the id recomputed from its identity material."""


class IndexMismatchError(IntegrityError):
    """``index/recorded.jsonl`` disagrees with the events it indexes."""


class RecordCountMismatchError(IntegrityError):
    """A JSONL member's line count differs from the manifest (or the manifest counts differ
    from the archive's events)."""


class PartFormatError(ArchiveError):
    """A JSONL line that is not a well-formed UTF-8, LF-terminated BeadLog event, or an event
    in the wrong part (wrong group, wrong kind for its directory)."""


class PartOrderError(PartFormatError):
    """Events within a group are not in part order (``sorted_by``), so the archive cannot be
    streamed or merged correctly."""


class ArchiveWriteError(ArchiveError):
    """The inputs to the writer cannot be written as a valid archive (undeclared group, bad
    directory name, too many parts, ...)."""
