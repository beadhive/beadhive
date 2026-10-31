"""Complexity labels: pass #1 of the hive apply envelope (``docs/BACKFILL.md``).

Ported from root ``beadhive.complexity_backfill``: every bead in scope converges on exactly one
canonical ``complexity:<TIER>`` label. A sole valid label is explicit routing intent and stays;
missing, malformed or duplicate ones are repaired from the stable classifier. ``model:*`` labels
are historical intent: they are reported, never added or removed.

Two faces share one policy (:func:`assess`):

- :class:`ComplexityPass` is the engine-native :class:`~beadhive_backfill.HivePass` (with the
  :class:`~beadhive_backfill.ScopedPass`, :class:`~beadhive_backfill.VerifyingPass` and
  :class:`~beadhive_backfill.ReportingPass` hooks) that :class:`~beadhive_backfill.BackfillEngine`
  plans and applies over any :class:`~beadhive_backfill.StoreLoader`;
- the **published contract** of ``bh backfill complexity`` / ``bh backfill-complexity``: the
  JSON plan (:func:`build_plan` / :func:`verify_plan`), its corpus hash over ``bd list`` rows,
  and :func:`apply_plan`, which runs that plan through the engine with row-store adapters and
  keeps the JSON audit shape the command has always written.

Nothing here imports ``beadhive``: the tier vocabulary and the classifier arrive through the
:class:`ComplexityRules` port, which root implements over ``beadhive.complexity``.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from beadlog import BeadEvent, BeadRef, BeadState, EventKind, Group, fold, mint_event
from beadlog_sources import JudgmentPort, baseline_events

from ._errors import BackfillError
from ._projection import bead_key
from .contracts import RECOVERY_INSTRUCTION, AuditEntry, AuditState
from .engine import BackfillEngine, system_clock

SCHEMA_VERSION = 1
MIGRATION = "complexity-label-backfill"
PASS_NAME = "complexity"
PASS_SCHEMA_VERSION = "1"
ROUTABLE_STATUSES = frozenset({"open", "blocked", "in_progress", "deferred", "closed"})
SYSTEM_ARTIFACT_LABELS = frozenset({"gt:slot"})
# Fields whose change since planning invalidates a plan (the legacy corpus projection).
TEXT_FIELDS = ("title", "description", "design", "acceptance_criteria")
INVALIDATING_FIELDS = frozenset(
    {"exists", "type", "phase", "labels", *(f"fields.{name}" for name in TEXT_FIELDS)}
)
# The group the row-store adapters mint bead refs in; it never reaches a store or an archive.
ROW_GROUP = "urn:beadhive:complexity-backfill:rows"
SOURCE_SYSTEM = "beadhive-backfill"
# When a row that carries no timestamp is taken to have been recorded (see ``_RowsReader``).
UNDATED_ROW = "1970-01-01T00:00:00Z"

# Historical Claude aliases expressed a three-step capability judgment. Opus spans the upper
# two provider-neutral tiers because the old vocabulary could not distinguish them.
LEGACY_MODEL_TIERS: Mapping[str, frozenset[str]] = {
    "haiku": frozenset({"SIMPLE"}),
    "sonnet": frozenset({"MEDIUM"}),
    "opus": frozenset({"COMPLEX", "REASONING"}),
}


@dataclass(frozen=True)
class Classification:
    """One required classification: a canonical tier name (``None`` violates the contract)."""

    tier: str | None
    score: Any
    source: str
    version: str
    fallback_used: bool


class ComplexityRules(Protocol):
    """The tier vocabulary and classifier the pass needs (root: ``beadhive.complexity``)."""

    @property
    def label_prefix(self) -> str:
        """``complexity:``."""
        ...

    @property
    def model_prefix(self) -> str:
        """``model:``."""
        ...

    @property
    def source(self) -> str:
        """The classifier's source identity (recorded when no bead was classified)."""
        ...

    @property
    def version(self) -> str:
        """The classifier's version (recorded when no bead was classified)."""
        ...

    def parse_label(self, label: str) -> str:
        """The tier name of a canonical complexity label; raise ``ValueError`` otherwise."""
        ...

    def label(self, tier: str) -> str:
        """The canonical label of a tier name."""
        ...

    def routable_type(self, issue_type: str) -> bool:
        """Whether a bead of this type receives capability routing."""
        ...

    def valid_model(self, value: str) -> bool:
        """Whether a ``model:`` label value has the ``provider/model-name`` shape."""
        ...

    def classify(self, bead: Mapping[str, Any]) -> Classification:
        """Classify a bead row (required: a concrete tier, with fallback provenance)."""
        ...


