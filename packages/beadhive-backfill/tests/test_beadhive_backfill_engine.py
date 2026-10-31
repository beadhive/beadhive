"""The hive apply envelope, generically: every behavior ``tests/test_complexity_backfill.py``
pins, reproduced through a fake policy pass and a fake fold-based store, plus the recorded
judgment, DRIFT, rollback-inverse and plan-archive guarantees of the engine itself.

The fake ``tier`` pass mirrors the complexity pass: one ``tier:*`` label per in-scope bead, an
existing sole valid label is authoritative, malformed or duplicate ones are repaired from a
judged (or fallback) tier, ``model:*`` labels are history and never touched, and the merge-slot
record (``gt:slot``) is counted but never a target.
"""

from __future__ import annotations

import io
import json
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

import beadlog
from beadhive_backfill import (
    ROLLBACK_REASON,
    ArchivePreStateExporter,
    AuditEntry,
    AuditState,
    BackfillEngine,
    BackfillError,
    HivePass,
    JsonFileAuditSink,
    Plan,
    ReportingPass,
    ScopedPass,
    StoreLoader,
    StoreReader,
    VerifyingPass,
    bead_key,
)
from beadlog_sources import (
    ABSTAIN,
    ChoiceQuery,
    Judgment,
    JudgmentPort,
    ReconcileClass,
    ScoreQuery,
    plan_entries,
    reconcile,
    write_plan,
)

GROUP = beadlog.Group("urn:uuid:00000000-0000-4000-8000-0000000000bb", "bh")
HISTORY = "2026-07-01T00:00:00Z"
TIER = "tier:"
TIERS = ("tier:HIGH", "tier:LOW")
FALLBACK_TIER = "tier:LOW"
ROUTABLE_TYPES = frozenset({"task", "bug", "feature", "chore", "epic"})
ROUTABLE_PHASES = frozenset({"open", "blocked", "in_progress", "deferred", "closed"})
SLOT = "gt:slot"


def ref(iid: str) -> beadlog.BeadRef:
    return beadlog.BeadRef(GROUP.id, iid)


def key(iid: str) -> str:
    return bead_key(ref(iid))


class TickClock:
    """A deterministic clock: each call is one second after the previous one."""

    def __init__(self, start: int = 0) -> None:
        self.ticks = start

    def __call__(self) -> str:
        value = f"2026-10-31T00:{self.ticks // 60:02d}:{self.ticks % 60:02d}Z"
        self.ticks += 1
        return value


def _mint(kind: str, iid: str, data: Mapping[str, Any], ref_: str, at: str = HISTORY) -> Any:
    return beadlog.mint_event(
        kind,
        ref(iid),
        data,
        occurred_at=at,
        recorded_at=at,
        extracted_at=at,
        source=("bd", ref_),
        provenance="observed",
    )


def bead(
    iid: str,
    *,
    kind: str = "task",
    phase: str = "open",
    title: str = "Implement a Python API and unit test",
    labels: Iterable[str] = (),
    **fields: Any,
) -> list[beadlog.BeadEvent]:
    """A baseline bead as bd would export it: create, phase, one add per label."""
    events = [
        _mint("create", iid, {"type": kind, "fields": {"title": title, **fields}}, f"{iid}/create")
    ]
    if phase != "open":
        events.append(_mint("phase", iid, {"phase": phase}, f"{iid}/phase"))
    events += [
        _mint("label_add", iid, {"label": label}, f"{iid}/label/{label}") for label in labels
    ]
    return events


def later(stamp: str, seconds: int) -> str:
    moment = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    return beadlog.format_timestamp(moment + timedelta(seconds=seconds))


def corpus(*beads: list[beadlog.BeadEvent]) -> list[beadlog.BeadEvent]:
    return [event for events in beads for event in events]


# -- the fake pass ---------------------------------------------------------------------------


