"""The evidence IR: source records as :class:`Artifact` and the ``bdlg.evidence/1`` JSONL corpus.

Phase A of the backfill pipeline parses each source (git, docs, planning, a tracker snapshot)
into **artifacts**: one record per thing a source states, with its un-interpreted references
(:class:`Ref`). Readers do no resolution: a bead-looking token is a ``Ref(kind="bead-token")``
whether it is local, foreign or noise, so every source shares one resolver downstream.

Artifacts are typed by :class:`Category`, a small closed set of shapes that sources with the
same meaning share (``change``, ``review``, ``tracker-item``, ``design-record``, ``plan-item``,
``release-marker``). Each category declares the normalized ``attrs`` it requires
(:data:`CATEGORY_SHAPES`); an artifact is validated against its shape when it is built, so an
invalid record is never representable. Collation and the BeadLog projection are written per
category, never per source: a new source is only a reader that emits an existing category.

The corpus is JSONL, schema ``bdlg.evidence/1`` (JSON Schema
``schemas/bdlg-evidence-v1.schema.json``, copied to ``docs/schemas/beadlog/``). It is **not**
part of the BeadLog format: it is an intermediate artifact, like a compiler IR. Layout:

- one ``header`` line per reader (``name``, ``version``, ``options``, ``cursor``), sorted by
  name, then
- one ``artifact`` line per record, sorted by ``(key, reader, id)``.

Every line is the RFC 8785 canonical JSON of its object (``beadlog.canonical_json``), so the
same corpus always has the same bytes, and a record's ``id`` is ``sha256:`` + the SHA-256 of the
canonical JSON of the record without its ``id`` and ``reader_version`` (content-addressed,
stable across runs and across reader versions that produce the same output). The
reader refuses a non-canonical line, an out-of-order line or an id that does not match.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import date
from enum import StrEnum
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any

from beadlog import AdmissibleValueError, TimestampError, canonical_json, check_timestamp

EVIDENCE_SCHEMA = "bdlg.evidence/1"
SCHEMA_RESOURCE = "bdlg-evidence-v1.schema.json"

HEADER = "header"
ARTIFACT = "artifact"

# How a reader reached a record. Readers never judge: ``inferred`` belongs to collation.
OBSERVED = "observed"
DERIVED = "derived"
READER_PROVENANCES = (OBSERVED, DERIVED)

# The normalized vocabulary of a design record's declared status (the raw text stays in
# ``raw_attrs``).
DECLARED_STATUSES = (
    "proposed",
    "draft",
    "accepted",
    "delivered",
    "superseded",
    "rejected",
    "deprecated",
)

# The reference kinds readers emit today. Open: a reader may emit another kind.
REF_KINDS = ("bead-token", "sha", "pr", "issue", "path", "url", "tag")

_EMPTY: Mapping[str, Any] = MappingProxyType({})


class EvidenceFormatError(ValueError):
    """An artifact, header or corpus line outside ``bdlg.evidence/1``."""


class ArtifactShapeError(EvidenceFormatError):
    """An artifact whose ``attrs`` do not match its category's required shape.

    ``key`` and ``category`` name the record; ``attr`` the offending attribute.
    """

    def __init__(self, key: str, category: Category, attr: str, problem: str) -> None:
        self.key = key
        self.category = category
        self.attr = attr
        self.problem = problem
        super().__init__(f"{category.value} artifact {key!r}: attr {attr!r} {problem}")


class Category(StrEnum):
    """The closed set of record shapes. Collation and projection are written per category."""

    CHANGE = "change"
    REVIEW = "review"
    TRACKER_ITEM = "tracker-item"
    DESIGN_RECORD = "design-record"
    PLAN_ITEM = "plan-item"
    RELEASE_MARKER = "release-marker"

    @property
    def shape(self) -> CategoryShape:
        """This category's required-attrs shape."""
        return CATEGORY_SHAPES[self]


class AttrType(StrEnum):
    """The JSON type a required attr must have. ``?`` suffixed types also admit ``null``."""

    STRING = "string"
    OPTIONAL_STRING = "string?"
    STRINGS = "strings"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    OBJECT = "object"
    TIMESTAMP = "timestamp"
    OPTIONAL_TIMESTAMP = "timestamp?"
    OPTIONAL_STATUS = "status?"
    OPTIONAL_DATE = "date?"


