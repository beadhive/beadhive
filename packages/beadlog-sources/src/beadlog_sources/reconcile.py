"""Reconcile a proposed BeadLog against a baseline BeadLog into a plan (platform ADR §1-2).

``reconcile(proposed, baseline, judge)`` needs no store: "existing beads" is the baseline log.
It is built on :func:`beadlog.diff` and classifies every proposed bead the diff touches:

- **PRESENT** — nothing to apply: the baseline already holds the bead and merging the proposal
  changes none of its facets, or the bead is new by id but a judge matched it to an existing
  baseline bead (``match``).
- **DRIFT** — the baseline holds the bead and the proposal changes facets (``changed``); its
  missing events go into the plan.
- **NOISE** — the proposal is already covered by a coarser epic of the baseline
  (``covered_by``). It is only ever offered, and only ever accepted, for a bead whose folded
  type in the **baseline** is ``epic``, so NOISE requires a covering-epic event there.
- **NEW** — no baseline bead represents it; its events go into the plan.

Beads unknown to the baseline by id are the fuzzy residual: candidates are baseline beads whose
titles share tokens with the proposal (and any epic the proposal names as its parent), asked as
one ``ChoiceQuery`` of family :data:`MATCH_FAMILY`. The deterministic engine answers first; the
configured judge is asked only when it abstains. Every verdict (engine, model, confidence,
rationale, fallback provenance) is recorded in the plan and is reused from a ``prior`` plan by
its query hash, so a recorded verdict is never asked again.

The plan is a ``.bdlg`` archive: its events are the NEW and DRIFT events, its manifest ``plan``
section carries ``pass``, ``schema_version``, ``corpus_hash``, ``plan_hash`` and the judge
identity, and the per-bead classification with each verdict lives in manifest annotation
:data:`PLAN_ANNOTATION` (the manifest ``plan`` object is closed by the v0.1 schema).
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import IO, Any

from beadlog import (
    BeadEvent,
    BeadRef,
    BeadState,
    Generator,
    Group,
    Manifest,
    canonical_json,
    diff,
    fold,
    read_archive,
    write_archive,
)

from .contracts import ABSTAIN, ChoiceQuery, Fallback, Judgment, JudgmentPort
from .judges import DeterministicJudge, jaccard, tokens

PASS = "reconcile"
SCHEMA_VERSION = "bdlg.reconcile/1"
PLAN_ANNOTATION = "bdlg.reconcile"
MATCH_FAMILY = "reconcile.match"
NEW_KEY = "new"
MAX_CANDIDATES = 5
MATCH_QUESTION = (
    "A backfill proposes the bead described in the context. Which option is true? "
    "'present:N' means existing bead N already represents the same work; 'noise:N' means the "
    "work is already covered by the coarser existing epic N, so no bead should be created; "
    "'new' means no existing bead represents it; 'abstain' means you cannot tell."
)
NEW_TEXT = "None of these: this is new work that no existing bead represents."
ABSTAIN_TEXT = "Cannot tell from the information given."
_CONTEXT_LIMIT = 4000
_EMPTY: Mapping[str, Any] = MappingProxyType({})


class ReconcileClass(StrEnum):
    """The four reconcile outcomes."""

    NEW = "NEW"
    PRESENT = "PRESENT"
    NOISE = "NOISE"
    DRIFT = "DRIFT"


class Review(StrEnum):
    """Whether an entry needs a human: ``none`` (decided structurally, by bead id), ``auto``
    (a judged verdict at or above its family's ``auto_accept`` threshold) or ``required``."""

    NONE = "none"
    AUTO = "auto"
    REQUIRED = "required"


@dataclass(frozen=True)
class Verdict:
    """A recorded judgment: the query identity (``family``, ``query_hash``) and the answer."""

    family: str
    query_hash: str
    judgment: Judgment

    def to_json(self) -> dict[str, Any]:
        judgment = self.judgment
        fallback = judgment.fallback
        return {
            "family": self.family,
            "query_hash": self.query_hash,
            "outcome": judgment.outcome,
            "probabilities": {k: judgment.probabilities[k] for k in sorted(judgment.probabilities)},
            "confidence": judgment.confidence,
            "option_count": judgment.option_count,
            "rationale": judgment.rationale,
            "engine": judgment.engine,
            "engine_version": judgment.engine_version,
            "model": judgment.model,
            "fallback": (
                None
                if fallback is None
                else {
                    "engine": fallback.engine,
                    "reason": fallback.reason,
                    "detail": fallback.detail,
                }
            ),
        }

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> Verdict:
        raw = value.get("fallback")
        fallback = None if raw is None else Fallback(raw["engine"], raw["reason"], raw["detail"])
        return cls(
            family=value["family"],
            query_hash=value["query_hash"],
            judgment=Judgment(
                outcome=value["outcome"],
                probabilities=dict(value["probabilities"]),
                confidence=float(value["confidence"]),
                option_count=int(value["option_count"]),
                rationale=value["rationale"],
                engine=value["engine"],
                engine_version=value["engine_version"],
                model=value.get("model"),
                fallback=fallback,
            ),
        )


def _ref_json(ref: BeadRef) -> dict[str, str]:
    return {"group": ref.group, "id": ref.id}


@dataclass(frozen=True)
class PlanEntry:
    """The classification of one proposed bead."""

    bead: BeadRef
    cls: ReconcileClass
    review: Review
    events: tuple[str, ...] = ()
    changed: tuple[str, ...] = ()
    match: BeadRef | None = None
    covered_by: BeadRef | None = None
    verdict: Verdict | None = None
    note: str = ""

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "bead": _ref_json(self.bead),
            "class": str(self.cls),
            "review": str(self.review),
            "events": list(self.events),
            "changed": list(self.changed),
        }
        if self.match is not None:
            out["match"] = _ref_json(self.match)
        if self.covered_by is not None:
            out["covered_by"] = _ref_json(self.covered_by)
        if self.verdict is not None:
            out["verdict"] = self.verdict.to_json()
        if self.note:
            out["note"] = self.note
        return out

    @classmethod
    def from_json(cls, value: Mapping[str, Any]) -> PlanEntry:
        def ref(item: Any) -> BeadRef | None:
            return None if item is None else BeadRef(item["group"], item["id"])

        verdict = value.get("verdict")
        return cls(
            bead=BeadRef(value["bead"]["group"], value["bead"]["id"]),
            cls=ReconcileClass(value["class"]),
            review=Review(value["review"]),
            events=tuple(value.get("events", ())),
            changed=tuple(value.get("changed", ())),
            match=ref(value.get("match")),
            covered_by=ref(value.get("covered_by")),
            verdict=None if verdict is None else Verdict.from_json(verdict),
            note=value.get("note", ""),
        )


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _json_hash(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return _digest(text.encode("utf-8"))


def corpus_hash(events: Iterable[BeadEvent]) -> str:
    """Hash of a log's sorted event ids (which events a plan was reconciled against)."""
    return _digest(canonical_json(sorted({event.id for event in events})).encode("utf-8"))


def query_hash(query: ChoiceQuery) -> str:
    """Identity of a choice query: family, question, options and context."""
    material = {
        "family": query.family,
        "kind": "choice",
        "question": query.question,
        "options": dict(query.options),
        "context": query.context,
    }
    return _digest(canonical_json(material).encode("utf-8"))


@dataclass(frozen=True)
class ReconcilePlan:
    """The result of :func:`reconcile`: plan events (NEW and DRIFT) and one entry per bead."""

    events: tuple[BeadEvent, ...]
    entries: tuple[PlanEntry, ...]
    corpus_hash: str
    proposed_hash: str
    judge: Mapping[str, Any] = field(default=_EMPTY, hash=False)
    calls: int = 0

    @property
    def empty(self) -> bool:
        """True when the plan proposes nothing: no events and nothing judged."""
        return not self.events and all(
            entry.cls is ReconcileClass.PRESENT and entry.verdict is None for entry in self.entries
        )

    def counts(self) -> dict[str, int]:
        out = {str(cls): 0 for cls in ReconcileClass}
        for entry in self.entries:
            out[str(entry.cls)] += 1
        return out

    def verdicts(self) -> dict[str, Verdict]:
        """Recorded verdicts by query hash (pass as ``prior`` to never re-query them)."""
        return {e.verdict.query_hash: e.verdict for e in self.entries if e.verdict is not None}

    def annotation(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "proposed_hash": self.proposed_hash,
            "counts": self.counts(),
            "review_required": sum(e.review is Review.REQUIRED for e in self.entries),
            "entries": [entry.to_json() for entry in self.entries],
        }

    def plan_hash(self) -> str:
        return _json_hash(
            {
                "corpus_hash": self.corpus_hash,
                "events": sorted(event.id for event in self.events),
                "reconcile": self.annotation(),
            }
        )

    def plan_section(self) -> dict[str, Any]:
        return {
            "pass": PASS,
            "schema_version": SCHEMA_VERSION,
            "corpus_hash": self.corpus_hash,
            "plan_hash": self.plan_hash(),
            "judge": dict(self.judge),
        }


def _title(state: BeadState | None) -> str:
    if state is None:
        return ""
    value = state.fields.get("title")
    return value if isinstance(value, str) else ""


def _context(state: BeadState | None) -> str:
    """Subject line (the title) first, then type and description for an engine that reads more."""
    title = _title(state)
    lines = [title]
    if state is not None:
        if state.type:
            lines.append(f"type: {state.type}")
        description = state.fields.get("description")
        if isinstance(description, str) and description:
            lines.append(f"description: {description}")
    return "\n".join(lines)[:_CONTEXT_LIMIT]


def _live(state: BeadState) -> bool:
    return state.exists and not state.deleted and not state.erased


def _ranked(
    subject: frozenset[str], states: Iterable[BeadState], limit: int
) -> list[tuple[float, BeadState]]:
    scored = [(jaccard(subject, tokens(_title(state))), state) for state in states]
    scored = [(score, state) for score, state in scored if score > 0]
    scored.sort(key=lambda item: (-item[0], item[1].bead))
    return scored[:limit]


def _parents(state: BeadState | None) -> list[BeadRef]:
    if state is None:
        return []
    return sorted(edge.target for edge in state.edges if edge.type == "parent-child")


def _judge_identity(judge: JudgmentPort | None) -> dict[str, Any]:
    engine = judge if judge is not None else DeterministicJudge()
    return {
        "engine": str(getattr(engine, "engine", type(engine).__name__)),
        "version": str(getattr(engine, "engine_version", "0")),
        "model": getattr(engine, "model", None),
    }


def _review(verdict: Judgment, family: str, auto_accept: Mapping[str, float]) -> Review:
    threshold = auto_accept.get(family)
    if verdict.outcome is None or verdict.fallback is not None or threshold is None:
        return Review.REQUIRED
    return Review.AUTO if verdict.confidence >= threshold else Review.REQUIRED


def reconcile(
    proposed: Iterable[BeadEvent],
    baseline: Iterable[BeadEvent],
    judge: JudgmentPort | None = None,
    *,
    auto_accept: Mapping[str, float] | None = None,
    prior: Mapping[str, Verdict] | None = None,
    max_candidates: int = MAX_CANDIDATES,
) -> ReconcilePlan:
    """Classify ``proposed`` against ``baseline`` (NEW / PRESENT / NOISE / DRIFT).

    ``judge`` answers the fuzzy residual after the deterministic engine abstains (``None`` or a
    :class:`~beadlog_sources.judges.DeterministicJudge`: deterministic only). ``auto_accept``
    maps a family to the confidence at which a judged verdict needs no review. ``prior`` maps
    query hashes to verdicts already recorded in an earlier plan; those are reused, never asked
    again. ``reconcile(x, x)`` proposes nothing.
    """
    proposed = tuple(proposed)
    baseline = tuple(baseline)
    auto_accept = dict(auto_accept or {})
    prior = dict(prior or {})
    lexical = DeterministicJudge()
    asks_judge = judge is not None and not isinstance(judge, DeterministicJudge)
    calls = 0

    delta_log = diff(proposed, baseline)
    current = fold(baseline)
    live = sorted(
        (state for state in current.values() if _live(state) and _title(state)),
        key=lambda state: state.bead,
    )
    epics = [state for state in live if state.type == "epic"]
    epic_refs = {state.bead for state in epics}

    entries: list[PlanEntry] = []
    planned: list[BeadEvent] = []
    for delta in delta_log.deltas:
        if not delta.events:
            continue
        ids = tuple(event.id for event in delta.events)
        if delta.baseline is not None:
            if not delta.changed:
                entries.append(
                    PlanEntry(delta.bead, ReconcileClass.PRESENT, Review.NONE, note="same facets")
                )
                continue
            entries.append(
                PlanEntry(delta.bead, ReconcileClass.DRIFT, Review.NONE, ids, delta.changed)
            )
            planned.extend(delta.events)
            continue

        state = delta.proposed or delta.merged
        subject = tokens(_title(state))
        present = _ranked(subject, live, max_candidates)
        parents = [ref for ref in _parents(state) if ref in epic_refs]
        noise_states = [current[ref] for ref in parents]
        for _, epic in _ranked(subject, epics, max_candidates):
            if epic.bead not in parents:
                noise_states.append(epic)
        noise_states = noise_states[:max_candidates]
        if not present and not noise_states:
            entries.append(
                PlanEntry(
                    delta.bead,
                    ReconcileClass.NEW,
                    Review.NONE,
                    ids,
                    delta.changed,
                    note="no baseline candidate",
                )
            )
            planned.extend(delta.events)
            continue

        options: dict[str, str] = {}
        targets: dict[str, tuple[ReconcileClass, BeadRef]] = {}
        for number, (_, candidate) in enumerate(present, start=1):
            options[f"present:{number}"] = _title(candidate)
            targets[f"present:{number}"] = (ReconcileClass.PRESENT, candidate.bead)
        for number, epic in enumerate(noise_states, start=1):
            options[f"noise:{number}"] = _title(epic)
            targets[f"noise:{number}"] = (ReconcileClass.NOISE, epic.bead)
        options[NEW_KEY] = NEW_TEXT
        options[ABSTAIN] = ABSTAIN_TEXT
        query = ChoiceQuery(MATCH_QUESTION, options, _context(state), MATCH_FAMILY)
        key = query_hash(query)

        if key in prior:
            verdict = prior[key].judgment
        else:
            verdict = lexical.choose(query)
            if verdict.outcome is None and asks_judge:
                verdict = judge.choose(query)  # type: ignore[union-attr]
                calls += 1
            if verdict.outcome is not None and verdict.outcome not in options:
                verdict = Judgment(
                    None,
                    verdict.probabilities,
                    0.0,
                    verdict.option_count,
                    f"refused outcome {verdict.outcome!r}: not an offered option",
                    verdict.engine,
                    verdict.engine_version,
                    verdict.model,
                    verdict.fallback,
                )
        record = Verdict(MATCH_FAMILY, key, verdict)
        review = _review(verdict, MATCH_FAMILY, auto_accept)
        target = targets.get(verdict.outcome or "")
        if target is None:
            note = "judged new" if verdict.outcome == NEW_KEY else "judge abstained"
            entries.append(
                PlanEntry(
                    delta.bead,
                    ReconcileClass.NEW,
                    review,
                    ids,
                    delta.changed,
                    verdict=record,
                    note=note,
                )
            )
            planned.extend(delta.events)
        elif target[0] is ReconcileClass.PRESENT:
            entries.append(
                PlanEntry(
                    delta.bead, ReconcileClass.PRESENT, review, match=target[1], verdict=record
                )
            )
        else:
            entries.append(
                PlanEntry(
                    delta.bead, ReconcileClass.NOISE, review, covered_by=target[1], verdict=record
                )
            )

    return ReconcilePlan(
        events=tuple(planned),
        entries=tuple(entries),
        corpus_hash=corpus_hash(baseline),
        proposed_hash=corpus_hash(proposed),
        judge=_judge_identity(judge),
        calls=calls,
    )


def write_plan(
    destination: str | os.PathLike[str] | IO[bytes],
    plan: ReconcilePlan,
    *,
    groups: Sequence[Group],
    generator: Generator,
    description: str | None = None,
) -> Manifest:
    """Write ``plan`` as a ``.bdlg`` plan archive (capability ``plan``)."""
    return write_archive(
        destination,
        plan.events,
        groups=groups,
        generator=generator,
        plan=plan.plan_section(),
        annotations={PLAN_ANNOTATION: plan.annotation()},
        description=description,
    )


def plan_entries(manifest: Manifest) -> tuple[PlanEntry, ...]:
    """The recorded reconcile entries of a plan archive's manifest (empty for other archives)."""
    annotation = (manifest.annotations or {}).get(PLAN_ANNOTATION)
    if not isinstance(annotation, Mapping):
        return ()
    return tuple(PlanEntry.from_json(entry) for entry in annotation.get("entries", ()))


def read_plan(source: str | os.PathLike[str] | IO[bytes]) -> tuple[Manifest, tuple[PlanEntry, ...]]:
    """Open and verify a plan archive; return its manifest and recorded entries."""
    manifest, _ = read_archive(source)
    return manifest, plan_entries(manifest)


def prior_verdicts(entries: Iterable[PlanEntry]) -> dict[str, Verdict]:
    """Verdicts by query hash, to pass as ``reconcile(prior=...)``."""
    return {e.verdict.query_hash: e.verdict for e in entries if e.verdict is not None}


__all__ = [
    "ABSTAIN_TEXT",
    "MATCH_FAMILY",
    "MATCH_QUESTION",
    "MAX_CANDIDATES",
    "NEW_KEY",
    "NEW_TEXT",
    "PASS",
    "PLAN_ANNOTATION",
    "SCHEMA_VERSION",
    "PlanEntry",
    "ReconcileClass",
    "ReconcilePlan",
    "Review",
    "Verdict",
    "corpus_hash",
    "plan_entries",
    "prior_verdicts",
    "query_hash",
    "read_plan",
    "reconcile",
    "write_plan",
]