class TierPass:
    """Complexity, generically: exactly one valid ``tier:*`` label per in-scope bead."""

    name = "tier"
    schema_version = "1"
    invalidating_fields = frozenset({"type", "phase", "fields.title", "labels"})

    def __init__(
        self, *, drift_applicable: bool = True, careless: bool = False, verifies: bool = True
    ) -> None:
        self.drift_applicable = drift_applicable
        self.verifies = verifies  # a report-only pass has no postcondition to prove
        self.careless = careless  # proposes for excluded beads too (the engine must drop them)
        self.fallbacks: list[str] = []
        self.targets: dict[str, str] = {}

    def exclusion(self, state: beadlog.BeadState) -> str | None:
        if SLOT in state.labels:
            return f"system_artifact:{SLOT}"
        if not state.exists:
            return "absent"
        if state.type not in ROUTABLE_TYPES:
            return f"non_routable_type:{state.type}"
        if state.phase not in ROUTABLE_PHASES:
            return f"non_routable_status:{state.phase}"
        return None

    @staticmethod
    def tier_labels(state: beadlog.BeadState) -> list[str]:
        return [label for label in state.labels if label.startswith(TIER)]

    def _target(self, state: beadlog.BeadState, judge: JudgmentPort) -> str:
        current = self.tier_labels(state)
        if len(current) == 1 and current[0] in TIERS:
            return current[0]
        verdict = judge.choose(
            ChoiceQuery(
                "Which effort tier fits this bead?",
                {"tier:LOW": "routine chore", "tier:HIGH": "deep reasoning", ABSTAIN: "unsure"},
                str(state.fields.get("title", "")),
                "tier",
            )
        )
        if verdict.outcome in TIERS:
            return str(verdict.outcome)
        self.fallbacks.append(state.bead.id)
        return FALLBACK_TIER

    def propose(
        self,
        baseline: Sequence[beadlog.BeadEvent],
        *,
        extracted_at: str,
        judge: JudgmentPort,
    ) -> Iterator[beadlog.BeadEvent]:
        self.fallbacks, self.targets = [], {}
        for bead_ref, state in sorted(beadlog.fold(baseline).items()):
            if self.exclusion(state) and not (self.careless and state.exists):
                continue
            target = self._target(state, judge)
            self.targets[bead_ref.id] = target
            for label in self.tier_labels(state):
                if label != target:
                    yield self._event("label_remove", bead_ref, label, extracted_at)
            if target not in state.labels:
                yield self._event("label_add", bead_ref, target, extracted_at)

    @staticmethod
    def _event(kind: str, bead_ref: beadlog.BeadRef, label: str, at: str) -> beadlog.BeadEvent:
        return beadlog.mint_event(
            kind,
            bead_ref,
            {"label": label},
            occurred_at=at,
            recorded_at=None,
            extracted_at=at,
            source=("tier", f"{bead_ref.id}/{kind}/{label}"),
            provenance="inferred",
        )

    def verify(self, states: Mapping[beadlog.BeadRef, beadlog.BeadState]) -> list[str]:
        errors: list[str] = []
        for bead_ref, state in sorted(states.items()):
            if not self.verifies:
                break
            labels = self.tier_labels(state)
            if len(labels) != 1 or labels[0] not in TIERS:
                errors.append(f"{bead_ref.id}: expected one valid tier label, found {labels}")
        return errors

    def report(
        self,
        states: Mapping[beadlog.BeadRef, beadlog.BeadState],
        proposed: Sequence[beadlog.BeadEvent],
    ) -> dict[str, Any]:
        by_tier: dict[str, int] = {}
        for target in self.targets.values():
            by_tier[target] = by_tier.get(target, 0) + 1
        models = {
            bead_ref.id: [label for label in state.labels if label.startswith("model:")]
            for bead_ref, state in states.items()
        }
        return {
            "fallbacks": sorted(self.fallbacks),
            "by_tier": dict(sorted(by_tier.items())),
            "preserved_model_hints": {iid: labels for iid, labels in models.items() if labels},
        }


class CreatePass:
    """Proposes brand-new beads (NEW): a create plus a label for each title given."""

    name = "create"
    schema_version = "1"
    invalidating_fields = frozenset({"exists", "fields.title", "labels"})
    drift_applicable = False

    def __init__(self, titles: Mapping[str, str], *, comment: bool = False) -> None:
        self.titles = dict(titles)
        self.comment = comment

    def propose(
        self, baseline: Sequence[beadlog.BeadEvent], *, extracted_at: str, judge: JudgmentPort
    ) -> Iterator[beadlog.BeadEvent]:
        # Distinct times give the causal order (create, then label, then comment); same-instant
        # events would be ordered by id, which is deterministic but not causal.
        for iid, title in sorted(self.titles.items()):
            steps = [("create", {"type": "task", "fields": {"title": title}})]
            steps.append(("label_add", {"label": "origin:backfill"}))
            if self.comment:
                steps.append(("comment", {"body": "backfilled from docs"}))
            for offset, (kind, data) in enumerate(steps):
                yield beadlog.mint_event(
                    kind,
                    ref(iid),
                    data,
                    occurred_at=later(extracted_at, offset),
                    recorded_at=None,
                    extracted_at=extracted_at,
                    source=("docs", f"{iid}/{kind}"),
                    provenance="inferred",
                )


class CountingJudge:
    """A judge that answers every query and counts its invocations."""

    engine = "counting-judge"
    engine_version = "1"
    model = "fake-model"

    def __init__(self) -> None:
        self.calls = 0

    def choose(self, query: ChoiceQuery) -> Judgment:
        self.calls += 1
        if query.family == "tier":
            outcome = "tier:HIGH" if "reasoning" in query.context.lower() else "tier:LOW"
        else:
            outcome = "new"
        return Judgment(outcome, {outcome: 0.9}, 0.85, len(query.options), "fake", self.engine, "1")

    def score(self, query: ScoreQuery) -> Judgment:
        self.calls += 1
        return Judgment(None, {}, 0.0, len(query.criteria), "abstain", self.engine, "1")


# -- the fake store --------------------------------------------------------------------------


