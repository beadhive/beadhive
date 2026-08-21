"""Safe full-corpus migration onto the provider-neutral complexity label contract.

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

import hashlib
import json
import tempfile
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from pathlib import Path
from typing import Any

import typer

from . import bd, complexity, config, registry
from .identity import resolve_actor

SCHEMA_VERSION = 1
MIGRATION = "complexity-label-backfill"
ROUTABLE_STATUSES = frozenset({"open", "blocked", "in_progress", "deferred", "closed"})
SYSTEM_ARTIFACT_LABELS = frozenset({"gt:slot"})

_PLAN_OPTION = typer.Option(
    None, "--plan", help="write this plan on dry-run; required and read back on --apply"
)
_PRE_STATE_OPTION = typer.Option(
    None, "--pre-state", help="JSONL recovery export (default: beside the plan)"
)
_AUDIT_OPTION = typer.Option(None, "--audit", help="machine audit JSON (default: beside the plan)")

# Historical Claude aliases expressed a three-step capability judgment.  Opus spans the upper
# two provider-neutral tiers because the old vocabulary could not distinguish them.
_LEGACY_MODEL_TIERS = {
    "haiku": frozenset({"SIMPLE"}),
    "sonnet": frozenset({"MEDIUM"}),
    "opus": frozenset({"COMPLEX", "REASONING"}),
}


class BackfillError(RuntimeError):
    """A safe refusal or failed apply that the CLI renders without a traceback."""


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


def _complexity_labels(row: Mapping[str, Any]) -> list[str]:
    return [label for label in _labels(row) if label.startswith(complexity.COMPLEXITY_LABEL_PREFIX)]


def _model_labels(row: Mapping[str, Any]) -> list[str]:
    return [label for label in _labels(row) if label.startswith(complexity.MODEL_LABEL_PREFIX)]


def _system_artifact_reason(row: Mapping[str, Any]) -> str | None:
    """A conservative semantic exclusion for records owned by factory infrastructure.

    ``gt:slot`` is the durable marker on the singleton merge-coordination record.  Its title and
    issue type intentionally look like ordinary work, so title/id matching would be both fragile
    and liable to exclude a real task.  Add future system artifacts here only when they have an
    equally explicit semantic label.
    """
    labels = set(_labels(row))
    matched = sorted(labels & SYSTEM_ARTIFACT_LABELS)
    return f"system_artifact:{matched[0]}" if matched else None


def _scope_reason(row: Mapping[str, Any]) -> str | None:
    if reason := _system_artifact_reason(row):
        return reason
    issue_type = _issue_type(row)
    if not complexity.is_routable_issue_type(issue_type):
        return f"non_routable_type:{issue_type or '<missing>'}"
    status = _status(row)
    if status not in ROUTABLE_STATUSES:
        return f"non_routable_status:{status or '<missing>'}"
    return None


def _corpus_projection(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Stable inputs whose change invalidates a plan, restricted to migration targets."""
    projected = []
    for row in records:
        if _scope_reason(row):
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


def corpus_hash(records: Iterable[Mapping[str, Any]]) -> str:
    return _digest(_corpus_projection(records))


def _counter(counter: Counter[str]) -> dict[str, int]:
    return dict(sorted(counter.items()))


def _legacy_model_disagreement(model_label: str, tier: str) -> dict[str, Any] | None:
    value = model_label[len(complexity.MODEL_LABEL_PREFIX) :]
    alias = value.rsplit("/", 1)[-1].lower()
    # Canonical names frequently carry a version suffix (claude-opus-4-1); recognize the old
    # capability word without pretending every future provider catalogue is known here.
    matched = next((name for name in _LEGACY_MODEL_TIERS if name in alias), None)
    if matched is None or tier in _LEGACY_MODEL_TIERS[matched]:
        return None
    return {
        "model": model_label,
        "scored_tier": tier,
        "legacy_expected_tiers": sorted(_LEGACY_MODEL_TIERS[matched]),
    }