_CALENDAR_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _is_calendar_date(value: Any) -> bool:
    """A real ``YYYY-MM-DD`` calendar date (day precision, no time, no zone)."""
    if not isinstance(value, str) or _CALENDAR_DATE.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


def _type_problem(kind: AttrType, value: Any) -> str | None:
    """``None`` when ``value`` has type ``kind``; otherwise what is wrong with it."""
    if kind.value.endswith("?") and value is None:
        return None
    if kind in (AttrType.STRING, AttrType.OPTIONAL_STRING):
        return None if isinstance(value, str) else "must be a string"
    if kind is AttrType.STRINGS:
        if isinstance(value, (list, tuple)) and all(isinstance(item, str) for item in value):
            return None
        return "must be an array of strings"
    if kind is AttrType.INTEGER:
        ok = isinstance(value, int) and not isinstance(value, bool)
        return None if ok else "must be an integer"
    if kind is AttrType.BOOLEAN:
        return None if isinstance(value, bool) else "must be a boolean"
    if kind is AttrType.OBJECT:
        return None if isinstance(value, Mapping) else "must be an object"
    if kind in (AttrType.TIMESTAMP, AttrType.OPTIONAL_TIMESTAMP):
        try:
            check_timestamp(value)
        except TimestampError:
            return "must be a canonical BeadLog timestamp (YYYY-MM-DDTHH:MM:SS[.f]Z)"
        return None
    if kind is AttrType.OPTIONAL_DATE:
        if _is_calendar_date(value):
            return None
        try:
            check_timestamp(value)
        except TimestampError:
            return "must be a calendar date YYYY-MM-DD, a canonical BeadLog timestamp or null"
        return None
    # OPTIONAL_STATUS
    if value in DECLARED_STATUSES:
        return None
    return f"must be one of {', '.join(DECLARED_STATUSES)} or null (raw text goes in raw_attrs)"


@dataclass(frozen=True)
class CategoryShape:
    """The normalized ``attrs`` a category requires: name to :class:`AttrType`.

    Attrs beyond ``required`` are allowed (readers may normalize more than the minimum).
    """

    category: Category
    required: Mapping[str, AttrType] = field(hash=False)

    def validate(self, attrs: Mapping[str, Any], key: str = "<unkeyed>") -> None:
        """Raise :class:`ArtifactShapeError` naming the first missing or mistyped attr."""
        for name, kind in self.required.items():
            if name not in attrs:
                raise ArtifactShapeError(key, self.category, name, f"is required ({kind.value})")
            problem = _type_problem(kind, attrs[name])
            if problem is not None:
                raise ArtifactShapeError(key, self.category, name, problem)


def _shape(category: Category, **required: AttrType) -> CategoryShape:
    return CategoryShape(category, MappingProxyType(required))


S, S0, SS = AttrType.STRING, AttrType.OPTIONAL_STRING, AttrType.STRINGS
T, T0 = AttrType.TIMESTAMP, AttrType.OPTIONAL_TIMESTAMP

CATEGORY_SHAPES: Mapping[Category, CategoryShape] = MappingProxyType(
    {
        Category.CHANGE: _shape(
            Category.CHANGE,
            sha=S,
            parents=SS,
            author=S,
            committed_at=T,
            cc_type=S0,
            cc_scope=S0,
            paths=SS,
            trailers=AttrType.OBJECT,
        ),
        Category.REVIEW: _shape(
            Category.REVIEW,
            number=AttrType.INTEGER,
            state=S,
            opened_at=T0,
            merged_at=T0,
            commits=SS,
            closes=SS,
        ),
        Category.TRACKER_ITEM: _shape(
            Category.TRACKER_ITEM,
            id=S,
            state=S,
            labels=SS,
            assignee=S0,
            opened_at=T0,
            closed_at=T0,
        ),
        Category.DESIGN_RECORD: _shape(
            Category.DESIGN_RECORD,
            declared_status=AttrType.OPTIONAL_STATUS,
            declared_date=AttrType.OPTIONAL_DATE,
            owner=S0,
            supersedes=SS,
        ),
        Category.PLAN_ITEM: _shape(
            Category.PLAN_ITEM,
            parent=S0,
            depends_on=SS,
            done_marker=AttrType.BOOLEAN,
            order=AttrType.INTEGER,
        ),
        Category.RELEASE_MARKER: _shape(
            Category.RELEASE_MARKER,
            version=S,
            released_at=T0,
            commit_range=S0,
        ),
    }
)
del S, S0, SS, T, T0


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _thaw(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw(item) for item in value]
    return value