def _report(store: FakeStore, events: Sequence[beadlog.BeadEvent], dry_run: bool) -> dict:
    losses = [
        {"event_id": event.id, "code": code, "member": "", "disposition": "degraded"}
        for event in events
        for code in store.loss_codes
    ]
    return {
        "loss_report_version": "0.1.0",
        "loader": {"name": store.name, "version": store.version, "capabilities": []},
        "target": "fake:",
        "mode": "dry-run" if dry_run else "apply",
        "summary": {
            "events": len(events),
            "loaded": len(events),
            "degraded": len(losses),
            "dropped": 0,
        },
        "losses": losses,
    }


class FakeStore:
    """A fold-based store (a list of events) that is also the reader and the pre-state port.

    Every write asserts that the pre-state export happened first. ``fail`` / ``interrupt`` /
    ``fail_inverse`` inject one-shot write failures; ``on_write`` observes each write.
    """

    name = "fake"
    version = "0"
    capabilities = frozenset({"valid-time"})

    def __init__(self, events: Iterable[beadlog.BeadEvent]) -> None:
        self.events = list(events)
        self.exported = False
        self.pre_state: tuple[beadlog.BeadEvent, ...] = ()
        self.writes: list[beadlog.BeadEvent] = []
        self.dry_runs = 0
        self.export_calls = 0
        self.loss_codes: list[str] = []
        self.fail: Callable[[beadlog.BeadEvent], bool] | None = None
        self.interrupt: Callable[[beadlog.BeadEvent], bool] | None = None
        self.fail_inverse = False
        self.on_write: Callable[[beadlog.BeadEvent], None] | None = None

    def export(self) -> tuple[beadlog.BeadEvent, ...]:
        self.export_calls += 1
        return tuple(self.events)

    def export_pre_state(self, baseline: Sequence[beadlog.BeadEvent]) -> str:
        self.exported = True
        self.pre_state = tuple(baseline)
        return "recovery/pre-state.bdlg"

    def load(self, events: Sequence[beadlog.BeadEvent], *, dry_run: bool) -> Mapping[str, Any]:
        if dry_run:
            self.dry_runs += 1
            return _report(self, events, dry_run)
        assert self.exported, "the recoverable export must precede every mutation"
        for event in events:
            if self.on_write is not None:
                self.on_write(event)
            self.writes.append(event)
            if self.interrupt is not None and self.interrupt(event):
                self.interrupt = None
                raise KeyboardInterrupt
            if self.fail is not None and self.fail(event):
                self.fail = None
                raise RuntimeError("injected write failure")
            if self.fail_inverse and event.source.system == "beadhive-backfill":
                raise RuntimeError("injected rollback failure")
            self.events.append(event)
        return _report(self, events, dry_run)

    def states(self) -> dict[beadlog.BeadRef, beadlog.BeadState]:
        return beadlog.fold(self.events)

    def labels(self) -> dict[str, tuple[str, ...]]:
        return {bead_ref.id: state.labels for bead_ref, state in self.states().items()}


class ListSink:
    def __init__(self) -> None:
        self.entries: list[AuditEntry] = []

    def record(self, entry: AuditEntry) -> None:
        self.entries.append(entry)


class FailingSink(ListSink):
    def record(self, entry: AuditEntry) -> None:
        raise OSError("disk full")


def engine(clock: TickClock | None = None, **options: Any) -> BackfillEngine:
    return BackfillEngine(groups=[GROUP], clock=clock or TickClock(), **options)


def apply(
    eng: BackfillEngine,
    plan: Plan,
    store: FakeStore,
    sink: Any = None,
    *,
    reader: StoreReader | None = None,
    **options: Any,
) -> AuditEntry:
    return eng.apply(
        plan,
        reader or store,
        store,
        sink if sink is not None else ListSink(),
        pre_state=store,
        **options,
    )


def tier_labels(store: FakeStore) -> dict[str, list[str]]:
    return {
        iid: [label for label in labels if label.startswith(TIER)]
        for iid, labels in store.labels().items()
    }


def model_labels(store: FakeStore) -> dict[str, list[str]]:
    return {
        iid: [label for label in labels if label.startswith("model:")]
        for iid, labels in store.labels().items()
    }


# -- ports -------------------------------------------------------------------------------------


def test_fakes_conform_to_the_ports() -> None:
    tier = TierPass()
    assert isinstance(tier, HivePass)
    assert isinstance(tier, ScopedPass)
    assert isinstance(tier, VerifyingPass)
    assert isinstance(tier, ReportingPass)
    assert isinstance(CreatePass({}), HivePass)
    assert not isinstance(CreatePass({}), ScopedPass)
    store = FakeStore([])
    assert isinstance(store, StoreReader)
    assert isinstance(store, StoreLoader)


# -- planning (complexity: plan coverage, merge slot, repair) ---------------------------------


