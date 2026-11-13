"""Phase B5 of the backfill pipeline: identify work units against the baseline.

:func:`identify` runs over a B4 :class:`~beadlog_sources.collate.Collation` and the baseline
BeadLog it was collated against (``docs/design/backfill-history-pipeline-adr.md``, B5). Every
unit, in key order, goes through these steps; the first that decides wins:

0. ``decision``: an operator decision recorded in the bead store (:mod:`beadlog_sources.decisions`,
   ``bh backfill decide``) for the unit's key: ``link`` is PRESENT against the named bead,
   ``noise`` is NOISE against the named epic, ``skip`` is class ``SKIP`` (projected to nothing,
   never residual); provenance ``derived``, review ``none``. A ``link`` / ``noise`` whose target
   is gone (or, for ``noise``, is not an epic) is stale: reported through :attr:`IdentifyRun.warn`
   and not applied, the unit goes on to step 1. Decision beads themselves are never candidates and
   never answer an ``external_ref`` lookup.
1. ``external-ref``: a live baseline bead whose ``external_ref`` is the unit's key, the path of a
   ``doc:<path>`` key, or one of the unit's artifact keys: PRESENT, provenance ``derived``.
2. ``explicit-id``: an ``attach`` unit names its local bead (B2/B3 resolved it): PRESENT.
3. ``no-candidate``: the baseline holds no bead (and no epic) sharing a title token with the unit,
   so there is nothing it could be present as or covered by: NEW, provenance ``derived``,
   confidence null (a rule decided it, nothing was estimated), always reviewed. Units WITH a
   candidate keep the flow below.
4. ``lexical``: the deterministic engine (:class:`~beadlog_sources.judges.DeterministicJudge`)
   over one :class:`~beadlog_sources.contracts.ChoiceQuery` shaped like reconcile's match query
   (``present:N`` for the baseline beads whose titles share the most tokens with the unit,
   ``noise:N`` for the best-matching baseline epics, ``new``, ``abstain``).
5. ``judge``: the configured :class:`~beadlog_sources.contracts.JudgmentPort` when it is enabled
   (not ``None`` and not the deterministic engine), asked the same query. A unit with no baseline
   candidate is never sent (there is nothing to choose between).
6. ``threshold``: when :attr:`IdentifyOptions.new_threshold` is set, a unit whose fuzzy score (the
   best title-token Jaccard against the baseline, in percent) is below it is NEW, provenance
   ``inferred``, confidence ``1 - score``, always reviewed. B5 mints no labels: label policy for
   created beads belongs to Phase C (``origin:backfill`` + ``source:<kind>``, D1); step, score,
   provenance and confidence carry what it needs.
7. otherwise the unit goes to ``residual`` for a human.

With the defaults (judge off, no threshold) the bridges and the lexical engine can still decide
PRESENT, and a unit with no candidate at all is NEW (derived); every other unit is residual.
Lexical NOISE effectively never fires: a matching epic is offered both as ``present:N`` and
``noise:M`` with the same score, and the deterministic engine abstains on that tie (only a judge
picks NOISE).

Every decision, and every judge answer (abstentions included), is a verdict record
(:data:`VERDICT_MEMBERS`) in the collation's ``verdicts``, sorted by unit then step: a judge
abstention followed by a threshold NEW is two records, and a unit's decision is its record with
a non-null ``class``. Every undecided unit is a residual record (:data:`RESIDUAL_MEMBERS`).
Probabilities and confidence are integers per mille, because the collation's canonical JSON
admits no floats.

**Resume.** A judge verdict is keyed by its query hash
(:func:`beadlog_sources.reconcile.query_hash`).
:attr:`IdentifyRun.prior` maps query hashes to verdicts recorded earlier (:func:`prior_verdicts`
reads them from a previous collation); a recorded query is reused and never asked again, and the
reused verdict is recorded again so the chain carries on. Verdicts that fell back (timeout,
malformed answer ...) are not reused, so a transient failure is retried. The judge is asked at
most ``max_calls_per_run`` times; units past the cap stay in ``residual`` with reason
``judge-budget`` (no threshold applies to them) and the next run, fed this collation as its
prior, continues where this one stopped.

**Cost preview.** Before the first call, the units that would go to the judge are counted and a
:class:`CostPreview` (``N judge calls ≈ $X``) is handed to :attr:`IdentifyRun.preview` (default:
written to stderr). When ``N`` exceeds ``max_calls_per_run`` the run refuses with
:class:`IdentifyConfirmationRequired` unless :attr:`IdentifyRun.confirm` is set, still before any
call. With the judge off nothing is previewed and nothing is ever sent anywhere.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from beadlog import BeadEvent, BeadState, fold

from ._sources import ROLLBACK_REASON
from .collate import ATTACH, Collation, CollationFormatError, Unit
from .contracts import ABSTAIN, ChoiceQuery, Fallback, Judgment, JudgmentPort
from .decisions import LINK, NOISE, SKIP, Decision, decisions_of, is_decision
from .docs import ask
from .judges import (
    DEFAULT_MAX_CALLS_PER_RUN,
    DeterministicJudge,
    JudgeSettings,
    build_judge,
    jaccard,
    tokens,
)
from .judges import Runner as JudgeRunner
from .reconcile import (
    ABSTAIN_TEXT,
    NEW_KEY,
    NEW_TEXT,
    ReconcileClass,
    Review,
    Verdict,
    _review,
    query_hash,
)

FAMILY = "collate.identify"
QUESTION = (
    "A backfill found the unit of work described in the context. Which option is true? "
    "'present:N' means existing bead N already represents the same work; 'noise:N' means the "
    "work is already covered by the coarser existing epic N, so no bead should be created; "
    "'new' means no existing bead represents it; 'abstain' means you cannot tell."
)

STEP_DECISION = "decision"
STEP_EXTERNAL_REF = "external-ref"
STEP_EXPLICIT_ID = "explicit-id"
STEP_LEXICAL = "lexical"
STEP_JUDGE = "judge"
STEP_NO_CANDIDATE = "no-candidate"
STEP_THRESHOLD = "threshold"
STEPS = (
    STEP_DECISION,
    STEP_EXTERNAL_REF,
    STEP_EXPLICIT_ID,
    STEP_NO_CANDIDATE,
    STEP_LEXICAL,
    STEP_JUDGE,
    STEP_THRESHOLD,
)

CLASS_SKIP = "SKIP"  # an operator decision (step 0): nothing is projected, the unit is not residual
CLASSES = (
    str(ReconcileClass.NEW),
    str(ReconcileClass.PRESENT),
    str(ReconcileClass.NOISE),
    CLASS_SKIP,
)
PROVENANCES = ("derived", "inferred")
REVIEWS = tuple(str(r) for r in Review)

# Why a unit is residual: nothing decided it (judge off, or no judge candidate); the judge
# abstained (or its answer was refused); the judge failed and fell back; the call cap stopped it.
REASON_NO_MATCH = "no-match"
REASON_JUDGE_ABSTAINED = "judge-abstained"
REASON_JUDGE_FAILED = "judge-failed"
REASON_JUDGE_BUDGET = "judge-budget"
RESIDUAL_REASONS = (
    REASON_NO_MATCH,
    REASON_JUDGE_ABSTAINED,
    REASON_JUDGE_FAILED,
    REASON_JUDGE_BUDGET,
)

THRESHOLD_ENGINE = "identify-threshold"
BRIDGE_ENGINE = "identify-bridge"
ENGINE_VERSION = "1"
OPTION_MEMBERS = ("new_threshold", "max_candidates")
DEFAULT_MAX_CANDIDATES = 5
PERMILLE = 1000
_CONTEXT_LIMIT = 4000
_MAX_CONTEXT_ARTIFACTS = 8


class IdentifyOptionsError(ValueError):
    """A B5 option outside its domain."""


class IdentifyConfirmationRequired(RuntimeError):
    """More judge calls are due than ``max_calls_per_run`` and the run was not confirmed."""

    def __init__(self, preview: CostPreview) -> None:
        self.preview = preview
        super().__init__(
            f"{preview}; more than max_calls_per_run={preview.max_calls_per_run}: confirm to "
            "judge the first batch now and resume the rest on the next run"
        )


def _int_in(value: Any, name: str, low: int, high: int | None = None) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < low
        or (high is not None and value > high)
    ):
        span = f"{low}..{high}" if high is not None else f">= {low}"
        raise IdentifyOptionsError(f"{name} must be an integer {span}")
    return value


@dataclass(frozen=True)
class IdentifyOptions:
    """B5 options (``backfill.collate``; recorded as the collation's ``options.identify``).

    - ``new_threshold``: integer percent ``1..100``, or ``None`` (default, the threshold step is
      off). A unit nothing else decided whose fuzzy score is below it is NEW (inferred).
    - ``max_candidates``: how many baseline beads (and, separately, epics) the match query offers
      (default 5).
    """

    new_threshold: int | None = None
    max_candidates: int = DEFAULT_MAX_CANDIDATES

    def __post_init__(self) -> None:
        if self.new_threshold is not None:
            _int_in(self.new_threshold, "new_threshold", 1, 100)
        _int_in(self.max_candidates, "max_candidates", 1)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> IdentifyOptions:
        """Options from a ``backfill.collate`` table; other members are ignored."""
        return cls(**{name: raw[name] for name in OPTION_MEMBERS if name in raw})

    def to_json(self) -> dict[str, Any]:
        """The effective options as a JSON object (the collation's ``options.identify``)."""
        return {"new_threshold": self.new_threshold, "max_candidates": self.max_candidates}

    @classmethod
    def from_json(cls, raw: Any) -> IdentifyOptions:
        """Parse :meth:`to_json` output; missing or unknown members are refused."""
        if not isinstance(raw, dict) or sorted(raw) != sorted(OPTION_MEMBERS):
            raise IdentifyOptionsError(f"identify options must have exactly {OPTION_MEMBERS}")
        return cls(**raw)


@dataclass(frozen=True)
class CostPreview:
    """What the judge step is about to cost, computed before any call.

    ``calls`` units would go to the judge (``reused`` more were answered from the prior);
    ``max_calls_per_run`` is the cap this run spends at most; ``cost_per_call_usd`` the per-call
    estimate (``backfill.judge.cost_per_call_usd``), ``None`` when not configured.
    """

    calls: int
    max_calls_per_run: int
    cost_per_call_usd: float | None = None
    reused: int = 0

    @property
    def estimated_usd(self) -> float | None:
        """The estimate for every pending call (``None`` without a per-call cost)."""
        return None if self.cost_per_call_usd is None else self.calls * self.cost_per_call_usd

    @property
    def this_run(self) -> int:
        """The calls this run makes: the pending calls up to the cap."""
        return min(self.calls, self.max_calls_per_run)

    @property
    def needs_confirm(self) -> bool:
        """True when more calls are pending than one run may make."""
        return self.calls > self.max_calls_per_run

    def __str__(self) -> str:
        cost = self.estimated_usd
        money = "$?" if cost is None else f"${cost:.2f}"
        text = f"{self.calls} judge calls ≈ {money}"
        details = [f"max {self.max_calls_per_run} per run"]
        if self.reused:
            details.append(f"{self.reused} reused from the prior collation")
        if cost is None:
            details.append("no cost_per_call_usd configured")
        return f"{text} ({'; '.join(details)})"


def print_preview(preview: CostPreview) -> None:
    """The default preview sink: one line on stderr."""
    sys.stderr.write(f"identify: {preview}\n")
    sys.stderr.flush()


@dataclass(frozen=True)
class IdentifyRun:
    """The runtime side of B5 (never serialized; nothing here changes a decided verdict).

    - ``judge``: the configured engine; ``None`` or the deterministic engine is "judge off".
    - ``prior``: query hash to verdict recorded earlier (:func:`prior_verdicts`).
    - ``auto_accept``: family to the confidence at which a verdict needs no review.
    - ``max_calls_per_run``: the call cap; ``None`` takes the judge's own
      ``max_calls_per_run``, else :data:`~beadlog_sources.judges.DEFAULT_MAX_CALLS_PER_RUN`.
    - ``cost_per_call_usd``: the per-call estimate for the preview (passed in by the caller).
    - ``confirm``: allow a run whose pending calls exceed the cap (it still spends at most the cap).
    - ``preview``: where the :class:`CostPreview` goes (default :func:`print_preview`).
    - ``warn``: where a stale operator decision is reported (default: a line on stderr).
    """

    judge: JudgmentPort | None = None
    prior: Mapping[str, Verdict] = field(default_factory=dict)
    auto_accept: Mapping[str, float] = field(default_factory=dict)
    max_calls_per_run: int | None = None
    cost_per_call_usd: float | None = None
    confirm: bool = False
    preview: Callable[[CostPreview], None] | None = None
    warn: Callable[[str], None] | None = None

    def __post_init__(self) -> None:
        if self.max_calls_per_run is not None and self.max_calls_per_run < 0:
            raise ValueError("max_calls_per_run must not be negative")
        if self.cost_per_call_usd is not None and not self.cost_per_call_usd >= 0:
            raise ValueError("cost_per_call_usd must not be negative")

    @classmethod
    def from_settings(
        cls,
        settings: JudgeSettings,
        *,
        runner: JudgeRunner | None = None,
        prior: Mapping[str, Verdict] | None = None,
        cost_per_call_usd: float | None = None,
        confirm: bool = False,
        preview: Callable[[CostPreview], None] | None = None,
    ) -> IdentifyRun:
        """A run from the ``backfill.judge`` settings (the judge built by :func:`build_judge`)."""
        return cls(
            judge=build_judge(settings, runner=runner) if settings.enabled else None,
            prior=dict(prior or {}),
            auto_accept=dict(settings.auto_accept),
            max_calls_per_run=settings.max_calls_per_run,
            cost_per_call_usd=cost_per_call_usd,
            confirm=confirm,
            preview=preview,
        )

    @property
    def judging(self) -> bool:
        """True when a non-deterministic judge is configured."""
        return self.judge is not None and not isinstance(self.judge, DeterministicJudge)

    def cap(self) -> int:
        if self.max_calls_per_run is not None:
            return self.max_calls_per_run
        own = getattr(self.judge, "max_calls_per_run", None)
        return own if isinstance(own, int) else DEFAULT_MAX_CALLS_PER_RUN


# -- records ---------------------------------------------------------------------------------

VERDICT_MEMBERS = (
    "unit",
    "step",
    "class",
    "bead",
    "provenance",
    "review",
    "score",
    "family",
    "query_hash",
    "outcome",
    "probabilities_permille",
    "confidence_permille",
    "option_count",
    "rationale",
    "engine",
    "engine_version",
    "model",
    "fallback",
)
RESIDUAL_MEMBERS = ("unit", "reason", "score", "query_hash", "candidates")
_FALLBACK_MEMBERS = ("engine", "reason", "detail")


def _permille(value: float) -> int:
    return max(0, min(PERMILLE, round(value * PERMILLE)))


def _verdict_json(
    unit: str,
    step: str,
    judgment: Judgment,
    *,
    cls: str | None,
    bead: str | None,
    provenance: str,
    review: Review,
    score: int | None,
    query: str | None = None,
    family: str | None = None,
) -> dict[str, Any]:
    fallback = judgment.fallback
    return {
        "unit": unit,
        "step": step,
        "class": cls,
        "bead": bead,
        "provenance": provenance,
        "review": str(review),
        "score": score,
        "family": family,
        "query_hash": query,
        "outcome": judgment.outcome,
        "probabilities_permille": {
            k: _permille(judgment.probabilities[k]) for k in sorted(judgment.probabilities)
        },
        "confidence_permille": _permille(judgment.confidence),
        "option_count": judgment.option_count,
        "rationale": judgment.rationale,
        "engine": judgment.engine,
        "engine_version": judgment.engine_version,
        "model": judgment.model,
        "fallback": (
            None
            if fallback is None
            else {"engine": fallback.engine, "reason": fallback.reason, "detail": fallback.detail}
        ),
    }


def _bad(what: str) -> CollationFormatError:
    return CollationFormatError(f"identify: {what}")


def check_verdict(raw: Any) -> dict[str, Any]:
    """Validate one verdict record (strictly, as the collation loader does); return it."""
    if not isinstance(raw, dict) or sorted(raw) != sorted(VERDICT_MEMBERS):
        raise _bad(f"verdict must have exactly {VERDICT_MEMBERS}")
    if not isinstance(raw["unit"], str) or not raw["unit"]:
        raise _bad("verdict unit must be a non-empty string")
    if raw["step"] not in STEPS:
        raise _bad(f"verdict {raw['unit']!r}: unknown step {raw['step']!r}")
    if raw["class"] is not None and raw["class"] not in CLASSES:
        raise _bad(f"verdict {raw['unit']!r}: unknown class {raw['class']!r}")
    if raw["provenance"] not in PROVENANCES or raw["review"] not in REVIEWS:
        raise _bad(f"verdict {raw['unit']!r}: bad provenance or review")
    fallback = raw["fallback"]
    if fallback is not None and (
        not isinstance(fallback, dict) or sorted(fallback) != sorted(_FALLBACK_MEMBERS)
    ):
        raise _bad(f"verdict {raw['unit']!r}: fallback must have exactly {_FALLBACK_MEMBERS}")
    for name in ("confidence_permille", "option_count"):
        if name == "confidence_permille" and raw[name] is None and raw["step"] == STEP_NO_CANDIDATE:
            continue  # a rule decided it: no estimate to carry
        if not isinstance(raw[name], int) or isinstance(raw[name], bool) or raw[name] < 0:
            raise _bad(f"verdict {raw['unit']!r}: {name} must be a non-negative integer")
    probabilities = raw["probabilities_permille"]
    if not isinstance(probabilities, dict) or not all(
        isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= PERMILLE
        for v in probabilities.values()
    ):
        raise _bad(f"verdict {raw['unit']!r}: probabilities_permille must map to 0..1000")
    return raw


def check_residual(raw: Any) -> dict[str, Any]:
    """Validate one residual record; return it."""
    if not isinstance(raw, dict) or sorted(raw) != sorted(RESIDUAL_MEMBERS):
        raise _bad(f"residual must have exactly {RESIDUAL_MEMBERS}")
    if not isinstance(raw["unit"], str) or not raw["unit"]:
        raise _bad("residual unit must be a non-empty string")
    if raw["reason"] not in RESIDUAL_REASONS:
        raise _bad(f"residual {raw['unit']!r}: unknown reason {raw['reason']!r}")
    if not isinstance(raw["candidates"], list) or not all(
        isinstance(x, str) for x in raw["candidates"]
    ):
        raise _bad(f"residual {raw['unit']!r}: candidates must be bead ids")
    return raw


def check_records(verdicts: Iterable[Any], residual: Iterable[Any]) -> None:
    """Validate a collation's B5 members (the collation loader calls this)."""
    for item in verdicts:
        check_verdict(item)
    for item in residual:
        check_residual(item)


def _judgment(raw: Mapping[str, Any]) -> Judgment:
    fallback = raw["fallback"]
    return Judgment(
        outcome=raw["outcome"],
        probabilities={k: v / PERMILLE for k, v in raw["probabilities_permille"].items()},
        confidence=raw["confidence_permille"] / PERMILLE,
        option_count=raw["option_count"],
        rationale=raw["rationale"],
        engine=raw["engine"],
        engine_version=raw["engine_version"],
        model=raw["model"],
        fallback=None
        if fallback is None
        else Fallback(fallback["engine"], fallback["reason"], fallback["detail"]),
    )


def prior_verdicts(source: Collation | Iterable[Mapping[str, Any]]) -> dict[str, Verdict]:
    """The reusable judge verdicts of an earlier collation (or its ``verdicts``), by query hash.

    Only ``judge`` verdicts that did not fall back are reusable: deterministic steps are
    recomputed anyway, and a failed call is worth retrying.
    """
    records = source.verdicts if isinstance(source, Collation) else source
    out: dict[str, Verdict] = {}
    for raw in records:
        check_verdict(raw)
        if raw["step"] != STEP_JUDGE or raw["query_hash"] is None or raw["fallback"] is not None:
            continue
        out[raw["query_hash"]] = Verdict(raw["family"] or FAMILY, raw["query_hash"], _judgment(raw))
    return out


# -- the engine ------------------------------------------------------------------------------


def _title(state: BeadState) -> str:
    value = state.fields.get("title")
    return value if isinstance(value, str) else ""


@dataclass(frozen=True)
class _Bead:
    id: str
    title: str
    epic: bool
    tokens: frozenset[str]


class _Baseline:
    """The live baseline beads, their title tokens and ``external_ref`` index (built once)."""

    def __init__(self, baseline: Iterable[BeadEvent]) -> None:
        beads: dict[str, _Bead] = {}
        refs: dict[str, list[str]] = {}
        states = fold(baseline)
        self.decisions: dict[str, Decision] = decisions_of(states)
        self.live: dict[str, bool] = {}  # live non-decision bead id -> is an epic
        for ref, state in sorted(states.items(), key=lambda item: item[0]):
            if not state.exists or state.deleted or state.erased:
                continue
            if is_decision(state):
                continue  # a decision record is never a candidate and never an external_ref
            self.live[ref.id] = state.type == "epic"
            external = state.fields.get("external_ref")
            if isinstance(external, str) and external:
                refs.setdefault(external, []).append(ref.id)
            if (state.retired or {}).get("reason") == ROLLBACK_REASON:
                continue  # only an exact external_ref names a rolled-back create (bh-12hka)
            title = _title(state)
            if title:
                beads[ref.id] = _Bead(ref.id, title, state.type == "epic", tokens(title))
        self.beads = tuple(beads[i] for i in sorted(beads))
        self.refs = {k: sorted(v) for k, v in refs.items()}

    def ranked(
        self, subject: frozenset[str], limit: int, *, epics: bool
    ) -> list[tuple[float, _Bead]]:
        scored = [
            (jaccard(subject, bead.tokens), bead) for bead in self.beads if not epics or bead.epic
        ]
        scored = [(score, bead) for score, bead in scored if score > 0]
        scored.sort(key=lambda item: (-item[0], item[1].id))
        return scored[:limit]


def _ref_keys(unit: Unit, attached: Sequence[str] = ()) -> list[str]:
    """The keys a baseline bead's ``external_ref`` may carry for ``unit``: its own, then (last, so
    an own key always wins) those of the docs B4 attached to it, since a doc that first made a
    bead of its own and later attached to a change is still that bead's evidence (bh-1vkff.3)."""
    keys = [unit.key]
    if unit.key.startswith("doc:"):
        keys.append(unit.key[4:])
    for artifact in unit.artifacts:
        keys.append(artifact)
        if artifact.startswith("doc:"):
            keys.append(artifact[4:])
    for doc in attached:
        keys.extend((doc, doc[4:]) if doc.startswith("doc:") else (doc,))
    return list(dict.fromkeys(keys))


def _bridge(rationale: str, outcome: str) -> Judgment:
    return Judgment(outcome, {outcome: 1.0}, 1.0, 1, rationale, BRIDGE_ENGINE, ENGINE_VERSION)


def _context(unit: Unit) -> str:
    lines = [unit.title, f"kind: {unit.kind}", f"rule: {unit.rule}", f"key: {unit.key}"]
    if unit.area:
        lines.append(f"area: {unit.area}")
    if unit.artifacts:
        shown = ", ".join(unit.artifacts[:_MAX_CONTEXT_ARTIFACTS])
        more = len(unit.artifacts) - _MAX_CONTEXT_ARTIFACTS
        lines.append(f"evidence: {shown}" + (f" (+{more} more)" if more > 0 else ""))
    return "\n".join(lines)[:_CONTEXT_LIMIT]


@dataclass
class _Pending:
    """A unit the deterministic steps left undecided."""

    unit: Unit
    score: int
    candidates: tuple[str, ...]
    query: ChoiceQuery | None = None
    key: str | None = None
    targets: dict[str, tuple[str, str]] = field(default_factory=dict)
    judgment: Judgment | None = None
    reason: str = REASON_NO_MATCH


@dataclass(frozen=True)
class IdentifyResult:
    """What :func:`identify_units` decided: the records, the calls made and the preview."""

    verdicts: tuple[dict[str, Any], ...]
    residual: tuple[dict[str, Any], ...]
    calls: int = 0
    reused: int = 0
    preview: CostPreview | None = None
    stale: tuple[str, ...] = ()


def _print_warning(line: str) -> None:
    sys.stderr.write(f"identify: {line}\n")
    sys.stderr.flush()


def _decided(unit: str, decision: Decision) -> dict[str, Any]:
    """The step 0 verdict of a (non-stale) operator decision."""
    reason = f": {decision.reason}" if decision.reason else ""
    by = f" by {decision.decided_by}" if decision.decided_by else ""
    note = f"operator decision {decision.kind}{by} (bead {decision.bead}){reason}"
    cls = {
        LINK: str(ReconcileClass.PRESENT),
        NOISE: str(ReconcileClass.NOISE),
        SKIP: CLASS_SKIP,
    }[decision.kind]
    return _verdict_json(
        unit,
        STEP_DECISION,
        _bridge(note, decision.kind if decision.target is None else decision.target),
        cls=cls,
        bead=decision.target,
        provenance="derived",
        review=Review.NONE,
        score=None,
    )


def _stale(decision: Decision, base: _Baseline) -> str | None:
    """Why a ``link`` / ``noise`` decision cannot be applied (``None`` when it can)."""
    if decision.kind == SKIP:
        return None
    target = decision.target
    if target is None or target not in base.live:
        return f"its target bead {target} is no longer in the store"
    if decision.kind == NOISE and not base.live[target]:
        return f"its covering bead {target} is not an epic"
    return None


def identify_units(
    units: Iterable[Unit],
    baseline: Iterable[BeadEvent],
    options: IdentifyOptions | None = None,
    run: IdentifyRun | None = None,
    attached: Mapping[str, Sequence[str]] | None = None,
) -> IdentifyResult:
    """B5 over ``units`` (see the module doc); pure apart from the judge and the preview sink.
    ``attached`` maps a unit key to the keys of the docs B4 attached to it."""
    options = options or IdentifyOptions()
    run = run or IdentifyRun()
    base = _Baseline(baseline)
    lexical = DeterministicJudge()
    verdicts: dict[tuple[str, int], dict[str, Any]] = {}
    pending: list[_Pending] = []

    def put(unit: str, step: str, record: dict[str, Any]) -> None:
        verdicts[(unit, STEPS.index(step))] = record

    def match(item: _Pending, step: str, judgment: Judgment, provenance: str) -> dict[str, Any]:
        target = item.targets.get(judgment.outcome or "")
        cls, bead = target if target is not None else (None, None)
        if judgment.outcome == NEW_KEY:
            cls = str(ReconcileClass.NEW)
        return _verdict_json(
            item.unit.key,
            step,
            judgment,
            cls=cls,
            bead=bead,
            provenance=provenance,
            review=_review(judgment, FAMILY, run.auto_accept),
            score=item.score,
            query=item.key,
            family=FAMILY,
        )

    stale: list[str] = []
    for unit in sorted(units, key=lambda u: u.key):
        # 0. an operator decision from the bead store.
        decision = base.decisions.get(unit.key)
        if decision is not None:
            why = _stale(decision, base)
            if why is None:
                put(unit.key, STEP_DECISION, _decided(unit.key, decision))
                continue
            stale.append(
                f"decision {decision.kind} on {unit.key} (bead {decision.bead}) not applied: {why}"
            )
        # 1. external_ref / source key.
        hit = next(
            (
                (k, base.refs[k])
                for k in _ref_keys(unit, (attached or {}).get(unit.key, ()))
                if k in base.refs
            ),
            None,
        )
        if hit is not None:
            ref, ids = hit
            note = f"; also carried by {', '.join(ids[1:])}" if len(ids) > 1 else ""
            put(
                unit.key,
                STEP_EXTERNAL_REF,
                _verdict_json(
                    unit.key,
                    STEP_EXTERNAL_REF,
                    _bridge(f"baseline bead {ids[0]} has external_ref {ref!r}{note}", ids[0]),
                    cls=str(ReconcileClass.PRESENT),
                    bead=ids[0],
                    provenance="derived",
                    review=Review.NONE,
                    score=None,
                ),
            )
            continue
        # 2. explicit id: an attach unit is the evidence of a local bead already.
        if unit.kind == ATTACH and unit.bead is not None:
            put(
                unit.key,
                STEP_EXPLICIT_ID,
                _verdict_json(
                    unit.key,
                    STEP_EXPLICIT_ID,
                    _bridge(f"the evidence names local bead {unit.bead}", unit.bead),
                    cls=str(ReconcileClass.PRESENT),
                    bead=unit.bead,
                    provenance="derived",
                    review=Review.NONE,
                    score=None,
                ),
            )
            continue
        # 3. the deterministic lexical engine over the match query (PRESENT / NOISE only).
        subject = tokens(unit.title)
        present = base.ranked(subject, options.max_candidates, epics=False)
        epics = base.ranked(subject, options.max_candidates, epics=True)
        if not present and not epics:
            why = "no baseline bead shares a title token with the unit"
            record = _verdict_json(
                unit.key,
                STEP_NO_CANDIDATE,
                Judgment(NEW_KEY, {NEW_KEY: 1.0}, 1.0, 1, why, BRIDGE_ENGINE, ENGINE_VERSION),
                cls=str(ReconcileClass.NEW),
                bead=None,
                provenance="derived",
                review=Review.REQUIRED,
                score=0,
            )
            record["confidence_permille"] = None
            put(unit.key, STEP_NO_CANDIDATE, record)
            continue
        score = round(present[0][0] * 100) if present else 0
        item = _Pending(unit, score, tuple(bead.id for _, bead in present))
        if present:
            opts: dict[str, str] = {}
            for number, (_, bead) in enumerate(present, start=1):
                opts[f"present:{number}"] = bead.title
                item.targets[f"present:{number}"] = (str(ReconcileClass.PRESENT), bead.id)
            for number, (_, bead) in enumerate(epics, start=1):
                opts[f"noise:{number}"] = bead.title
                item.targets[f"noise:{number}"] = (str(ReconcileClass.NOISE), bead.id)
            opts[NEW_KEY] = NEW_TEXT
            opts[ABSTAIN] = ABSTAIN_TEXT
            item.query = ChoiceQuery(QUESTION, opts, _context(unit), FAMILY)
            item.key = query_hash(item.query)
            answer = lexical.choose(item.query)
            if answer.outcome in item.targets:
                put(unit.key, STEP_LEXICAL, match(item, STEP_LEXICAL, answer, "derived"))
                continue
        pending.append(item)

    # 4. the judge, after the cost preview.
    calls = reused = 0
    preview: CostPreview | None = None
    if run.judging:
        due: list[_Pending] = []
        for item in pending:
            if item.key is None:
                continue
            prior = run.prior.get(item.key)
            if prior is not None:
                item.judgment = prior.judgment
                reused += 1
            else:
                due.append(item)
        preview = CostPreview(len(due), run.cap(), run.cost_per_call_usd, reused)
        (run.preview or print_preview)(preview)
        if preview.needs_confirm and not run.confirm:
            raise IdentifyConfirmationRequired(preview)
        for item in due:
            if calls >= preview.max_calls_per_run:
                item.reason = REASON_JUDGE_BUDGET
                continue
            answer = ask(run.judge, item.query)  # type: ignore[arg-type]
            calls += 1
            if answer.fallback is not None and answer.fallback.reason == "max-calls":
                item.reason = REASON_JUDGE_BUDGET  # the judge's own cap: not an answer
                continue
            item.judgment = answer

    # 5. threshold, then 6. residual.
    residual: list[dict[str, Any]] = []
    threshold = options.new_threshold
    for item in pending:
        if item.judgment is not None:
            judged = match(item, STEP_JUDGE, item.judgment, "inferred")
            put(item.unit.key, STEP_JUDGE, judged)  # abstentions too: resume never re-asks
            if judged["class"] is not None:
                continue
            failed = item.judgment.fallback is not None
            item.reason = REASON_JUDGE_FAILED if failed else REASON_JUDGE_ABSTAINED
        if item.reason != REASON_JUDGE_BUDGET and threshold is not None and item.score < threshold:
            confidence = 1 - item.score / 100
            why = f"fuzzy score {item.score}% is below new_threshold {threshold}%"
            put(
                item.unit.key,
                STEP_THRESHOLD,
                _verdict_json(
                    item.unit.key,
                    STEP_THRESHOLD,
                    Judgment(
                        NEW_KEY,
                        {NEW_KEY: confidence},
                        confidence,
                        1,
                        why,
                        THRESHOLD_ENGINE,
                        ENGINE_VERSION,
                    ),
                    cls=str(ReconcileClass.NEW),
                    bead=None,
                    provenance="inferred",
                    review=Review.REQUIRED,
                    score=item.score,
                ),
            )
            continue
        residual.append(
            {
                "unit": item.unit.key,
                "reason": item.reason,
                "score": item.score,
                "query_hash": item.key,
                "candidates": list(item.candidates),
            }
        )
    for line in stale:
        (run.warn or _print_warning)(line)
    return IdentifyResult(
        verdicts=tuple(verdicts[k] for k in sorted(verdicts)),
        residual=tuple(residual),
        calls=calls,
        reused=reused,
        preview=preview,
        stale=tuple(stale),
    )


def identify(
    collation: Collation,
    baseline: Iterable[BeadEvent],
    options: IdentifyOptions | None = None,
    run: IdentifyRun | None = None,
) -> Collation:
    """B5: ``collation`` with ``verdicts``, ``residual`` and ``options.identify`` filled."""
    options = options or IdentifyOptions()
    docs: dict[str, list[str]] = {}
    for found in collation.attachments:
        docs.setdefault(found.unit, []).append(found.doc)
    result = identify_units(collation.units, baseline, options, run, docs)
    return replace(
        collation,
        verdicts=result.verdicts,
        residual=result.residual,
        options={**collation.options, "identify": options.to_json()},
    )


__all__ = [
    "CLASSES",
    "CLASS_SKIP",
    "FAMILY",
    "OPTION_MEMBERS",
    "RESIDUAL_MEMBERS",
    "RESIDUAL_REASONS",
    "STEPS",
    "STEP_DECISION",
    "VERDICT_MEMBERS",
    "CostPreview",
    "IdentifyConfirmationRequired",
    "IdentifyOptions",
    "IdentifyOptionsError",
    "IdentifyResult",
    "IdentifyRun",
    "check_records",
    "identify",
    "identify_units",
    "print_preview",
    "prior_verdicts",
]
