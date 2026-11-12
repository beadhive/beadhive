"""Phase C of the backfill pipeline: project collated work units to proposed BeadLog events.

:func:`project` maps a :class:`~beadlog_sources.collate.Collation` (B1 to B6 filled), the evidence
corpus it was collated from and the same baseline BeadLog to the events of one proposal
(``docs/design/backfill-history-pipeline-adr.md``, Phase C and "How every category feeds
BeadLog"). It is pure and neutral: no store, no git, no clock (``extracted_at`` is an argument),
no ``beadhive`` import. The proposal then goes through the unchanged
:func:`~beadlog_sources.reconcile.reconcile` -> plan -> apply path.

**One contract for every category.** Each event's ``source`` is ``(collate, <unit key>)`` and its
``source.evidence`` lists the artifact keys that support it (``Evidence(<artifact kind>, <key>)``).
Only BeadLog v0.1 kinds are used (:data:`EVENT_KINDS`: ``create``, ``set``, ``label_add``,
``edge_add``, ``phase``; ``comment`` is reserved for opt-in tracker comments and not emitted
today).

**What a unit projects to** is decided by its B5 verdict (the record with a non-null ``class``):

- **NEW** (the threshold or a judge decided it, so the decision is ``inferred``): a ``create``
  (type per :attr:`ProjectOptions.types`, ``epic`` for an epic unit; title; ``external_ref`` = the
  unit key, which is the idempotency key B5 step 1 matches on the next run; a ``Reconstructed from``
  description) carrying the verdict's confidence and, as annotation :data:`ANNOTATION`, the
  verdict; one ``label_add`` of its source label (:attr:`ProjectOptions.source_labels`, looked
  up by the anchor artifact's kind, then its category, then the unit's rule; the defaults
  :data:`DEFAULT_SOURCE_LABELS` match the labels the older passes already wrote:
  ``source:docs``, ``source:planning``, ``source:commit``, ``source:github``, ``source:area``)
  plus :attr:`ProjectOptions.creation_labels` (default none: the ``origin:backfill``
  creation label of decision D1 is wired by the create-mode caller); a ``parent-child``
  ``edge_add`` to the parent unit's bead (an area epic or a plan parent) and a ``blocks`` edge
  per plan ``depends_on`` whose target has a bead; a closed ``phase`` when B6 says closed; and the
  facts below.
- **PRESENT** (matched to baseline bead ``bead``): only DRIFT-style appends and stamps on that
  bead, never an overwrite: the facts below, plus an ``external_ref`` stamp (the unit key) when the
  bead has none. A stamp whose field already holds another value is not emitted; it is reported
  in :attr:`UnitProjection.kept`. No ``create``, ``phase``, label or edge ever touches an
  existing bead (it may be hand-made).
- **NOISE** (covered by a coarser epic) and **residual** (no decision): nothing is projected; the
  unit is reported (:attr:`Projection.residual` lists the residual units).

**The facts** (``set`` events, the same rule for NEW and PRESENT):

- ``metadata.git.commits`` (``change``): the unit's change shas appended in commit order to the
  bead's list (the ``git.commits`` wire format of :mod:`beadlog_sources.commits`, a JSON array as
  a string); nothing when every sha is recorded; an unparsable list is kept and reported.
- ``metadata.lifecycle.created_at`` / ``metadata.lifecycle.closed_at`` (B6): the moment as B6
  wrote it, a canonical timestamp or a ``YYYY-MM-DD`` day.
- ``metadata.xref.<ledger>`` (``tracker-item`` and foreign ledgers, B2): the bead tokens of the
  unit's evidence that resolved ``foreign`` to ``<ledger>``, and ``metadata.xref.tracker``: the
  keys of the tracker items its evidence refs (both appended as JSON arrays).
- ``metadata.release`` (``release-marker``): the version of the earliest release whose commit
  range holds one of the unit's changes, or of the release marker an epic cites.
- ``metadata.backfill.conflict`` (B6): the unit's conflicts as canonical JSON (rule, detail and
  both sources). Conflicts are reported, never resolved: the status stays as B6 derived it.

**Provenance** is set by how a fact was reached (:data:`OBSERVED` > :data:`DERIVED` >
:data:`INFERRED`): ``observed`` when a record states it (a review's ``merged_at``, a plan item's
done marker, parent and ``depends_on``, a closed tracker item); ``derived`` when a rule computed
it (clustering: ``git.commits``, area-epic edges, ``source:`` labels; B6 dates and conflicts; B2
xrefs; release windows); ``inferred`` for the B5 threshold/judge NEW, carrying ``confidence``.
On a PRESENT unit every fact is attributed to the bead through the B5 match, so its provenance is
the weaker of the fact's and the match's (a judged match makes every stamp ``inferred``, with the
judge's confidence).

**Dates.** ``occurred_at`` comes from the B6 :class:`~beadlog_sources.lifecycle.Moment` of the fact
(``created`` for the create, labels and edges; ``closed`` for the phase; the newest appended
commit for ``git.commits``; the release for ``metadata.release``). A day-precision moment
``YYYY-MM-DD`` is written the way BeadLog spells a coarser instant: ``YYYY-MM-DDT00:00:00Z`` with
``occurred_precision: day`` (the start of the interval, format ADR section 3), never as a bare
midnight. Two rules may move an instant, and both drop ``occurred_precision`` because the written
time is then an ordering floor, not the fact's clock (the fact's own moment always stays in
``metadata.lifecycle.*`` and the evidence):

- a closed ``phase`` that would not sort strictly after its bead's ``create`` (a commit unit is
  created and closed by the same commit) is raised to one microsecond after the create
  (:func:`~beadlog_sources.commits.just_after`), so the last-writer-wins fold ends ``closed``;
- a ``set`` on an existing bead that would not postdate the baseline's latest decider of that field
  is raised past it, as :mod:`beadlog_sources.commits` does.

A closed unit with no date at all gets no ``phase`` (an undated phase could sort before its
create); it is reported with note ``closed-undated``.

**Ids** are minted with :func:`~beadlog_sources._sources.mint_bead_id` from ``(prefix, group,
unit key)`` under the explicit ``prefix`` argument; a prefix is never derived (bh-ear6v). Units are
projected in key order and a minted id never collides with a baseline id, so the same inputs give
the same events, byte for byte.

**Idempotency.** After the events are applied, collating again matches every projected unit by its
``external_ref`` (B5 step 1, PRESENT), and every fact is already present, so projecting again
proposes zero events.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any

from beadlog import (
    BeadEvent,
    BeadRef,
    BeadState,
    Edge,
    Evidence,
    TimestampError,
    canonical_json,
    check_timestamp,
    fold,
    instant,
    mint_event,
    sort_events,
)

from ._sources import commit_time, floors, live_beads, mint_bead_id
from .collate import (
    ARTIFACT,
    ATTACH,
    EPIC,
    RULE_AREA,
    RULE_PLAN_ITEM,
    Collation,
    Indexes,
    Unit,
    build_indexes,
)
from .commits import dump_linkage, just_after, parse_linkage
from .evidence import Artifact, Category, EvidenceCorpus
from .ledger import BEAD_TOKEN, FOREIGN
from .lifecycle import STATUS_CLOSED, Lifecycle, Moment, conflicts_of, lifecycles
from .reconcile import ReconcileClass

SOURCE_SYSTEM = "collate"
ANNOTATION = "bdlg.collate"
EVENT_KINDS = ("create", "set", "label_add", "edge_add", "phase", "comment")

OBSERVED = "observed"
DERIVED = "derived"
INFERRED = "inferred"
_STRENGTH = {OBSERVED: 2, DERIVED: 1, INFERRED: 0}

EXTERNAL_REF = "external_ref"
GIT_COMMITS = "metadata.git.commits"
CREATED_AT = "metadata.lifecycle.created_at"
CLOSED_AT = "metadata.lifecycle.closed_at"
XREF_PREFIX = "metadata.xref."
XREF_TRACKER = "metadata.xref.tracker"
RELEASE = "metadata.release"
CONFLICT = "metadata.backfill.conflict"

DECISION_NEW = str(ReconcileClass.NEW)
DECISION_PRESENT = str(ReconcileClass.PRESENT)
DECISION_NOISE = str(ReconcileClass.NOISE)
DECISION_RESIDUAL = "residual"
DECISIONS = (DECISION_NEW, DECISION_PRESENT, DECISION_NOISE, DECISION_RESIDUAL)

# The B6 bases whose closing record states the closure itself.
OBSERVED_BASES = frozenset({"merged", "plan-item", "tracker"})

DEFAULT_TYPES: Mapping[str, str] = MappingProxyType(
    {
        Category.CHANGE.value: "feature",
        Category.REVIEW.value: "feature",
        Category.TRACKER_ITEM.value: "task",
        Category.DESIGN_RECORD.value: "decision",
        Category.PLAN_ITEM.value: "task",
        Category.RELEASE_MARKER.value: "epic",
    }
)
# The ``source:`` label of a NEW bead, by its anchor's kind, category or (area epics) unit rule.
# The values match the corpus the older passes wrote (``source:docs``, ``source:planning``) and
# #143's ``source:commit``, so the backfilled corpus is not split by a new spelling.
DEFAULT_SOURCE_LABELS: Mapping[str, str] = MappingProxyType(
    {
        Category.DESIGN_RECORD.value: "source:docs",
        Category.PLAN_ITEM.value: "source:planning",
        Category.CHANGE.value: "source:commit",
        Category.REVIEW.value: "source:github",
        Category.TRACKER_ITEM.value: "source:github",
        RULE_AREA: "source:area",
    }
)
OPTION_MEMBERS = ("creation_labels", "types", "source_labels")

NOTE_CLOSED_UNDATED = "closed-undated"
NOTE_PHASE_RAISED = "phase-raised"
NOTE_NO_BEAD = "missing-bead"
NOTE_PARENT_SKIPPED = "parent-skipped"
NOTE_DEPENDENCY_SKIPPED = "dependency-skipped"

_PREFIX = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")


class ProjectError(ValueError):
    """A projection that cannot be made (a missing prefix, an invalid option)."""


@dataclass(frozen=True)
class ProjectOptions:
    """Phase C options.

    - ``source_labels``: anchor kind, category or unit rule to the ``source:`` label of a NEW
      bead (default :data:`DEFAULT_SOURCE_LABELS`; a given mapping overrides or adds keys only).
      A unit no key covers gets ``source:<rule>``.
    - ``creation_labels``: labels every NEW bead gets besides its source label (default none;
      the create-mode caller passes ``origin:backfill``, decision D1).
    - ``types``: category to the bead type of a NEW non-epic unit (default
      :data:`DEFAULT_TYPES`; a given mapping overrides those keys only). Epic units are ``epic``.
    """

    creation_labels: tuple[str, ...] = ()
    types: Mapping[str, str] = field(default_factory=dict, hash=False)
    source_labels: Mapping[str, str] = field(default_factory=dict, hash=False)

    def __post_init__(self) -> None:
        labels = self.creation_labels
        if isinstance(labels, str) or not isinstance(labels, Iterable):
            raise ProjectError("creation_labels must be a list of strings")
        labels = tuple(labels)
        if not all(isinstance(label, str) and label.strip() for label in labels):
            raise ProjectError("creation_labels must be non-empty strings")
        object.__setattr__(self, "creation_labels", tuple(dict.fromkeys(labels)))
        if not isinstance(self.types, Mapping):
            raise ProjectError("types must map categories to bead types")
        unknown = sorted(set(self.types) - set(DEFAULT_TYPES))
        if unknown:
            raise ProjectError(f"types: unknown categories {unknown}")
        if not all(isinstance(v, str) and v.strip() for v in self.types.values()):
            raise ProjectError("types must map to non-empty bead types")
        object.__setattr__(
            self, "types", MappingProxyType({**DEFAULT_TYPES, **dict(sorted(self.types.items()))})
        )
        sources = self.source_labels
        if not isinstance(sources, Mapping) or not all(
            isinstance(k, str) and k and isinstance(v, str) and v.strip()
            for k, v in sources.items()
        ):
            raise ProjectError("source_labels must map non-empty keys to non-empty labels")
        object.__setattr__(
            self,
            "source_labels",
            MappingProxyType(dict(sorted({**DEFAULT_SOURCE_LABELS, **dict(sources)}.items()))),
        )

    def source_label(self, kind: str | None, category: str | None, rule: str) -> str:
        """The source label of a NEW bead: by anchor kind, then category, then unit rule."""
        for key in (kind, category, rule):
            if key is not None and key in self.source_labels:
                return self.source_labels[key]
        return f"source:{rule}"

    def to_json(self) -> dict[str, Any]:
        """The effective options as a JSON object."""
        return {
            "creation_labels": list(self.creation_labels),
            "types": dict(self.types),
            "source_labels": dict(self.source_labels),
        }


@dataclass(frozen=True)
class Kept:
    """A fact not written because the bead already holds another value (never overwritten)."""

    field: str
    existing: Any
    proposed: Any

    def to_json(self) -> dict[str, Any]:
        return {"field": self.field, "existing": self.existing, "proposed": self.proposed}


@dataclass(frozen=True)
class UnitProjection:
    """What one unit projected to: its B5 ``decision`` (:data:`DECISIONS`), the bead it maps to
    (minted for NEW, matched for PRESENT), the ids of its events, the facts ``kept`` and notes."""

    unit: str
    decision: str
    bead: str | None = None
    events: tuple[str, ...] = ()
    kept: tuple[Kept, ...] = ()
    notes: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "decision": self.decision,
            "bead": self.bead,
            "events": list(self.events),
            "kept": [k.to_json() for k in self.kept],
            "notes": list(self.notes),
        }


@dataclass(frozen=True)
class Projection:
    """The proposal (:attr:`events`, archive part order) and one record per unit, in key order."""

    events: tuple[BeadEvent, ...]
    units: tuple[UnitProjection, ...]
    options: ProjectOptions = field(default_factory=ProjectOptions)

    @property
    def residual(self) -> tuple[str, ...]:
        """The units no NEW/PRESENT/NOISE decision covers (projected nothing)."""
        return tuple(u.unit for u in self.units if u.decision == DECISION_RESIDUAL)

    def unit(self, key: str) -> UnitProjection | None:
        return next((u for u in self.units if u.unit == key), None)

    def counts(self) -> dict[str, int]:
        out = dict.fromkeys(DECISIONS, 0)
        for item in self.units:
            out[item.decision] += 1
        return out

    def to_json(self) -> dict[str, Any]:
        """The report (not the events) as a JSON object."""
        return {
            "counts": self.counts(),
            "events": len(self.events),
            "options": self.options.to_json(),
            "units": [u.to_json() for u in self.units],
        }


# -- helpers -----------------------------------------------------------------------------------


def _weaker(*provenances: str) -> str:
    return min(provenances, key=_STRENGTH.__getitem__)


def _canonical(at: str) -> str | None:
    try:
        return check_timestamp(at)
    except TimestampError:
        return commit_time(at)


@dataclass(frozen=True)
class _When:
    """An event's ``occurred_at`` and ``occurred_precision``."""

    at: str | None = None
    precision: str | None = None

    @classmethod
    def of(cls, moment: Moment | None) -> _When:
        if moment is None:
            return cls()
        if moment.precision == "day":
            return cls(f"{moment.at}T00:00:00Z", "day")
        return cls(_canonical(moment.at))

    def after(self, floor: str | None) -> _When:
        """This time, raised past ``floor`` when it does not postdate it (precision dropped)."""
        if floor is None or (self.at is not None and instant(self.at) > instant(floor)):
            return self
        return _When(just_after(floor))


