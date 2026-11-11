"""Phase B of the backfill pipeline: collate an evidence corpus (``bdlg.collation/1``).

One engine over **evidence corpus + baseline BeadLog + LedgerDirectory**, pure and
deterministic: no git, no store, no network, every artifact read once. This module is steps
B1 and B2 of ``docs/design/backfill-history-pipeline-adr.md``; later steps (B3 cluster, B4 attach
docs, B5 identify, B6 status/conflicts) extend :class:`Collation` through the members reserved in
:data:`EXTENSION_MEMBERS`.

**B1, :func:`build_indexes`.** One pass over the corpus builds :class:`Indexes`, all values
artifact keys (so they serialize and compare without holding records):

- ``by_key``: key to the artifacts with that key (one per reader);
- ``by_sha``: full sha to its ``change`` key (short shas resolve by prefix, see below);
- ``merge_children``: a merge commit's key to the keys of the commits its second (and later)
  parents bring in, i.e. everything reachable from those parents that is not on a first-parent
  mainline (nested merges and their branches included);
- ``tag_range``: a release marker's key to the change keys in its ``commit_range``
  (``<from>..<to>`` or ``<to>``, each side a sha; reachable from ``to`` and not from ``from``);
- ``pr_commits`` / ``pr_of_commit``: a review's key to its change keys, and the reverse;
- ``path_changes``: a path to the change keys that touched it, oldest first;
- ``doc_add``: a ``doc:<path>`` design record's key to the oldest change touching its path.

Changes order by ``(committed_at, sha)``; a missing parent is simply not walked.

**B2, :func:`resolve_refs`.** Every :class:`~beadlog_sources.evidence.Ref` of every artifact gets
one :class:`ResolvedRef`:

- ``bead-token``: the baseline's own id set is the local ledger (``local`` names it), then the
  :class:`~beadlog_sources.ledger.LedgerDirectory`, via :func:`beadlog_sources.ledger.resolve`:
  ``local`` / ``foreign`` (with the owning ``ledger``) / ``ambiguous`` / ``unresolved``. Exact
  id membership only; a prefix is never a ledger (bh-ear6v).
- ``sha`` / ``pr`` / ``issue`` / ``path`` / ``tag``: resolved through the indexes to
  ``artifact`` (``targets`` are keys) or ``unresolved``. A sha shorter than 40 characters
  resolves by prefix when it has at least :data:`MIN_SHA_PREFIX` hex digits, and is
  ``ambiguous`` when several shas share it. A path resolves to its ``doc:<path>`` artifact, else
  the changes that touched it.
- any other kind (``url`` ...) is ``unresolved``: a later step may interpret it. A raw value that
  is itself an artifact key resolves to that artifact whatever its kind.

:func:`collate` returns a :class:`Collation`, serialized as JSON ``bdlg.collation/1`` (schema
``schemas/bdlg-collation-v1.schema.json``) with canonical bytes: the same inputs, the same bytes.
"""

from __future__ import annotations

import hashlib
import json
import re
from bisect import bisect_left
from collections import defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path
from types import MappingProxyType
from typing import Any

from beadlog import BeadEvent, canonical_json, fold

from .evidence import Artifact, Category, EvidenceCorpus, Ref, dumps_evidence
from .ledger import (
    AMBIGUOUS,
    BEAD_TOKEN,
    FOREIGN,
    LOCAL,
    UNRESOLVED,
    LedgerDirectory,
    resolve,
)
from .reconcile import corpus_hash

COLLATION_SCHEMA = "bdlg.collation/1"
SCHEMA_RESOURCE = "bdlg-collation-v1.schema.json"

# The resolution outcomes of a ref: the ledger outcomes plus ``artifact``.
ARTIFACT = "artifact"
OUTCOMES = (LOCAL, FOREIGN, ARTIFACT, AMBIGUOUS, UNRESOLVED)

MIN_SHA_PREFIX = 7
# Members later collate steps fill; B1/B2 write them empty so the schema is stable.
EXTENSION_MEMBERS = ("units", "verdicts", "conflicts", "residual")

_HEX = re.compile(r"^[0-9a-f]+$")
_FULL_SHA = 40


class CollationFormatError(ValueError):
    """A collation outside ``bdlg.collation/1``."""


def _order(artifact: Artifact) -> tuple[str, str]:
    return (str(artifact.attrs["committed_at"]), str(artifact.attrs["sha"]).lower())


