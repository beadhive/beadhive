"""``bh backfill``: the BeadLog backfill verbs (``docs/BACKFILL.md``).

Root wiring only (``docs/design/backfill-platform-adr.md`` section 2): the format lives in
``beadlog``, the neutral bd reader in ``beadlog_sources``, the plan/apply envelope and the passes
in ``beadhive_backfill``; this module resolves the target hive, composes the bd StoreLoader
(:mod:`beadhive.backfill_bd`) and renders results.

- ``complexity`` — pass #1, the complexity-label migration (also the flat, published
  ``bh backfill-complexity``; both are the same command);
- ``timeline`` — pass #2, the lifecycle timeline: derives claimed/committed/submitted/reviewed/
  merged/reopened facts from the history readers and writes only ``metadata.lifecycle.*``;
- ``export`` — the hive's current state as a ``.bdlg`` archive, read by the neutral bd strategy;
  with ``--history`` also every history event of the bd events table (the archival archive);
- ``inspect`` — an archive's manifest, counts and (for a plan) its classification and verdicts;
- ``verify`` — an archive's integrity and event ids, and a plan's hash;
- ``apply`` — a ``.bdlg`` plan through the envelope: one written by ``bh backfill`` or by the
  standalone ``beadlog reconcile``;
- ``coverage`` — the three link-coverage ratios from a cached state (``--refresh`` recomputes it;
  an enabled ``backfill.coverage`` also refreshes it after every applied pass).
"""

from __future__ import annotations

import importlib.metadata
import json
import re
import uuid
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import typer

from beadhive_backfill import (
    BackfillEngine,
    BackfillError,
    JsonFileAuditSink,
    PathReaders,
    Plan,
    TimelinePass,
    system_clock,
)
from beadhive_backfill.coverage import (
    RATIO_BEADS,
    RATIO_COMMITS,
    RATIO_DOCS,
    CacheState,
    CoverageError,
    CoverageReport,
    check_cache,
    write_coverage_cache,
)
from beadhive_backfill.timeline import PASS_NAME as TIMELINE_PASS
from beadlog import ArchiveError, ArchiveReader, Generator, Group, ManifestError, write_archive
from beadlog_sources import (
    HISTORY_ANNOTATION,
    BdReadError,
    GapfillReader,
    HistoryRead,
    JsonCursorFile,
    gap_events,
    gapfill_annotation,
    history_annotation,
    history_events,
    lacking_created,
    read_history,
    read_store_start,
)

from . import backfill_coverage, complexity_backfill, config, registry
from .backfill_bd import BdLoadError, BdStoreLoader, bd_store_loader, default_pre_state_path
from .modules.config.contracts import BackfillGapfillConfig

app = typer.Typer(
    no_args_is_help=True,
    help="BeadLog backfill: passes, current-state export, inspect, verify and plan apply.",
)
app.command(
    "complexity",
    help="preview/apply a hash-gated full-corpus complexity-label migration",
)(complexity_backfill.command)

# The namespace a hive's derived bead-group id is minted in (uuid5 over the hive triplet).
GROUP_NAMESPACE = "https://beadhive.ai/hive/"
# The reconcile and engine plan annotations (``beadlog_sources`` / ``beadhive_backfill``).
RECONCILE_ANNOTATION = "bdlg.reconcile"
ENGINE_ANNOTATION = "beadhive.backfill"
_DIR_UNSAFE = re.compile(r"[^a-z0-9._-]+")
_OUT_OPTION = typer.Option(
    None, "--out", help="archive to write (default: <hive-prefix>.bdlg in cwd)"
)
_ARCHIVE_ARGUMENT = typer.Argument(..., help="a .bdlg archive or plan")
_PLAN_ARGUMENT = typer.Argument(..., help="a .bdlg plan (bh backfill or beadlog reconcile)")
_PRE_STATE_OPTION = typer.Option(
    None, "--pre-state", help="JSONL recovery export (default: beside the plan)"
)
_AUDIT_OPTION = typer.Option(None, "--audit", help="machine audit JSON (default: beside the plan)")
_ERRORS = (
    BackfillError,
    CoverageError,
    BdLoadError,
    BdReadError,
    ArchiveError,
    ManifestError,
    OSError,
    ValueError,
)
GAPFILL_STATE_SUFFIX = ".gapfill.json"