# -- the policy ----------------------------------------------------------------------------


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode()).hexdigest()


def _issue_type(row: Mapping[str, Any]) -> str:
    return str(row.get("issue_type") or row.get("type") or "").strip().lower()


def _status(row: Mapping[str, Any]) -> str:
    return str(row.get("status") or "").strip().lower().replace("-", "_")


def _labels(row: Mapping[str, Any]) -> list[str]:
    labels = row.get("labels") or []
    return [str(label) for label in labels] if isinstance(labels, (list, tuple, set)) else []


def complexity_labels(row: Mapping[str, Any], rules: ComplexityRules) -> list[str]:
    return [label for label in _labels(row) if label.startswith(rules.label_prefix)]


def _model_labels(row: Mapping[str, Any], rules: ComplexityRules) -> list[str]:
    return [label for label in _labels(row) if label.startswith(rules.model_prefix)]


def _system_artifact_reason(row: Mapping[str, Any]) -> str | None:
    """A conservative semantic exclusion for records owned by factory infrastructure.

    ``gt:slot`` is the durable marker on the singleton merge-coordination record. Its title and
    issue type intentionally look like ordinary work, so title/id matching would be both fragile
    and liable to exclude a real task. Add future system artifacts here only when they have an
    equally explicit semantic label.
    """
    matched = sorted(set(_labels(row)) & SYSTEM_ARTIFACT_LABELS)
    return f"system_artifact:{matched[0]}" if matched else None


def scope_reason(row: Mapping[str, Any], rules: ComplexityRules) -> str | None:
    """Why a bead row is outside the migration (``None`` when it is a target)."""
    if reason := _system_artifact_reason(row):
        return reason
    issue_type = _issue_type(row)
    if not rules.routable_type(issue_type):
        return f"non_routable_type:{issue_type or '<missing>'}"
    status = _status(row)
    if status not in ROUTABLE_STATUSES:
        return f"non_routable_status:{status or '<missing>'}"
    return None


def _corpus_projection(
    records: Iterable[Mapping[str, Any]], rules: ComplexityRules
) -> list[dict[str, Any]]:
    """Stable inputs whose change invalidates a plan, restricted to migration targets."""
    projected = []
    for row in records:
        if scope_reason(row, rules):
            continue
        projected.append(
            {
                "id": str(row.get("id") or ""),
                "issue_type": _issue_type(row),
                "status": _status(row),
                "title": row.get("title"),
                "description": row.get("description"),
                "design": row.get("design"),
                "acceptance_criteria": row.get("acceptance_criteria")
                if row.get("acceptance_criteria") is not None
                else row.get("acceptance"),
                "labels": sorted(_labels(row)),
            }
        )
    return sorted(projected, key=lambda item: item["id"])


def corpus_hash(records: Iterable[Mapping[str, Any]], rules: ComplexityRules) -> str:
    """The legacy plan's corpus hash over ``bd list`` rows."""
    return _digest(_corpus_projection(records, rules))


def _counter(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items()))


def _legacy_model_disagreement(
    model_label: str, tier: str, rules: ComplexityRules
) -> dict[str, Any] | None:
    value = model_label[len(rules.model_prefix) :]
    alias = value.rsplit("/", 1)[-1].lower()
    # Canonical names frequently carry a version suffix (claude-opus-4-1); recognize the old
    # capability word without pretending every future provider catalogue is known here.
    matched = next((name for name in LEGACY_MODEL_TIERS if name in alias), None)
    if matched is None or tier in LEGACY_MODEL_TIERS[matched]:
        return None
    return {
        "model": model_label,
        "scored_tier": tier,
        "legacy_expected_tiers": sorted(LEGACY_MODEL_TIERS[matched]),
    }