@dataclass(frozen=True)
class Indexes:
    """The B1 indexes (see the module doc). Values are artifact keys, deterministic order."""

    by_key: Mapping[str, tuple[Artifact, ...]] = field(hash=False, compare=False)
    by_sha: Mapping[str, str]
    merge_children: Mapping[str, tuple[str, ...]]
    tag_range: Mapping[str, tuple[str, ...]]
    pr_commits: Mapping[str, tuple[str, ...]]
    pr_of_commit: Mapping[str, tuple[str, ...]]
    path_changes: Mapping[str, tuple[str, ...]]
    doc_add: Mapping[str, str]
    pr_by_number: Mapping[int, tuple[str, ...]]
    tracker_by_id: Mapping[str, tuple[str, ...]]
    tag_by_version: Mapping[str, tuple[str, ...]]
    sorted_shas: tuple[str, ...] = ()

    def artifact(self, key: str) -> Artifact | None:
        """The first artifact with ``key`` (corpus order), or ``None``."""
        found = self.by_key.get(key)
        return found[0] if found else None

    def shas_with_prefix(self, prefix: str) -> tuple[str, ...]:
        """Every indexed full sha starting with ``prefix`` (sorted)."""
        start = bisect_left(self.sorted_shas, prefix)
        out = []
        for sha in self.sorted_shas[start:]:
            if not sha.startswith(prefix):
                break
            out.append(sha)
        return tuple(out)


def _commit_range(raw: Any) -> tuple[str | None, str | None]:
    if not isinstance(raw, str) or not raw.strip():
        return None, None
    if ".." in raw:
        low, _, high = raw.partition("..")
        low = low.strip().lstrip(".")
        high = high.strip().lstrip(".")
        return (low.lower() or None), (high.lower() or None)
    return None, raw.strip().lower()