def _version() -> str:
    try:
        return importlib.metadata.version("beadhive")
    except importlib.metadata.PackageNotFoundError:
        return "0+unknown"


GENERATOR = Generator("beadhive", _version())


def hive_group(entry: Mapping[str, Any] | None, cwd: Path) -> Group:
    """The bead group a hive's export declares: a uuid5 over its ``provider/org/repo`` (the
    same id on every export, so re-exports union-merge), its directory the hive prefix."""
    if entry is not None and all(entry.get(key) for key in ("provider", "org", "repo")):
        key = f"{entry['provider']}/{entry['org']}/{entry['repo']}"
        name = str(entry.get("prefix") or entry["repo"])
    else:
        key = f"path:{cwd.resolve()}"
        name = cwd.name
    directory = _DIR_UNSAFE.sub("-", name.lower()).strip("-._") or "hive"
    group_id = f"urn:uuid:{uuid.uuid5(uuid.NAMESPACE_URL, GROUP_NAMESPACE + key)}"
    return Group(group_id, directory, description=f"bead hive {key}")


def _target(hive: str) -> tuple[Path, Group]:
    cfg = config.load()
    entry = registry.resolve_hive(cfg, hive) if hive else registry.current_hive(cfg)
    cwd = Path(registry.hive_dir_for(cfg, hive))
    return cwd, hive_group(entry, cwd)


def store_loader(cwd: Path, *, group: str, pre_state_path: Path | None = None) -> BdStoreLoader:
    """The bd StoreLoader for the hive at ``cwd`` (the seam tests replace with a fake bd)."""
    return bd_store_loader(cwd, group=group, pre_state_path=pre_state_path)


def history_inputs(cwd: Path) -> tuple[HistoryRead, str | None]:
    """The bulk reads behind ``export --history``: the bd events table (one ``SELECT``) and the
    store's first commit date (one more). The seam tests replace."""
    return read_history(cwd), read_store_start(cwd)


def timeline_pass(cwd: Path, hive: str) -> TimelinePass:
    """The lifecycle timeline pass reading the hive at ``cwd`` (the seam tests replace): its
    first-parent history on the hive's integration branch and its host-local run journals."""
    cfg = config.load()
    entry = registry.resolve_hive(cfg, hive) if hive else registry.current_hive(cfg)
    ref = str(config.integration_branch(cfg, entry)) if entry is not None else "main"
    journals = None
    if entry is not None:
        from . import run_journal  # lazy: the journal store is only needed for this pass

        journals = run_journal.journal_root_for_hive(registry.hive_key(entry))
    return TimelinePass(PathReaders(cwd, ref=ref, journals=journals))


def _fail(exc: BaseException) -> typer.Exit:
    typer.echo(f"✗ {exc}", err=True)
    return typer.Exit(1)


def _emit(value: Mapping[str, Any]) -> None:
    typer.echo(json.dumps(value, sort_keys=True, indent=2))


def _coverage_target(cfg: Mapping[str, Any], hive: str, cwd: Path) -> tuple[Path, str]:
    """Where the hive's coverage cache lives and which branch's commits are counted."""
    entry = registry.resolve_hive(cfg, hive) if hive else registry.current_hive(cfg)
    ref = str(config.integration_branch(cfg, entry)) if entry is not None else "main"
    return backfill_coverage.state_path(entry, cwd, config.home()), ref