def _check_ids(rows: Sequence[Mapping[str, Any]]) -> None:
    ids = [str(row.get("id") or "") for row in rows]
    duplicates = sorted(iid for iid, count in Counter(ids).items() if not iid or count != 1)
    if duplicates:
        raise BackfillError(f"corpus contains missing or duplicate ids: {', '.join(duplicates)}")


def assess(rows: Iterable[Mapping[str, Any]], rules: ComplexityRules) -> dict[str, Any]:
    """The policy over complete bead rows: per-bead entries plus the report counters.

    Shared by the JSON plan (:func:`build_plan`) and :class:`ComplexityPass` (its proposals and
    its report), so both faces decide every bead identically.
    """
    excluded: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    statuses: Counter[str] = Counter()
    issue_types: Counter[str] = Counter()
    existing: Counter[str] = Counter()
    entries: list[dict[str, Any]] = []
    fallbacks: list[str] = []
    duplicate_or_invalid: list[dict[str, Any]] = []
    preserved_models: list[dict[str, Any]] = []
    disagreements: list[dict[str, Any]] = []

    for row in sorted(rows, key=lambda item: str(item.get("id") or "")):
        reason = scope_reason(row, rules)
        if reason:
            excluded[reason] += 1
            continue

        iid = str(row["id"])
        issue_type = _issue_type(row)
        status = _status(row)
        current = complexity_labels(row, rules)
        valid = []
        for label in current:
            try:
                valid.append(rules.parse_label(label))
            except ValueError:
                pass

        result = rules.classify(row)
        if result.tier is None:  # a backend violated the required-classification contract
            raise BackfillError(f"classifier returned UNKNOWN for required bead {iid}")
        scored_tier = result.tier

        # A sole canonical label is already explicit routing intent and remains authoritative,
        # matching plan compilation's explicit-complexity override. Malformed/ambiguous history
        # is repaired from the stable scorer instead of guessing which old label should win.
        preserves_existing = len(current) == 1 and len(valid) == 1
        target_tier = valid[0] if preserves_existing else scored_tier
        target = rules.label(target_tier)
        needs_change = current != [target]
        if not current:
            existing["missing"] += 1
        elif len(current) == 1 and len(valid) == 1:
            existing["valid"] += 1
        elif len(current) > 1:
            existing["duplicate"] += 1
        else:
            existing["invalid"] += 1
        if current and (len(current) != 1 or len(valid) != 1):
            duplicate_or_invalid.append({"id": iid, "labels": current})

        models = _model_labels(row, rules)
        if models:
            preserved_models.append(
                {
                    "id": iid,
                    "labels": models,
                    "structurally_invalid": [
                        label
                        for label in models
                        if not rules.valid_model(label[len(rules.model_prefix) :])
                    ],
                }
            )
        for model in models:
            disagreement = _legacy_model_disagreement(model, scored_tier, rules)
            if disagreement:
                disagreements.append({"id": iid, **disagreement})

        if result.fallback_used:
            fallbacks.append(iid)
        tiers[target_tier] += 1
        statuses[status] += 1
        issue_types[issue_type] += 1
        entries.append(
            {
                "id": iid,
                "issue_type": issue_type,
                "status": status,
                "before_labels": sorted(_labels(row)),
                "before_complexity": current,
                "target_label": target,
                "scored_tier": scored_tier,
                "score": result.score,
                "source": result.source,
                "version": result.version,
                "fallback_used": result.fallback_used,
                "provenance": "existing" if preserves_existing else "scored",
                "changes": needs_change,
            }
        )

    return {
        "classifier": {
            "source": entries[0]["source"] if entries else rules.source,
            "version": entries[0]["version"] if entries else rules.version,
        },
        "excluded": excluded,
        "by_tier": _counter(tiers),
        "by_status": _counter(statuses),
        "by_type": _counter(issue_types),
        "excluded_by_reason": _counter(excluded),
        "existing_complexity": _counter(existing),
        "unknown_to_medium_fallbacks": fallbacks,
        "duplicate_or_invalid_complexity": duplicate_or_invalid,
        "preserved_model_hints": preserved_models,
        "model_score_disagreements": disagreements,
        "entries": entries,
    }