def build_indexes(corpus: EvidenceCorpus) -> Indexes:
    """B1: build every index in one pass over the corpus (and the commit graph once per merge)."""
    by_key: dict[str, list[Artifact]] = defaultdict(list)
    for artifact in corpus.artifacts:
        by_key[artifact.key].append(artifact)

    changes: dict[str, Artifact] = {}  # full sha -> first change artifact
    for artifact in corpus.by_category(Category.CHANGE):
        changes.setdefault(str(artifact.attrs["sha"]).lower(), artifact)
    sorted_shas = tuple(sorted(changes))
    sha_key = {sha: artifact.key for sha, artifact in changes.items()}
    rank = {sha: i for i, sha in enumerate(sorted(changes, key=lambda s: _order(changes[s])))}

    def sha_of(token: str) -> str | None:
        token = token.lower()
        if token in changes:
            return token
        if _HEX.match(token) and len(token) >= MIN_SHA_PREFIX:
            start = bisect_left(sorted_shas, token)
            hits = [s for s in sorted_shas[start : start + 2] if s.startswith(token)]
            if len(hits) == 1:
                return hits[0]
        return None

    def keys_in_order(shas: Iterable[str]) -> tuple[str, ...]:
        return tuple(sha_key[s] for s in sorted(set(shas), key=rank.__getitem__))

    parents = {
        sha: tuple(p for p in (sha_of(str(x)) for x in art.attrs["parents"]) if p)
        for sha, art in changes.items()
    }

    # Paths and docs.
    touching: dict[str, list[str]] = defaultdict(list)
    for sha, art in changes.items():
        for path in art.attrs["paths"]:
            touching[_norm_path(str(path))].append(sha)
    path_changes = {path: keys_in_order(shas) for path, shas in touching.items()}
    doc_add: dict[str, str] = {}
    for art in corpus.by_category(Category.DESIGN_RECORD):
        if art.key.startswith("doc:") and art.key[4:] in path_changes:
            doc_add[art.key] = path_changes[art.key[4:]][0]

    # Reviews.
    pr_commits: dict[str, tuple[str, ...]] = {}
    pr_of: dict[str, list[str]] = defaultdict(list)
    pr_by_number: dict[int, list[str]] = defaultdict(list)
    for art in corpus.by_category(Category.REVIEW):
        pr_by_number[int(art.attrs["number"])].append(art.key)
        shas = [s for s in (sha_of(str(c)) for c in art.attrs["commits"]) if s]
        pr_commits[art.key] = keys_in_order(shas)
        for sha in set(shas):
            pr_of[sha_key[sha]].append(art.key)

    # Merge children: walk second+ parents, stopping on the first-parent mainline.
    has_child = {p for ps in parents.values() for p in ps}
    mainline: set[str] = set()
    for head in (s for s in changes if s not in has_child):
        cur: str | None = head
        while cur is not None and cur not in mainline:
            mainline.add(cur)
            cur = parents[cur][0] if parents[cur] else None
    merge_children: dict[str, tuple[str, ...]] = {}
    for sha, art in changes.items():
        if len(parents[sha]) < 2:
            continue
        seen: set[str] = set()
        stack = list(parents[sha][1:])
        while stack:
            cur = stack.pop()
            if cur in seen or cur in mainline:
                continue
            seen.add(cur)
            stack.extend(parents[cur])
        merge_children[art.key] = keys_in_order(seen)

    # Tags.
    def ancestors(tip: str) -> set[str]:
        out: set[str] = set()
        stack = [tip]
        while stack:
            cur = stack.pop()
            if cur not in out:
                out.add(cur)
                stack.extend(parents[cur])
        return out

    tag_range: dict[str, tuple[str, ...]] = {}
    tag_by_version: dict[str, list[str]] = defaultdict(list)
    tips: dict[str, set[str]] = {}
    for art in corpus.by_category(Category.RELEASE_MARKER):
        tag_by_version[str(art.attrs["version"])].append(art.key)
        low, high = _commit_range(art.attrs.get("commit_range"))
        high_sha = sha_of(high) if high else None
        if high_sha is None:
            tag_range[art.key] = ()
            continue
        below = ancestors(sha_of(low)) if low and sha_of(low) else set()
        tips[art.key] = ancestors(high_sha) - below
        tag_range[art.key] = keys_in_order(tips[art.key])

    tracker: dict[str, list[str]] = defaultdict(list)
    for art in corpus.by_category(Category.TRACKER_ITEM):
        tracker[str(art.attrs["id"])].append(art.key)

    return Indexes(
        by_key=MappingProxyType({k: tuple(v) for k, v in by_key.items()}),
        by_sha=MappingProxyType(sha_key),
        merge_children=MappingProxyType(merge_children),
        tag_range=MappingProxyType(tag_range),
        pr_commits=MappingProxyType(pr_commits),
        pr_of_commit=MappingProxyType({k: tuple(sorted(set(v))) for k, v in pr_of.items()}),
        path_changes=MappingProxyType(path_changes),
        doc_add=MappingProxyType(doc_add),
        pr_by_number=MappingProxyType({k: tuple(sorted(set(v))) for k, v in pr_by_number.items()}),
        tracker_by_id=MappingProxyType({k: tuple(sorted(set(v))) for k, v in tracker.items()}),
        tag_by_version=MappingProxyType(
            {k: tuple(sorted(set(v))) for k, v in tag_by_version.items()}
        ),
        sorted_shas=sorted_shas,
    )


def _norm_path(path: str) -> str:
    path = path.strip()
    while path.startswith("./"):
        path = path[2:]
    return path


@dataclass(frozen=True)
class ResolvedRef:
    """The resolution of one :class:`Ref` found in artifact ``source`` (an artifact key).

    ``outcome`` is one of :data:`OUTCOMES`. ``ledger`` names the owning ledger for ``local`` and
    ``foreign``; ``candidates`` lists the competing ledgers (``ambiguous``, or the foreign
    shadow of a ``local`` hit); ``targets`` the artifact keys for ``artifact`` (and the
    competing artifacts of an ``ambiguous`` sha).
    """

    source: str
    ref: Ref
    outcome: str
    ledger: str | None = None
    candidates: tuple[str, ...] = ()
    targets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.outcome not in OUTCOMES:
            raise CollationFormatError(f"unknown ref outcome {self.outcome!r}")

    def to_json(self) -> dict[str, Any]:
        """The record as a JSON object."""
        return {
            "source": self.source,
            "ref": self.ref.to_json(),
            "outcome": self.outcome,
            "ledger": self.ledger,
            "candidates": list(self.candidates),
            "targets": list(self.targets),
        }

    @classmethod
    def from_json(cls, raw: Any) -> ResolvedRef:
        """Parse a record object; unknown or missing members are refused."""
        members = ("source", "ref", "outcome", "ledger", "candidates", "targets")
        if not isinstance(raw, dict) or sorted(raw) != sorted(members):
            raise CollationFormatError(f"ref record must have exactly {members}")
        return cls(
            source=raw["source"],
            ref=Ref.from_json(raw["ref"]),
            outcome=raw["outcome"],
            ledger=raw["ledger"],
            candidates=tuple(raw["candidates"]),
            targets=tuple(raw["targets"]),
        )