def build_plan(
    records: Iterable[Mapping[str, Any]],
    classifier: complexity.ComplexityClassifier = complexity.DEFAULT_CLASSIFIER,
) -> dict[str, Any]:
    """Build a deterministic, JSON-serializable migration plan over a complete corpus read."""
    rows = [dict(row) for row in records]
    ids = [str(row.get("id") or "") for row in rows]
    duplicates = sorted(iid for iid, count in Counter(ids).items() if not iid or count != 1)
    if duplicates:
        raise BackfillError(f"corpus contains missing or duplicate ids: {', '.join(duplicates)}")

    excluded = Counter()
    tiers = Counter()
    statuses = Counter()
    issue_types = Counter()
    existing = Counter()
    entries: list[dict[str, Any]] = []
    fallbacks: list[str] = []
    duplicate_or_invalid: list[dict[str, Any]] = []
    preserved_models: list[dict[str, Any]] = []
    disagreements: list[dict[str, Any]] = []

    for row in sorted(rows, key=lambda item: str(item.get("id") or "")):
        reason = _scope_reason(row)
        if reason:
            excluded[reason] += 1
            continue

        iid = str(row["id"])
        issue_type = _issue_type(row)
        status = _status(row)
        current = _complexity_labels(row)
        valid = []
        for label in current:
            try:
                valid.append(complexity.parse_complexity_label(label))
            except ValueError:
                pass

        result = classifier.classify(complexity.stable_bead_text(row), required=True)
        if result.tier is None:  # a backend violated the required-classification contract
            raise BackfillError(f"classifier returned UNKNOWN for required bead {iid}")
        scored_tier = result.tier

        # A sole canonical label is already explicit routing intent and remains authoritative,
        # matching plan compilation's explicit-complexity override.  Malformed/ambiguous history
        # is repaired from the stable scorer instead of guessing which old label should win.
        preserves_existing = len(current) == 1 and len(valid) == 1
        target_tier = valid[0] if preserves_existing else scored_tier
        target = target_tier.label
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

        models = _model_labels(row)
        if models:
            preserved_models.append(
                {
                    "id": iid,
                    "labels": models,
                    "structurally_invalid": [
                        label
                        for label in models
                        if not complexity.valid_model_preference(
                            label[len(complexity.MODEL_LABEL_PREFIX) :]
                        )
                    ],
                }
            )
        for model in models:
            disagreement = _legacy_model_disagreement(model, scored_tier.name)
            if disagreement:
                disagreements.append({"id": iid, **disagreement})

        if result.fallback_used:
            fallbacks.append(iid)
        tiers[target_tier.name] += 1
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
                "scored_tier": scored_tier.name,
                "score": result.score,
                "source": result.source,
                "version": result.version,
                "fallback_used": result.fallback_used,
                "provenance": "existing" if preserves_existing else "scored",
                "changes": needs_change,
            }
        )

    source = entries[0]["source"] if entries else getattr(classifier, "source", "unknown")
    version = entries[0]["version"] if entries else getattr(classifier, "version", "unknown")
    plan: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "migration": MIGRATION,
        "classifier": {"source": source, "version": version},
        "corpus_hash": corpus_hash(rows),
        "totals": {
            "records": len(rows),
            "in_scope": len(entries),
            "excluded": sum(excluded.values()),
            "changes": sum(bool(entry["changes"]) for entry in entries),
        },
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
    plan["plan_hash"] = _digest(plan)
    return plan


def verify_plan(plan: Mapping[str, Any]) -> None:
    if plan.get("schema_version") != SCHEMA_VERSION or plan.get("migration") != MIGRATION:
        raise BackfillError("unsupported complexity-backfill plan schema")
    expected = plan.get("plan_hash")
    payload = dict(plan)
    payload.pop("plan_hash", None)
    if not isinstance(expected, str) or expected != _digest(payload):
        raise BackfillError("plan hash mismatch — refusing an edited or corrupt plan")