def complexity_errors(records: Iterable[Mapping[str, Any]], rules: ComplexityRules) -> list[str]:
    """The postcondition: every in-scope bead carries exactly one valid complexity label."""
    errors = []
    for row in records:
        if scope_reason(row, rules):
            continue
        labels = complexity_labels(row, rules)
        if len(labels) != 1:
            errors.append(f"{row.get('id')}: expected one complexity label, found {len(labels)}")
            continue
        try:
            rules.parse_label(labels[0])
        except ValueError:
            errors.append(f"{row.get('id')}: invalid complexity label {labels[0]}")
    return errors


# -- the engine pass -----------------------------------------------------------------------


def state_row(state: BeadState) -> dict[str, Any]:
    """A folded bead as the row shape the policy reads."""
    row: dict[str, Any] = {
        "id": state.bead.id,
        "issue_type": state.type or "",
        "status": state.phase or "",
        "labels": list(state.labels),
    }
    for name in TEXT_FIELDS:
        row[name] = state.fields.get(name)
    return row


class ComplexityPass:
    """The complexity pass on :class:`~beadhive_backfill.BackfillEngine` (module doc).

    ``targets`` pins the target label per bead id (decisions already recorded in a plan, never
    re-classified); a bead without a pin is classified through ``rules``.
    """

    name = PASS_NAME
    schema_version = PASS_SCHEMA_VERSION
    invalidating_fields = INVALIDATING_FIELDS
    drift_applicable = True

    def __init__(self, rules: ComplexityRules, *, targets: Mapping[str, str] | None = None):
        self.rules = rules
        self.targets = dict(targets or {})

    def exclusion(self, state: BeadState) -> str | None:
        if not state.exists or state.deleted or state.erased:
            return "absent"
        return scope_reason(state_row(state), self.rules)

    def _assess(self, states: Iterable[BeadState]) -> dict[str, Any]:
        rows = [state_row(state) for state in states if self.exclusion(state) is None]
        # Pinned beads skip classification: the recorded decision is the target.
        rules = _PinnedRules(self.rules, self.targets) if self.targets else self.rules
        return assess(rows, rules)

    def propose(
        self,
        baseline: Sequence[BeadEvent],
        *,
        extracted_at: str,
        judge: JudgmentPort,
    ) -> Iterator[BeadEvent]:
        states = fold(baseline)
        refs = {ref.id: ref for ref in states}
        for entry in self._assess(states.values())["entries"]:
            if not entry["changes"]:
                continue
            ref = refs[str(entry["id"])]
            target = str(entry["target_label"])
            for label in entry["before_complexity"]:
                if label != target:
                    yield _label_event(EventKind.LABEL_REMOVE, ref, label, extracted_at)
            if target not in entry["before_complexity"]:
                yield _label_event(EventKind.LABEL_ADD, ref, target, extracted_at)

    def verify(self, states: Mapping[BeadRef, BeadState]) -> list[str]:
        return complexity_errors((state_row(state) for state in states.values()), self.rules)

    def report(
        self, states: Mapping[BeadRef, BeadState], proposed: Sequence[BeadEvent]
    ) -> dict[str, Any]:
        """The JSON plan's counters and per-bead entries over the in-scope ``states``."""
        assessment = self._assess(states.values())
        return {key: value for key, value in assessment.items() if key != "excluded"}


class _PinnedRules:
    """``rules`` whose classification of a pinned bead returns the recorded target tier."""

    def __init__(self, rules: ComplexityRules, targets: Mapping[str, str]) -> None:
        self._rules = rules
        self._targets = targets
        self.label_prefix = rules.label_prefix
        self.model_prefix = rules.model_prefix
        self.source = rules.source
        self.version = rules.version

    def parse_label(self, label: str) -> str:
        return self._rules.parse_label(label)

    def label(self, tier: str) -> str:
        return self._rules.label(tier)

    def routable_type(self, issue_type: str) -> bool:
        return self._rules.routable_type(issue_type)

    def valid_model(self, value: str) -> bool:
        return self._rules.valid_model(value)

    def classify(self, bead: Mapping[str, Any]) -> Classification:
        pinned = self._targets.get(str(bead.get("id") or ""))
        if pinned is None:
            return self._rules.classify(bead)
        return Classification(
            tier=self._rules.parse_label(pinned),
            score=None,
            source="recorded",
            version=self._rules.version,
            fallback_used=False,
        )