def test_plan_covers_history_epics_fallbacks_and_counts_exclusions() -> None:
    store = FakeStore(
        corpus(
            bead("h-closed", phase="closed", title="Polish wording"),
            bead("h-epic", kind="epic", phase="in_progress"),
            bead("h-blocked", kind="bug", phase="blocked"),
            bead("h-deferred", kind="chore", phase="deferred"),
            bead("h-gate", kind="gate", labels=["model:opus"]),
            bead("h-event", kind="event"),
            bead("h-internal", kind="molecule"),
            bead("h-unknown-status", phase="tombstoned"),
        )
    )

    plan = engine().plan(TierPass(), store)

    totals = plan.totals
    assert (totals["records"], totals["in_scope"], totals["excluded"], totals["changes"]) == (
        8,
        4,
        4,
        4,
    )
    assert plan.engine is not None
    assert plan.engine["excluded_by_reason"] == {
        "non_routable_status:tombstoned": 1,
        "non_routable_type:event": 1,
        "non_routable_type:gate": 1,
        "non_routable_type:molecule": 1,
    }
    assert plan.report["fallbacks"] == ["h-blocked", "h-closed", "h-deferred", "h-epic"]
    assert totals["by_class"]["DRIFT"] == 4
    assert [bead_ref.id for bead_ref, _ in plan.units()] == [
        "h-blocked",
        "h-closed",
        "h-deferred",
        "h-epic",
    ]
    assert plan.section["pass"] == "tier" and plan.section["schema_version"] == "1"
    assert plan.section["corpus_hash"].startswith("sha256:")


def test_merge_slot_is_counted_but_never_becomes_a_target() -> None:
    title = "Reserved coordination record"
    slot = bead("bh-merge-slot", title=title, labels=[SLOT])
    ordinary = bead("h-ordinary", title=title)

    plan = engine().plan(TierPass(), FakeStore(corpus(slot, ordinary)))
    repeated = engine().plan(TierPass(), FakeStore(corpus(slot, ordinary)))

    assert plan.plan_hash == repeated.plan_hash
    assert plan == repeated
    assert (plan.totals["records"], plan.totals["in_scope"], plan.totals["excluded"]) == (2, 1, 1)
    assert plan.totals["changes"] == 1
    assert plan.engine is not None
    assert plan.engine["excluded_by_reason"] == {f"system_artifact:{SLOT}": 1}
    assert [bead_ref.id for bead_ref, _ in plan.units()] == ["h-ordinary"]
    assert plan.report["fallbacks"] == ["h-ordinary"]
    # The corpus projection ignores the excluded bead entirely.
    assert plan.corpus_hash == engine().plan(TierPass(), FakeStore(ordinary)).corpus_hash

    converged = bead("h-ordinary", title=title, labels=[FALLBACK_TIER])
    second = engine().plan(TierPass(), FakeStore(corpus(slot, converged)))
    assert second.changes == 0
    assert second.events == ()


def test_engine_drops_proposals_for_excluded_beads() -> None:
    slot = bead("bh-merge-slot", labels=[SLOT])
    ordinary = bead("h-ordinary")

    plan = engine().plan(TierPass(careless=True), FakeStore(corpus(slot, ordinary)))

    assert plan.totals["out_of_scope_proposals"] == 1
    assert {event.bead.id for event in plan.events} == {"h-ordinary"}


def test_merge_slot_never_enters_apply_checkpoints_or_verification() -> None:
    title = "Reserved coordination record"
    store = FakeStore(corpus(bead("bh-merge-slot", title=title, labels=[SLOT]), bead("h-ordinary")))
    eng = engine()
    plan = eng.plan(TierPass(), store)

    audit = apply(eng, plan, store, pass_=TierPass())

    assert audit.planned == (key("h-ordinary"),)
    assert audit.attempted == (key("h-ordinary"),)
    assert audit.completed == (key("h-ordinary"),)
    assert all(event.bead.id == "h-ordinary" for event in store.writes)
    assert store.labels()["bh-merge-slot"] == (SLOT,)
    assert audit.verification is not None
    assert audit.verification["pass_errors"] == []
    assert audit.verification["second_dry_run_changes"] == 0


def test_plan_repairs_bad_labels_but_preserves_and_reports_model_history() -> None:
    store = FakeStore(
        corpus(
            bead(
                "h-malformed",
                title="What is a webhook?",
                labels=["tier:medium", "tier:HIGH", "model:sonnet"],
            ),
            bead(
                "h-existing",
                title="What is a webhook?",
                labels=["tier:HIGH", "model:anthropic/claude-opus-4-1"],
            ),
        )
    )

    plan = engine().plan(TierPass(), store)

    (entry,) = plan.entries
    assert entry.bead == ref("h-malformed")
    assert entry.cls is ReconcileClass.DRIFT
    assert set(entry.changed) == {"label:tier:HIGH", "label:tier:LOW", "label:tier:medium"}
    kinds = sorted((event.kind, event.data["label"]) for event in plan.applicable_events())
    assert kinds == [
        ("label_add", FALLBACK_TIER),
        ("label_remove", "tier:HIGH"),
        ("label_remove", "tier:medium"),
    ]
    assert not any(event.data["label"].startswith("model:") for event in plan.events)
    assert plan.report["preserved_model_hints"] == {
        "h-existing": ["model:anthropic/claude-opus-4-1"],
        "h-malformed": ["model:sonnet"],
    }


# -- plan hash and corpus drift ---------------------------------------------------------------


