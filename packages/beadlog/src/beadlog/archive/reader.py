"""Streaming, untrusted-input reader for ``.bdlg`` archives (format ADR section 1).

Every archive read is hostile until proven otherwise. Opening an archive checks, before any
event is parsed: the entry count against the limit (read from the ZIP end record, so a flood is
refused before the central directory is materialised), safe member names (zip-slip), declared
decompressed sizes, ``mimetype`` first / STORED / exact, ``MANIFEST.json`` second, the manifest
shape, unknown required capabilities (refused), and that the member set is exactly what the
manifest lists. Reading events streams each part: a part is hashed in a first pass and only
then parsed, so no event of a tampered part reaches the caller. ZIP64 is handled by
:mod:`zipfile`.
"""

from __future__ import annotations

import hashlib
import heapq
import json
import os
import re
import struct
import zipfile
import zlib
from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import IO, Any

from .._ids import event_id
from .._manifest import (
    KNOWN_CAPABILITIES,
    MEDIA_TYPE,
    Capabilities,
    Manifest,
)
from .._model import BeadEvent, EventFormatError, EventKind
from .._order import part_order
from .._time import TimestampError, instant
from .errors import (
    ArchiveError,
    ArchiveTooLargeError,
    CorruptMemberError,
    EntryTooLargeError,
    EventIdMismatchError,
    IndexMismatchError,
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
from .writer import (
    EVENTS_SORTED_BY,
    INDEX_MEMBER,
    INDEX_SORTED_BY,
    MANIFEST_MEMBER,
    MIMETYPE_MEMBER,
    SCHEMA_MEMBERS,
)

_CHUNK = 64 * 1024
_EOCD = b"PK\x05\x06"
_ZIP64_LOCATOR = b"PK\x06\x07"
_ZIP64_EOCD = b"PK\x06\x06"
_DRIVE = re.compile(r"^[A-Za-z]:")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_DIR = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_PART = re.compile(r"^(events|checkpoints)/([a-z0-9][a-z0-9._-]*)/part-([0-9]{4})\.jsonl$")
_SCHEMA = re.compile(r"^schema/[a-z0-9._-]+\.schema\.json$")
_ALLOWED_COMPRESSION = (zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED)
_JSONL_ROLES = ("events", "checkpoints", "index")


@dataclass(frozen=True)
class ReadLimits:
    """Reader ceilings against decompression bombs and entry floods (all in decompressed bytes
    except the counts). The defaults leave room for ZIP64-sized archives while still bounding
    any single allocation."""

    max_entries: int = 100_000
    max_entry_bytes: int = 1024**3
    max_total_bytes: int = 64 * 1024**3
    max_manifest_bytes: int = 16 * 1024**2
    max_line_bytes: int = 16 * 1024**2


@dataclass(frozen=True)
class IndexEntry:
    """One line of ``index/recorded.jsonl``: where an event lives and its recorded time."""

    id: str
    line: int
    part: str
    recorded_at: str | None


@dataclass(frozen=True)
class _Row:
    path: str
    role: str
    sha256: str
    bytes: int
    records: int | None
    sorted_by: str | None


def check_member_name(name: str) -> str:
    """Return ``name`` if it cannot escape an extraction root; raise :class:`UnsafePathError`."""
    if not name or "\\" in name or "\x00" in name or _DRIVE.match(name):
        raise UnsafePathError(f"unsafe archive member name {name!r}")
    if name.startswith("/"):
        raise UnsafePathError(f"absolute archive member name {name!r}")
    if any(segment in ("", ".", "..") for segment in name.split("/")):
        raise UnsafePathError(f"archive member name {name!r} has an empty, '.' or '..' segment")
    return name


def declared_entry_count(handle: IO[bytes]) -> int | None:
    """The entry total in the ZIP (or ZIP64) end record, without parsing the central
    directory; ``None`` when no end record is found (``zipfile`` will then refuse the file)."""
    handle.seek(0, os.SEEK_END)
    end = handle.tell()
    tail = min(end, 22 + 65535 + 20)
    handle.seek(end - tail)
    data = handle.read(tail)
    handle.seek(0)
    at = data.rfind(_EOCD)
    if at < 0 or len(data) - at < 22:
        return None
    total = struct.unpack_from("<H", data, at + 10)[0]
    if total == 0xFFFF and at >= 20 and data[at - 20 : at - 16] == _ZIP64_LOCATOR:
        offset = struct.unpack_from("<Q", data, at - 12)[0]
        handle.seek(offset)
        record = handle.read(56)
        handle.seek(0)
        if len(record) == 56 and record[:4] == _ZIP64_EOCD:
            total = struct.unpack_from("<Q", record, 32)[0]
    return total


def _parse_constant(token: str) -> Any:
    raise ValueError(f"non-finite number {token} is not JSON")


def _loads(data: bytes) -> Any:
    return json.loads(data.decode("utf-8"), parse_constant=_parse_constant)


class ArchiveReader:
    """An opened, structurally validated archive. Use as a context manager; ``manifest`` and
    ``events()`` are the (structural) ``PlanArchive`` shape downstream packages consume."""

    def __init__(
        self,
        source: str | os.PathLike[str] | IO[bytes],
        *,
        limits: ReadLimits | None = None,
        implemented_capabilities: Iterable[str] = KNOWN_CAPABILITIES,
    ) -> None:
        self.limits = limits or ReadLimits()
        limits = self.limits
        self._owned: IO[bytes] | None = None
        self._zip: zipfile.ZipFile | None = None
        try:
            if isinstance(source, (str, os.PathLike)):
                self._owned = open(source, "rb")  # noqa: SIM115 - closed in close()
                handle: IO[bytes] = self._owned
            else:
                handle = source
            self._open(handle, frozenset(implemented_capabilities))
        except BaseException:
            self.close()
            raise

    # -- opening ---------------------------------------------------------------------------

    def _open(self, handle: IO[bytes], implemented: frozenset[str]) -> None:
        limits = self.limits
        declared = declared_entry_count(handle)
        if declared is not None and declared > limits.max_entries:
            raise TooManyEntriesError(
                f"archive declares {declared} entries; limit {limits.max_entries}"
            )
        try:
            self._zip = zipfile.ZipFile(handle)
            infos = self._zip.infolist()
        except (zipfile.BadZipFile, EOFError, struct.error, ValueError, OverflowError) as exc:
            raise MalformedArchiveError(f"not a readable ZIP archive: {exc}") from exc
        if len(infos) > limits.max_entries:
            raise TooManyEntriesError(
                f"archive has {len(infos)} entries; limit {limits.max_entries}"
            )

        by_name: dict[str, zipfile.ZipInfo] = {}
        total = 0
        for info in infos:
            check_member_name(info.filename)
            if info.filename in by_name:
                raise UnsafePathError(f"duplicate archive member {info.filename!r}")
            by_name[info.filename] = info
            if info.flag_bits & 0x1:
                raise MalformedArchiveError(f"member {info.filename!r} is encrypted")
            if info.compress_type not in _ALLOWED_COMPRESSION:
                raise MalformedArchiveError(
                    f"member {info.filename!r} uses unsupported compression {info.compress_type}"
                )
            if info.file_size > limits.max_entry_bytes:
                raise EntryTooLargeError(
                    f"member {info.filename!r} declares {info.file_size} bytes; "
                    f"limit {limits.max_entry_bytes}"
                )
            total += info.file_size
            if total > limits.max_total_bytes:
                raise ArchiveTooLargeError(
                    f"archive members declare more than {limits.max_total_bytes} bytes in total"
                )
        self._infos = by_name

        self._check_fixed_members(infos)
        manifest_info = infos[1]
        if manifest_info.file_size > limits.max_manifest_bytes:
            raise EntryTooLargeError(
                f"{MANIFEST_MEMBER} declares {manifest_info.file_size} bytes; "
                f"limit {limits.max_manifest_bytes}"
            )
        manifest_data = self._read_small(manifest_info, limits.max_manifest_bytes)
        self.manifest_sha256 = hashlib.sha256(manifest_data).hexdigest()
        try:
            raw = _loads(manifest_data)
        except ValueError as exc:
            raise MalformedArchiveError(
                f"{MANIFEST_MEMBER} is not valid UTF-8 JSON: {exc}"
            ) from exc
        if isinstance(raw, Mapping) and isinstance(raw.get("capabilities"), Mapping):
            Capabilities.from_json(raw["capabilities"]).require(implemented)
        self.manifest = Manifest.from_json(raw)
        self._rows = self._check_manifest()
        self._check_members(infos)

    def _check_fixed_members(self, infos: list[zipfile.ZipInfo]) -> None:
        if not infos or infos[0].filename != MIMETYPE_MEMBER:
            where = "absent" if MIMETYPE_MEMBER not in self._infos else "not the first entry"
            raise MissingMimetypeError(f"{MIMETYPE_MEMBER!r} is {where}")
        first = infos[0]
        media = MEDIA_TYPE.encode("ascii")
        if first.compress_type != zipfile.ZIP_STORED or first.extra:
            raise InvalidMimetypeError(f"{MIMETYPE_MEMBER!r} must be STORED with no extra field")
        if first.file_size != len(media) or self._read_small(first, len(media)) != media:
            raise InvalidMimetypeError(f"{MIMETYPE_MEMBER!r} is not exactly {MEDIA_TYPE!r}")
        if len(infos) < 2 or infos[1].filename != MANIFEST_MEMBER:
            raise MalformedArchiveError(f"{MANIFEST_MEMBER!r} must be the second entry")

    def _read_small(self, info: zipfile.ZipInfo, limit: int) -> bytes:
        assert self._zip is not None
        try:
            with self._zip.open(info) as handle:
                data = handle.read(limit + 1)
        except (zipfile.BadZipFile, EOFError, zlib.error, NotImplementedError) as exc:
            raise CorruptMemberError(f"member {info.filename!r} is damaged: {exc}") from exc
        if len(data) > limit:
            raise EntryTooLargeError(f"member {info.filename!r} exceeds {limit} bytes")
        return data

    def _check_manifest(self) -> dict[str, _Row]:
        manifest = self.manifest
        dirs: dict[str, str] = {}
        ids: set[str] = set()
        for group in manifest.groups:
            if _DIR.fullmatch(group.dir) is None:
                raise MalformedArchiveError(
                    f"group dir {group.dir!r} is not a valid directory name"
                )
            if group.dir in dirs or group.id in ids:
                raise MalformedArchiveError(f"group {group.id!r} / dir {group.dir!r} is repeated")
            dirs[group.dir] = group.id
            ids.add(group.id)
        self._group_of_dir = dirs

        rows: dict[str, _Row] = {}
        for item in manifest.files:
            row = self._row(item)
            if row.path in rows:
                raise MalformedArchiveError(f"manifest lists {row.path!r} twice")
            rows[row.path] = row
        for required in SCHEMA_MEMBERS:
            if required not in rows or rows[required].role != "schema":
                raise MalformedArchiveError(
                    f"manifest does not list the required member {required!r}"
                )

        numbers: dict[tuple[str, str], list[int]] = {}
        for row in rows.values():
            match = _PART.fullmatch(row.path)
            if match is not None:
                if match[2] not in dirs:
                    raise MalformedArchiveError(f"{row.path!r} is in no declared group directory")
                numbers.setdefault((match[1], match[2]), []).append(int(match[3]))
        for (role, folder), found in numbers.items():
            if sorted(found) != list(range(len(found))):
                raise MalformedArchiveError(
                    f"{role}/{folder} parts are not numbered 0000.. without gaps"
                )

        declares = set(manifest.capabilities.required) | set(manifest.capabilities.optional)
        if ("recorded-time-monotone-load" in declares) != (INDEX_MEMBER in rows):
            raise MalformedArchiveError(
                "recorded-time-monotone-load is declared if and only if the index is present"
            )
        return rows

    def _row(self, item: Any) -> _Row:
        if not isinstance(item, Mapping):
            raise MalformedArchiveError(f"manifest file entry must be an object, got {item!r}")
        path, role = item.get("path"), item.get("role")
        sha, size = item.get("sha256"), item.get("bytes")
        records, sorted_by = item.get("records"), item.get("sorted_by")
        if not isinstance(path, str) or path in (MIMETYPE_MEMBER, MANIFEST_MEMBER):
            raise MalformedArchiveError(f"manifest file entry has a bad path {path!r}")
        check_member_name(path)
        if not isinstance(sha, str) or _SHA256.fullmatch(sha) is None:
            raise MalformedArchiveError(f"{path!r} has a bad sha256 {sha!r}")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise MalformedArchiveError(f"{path!r} has a bad byte count {size!r}")
        if role == "schema":
            ok = _SCHEMA.fullmatch(path) is not None
        elif role in ("events", "checkpoints"):
            match = _PART.fullmatch(path)
            ok = match is not None and match[1] == role and sorted_by == EVENTS_SORTED_BY
        elif role == "index":
            ok = path == INDEX_MEMBER and sorted_by == INDEX_SORTED_BY
        else:
            ok = False
        if not ok:
            raise MalformedArchiveError(f"manifest entry {path!r} does not fit role {role!r}")
        if role in _JSONL_ROLES and (
            isinstance(records, bool) or not isinstance(records, int) or records < 0
        ):
            raise MalformedArchiveError(f"{path!r} needs a record count, got {records!r}")
        if size > self.limits.max_entry_bytes:
            raise EntryTooLargeError(
                f"{path!r} is {size} bytes; limit {self.limits.max_entry_bytes}"
            )
        return _Row(path, role, sha, size, records if role in _JSONL_ROLES else None, sorted_by)

    def _check_members(self, infos: list[zipfile.ZipInfo]) -> None:
        listed = set(self._rows)
        present = {info.filename for info in infos[2:]}
        if present - listed:
            raise MalformedArchiveError(
                "archive members not in the manifest: " + ", ".join(sorted(present - listed))
            )
        if listed - present:
            raise MalformedArchiveError(
                "manifest members missing from the archive: " + ", ".join(sorted(listed - present))
            )
        for row in self._rows.values():
            declared = self._infos[row.path].file_size
            if declared != row.bytes:
                raise MemberSizeMismatchError(
                    f"{row.path!r}: archive holds {declared} bytes, manifest says {row.bytes}"
                )

    # -- reading ---------------------------------------------------------------------------

    def close(self) -> None:
        """Release the archive (and the file, when this reader opened it)."""
        if self._zip is not None:
            self._zip.close()
            self._zip = None
        if self._owned is not None:
            self._owned.close()
            self._owned = None

    def __enter__(self) -> ArchiveReader:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    @property
    def has_index(self) -> bool:
        """Whether ``index/recorded.jsonl`` ships (capability ``recorded-time-monotone-load``)."""
        return INDEX_MEMBER in self._rows

    def _stream(self, row: _Row) -> Iterator[bytes]:
        """Raw chunks of one member, bounded by the manifest's byte count."""
        if self._zip is None:
            raise ArchiveError("archive reader is closed")
        try:
            with self._zip.open(self._infos[row.path]) as handle:
                seen = 0
                while chunk := handle.read(_CHUNK):
                    seen += len(chunk)
                    if seen > row.bytes:
                        raise MemberSizeMismatchError(
                            f"{row.path!r} holds more than the {row.bytes} bytes the manifest lists"
                        )
                    yield chunk
        except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
            raise CorruptMemberError(f"member {row.path!r} is damaged: {exc}") from exc

    def _digest(self, row: _Row) -> None:
        """Hash ``row``'s member and compare size, SHA-256 and record count to the manifest."""
        digest, size, newlines, last = hashlib.sha256(), 0, 0, b"\n"
        for chunk in self._stream(row):
            digest.update(chunk)
            size += len(chunk)
            newlines += chunk.count(b"\n")
            last = chunk[-1:]
        if size != row.bytes:
            raise MemberSizeMismatchError(
                f"{row.path!r}: read {size} bytes, manifest says {row.bytes}"
            )
        if digest.hexdigest() != row.sha256:
            raise PartHashMismatchError(
                f"{row.path!r}: sha256 is {digest.hexdigest()}, the manifest says {row.sha256}"
            )
        if row.records is not None:
            if size and last != b"\n":
                raise PartFormatError(f"{row.path!r}: the last line is not LF-terminated")
            if newlines != row.records:
                raise RecordCountMismatchError(
                    f"{row.path!r}: {newlines} records, manifest says {row.records}"
                )

    def _lines(self, row: _Row) -> Iterator[tuple[int, bytes]]:
        """Verified lines of a JSONL member (verified in full before the first line)."""
        self._digest(row)
        if self._zip is None:
            raise ArchiveError("archive reader is closed")
        cap = self.limits.max_line_bytes
        try:
            with self._zip.open(self._infos[row.path]) as handle:
                number = 0
                while line := handle.readline(cap + 1):
                    number += 1
                    if len(line) > cap or not line.endswith(b"\n"):
                        raise PartFormatError(
                            f"{row.path}:{number}: line is over {cap} bytes or not LF-terminated"
                        )
                    yield number, line
        except (zipfile.BadZipFile, EOFError, zlib.error) as exc:
            raise CorruptMemberError(f"member {row.path!r} is damaged: {exc}") from exc

    def _part_rows(self, role: str, group_id: str | None) -> Iterator[tuple[str, _Row]]:
        """``(group id, row)`` of every part of ``role``, in group-id then part-number order."""
        selected = []
        for row in self._rows.values():
            match = _PART.fullmatch(row.path)
            if match is None or match[1] != role:
                continue
            owner = self._group_of_dir[match[2]]
            if group_id is None or owner == group_id:
                selected.append((owner, int(match[3]), row))
        for owner, _, row in sorted(selected, key=lambda item: item[:2]):
            yield owner, row

    def _parse(self, row: _Row, group_id: str, role: str) -> Iterator[BeadEvent]:
        previous = None
        for number, line in self._lines(row):
            where = f"{row.path}:{number}"
            try:
                event = BeadEvent.from_json(_loads(line))
            except (ValueError, TimestampError, EventFormatError) as exc:
                raise PartFormatError(f"{where}: {exc}") from exc
            if event.bead.group != group_id:
                raise PartFormatError(
                    f"{where}: event is for group {event.bead.group!r}, not {group_id!r}"
                )
            if (event.kind is EventKind.CHECKPOINT) != (role == "checkpoints"):
                raise PartFormatError(f"{where}: a {event.kind} event does not belong in {role}/")
            key = part_order(event)
            if previous is not None and key < previous:
                raise PartOrderError(f"{where}: events are not in part order")
            previous = key
            yield event

    def _role_events(self, role: str, group_id: str) -> Iterator[BeadEvent]:
        previous = None
        for owner, row in self._part_rows(role, group_id):
            for event in self._parse(row, owner, role):
                key = part_order(event)
                if previous is not None and key < previous:
                    raise PartOrderError(f"{row.path}: parts of {group_id!r} are not in part order")
                previous = key
                yield event

    def events(self, *, group: str | None = None, checkpoints: bool = True) -> Iterator[BeadEvent]:
        """Stream the archive's events in part order, group by group.

        Each part is verified (size, SHA-256, record count) before its first event is yielded,
        so a tampered part raises before any of its events leaves the reader. ``checkpoints``
        merges the ``checkpoints/`` parts in; ``group`` restricts to one durable group id.
        Part order within a group makes the concatenation globally sorted, so the stream can be
        merged or loaded without sorting the archive.
        """
        for declared in sorted(self.manifest.groups, key=lambda item: item.id):
            if group is not None and declared.id != group:
                continue
            streams = [self._role_events("events", declared.id)]
            if checkpoints:
                streams.append(self._role_events("checkpoints", declared.id))
            yield from heapq.merge(*streams, key=part_order)

    def index(self) -> Iterator[IndexEntry]:
        """Stream ``index/recorded.jsonl`` in ``(recorded_at, id)`` order (verified first)."""
        row = self._rows.get(INDEX_MEMBER)
        if row is None:
            raise ArchiveError("the archive ships no recorded-time index")
        previous = None
        for number, line in self._lines(row):
            where = f"{INDEX_MEMBER}:{number}"
            try:
                raw = _loads(line)
                entry = IndexEntry(raw["id"], raw["line"], raw["part"], raw["recorded_at"])
            except (ValueError, KeyError, TypeError) as exc:
                raise PartFormatError(f"{where}: bad index line: {exc}") from exc
            if (
                not isinstance(entry.line, int)
                or isinstance(entry.line, bool)
                or entry.line < 1
                or entry.part not in self._rows
            ):
                raise PartFormatError(f"{where}: index line names no part or line")
            try:
                key = (instant(entry.recorded_at), entry.id)
            except TimestampError as exc:
                raise PartFormatError(f"{where}: {exc}") from exc
            if previous is not None and key < previous:
                raise PartOrderError(f"{where}: index is not sorted by (recorded_at, id)")
            previous = key
            yield entry

    def verify(self, *, ids: bool = False) -> None:
        """Verify the whole archive: every member's size and SHA-256, every event parsed and in
        order, the manifest's record and per-kind counts, no duplicate event ids and an index
        that points at the right events. ``ids=True`` also recomputes every event id (events
        carrying ``redacted`` cannot be recomputed and are skipped). Holds the event ids in
        memory."""
        for row in self._rows.values():
            if row.role == "schema":  # JSONL members are digested as they are parsed
                self._digest(row)
        located: dict[tuple[str, int], BeadEvent] = {}
        kinds: dict[str, int] = {}
        beads = set()
        for role in ("events", "checkpoints"):
            for owner, row in self._part_rows(role, None):
                for number, event in enumerate(self._parse(row, owner, role), start=1):
                    located[(row.path, number)] = event
                    kinds[str(event.kind)] = kinds.get(str(event.kind), 0) + 1
                    beads.add(event.bead)
                    if ids and not event.redacted and event_id(event) != event.id:
                        raise EventIdMismatchError(
                            f"{row.path}:{number}: id does not match content"
                        )
        distinct = {event.id for event in located.values()}
        if len(distinct) != len(located):
            raise PartFormatError("the same event id appears more than once in the archive")
        expected = self.manifest.counts
        by_kind = {
            str(kind): count for kind, count in dict(expected.get("by_kind", {})).items() if count
        }
        if (
            expected.get("events") != len(located)
            or expected.get("beads") != len(beads)
            or by_kind != kinds
        ):
            raise RecordCountMismatchError("manifest counts differ from the archive's events")
        if self.has_index:
            entries = 0
            for entry in self.index():
                entries += 1
                event = located.get((entry.part, entry.line))
                if event is None or event.id != entry.id or event.recorded_at != entry.recorded_at:
                    raise IndexMismatchError(
                        f"index entry {entry.id} does not match {entry.part}:{entry.line}"
                    )
            if entries != len(located):
                raise IndexMismatchError(f"index has {entries} entries for {len(located)} events")


def read_archive(
    source: str | os.PathLike[str] | IO[bytes], **options: Any
) -> tuple[Manifest, tuple[BeadEvent, ...]]:
    """Open, fully verify and load a (small) archive: its manifest and every event in part
    order. Large archives should stream via :class:`ArchiveReader` instead."""
    with ArchiveReader(source, **options) as reader:
        reader.verify()
        return reader.manifest, tuple(reader.events())


__all__ = [
    "ArchiveReader",
    "IndexEntry",
    "ReadLimits",
    "check_member_name",
    "declared_entry_count",
    "read_archive",
]
