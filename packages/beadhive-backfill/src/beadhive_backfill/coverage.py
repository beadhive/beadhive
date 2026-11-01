"""Link-coverage metrics and their cached state (``docs/BACKFILL.md`` "Coverage").

Three ratios say how well a hive's work is linked (``docs/design/backfill-platform-adr.md``
"Context" baselines), computed as a pure domain function over three injected reads: the hive
as events, the commit list and the doc set.

(i) ``closed_beads_with_commits``
    Closed (or retired) beads of a *code-bearing* type that record at least one commit in
    ``metadata.git.commits``. Code-bearing types are configurable
    (:attr:`CoverageSettings.code_types`); the infrastructure types in :data:`EXCLUDED_TYPES`
    (``event``, ``gate``, ``molecule``) are never code-bearing, so they leave the denominator
    whatever the configuration says.
(ii) ``commits_resolving_to_bead``
    Commits whose message (subject and body) names a bead of the hive. The matcher is the neutral
    two-step one of the ``git-commits`` strategy: a loose namespace-anchored candidate, kept only
    when it resolves in the hive's own id set.
(iii) ``docs_linked_to_bead``
    Docs under the configured roots that some bead carries as its ``external_ref`` (the stored
    link; a ``Beads:`` line or an add commit naming a bead is a proposal until applied).

A ratio over an empty denominator is ``None``, never 0 or 1.

**Cached state.** :class:`CoverageReport` is also the cache: versioned (:data:`CACHE_VERSION`), with
``computed_at``, an ``inputs_hash`` over exactly what the ratios depend on, the ratios and the
top unlinked examples. :func:`write_coverage_cache` is the one writer (atomic replace); every
caller that changes the inputs (``coverage --refresh``, an applied pass, an incremental run)
rewrites the cache through it. :func:`check_cache` never serves a cache silently: a cache whose
``inputs_hash`` differs from the current inputs is :attr:`CacheState.STALE`, and an unreadable or
unsupported one is :attr:`CacheState.INVALID`. A reader with no inputs at hand (a cheap advisory
check) gets :attr:`CacheState.UNVERIFIED`.

Library rules: no ``beadhive`` import, no clock or I/O except the explicit cache file functions;
the readers and the clock are injected.
"""

from __future__ import annotations

import hashlib
import json
import os
import posixpath
import tempfile
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from importlib import resources
from pathlib import Path
from typing import Any, Protocol

from beadlog import BeadEvent, BeadState, fold
from beadlog_sources import (
    candidate_pattern,
    derive_namespaces,
    extract_candidates,
    parse_linkage,
    resolve_candidates,
)

from ._errors import BackfillError

CACHE_SCHEMA = "beadhive.backfill.coverage"
CACHE_VERSION = 1
SCHEMA_ID = "urn:beadhive:backfill:coverage:1"
# The bd metadata key as the BeadLog field name the bd reader folds it to.
COMMITS_FIELD = "metadata.git.commits"
EXTERNAL_REF_FIELD = "external_ref"
CLOSED_PHASES = frozenset({"closed", "retired"})
DEFAULT_CODE_TYPES = ("bug", "chore", "feature", "task")
DEFAULT_DOC_ROOTS = ("docs",)
DEFAULT_EXAMPLE_LIMIT = 5
# Infrastructure records are never code-bearing, whatever ``code_types`` says.
EXCLUDED_TYPES = frozenset({"event", "gate", "molecule"})
RATIO_BEADS = "closed_beads_with_commits"
RATIO_COMMITS = "commits_resolving_to_bead"
RATIO_DOCS = "docs_linked_to_bead"
RATIOS = (RATIO_BEADS, RATIO_COMMITS, RATIO_DOCS)


class CoverageError(BackfillError):
    """The coverage inputs or cache cannot be used."""


class CommitRecord(Protocol):
    """What the coverage matcher reads of a commit (``beadlog_sources.commits.Commit`` fits)."""

    @property
    def sha(self) -> str: ...

    @property
    def subject(self) -> str: ...

    @property
    def message(self) -> str: ...


class CoverageReaders(Protocol):
    """The three bulk reads a measurement needs; each is called exactly once per measurement."""

    def beads(self) -> Iterable[BeadEvent]:
        """The hive as events (a :class:`~beadhive_backfill.StoreReader` export)."""
        ...

    def commits(self) -> Sequence[CommitRecord]:
        """The commits of the hive's integration history, newest first."""
        ...

    def docs(self) -> Iterable[str]:
        """Repo-relative posix paths of the docs under the configured roots."""
        ...