def refresh_coverage_after_apply(hive: str, quiet: bool = False) -> None:
    """Rewrite the hive's cached coverage state after an applied pass, when
    ``backfill.coverage.enabled``.

    The seam every applied pass ends with (``complexity``, ``timeline``, the neutral ``apply``;
    later passes call it too). The apply has already succeeded and is audited, so a refresh
    that fails only warns on stderr. ``quiet`` keeps stdout pure JSON."""
    try:
        cfg = config.load()
        if not backfill_coverage.refresh_enabled(cfg):
            return
        cwd, group = _target(hive)
        path, ref = _coverage_target(cfg, hive, cwd)
        backfill_coverage.refresh_cache(
            cwd,
            reader=store_loader(cwd, group=group.id),
            ref=ref,
            settings=backfill_coverage.coverage_settings(cfg),
            group=group.id,
            path=path,
        )
    except Exception as exc:  # noqa: BLE001 - advisory: never fail an apply that already landed
        typer.echo(f"! coverage cache not refreshed: {exc}", err=True)
        return
    if not quiet:
        typer.echo(f"coverage: cache refreshed ({path})")


# The complexity pass is its own module (and the flat published ``backfill-complexity``); it
# calls the registered hooks after an applied run.
if refresh_coverage_after_apply not in complexity_backfill.APPLIED_HOOKS:
    complexity_backfill.APPLIED_HOOKS.append(refresh_coverage_after_apply)


def gapfill_settings(cfg: Mapping[str, Any] | None = None) -> BackfillGapfillConfig:
    """The ``backfill.timeline.gapfill`` settings (disabled unless the config enables them)."""
    cfg = config.load() if cfg is None else cfg
    section = (cfg.get("backfill") or {}).get("timeline") or {}
    return BackfillGapfillConfig.model_validate(section.get("gapfill") or {})


def gapfill_reader(cwd: Path, settings: BackfillGapfillConfig) -> GapfillReader:
    """The bounded ``dolt_history`` reader for the hive at ``cwd`` (the seam tests replace)."""
    return GapfillReader(cwd, batch_size=settings.batch_size, timeout=settings.timeout_seconds)


def gapfill_state_path(out: Path) -> Path:
    """Where an interrupted gap fill keeps its cursor: beside the archive being written."""
    return out.with_name(out.name + GAPFILL_STATE_SUFFIX)


def _history_archive(
    cwd: Path,
    group: Group,
    baseline: tuple[Any, ...],
    *,
    gapfill: BackfillGapfillConfig | None = None,
    state: Path | None = None,
) -> tuple[tuple[Any, ...], dict[str, Any]]:
    """``baseline`` plus every history event of the hive, and the ``bdlg.history`` annotation.

    With ``gapfill`` enabled, beads that have no ``created`` row in the events table also get
    snapshot-level events from ``dolt_history_issues``, resumable through the cursor ``state``."""
    read, store_start = history_inputs(cwd)
    extracted_at = system_clock()
    history = history_events(read, baseline, group=group.id, extracted_at=extracted_at)
    events = (*baseline, *history.events)
    account: dict[str, Any] | None = None
    if gapfill is not None and gapfill.enabled:
        ids = lacking_created(history.events)
        cursor = JsonCursorFile(state) if state is not None else None
        try:
            scanned = gapfill_reader(cwd, gapfill).read(ids, cursor)
        except BdReadError as exc:
            hint = f"; progress is saved in {state}, re-run to resume" if state is not None else ""
            raise BdReadError(f"gap fill stopped: {exc}{hint}") from exc
        gap = gap_events(scanned, events, group=group.id, extracted_at=extracted_at, queried=ids)
        events = (*events, *gap.events)
        account = gapfill_annotation(gap, batch_size=gapfill.batch_size)
    return events, history_annotation(
        events, history, baseline, store_start=store_start, gapfill=account
    )


_PLAN_OPTION = typer.Option(
    None, "--plan", help="write this plan on dry-run; required and read back on --apply"
)


def _render_timeline(plan: Plan) -> None:
    report = plan.report
    facts = ", ".join(f"{key}={value}" for key, value in (report.get("facts") or {}).items())
    typer.echo(
        f"timeline: {plan.changes} beads would change "
        f"({len(plan.applicable_events())} events); in scope {plan.totals.get('in_scope', 0)}"
    )
    typer.echo(f"facts: {facts or 'none'}")
    tiers = ", ".join(f"{k}={v}" for k, v in (report.get("confidence") or {}).items())
    typer.echo(f"confidence: {tiers or 'none'}")
    typer.echo(f"plan hash: {plan.plan_hash}")