def _decision(collation: Collation) -> dict[str, Mapping[str, Any]]:
    decided: dict[str, Mapping[str, Any]] = {}
    for record in collation.verdicts:
        if record["class"] is not None:
            decided[record["unit"]] = record
    return decided


class _Projector:
    def __init__(
        self,
        collation: Collation,
        corpus: EvidenceCorpus,
        baseline: tuple[BeadEvent, ...],
        *,
        prefix: str,
        group: str,
        extracted_at: str,
        options: ProjectOptions,
    ) -> None:
        self.collation = collation
        self.prefix = prefix
        self.group = group
        self.extracted_at = extracted_at
        self.options = options
        self.arts: dict[str, Artifact] = {}
        for art in corpus.artifacts:
            self.arts.setdefault(art.key, art)
        self.ix: Indexes = collation.indexes or build_indexes(corpus)
        self.units = {u.key: u for u in collation.units}
        self.life: dict[str, Lifecycle] = {life.unit: life for life in lifecycles(collation)}
        self.decided = _decision(collation)
        states = fold(baseline)
        self.live = live_beads(states, group)
        self.baseline = baseline
        self._floors: dict[str, dict[str, str]] = {}
        taken = {ref.id for ref in states if ref.group == group}
        self.beads: dict[str, str] = {}  # unit key -> its bead id (minted or matched)
        for key in sorted(self.units):
            record = self.decided.get(key)
            if record is None:
                continue
            if record["class"] == DECISION_NEW:
                ident = mint_bead_id(prefix, group, key, taken)
                taken.add(ident)
                self.beads[key] = ident
            elif record["class"] == DECISION_PRESENT and record["bead"] in self.live:
                self.beads[key] = record["bead"]

    # -- evidence ------------------------------------------------------------------------------

    def kind_of(self, key: str) -> str:
        art = self.arts.get(key)
        return art.kind if art is not None else "unit"

    def own_keys(self, unit: Unit) -> list[str]:
        """The unit's artifacts and attached docs."""
        return list(
            dict.fromkeys([*unit.artifacts, *(a.doc for a in self.collation.docs_of(unit.key))])
        )

    def evidence_keys(self, unit: Unit) -> list[str]:
        """The unit's own keys; an epic with none cites its children's."""
        keys = self.own_keys(unit)
        if not keys:
            for child in unit.children:
                kid = self.units.get(child)
                if kid is not None:
                    keys.extend(self.evidence_keys(kid))
        return list(dict.fromkeys(keys)) or [unit.key]

    def evidence(self, keys: Iterable[str]) -> list[Evidence]:
        return [Evidence(self.kind_of(k), k) for k in dict.fromkeys(keys)]

    def anchor(self, unit: Unit) -> Artifact | None:
        """The artifact the unit is keyed by (none for an area epic or an attach unit)."""
        return self.arts.get(unit.key)

    # -- facts -------------------------------------------------------------------------------

    def changes(self, unit: Unit) -> list[Artifact]:
        found = [self.arts[k] for k in unit.artifacts if k in self.arts]
        changes = [a for a in found if a.category is Category.CHANGE]
        return sorted(changes, key=lambda a: (str(a.attrs["committed_at"]), str(a.attrs["sha"])))

    def foreign(self, unit: Unit) -> tuple[dict[str, list[str]], dict[str, list[str]], list[str]]:
        """``(ledger -> tokens, token -> sources, tracker keys)`` of the refs in the unit's own
        artifacts and docs (an epic does not repeat its children's)."""
        sources = set(self.own_keys(unit))
        tokens: dict[str, list[str]] = {}
        where: dict[str, list[str]] = {}
        trackers: list[str] = []
        for ref in self.collation.refs:
            if ref.source not in sources:
                continue
            if ref.ref.kind == BEAD_TOKEN and ref.outcome == FOREIGN and ref.ledger:
                token = ref.ref.raw.strip()
                tokens.setdefault(ref.ledger, []).append(token)
                where.setdefault(f"{ref.ledger}\0{token}", []).append(ref.source)
            elif ref.outcome == ARTIFACT:
                for target in ref.targets:
                    art = self.arts.get(target)
                    if art is not None and art.category is Category.TRACKER_ITEM:
                        trackers.append(target)
                        where.setdefault(f"tracker\0{target}", []).append(ref.source)
        return (
            {k: sorted(set(v)) for k, v in sorted(tokens.items())},
            where,
            sorted(set(trackers)),
        )

    def release(self, unit: Unit) -> Artifact | None:
        cited = [
            self.arts[k]
            for k in unit.artifacts
            if k in self.arts and self.arts[k].category is Category.RELEASE_MARKER
        ]
        members = {a.key for a in self.changes(unit)}
        holding = [
            self.arts[key]
            for key, keys in self.ix.tag_range.items()
            if key in self.arts and members.intersection(keys)
        ]
        found = cited or holding
        if not found:
            return None
        return min(
            found,
            key=lambda a: (a.attrs["released_at"] is None, str(a.attrs["released_at"]), a.key),
        )

    # -- events ------------------------------------------------------------------------------

    def floor(self, bead: str, name: str) -> str | None:
        if name not in self._floors:
            self._floors[name] = floors(self.baseline, self.group, name)
        return self._floors[name].get(bead)

    def run(self) -> Projection:
        events: list[BeadEvent] = []
        out: list[UnitProjection] = []
        for key in sorted(self.units):
            made, record = self.unit(self.units[key])
            events.extend(made)
            out.append(record)
        return Projection(sort_events(events), tuple(out), self.options)

    def unit(self, unit: Unit) -> tuple[list[BeadEvent], UnitProjection]:
        record = self.decided.get(unit.key)
        if record is None:
            return [], UnitProjection(unit.key, DECISION_RESIDUAL)
        cls = record["class"]
        if cls == DECISION_NOISE:
            return [], UnitProjection(unit.key, DECISION_NOISE, record["bead"])
        if unit.key not in self.beads:
            return [], UnitProjection(unit.key, cls, record["bead"], notes=(NOTE_NO_BEAD,))
        ident = self.beads[unit.key]
        writer = _Writer(self, unit, record, BeadRef(self.group, ident))
        if cls == DECISION_NEW:
            writer.create()
        elif unit.kind != ATTACH:  # an attach unit's key names its bead: nothing to stamp
            writer.link()
        writer.facts()
        return writer.events, UnitProjection(
            unit.key,
            cls,
            ident,
            tuple(e.id for e in writer.events),
            tuple(writer.kept),
            tuple(writer.notes),
        )