def _admissible(value: Any, what: str) -> None:
    try:
        canonical_json(value)
    except AdmissibleValueError as error:
        raise EvidenceFormatError(f"{what}: {error}") from error


def _text(value: Any, what: str) -> str:
    if not isinstance(value, str) or not value:
        raise EvidenceFormatError(f"{what} must be a non-empty string, got {value!r}")
    return value


def _exact_members(raw: Any, members: tuple[str, ...], what: str) -> Mapping[str, Any]:
    if not isinstance(raw, Mapping):
        raise EvidenceFormatError(f"{what} must be a JSON object")
    missing = [name for name in members if name not in raw]
    extra = sorted(set(raw) - set(members))
    if missing or extra:
        raise EvidenceFormatError(f"{what}: missing members {missing}, unknown members {extra}")
    return raw


@dataclass(frozen=True)
class Ref:
    """An un-interpreted reference found in an artifact.

    ``kind`` is what it looks like (see :data:`REF_KINDS`), ``raw`` exactly as written
    (``abc-12x``, ``#143``, ``a1b2c3d``) and ``where`` the part of the record it was found in
    (``subject``, ``body``, ``frontmatter``, ``trailer``, ``html-meta`` ...).
    """

    kind: str
    raw: str
    where: str

    def __post_init__(self) -> None:
        _text(self.kind, "ref kind")
        _text(self.raw, "ref raw")
        _text(self.where, "ref where")

    def to_json(self) -> dict[str, str]:
        """The ref as a JSON object."""
        return {"kind": self.kind, "raw": self.raw, "where": self.where}

    @classmethod
    def from_json(cls, raw: Any) -> Ref:
        """Parse a ref object; unknown or missing members are refused."""
        raw = _exact_members(raw, ("kind", "raw", "where"), "ref")
        return cls(raw["kind"], raw["raw"], raw["where"])


_ARTIFACT_MEMBERS = (
    "type",
    "id",
    "category",
    "kind",
    "key",
    "title",
    "occurred_at",
    "attrs",
    "raw_attrs",
    "refs",
    "provenance",
    "reader",
    "reader_version",
)