@app.command("timeline")
def timeline_cmd(
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
    apply: bool = typer.Option(False, "--apply", help="apply a previously saved --plan"),
    dry_run: bool = typer.Option(False, "--dry-run", help="explicit preview mode (the default)"),
    plan_path: Path | None = _PLAN_OPTION,
    pre_state: Path | None = _PRE_STATE_OPTION,
    audit: Path | None = _AUDIT_OPTION,
    as_json: bool = typer.Option(False, "--json", help="emit the plan summary/audit as JSON"),
) -> None:
    """Preview or apply the lifecycle timeline (writes only metadata.lifecycle.* keys)."""
    if apply and dry_run:
        raise typer.BadParameter("choose either --dry-run or --apply")
    try:
        cwd, group = _target(hive)
        pass_ = timeline_pass(cwd, hive)
        engine = BackfillEngine(groups=[group], passes=[pass_])
        if not apply:
            plan = engine.plan(pass_, store_loader(cwd, group=group.id))
            if plan_path is not None:
                plan.write(plan_path)
            if as_json:
                _emit(
                    {
                        "pass": plan.pass_name,
                        "plan_hash": plan.plan_hash,
                        "changes": plan.changes,
                        "totals": plan.totals,
                        "report": plan.report,
                    }
                )
                return
            _render_timeline(plan)
            if plan_path is not None:
                typer.echo(f"saved plan: {plan_path}")
            return
        if plan_path is None:
            raise BackfillError("--apply requires --plan from a prior dry-run")
        plan = Plan.read(plan_path)
        if plan.pass_name != TIMELINE_PASS:
            raise BackfillError(f"{plan_path} is a {plan.pass_name!r} plan, not a timeline plan")
        pre_state = pre_state or default_pre_state_path(plan_path)
        audit = audit or plan_path.with_name(f"{plan_path.stem}.audit.json")
        loader = store_loader(cwd, group=group.id, pre_state_path=pre_state)
        entry = engine.apply(plan, loader, loader, JsonFileAuditSink(audit), pre_state=loader)
    except _ERRORS as exc:
        raise _fail(exc) from None
    if as_json:
        _emit(entry.to_json())
    else:
        typer.echo(
            f"applied {len(entry.completed)} beads; only metadata.lifecycle.* written; "
            "second dry-run empty"
        )
        typer.echo(f"pre-state: {entry.recovery.pre_state_artifact}")
        typer.echo(f"audit: {audit}")
    refresh_coverage_after_apply(hive, as_json)


@app.command("export")
def export_cmd(
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
    out: Path | None = _OUT_OPTION,
    as_json: bool = typer.Option(False, "--json", help="emit the export summary as JSON"),
    history: bool = typer.Option(
        False,
        "--history",
        help="also export every history event of the bd events table (archival archive)",
    ),
) -> None:
    """Export the hive's current state as a BeadLog archive (the neutral bd strategy)."""
    try:
        cwd, group = _target(hive)
        loader = store_loader(cwd, group=group.id)
        events = loader.export()
        annotations: dict[str, Any] | None = None
        description = f"current state of {loader.target}"
        default = f"{group.dir}.history.bdlg" if history else f"{group.dir}.bdlg"
        out = out if out is not None else Path(default)
        state = None
        if history:
            settings = gapfill_settings()
            state = gapfill_state_path(out) if settings.enabled else None
            events, annotations = _history_archive(
                cwd, group, tuple(events), gapfill=settings, state=state
            )
            description = f"current state and bd event history of {loader.target}"
        manifest = write_archive(
            out,
            events,
            groups=[group],
            generator=GENERATOR,
            description=description,
            annotations=annotations,
        )
        if state is not None:
            state.unlink(missing_ok=True)  # the run is complete: the cursor has done its job
    except _ERRORS as exc:
        raise _fail(exc) from None
    summary: dict[str, Any] = {
        "path": str(out),
        "group": group.to_json(),
        "events": int(manifest.counts.get("events", 0)),
        "beads": int(manifest.counts.get("beads", 0)),
    }
    if annotations is not None:
        summary["history"] = annotations[HISTORY_ANNOTATION]
    if as_json:
        _emit(summary)
        return
    typer.echo(
        f"exported {summary['events']} events across {summary['beads']} beads to {out} "
        f"(group {group.id})"
    )
    if "history" in summary:
        _echo_history(summary["history"])


