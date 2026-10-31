"""Safe full-corpus migration onto the provider-neutral complexity label contract.

``bh backfill complexity`` and its flat alias ``bh backfill-complexity`` (published wire surface)
run this command. The policy and the safety envelope live in the ``beadhive-backfill`` library:
:mod:`beadhive_backfill.complexity` is complexity as pass #1 of the backfill engine, and its JSON
plan/audit contract. This module is root wiring only: the tier vocabulary and classifier from
:mod:`beadhive.complexity` (the pass's ``ComplexityRules`` port), the sanctioned ``bd`` reads and
writes, and the CLI. See ``docs/BACKFILL.md``.

The preview and apply phases are intentionally separate.  A preview captures the complete
``bd list --all --include-infra`` view, hashes the inputs that affect scope or classification,
and may be saved as a plan.  Apply accepts only that saved plan, exports the pre-state before
the first mutation, refuses corpus drift, rolls back ordinary write failures/interruption, and
verifies both the whole in-scope corpus and an idempotent second plan.

Legacy ``model:`` labels are historical intent.  They are report inputs only: this module never
passes one to ``bd label remove`` or ``bd label add``.  In particular, old slashless aliases are
preserved even though today's generic routing validation quite correctly reports their shape.
This migration's postcondition is exactly one canonical complexity label, not a rewrite of model
history.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer

from beadhive_backfill import BackfillError
from beadhive_backfill.complexity import (
    MIGRATION,
    ROUTABLE_STATUSES,
    SCHEMA_VERSION,
    SYSTEM_ARTIFACT_LABELS,
    Classification,
    ComplexityPass,
    read_plan,
    verify_plan,
    write_plan,
)
from beadhive_backfill.complexity import apply_plan as _apply_plan
from beadhive_backfill.complexity import build_plan as _build_plan
from beadhive_backfill.complexity import complexity_errors as _errors
from beadhive_backfill.complexity import corpus_hash as _corpus_hash

from . import bd, complexity, config, registry
from .identity import resolve_actor

_PLAN_OPTION = typer.Option(
    None, "--plan", help="write this plan on dry-run; required and read back on --apply"
)
_PRE_STATE_OPTION = typer.Option(
    None, "--pre-state", help="JSONL recovery export (default: beside the plan)"
)
_AUDIT_OPTION = typer.Option(None, "--audit", help="machine audit JSON (default: beside the plan)")


@dataclass(frozen=True)
class ClassifierRules:
    """The pass's ``ComplexityRules`` port over :mod:`beadhive.complexity` and a classifier."""

    classifier: complexity.ComplexityClassifier = complexity.DEFAULT_CLASSIFIER
    label_prefix: str = complexity.COMPLEXITY_LABEL_PREFIX
    model_prefix: str = complexity.MODEL_LABEL_PREFIX

    @property
    def source(self) -> str:
        return str(getattr(self.classifier, "source", "unknown"))

    @property
    def version(self) -> str:
        return str(getattr(self.classifier, "version", "unknown"))

    def parse_label(self, label: str) -> str:
        return complexity.parse_complexity_label(label).name

    def label(self, tier: str) -> str:
        return complexity.ComplexityTier.parse(tier).label

    def routable_type(self, issue_type: str) -> bool:
        return complexity.is_routable_issue_type(issue_type)

    def valid_model(self, value: str) -> bool:
        return complexity.valid_model_preference(value)

    def classify(self, bead: Mapping[str, Any]) -> Classification:
        result = self.classifier.classify(complexity.stable_bead_text(bead), required=True)
        return Classification(
            tier=None if result.tier is None else result.tier.name,
            score=result.score,
            source=result.source,
            version=result.version,
            fallback_used=result.fallback_used,
        )


def rules(
    classifier: complexity.ComplexityClassifier = complexity.DEFAULT_CLASSIFIER,
) -> ClassifierRules:
    """The ``ComplexityRules`` the pass decides with."""
    return ClassifierRules(classifier)


def complexity_pass(
    classifier: complexity.ComplexityClassifier = complexity.DEFAULT_CLASSIFIER,
) -> ComplexityPass:
    """Complexity as an engine pass (registered for ``bh backfill apply`` of ``.bdlg`` plans)."""
    return ComplexityPass(rules(classifier))


def corpus_hash(records: Iterable[Mapping[str, Any]]) -> str:
    return _corpus_hash(records, rules())


def build_plan(
    records: Iterable[Mapping[str, Any]],
    classifier: complexity.ComplexityClassifier = complexity.DEFAULT_CLASSIFIER,
) -> dict[str, Any]:
    """Build a deterministic, JSON-serializable migration plan over a complete corpus read."""
    return _build_plan(records, rules(classifier))


def _complexity_errors(records: Iterable[Mapping[str, Any]]) -> list[str]:
    return _errors(records, rules())