class _Writer:
    """The events of one unit onto one bead."""

    def __init__(
        self, owner: _Projector, unit: Unit, record: Mapping[str, Any], bead: BeadRef
    ) -> None:
        self.p = owner
        self.unit = unit
        self.record = record
        self.bead = bead
        self.new = record["class"] == DECISION_NEW
        self.state: BeadState | None = None if self.new else owner.live[bead.id]
        self.match = str(record["provenance"])
        permille = record["confidence_permille"]
        self.confidence = None if permille is None else permille / 1000
        life = owner.life.get(unit.key)
        self.lifecycle = life
        self.created = None if life is None else life.created
        self.closed = None if life is None else life.closed
        self.events: list[BeadEvent] = []
        self.kept: list[Kept] = []
        self.notes: list[str] = []
        self.create_at = _When.of(self.created)

    # -- minting -----------------------------------------------------------------------------

    def provenance(self, fact: str) -> tuple[str, float | None]:
        """A fact's provenance on this bead: on a matched bead, no stronger than the match."""
        prov = fact if self.new else _weaker(fact, self.match)
        return prov, (self.confidence if prov == INFERRED else None)

    def emit(
        self,
        kind: str,
        data: Mapping[str, Any],
        when: _When,
        evidence: Iterable[str],
        provenance: str,
        confidence: float | None = None,
        annotations: Mapping[str, Any] | None = None,
    ) -> BeadEvent:
        event = mint_event(
            kind,
            self.bead,
            data,
            occurred_at=when.at,
            occurred_precision=when.precision,
            recorded_at=None,
            extracted_at=self.p.extracted_at,
            source=(SOURCE_SYSTEM, self.unit.key),
            provenance=provenance,
            confidence=confidence,
            evidence=self.p.evidence(evidence),
            annotations=annotations,
        )
        self.events.append(event)
        return event

    # -- NEW ---------------------------------------------------------------------------------

    def bead_type(self) -> str:
        if self.unit.kind == EPIC:
            return "epic"
        anchor = self.p.anchor(self.unit)
        category = anchor.category.value if anchor is not None else Category.CHANGE.value
        return self.p.options.types[category]

    def verdict(self) -> dict[str, Any]:
        r = self.record
        return {
            "step": r["step"],
            "class": r["class"],
            "engine": r["engine"],
            "engine_version": r["engine_version"],
            "model": r["model"],
            "score": r["score"],
            "review": r["review"],
            "query_hash": r["query_hash"],
            "confidence_permille": r["confidence_permille"],
            "rationale": r["rationale"],
        }

    def create(self) -> None:
        unit = self.unit
        keys = self.p.evidence_keys(unit)
        create = self.emit(
            "create",
            {
                "type": self.bead_type(),
                "fields": {
                    "title": unit.title,
                    EXTERNAL_REF: unit.key,
                    "description": f"Reconstructed from {unit.key}.",
                },
            },
            self.create_at,
            keys,
            str(self.record["provenance"]),
            self.confidence,
            {ANNOTATION: {"verdict": self.verdict()}},
        )
        anchor = self.p.anchor(unit)
        source = self.p.options.source_label(
            anchor.kind if anchor is not None else None,
            anchor.category.value if anchor is not None else None,
            unit.rule,
        )
        label_evidence = [anchor.key] if anchor is not None else keys
        for label in dict.fromkeys([source, *self.p.options.creation_labels]):
            self.emit("label_add", {"label": label}, self.create_at, label_evidence, DERIVED)
        self.edges(keys)
        self.phase(create)

    def edges(self, keys: list[str]) -> None:
        unit = self.unit
        plan = unit.rule == RULE_PLAN_ITEM
        stated = [unit.key] if unit.key in self.p.arts else keys
        if unit.parent is not None:
            parent = self.p.beads.get(unit.parent)
            if parent is None:
                self.notes.append(f"{NOTE_PARENT_SKIPPED}:{unit.parent}")
            else:
                parent_unit = self.p.units.get(unit.parent)
                by_rule = parent_unit is not None and parent_unit.rule == RULE_AREA
                edge = Edge("parent-child", BeadRef(self.p.group, parent))
                self.emit(
                    "edge_add",
                    edge.to_json(),
                    self.create_at,
                    stated if plan else keys,
                    OBSERVED if plan and not by_rule else DERIVED,
                )
        for dep in unit.depends_on:
            target = self.p.beads.get(dep)
            if target is None:
                self.notes.append(f"{NOTE_DEPENDENCY_SKIPPED}:{dep}")
                continue
            edge = Edge("blocks", BeadRef(self.p.group, target))
            self.emit(
                "edge_add",
                edge.to_json(),
                self.create_at,
                [*stated, dep],
                OBSERVED if plan else DERIVED,
            )

    def phase(self, create: BeadEvent) -> None:
        life = self.lifecycle
        if life is None or life.status != STATUS_CLOSED:
            return
        if create.occurred_at is None:
            self.notes.append(NOTE_CLOSED_UNDATED)
            return
        when = _When.of(self.closed) if self.closed is not None else _When()
        if when.at is None or instant(when.at) <= instant(create.occurred_at):
            when = _When(just_after(create.occurred_at))
            self.notes.append(NOTE_PHASE_RAISED)
        source = self.closed.source if self.closed is not None else self.unit.key
        evidence = [source] if source in self.p.arts else self.p.evidence_keys(self.unit)
        self.emit(
            "phase",
            {"phase": "closed", "reason": f"{life.basis}: {source}"},
            when,
            evidence,
            OBSERVED if life.basis in OBSERVED_BASES else DERIVED,
        )

    def link(self) -> None:
        """Stamp the unit key as a matched bead's ``external_ref`` when it has none. A bead
        already linked to the unit (its key, a doc's path or one of its artifacts, as B5 step 1
        matches) is left as it is; any other link is kept and reported."""
        current = self.existing(EXTERNAL_REF)
        if current is None:
            self.stamp(EXTERNAL_REF, self.unit.key, self.created, [self.unit.key], DERIVED)
            return
        linked = {self.unit.key, *self.unit.artifacts}
        linked |= {k[len("doc:") :] for k in linked if k.startswith("doc:")}
        if current not in linked:
            self.kept.append(Kept(EXTERNAL_REF, current, self.unit.key))

    # -- facts (NEW and PRESENT) --------------------------------------------------------------

    def existing(self, name: str) -> Any:
        return None if self.state is None else self.state.fields.get(name)

    def stamp(
        self,
        name: str,
        value: Any,
        moment: Moment | None,
        evidence: Iterable[str],
        fact: str,
        when: _When | None = None,
    ) -> None:
        """Set ``name`` to ``value`` unless the bead holds it already (never overwrite)."""
        current = self.existing(name)
        if current == value:
            return
        if current is not None:
            self.kept.append(Kept(name, current, value))
            return
        self.set(name, value, when or _When.of(moment), evidence, fact)

    def append(
        self,
        name: str,
        values: list[str],
        when: _When,
        evidence: Iterable[str],
        fact: str,
    ) -> None:
        """Append the missing ``values`` to the JSON-array string ``name``."""
        raw = self.existing(name)
        existing: list[str] = []
        if raw is not None:
            parsed = parse_linkage(raw)
            if parsed is None:
                self.kept.append(Kept(name, raw, values))
                return
            existing = parsed
        missing = [v for v in dict.fromkeys(values) if v not in existing]
        if missing:
            self.set(name, dump_linkage([*existing, *missing]), when, evidence, fact)

    def set(self, name: str, value: Any, when: _When, evidence: Iterable[str], fact: str) -> None:
        if not self.new:
            when = when.after(self.p.floor(self.bead.id, name))
        provenance, confidence = self.provenance(fact)
        self.emit("set", {"field": name, "value": value}, when, evidence, provenance, confidence)

    def facts(self) -> None:
        unit = self.unit
        changes = self.p.changes(unit)
        if changes:
            shas = [str(a.attrs["sha"]).lower() for a in changes]
            recorded = set(parse_linkage(self.existing(GIT_COMMITS)) or ())
            fresh = [a for a in changes if str(a.attrs["sha"]).lower() not in recorded] or changes
            newest = Moment.of(str(fresh[-1].attrs["committed_at"]), fresh[-1].key)
            self.append(GIT_COMMITS, shas, _When.of(newest), [a.key for a in fresh], DERIVED)
        if unit.kind == ATTACH:  # partial evidence of an existing bead, not its whole life
            pass
        elif self.created is not None:
            self.stamp(CREATED_AT, self.created.at, self.created, [self.created.source], DERIVED)
        if self.closed is not None and unit.kind != ATTACH:
            self.stamp(CLOSED_AT, self.closed.at, self.closed, [self.closed.source], DERIVED)
        tokens, where, trackers = self.p.foreign(unit)
        for ledger, found in tokens.items():
            sources = [s for t in found for s in where[f"{ledger}\0{t}"]]
            self.append(f"{XREF_PREFIX}{ledger}", found, self.create_at, sources, DERIVED)
        if trackers:
            sources = [s for t in trackers for s in (*where[f"tracker\0{t}"], t)]
            self.append(XREF_TRACKER, trackers, self.create_at, sources, DERIVED)
        release = self.p.release(unit)
        if release is not None:
            when = release.attrs["released_at"]
            moment = None if when is None else Moment.of(str(when), release.key)
            self.stamp(RELEASE, str(release.attrs["version"]), moment, [release.key], DERIVED)
        conflicts = conflicts_of(self.p.collation, unit.key)
        if conflicts:
            value = canonical_json(
                [
                    {
                        "rule": c.rule,
                        "detail": c.detail,
                        "sources": [
                            {"artifact": s.artifact, "field": s.field, "value": s.value}
                            for s in c.sources
                        ],
                    }
                    for c in conflicts
                ]
            )
            keys = [s.artifact for c in conflicts for s in c.sources]
            self.stamp(CONFLICT, value, None, keys, DERIVED, when=self.last())

    def last(self) -> _When:
        return _When.of(self.closed or self.created)