def test_plan_hash_detects_edits_and_apply_refuses_concurrent_corpus_drift() -> None:
    rows = bead("h-1")
    eng = engine()
    plan = eng.plan(TierPass(), FakeStore(rows))

    edited = Plan(
        plan.section,
        tuple(TierPass._event("label_add", ref("h-1"), "tier:HIGH", HISTORY) for _ in (1,)),
        plan.reconcile,
        plan.engine,
        plan.groups,
    )
    with pytest.raises(BackfillError, match="plan hash mismatch"):
        edited.verify()
    tampered_engine = {**(plan.engine or {}), "applicable": []}
    with pytest.raises(BackfillError, match="plan hash mismatch"):
        Plan(plan.section, plan.events, plan.reconcile, tampered_engine, plan.groups).verify()

    store = FakeStore(corpus(rows, bead("h-concurrent")))
    sink = ListSink()
    with pytest.raises(BackfillError, match="corpus changed after planning"):
        apply(eng, plan, store, sink, pass_=TierPass())
    assert not store.exported
    assert store.writes == []
    assert store.dry_runs == 0
    assert sink.entries == []


def test_edited_plan_archive_is_refused(tmp_path: Any) -> None:
    eng = engine()
    plan = eng.plan(TierPass(), FakeStore(bead("h-1")))
    path = tmp_path / "plan.bdlg"
    plan.write(path)
    assert Plan.read(path).plan_hash == plan.plan_hash

    # A rewritten archive is internally consistent (fresh member digests) but its plan hash
    # no longer matches: flip the pass's DRIFT setting in the engine record.
    manifest, events = beadlog.read_archive(path)
    raw = manifest.to_json()
    raw["annotations"]["beadhive.backfill"]["pass"]["drift_applicable"] = False
    beadlog.write_archive(
        path,
        events,
        groups=list(manifest.groups),
        generator=manifest.generator,
        plan=raw["plan"],
        annotations=raw["annotations"],
    )
    with pytest.raises(BackfillError, match="plan hash mismatch"):
        Plan.read(path)


def test_changes_outside_the_projection_do_not_invalidate_a_plan() -> None:
    eng = engine()
    plan = eng.plan(TierPass(), FakeStore(corpus(bead("h-1"), bead("bh-slot", labels=[SLOT]))))
    # A description edit (not an invalidating facet) and a change to an excluded bead.
    store = FakeStore(
        corpus(
            bead("h-1", description="edited after planning"),
            bead("bh-slot", labels=[SLOT, "extra"]),
        )
    )

    audit = apply(eng, plan, store, pass_=TierPass())

    assert audit.state is AuditState.APPLIED
    assert tier_labels(store)["h-1"] == [FALLBACK_TIER]


def test_apply_refuses_a_plan_from_another_pass_or_schema() -> None:
    eng = engine()
    store = FakeStore(bead("h-1"))
    plan = eng.plan(TierPass(), store)

    other = TierPass()
    other.schema_version = "2"  # type: ignore[misc]
    with pytest.raises(BackfillError, match="refusing"):
        apply(eng, plan, store, pass_=other)
    with pytest.raises(BackfillError, match="no pass registered"):
        apply(eng, plan, store)
    with pytest.raises(BackfillError, match="settings differ"):
        apply(eng, plan, store, pass_=TierPass(drift_applicable=False))
    assert store.writes == [] and not store.exported

    registered = engine(passes=[TierPass()])
    assert apply(registered, plan, store).state is AuditState.APPLIED


# -- apply: export first, convergence, audit checkpoints --------------------------------------


def test_apply_exports_first_converges_validates_models_and_is_idempotent(tmp_path: Any) -> None:
    store = FakeStore(
        corpus(
            bead("h-1", labels=["model:sonnet"]),
            bead("h-2", phase="closed", labels=["tier:bogus", "model:opus"]),
            bead("h-gate", kind="gate", labels=["model:haiku"]),
        )
    )
    eng = engine()
    plan = eng.plan(TierPass(), store)
    audit_path = tmp_path / "audit.json"

    audit = apply(eng, plan, store, JsonFileAuditSink(audit_path), pass_=TierPass())

    assert audit.state is AuditState.APPLIED
    assert audit.verification == {
        "pass_errors": [],
        "second_dry_run_changes": 0,
        "unrecorded_judgments": 0,
    }
    assert audit.corpus_hash_after == audit.corpus_hash_expected_after
    assert audit.recovery.pre_state_artifact == "recovery/pre-state.bdlg"
    assert store.pre_state == tuple(
        corpus(
            bead("h-1", labels=["model:sonnet"]),
            bead("h-2", phase="closed", labels=["tier:bogus", "model:opus"]),
            bead("h-gate", kind="gate", labels=["model:haiku"]),
        )
    )
    assert json.loads(audit_path.read_text()) == audit.to_json()
    assert eng.plan(TierPass(), store).changes == 0
    assert model_labels(store) == {
        "h-1": ["model:sonnet"],
        "h-2": ["model:opus"],
        "h-gate": ["model:haiku"],
    }
    assert not any(event.data.get("label", "").startswith("model:") for event in store.writes)