def apply_plan(
    plan: Mapping[str, Any],
    *,
    load_records: Callable[[], list[dict[str, Any]]],
    mutate: Callable[[str, str, str], None],
    export_pre_state: Callable[[], None],
    audit_path: Path,
    classifier: complexity.ComplexityClassifier = complexity.DEFAULT_CLASSIFIER,
    pre_state_artifact: str = "",
) -> dict[str, Any]:
    """Apply one verified plan through injected sanctioned reads/writes (the engine envelope).

    Injection keeps the transaction policy testable without a live hive.  Production passes
    ``bd list``, one-label-at-a-time ``bd label add/remove``, and ``bd export --all`` adapters.
    """
    return _apply_plan(
        plan,
        rules=rules(classifier),
        load_records=load_records,
        mutate=mutate,
        export_pre_state=export_pre_state,
        audit_path=audit_path,
        pre_state_artifact=pre_state_artifact,
    )


def enumerate_corpus(cwd: Path | str) -> list[dict[str, Any]]:
    rows = bd.json(["list", "--all", "--include-infra", "--limit", "0"], cwd)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise BackfillError("could not enumerate the complete bead corpus")
    return rows


def _render_report(plan: Mapping[str, Any]) -> None:
    totals = plan["totals"]
    typer.echo(
        f"corpus={totals['records']} in-scope={totals['in_scope']} "
        f"excluded={totals['excluded']} changes={totals['changes']}"
    )
    for heading, field in (
        ("tier", "by_tier"),
        ("status", "by_status"),
        ("type", "by_type"),
        ("excluded", "excluded_by_reason"),
        ("existing", "existing_complexity"),
    ):
        values = ", ".join(f"{key}={value}" for key, value in plan[field].items()) or "none"
        typer.echo(f"{heading}: {values}")
    typer.echo(f"UNKNOWN→MEDIUM fallbacks: {len(plan['unknown_to_medium_fallbacks'])}")
    typer.echo(f"duplicate/invalid complexity: {len(plan['duplicate_or_invalid_complexity'])}")
    typer.echo(f"preserved model hints: {len(plan['preserved_model_hints'])}")
    typer.echo(f"model-vs-score disagreements: {len(plan['model_score_disagreements'])}")
    typer.echo(f"plan hash: {plan['plan_hash']}")


def command(
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
    apply: bool = typer.Option(False, "--apply", help="apply a previously saved --plan"),
    dry_run: bool = typer.Option(False, "--dry-run", help="explicit preview mode (the default)"),
    plan_path: Path | None = _PLAN_OPTION,
    pre_state: Path | None = _PRE_STATE_OPTION,
    audit: Path | None = _AUDIT_OPTION,
    as_json: bool = typer.Option(False, "--json", help="emit the plan/audit as JSON"),
) -> None:
    """Preview or apply an idempotent complexity-label backfill over the complete corpus."""
    if apply and dry_run:
        raise typer.BadParameter("choose either --dry-run or --apply")
    cfg = config.load()
    cwd = Path(registry.hive_dir_for(cfg, hive))

    try:
        if not apply:
            plan = build_plan(enumerate_corpus(cwd))
            if plan_path is not None:
                write_plan(plan_path, plan)
            if as_json:
                typer.echo(json.dumps(plan, sort_keys=True, indent=2))
            else:
                _render_report(plan)
                if plan_path is not None:
                    typer.echo(f"saved plan: {plan_path}")
            return

        if plan_path is None:
            raise BackfillError("--apply requires --plan from a prior dry-run")
        plan = read_plan(plan_path)
        pre_state = pre_state or plan_path.with_suffix(".pre-state.jsonl")
        audit = audit or plan_path.with_suffix(".audit.json")
        actor = resolve_actor("", "", cwd=cwd)

        def mutate(operation: str, iid: str, label: str) -> None:
            result = bd.run(["label", operation, iid, label], cwd, actor=actor)
            if result.returncode != 0:
                detail = bd.err_detail(result)
                raise BackfillError(f"bd label {operation} {iid} {label} failed: {detail}")

        def export() -> None:
            pre_state.parent.mkdir(parents=True, exist_ok=True)
            result = bd.run(["export", "-o", str(pre_state), "--all"], cwd, actor=actor)
            if result.returncode != 0:
                raise BackfillError(f"pre-state export failed: {bd.err_detail(result)}")

        result = apply_plan(
            plan,
            load_records=lambda: enumerate_corpus(cwd),
            mutate=mutate,
            export_pre_state=export,
            audit_path=audit,
            pre_state_artifact=str(pre_state),
        )
        if as_json:
            typer.echo(json.dumps(result, sort_keys=True, indent=2))
        else:
            typer.echo(
                f"applied {result['applied_count']} complexity updates; "
                f"validation clean; second dry-run changes=0"
            )
            typer.echo(f"pre-state: {pre_state}")
            typer.echo(f"audit: {audit}")
    except BackfillError as exc:
        typer.echo(f"✗ {exc}", err=True)
        raise typer.Exit(1) from None


__all__ = [
    "MIGRATION",
    "ROUTABLE_STATUSES",
    "SCHEMA_VERSION",
    "SYSTEM_ARTIFACT_LABELS",
    "BackfillError",
    "ClassifierRules",
    "apply_plan",
    "build_plan",
    "command",
    "complexity_pass",
    "corpus_hash",
    "enumerate_corpus",
    "read_plan",
    "rules",
    "verify_plan",
    "write_plan",
]