@dataclass(frozen=True, kw_only=True)
class Artifact:
    """One record a source states, typed by :class:`Category`.

    ``kind`` is the source-specific record kind (``commit``, ``merge``, ``pr``, ``doc`` ...);
    ``key`` the stable source key (``git:commit/<sha>``, ``doc:docs/design/x.html``,
    ``gh:pr/123``); ``occurred_at`` the category's own clock as a canonical BeadLog timestamp
    (or ``None``); ``attrs`` the normalized attributes (validated against the category shape);
    ``raw_attrs`` the same as written, kept for conflict display; ``refs`` every reference found;
    ``provenance`` :data:`OBSERVED` or :data:`DERIVED`; ``reader`` / ``reader_version`` the
    reader that emitted it.

    Fields are keyword-only. Construction validates and deep-freezes the record; an invalid
    artifact raises :class:`EvidenceFormatError` (:class:`ArtifactShapeError` for a shape
    violation).
    """

    category: Category
    kind: str
    key: str
    title: str
    occurred_at: str | None
    attrs: Mapping[str, Any] = field(hash=False)
    raw_attrs: Mapping[str, Any] = field(default=_EMPTY, hash=False)
    refs: tuple[Ref, ...] = ()
    provenance: str = OBSERVED
    reader: str
    reader_version: str

    def __post_init__(self) -> None:
        try:
            category = Category(self.category)
        except ValueError:
            raise EvidenceFormatError(
                f"artifact {self.key!r}: unknown category {self.category!r}"
            ) from None
        object.__setattr__(self, "category", category)
        key = _text(self.key, "artifact key")
        _text(self.kind, f"artifact {key!r} kind")
        _text(self.reader, f"artifact {key!r} reader")
        _text(self.reader_version, f"artifact {key!r} reader_version")
        if not isinstance(self.title, str):
            raise EvidenceFormatError(f"artifact {key!r}: title must be a string")
        if self.occurred_at is not None:
            try:
                check_timestamp(self.occurred_at)
            except TimestampError as error:
                raise EvidenceFormatError(f"artifact {key!r} occurred_at: {error}") from None
        if self.provenance not in READER_PROVENANCES:
            raise EvidenceFormatError(
                f"artifact {key!r}: provenance {self.provenance!r} is not one of "
                f"{', '.join(READER_PROVENANCES)} (readers never infer)"
            )
        for name in ("attrs", "raw_attrs"):
            value = getattr(self, name)
            if not isinstance(value, Mapping):
                raise EvidenceFormatError(f"artifact {key!r}: {name} must be an object")
            _admissible(value, f"artifact {key!r} {name}")
            object.__setattr__(self, name, _freeze(value))
        refs = tuple(self.refs)
        if not all(isinstance(ref, Ref) for ref in refs):
            raise EvidenceFormatError(f"artifact {key!r}: refs must be Ref instances")
        object.__setattr__(self, "refs", refs)
        category.shape.validate(self.attrs, key)

    @property
    def id(self) -> str:
        """The content-addressed record id (see :func:`artifact_id`)."""
        return artifact_id(self)

    def body(self) -> dict[str, Any]:
        """The record's JSON object without ``id`` (the id hashes this minus ``reader_version``)."""
        return {
            "type": ARTIFACT,
            "category": self.category.value,
            "kind": self.kind,
            "key": self.key,
            "title": self.title,
            "occurred_at": self.occurred_at,
            "attrs": _thaw(self.attrs),
            "raw_attrs": _thaw(self.raw_attrs),
            "refs": [ref.to_json() for ref in self.refs],
            "provenance": self.provenance,
            "reader": self.reader,
            "reader_version": self.reader_version,
        }

    def to_json(self) -> dict[str, Any]:
        """The record's full JSON object, ``id`` included."""
        raw = self.body()
        raw["id"] = artifact_id(self)
        return raw

    @classmethod
    def from_json(cls, raw: Any) -> Artifact:
        """Parse a record object and verify its ``id``; any mismatch is refused."""
        raw = _exact_members(raw, _ARTIFACT_MEMBERS, "artifact")
        if raw["type"] != ARTIFACT:
            raise EvidenceFormatError(f"artifact type must be {ARTIFACT!r}, got {raw['type']!r}")
        refs = raw["refs"]
        if not isinstance(refs, list):
            raise EvidenceFormatError(f"artifact {raw['key']!r}: refs must be an array")
        artifact = cls(
            category=raw["category"],
            kind=raw["kind"],
            key=raw["key"],
            title=raw["title"],
            occurred_at=raw["occurred_at"],
            attrs=raw["attrs"],
            raw_attrs=raw["raw_attrs"],
            refs=tuple(Ref.from_json(ref) for ref in refs),
            provenance=raw["provenance"],
            reader=raw["reader"],
            reader_version=raw["reader_version"],
        )
        if raw["id"] != artifact.id:
            raise EvidenceFormatError(
                f"artifact {artifact.key!r}: id {raw['id']!r} does not match its content "
                f"({artifact.id})"
            )
        return artifact


def artifact_id(artifact: Artifact) -> str:
    """``sha256:`` + lowercase hex SHA-256 of the canonical JSON of :meth:`Artifact.body`
    without ``reader_version``.

    Ids are content addresses for diffing corpora across runs: a reader version bump that emits
    the same record keeps its id (the header still records the version). ``reader`` is identity
    material: the same key read by two readers is two records.
    """
    identity = artifact.body()
    del identity["reader_version"]
    digest = hashlib.sha256(canonical_json(identity).encode("utf-8")).hexdigest()
    return "sha256:" + digest


