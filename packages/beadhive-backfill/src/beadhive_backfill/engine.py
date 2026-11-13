"""The hive apply envelope, lifted from ``complexity_backfill`` (platform ADR section 2).

:meth:`BackfillEngine.plan` reads a fresh baseline, lets a pass propose events, classifies them
with :func:`beadlog_sources.reconcile` (NEW / PRESENT / NOISE / DRIFT) and seals a
:class:`~beadhive_backfill.plan.Plan` whose corpus hash is the pass's projection of the
baseline. :meth:`BackfillEngine.apply` applies one verified plan through the store ports:

1. verify the plan hash (an edited plan is refused);
2. read a fresh baseline and refuse corpus drift **before** any export or write;
3. assess losses with a loader dry run (``erasure.incomplete`` needs acknowledgement);
4. export the pre-state, then persist the ``applying`` audit, both before the first write;
5. write one bead per unit, checkpointing ``progress`` / ``recovery`` around each write;
6. verify: the whole projection equals the expected post-state (drift inside the write window
   fails), the pass postcondition holds, and a second dry run over the fresh store proposes
   nothing using the plan's RECORDED judgments only (no judge is invoked);
7. on any failure or interruption, replay the loader inverses of every touched unit in reverse
   and record ``rolled_back`` (or ``rollback_failed``) before re-raising.

Nothing here knows about bd or a hive: those arrive through :class:`StoreReader`,
:class:`StoreLoader`, :class:`PreStateExporter` and :class:`AuditSink`.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from beadlog import (
    BeadEvent,
    BeadRef,
    BeadState,
    EventKind,
    Group,
    IdCollisionError,
    changed_facets,
    fold,
    format_timestamp,
    union_merge,
)
from beadlog_sources import DeterministicJudge, JudgmentPort, ReconcileClass, reconcile

from ._errors import BackfillError
from ._inverse import Stamper, has_inverse, inverse_of
from ._judgments import JudgeTap, verdicts_to_json
from ._projection import ALL_FACETS, Scope, bead_key, check_facets, projection_hash
from .contracts import (
    RETIRED_INSTRUCTION,
    AuditEntry,
    AuditProgress,
    AuditRecovery,
    AuditSink,
    AuditState,
    HivePass,
    PreStateExporter,
    ReportingPass,
    ScopedPass,
    StoreLoader,
    StoreReader,
    VerifyingPass,
)
from .plan import ENGINE_SCHEMA, Plan

Clock = Callable[[], str]
ERASURE_INCOMPLETE = "erasure.incomplete"


def system_clock() -> str:
    """The wall clock as a canonical BeadLog timestamp (the default; tests inject their own)."""
    return format_timestamp(datetime.now(UTC))


def _exclusion(pass_: HivePass | None) -> Callable[[BeadState], str | None] | None:
    return pass_.exclusion if isinstance(pass_, ScopedPass) else None


def _loss_counts(report: Mapping[str, Any]) -> dict[str, int]:
    """Loss codes of a loss report: its ``summary.by_code``, else counted from ``losses``."""
    summary = report.get("summary")
    by_code = summary.get("by_code") if isinstance(summary, Mapping) else None
    if isinstance(by_code, Mapping) and by_code:
        counts = Counter({str(code): int(count) for code, count in by_code.items()})
    else:
        counts = Counter(str(loss.get("code", "")) for loss in report.get("losses") or ())
    return {code: count for code, count in sorted(counts.items()) if code and count}


def _by_bead(events: Iterable[BeadEvent]) -> dict[BeadRef, list[BeadEvent]]:
    grouped: dict[BeadRef, list[BeadEvent]] = {}
    for event in events:
        grouped.setdefault(event.bead, []).append(event)
    return grouped


class BackfillEngine:
    """Plans and applies hive backfill passes inside the complexity-proven safety envelope.

    ``groups`` are declared in written plans; ``clock`` stamps ``extracted_at`` and rollback
    inverses (inject a fake one in tests); ``passes`` are looked up by name when a saved plan
    is applied; ``auto_accept`` maps a judgment family to the confidence at which reconcile
    needs no review.
    """

    def __init__(
        self,
        *,
        groups: Sequence[Group] = (),
        clock: Clock = system_clock,
        passes: Iterable[HivePass] = (),
        auto_accept: Mapping[str, float] | None = None,
    ) -> None:
        self.groups = tuple(groups)
        self._clock = clock
        self._passes = {pass_.name: pass_ for pass_ in passes}
        self._auto_accept = dict(auto_accept or {})

    # -- plan ----------------------------------------------------------------------------

    def plan(
        self,
        pass_: HivePass,
        reader: StoreReader,
        judge: JudgmentPort | None = None,
        *,
        prior: Plan | None = None,
    ) -> Plan:
        """Dry run: propose against a fresh baseline, reconcile, and seal a plan.

        ``judge`` answers what the deterministic engine cannot (default: deterministic only).
        With ``prior``, verdicts recorded in that plan are reused and never asked again; with
        ``prior`` and no ``judge`` the dry run uses recorded judgments only.
        """
        replay_only = prior is not None and judge is None
        return self._plan(pass_, tuple(reader.export()), judge, prior=prior, replay=replay_only)

    def _plan(
        self,
        pass_: HivePass,
        baseline: tuple[BeadEvent, ...],
        judge: JudgmentPort | None,
        *,
        prior: Plan | None,
        replay: bool,
    ) -> Plan:
        facets = check_facets(pass_.invalidating_fields)
        extracted_at = self._clock()
        states = fold(baseline)
        exclusion = _exclusion(pass_)
        scope = Scope.of(states, exclusion)

        recorded = prior.pass_verdicts() if prior is not None else {}
        fallback = None if replay else (judge if judge is not None else DeterministicJudge())
        pass_tap = JudgeTap(recorded, fallback)
        proposed = tuple(pass_.propose(baseline, extracted_at=extracted_at, judge=pass_tap))
        kept = tuple(event for event in proposed if event.bead not in scope.excluded)
        dropped = len(proposed) - len(kept)

        reconcile_tap = JudgeTap({}) if replay else None
        try:
            result = reconcile(
                kept,
                baseline,
                reconcile_tap if replay else judge,
                auto_accept=self._auto_accept,
                prior=prior.reconcile_verdicts() if prior is not None else None,
            )
        except IdCollisionError as exc:
            raise BackfillError(f"proposal collides with the baseline: {exc}") from exc

        drift_applicable = bool(pass_.drift_applicable)
        applicable = {
            event_id
            for entry in result.entries
            if entry.cls is ReconcileClass.NEW
            or (entry.cls is ReconcileClass.DRIFT and drift_applicable)
            for event_id in entry.events
        }
        by_id = {event.id: event for event in result.events}
        in_scope = {bead: states[bead] for bead in scope.included}
        report = dict(pass_.report(in_scope, kept)) if isinstance(pass_, ReportingPass) else {}
        unrecorded = pass_tap.misses + (reconcile_tap.misses if reconcile_tap else 0)
        engine = {
            "schema_version": ENGINE_SCHEMA,
            "pass": {
                "name": pass_.name,
                "schema_version": pass_.schema_version,
                "invalidating_fields": sorted(facets),
                "drift_applicable": drift_applicable,
                "scoped": exclusion is not None,
            },
            "extracted_at": extracted_at,
            "totals": {
                "records": len(states),
                "in_scope": len(scope.included),
                "excluded": len(scope.excluded),
                "proposed_beads": len({event.bead for event in kept}),
                "out_of_scope_proposals": dropped,
                "changes": len({by_id[event_id].bead for event_id in applicable}),
                "by_class": result.counts(),
                "drift_reported": 0
                if drift_applicable
                else sum(entry.cls is ReconcileClass.DRIFT for entry in result.entries),
                "review_required": result.annotation()["review_required"],
                "irreversible": sum(not has_inverse(by_id[event_id]) for event_id in applicable),
                "judge_calls": result.calls,
                "unrecorded_judgments": unrecorded,
            },
            "excluded_by_reason": scope.by_reason(),
            "applicable": sorted(applicable),
            "judgments": verdicts_to_json(pass_tap.verdicts),
            "report": report,
        }
        section = {
            "pass": pass_.name,
            "schema_version": pass_.schema_version,
            "corpus_hash": projection_hash(states, facets, exclusion),
            "judge": dict(result.judge),
        }
        return Plan(section, result.events, result.annotation(), engine, self.groups).sealed()

    # -- apply ---------------------------------------------------------------------------

    def _resolve(self, plan: Plan, pass_: HivePass | None) -> HivePass | None:
        if plan.neutral:
            if pass_ is not None:
                raise BackfillError("a neutral reconcile plan is applied without a pass")
            return None
        chosen = pass_ if pass_ is not None else self._passes.get(plan.pass_name)
        if chosen is None:
            raise BackfillError(f"no pass registered for plan pass {plan.pass_name!r}")
        if chosen.name != plan.pass_name or chosen.schema_version != plan.schema_version:
            raise BackfillError(
                f"plan was made by pass {plan.pass_name!r} schema {plan.schema_version!r}; "
                f"refusing {chosen.name!r} schema {chosen.schema_version!r}"
            )
        recorded = plan.engine.get("pass", {}) if plan.engine else {}
        if sorted(check_facets(chosen.invalidating_fields)) != recorded.get(
            "invalidating_fields"
        ) or bool(chosen.drift_applicable) != bool(recorded.get("drift_applicable")):
            raise BackfillError("pass settings differ from the ones recorded in the plan")
        return chosen

    def _corpus_hash(self, plan: Plan, pass_: HivePass | None, events: Sequence[BeadEvent]) -> str:
        if pass_ is None:  # a neutral plan recorded reconcile's event-id corpus hash
            return reconcile((), events).corpus_hash
        return projection_hash(fold(events), pass_.invalidating_fields, _exclusion(pass_))

    def _verification_hash(
        self, pass_: HivePass | None, states: Mapping[BeadRef, BeadState]
    ) -> str:
        if pass_ is None:
            return projection_hash(states, ALL_FACETS)
        return projection_hash(states, pass_.invalidating_fields, _exclusion(pass_))

    def apply(
        self,
        plan: Plan,
        reader: StoreReader,
        loader: StoreLoader,
        audit: AuditSink,
        *,
        pre_state: PreStateExporter,
        pass_: HivePass | None = None,
        acknowledge_erasure: bool = False,
    ) -> AuditEntry:
        """Apply one verified plan; return the ``applied`` audit entry.

        Raises :class:`BackfillError` on a refusal (nothing exported or written) or on an
        audited, rolled-back failure. ``KeyboardInterrupt`` / ``SystemExit`` roll back, are
        audited and are re-raised.
        """
        plan.verify()
        pass_ = self._resolve(plan, pass_)
        before = tuple(reader.export())
        actual = self._corpus_hash(plan, pass_, before)
        if actual != plan.corpus_hash:
            raise BackfillError(
                f"corpus changed after planning (planned {plan.corpus_hash}, current {actual})"
                " — rerun dry-run"
            )

        units = plan.units()
        applicable = plan.applicable_events()
        expected_after = self._verification_hash(pass_, fold(union_merge([before, applicable])))
        losses: dict[str, int] = {}
        if applicable:
            losses = _loss_counts(loader.load(applicable, dry_run=True))
            if losses.get(ERASURE_INCOMPLETE) and not acknowledge_erasure:
                raise BackfillError(
                    f"loader reports {ERASURE_INCOMPLETE} for {losses[ERASURE_INCOMPLETE]} "
                    "event(s); erased content may survive in store history — acknowledge to "
                    "apply"
                )

        planned = tuple(bead_key(bead) for bead, _ in units)
        artifact = pre_state.export_pre_state(before)  # recovery exists before any write
        entry = AuditEntry(
            pass_name=plan.pass_name,
            plan_hash=plan.plan_hash,
            state=AuditState.APPLYING,
            progress=AuditProgress(
                planned=len(planned), attempted=0, completed=0, next_index=0, current=None
            ),
            recovery=AuditRecovery(pre_state_artifact=artifact),
            corpus_hash_before=actual,
            corpus_hash_expected_after=expected_after,
            planned=planned,
            losses=losses,
        )
        # The durable crash marker: after the export, before the first write, so a crash can
        # never leave a partial apply without an ``applying`` record.
        try:
            audit.record(entry)
        except Exception as exc:
            raise BackfillError(f"could not persist applying audit: {exc}") from exc

        touched: list[tuple[BeadRef, tuple[BeadEvent, ...]]] = []
        try:
            for index, (bead, events) in enumerate(units):
                key = bead_key(bead)
                entry = replace(
                    entry,
                    attempted=(*entry.attempted, key),
                    progress=replace(
                        entry.progress,
                        attempted=len(entry.attempted) + 1,
                        current=key,
                        next_index=index,
                    ),
                    recovery=replace(entry.recovery, uncertain=key),
                )
                audit.record(entry)
                touched.append((bead, events))
                loader.load(events, dry_run=False)
                entry = replace(
                    entry,
                    completed=(*entry.completed, key),
                    progress=replace(
                        entry.progress,
                        completed=len(entry.completed) + 1,
                        current=None,
                        next_index=index + 1,
                    ),
                    recovery=replace(entry.recovery, uncertain=None),
                )
                audit.record(entry)

            after = tuple(reader.export())
            after_states = fold(after)
            after_hash = self._verification_hash(pass_, after_states)
            verification = self._verify(plan, pass_, after, after_states)
            details = []
            if after_hash != expected_after:
                details.append("unexpected corpus drift during apply")
            if verification["pass_errors"]:
                details.append(f"pass verification errors={len(verification['pass_errors'])}")
            if verification["second_dry_run_changes"]:
                details.append(f"second-dry-run changes={verification['second_dry_run_changes']}")
            if verification["unrecorded_judgments"]:
                details.append(
                    f"second-dry-run unrecorded judgments={verification['unrecorded_judgments']}"
                )
            if details:
                raise BackfillError("post-apply verification failed: " + ", ".join(details))

            entry = replace(
                entry,
                state=AuditState.APPLIED,
                corpus_hash_after=after_hash,
                verification=verification,
            )
            audit.record(entry)
            return entry
        except BaseException as exc:
            entry = self._rollback(plan, reader, loader, before, touched, entry, exc)
            try:
                audit.record(entry)
            except Exception as audit_exc:
                raise BackfillError(
                    f"apply failed ({exc}) and its rollback audit could not be persisted: "
                    f"{audit_exc}"
                ) from exc
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            suffix = "" if entry.state is AuditState.ROLLED_BACK else "; rollback was incomplete"
            raise BackfillError(f"apply failed and was audited{suffix}: {exc}") from exc

    def _verify(
        self,
        plan: Plan,
        pass_: HivePass | None,
        after: tuple[BeadEvent, ...],
        after_states: Mapping[BeadRef, BeadState],
    ) -> dict[str, Any]:
        """The post-apply proof: pass postcondition and a recorded-judgments-only dry run."""
        if pass_ is None:
            tap = JudgeTap({})
            second = reconcile(plan.events, after, tap, prior=plan.reconcile_verdicts())
            changes = len(
                {entry.bead for entry in second.entries if entry.cls is ReconcileClass.NEW}
            )
            return {
                "pass_errors": [],
                "second_dry_run_changes": changes,
                "unrecorded_judgments": tap.misses,
            }
        errors: list[str] = []
        if isinstance(pass_, VerifyingPass):
            scope = Scope.of(after_states, _exclusion(pass_))
            errors = list(pass_.verify({bead: after_states[bead] for bead in scope.included}))
        second = self._plan(pass_, after, None, prior=plan, replay=True)
        return {
            "pass_errors": errors,
            "second_dry_run_changes": second.changes,
            "unrecorded_judgments": int(second.totals.get("unrecorded_judgments", 0)),
        }

    # -- rollback ------------------------------------------------------------------------

    def _rollback(
        self,
        plan: Plan,
        reader: StoreReader,
        loader: StoreLoader,
        before: tuple[BeadEvent, ...],
        touched: Sequence[tuple[BeadRef, tuple[BeadEvent, ...]]],
        entry: AuditEntry,
        exc: BaseException,
    ) -> AuditEntry:
        """Replay the inverses of every touched unit's events in reverse order.

        Each inverse restores the bead's fold just before the event it undoes. A fresh read of
        the store skips inverses that would change nothing (the uncertain unit may be only
        partly written); when that read fails every inverse is replayed. Failures are collected
        and rollback continues across independent writes.
        """
        try:
            current: dict[BeadRef, list[BeadEvent]] | None = _by_bead(reader.export())
        except BaseException:  # a failed read must never suppress best-effort rollback
            current = None
        uncertain = entry.recovery.uncertain
        applied = [event for _, events in touched for event in events]
        seen = [*before, *applied, *(e for es in (current or {}).values() for e in es)]
        stamp = Stamper(self._clock(), seen)
        prior_events = _by_bead(before)
        failures: list[str] = []
        residual: list[str] = []
        for bead, events in reversed(touched):
            base = prior_events.get(bead, [])
            states = [fold([*base, *events[:index]]).get(bead) for index in range(len(events))]
            for event, state in reversed(list(zip(events, states, strict=True))):
                inverses = inverse_of(event, state, stamp=stamp, scope=plan.plan_hash)
                if inverses is None:
                    # No inverse by design: residue, unless the read proves the bead unwritten.
                    if current is None or fold(current.get(bead, [])).get(bead) is not None:
                        residual.append(event.id)
                    continue
                for inverse in inverses:
                    if current is not None:
                        now = current.setdefault(bead, [])
                        state_now = fold(now).get(bead)
                        if state_now is None and inverse.kind is not EventKind.CREATE:
                            if bead_key(bead) != uncertain:
                                # a completed unit's bead is gone from the store: not "never
                                # written", so the undo cannot be confirmed (bh-12hka)
                                note = f"{bead.id}: missing from the store"
                                if note not in residual:
                                    residual.append(note)
                            continue  # the uncertain unit may never have been written
                        if not changed_facets(state_now, fold([*now, inverse]).get(bead)):
                            continue
                    try:
                        loader.load([inverse], dry_run=False)
                    except BaseException as failure:  # continue across independent writes
                        failures.append(
                            f"{inverse.kind} {bead_key(bead)} (undoing {event.id}): "
                            f"{type(failure).__name__}: {failure}"
                        )
                        continue
                    if current is not None:
                        current[bead].append(inverse)
        retired = self._created_beads(reader, touched, prior_events, uncertain, residual)
        complete = not failures and not residual
        instruction = RETIRED_INSTRUCTION if retired else entry.recovery.instruction
        return replace(
            entry,
            state=AuditState.ROLLED_BACK if complete else AuditState.ROLLBACK_FAILED,
            error=f"{type(exc).__name__}: {exc}",
            rollback_failures=(*entry.rollback_failures, *failures),
            rollback_residual=tuple(residual),
            rollback_retired=retired,
            progress=replace(entry.progress, current=None),
            recovery=replace(
                entry.recovery,
                uncertain=None,
                rollback_complete=complete,
                instruction=instruction,
            ),
        )

    def _created_beads(
        self,
        reader: StoreReader,
        touched: Sequence[tuple[BeadRef, tuple[BeadEvent, ...]]],
        prior_events: Mapping[BeadRef, list[BeadEvent]],
        uncertain: str | None,
        residual: list[str],
    ) -> tuple[str, ...]:
        """The ids of the beads this apply created that rollback retired.

        Re-reads the store: a created bead that is not retired afterwards (still open, or
        missing) is appended to ``residual`` rather than reported as undone, and when the read
        fails every created bead is reported unverified.
        """
        created = sorted(
            {
                bead.id
                for bead, events in touched
                if any(e.kind is EventKind.CREATE for e in events)
                and fold(prior_events.get(bead, [])).get(bead) is None
            }
        )
        if not created:
            return ()
        group = {bead.id: bead for bead, _ in touched}
        try:
            states = fold(reader.export())
        except BaseException:
            residual.extend(
                f"{ident}: retirement unverified (the store could not be read)" for ident in created
            )
            return ()
        retired: list[str] = []
        for ident in created:
            state = states.get(group[ident])
            if state is not None and state.phase == "retired":
                retired.append(ident)
            elif state is None and bead_key(group[ident]) == uncertain:
                continue  # the interrupted create may never have been written
            elif not any(item.startswith(f"{ident}:") for item in residual):
                found = "missing from the store" if state is None else f"still {state.phase}"
                residual.append(f"{ident}: created by the apply but {found} after rollback")
        return tuple(retired)