def _echo_history(history: Mapping[str, Any]) -> None:
    sources = ", ".join(f"{key}={value}" for key, value in (history.get("sources") or {}).items())
    typer.echo(
        f"history: {history.get('history_events', 0)} events from "
        f"{history.get('events_table_rows', 0)} bd events-table rows; "
        f"earliest recorded {history.get('earliest_recorded')}"
    )
    typer.echo(f"sources: {sources or 'none'}")
    typer.echo((history.get("gap") or {}).get("note", ""))
    if gap := history.get("gapfill"):
        typer.echo(
            f"gap fill: {gap.get('beads_queried', 0)} beads queried, {gap.get('events', 0)} "
            f"events (resumed from bead {gap.get('resumed_from', 0)})"
        )


def _verdicts(data: Mapping[str, Any]) -> dict[str, Any]:
    """Recorded verdicts of a plan (reconcile entries and the pass's judgments), summarized."""
    annotations = data.get("annotations") or {}
    reconcile = annotations.get(RECONCILE_ANNOTATION) or {}
    engine = annotations.get(ENGINE_ANNOTATION) or {}
    verdicts = [
        entry["verdict"] for entry in reconcile.get("entries") or () if entry.get("verdict")
    ]
    verdicts += list(engine.get("judgments") or ())
    by_engine = Counter(str(verdict.get("engine") or "unknown") for verdict in verdicts)
    by_outcome = Counter(
        "abstain" if verdict.get("outcome") is None else str(verdict["outcome"])
        for verdict in verdicts
    )
    return {
        "recorded": len(verdicts),
        "by_engine": dict(sorted(by_engine.items())),
        "by_outcome": dict(sorted(by_outcome.items())),
    }


def archive_summary(path: Path) -> dict[str, Any]:
    """An archive's manifest summary; for a plan also its classification and verdicts."""
    with ArchiveReader(path) as reader:
        data = reader.manifest.to_json()
    summary: dict[str, Any] = {
        "path": str(path),
        "format_version": data["format_version"],
        "generator": data["generator"],
        "groups": data["groups"],
        "capabilities": data["capabilities"],
        "counts": data["counts"],
        "description": data.get("description"),
    }
    history = (data.get("annotations") or {}).get(HISTORY_ANNOTATION)
    if isinstance(history, Mapping):
        summary["history"] = history
    plan = data.get("plan")
    if isinstance(plan, Mapping):
        annotations = data.get("annotations") or {}
        reconcile = annotations.get(RECONCILE_ANNOTATION) or {}
        engine = annotations.get(ENGINE_ANNOTATION) or {}
        summary["plan"] = {
            "pass": plan.get("pass"),
            "schema_version": plan.get("schema_version"),
            "corpus_hash": plan.get("corpus_hash"),
            "plan_hash": plan.get("plan_hash"),
            "judge": plan.get("judge"),
            "classes": reconcile.get("counts") or {},
            "review_required": reconcile.get("review_required", 0),
            "totals": engine.get("totals") or {},
            "verdicts": _verdicts(data),
        }
    return summary


@app.command("inspect")
def inspect_cmd(
    path: Path = _ARCHIVE_ARGUMENT,
    as_json: bool = typer.Option(False, "--json", help="emit the summary as JSON"),
) -> None:
    """Show an archive's manifest and counts; for a plan, its classes and verdict summary."""
    try:
        summary = archive_summary(path)
    except _ERRORS as exc:
        raise _fail(exc) from None
    if as_json:
        _emit(summary)
        return
    generator = summary["generator"]
    counts = summary["counts"]
    typer.echo(f"{path}: BeadLog {summary['format_version']} by {generator.get('name')}")
    typer.echo(f"groups: {', '.join(group['id'] for group in summary['groups'])}")
    typer.echo(f"events: {counts.get('events', 0)} across {counts.get('beads', 0)} beads")
    for kind, number in sorted((counts.get("by_kind") or {}).items()):
        typer.echo(f"  {kind}: {number}")
    caps = summary["capabilities"]
    typer.echo(f"capabilities: required={caps.get('required')} optional={caps.get('optional')}")
    if history := summary.get("history"):
        _echo_history(history)
    if plan := summary.get("plan"):
        classes = ", ".join(f"{key}={value}" for key, value in plan["classes"].items()) or "none"
        verdicts = plan["verdicts"]
        typer.echo(f"plan: pass={plan['pass']} plan_hash={plan['plan_hash']}")
        typer.echo(f"classes: {classes}; review required: {plan['review_required']}")
        typer.echo(
            f"verdicts: {verdicts['recorded']} recorded; "
            + (", ".join(f"{k}={v}" for k, v in verdicts["by_outcome"].items()) or "none")
        )