def test_applying_audit_precedes_first_write_and_checkpoints_progress() -> None:
    store = FakeStore(corpus(bead("h-1"), bead("h-2")))
    eng = engine()
    plan = eng.plan(TierPass(), store)
    sink = ListSink()
    snapshots: list[AuditEntry] = []
    store.on_write = lambda event: snapshots.append(sink.entries[-1])

    apply(eng, plan, store, sink, pass_=TierPass())

    first = snapshots[0]
    assert first.state is AuditState.APPLYING
    assert first.recovery.pre_state_artifact == "recovery/pre-state.bdlg"
    assert first.planned == (key("h-1"), key("h-2"))
    assert first.attempted == (key("h-1"),)
    assert first.completed == ()
    assert first.progress.to_json() == {
        "planned": 2,
        "attempted": 1,
        "completed": 0,
        "current": key("h-1"),
        "next_index": 0,
    }
    assert first.recovery.uncertain == key("h-1")

    second = snapshots[1]
    assert second.attempted == (key("h-1"), key("h-2"))
    assert second.completed == (key("h-1"),)
    assert second.progress.completed == 1
    assert second.progress.current == key("h-2")
    assert second.progress.next_index == 1
    assert second.recovery.uncertain == key("h-2")

    states = [entry.state for entry in sink.entries]
    assert states[0] is AuditState.APPLYING and states[-1] is AuditState.APPLIED
    assert sink.entries[-1].progress.next_index == 2
    assert sink.entries[-1].recovery.uncertain is None


def test_unpersistable_applying_audit_refuses_before_any_write() -> None:
    store = FakeStore(bead("h-1"))
    eng = engine()
    plan = eng.plan(TierPass(), store)

    with pytest.raises(BackfillError, match="could not persist applying audit"):
        apply(eng, plan, store, FailingSink(), pass_=TierPass())
    assert store.exported
    assert store.writes == []


# -- apply: drift in the write window, failure, interruption ----------------------------------