def _label_event(kind: EventKind, ref: BeadRef, label: str, at: str) -> BeadEvent:
    return mint_event(
        str(kind),
        ref,
        {"label": label},
        occurred_at=at,
        recorded_at=None,
        extracted_at=at,
        source=(SOURCE_SYSTEM, f"complexity/{ref.id}/{kind}/{label}"),
        provenance="derived",
    )


# -- the published JSON plan ---------------------------------------------------------------


def build_plan(records: Iterable[Mapping[str, Any]], rules: ComplexityRules) -> dict[str, Any]:
    """Build a deterministic, JSON-serializable migration plan over a complete corpus read."""
    rows = [dict(row) for row in records]
    _check_ids(rows)
    assessment = assess(rows, rules)
    entries = assessment["entries"]
    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "migration": MIGRATION,
        "classifier": assessment["classifier"],
        "corpus_hash": corpus_hash(rows, rules),
        "totals": {
            "records": len(rows),
            "in_scope": len(entries),
            "excluded": sum(assessment["excluded"].values()),
            "changes": sum(bool(entry["changes"]) for entry in entries),
        },
    }
    for key in (
        "by_tier",
        "by_status",
        "by_type",
        "excluded_by_reason",
        "existing_complexity",
        "unknown_to_medium_fallbacks",
        "duplicate_or_invalid_complexity",
        "preserved_model_hints",
        "model_score_disagreements",
        "entries",
    ):
        plan[key] = assessment[key]
    plan["plan_hash"] = _digest(plan)
    return plan


def verify_plan(plan: Mapping[str, Any]) -> None:
    """Refuse an unsupported, edited or corrupt JSON plan."""
    if plan.get("schema_version") != SCHEMA_VERSION or plan.get("migration") != MIGRATION:
        raise BackfillError("unsupported complexity-backfill plan schema")
    expected = plan.get("plan_hash")
    payload = dict(plan)
    payload.pop("plan_hash", None)
    if not isinstance(expected, str) or expected != _digest(payload):
        raise BackfillError("plan hash mismatch — refusing an edited or corrupt plan")