def project(
    collation: Collation,
    corpus: EvidenceCorpus,
    baseline: Iterable[BeadEvent],
    *,
    prefix: str,
    group: str,
    extracted_at: str,
    options: ProjectOptions | None = None,
) -> Projection:
    """Phase C: the proposed BeadLog events of ``collation``'s units (see the module doc).

    ``corpus`` is the evidence corpus and ``baseline`` the BeadLog the collation was made from;
    ``prefix`` the explicit id prefix of minted beads (required, never derived); ``group`` the
    BeadLog group the beads live in; ``extracted_at`` the canonical timestamp stamped on every
    event (not identity, so re-projection mints the same ids).
    """
    if not isinstance(prefix, str) or _PREFIX.fullmatch(prefix) is None:
        raise ProjectError(
            "prefix must be an explicit bead id prefix (prefixes are never derived), "
            f"got {prefix!r}"
        )
    if not isinstance(group, str) or not group:
        raise ProjectError("group must be a non-empty BeadLog group id")
    check_timestamp(extracted_at)
    return _Projector(
        collation,
        corpus,
        tuple(baseline),
        prefix=prefix,
        group=group,
        extracted_at=extracted_at,
        options=options or ProjectOptions(),
    ).run()


__all__ = [
    "ANNOTATION",
    "CLOSED_AT",
    "CONFLICT",
    "CREATED_AT",
    "DECISIONS",
    "DEFAULT_SOURCE_LABELS",
    "DEFAULT_TYPES",
    "EVENT_KINDS",
    "GIT_COMMITS",
    "RELEASE",
    "SOURCE_SYSTEM",
    "XREF_PREFIX",
    "XREF_TRACKER",
    "Kept",
    "ProjectError",
    "ProjectOptions",
    "Projection",
    "UnitProjection",
    "project",
]