class ConcurrentReader:
    """Adds a concurrent in-scope bead to the store's second export (the post-apply read)."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.loads = 0

    def export(self) -> tuple[beadlog.BeadEvent, ...]:
        self.loads += 1
        events = self.store.export()
        if self.loads == 2:
            events += tuple(bead("h-concurrent-task"))
        return events


def test_apply_detects_drift_during_write_window_and_rolls_back() -> None:
    store = FakeStore(bead("h-1"))
    original = store.labels()
    eng = engine()
    plan = eng.plan(TierPass(), store)
    sink = ListSink()

    with pytest.raises(BackfillError, match="post-apply verification failed"):
        apply(eng, plan, store, sink, reader=ConcurrentReader(store), pass_=TierPass())

    assert store.labels() == original
    final = sink.entries[-1]
    assert final.state is AuditState.ROLLED_BACK
    assert final.error is not None and "unexpected corpus drift during apply" in final.error
    assert final.recovery.rollback_complete is True


@pytest.mark.parametrize("interrupted", [False, True])
def test_failed_or_interrupted_apply_rolls_back_and_audits(interrupted: bool) -> None:
    store = FakeStore(corpus(bead("h-1"), bead("h-2", labels=["tier:bad", "model:sonnet"])))
    original = store.labels()
    eng = engine()
    plan = eng.plan(TierPass(), store)

    def marker(event: beadlog.BeadEvent) -> bool:
        return event.bead.id == "h-2" and event.kind == "label_add"

    if interrupted:
        store.interrupt = marker
    else:
        store.fail = marker
    sink = ListSink()

    expected = KeyboardInterrupt if interrupted else BackfillError
    with pytest.raises(expected):
        apply(eng, plan, store, sink, pass_=TierPass())

    assert store.labels() == original
    final = sink.entries[-1]
    assert final.state is AuditState.ROLLED_BACK
    assert final.rollback_failures == ()
    assert final.recovery.rollback_complete is True
    assert final.recovery.uncertain is None and final.progress.current is None
    assert final.attempted == (key("h-1"), key("h-2"))
    assert final.completed == (key("h-1"),)


def test_incomplete_rollback_is_audited_as_rollback_failed() -> None:
    store = FakeStore(corpus(bead("h-1"), bead("h-2")))
    eng = engine()
    plan = eng.plan(TierPass(), store)
    store.fail = lambda event: event.bead.id == "h-2"
    store.fail_inverse = True
    sink = ListSink()

    with pytest.raises(BackfillError, match="rollback was incomplete"):
        apply(eng, plan, store, sink, pass_=TierPass())

    final = sink.entries[-1]
    assert final.state is AuditState.ROLLBACK_FAILED
    assert final.recovery.rollback_complete is False
    assert len(final.rollback_failures) == 1
    assert "injected rollback failure" in final.rollback_failures[0]


def test_post_apply_validation_ignores_model_shape_by_design() -> None:
    store = FakeStore(bead("h-1", labels=["model:slashless-legacy"]))
    eng = engine()
    plan = eng.plan(TierPass(), store)

    audit = apply(eng, plan, store, pass_=TierPass())

    assert audit.state is AuditState.APPLIED
    assert "model:slashless-legacy" in store.labels()["h-1"]


# -- NEW beads: create inverse is a retire, never a delete ------------------------------------


def test_new_beads_apply_and_create_rolls_back_by_retiring_never_deleting() -> None:
    store = FakeStore(bead("h-old", title="Existing unrelated work"))
    eng = engine()
    pass_ = CreatePass({"h-a": "Brand new alpha item", "h-b": "Brand new beta item"})
    plan = eng.plan(pass_, store)
    assert [entry.cls for entry in plan.entries] == [ReconcileClass.NEW, ReconcileClass.NEW]
    assert plan.changes == 2

    store.fail = lambda event: event.bead.id == "h-b" and event.kind == "label_add"
    sink = ListSink()
    with pytest.raises(BackfillError, match="apply failed and was audited"):
        apply(eng, plan, store, sink, pass_=pass_)

    states = store.states()
    for iid in ("h-a", "h-b"):
        state = states[ref(iid)]
        assert state.exists and not state.deleted
        assert state.phase == "retired"
        assert state.retired is not None and state.retired["reason"] == ROLLBACK_REASON
        assert "origin:backfill" not in state.labels
    assert not any(event.kind == "delete" for event in store.events)
    assert sink.entries[-1].state is AuditState.ROLLED_BACK


def test_new_beads_apply_and_converge() -> None:
    store = FakeStore(bead("h-old", title="Existing unrelated work"))
    eng = engine()
    pass_ = CreatePass({"h-a": "Brand new alpha item"})

    audit = apply(eng, eng.plan(pass_, store), store, pass_=pass_)

    assert audit.state is AuditState.APPLIED
    assert store.states()[ref("h-a")].labels == ("origin:backfill",)
    assert eng.plan(pass_, store).changes == 0


def test_irreversible_events_leave_residue_and_a_failed_rollback_state() -> None:
    store = FakeStore([])
    eng = engine()
    pass_ = CreatePass({"h-a": "Brand new alpha item", "h-b": "Brand new beta item"}, comment=True)
    plan = eng.plan(pass_, store)
    assert plan.totals["irreversible"] == 2
    store.fail = lambda event: event.bead.id == "h-b"
    sink = ListSink()

    with pytest.raises(BackfillError, match="rollback was incomplete"):
        apply(eng, plan, store, sink, pass_=pass_)

    final = sink.entries[-1]
    assert final.state is AuditState.ROLLBACK_FAILED
    assert len(final.rollback_residual) == 1
    assert final.rollback_failures == ()


# -- recorded judgments -----------------------------------------------------------------------


def test_judged_plan_applies_and_reverifies_without_invoking_the_judge(tmp_path: Any) -> None:
    store = FakeStore(
        corpus(
            bead("h-1", title="Deep reasoning about consensus"),
            bead("h-2", title="Rename a variable"),
        )
    )
    eng = engine()
    judge = CountingJudge()
    plan = eng.plan(TierPass(), store, judge)
    assert judge.calls == 2
    assert plan.section["judge"] == {
        "engine": "counting-judge",
        "version": "1",
        "model": "fake-model",
    }
    assert len(plan.engine["judgments"]) == 2 if plan.engine else False
    path = tmp_path / "plan.bdlg"
    plan.write(path)

    audit = apply(eng, Plan.read(path), store, pass_=TierPass())

    assert judge.calls == 2  # never asked again at apply time or during re-verification
    assert audit.state is AuditState.APPLIED
    assert audit.verification is not None
    assert audit.verification["unrecorded_judgments"] == 0
    assert tier_labels(store) == {"h-1": ["tier:HIGH"], "h-2": ["tier:LOW"]}


def test_recorded_only_dry_run_never_invokes_a_judge_and_counts_misses() -> None:
    store = FakeStore(corpus(bead("h-1", title="Deep reasoning about consensus")))
    eng = engine()
    judge = CountingJudge()
    plan = eng.plan(TierPass(), store, judge)

    replay = eng.plan(TierPass(), store, prior=plan)
    assert judge.calls == 1
    assert replay.totals["unrecorded_judgments"] == 0
    assert replay.applicable_events() and replay.changes == plan.changes

    store.events += bead("h-new", title="Another question entirely")
    widened = eng.plan(TierPass(), store, prior=plan)
    assert judge.calls == 1
    assert widened.totals["unrecorded_judgments"] == 1


def test_reconcile_judgments_are_recorded_and_reused() -> None:
    store = FakeStore(bead("h-old", title="Write the release notes"))
    eng = engine()
    judge = CountingJudge()
    pass_ = CreatePass({"h-new": "Write release notes draft"})
    plan = eng.plan(pass_, store, judge)
    (entry,) = plan.entries
    assert entry.verdict is not None and entry.verdict.judgment.engine == "counting-judge"
    calls = judge.calls

    audit = apply(eng, plan, store, pass_=pass_)

    assert audit.state is AuditState.APPLIED
    assert judge.calls == calls
    assert eng.plan(pass_, store, prior=plan).changes == 0


# -- DRIFT ------------------------------------------------------------------------------------


def test_drift_is_reported_and_never_applied_unless_the_pass_marks_it_applicable() -> None:
    store = FakeStore(corpus(bead("h-1"), bead("h-2", labels=["tier:bogus"])))
    original = store.labels()
    eng = engine()
    pass_ = TierPass(drift_applicable=False, verifies=False)

    plan = eng.plan(pass_, store)

    assert plan.totals["by_class"]["DRIFT"] == 2
    assert plan.totals["drift_reported"] == 2
    assert len(plan.drift_reported()) == 2
    assert plan.events  # reported in the plan ...
    assert plan.applicable_events() == () and plan.changes == 0  # ... but never applied
    audit = apply(eng, plan, store, pass_=pass_)
    assert audit.state is AuditState.APPLIED
    assert audit.planned == ()
    assert store.writes == []
    assert store.labels() == original

    applicable = eng.plan(TierPass(drift_applicable=True), store)
    assert applicable.changes == 2


# -- losses -----------------------------------------------------------------------------------


def test_erasure_incomplete_loss_needs_acknowledgement_before_export() -> None:
    store = FakeStore(bead("h-1"))
    store.loss_codes = ["erasure.incomplete"]
    eng = engine()
    plan = eng.plan(TierPass(), store)

    with pytest.raises(BackfillError, match="erasure.incomplete"):
        apply(eng, plan, store, pass_=TierPass())
    assert not store.exported and store.writes == []

    audit = apply(eng, plan, store, pass_=TierPass(), acknowledge_erasure=True)
    assert audit.state is AuditState.APPLIED
    assert audit.losses == {"erasure.incomplete": 1}


# -- plan archives ----------------------------------------------------------------------------


def test_plan_archive_round_trips_and_neutral_tools_read_its_classification() -> None:
    store = FakeStore(corpus(bead("h-1"), bead("h-2", labels=["tier:LOW"])))
    plan = engine().plan(TierPass(), store)
    buffer = io.BytesIO()

    manifest = plan.write(buffer)

    assert manifest.plan is not None and manifest.plan["plan_hash"] == plan.plan_hash
    assert "plan" in manifest.capabilities.optional
    buffer.seek(0)
    read = Plan.read(buffer)
    assert read.plan_hash == plan.plan_hash
    assert read.applicable_events() == plan.applicable_events()
    assert [entry.bead.id for entry in plan_entries(manifest)] == ["h-1"]


def test_a_neutral_reconcile_plan_applies_new_beads_only() -> None:
    baseline = corpus(bead("h-1", title="Existing unrelated work"))
    store = FakeStore(baseline)
    proposed = [
        *CreatePass({"h-new": "Brand new gamma item"}).propose(
            (), extracted_at="2026-10-31T00:00:00Z", judge=CountingJudge()
        ),
        TierPass._event("label_add", ref("h-1"), "tier:LOW", "2026-10-31T00:00:00Z"),
    ]
    neutral = reconcile(proposed, baseline)
    buffer = io.BytesIO()
    write_plan(buffer, neutral, groups=[GROUP], generator=beadlog.Generator("t", "0"))
    buffer.seek(0)
    plan = Plan.read(buffer)
    assert plan.neutral and plan.pass_name == "reconcile"
    eng = engine()

    with pytest.raises(BackfillError, match="without a pass"):
        apply(eng, plan, store, pass_=TierPass())
    audit = apply(eng, plan, store)

    assert audit.state is AuditState.APPLIED
    assert audit.planned == (key("h-new"),)
    assert ref("h-new") in store.states()
    assert "tier:LOW" not in store.labels()["h-1"]  # DRIFT is reported, not applied

    drifted = FakeStore(corpus(baseline, bead("h-other")))
    with pytest.raises(BackfillError, match="corpus changed after planning"):
        apply(eng, plan, drifted)


def test_file_sinks_persist_the_pre_state_archive_and_the_rollback_audit(tmp_path: Any) -> None:
    store = FakeStore(corpus(bead("h-1"), bead("h-2")))
    eng = engine()
    plan = eng.plan(TierPass(), store)
    store.fail = lambda event: event.bead.id == "h-2"
    store.exported = True  # the archive exporter below is the real pre-state port here
    pre_state = ArchivePreStateExporter(tmp_path / "pre-state.bdlg", groups=[GROUP])
    audit_path = tmp_path / "audit.json"

    with pytest.raises(BackfillError):
        eng.apply(
            plan,
            store,
            store,
            JsonFileAuditSink(audit_path),
            pre_state=pre_state,
            pass_=TierPass(),
        )

    _, saved = beadlog.read_archive(tmp_path / "pre-state.bdlg")
    assert beadlog.fold(saved) == beadlog.fold(corpus(bead("h-1"), bead("h-2")))
    audit = json.loads(audit_path.read_text())
    assert audit["state"] == "rolled_back"
    assert audit["recovery"]["pre_state_artifact"] == str(tmp_path / "pre-state.bdlg")
    assert audit["recovery"]["rollback_complete"] is True
    assert audit["progress"]["current"] is None