def write_json(path: Path, value: Any) -> None:
    """Write ``value`` as sorted, indented JSON at ``path`` atomically (sibling + rename)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(value, handle, sort_keys=True, indent=2, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def read_plan(path: Path) -> dict[str, Any]:
    """Read and verify a saved JSON plan."""
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BackfillError(f"could not read plan {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BackfillError(f"plan {path} is not a JSON object")
    verify_plan(data)
    return data


def write_plan(path: Path, plan: Mapping[str, Any]) -> None:
    """Verify, then save a JSON plan."""
    verify_plan(plan)
    write_json(path, plan)


def _expected_after(
    records: Iterable[Mapping[str, Any]], plan: Mapping[str, Any], rules: ComplexityRules
) -> list[dict[str, Any]]:
    """Apply the plan to an in-memory copy for a whole-corpus post-write drift check."""
    expected = [{**row, "labels": list(_labels(row))} for row in records]
    by_id = {str(row.get("id") or ""): row for row in expected}
    for entry in plan.get("entries") or []:
        if not entry.get("changes"):
            continue
        row = by_id[str(entry["id"])]
        row["labels"] = [
            label for label in _labels(row) if not label.startswith(rules.label_prefix)
        ] + [str(entry["target_label"])]
    return expected


# -- the JSON plan's apply, through the engine ---------------------------------------------


class _RowsReader:
    """A :class:`~beadhive_backfill.StoreReader` over a row store (``bd list`` rows).

    ``primed`` reads are served before the store is read again: the drift gate's read is the
    engine's planning and pre-apply baseline, so one corpus read decides all three. A row with
    neither ``updated_at`` nor ``created_at`` is read as recorded at :data:`UNDATED_ROW`: an
    undated event sorts after every dated one, so it would outlive any planned or rollback write
    in the fold.
    """

    def __init__(
        self,
        load_records: Callable[[], list[dict[str, Any]]],
        *,
        primed: Sequence[list[dict[str, Any]]],
        clock: Callable[[], str],
    ) -> None:
        self._load = load_records
        self._primed = list(primed)
        self._clock = clock
        self.last_rows: list[dict[str, Any]] = []

    def export(self) -> tuple[BeadEvent, ...]:
        rows = self._primed.pop(0) if self._primed else self._load()
        self.last_rows = rows
        dated = [
            row
            if row.get("updated_at") or row.get("created_at")
            else {**row, "updated_at": UNDATED_ROW}
            for row in rows
        ]
        return baseline_events(dated, group=ROW_GROUP, extracted_at=self._clock())


class _LabelLoader:
    """A :class:`~beadhive_backfill.StoreLoader` writing label events one label at a time."""

    name = "complexity-labels"
    version = "1"
    capabilities: frozenset[str] = frozenset()

    def __init__(self, mutate: Callable[[str, str, str], None]) -> None:
        self._mutate = mutate

    def export(self) -> tuple[BeadEvent, ...]:  # the engine reads through _RowsReader
        raise BackfillError("the label loader is write-only")

    def load(self, events: Sequence[BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        kinds = {EventKind.LABEL_REMOVE: "remove", EventKind.LABEL_ADD: "add"}
        unsupported = sorted({str(event.kind) for event in events} - {str(k) for k in kinds})
        if unsupported:
            raise BackfillError(f"complexity apply writes labels only, not {unsupported}")
        if not dry_run:
            # Removals first, then the target: the order the command has always written.
            for kind in (EventKind.LABEL_REMOVE, EventKind.LABEL_ADD):
                for event in events:
                    if event.kind is kind:
                        self._mutate(kinds[kind], event.bead.id, str(event.data["label"]))
        return {"losses": [], "summary": {"by_code": {}}}


class _CallbackPreState:
    """A :class:`~beadhive_backfill.PreStateExporter` over a store-native export callback."""

    def __init__(self, export: Callable[[], None], artifact: str) -> None:
        self._export = export
        self._artifact = artifact

    def export_pre_state(self, baseline: Sequence[BeadEvent]) -> str:
        self._export()
        return self._artifact


class _JsonPlanAuditSink:
    """Renders each engine :class:`~beadhive_backfill.AuditEntry` as the JSON audit the
    command has always written (``planned_updates``, ``progress.*_count``, ...)."""

    def __init__(
        self,
        path: Path,
        plan: Mapping[str, Any],
        *,
        before: Sequence[Mapping[str, Any]],
        reader: _RowsReader,
        rules: ComplexityRules,
    ) -> None:
        self.path = path
        self._plan = plan
        self._reader = reader
        self._rules = rules
        self._before_hash = corpus_hash(before, rules)
        self._expected_hash = corpus_hash(_expected_after(before, plan, rules), rules)
        self._ids = {
            bead_key(BeadRef(ROW_GROUP, str(entry["id"]))): str(entry["id"])
            for entry in plan.get("entries") or []
        }
        self.last: dict[str, Any] = {}

    def _id(self, key: str | None) -> str | None:
        return None if key is None else self._ids.get(key, key)

    def record(self, entry: AuditEntry) -> None:
        artifact = entry.recovery.pre_state_artifact
        audit: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "migration": MIGRATION,
            "plan_hash": self._plan["plan_hash"],
            "corpus_hash_before": self._before_hash,
            "corpus_hash_expected_after": self._expected_hash,
            "pre_state_artifact": artifact,
            "state": str(entry.state),
            "planned_updates": [self._id(key) for key in entry.planned],
            "attempted": [self._id(key) for key in entry.attempted],
            "completed": [self._id(key) for key in entry.completed],
            "rollback_failures": [*entry.rollback_failures, *entry.rollback_residual],
            "progress": {
                "planned_count": entry.progress.planned,
                "attempted_count": entry.progress.attempted,
                "completed_count": entry.progress.completed,
                "current_bead": self._id(entry.progress.current),
                "next_index": entry.progress.next_index,
            },
            "recovery": {
                "pre_state_artifact": artifact,
                "uncertain_bead": self._id(entry.recovery.uncertain),
                "instruction": RECOVERY_INSTRUCTION,
            },
        }
        if entry.state in (AuditState.ROLLED_BACK, AuditState.ROLLBACK_FAILED):
            audit["error"] = entry.error
            audit["recovery"]["rollback_complete"] = bool(entry.recovery.rollback_complete)
        elif entry.state is AuditState.APPLIED:
            verification = entry.verification or {}
            audit.update(
                {
                    "corpus_hash_after": corpus_hash(self._reader.last_rows, self._rules),
                    "post_apply_complexity_errors": list(verification.get("pass_errors") or []),
                    "model_labels_preserved": True,
                    "second_dry_run_changes": int(verification.get("second_dry_run_changes", 0)),
                    "applied_count": len(entry.completed),
                }
            )
        try:
            write_json(self.path, audit)
        except OSError as exc:
            raise BackfillError(f"could not persist applying audit {self.path}: {exc}") from exc
        self.last = audit


def apply_plan(
    plan: Mapping[str, Any],
    *,
    rules: ComplexityRules,
    load_records: Callable[[], list[dict[str, Any]]],
    mutate: Callable[[str, str, str], None],
    export_pre_state: Callable[[], None],
    audit_path: Path | str | os.PathLike[str],
    pre_state_artifact: str = "",
    clock: Callable[[], str] = system_clock,
) -> dict[str, Any]:
    """Apply one verified JSON plan through :class:`~beadhive_backfill.BackfillEngine`.

    Injection keeps the transaction policy testable without a live hive: production passes
    ``bd list``, one-label-at-a-time ``bd label add/remove`` and ``bd export --all`` adapters.
    The corpus is refused before any export or write when it drifted from the plan; the engine
    then exports the pre-state, persists the ``applying`` audit, writes one bead at a time with
    checkpoints, verifies the whole corpus, the one-label postcondition and an empty second dry
    run, and rolls every touched bead back (audited) on failure or interruption. Planned target
    labels are recorded decisions: apply never re-classifies.
    """
    verify_plan(plan)
    before = load_records()
    actual = corpus_hash(before, rules)
    if actual != plan["corpus_hash"]:
        raise BackfillError(
            "corpus changed after planning "
            f"(planned {plan['corpus_hash']}, current {actual}) — rerun dry-run"
        )
    targets = {str(entry["id"]): str(entry["target_label"]) for entry in plan.get("entries") or []}
    pass_ = ComplexityPass(rules, targets=targets)
    reader = _RowsReader(load_records, primed=[before, before], clock=clock)
    engine = BackfillEngine(groups=[Group(ROW_GROUP, "rows")], clock=clock)
    engine_plan = engine.plan(pass_, reader)
    planned = sorted(str(entry["id"]) for entry in plan.get("entries") or [] if entry["changes"])
    units = sorted(bead.id for bead, _events in engine_plan.units())
    if units != planned:  # e.g. a row the neutral reader could not read back as events
        raise BackfillError(
            f"the engine plan writes {units} but the JSON plan changes {planned}; nothing written"
        )
    sink = _JsonPlanAuditSink(Path(audit_path), plan, before=before, reader=reader, rules=rules)
    engine.apply(
        engine_plan,
        reader,
        _LabelLoader(mutate),
        sink,
        pre_state=_CallbackPreState(export_pre_state, pre_state_artifact),
        pass_=pass_,
    )
    return sink.last


__all__ = [
    "INVALIDATING_FIELDS",
    "LEGACY_MODEL_TIERS",
    "MIGRATION",
    "PASS_NAME",
    "PASS_SCHEMA_VERSION",
    "ROUTABLE_STATUSES",
    "ROW_GROUP",
    "SCHEMA_VERSION",
    "SYSTEM_ARTIFACT_LABELS",
    "TEXT_FIELDS",
    "Classification",
    "ComplexityPass",
    "ComplexityRules",
    "apply_plan",
    "assess",
    "build_plan",
    "complexity_errors",
    "complexity_labels",
    "corpus_hash",
    "read_plan",
    "scope_reason",
    "state_row",
    "verify_plan",
    "write_json",
    "write_plan",
]