class _Overlay:
    """The baseline as the local ledger over a directory (local owners are the baseline's ids)."""

    def __init__(self, directory: LedgerDirectory, local: str, ids: frozenset[str]) -> None:
        self._directory = directory
        self._local = local
        self._ids = ids
        self._memo: dict[str, tuple[str, ...]] = {}

    def ledgers(self) -> tuple[str, ...]:
        return tuple(sorted({*self._directory.ledgers(), self._local}))

    def contains(self, ledger: str, token: str) -> bool:
        return ledger in self.owners(token)

    def owners(self, token: str) -> tuple[str, ...]:
        if token not in self._memo:
            found = {o for o in self._directory.owners(token) if o != self._local}
            if token in self._ids:
                found.add(self._local)
            self._memo[token] = tuple(sorted(found))
        return self._memo[token]


def baseline_ids(baseline: Iterable[BeadEvent]) -> frozenset[str]:
    """The bead ids the baseline holds (the local ledger's id set)."""
    return frozenset(ref.id for ref in fold(baseline))


def _strip(raw: str, *prefixes: str) -> str:
    raw = raw.strip().lstrip("#!")
    for prefix in prefixes:
        if raw.startswith(prefix):
            return raw[len(prefix) :]
    return raw


def _resolve_artifact_ref(source: str, ref: Ref, ix: Indexes) -> ResolvedRef:
    def hit(keys: Iterable[str]) -> ResolvedRef:
        targets = tuple(sorted(set(keys)))
        return ResolvedRef(source, ref, ARTIFACT, targets=targets) if targets else miss()

    def miss() -> ResolvedRef:
        return ResolvedRef(source, ref, UNRESOLVED)

    raw = ref.raw.strip()
    if raw in ix.by_key:
        return hit([raw])
    if ref.kind == "sha":
        token = raw.lower()
        if token in ix.by_sha:
            return hit([ix.by_sha[token]])
        if _HEX.match(token) and len(token) >= MIN_SHA_PREFIX and len(token) < _FULL_SHA:
            shas = ix.shas_with_prefix(token)
            if len(shas) == 1:
                return hit([ix.by_sha[shas[0]]])
            if len(shas) > 1:
                return ResolvedRef(
                    source, ref, AMBIGUOUS, targets=tuple(sorted(ix.by_sha[s] for s in shas))
                )
        return miss()
    if ref.kind == "pr":
        number = _strip(raw, "gh:pr/", "pr/")
        return hit(ix.pr_by_number.get(int(number), ())) if number.isdigit() else miss()
    if ref.kind == "issue":
        return hit(ix.tracker_by_id.get(_strip(raw, "gh:issue/", "issue/"), ()))
    if ref.kind == "tag":
        return hit(ix.tag_by_version.get(_strip(raw, "git:tag/"), ()))
    if ref.kind == "path":
        path = _norm_path(raw)
        doc = f"doc:{path}"
        if doc in ix.by_key:
            return hit([doc])
        return hit(ix.path_changes.get(path, ()))
    return miss()


def resolve_refs(
    corpus: EvidenceCorpus,
    indexes: Indexes,
    baseline: Iterable[BeadEvent],
    directory: LedgerDirectory,
    *,
    local: str,
) -> tuple[ResolvedRef, ...]:
    """B2: resolve every ref of every artifact, in corpus order then ref order."""
    overlay = _Overlay(directory, local, baseline_ids(baseline))
    out: list[ResolvedRef] = []
    for artifact in corpus.artifacts:
        for ref in artifact.refs:
            if ref.kind == BEAD_TOKEN:
                r = resolve(overlay, ref.raw, local=local)
                out.append(
                    ResolvedRef(
                        artifact.key,
                        ref,
                        r.kind,
                        ledger=r.ledger,
                        candidates=r.candidates,
                    )
                )
            else:
                out.append(_resolve_artifact_ref(artifact.key, ref, indexes))
    return tuple(out)