@app.command("verify")
def verify_cmd(
    path: Path = _ARCHIVE_ARGUMENT,
    as_json: bool = typer.Option(False, "--json", help="emit the verification result as JSON"),
) -> None:
    """Verify an archive's members, order, counts and event ids; for a plan, its plan hash."""
    try:
        with ArchiveReader(path) as reader:
            reader.verify(ids=True)
            manifest = reader.manifest.to_json()
        plan_hash = None
        if isinstance(manifest.get("plan"), Mapping):
            plan_hash = Plan.read(path).plan_hash
    except _ERRORS as exc:
        raise _fail(exc) from None
    result = {
        "path": str(path),
        "ok": True,
        "events": int((manifest.get("counts") or {}).get("events", 0)),
        "plan_hash": plan_hash,
    }
    if as_json:
        _emit(result)
        return
    suffix = f"; plan hash {plan_hash} verified" if plan_hash else ""
    typer.echo(f"ok: {path} ({result['events']} events){suffix}")


def _plan_group(plan: Plan) -> Group:
    groups = {group.id: group for group in plan.groups}
    used = {event.bead.group for event in plan.events}
    if len(groups) != 1 or not used <= set(groups):
        raise BackfillError(
            f"apply loads one bead group into a hive; the plan declares {sorted(groups)} "
            f"and its events use {sorted(used)}"
        )
    return next(iter(groups.values()))


@app.command("apply")
def apply_cmd(
    plan_path: Path = _PLAN_ARGUMENT,
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="report the loader's losses for the plan; write nothing"
    ),
    pre_state: Path | None = _PRE_STATE_OPTION,
    audit: Path | None = _AUDIT_OPTION,
    as_json: bool = typer.Option(False, "--json", help="emit the loss report/audit as JSON"),
) -> None:
    """Apply a BeadLog plan to the hive through the backfill safety envelope."""
    try:
        plan = Plan.read(plan_path)
        group = _plan_group(plan)
        cwd, _hive_group = _target(hive)
        pre_state = pre_state or default_pre_state_path(plan_path)
        audit = audit or plan_path.with_name(f"{plan_path.stem}.audit.json")
        loader = store_loader(cwd, group=group.id, pre_state_path=pre_state)
        if dry_run:
            report = loader.load(plan.applicable_events(), dry_run=True)
            summary = {
                "plan_hash": plan.plan_hash,
                "pass": plan.pass_name,
                "changes": plan.changes,
                "applicable_events": len(plan.applicable_events()),
                "loss_report": report,
            }
            if as_json:
                _emit(summary)
            else:
                by_code = (report.get("summary") or {}).get("by_code") or {}
                losses = ", ".join(f"{k}={v}" for k, v in sorted(by_code.items())) or "none"
                typer.echo(
                    f"plan {plan.plan_hash} ({plan.pass_name}): {plan.changes} beads, "
                    f"{summary['applicable_events']} events; losses: {losses}"
                )
            return
        passes: list[Any] = [complexity_backfill.complexity_pass()]
        if plan.pass_name == TIMELINE_PASS:
            passes.append(timeline_pass(cwd, hive))
        engine = BackfillEngine(groups=plan.groups, passes=passes)
        entry = engine.apply(plan, loader, loader, JsonFileAuditSink(audit), pre_state=loader)
    except _ERRORS as exc:
        raise _fail(exc) from None
    if as_json:
        _emit(entry.to_json())
    else:
        typer.echo(
            f"applied {len(entry.completed)} beads; verification clean; second dry-run empty"
        )
        typer.echo(f"pre-state: {entry.recovery.pre_state_artifact}")
        typer.echo(f"audit: {audit}")
    refresh_coverage_after_apply(hive, as_json)