_HEADER_MEMBERS = ("type", "schema", "name", "version", "options", "cursor")


@dataclass(frozen=True)
class ReaderHeader:
    """One reader's run in a corpus: its ``name`` and ``version``, the ``options`` it ran with
    and its source ``cursor`` (any JSON value, ``None`` when the reader is not incremental)."""

    name: str
    version: str
    options: Mapping[str, Any] = field(default=_EMPTY, hash=False)
    cursor: Any = field(default=None, hash=False)

    def __post_init__(self) -> None:
        _text(self.name, "reader name")
        _text(self.version, f"reader {self.name!r} version")
        if not isinstance(self.options, Mapping):
            raise EvidenceFormatError(f"reader {self.name!r}: options must be an object")
        _admissible(self.options, f"reader {self.name!r} options")
        _admissible(self.cursor, f"reader {self.name!r} cursor")
        object.__setattr__(self, "options", _freeze(self.options))
        object.__setattr__(self, "cursor", _freeze(self.cursor))

    def to_json(self) -> dict[str, Any]:
        """The header line's JSON object."""
        return {
            "type": HEADER,
            "schema": EVIDENCE_SCHEMA,
            "name": self.name,
            "version": self.version,
            "options": _thaw(self.options),
            "cursor": _thaw(self.cursor),
        }

    @classmethod
    def from_json(cls, raw: Any) -> ReaderHeader:
        """Parse a header object; a schema other than ``bdlg.evidence/1`` is refused."""
        raw = _exact_members(raw, _HEADER_MEMBERS, "header")
        if raw["type"] != HEADER:
            raise EvidenceFormatError(f"header type must be {HEADER!r}, got {raw['type']!r}")
        if raw["schema"] != EVIDENCE_SCHEMA:
            raise EvidenceFormatError(f"unsupported evidence schema {raw['schema']!r}")
        return cls(raw["name"], raw["version"], raw["options"], raw["cursor"])


def _record_order(artifact: Artifact) -> tuple[str, str, str]:
    return (artifact.key, artifact.reader, artifact.id)


@dataclass(frozen=True)
class EvidenceCorpus:
    """A ``bdlg.evidence/1`` corpus: reader headers and their artifacts, in canonical order.

    Construction sorts headers by name and artifacts by ``(key, reader, id)`` and checks that
    reader names are unique, every artifact's reader has a header of the same version, and no
    record appears twice.
    """

    headers: tuple[ReaderHeader, ...]
    artifacts: tuple[Artifact, ...]

    def __post_init__(self) -> None:
        headers = tuple(sorted(self.headers, key=lambda header: header.name))
        versions: dict[str, str] = {}
        for header in headers:
            if header.name in versions:
                raise EvidenceFormatError(f"reader {header.name!r} has two headers")
            versions[header.name] = header.version
        artifacts = tuple(sorted(self.artifacts, key=_record_order))
        seen: set[str] = set()
        for artifact in artifacts:
            version = versions.get(artifact.reader)
            if version is None:
                raise EvidenceFormatError(
                    f"artifact {artifact.key!r}: reader {artifact.reader!r} has no header"
                )
            if version != artifact.reader_version:
                raise EvidenceFormatError(
                    f"artifact {artifact.key!r}: reader_version {artifact.reader_version!r} "
                    f"differs from header version {version!r}"
                )
            if artifact.id in seen:
                raise EvidenceFormatError(f"artifact {artifact.key!r} appears twice")
            seen.add(artifact.id)
        object.__setattr__(self, "headers", headers)
        object.__setattr__(self, "artifacts", artifacts)

    def by_category(self, category: Category | str) -> tuple[Artifact, ...]:
        """The artifacts of one category, in corpus order."""
        wanted = Category(category)
        return tuple(artifact for artifact in self.artifacts if artifact.category is wanted)


def _line(value: Mapping[str, Any]) -> bytes:
    return canonical_json(value).encode("utf-8") + b"\n"