@dataclass(frozen=True)
class Collation:
    """The collation of one run (``bdlg.collation/1``).

    ``local`` names the ledger being backfilled; ``corpus`` and ``baseline`` fingerprint the
    inputs (``sha256:`` of the corpus bytes, ``beadlog.corpus_hash`` of the baseline). ``refs``
    is B2. ``units``, ``verdicts``, ``conflicts`` and ``residual`` (:data:`EXTENSION_MEMBERS`)
    are JSON-valued tuples B3 to B6 fill; B1/B2 leave them empty. ``indexes`` is the in-memory
    B1 result (derived, never serialized; ``None`` on a loaded collation).
    """

    local: str
    corpus: str
    baseline: str
    refs: tuple[ResolvedRef, ...] = ()
    units: tuple[Any, ...] = ()
    verdicts: tuple[Any, ...] = ()
    conflicts: tuple[Any, ...] = ()
    residual: tuple[Any, ...] = ()
    indexes: Indexes | None = field(default=None, compare=False, hash=False, repr=False)

    def refs_of(self, source: str) -> tuple[ResolvedRef, ...]:
        """The resolved refs found in artifact ``source``."""
        return tuple(r for r in self.refs if r.source == source)

    def to_json(self) -> dict[str, Any]:
        """The collation as a JSON object."""
        return {
            "schema": COLLATION_SCHEMA,
            "local": self.local,
            "corpus": self.corpus,
            "baseline": self.baseline,
            "refs": [r.to_json() for r in self.refs],
            "units": list(self.units),
            "verdicts": list(self.verdicts),
            "conflicts": list(self.conflicts),
            "residual": list(self.residual),
        }


def collate(
    corpus: EvidenceCorpus,
    baseline: Iterable[BeadEvent],
    directory: LedgerDirectory,
    *,
    local: str,
) -> Collation:
    """Run B1 and B2 over ``corpus`` (see the module doc)."""
    baseline = tuple(baseline)
    indexes = build_indexes(corpus)
    return Collation(
        local=local,
        corpus="sha256:" + hashlib.sha256(dumps_evidence(corpus)).hexdigest(),
        baseline=corpus_hash(baseline),
        refs=resolve_refs(corpus, indexes, baseline, directory, local=local),
        indexes=indexes,
    )


def dumps_collation(collation: Collation) -> bytes:
    """The collation as canonical ``bdlg.collation/1`` JSON bytes (one line, trailing newline)."""
    return canonical_json(collation.to_json()).encode("utf-8") + b"\n"


def loads_collation(data: bytes) -> Collation:
    """Parse :func:`dumps_collation` bytes; anything it would not write is refused."""
    try:
        raw = json.loads(data)
    except ValueError as error:
        raise CollationFormatError(f"collation is not JSON: {error}") from None
    members = ("schema", "local", "corpus", "baseline", "refs", *EXTENSION_MEMBERS)
    if not isinstance(raw, dict) or sorted(raw) != sorted(members):
        raise CollationFormatError(f"collation must have exactly the members {members}")
    if raw["schema"] != COLLATION_SCHEMA:
        raise CollationFormatError(f"schema must be {COLLATION_SCHEMA!r}, got {raw['schema']!r}")
    collation = Collation(
        local=raw["local"],
        corpus=raw["corpus"],
        baseline=raw["baseline"],
        refs=tuple(ResolvedRef.from_json(r) for r in raw["refs"]),
        **{name: tuple(raw[name]) for name in EXTENSION_MEMBERS},
    )
    if dumps_collation(collation) != data:
        raise CollationFormatError("collation is not canonical JSON")
    return collation


def write_collation(collation: Collation, path: str | Path) -> None:
    """Write ``collation`` to ``path``."""
    Path(path).write_bytes(dumps_collation(collation))


def read_collation(path: str | Path) -> Collation:
    """Read a collation from ``path``."""
    return loads_collation(Path(path).read_bytes())


def collation_schema() -> dict[str, Any]:
    """The packaged JSON Schema of a ``bdlg.collation/1`` document."""
    text = (resources.files("beadlog_sources") / "schemas" / SCHEMA_RESOURCE).read_text(
        encoding="utf-8"
    )
    schema: dict[str, Any] = json.loads(text)
    return schema


__all__ = [
    "ARTIFACT",
    "COLLATION_SCHEMA",
    "EXTENSION_MEMBERS",
    "MIN_SHA_PREFIX",
    "OUTCOMES",
    "Collation",
    "CollationFormatError",
    "Indexes",
    "ResolvedRef",
    "baseline_ids",
    "build_indexes",
    "collate",
    "collation_schema",
    "dumps_collation",
    "loads_collation",
    "read_collation",
    "resolve_refs",
    "write_collation",
]