def read_plan(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BackfillError(f"could not read plan {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise BackfillError(f"plan {path} is not a JSON object")
    verify_plan(data)
    return data


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", encoding="utf-8", dir=path.parent, prefix=f".{path.name}.", delete=False
    ) as handle:
        json.dump(value, handle, sort_keys=True, indent=2, ensure_ascii=False)
        handle.write("\n")
        temporary = Path(handle.name)
    temporary.replace(path)


def write_plan(path: Path, plan: Mapping[str, Any]) -> None:
    verify_plan(plan)
    _write_json(path, plan)


def enumerate_corpus(cwd: Path | str) -> list[dict[str, Any]]:
    rows = bd.json(["list", "--all", "--include-infra", "--limit", "0"], cwd)
    if not isinstance(rows, list) or not all(isinstance(row, dict) for row in rows):
        raise BackfillError("could not enumerate the complete bead corpus")
    return rows


def _complexity_errors(records: Iterable[Mapping[str, Any]]) -> list[str]:
    errors = []
    for row in records:
        if _scope_reason(row):
            continue
        labels = _complexity_labels(row)
        if len(labels) != 1:
            errors.append(f"{row.get('id')}: expected one complexity label, found {len(labels)}")
            continue
        try:
            complexity.parse_complexity_label(labels[0])
        except ValueError:
            errors.append(f"{row.get('id')}: invalid complexity label {labels[0]}")
    return errors


def _models_by_id(records: Iterable[Mapping[str, Any]]) -> dict[str, list[str]]:
    return {str(row.get("id") or ""): _model_labels(row) for row in records}


def _expected_after(
    records: Iterable[Mapping[str, Any]], plan: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """Apply the plan to an in-memory copy for a whole-corpus post-write drift check."""
    expected = [dict(row) for row in _copy_records(records)]
    by_id = {str(row.get("id") or ""): row for row in expected}
    for entry in plan.get("entries") or []:
        if not entry.get("changes"):
            continue
        row = by_id[str(entry["id"])]
        row["labels"] = [
            label
            for label in _labels(row)
            if not label.startswith(complexity.COMPLEXITY_LABEL_PREFIX)
        ] + [str(entry["target_label"])]
    return expected


def _copy_records(records: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Copy record dictionaries and their only mutated nested field (labels)."""
    return [{**row, "labels": list(_labels(row))} for row in records]


def _restore_entry(
    entry: Mapping[str, Any],
    mutate: Callable[[str, str, str], None],
    current_complexity: Iterable[str] | None = None,
) -> list[str]:
    failures = []
    iid = str(entry["id"])
    if current_complexity is None:
        current_complexity = set(entry.get("before_complexity") or []) | {
            str(entry["target_label"])
        }
    for label in sorted(set(current_complexity)):
        try:
            mutate("remove", iid, label)
        except BaseException as exc:  # rollback must continue across independent labels
            failures.append(f"remove {iid} {label}: {type(exc).__name__}: {exc}")
    for label in entry.get("before_complexity") or []:
        try:
            mutate("add", iid, str(label))
        except BaseException as exc:
            failures.append(f"add {iid} {label}: {type(exc).__name__}: {exc}")
    return failures


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
    """Apply one verified plan through injected sanctioned reads/writes.

    Injection keeps the transaction policy testable without a live hive.  Production passes
    ``bd list``, one-label-at-a-time ``bd label add/remove``, and ``bd export --all`` adapters.
    """
    verify_plan(plan)
    before = load_records()
    actual_hash = corpus_hash(before)
    if actual_hash != plan["corpus_hash"]:
        raise BackfillError(
            "corpus changed after planning "
            f"(planned {plan['corpus_hash']}, current {actual_hash}) — rerun dry-run"
        )

    planned_models = _models_by_id(row for row in before if _scope_reason(row) is None)
    expected_after_hash = corpus_hash(_expected_after(before, plan))
    planned_updates = [
        str(entry["id"]) for entry in plan.get("entries") or [] if entry.get("changes")
    ]
    audit: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "migration": MIGRATION,
        "plan_hash": plan["plan_hash"],
        "corpus_hash_before": actual_hash,
        "corpus_hash_expected_after": expected_after_hash,
        "pre_state_artifact": pre_state_artifact,
        "state": "applying",
        "planned_updates": planned_updates,
        "attempted": [],
        "completed": [],
        "rollback_failures": [],
        "progress": {
            "planned_count": len(planned_updates),
            "attempted_count": 0,
            "completed_count": 0,
            "current_bead": None,
            "next_index": 0,
        },
        "recovery": {
            "pre_state_artifact": pre_state_artifact,
            "uncertain_bead": None,
            "instruction": "restore the exported pre-state before retrying an incomplete apply",
        },
    }
    export_pre_state()  # the recovery artifact exists before the first bead mutation
    # This is the durable crash marker.  It must exist after export and before the first label
    # write so a SIGKILL/power loss can never leave a partial migration with no applying record.
    try:
        _write_json(audit_path, audit)
    except OSError as exc:
        raise BackfillError(f"could not persist applying audit {audit_path}: {exc}") from exc
    touched: list[Mapping[str, Any]] = []
    try:
        update_index = 0
        for entry in plan.get("entries") or []:
            if not entry.get("changes"):
                continue
            iid = str(entry["id"])
            audit["attempted"].append(iid)
            audit["progress"].update(
                {
                    "attempted_count": len(audit["attempted"]),
                    "current_bead": iid,
                    "next_index": update_index,
                }
            )
            audit["recovery"]["uncertain_bead"] = iid
            _write_json(audit_path, audit)
            touched.append(entry)
            for label in entry.get("before_complexity") or []:
                mutate("remove", iid, str(label))
            mutate("add", iid, str(entry["target_label"]))
            audit["completed"].append(iid)
            update_index += 1
            audit["progress"].update(
                {
                    "completed_count": len(audit["completed"]),
                    "current_bead": None,
                    "next_index": update_index,
                }
            )
            audit["recovery"]["uncertain_bead"] = None
            _write_json(audit_path, audit)

        after = load_records()
        errors = _complexity_errors(after)
        after_hash = corpus_hash(after)
        after_models = _models_by_id(after)
        model_drift = {
            iid: {"before": labels, "after": after_models.get(iid, [])}
            for iid, labels in planned_models.items()
            if after_models.get(iid, []) != labels
        }
        second = build_plan(after, classifier)
        verification_failed = (
            errors
            or model_drift
            or after_hash != expected_after_hash
            or second["totals"]["changes"]
        )
        if verification_failed:
            details = []
            if errors:
                details.append(f"complexity errors={len(errors)}")
            if model_drift:
                details.append(f"model-label drift={len(model_drift)}")
            if after_hash != expected_after_hash:
                details.append("unexpected corpus drift during apply")
            if second["totals"]["changes"]:
                details.append(f"second-dry-run changes={second['totals']['changes']}")
            raise BackfillError("post-apply verification failed: " + ", ".join(details))

        audit.update(
            {
                "state": "applied",
                "corpus_hash_after": after_hash,
                "post_apply_complexity_errors": [],
                "model_labels_preserved": True,
                "second_dry_run_changes": 0,
                "applied_count": len(audit["completed"]),
            }
        )
        _write_json(audit_path, audit)
        return audit
    except BaseException as exc:
        try:
            rollback_rows = {str(row.get("id") or ""): row for row in load_records()}
        except BaseException:  # a failed read cannot be allowed to suppress best-effort rollback
            rollback_rows = {}
        for entry in reversed(touched):
            row = rollback_rows.get(str(entry["id"]))
            current = _complexity_labels(row) if row is not None else None
            audit["rollback_failures"].extend(_restore_entry(entry, mutate, current))
        audit.update(
            {
                "state": "rolled_back" if not audit["rollback_failures"] else "rollback_failed",
                "error": f"{type(exc).__name__}: {exc}",
            }
        )
        audit["progress"]["current_bead"] = None
        audit["recovery"]["uncertain_bead"] = None
        audit["recovery"]["rollback_complete"] = not audit["rollback_failures"]
        _write_json(audit_path, audit)
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        suffix = "" if not audit["rollback_failures"] else "; rollback was incomplete"
        raise BackfillError(f"apply failed and was audited{suffix}: {exc}") from exc


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