_RATIO_LABELS = (
    (RATIO_BEADS, "closed code beads with recorded commits", "beads"),
    (RATIO_COMMITS, "commits resolving to a bead", "commits"),
    (RATIO_DOCS, "docs linked to a bead", "docs"),
)
_REFRESH_HINT = "run `bh backfill coverage --refresh`"


def _percent(ratio: Any) -> str:
    value = ratio.value
    return "n/a" if value is None else f"{value * 100:.1f}%"


def _example_text(kind: str, item: Mapping[str, str]) -> str:
    if kind == "beads":
        return f"{item.get('id')}  {item.get('title', '')}".rstrip()
    if kind == "commits":
        return f"{item.get('sha', '')[:12]}  {item.get('subject', '')}".rstrip()
    return str(item.get("path", ""))


def _render_coverage(report: CoverageReport, state: CacheState, detail: str) -> None:
    stale = state is CacheState.STALE
    heading = "STALE cache (not current)" if stale else f"cache {state.value}"
    typer.echo(f"coverage: {heading}, computed {report.computed_at}")
    for key, label, _kind in _RATIO_LABELS:
        ratio = report.ratios[key]
        typer.echo(f"  {label}: {ratio.linked}/{ratio.total} ({_percent(ratio)})")
    examples = report.examples.to_json()
    for key, _label, kind in _RATIO_LABELS:
        if items := examples[key]:
            typer.echo(f"unlinked {kind} (top {len(items)}):")
            for item in items:
                typer.echo(f"  {_example_text(kind, item)}")
    if stale:
        typer.echo(f"! stale: {detail}; {_REFRESH_HINT}")


@app.command("coverage")
def coverage_cmd(
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
    refresh: bool = typer.Option(
        False, "--refresh", help="recompute from the hive and rewrite the cached state"
    ),
    as_json: bool = typer.Option(False, "--json", help="emit the coverage state as JSON"),
) -> None:
    """Show link coverage: closed beads with commits, commits with beads, docs with beads.

    Reads the cached state and compares its inputs hash with the hive's current inputs: a cache
    computed from different inputs is reported stale and its figures are marked as such. With
    --refresh the ratios are recomputed (one bd export, one git log, one doc walk) and the cache
    is rewritten."""
    try:
        cwd, group = _target(hive)
        cfg = config.load()
        path, ref = _coverage_target(cfg, hive, cwd)
        settings = backfill_coverage.coverage_settings(cfg)
        current = backfill_coverage.measure_hive(
            cwd,
            reader=store_loader(cwd, group=group.id),
            ref=ref,
            settings=settings,
            group=group.id,
        )
        if refresh:
            write_coverage_cache(path, current)
        check = check_cache(path, current.inputs_hash)
    except _ERRORS as exc:
        raise _fail(exc) from None
    envelope: dict[str, Any] = {
        "status": check.state.value,
        "cache_path": str(path),
        "detail": check.detail,
        "refreshed": refresh,
        "current_inputs_hash": current.inputs_hash,
        "cache": None if check.report is None else check.report.to_json(),
    }
    if as_json:
        _emit(envelope)
        return
    if check.report is None:
        reason = "unusable" if check.state is CacheState.INVALID else "missing"
        typer.echo(f"coverage: cache {reason} ({check.detail}); {_REFRESH_HINT}")
        return
    _render_coverage(check.report, check.state, check.detail)


__all__ = [
    "GENERATOR",
    "GROUP_NAMESPACE",
    "app",
    "archive_summary",
    "gapfill_reader",
    "gapfill_settings",
    "gapfill_state_path",
    "hive_group",
    "history_inputs",
    "refresh_coverage_after_apply",
    "store_loader",
    "timeline_pass",
]