def dumps_evidence(corpus: EvidenceCorpus) -> bytes:
    """The corpus as ``bdlg.evidence/1`` JSONL bytes (the same corpus, the same bytes)."""
    lines = [_line(header.to_json()) for header in corpus.headers]
    lines.extend(_line(artifact.to_json()) for artifact in corpus.artifacts)
    return b"".join(lines)


def loads_evidence(data: bytes) -> EvidenceCorpus:
    """Parse ``bdlg.evidence/1`` JSONL, refusing anything :func:`dumps_evidence` would not write.

    Every line must be canonical JSON ending in ``\\n``, headers must precede artifacts, both in
    canonical order, and every record id must match its content.
    """
    if data and not data.endswith(b"\n"):
        raise EvidenceFormatError("evidence corpus must end with a newline")
    headers: list[ReaderHeader] = []
    artifacts: list[Artifact] = []
    for number, line in enumerate(data.splitlines(keepends=False), start=1):
        try:
            raw = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise EvidenceFormatError(f"line {number}: not JSON ({error})") from None
        if not isinstance(raw, dict):
            raise EvidenceFormatError(f"line {number}: not a JSON object")
        try:
            canonical = canonical_json(raw).encode("utf-8")
        except AdmissibleValueError as error:
            raise EvidenceFormatError(f"line {number}: {error}") from None
        if canonical != line:
            raise EvidenceFormatError(f"line {number}: not canonical JSON (RFC 8785)")
        kind = raw.get("type")
        if kind == HEADER:
            if artifacts:
                raise EvidenceFormatError(f"line {number}: header after an artifact")
            header = ReaderHeader.from_json(raw)
            if headers and header.name <= headers[-1].name:
                raise EvidenceFormatError(f"line {number}: headers out of name order")
            headers.append(header)
        elif kind == ARTIFACT:
            artifact = Artifact.from_json(raw)
            if artifacts and _record_order(artifact) <= _record_order(artifacts[-1]):
                raise EvidenceFormatError(f"line {number}: artifacts out of (key, reader) order")
            artifacts.append(artifact)
        else:
            raise EvidenceFormatError(f"line {number}: unknown line type {kind!r}")
    return EvidenceCorpus(tuple(headers), tuple(artifacts))


def write_evidence(corpus: EvidenceCorpus, path: Path) -> Path:
    """Write the corpus to ``path`` (atomically, via a sibling temporary file)."""
    path = Path(path)
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(dumps_evidence(corpus))
    partial.replace(path)
    return path


def read_evidence(path: Path) -> EvidenceCorpus:
    """Read and verify a corpus written by :func:`write_evidence`."""
    return loads_evidence(Path(path).read_bytes())


def build_corpus(
    artifacts: Iterable[Artifact],
    headers: Iterable[ReaderHeader] = (),
) -> EvidenceCorpus:
    """A corpus of ``artifacts``, adding a default header (no options, no cursor) for every
    reader that ``headers`` does not already cover."""
    artifacts = tuple(artifacts)
    known = {header.name: header for header in headers}
    for artifact in artifacts:
        if artifact.reader not in known:
            known[artifact.reader] = ReaderHeader(artifact.reader, artifact.reader_version)
    return EvidenceCorpus(tuple(known.values()), artifacts)


def evidence_schema() -> dict[str, Any]:
    """The packaged JSON Schema of one ``bdlg.evidence/1`` line (header or artifact)."""
    text = (resources.files("beadlog_sources") / "schemas" / SCHEMA_RESOURCE).read_text(
        encoding="utf-8"
    )
    schema: dict[str, Any] = json.loads(text)
    return schema


__all__ = [
    "ARTIFACT",
    "CATEGORY_SHAPES",
    "DECLARED_STATUSES",
    "DERIVED",
    "EVIDENCE_SCHEMA",
    "HEADER",
    "OBSERVED",
    "READER_PROVENANCES",
    "REF_KINDS",
    "Artifact",
    "ArtifactShapeError",
    "AttrType",
    "Category",
    "CategoryShape",
    "EvidenceCorpus",
    "EvidenceFormatError",
    "ReaderHeader",
    "Ref",
    "artifact_id",
    "build_corpus",
    "dumps_evidence",
    "evidence_schema",
    "loads_evidence",
    "read_evidence",
    "write_evidence",
]
