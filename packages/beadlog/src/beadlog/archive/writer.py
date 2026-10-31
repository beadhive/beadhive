"""Write a reproducible ``.bdlg`` archive (format ADR section 1).

Same events and settings give a byte-identical archive: ``mimetype`` first and STORED, then
``MANIFEST.json``, then every other member in path order, each with modification time
1980-01-01 00:00:00, mode 0644, a fixed compression level and no directory entries. ZIP64
extensions appear only when a member needs them.

Memory: the events are merged, de-duplicated, erasure-filtered and sorted in memory (a
tombstone's content must not reach any part, and parts are sorted globally), but serialization,
hashing and compression stream: the manifest's digests, byte counts and record counts are
computed in one pass over the serialized lines and verified again while the members are written.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import zipfile
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from typing import IO, Any

from .._fold import checkpoint_key
from .._manifest import (
    FORMAT_VERSION,
    MEDIA_TYPE,
    Capabilities,
    Generator,
    Group,
    Manifest,
)
from .._merge import union_merge
from .._model import BeadEvent, EventKind
from .._order import ordering_key
from .._time import check_timestamp, instant
from .errors import ArchiveWriteError

DEFAULT_MAX_PART_BYTES = 8 * 1024 * 1024
MAX_PARTS_PER_GROUP = 10_000
MIMETYPE_MEMBER = "mimetype"
MANIFEST_MEMBER = "MANIFEST.json"
INDEX_MEMBER = "index/recorded.jsonl"
SCHEMA_MEMBERS = ("schema/event.schema.json", "schema/manifest.schema.json")
EVENTS_SORTED_BY = "bead,occurred_at,recorded_at,id"
INDEX_SORTED_BY = "recorded_at,id"

_FIXED_DATE_TIME = (1980, 1, 1, 0, 0, 0)
_REGULAR_FILE_MODE = 0o644
_UNIX = 3
_COMPRESS_LEVEL = 6
_GROUP_DIR = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
_SCHEMA_PATH = re.compile(r"^schema/[a-z0-9._-]+\.schema\.json$")
_CORRECTNESS = frozenset({"tombstones", "checkpoint-seeded", "redaction"})
_MONOTONE_LOAD = "recorded-time-monotone-load"


def packaged_schemas() -> dict[str, bytes]:
    """Byte copies of the v0.1 schemas this package embeds in every archive, by member path."""
    folder = resources.files("beadlog.archive") / "schemas"
    return {member: (folder / member.rsplit("/", 1)[1]).read_bytes() for member in SCHEMA_MEMBERS}


def serialize_event(event: BeadEvent) -> bytes:
    """One part line: the event as sorted-key, whitespace-free UTF-8 JSON plus LF."""
    try:
        text = json.dumps(
            event.to_json(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
        return text.encode("utf-8") + b"\n"
    except (UnicodeEncodeError, ValueError) as exc:
        raise ArchiveWriteError(f"event {event.id} cannot be written as UTF-8 JSON: {exc}") from exc


@dataclass
class _Member:
    """One non-fixed archive member: a path, how to produce its bytes, and its inventory row."""

    path: str
    role: str
    chunks: Any  # () -> Iterator[bytes]; deterministic
    sha256: str = ""
    bytes: int = 0
    records: int | None = None
    sorted_by: str | None = None
    events: tuple[BeadEvent, ...] = field(default=(), repr=False)

    def row(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "path": self.path,
            "role": self.role,
            "sha256": self.sha256,
            "bytes": self.bytes,
        }
        if self.records is not None:
            out["records"] = self.records
        if self.sorted_by is not None:
            out["sorted_by"] = self.sorted_by
        return out


def _hash_pass(member: _Member) -> None:
    digest, size = hashlib.sha256(), 0
    for chunk in member.chunks():
        digest.update(chunk)
        size += len(chunk)
    member.sha256, member.bytes = digest.hexdigest(), size


def _part_lines(events: Sequence[BeadEvent]) -> Any:
    def chunks() -> Iterator[bytes]:
        for event in events:
            yield serialize_event(event)

    return chunks


def _split(events: Sequence[BeadEvent], max_bytes: int) -> Iterator[Sequence[BeadEvent]]:
    """Consecutive runs of ``events`` whose serialized size stays within ``max_bytes`` (a single
    larger event still gets a part of its own)."""
    start, size = 0, 0
    for position, event in enumerate(events):
        length = len(serialize_event(event))
        if position > start and size + length > max_bytes:
            yield events[start:position]
            start, size = position, 0
        size += length
    if start < len(events):
        yield events[start:]


def _plan_group_parts(
    role: str, group: Group, events: Sequence[BeadEvent], max_bytes: int
) -> list[_Member]:
    folder = "events" if role == "events" else "checkpoints"
    parts = list(_split(events, max_bytes))
    if len(parts) > MAX_PARTS_PER_GROUP:
        raise ArchiveWriteError(
            f"group {group.id!r} needs {len(parts)} {role} parts; at most {MAX_PARTS_PER_GROUP} "
            "fit the part-NNNN naming (raise max_part_bytes)"
        )
    return [
        _Member(
            path=f"{folder}/{group.dir}/part-{number:04d}.jsonl",
            role=role,
            chunks=_part_lines(chunk),
            records=len(chunk),
            sorted_by=EVENTS_SORTED_BY,
            events=tuple(chunk),
        )
        for number, chunk in enumerate(parts)
    ]


def _index_member(parts: Sequence[_Member]) -> _Member:
    entries: list[tuple[Any, str, int, str, str | None]] = []
    for member in parts:
        for line, event in enumerate(member.events, start=1):
            entries.append(
                (instant(event.recorded_at), event.id, line, member.path, event.recorded_at)
            )
    entries.sort(key=lambda entry: (entry[0], entry[1]))

    def chunks() -> Iterator[bytes]:
        for _, ident, line, part, recorded_at in entries:
            row = {"id": ident, "line": line, "part": part, "recorded_at": recorded_at}
            yield (
                json.dumps(row, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
                + b"\n"
            )

    return _Member(INDEX_MEMBER, "index", chunks, records=len(entries), sorted_by=INDEX_SORTED_BY)


def _schema_member(path: str, content: bytes) -> _Member:
    return _Member(path, "schema", lambda: iter((content,)))


def seeding_checkpoints(events: Sequence[BeadEvent]) -> list[BeadEvent]:
    """Checkpoints that begin their bead's history (no change event at or before them); a fold
    needs them as a seed, so the archive must declare ``checkpoint-seeded``."""
    first: dict[Any, Any] = {}
    for event in events:
        if event.kind is not EventKind.CHECKPOINT:
            key = ordering_key(event)
            if event.bead not in first or key < first[event.bead]:
                first[event.bead] = key
    return [
        event
        for event in events
        if event.kind is EventKind.CHECKPOINT
        and (event.bead not in first or first[event.bead] > checkpoint_key(event))
    ]


def derive_capabilities(
    events: Sequence[BeadEvent],
    base: Capabilities,
    *,
    redaction_applied: bool,
    index: bool,
    plan: bool,
) -> Capabilities:
    """The capability declaration the schema's conditionals require for these events, merged
    with ``base`` (what the caller insists on). Correctness capabilities are never optional."""
    required, optional = set(base.required), set(base.optional)
    kinds = {event.kind for event in events}
    if EventKind.TOMBSTONE in kinds:
        required.add("tombstones")
    if redaction_applied or any(event.redacted for event in events):
        required.add("redaction")
    checkpoints = [event for event in events if event.kind is EventKind.CHECKPOINT]
    seeding = seeding_checkpoints(events)
    if seeding:
        required.add("checkpoint-seeded")
    if len(seeding) < len(checkpoints):
        optional.add("checkpoints")
    if index:
        optional.add(_MONOTONE_LOAD)
    if any(event.lamport is not None or event.prev is not None for event in events):
        optional.add("lamport")
    if plan:
        optional.add("plan")
    required |= optional & _CORRECTNESS
    optional -= required
    return Capabilities(tuple(sorted(required)), tuple(sorted(optional)))


def _zip_info(name: str, compress_type: int, size: int) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _FIXED_DATE_TIME)
    info.compress_type = compress_type
    info.external_attr = _REGULAR_FILE_MODE << 16
    info.create_system = _UNIX
    info.file_size = size  # lets zipfile choose ZIP64 exactly when the member needs it
    return info


def _put(
    archive: zipfile.ZipFile, name: str, chunks: Iterable[bytes], compress_type: int, size: int
) -> None:
    with archive.open(_zip_info(name, compress_type, size), "w") as handle:
        for chunk in chunks:
            handle.write(chunk)


def _check_groups(groups: Sequence[Group]) -> dict[str, Group]:
    if not groups:
        raise ArchiveWriteError("an archive declares at least one group")
    by_id: dict[str, Group] = {}
    dirs: set[str] = set()
    for group in groups:
        if group.id in by_id:
            raise ArchiveWriteError(f"group {group.id!r} is declared twice")
        if _GROUP_DIR.fullmatch(group.dir) is None:
            raise ArchiveWriteError(f"group dir {group.dir!r} does not match {_GROUP_DIR.pattern}")
        if group.dir in dirs:
            raise ArchiveWriteError(f"group dir {group.dir!r} is used by two groups")
        by_id[group.id] = group
        dirs.add(group.dir)
    return by_id


def build_archive(
    events: Iterable[BeadEvent],
    *,
    groups: Sequence[Group],
    generator: Generator,
    capabilities: Capabilities | None = None,
    redaction: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
    annotations: Mapping[str, Any] | None = None,
    description: str | None = None,
    created_at: str | None = None,
    max_part_bytes: int = DEFAULT_MAX_PART_BYTES,
    index: bool = False,
    schemas: Mapping[str, bytes] | None = None,
) -> tuple[Manifest, list[_Member]]:
    capabilities = capabilities or Capabilities()
    if max_part_bytes < 1:
        raise ArchiveWriteError("max_part_bytes must be positive")
    if created_at is not None:
        check_timestamp(created_at)
    by_id = _check_groups(groups)
    merged = union_merge([events])
    for event in merged:
        if event.bead.group not in by_id:
            raise ArchiveWriteError(
                f"event {event.id} names group {event.bead.group!r}, which is not declared"
            )

    redaction_block: dict[str, Any] = dict(redaction) if redaction else {"applied": False}
    applied = bool(redaction_block.get("applied"))
    if not applied and any(event.redacted for event in merged):
        raise ArchiveWriteError("events carry `redacted` but redaction.applied is not true")
    declared = set(capabilities.required) | set(capabilities.optional)
    if _MONOTONE_LOAD in declared and not index:
        raise ArchiveWriteError(f"{_MONOTONE_LOAD} is declared but index=False writes no index")

    members: list[_Member] = []
    for group in sorted(groups, key=lambda item: item.id):
        mine = [event for event in merged if event.bead.group == group.id]
        regular = [event for event in mine if event.kind is not EventKind.CHECKPOINT]
        checkpoints = [event for event in mine if event.kind is EventKind.CHECKPOINT]
        members += _plan_group_parts("events", group, regular, max_part_bytes)
        members += _plan_group_parts("checkpoints", group, checkpoints, max_part_bytes)
    if index:
        members.append(_index_member(members))
    chosen = packaged_schemas() if schemas is None else dict(schemas)
    for path, content in chosen.items():
        if _SCHEMA_PATH.fullmatch(path) is None:
            raise ArchiveWriteError(f"schema member {path!r} is not schema/<name>.schema.json")
        members.append(_schema_member(path, content))
    members.sort(key=lambda member: member.path)
    for member in members:
        _hash_pass(member)

    kinds = Counter(str(event.kind) for event in merged)
    manifest = Manifest(
        format_version=FORMAT_VERSION,
        generator=generator,
        groups=tuple(groups),
        capabilities=derive_capabilities(
            merged, capabilities, redaction_applied=applied, index=index, plan=plan is not None
        ),
        redaction=redaction_block,
        files=tuple(member.row() for member in members),
        counts={
            "events": len(merged),
            "beads": len({event.bead for event in merged}),
            "by_kind": {str(kind): kinds[str(kind)] for kind in EventKind if kinds[str(kind)]},
        },
        created_at=created_at,
        description=description,
        plan=plan,
        annotations=annotations,
    )
    return manifest, members


def manifest_bytes(manifest: Manifest) -> bytes:
    """The bytes of ``MANIFEST.json``: two-space indent, insertion order, trailing LF."""
    return (json.dumps(manifest.to_json(), indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write_to(handle: IO[bytes], manifest: Manifest, members: Sequence[_Member]) -> None:
    media = MEDIA_TYPE.encode("ascii")
    manifest_data = manifest_bytes(manifest)
    with zipfile.ZipFile(
        handle, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=_COMPRESS_LEVEL
    ) as archive:
        _put(archive, MIMETYPE_MEMBER, [media], zipfile.ZIP_STORED, len(media))
        _put(archive, MANIFEST_MEMBER, [manifest_data], zipfile.ZIP_DEFLATED, len(manifest_data))
        for member in members:
            digest = hashlib.sha256()

            def checked(member: _Member = member, digest: Any = digest) -> Iterator[bytes]:
                for chunk in member.chunks():
                    digest.update(chunk)
                    yield chunk

            _put(archive, member.path, checked(), zipfile.ZIP_DEFLATED, member.bytes)
            if digest.hexdigest() != member.sha256:  # pragma: no cover - serialization is pure
                raise ArchiveWriteError(f"{member.path} changed between hashing and writing")


def write_archive(
    destination: str | os.PathLike[str] | IO[bytes],
    events: Iterable[BeadEvent],
    *,
    groups: Sequence[Group],
    generator: Generator,
    capabilities: Capabilities | None = None,
    redaction: Mapping[str, Any] | None = None,
    plan: Mapping[str, Any] | None = None,
    annotations: Mapping[str, Any] | None = None,
    description: str | None = None,
    created_at: str | None = None,
    max_part_bytes: int = DEFAULT_MAX_PART_BYTES,
    index: bool = False,
    schemas: Mapping[str, bytes] | None = None,
) -> Manifest:
    """Write ``events`` as a ``.bdlg`` archive to a path or a seekable binary file; return the
    written :class:`~beadlog.Manifest`.

    Events are merged first (:func:`~beadlog.union_merge`: duplicates join, an id collision
    raises, erased content is dropped), then split per group into ``events/`` and
    ``checkpoints/`` parts of about ``max_part_bytes`` of UTF-8 JSONL each. Capabilities are
    derived from the content and merged with ``capabilities`` (what the caller insists on).
    ``index=True`` also writes ``index/recorded.jsonl`` and declares
    ``recorded-time-monotone-load`` (off by default). ``created_at``, when given, should be the
    maximum ``extracted_at``, never the wall clock. A path destination is written to a sibling
    temporary file and renamed into place, so a failed write leaves nothing behind.
    """
    manifest, members = build_archive(
        events,
        groups=groups,
        generator=generator,
        capabilities=capabilities,
        redaction=redaction,
        plan=plan,
        annotations=annotations,
        description=description,
        created_at=created_at,
        max_part_bytes=max_part_bytes,
        index=index,
        schemas=schemas,
    )
    if not isinstance(destination, (str, os.PathLike)):
        _write_to(destination, manifest, members)
        return manifest
    target = Path(destination)
    descriptor, temporary = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            _write_to(handle, manifest, members)
        os.chmod(temporary, 0o644)
        os.replace(temporary, target)
    except BaseException:
        Path(temporary).unlink(missing_ok=True)
        raise
    return manifest


__all__ = [
    "DEFAULT_MAX_PART_BYTES",
    "MAX_PARTS_PER_GROUP",
    "build_archive",
    "derive_capabilities",
    "manifest_bytes",
    "packaged_schemas",
    "serialize_event",
    "write_archive",
]
