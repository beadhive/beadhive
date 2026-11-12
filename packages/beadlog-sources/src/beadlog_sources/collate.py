"""Phase B of the backfill pipeline: collate an evidence corpus (``bdlg.collation/1``).

One engine over **evidence corpus + baseline BeadLog + LedgerDirectory**, pure and
deterministic: no git, no store, no network, every artifact read once. This module is steps
B1 and B2 of ``docs/design/backfill-history-pipeline-adr.md``; later steps (B3 cluster, B4 attach
docs, B5 identify, B6 status/conflicts) extend :class:`Collation` through the members reserved in
:data:`EXTENSION_MEMBERS`. B3 (``beadlog_sources.cluster``) fills ``units`` with :class:`Unit`
records; :func:`collate` runs it after B2. B5 (``beadlog_sources.identify``) fills ``verdicts``
and ``residual``; :func:`collate` runs it after B4.

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
from typing import TYPE_CHECKING, Any

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

if TYPE_CHECKING:
    from .attach import AttachOptions
    from .cluster import ClusterOptions
    from .identify import IdentifyOptions, IdentifyRun

COLLATION_SCHEMA = "bdlg.collation/1"
SCHEMA_RESOURCE = "bdlg-collation-v1.schema.json"

# The resolution outcomes of a ref: the ledger outcomes plus ``artifact``.
ARTIFACT = "artifact"
OUTCOMES = (LOCAL, FOREIGN, ARTIFACT, AMBIGUOUS, UNRESOLVED)

MIN_SHA_PREFIX = 7
# Members later collate steps fill; B1/B2 write them empty so the schema is stable.
EXTENSION_MEMBERS = ("units", "verdicts", "conflicts", "residual")

# The sub-objects of a collation's ``options``: the effective options of each step that has
# them (B3 ``cluster`` today; B4 to B6 add siblings).
OPTION_SECTIONS = ("cluster", "attach", "identify")

# B3 unit shapes and the rules that form units (``beadlog_sources.cluster``). The claim rules
# run in :data:`CLAIM_RULES` order by default; ``area`` groups units into epics afterwards.
EPIC = "epic"
ISSUE = "issue"
ATTACH = "attach"
UNIT_KINDS = (EPIC, ISSUE, ATTACH)
RULE_PLAN_ITEM = "plan-item"
RULE_LOCAL_BEAD = "local-bead"
RULE_REVIEW = "review"
RULE_MERGE = "merge"
RULE_COMMIT = "commit"
RULE_AREA = "area"
CLAIM_RULES = (RULE_PLAN_ITEM, RULE_LOCAL_BEAD, RULE_REVIEW, RULE_MERGE, RULE_COMMIT)
# ``doc`` (B4): a design record no unit took, a unit of its own.
RULE_DOC = "doc"
UNIT_RULES = (*CLAIM_RULES, RULE_AREA, RULE_DOC)

# B4: why a doc is attached to a unit (``beadlog_sources.attach``), in precedence order, then
# ``standalone`` for a doc that is a doc-unit of its own.
REASON_EXPLICIT = "explicit"
REASON_SAME_CHANGE = "same-change"
REASON_SHARED_PATH = "shared-path"
REASON_LEXICAL = "lexical"
REASON_STANDALONE = "standalone"
ATTACH_REASONS = (
    REASON_EXPLICIT,
    REASON_SAME_CHANGE,
    REASON_SHARED_PATH,
    REASON_LEXICAL,
    REASON_STANDALONE,
)

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


_UNIT_MEMBERS = (
    "key",
    "kind",
    "rule",
    "title",
    "bead",
    "artifacts",
    "area",
    "parent",
    "children",
    "depends_on",
)


@dataclass(frozen=True)
class Unit:
    """One B3 work unit: a candidate bead, or evidence to attach to an existing local bead.

    - ``key``: the stable source key, later the bead's ``external_ref`` (the anchor artifact's
      key: ``plan:<item>``, ``gh:pr/<n>``, ``git:commit/<sha>`` of a merge or a commit), or
      ``bead:<id>`` for an ``attach`` unit and ``area:<name>`` for an area epic.
    - ``kind``: ``epic`` (has children), ``issue`` (a leaf candidate bead) or ``attach`` (the
      evidence of an existing local bead ``bead``; never a new bead).
    - ``rule``: the rule that formed it (:data:`UNIT_RULES`).
    - ``artifacts``: the artifact keys it claimed (its evidence), anchor first, then members in
      claim order (commit order for changes). Each artifact belongs to at most one unit. An
      area epic claims no artifact; a release-window epic cites its release marker.
    - ``area``: the area a grouped unit falls in (also set when its area is too small to get
      an epic); ``parent`` / ``children``: unit keys of the parent-child membership (area epics
      and plan-item structure); ``depends_on``: unit keys a plan item depends on.

    Later steps add members of their own (B4 attached docs, B6 status and dates); B5 verdicts
    live in the collation's ``verdicts``, keyed by ``key``.
    """

    key: str
    kind: str
    rule: str
    title: str
    bead: str | None = None
    artifacts: tuple[str, ...] = ()
    area: str | None = None
    parent: str | None = None
    children: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.key, str) or not self.key:
            raise CollationFormatError("unit key must be a non-empty string")
        if self.kind not in UNIT_KINDS:
            raise CollationFormatError(f"unit {self.key!r}: unknown kind {self.kind!r}")
        if self.rule not in UNIT_RULES:
            raise CollationFormatError(f"unit {self.key!r}: unknown rule {self.rule!r}")
        if (self.kind == ATTACH) != (self.bead is not None):
            raise CollationFormatError(f"unit {self.key!r}: bead is set exactly for attach units")

    def to_json(self) -> dict[str, Any]:
        """The unit as a JSON object."""
        return {
            "key": self.key,
            "kind": self.kind,
            "rule": self.rule,
            "title": self.title,
            "bead": self.bead,
            "artifacts": list(self.artifacts),
            "area": self.area,
            "parent": self.parent,
            "children": list(self.children),
            "depends_on": list(self.depends_on),
        }

    @classmethod
    def from_json(cls, raw: Any) -> Unit:
        """Parse a unit object; unknown or missing members are refused."""
        if not isinstance(raw, dict) or sorted(raw) != sorted(_UNIT_MEMBERS):
            raise CollationFormatError(f"unit must have exactly {_UNIT_MEMBERS}")
        return cls(
            key=raw["key"],
            kind=raw["kind"],
            rule=raw["rule"],
            title=raw["title"],
            bead=raw["bead"],
            artifacts=tuple(raw["artifacts"]),
            area=raw["area"],
            parent=raw["parent"],
            children=tuple(raw["children"]),
            depends_on=tuple(raw["depends_on"]),
        )


def claimed_artifacts(units: Iterable[Unit]) -> dict[str, str]:
    """Artifact key to the key of the unit that claimed it."""
    return {artifact: unit.key for unit in units for artifact in unit.artifacts}


_ATTACHMENT_MEMBERS = ("doc", "unit", "reason", "evidence")


@dataclass(frozen=True)
class Attachment:
    """One B4 record: design record ``doc`` (a ``doc:<path>`` key) belongs to unit ``unit``.

    ``reason`` is one of :data:`ATTACH_REASONS`; ``evidence`` the artifact keys behind it (empty
    for ``standalone``, where ``unit`` is the doc's own doc-unit, i.e. ``unit == doc``). A doc has
    at most one attachment.
    """

    doc: str
    unit: str
    reason: str
    evidence: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.doc, str) or not self.doc or not isinstance(self.unit, str):
            raise CollationFormatError("attachment doc and unit must be non-empty strings")
        if self.reason not in ATTACH_REASONS:
            raise CollationFormatError(f"attachment {self.doc!r}: unknown reason {self.reason!r}")
        if (self.reason == REASON_STANDALONE) != (self.unit == self.doc and not self.evidence):
            raise CollationFormatError(
                f"attachment {self.doc!r}: standalone exactly when the unit is the doc itself"
            )

    def to_json(self) -> dict[str, Any]:
        """The record as a JSON object."""
        return {
            "doc": self.doc,
            "unit": self.unit,
            "reason": self.reason,
            "evidence": list(self.evidence),
        }

    @classmethod
    def from_json(cls, raw: Any) -> Attachment:
        """Parse a record object; unknown or missing members are refused."""
        if not isinstance(raw, dict) or sorted(raw) != sorted(_ATTACHMENT_MEMBERS):
            raise CollationFormatError(f"attachment must have exactly {_ATTACHMENT_MEMBERS}")
        return cls(
            doc=raw["doc"], unit=raw["unit"], reason=raw["reason"], evidence=tuple(raw["evidence"])
        )


@dataclass(frozen=True)
class Collation:
    """The collation of one run (``bdlg.collation/1``).

    ``local`` names the ledger being backfilled; ``corpus`` and ``baseline`` fingerprint the
    inputs (``sha256:`` of the corpus bytes, ``beadlog.corpus_hash`` of the baseline). ``refs``
    is B2, ``units`` B3 (:class:`Unit`, sorted by key). ``verdicts`` and ``residual`` are B5's
    JSON records (``beadlog_sources.identify``: one verdict per step that answered for a unit,
    sorted by unit then step; one residual record per unit nothing decided, sorted by unit).
    ``conflicts`` is a JSON-valued tuple B6 fills; until then it is empty. ``options``
    records the effective options of the steps that ran, one JSON sub-object per step
    (:data:`OPTION_SECTIONS`; ``cluster`` is :meth:`ClusterOptions.to_json`, ``attach``
    :meth:`~beadlog_sources.attach.AttachOptions.to_json`), so a collation
    made under other options has other bytes. ``indexes`` is the in-memory B1 result (derived,
    never serialized; ``None`` on a loaded collation).
    """

    local: str
    corpus: str
    baseline: str
    refs: tuple[ResolvedRef, ...] = ()
    units: tuple[Unit, ...] = ()
    verdicts: tuple[Any, ...] = ()
    conflicts: tuple[Any, ...] = ()
    residual: tuple[Any, ...] = ()
    attachments: tuple[Attachment, ...] = ()
    options: Mapping[str, Any] = field(default_factory=dict, hash=False)
    indexes: Indexes | None = field(default=None, compare=False, hash=False, repr=False)

    def refs_of(self, source: str) -> tuple[ResolvedRef, ...]:
        """The resolved refs found in artifact ``source``."""
        return tuple(r for r in self.refs if r.source == source)

    def unit(self, key: str) -> Unit | None:
        """The unit keyed ``key``, or ``None``."""
        return next((u for u in self.units if u.key == key), None)

    def docs_of(self, unit: str) -> tuple[Attachment, ...]:
        """The B4 attachments to ``unit`` (its doc-unit's own standalone record included)."""
        return tuple(a for a in self.attachments if a.unit == unit)

    def to_json(self) -> dict[str, Any]:
        """The collation as a JSON object."""
        return {
            "schema": COLLATION_SCHEMA,
            "local": self.local,
            "corpus": self.corpus,
            "baseline": self.baseline,
            "refs": [r.to_json() for r in self.refs],
            "units": [u.to_json() for u in self.units],
            "verdicts": list(self.verdicts),
            "conflicts": list(self.conflicts),
            "residual": list(self.residual),
            "attachments": [a.to_json() for a in self.attachments],
            "options": json.loads(canonical_json(dict(self.options))),
        }


def collate(
    corpus: EvidenceCorpus,
    baseline: Iterable[BeadEvent],
    directory: LedgerDirectory,
    *,
    local: str,
    options: ClusterOptions | None = None,
    attach_options: AttachOptions | None = None,
    identify_options: IdentifyOptions | None = None,
    identify_run: IdentifyRun | None = None,
) -> Collation:
    """Run B1 to B4 over ``corpus`` (see the module doc, ``beadlog_sources.cluster`` and
    ``beadlog_sources.attach``).

    ``options`` configures B3 (default :class:`~beadlog_sources.cluster.ClusterOptions`),
    ``attach_options`` B4 (default :class:`~beadlog_sources.attach.AttachOptions`),
    ``identify_options`` and ``identify_run`` B5 (``beadlog_sources.identify``; default: no
    judge, no threshold).
    """
    from .attach import attach  # B3 and B4 build on this module's records
    from .cluster import cluster

    baseline = tuple(baseline)
    indexes = build_indexes(corpus)
    resolved = Collation(
        local=local,
        corpus="sha256:" + hashlib.sha256(dumps_evidence(corpus)).hexdigest(),
        baseline=corpus_hash(baseline),
        refs=resolve_refs(corpus, indexes, baseline, directory, local=local),
        indexes=indexes,
    )
    from .identify import identify  # B5

    return identify(
        attach(cluster(resolved, options), attach_options), baseline, identify_options, identify_run
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
    members = (
        "schema",
        "local",
        "corpus",
        "baseline",
        "refs",
        *EXTENSION_MEMBERS,
        "attachments",
        "options",
    )
    if not isinstance(raw, dict) or sorted(raw) != sorted(members):
        raise CollationFormatError(f"collation must have exactly the members {members}")
    if raw["schema"] != COLLATION_SCHEMA:
        raise CollationFormatError(f"schema must be {COLLATION_SCHEMA!r}, got {raw['schema']!r}")
    collation = Collation(
        local=raw["local"],
        corpus=raw["corpus"],
        baseline=raw["baseline"],
        refs=tuple(ResolvedRef.from_json(r) for r in raw["refs"]),
        units=tuple(Unit.from_json(u) for u in raw["units"]),
        **{name: tuple(raw[name]) for name in EXTENSION_MEMBERS if name != "units"},
        attachments=tuple(Attachment.from_json(a) for a in raw["attachments"]),
        options=_load_options(raw["options"]),
    )
    _check_identify(collation)
    if dumps_collation(collation) != data:
        raise CollationFormatError("collation is not canonical JSON")
    return collation


def _check_identify(collation: Collation) -> None:
    from .identify import check_records  # B5 owns its record shapes

    check_records(collation.verdicts, collation.residual)


def _load_options(raw: Any) -> dict[str, Any]:
    from .attach import AttachOptions, AttachOptionsError  # B3 and B4 own their options
    from .cluster import ClusterOptions, ClusterOptionsError
    from .identify import IdentifyOptions, IdentifyOptionsError

    if not isinstance(raw, dict) or sorted(raw) != sorted(OPTION_SECTIONS):
        raise CollationFormatError(f"options must have exactly the members {OPTION_SECTIONS}")
    try:
        cluster_options = ClusterOptions.from_json(raw["cluster"])
    except ClusterOptionsError as error:
        raise CollationFormatError(f"options.cluster: {error}") from None
    try:
        attach_options = AttachOptions.from_json(raw["attach"])
    except AttachOptionsError as error:
        raise CollationFormatError(f"options.attach: {error}") from None
    try:
        identify_options = IdentifyOptions.from_json(raw["identify"])
    except IdentifyOptionsError as error:
        raise CollationFormatError(f"options.identify: {error}") from None
    return {
        "cluster": cluster_options.to_json(),
        "attach": attach_options.to_json(),
        "identify": identify_options.to_json(),
    }


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
    "ATTACH",
    "ATTACH_REASONS",
    "CLAIM_RULES",
    "COLLATION_SCHEMA",
    "EPIC",
    "EXTENSION_MEMBERS",
    "ISSUE",
    "MIN_SHA_PREFIX",
    "OPTION_SECTIONS",
    "OUTCOMES",
    "RULE_AREA",
    "REASON_EXPLICIT",
    "REASON_LEXICAL",
    "REASON_SAME_CHANGE",
    "REASON_SHARED_PATH",
    "REASON_STANDALONE",
    "RULE_COMMIT",
    "RULE_DOC",
    "RULE_LOCAL_BEAD",
    "RULE_MERGE",
    "RULE_PLAN_ITEM",
    "RULE_REVIEW",
    "UNIT_KINDS",
    "UNIT_RULES",
    "Attachment",
    "Collation",
    "CollationFormatError",
    "Indexes",
    "ResolvedRef",
    "Unit",
    "baseline_ids",
    "build_indexes",
    "claimed_artifacts",
    "collate",
    "collation_schema",
    "dumps_collation",
    "loads_collation",
    "read_collation",
    "resolve_refs",
    "write_collation",
]