@dataclass(frozen=True)
class CoverageSettings:
    """What is measured: the code-bearing bead types, the doc roots, how many examples to keep."""

    code_types: tuple[str, ...] = DEFAULT_CODE_TYPES
    doc_roots: tuple[str, ...] = DEFAULT_DOC_ROOTS
    example_limit: int = DEFAULT_EXAMPLE_LIMIT

    def __post_init__(self) -> None:
        if self.example_limit < 0:
            raise CoverageError("example_limit must not be negative")

    @property
    def counted_types(self) -> frozenset[str]:
        """The types counted by (i): ``code_types`` minus :data:`EXCLUDED_TYPES`."""
        return frozenset(t.lower() for t in self.code_types) - EXCLUDED_TYPES

    def to_json(self) -> dict[str, Any]:
        return {
            "code_types": sorted({t.lower() for t in self.code_types}),
            "doc_roots": sorted(set(self.doc_roots)),
        }


@dataclass(frozen=True)
class Ratio:
    """``linked`` of ``total``; :attr:`value` is ``None`` when ``total`` is zero."""

    linked: int
    total: int

    def __post_init__(self) -> None:
        if not 0 <= self.linked <= self.total:
            raise CoverageError(f"ratio {self.linked}/{self.total} is not within 0..total")

    @property
    def value(self) -> float | None:
        return None if self.total == 0 else self.linked / self.total

    def to_json(self) -> dict[str, Any]:
        value = self.value
        return {
            "linked": self.linked,
            "total": self.total,
            "ratio": None if value is None else round(value, 6),
        }

    @classmethod
    def from_json(cls, data: Any) -> Ratio:
        try:
            return cls(int(data["linked"]), int(data["total"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise CoverageError(f"malformed ratio: {data!r}") from exc


@dataclass(frozen=True)
class Examples:
    """The top unlinked items per ratio (at most ``example_limit`` each, deterministic order)."""

    beads: tuple[Mapping[str, str], ...] = field(default=(), hash=False)
    commits: tuple[Mapping[str, str], ...] = field(default=(), hash=False)
    docs: tuple[Mapping[str, str], ...] = field(default=(), hash=False)

    def to_json(self) -> dict[str, Any]:
        return {
            RATIO_BEADS: [dict(item) for item in self.beads],
            RATIO_COMMITS: [dict(item) for item in self.commits],
            RATIO_DOCS: [dict(item) for item in self.docs],
        }

    @classmethod
    def from_json(cls, data: Any) -> Examples:
        try:
            return cls(
                tuple(dict(item) for item in data[RATIO_BEADS]),
                tuple(dict(item) for item in data[RATIO_COMMITS]),
                tuple(dict(item) for item in data[RATIO_DOCS]),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise CoverageError(f"malformed examples: {data!r}") from exc


@dataclass(frozen=True)
class CoverageReport:
    """The three ratios, the examples and the provenance: the cache document."""

    computed_at: str
    inputs_hash: str
    group: str | None
    settings: CoverageSettings
    beads: Ratio
    commits: Ratio
    docs: Ratio
    examples: Examples = field(default_factory=Examples, hash=False)

    @property
    def ratios(self) -> dict[str, Ratio]:
        return {RATIO_BEADS: self.beads, RATIO_COMMITS: self.commits, RATIO_DOCS: self.docs}

    def to_json(self) -> dict[str, Any]:
        """The versioned cache object (``cache`` in ``coverage-v1.schema.json``)."""
        return {
            "schema": CACHE_SCHEMA,
            "version": CACHE_VERSION,
            "computed_at": self.computed_at,
            "inputs_hash": self.inputs_hash,
            "group": self.group,
            "settings": self.settings.to_json(),
            "ratios": {name: ratio.to_json() for name, ratio in self.ratios.items()},
            "examples": self.examples.to_json(),
        }

    @classmethod
    def from_json(cls, data: Any) -> CoverageReport:
        """Parse a cache object; raise :class:`CoverageError` unless it is a supported version."""
        if not isinstance(data, Mapping) or data.get("schema") != CACHE_SCHEMA:
            raise CoverageError("not a coverage cache")
        if data.get("version") != CACHE_VERSION:
            raise CoverageError(
                f"unsupported coverage cache version {data.get('version')!r} "
                f"(this build reads {CACHE_VERSION})"
            )
        try:
            ratios = data["ratios"]
            settings = data["settings"]
            return cls(
                computed_at=str(data["computed_at"]),
                inputs_hash=str(data["inputs_hash"]),
                group=None if data.get("group") is None else str(data["group"]),
                settings=CoverageSettings(
                    tuple(settings["code_types"]), tuple(settings["doc_roots"])
                ),
                beads=Ratio.from_json(ratios[RATIO_BEADS]),
                commits=Ratio.from_json(ratios[RATIO_COMMITS]),
                docs=Ratio.from_json(ratios[RATIO_DOCS]),
                examples=Examples.from_json(data["examples"]),
            )
        except (KeyError, TypeError) as exc:
            raise CoverageError(f"malformed coverage cache: missing or invalid {exc}") from exc


def coverage_schema() -> dict[str, Any]:
    """The JSON Schema of ``bh backfill coverage --json`` (``$defs/cache`` is the cache file)."""
    text = (resources.files("beadhive_backfill") / "schemas" / "coverage-v1.schema.json").read_text(
        encoding="utf-8"
    )
    return dict(json.loads(text))


# -- the pure domain -------------------------------------------------------------------------


def normalize_doc_ref(ref: str) -> str:
    """A doc path or ``external_ref`` in one comparable form: no fragment, no leading ``./``."""
    path = ref.strip().split("#", 1)[0]
    if not path:
        return ""
    return posixpath.normpath(path)


def _live(states: Mapping[Any, BeadState], group: str | None) -> dict[str, BeadState]:
    """Lowercase id -> state of the existing, un-erased beads (of ``group``, when given)."""
    return {
        ref.id.lower(): state
        for ref, state in states.items()
        if (group is None or ref.group == group)
        and state.exists
        and not state.deleted
        and not state.erased
    }


def _title(state: BeadState) -> str:
    title = state.fields.get("title")
    return title if isinstance(title, str) else ""


def _recorded_commits(state: BeadState) -> tuple[str, ...]:
    shas = parse_linkage(state.fields.get(COMMITS_FIELD))
    return tuple(shas) if shas else ()


def _external_ref(state: BeadState) -> str:
    ref = state.fields.get(EXTERNAL_REF_FIELD)
    return normalize_doc_ref(ref) if isinstance(ref, str) else ""


def inputs_hash(
    live: Mapping[str, BeadState],
    commits: Sequence[CommitRecord],
    docs: Iterable[str],
    settings: CoverageSettings,
    group: str | None = None,
) -> str:
    """``sha256:`` over exactly what the ratios depend on: the settings, per bead its type,
    closedness, recorded commits and doc link, per commit its sha and message digest, and the doc
    paths. Order of the inputs never matters."""
    payload = {
        "version": CACHE_VERSION,
        "group": group,
        "settings": settings.to_json(),
        "beads": [
            [
                ident,
                (state.type or "").lower(),
                state.phase in CLOSED_PHASES,
                list(_recorded_commits(state)),
                _external_ref(state),
            ]
            for ident, state in sorted(live.items())
        ],
        "commits": sorted(
            [commit.sha, hashlib.sha256(commit.message.encode("utf-8")).hexdigest()]
            for commit in commits
        ),
        "docs": sorted({normalize_doc_ref(doc) for doc in docs}),
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return "sha256:" + hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _bead_examples(
    unlinked: Sequence[str], live: Mapping[str, BeadState], limit: int
) -> tuple[Mapping[str, str], ...]:
    """Most recently closed first (``closed_at`` when the bead has one), then by id."""
    closed = {
        ident: live[ident].fields.get("closed_at")
        for ident in unlinked
        if isinstance(live[ident].fields.get("closed_at"), str)
    }
    ordered = sorted(unlinked)
    ordered = sorted(ordered, key=lambda ident: str(closed.get(ident, "")), reverse=True)
    return tuple({"id": ident, "title": _title(live[ident])} for ident in ordered[:limit])


def compute_coverage(
    events: Iterable[BeadEvent],
    commits: Sequence[CommitRecord],
    docs: Iterable[str],
    *,
    computed_at: str,
    settings: CoverageSettings | None = None,
    group: str | None = None,
) -> CoverageReport:
    """Measure the three ratios (module doc). ``group`` narrows the fold to one bead group."""
    settings = settings or CoverageSettings()
    live = _live(fold(events), group)
    doc_paths = sorted({path for doc in docs if (path := normalize_doc_ref(doc))})

    counted = settings.counted_types
    closed_code = sorted(
        ident
        for ident, state in live.items()
        if (state.type or "").lower() in counted and state.phase in CLOSED_PHASES
    )
    without_commits = [ident for ident in closed_code if not _recorded_commits(live[ident])]

    resolved = 0
    unresolved: list[CommitRecord] = []
    ids = frozenset(live)
    namespaces = derive_namespaces(ids)
    pattern = candidate_pattern(namespaces) if namespaces else None
    for commit in commits:
        hit = False
        if pattern is not None:
            found, _dropped = resolve_candidates(extract_candidates(commit.message, pattern), ids)
            hit = bool(found)
        if hit:
            resolved += 1
        else:
            unresolved.append(commit)

    linked_docs = {ref for state in live.values() if (ref := _external_ref(state))}
    unlinked_docs = [path for path in doc_paths if path not in linked_docs]

    limit = settings.example_limit
    return CoverageReport(
        computed_at=computed_at,
        inputs_hash=inputs_hash(live, commits, doc_paths, settings, group),
        group=group,
        settings=settings,
        beads=Ratio(len(closed_code) - len(without_commits), len(closed_code)),
        commits=Ratio(resolved, len(commits)),
        docs=Ratio(len(doc_paths) - len(unlinked_docs), len(doc_paths)),
        examples=Examples(
            beads=_bead_examples(without_commits, live, limit),
            commits=tuple({"sha": c.sha, "subject": c.subject} for c in unresolved[:limit]),
            docs=tuple({"path": path} for path in unlinked_docs[:limit]),
        ),
    )


def measure(
    readers: CoverageReaders,
    settings: CoverageSettings | None = None,
    *,
    clock: Callable[[], str],
    group: str | None = None,
) -> CoverageReport:
    """Read each input once through ``readers`` and compute the report stamped by ``clock``."""
    return compute_coverage(
        readers.beads(),
        readers.commits(),
        readers.docs(),
        computed_at=clock(),
        settings=settings,
        group=group,
    )


# -- cached state ----------------------------------------------------------------------------


class CacheState(StrEnum):
    """How a cache read relates to the current inputs."""

    FRESH = "fresh"  # inputs hash equals the current one
    STALE = "stale"  # inputs hash differs: the cache describes older inputs
    MISSING = "missing"  # no cache file
    INVALID = "invalid"  # unreadable, corrupt or an unsupported version
    UNVERIFIED = "unverified"  # readable, but no current inputs hash to compare against


@dataclass(frozen=True)
class CacheCheck:
    """A cache read: its state, the report (absent when missing or invalid) and a detail."""

    state: CacheState
    report: CoverageReport | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        """Whether the ratios may be presented as current (never for stale or invalid)."""
        return self.state in (CacheState.FRESH, CacheState.UNVERIFIED)


def write_coverage_cache(path: Path | str, report: CoverageReport) -> Path:
    """Persist ``report`` as the cache at ``path`` (parents created, replaced atomically).

    The one cache writer: ``coverage --refresh``, every applied pass and the incremental run all
    end here, so the cache is never partially written."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(report.to_json(), sort_keys=True, indent=2) + "\n"
    handle, temp = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, target)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise
    return target


def read_coverage_cache(path: Path | str) -> CoverageReport | None:
    """The cached report at ``path``; ``None`` when the file does not exist. Raises
    :class:`CoverageError` when it exists but cannot be used."""
    target = Path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CoverageError(f"cannot read {target}: {exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError as exc:
        raise CoverageError(f"{target} is not valid JSON: {exc}") from exc
    return CoverageReport.from_json(data)


def check_cache(path: Path | str, current_inputs_hash: str | None = None) -> CacheCheck:
    """Read the cache and relate it to the current inputs (:class:`CacheState`)."""
    try:
        report = read_coverage_cache(path)
    except CoverageError as exc:
        return CacheCheck(CacheState.INVALID, None, str(exc))
    if report is None:
        return CacheCheck(CacheState.MISSING, None, f"no coverage cache at {path}")
    if current_inputs_hash is None:
        return CacheCheck(CacheState.UNVERIFIED, report, "inputs not compared")
    if report.inputs_hash != current_inputs_hash:
        return CacheCheck(
            CacheState.STALE,
            report,
            f"inputs changed since {report.computed_at} "
            f"(cache {report.inputs_hash[:19]}, current {current_inputs_hash[:19]})",
        )
    return CacheCheck(CacheState.FRESH, report)


__all__ = [
    "CACHE_SCHEMA",
    "CACHE_VERSION",
    "CLOSED_PHASES",
    "COMMITS_FIELD",
    "DEFAULT_CODE_TYPES",
    "DEFAULT_DOC_ROOTS",
    "DEFAULT_EXAMPLE_LIMIT",
    "EXCLUDED_TYPES",
    "EXTERNAL_REF_FIELD",
    "RATIOS",
    "RATIO_BEADS",
    "RATIO_COMMITS",
    "RATIO_DOCS",
    "SCHEMA_ID",
    "CacheCheck",
    "CacheState",
    "CommitRecord",
    "CoverageError",
    "CoverageReaders",
    "CoverageReport",
    "CoverageSettings",
    "Examples",
    "Ratio",
    "check_cache",
    "compute_coverage",
    "coverage_schema",
    "inputs_hash",
    "measure",
    "normalize_doc_ref",
    "read_coverage_cache",
    "write_coverage_cache",
]
