"""``bh backfill``: the BeadLog backfill verbs (``docs/BACKFILL.md``).

Root wiring only (``docs/design/backfill-platform-adr.md`` section 2): the format lives in
``beadlog``, the neutral bd reader in ``beadlog_sources``, the plan/apply envelope and the passes
in ``beadhive_backfill``; this module resolves the target hive, composes the bd StoreLoader
(:mod:`beadhive.backfill_bd`) and renders results.

- ``complexity`` — pass #1, the complexity-label migration (also the flat, published
  ``bh backfill-complexity``; both are the same command);
- ``timeline`` — pass #2, the lifecycle timeline: derives claimed/committed/submitted/reviewed/
  merged/reopened facts from the history readers and writes only ``metadata.lifecycle.*``;
- ``commits`` / ``docs`` / ``planning`` — the hive passes over the neutral ``git-commits``,
  ``docs`` and ``planning`` strategies: they apply DRIFT (``git.commits`` appends,
  ``external_ref`` stamps, a later close) as well as NEW through the same envelope;
- ``commits --since-cursor`` — an incremental dry run from the pass's recorded cursor
  (``docs/BACKFILL.md`` "Incremental and continuous backfill"); an applied plan moves the cursor,
  and ``backfill.continuous`` runs the incremental ``commits`` and ``timeline`` passes after
  merges (the published ``timeline`` verb itself is unchanged);
- ``export`` — the hive's current state as a ``.bdlg`` archive, read by the neutral bd strategy;
  with ``--history`` also every history event of the bd events table (the archival archive);
- ``inspect`` — an archive's manifest, counts and (for a plan) its classification and verdicts;
- ``verdicts`` — review the recorded judge verdicts by class (a plan, archive or collation JSON);
- ``verify`` — an archive's integrity and event ids, and a plan's hash;
- ``apply`` — a ``.bdlg`` plan through the envelope: one written by ``bh backfill`` or by the
  standalone ``beadlog reconcile``;
- ``apply --store xtdb`` / ``export --store xtdb`` — the same verbs against another BeadLog store,
  composed through the ``beadlog.store`` capability (:mod:`beadhive.bootstrap.beadlog_store`) from
  the opt-in ``beadlog-xtdb`` plugin; without ``--store`` (or with ``--store bd``) they use the
  hive's bd store exactly as before;
- ``coverage`` — the three link-coverage ratios from a cached state (``--refresh`` recomputes it;
  an enabled ``backfill.coverage`` also refreshes it after every applied pass).
"""

from __future__ import annotations

import importlib.metadata
import json
import os
import re
import uuid
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

import typer

from beadhive_backfill import (
    ArchivePreStateExporter,
    BackfillEngine,
    BackfillError,
    JsonFileAuditSink,
    PassCursor,
    PathReaders,
    Plan,
    SourcePass,
    TimelinePass,
    read_cursor,
    source_pass,
    source_pass_for_plan,
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
from beadhive_backfill.source_passes import SOURCE_PASS_NAMES, parse_options
from beadhive_backfill.timeline import PASS_NAME as TIMELINE_PASS
from beadlog import ArchiveError, ArchiveReader, Generator, Group, ManifestError, write_archive
from beadlog_sources import (
    HISTORY_ANNOTATION,
    BdReadError,
    GapfillReader,
    HistoryRead,
    IdentifyConfirmationRequired,
    JsonCursorFile,
    JudgeSettings,
    build_judge,
    gap_events,
    gapfill_annotation,
    history_annotation,
    history_events,
    lacking_created,
    read_history,
    read_store_start,
)

from . import (
    backfill_continuous,
    backfill_coverage,
    backfill_history,
    backfill_views,
    complexity_backfill,
    config,
    registry,
)
from .backfill_bd import BdLoadError, BdStoreLoader, bd_store_loader, default_pre_state_path
from .backfill_gapfill import created_ids, gapfill_created, on_created_enabled
from .modules.beadlog_store.contracts import BUILTIN_STORE, BeadlogStoreError
from .modules.config.contracts import BackfillConfig, BackfillGapfillConfig, BackfillJudgeConfig

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
_STORE_OPTION = typer.Option(
    None,
    "--store",
    help="BeadLog store: bd (default; the hive's bd store) or xtdb (the opt-in beadlog-xtdb "
    "plugin, `bh plugin beadlog-xtdb doctor`)",
)
# The annotation a non-bd export records: which store and target it read, and the read's notes.
STORE_ANNOTATION = "beadhive.store"
_ERRORS = (
    BeadlogStoreError,
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


def hive_prefix(hive: str) -> str | None:
    """The hive's configured bead-id prefix (registry ``prefix``), or ``None`` when unset.
    Never derived or guessed (bh-ear6v)."""
    cfg = config.load()
    entry = registry.resolve_hive(cfg, hive) if hive else registry.current_hive(cfg)
    return str((entry or {}).get("prefix") or "") or None


def store_loader(cwd: Path, *, group: str, pre_state_path: Path | None = None) -> BdStoreLoader:
    """The bd StoreLoader for the hive at ``cwd`` (the seam tests replace with a fake bd)."""
    return bd_store_loader(cwd, group=group, pre_state_path=pre_state_path)


def _store_name(store: str | None) -> str:
    """``--store``: absent means the built-in bd store."""
    return BUILTIN_STORE if store is None or store == BUILTIN_STORE else store


def beadlog_store(name: str) -> Any:
    """The ``beadlog.store`` store answering to ``--store <name>`` (the seam tests replace).

    Composed lazily from this host's ``plugins`` / ``plugin_kernel`` config: the plugin package
    (and psycopg) is imported only when it is installed, enabled and selected; otherwise a
    :class:`~beadhive.modules.beadlog_store.contracts.BeadlogStoreUnavailable` says what to do."""
    from .beadlog_store_credentials import fnox_password_source  # lazy: only for a non-bd store
    from .bootstrap.beadlog_store import BeadlogStores

    cfg = config.load()
    stores = BeadlogStores(
        plugins=cfg.get("plugins"),
        plugin_kernel=cfg.get("plugin_kernel"),
        password_source_for=fnox_password_source,
    )
    return stores.resolve(name).port


def history_inputs(cwd: Path) -> tuple[HistoryRead, str | None]:
    """The bulk reads behind ``export --history``: the bd events table (one ``SELECT``) and the
    store's first commit date (one more). The seam tests replace."""
    return read_history(cwd), read_store_start(cwd)


def timeline_pass(cwd: Path, hive: str, **options: Any) -> TimelinePass:
    """The lifecycle timeline pass reading the hive at ``cwd`` (the seam tests replace): its
    first-parent history on the hive's integration branch and its host-local run journals.
    ``options`` are the incremental settings of :class:`~beadhive_backfill.TimelinePass`."""
    cfg = config.load()
    entry = registry.resolve_hive(cfg, hive) if hive else registry.current_hive(cfg)
    ref = str(config.integration_branch(cfg, entry)) if entry is not None else "main"
    return timeline_pass_for(cwd, entry, ref=ref, **options)


def timeline_pass_for(
    cwd: Path, entry: Mapping[str, Any] | None, *, ref: str, **options: Any
) -> TimelinePass:
    """The timeline pass for an already-resolved hive (``entry``) and branch ``ref`` (the seam
    the post-merge hook uses, which knows both). The host-local run journals are optional."""
    journals = None
    if entry is not None:
        from . import run_journal  # lazy: the journal store is only needed for this pass

        journals = run_journal.journal_root_for_hive(registry.hive_key(entry))
    return TimelinePass(PathReaders(cwd, ref=ref, journals=journals), **options)


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


def _cursor_file(hive: str, cwd: Path) -> Path:
    cfg = config.load()
    entry = registry.resolve_hive(cfg, hive) if hive else registry.current_hive(cfg)
    return backfill_continuous.cursor_path(entry, cwd, config.home())


def _load_cursor(hive: str, cwd: Path, name: str) -> PassCursor | None:
    """The pass's recorded cursor for ``--since-cursor``. A cursor file that cannot be trusted
    warns and reads as no cursor, so the run goes ahead in full."""
    read = read_cursor(_cursor_file(hive, cwd), name)
    if read.warning:
        typer.echo(f"! {read.warning}; running in full", err=True)
    return read.cursor


def _incremental(plan: Plan) -> Mapping[str, Any] | None:
    info = plan.report.get(backfill_continuous.INCREMENTAL_KEY)
    return info if isinstance(info, Mapping) else None


def _render_incremental(plan: Plan) -> None:
    """The incremental run's mode and scope (one line; absent for a run that is not one)."""
    info = _incremental(plan)
    if info is None:
        return
    scope = info.get("scope")
    where = "" if scope is None else f", {len(scope)} bead(s) in scope"
    cold = info.get("cursor_in") is None and info.get("mode") == "full"
    suffix = " (no cursor recorded yet)" if cold else ""
    typer.echo(f"incremental: {info.get('mode')}{where}{suffix}")


def _warn_incremental(plan: Plan) -> None:
    """A cursor that could not bound the run (corrupt, or its commit left the history) warns on
    stderr, whatever the output format."""
    for warning in (_incremental(plan) or {}).get("warnings") or ():
        typer.echo(f"! {warning}", err=True)


def _record_cursor(hive: str, cwd: Path, plan: Plan, audit: Path) -> None:
    """Move the pass's cursor after an applied incremental plan; a failure only warns (the
    apply is done and audited, and a stale cursor costs time, not correctness)."""
    try:
        backfill_continuous.record_cursor(_cursor_file(hive, cwd), plan, audit)
    except Exception as exc:  # noqa: BLE001 - the apply already landed
        typer.echo(f"! cursor not recorded: {exc}", err=True)


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
    except Exception as exc:  # noqa: BLE001 - advisory: never fail an apply that already landed
        typer.echo(f"! coverage cache not refreshed: {exc}", err=True)
        return
    _refresh_coverage(cfg, cwd, group, path, ref, quiet)


def refresh_coverage_at(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any] | None,
    cwd: Path,
    group: Group,
    *,
    ref: str,
    quiet: bool = True,
) -> None:
    """:func:`refresh_coverage_after_apply` for a hive the caller already resolved (the
    post-merge hook, which holds the hive entry and the branch it just merged onto)."""
    if not backfill_coverage.refresh_enabled(cfg):
        return
    path = backfill_coverage.state_path(entry, cwd, config.home())
    _refresh_coverage(cfg, cwd, group, path, ref, quiet)


def _refresh_coverage(
    cfg: Mapping[str, Any], cwd: Path, group: Group, path: Path, ref: str, quiet: bool
) -> None:
    try:
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
_SINCE_CURSOR_OPTION = typer.Option(
    False,
    "--since-cursor",
    help="incremental dry run: read only what moved since the pass's recorded cursor "
    "(a missing or corrupt cursor runs in full)",
)


def _timeline_pass_for_plan(cwd: Path, hive: str, plan: Plan) -> TimelinePass:
    """The pass that applies a timeline ``plan``: an incremental plan re-derives exactly the
    beads it recorded (so its second dry run is as narrow as the plan), any other runs in full."""
    info = _incremental(plan)
    scope = info.get("scope") if info is not None else None
    if isinstance(scope, list):
        return timeline_pass(cwd, hive, scope=[str(item) for item in scope])
    return timeline_pass(cwd, hive)


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


judge_app = typer.Typer(no_args_is_help=True, help="The opt-in haiku judge for reconcile.")
app.add_typer(judge_app, name="judge")


@judge_app.command("setup")
def judge_setup(
    dry_run: bool = typer.Option(False, "--dry-run", help="show each step; change nothing"),
    install: bool = typer.Option(
        False,
        "--install",
        help="install beadlog-sources[haiku] when missing (default: verify only)",
    ),
    enable: bool = typer.Option(
        False, "--enable", help="set backfill.judge.enabled once every step passed"
    ),
    fnox_config: str | None = typer.Option(
        None, "--fnox-config", help="absolute fnox config path holding ANTHROPIC_API_KEY"
    ),
    fnox_profile: str | None = typer.Option(None, "--fnox-profile", help="fnox profile"),
    fnox_key: str | None = typer.Option(
        None, "--fnox-key", help="fnox key name (a reference; the value is never stored)"
    ),
    as_json: bool = typer.Option(False, "--json", help="machine-readable report"),
) -> None:
    """Install or verify the extra, resolve the key from the secret store, probe once.

    Never prints or stores the key. Config changes only with --enable and only after every
    step passed.
    """
    from . import backfill_judge_setup as setup

    cfg = config.load()
    section = BackfillJudgeConfig.model_validate(
        (cfg.get("backfill") or {}).get("judge") or {}
    ).model_dump()
    reference = None
    if any((fnox_config, fnox_profile, fnox_key)):
        reference = {
            "config_path": fnox_config or "",
            "profile": fnox_profile or "",
            "key": fnox_key or "",
        }
        try:
            from .modules.config.contracts import HqFnoxRef

            HqFnoxRef.model_validate(reference)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from None
        if not all(reference.values()):
            raise typer.BadParameter("--fnox-config, --fnox-profile and --fnox-key go together")

    def persist(ref: Mapping[str, str] | None) -> None:
        # Only reached after the extra, key and probe all passed.
        if reference:
            for name, value in reference.items():
                config.set_value(f"backfill.judge.credential.{name}", value)
        config.set_value("backfill.judge.enabled", "true", as_json=True)

    report = setup.run_setup(
        section,
        environ=os.environ,
        dry_run=dry_run,
        install=install,
        enable=enable,
        reference=reference,
        persist=persist,
    )
    if as_json:
        _emit(
            {
                "ok": report.ok,
                "dry_run": report.dry_run,
                "enabled": report.enabled,
                "steps": [vars(step) for step in report.steps],
            }
        )
    else:
        for step in report.steps:
            typer.echo(f"{step.name}: {step.status}" + (f" - {step.detail}" if step.detail else ""))
        cost = section.get("cost_per_call_usd")
        if report.ok and cost is not None:
            typer.echo(f"cost: ~${cost} per call (backfill.judge.cost_per_call_usd)")
    if not report.ok:
        raise typer.Exit(1)


@app.command("timeline")
def timeline_cmd(
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
    apply: bool = typer.Option(False, "--apply", help="apply a previously saved --plan"),
    dry_run: bool = typer.Option(False, "--dry-run", help="explicit preview mode (the default)"),
    plan_path: Path | None = _PLAN_OPTION,
    pre_state: Path | None = _PRE_STATE_OPTION,
    audit: Path | None = _AUDIT_OPTION,
    as_json: bool = typer.Option(False, "--json", help="emit the plan summary/audit as JSON"),
    since_cursor: bool = _SINCE_CURSOR_OPTION,
) -> None:
    """Preview or apply the lifecycle timeline (writes only metadata.lifecycle.* keys)."""
    if apply and dry_run:
        raise typer.BadParameter("choose either --dry-run or --apply")
    if apply and since_cursor:
        raise typer.BadParameter(
            "--since-cursor applies to the dry run; the plan records its range"
        )
    try:
        cwd, group = _target(hive)
        if not apply:
            extra: dict[str, Any] = {}
            if since_cursor:
                extra = {"incremental": True, "cursor": _load_cursor(hive, cwd, TIMELINE_PASS)}
            pass_ = timeline_pass(cwd, hive, **extra)
            engine = BackfillEngine(groups=[group], passes=[pass_])
            plan = engine.plan(pass_, store_loader(cwd, group=group.id))
            _warn_incremental(plan)
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
            _render_incremental(plan)
            if plan_path is not None:
                typer.echo(f"saved plan: {plan_path}")
            return
        if plan_path is None:
            raise BackfillError("--apply requires --plan from a prior dry-run")
        plan = Plan.read(plan_path)
        if plan.pass_name != TIMELINE_PASS:
            raise BackfillError(f"{plan_path} is a {plan.pass_name!r} plan, not a timeline plan")
        pass_ = _timeline_pass_for_plan(cwd, hive, plan)
        engine = BackfillEngine(groups=[group], passes=[pass_])
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
    _record_cursor(hive, cwd, plan, audit)
    refresh_coverage_after_apply(hive, as_json)


# -- the source-strategy passes: commits, docs, planning ---------------------------------------

_SOURCE_PASS_HELP = {
    "commits": "preview/apply git.commits appends from git log (DRIFT and NEW)",
    "docs": "preview/apply external_ref stamps linking decision/design docs to beads",
    "planning": "preview/apply epics, issues, edges and closes from a .planning tree",
}
_OPTION_OPTION = typer.Option(
    None,
    "--option",
    help="strategy option key=value (repeatable): rev, since, docs, planning, labels, id_prefix",
)


_VERDICTS_ARGUMENT = typer.Argument(..., help="a .bdlg plan/archive or a bdlg.collation/1 JSON")
_CLASS_OPTION = typer.Option(
    None, "--class", help="verdict class to show (repeatable): NEW, PRESENT, NOISE, abstain"
)


def judge_settings(cfg: Mapping[str, Any] | None = None) -> JudgeSettings:
    """The ``backfill.judge`` settings (disabled unless the config enables them)."""
    cfg = config.load() if cfg is None else cfg
    section = BackfillJudgeConfig.model_validate((cfg.get("backfill") or {}).get("judge") or {})
    return JudgeSettings(
        **section.model_dump(exclude={"cost_per_call_usd", "credential", "resume"})
    )


def _judge_runner(cfg: Mapping[str, Any] | None = None) -> Any:
    """A judge runner that hands the command ``ANTHROPIC_API_KEY`` from the configured fnox
    reference (``backfill.judge.credential``), or ``None`` to inherit the environment. The key
    is resolved on the first call, passed to the child's environment only, never printed."""
    from . import backfill_judge_setup as setup

    cfg = config.load() if cfg is None else cfg
    section = (cfg.get("backfill") or {}).get("judge") or {}
    reference = setup.fnox_reference(section)
    if reference is None:
        return None
    holder: dict[str, str] = {}

    def runner(argv: Any, stdin: bytes, timeout: float) -> Any:
        import os
        import subprocess

        from beadlog_sources.judges import CommandResult

        if "key" not in holder:
            try:
                holder["key"] = setup.fnox_key(reference)
            except KeyError:
                return CommandResult(1, b"")  # fails closed to the deterministic engine
        done = subprocess.run(  # noqa: S603 - the operator configures the command
            list(argv),
            input=stdin,
            stdout=subprocess.PIPE,
            env={**os.environ, setup.API_KEY_ENV: holder["key"]},
            timeout=timeout,
            check=False,
        )
        return CommandResult(done.returncode, done.stdout)

    return runner


def source_pass_for(
    name: str,
    cwd: Path,
    group: Group,
    options: Mapping[str, str],
    *,
    judging: bool,
    **incremental: Any,
) -> SourcePass:
    """The hive pass ``name`` reading the checkout at ``cwd`` (the seam tests replace).
    ``incremental`` carries ``incremental`` / ``cursor`` / ``since`` for a ``commits`` run."""
    return source_pass(name, cwd, group.id, options=options, judging=judging, **incremental)


def _plan_counts(plan: Plan) -> str:
    return ", ".join(f"{key}={value}" for key, value in plan.totals.get("by_class", {}).items())


def _render_source_plan(name: str, plan: Plan) -> None:
    report = plan.report
    typer.echo(
        f"{name}: {plan.changes} beads would change "
        f"({len(plan.applicable_events())} events); classes: {_plan_counts(plan) or 'none'}"
    )
    judge = "on" if report.get("judging") else "off"
    typer.echo(f"judge: {judge}; judge calls: {plan.totals.get('judge_calls', 0)}")
    _render_incremental(plan)
    if plan.drift_reported():
        typer.echo(f"drift reported, not applied: {len(plan.drift_reported())}")
    for item in report.get("drift") or ():
        typer.echo(f"DRIFT {item.get('bead')}: {item.get('reason')}; not overwritten")
    for doc in report.get("residual") or ():
        typer.echo(f"RESIDUAL {doc}: undecided; needs human confirm")
    typer.echo(f"plan hash: {plan.plan_hash}")


def _run_source(
    name: str,
    hive: str,
    apply: bool,
    dry_run: bool,
    plan_path: Path | None,
    pre_state: Path | None,
    audit: Path | None,
    option: list[str] | None,
    as_json: bool,
    since_cursor: bool,
) -> None:
    if apply and dry_run:
        raise typer.BadParameter("choose either --dry-run or --apply")
    if apply and since_cursor:
        raise typer.BadParameter(
            "--since-cursor applies to the dry run; the plan records its range"
        )
    try:
        cwd, group = _target(hive)
        if not apply:
            settings = judge_settings()
            extra: dict[str, Any] = {}
            if since_cursor:
                extra = {"incremental": True, "cursor": _load_cursor(hive, cwd, name)}
            options = parse_options(option or ())
            prefix = hive_prefix(hive) if name in ("docs", "planning") else None
            if prefix and not options.get("id_prefix"):
                options["id_prefix"] = prefix
            pass_ = source_pass_for(
                name,
                cwd,
                group,
                options,
                judging=settings.enabled,
                **extra,
            )
            engine = BackfillEngine(groups=[group], passes=[pass_])
            judge = build_judge(settings, runner=_judge_runner()) if settings.enabled else None
            plan = engine.plan(pass_, store_loader(cwd, group=group.id), judge)
            _warn_incremental(plan)
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
            _render_source_plan(name, plan)
            if plan_path is not None:
                typer.echo(f"saved plan: {plan_path}")
            return
        if plan_path is None:
            raise BackfillError("--apply requires --plan from a prior dry-run")
        if option:
            raise BackfillError("--option applies to the dry-run; the plan records its options")
        plan = Plan.read(plan_path)
        if plan.pass_name != name:
            raise BackfillError(f"{plan_path} is a {plan.pass_name!r} plan, not a {name} plan")
        pass_ = source_pass_for_plan(name, cwd, group.id, plan.report)
        engine = BackfillEngine(groups=[group], passes=[pass_])
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
            f"applied {len(entry.completed)} beads; verification clean; second dry-run empty"
        )
        typer.echo(f"pre-state: {entry.recovery.pre_state_artifact}")
        typer.echo(f"audit: {audit}")
    _record_cursor(hive, cwd, plan, audit)
    refresh_coverage_after_apply(hive, as_json)


def _source_command(name: str) -> Callable[..., None]:
    if name == "commits":

        def commits(
            hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
            apply: bool = typer.Option(False, "--apply", help="apply a previously saved --plan"),
            dry_run: bool = typer.Option(
                False, "--dry-run", help="explicit preview mode (the default)"
            ),
            plan_path: Path | None = _PLAN_OPTION,
            pre_state: Path | None = _PRE_STATE_OPTION,
            audit: Path | None = _AUDIT_OPTION,
            option: list[str] | None = _OPTION_OPTION,
            as_json: bool = typer.Option(
                False, "--json", help="emit the plan summary/audit as JSON"
            ),
            since_cursor: bool = _SINCE_CURSOR_OPTION,
        ) -> None:
            _run_source(
                name, hive, apply, dry_run, plan_path, pre_state, audit, option, as_json,
                since_cursor,
            )  # fmt: skip

        command: Callable[..., None] = commits
    else:

        def other(
            hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
            apply: bool = typer.Option(False, "--apply", help="apply a previously saved --plan"),
            dry_run: bool = typer.Option(
                False, "--dry-run", help="explicit preview mode (the default)"
            ),
            plan_path: Path | None = _PLAN_OPTION,
            pre_state: Path | None = _PRE_STATE_OPTION,
            audit: Path | None = _AUDIT_OPTION,
            option: list[str] | None = _OPTION_OPTION,
            as_json: bool = typer.Option(
                False, "--json", help="emit the plan summary/audit as JSON"
            ),
        ) -> None:
            _run_source(
                name, hive, apply, dry_run, plan_path, pre_state, audit, option, as_json, False
            )

        command = other
    command.__name__ = f"{name}_cmd"
    command.__doc__ = _SOURCE_PASS_HELP[name].capitalize() + "."
    return command


for _name in SOURCE_PASS_NAMES:
    app.command(_name, help=_SOURCE_PASS_HELP[_name])(_source_command(_name))


# -- history pipeline (bh backfill history | collate | view ...) --------------------------------

_SOURCES_OPTION = typer.Option(
    None,
    "--sources",
    help="readers to run, comma-separated (default: backfill.history.sources; github is opt-in)",
)
_RUN_DIR_OPTION = typer.Option(
    None,
    "--run-dir",
    help="persistent run directory (default: backfill.history.run_dir, else a per-hive state dir)",
)
_REFRESH_OPTION = typer.Option(
    False,
    "--refresh",
    help="read the sources again even when the run directory's evidence is current",
)
_YES_OPTION = typer.Option(
    False,
    "--yes",
    help="confirm: judge more units than max_calls_per_run (first batch now, rest on the next "
    "run), and apply a plan with verdicts that need review",
)
_DECISION_OPTION = typer.Option(
    None,
    "--decision",
    help="only units with this decision (repeatable): NEW, PRESENT, NOISE, residual",
)
_VIEW_PATH = typer.Argument(..., help="a run directory or a bdlg.collation/1 JSON")


def history_directory(hive: str, cfg: Mapping[str, Any], backfill: BackfillConfig) -> Any:
    """The ledger directory of a history run (the seam tests replace): the registry's ledgers
    (``backfill.history.ledgers`` restricts which; the hive's own is always read), each read
    announced on stderr as it starts, since the first foreign-token lookup exports every one."""
    from .backfill_ledgers import WarningLedgerDirectory, bd_export_ids, ledger_directory_for

    prefix = hive_prefix(hive)
    if not prefix:
        raise BackfillError("the hive has no registered prefix (prefixes are never derived)")
    names: dict[str, str] = {}

    def announce(path: Path) -> list[str]:
        typer.echo(f"ledger {names.get(str(path), path)}: reading bead ids ...", err=True)
        return bd_export_ids(path)

    full = ledger_directory_for(prefix, cfg, read_ids=announce)
    allowed = set(backfill.history.ledgers)
    sources = {
        name: path
        for name, path in full.sources.items()
        if not allowed or name in allowed or name == prefix
    }
    names.update({str(path): name for name, path in sources.items()})
    return WarningLedgerDirectory(sources, announce)


class _Snapshot:
    """A StoreReader over a baseline already read (so planning does not export the store twice)."""

    def __init__(self, events: tuple[Any, ...]) -> None:
        self._events = events

    def export(self) -> tuple[Any, ...]:
        return self._events


def _run_dir_for(hive: str, backfill: BackfillConfig, override: Path | None) -> Path:
    if override is not None:
        return override
    if backfill.history.run_dir:
        return Path(backfill.history.run_dir).expanduser()
    cfg = config.load()
    entry = registry.resolve_hive(cfg, hive) if hive else registry.current_hive(cfg)
    key = registry.sanitize(registry.hive_key(entry)) if entry is not None else "default"
    return backfill_history.default_run_dir(config.home(), key)


def _split_sources(sources: str | None) -> list[str]:
    return [item.strip() for item in (sources or "").split(",") if item.strip()]


def _warn_ledgers(directory: Any) -> list[str]:
    warnings = [str(w) for w in getattr(directory, "warnings", ())]
    for text in warnings:
        typer.echo(f"! {text}", err=True)
    return warnings


def _history_pass(
    cfg: Mapping[str, Any],
    hive: str,
    corpus: Any,
    directory: Any,
    group: Group,
    options: backfill_history.CollateOptions,
    run: Any,
    run_dir: Path,
    names: Sequence[str],
) -> backfill_history.HistoryPass:
    """The ``history`` pass of this hive: create mode, prefix and creation labels come from
    :mod:`beadhive.backfill_create`, the prefix from the registry (never derived)."""
    from . import backfill_create

    create = backfill_create.parse_create(options.create)
    prefix = hive_prefix(hive)
    if create == backfill_create.CREATE_PROPOSE and not prefix:
        raise BackfillError("create mode needs the hive's explicit bead id prefix (never derived)")
    if not prefix:
        raise BackfillError("the hive has no registered prefix (prefixes are never derived)")
    project_options = (
        backfill_create.create_mode_options(hive, cfg, create)
        if create == backfill_create.CREATE_PROPOSE
        else None
    )
    return backfill_history.HistoryPass(
        corpus,
        directory,
        local=prefix,
        group=group.id,
        prefix=prefix,
        create=create,
        project_options=project_options,
        options=options,
        run=run,
        run_dir=str(run_dir),
        sources=names,
    )


def history_pass_for_plan(plan: Plan, group: Group, hive: str, *, cfg: Mapping[str, Any]) -> Any:
    """The ``history`` pass that applies ``plan``: rebuilt from the run directory the plan
    recorded (its ``evidence.jsonl`` and ``collation.json``), replaying the recorded verdicts."""
    from beadlog_sources import (
        AttachOptions,
        ClusterOptions,
        IdentifyOptions,
        StatusOptions,
        read_collation,
        read_evidence,
    )

    report = plan.report
    run_dir = Path(str(report.get("run_dir") or ""))
    files = backfill_history.run_paths(run_dir)
    if (
        not report.get("run_dir")
        or not files["evidence"].is_file()
        or not files["collation"].is_file()
    ):
        raise BackfillError(
            f"the history plan's run directory {str(run_dir)!r} no longer holds its evidence and "
            "collation; rerun `bh backfill history` to plan again"
        )
    backfill = BackfillConfig.model_validate(cfg.get("backfill") or {})
    collation = read_collation(files["collation"])
    recorded = collation.options
    options = backfill_history.CollateOptions(
        ClusterOptions.from_json(recorded["cluster"]),
        AttachOptions.from_json(recorded["attach"]),
        IdentifyOptions.from_json(recorded["identify"]),
        StatusOptions.from_json(recorded["status"]),
        str(report.get("create_mode") or "off"),
    )
    return _history_pass(
        cfg,
        hive,
        read_evidence(files["evidence"]),
        history_directory(hive, cfg, backfill),
        group,
        options,
        backfill_history.replay_run(collation),
        run_dir,
        report.get("sources") or (),
    )


def _history_run(
    hive: str,
    sources: str | None,
    until: str,
    run_dir: Path | None,
    refresh: bool,
    yes: bool,
    as_json: bool,
) -> None:
    cfg = config.load()
    backfill = BackfillConfig.model_validate(cfg.get("backfill") or {})
    summary: dict[str, Any] = {"until": until}
    try:
        last = backfill_history.stage_index(until)
        cwd, group = _target(hive)
        prefix = hive_prefix(hive)
        if not prefix:
            raise BackfillError("the hive has no registered prefix (prefixes are never derived)")
        chosen = backfill_history.resolve_sources(_split_sources(sources), backfill, cfg)
        names = [name for name, _ in chosen]
        run = _run_dir_for(hive, backfill, run_dir)
        run.mkdir(parents=True, exist_ok=True)
        files = backfill_history.run_paths(run)
        summary.update({"run_dir": str(run), "sources": names, "stages": {}})
        directory = history_directory(hive, cfg, backfill)
        manifest = backfill_history.read_manifest(run)

        # parse
        namespaces = list(directory.ledgers())
        key = backfill_history.parse_key(
            cwd, [(n, backfill_history.source_options(n, backfill, namespaces)) for n in names]
        )
        reused = (
            not refresh
            and files["evidence"].is_file()
            and (manifest.get("parse") or {}).get("key") == key
        )
        if reused:
            corpus = backfill_history.read_evidence(files["evidence"])
        else:
            corpus = backfill_history.read_sources(cwd, chosen, backfill, namespaces)
            backfill_history.write_evidence(corpus, files["evidence"])
        manifest["parse"] = {"key": key, "artifacts": len(corpus.artifacts), "reused": reused}
        summary["stages"]["parse"] = manifest["parse"]
        backfill_history.write_manifest(run, manifest)
        if last == 0:
            return _history_done(summary, files, as_json)

        # collate
        loader = store_loader(cwd, group=group.id)
        baseline = tuple(loader.export())
        prior, note = (
            backfill_history.load_prior(files["collation"])
            if backfill.judge.resume
            else (None, None)
        )
        if note:
            typer.echo(f"! {note}", err=True)
        settings = judge_settings(cfg)
        identify = backfill_history.identify_run(
            settings,
            prior=prior,
            cost_per_call_usd=backfill.judge.cost_per_call_usd,
            runner=_judge_runner(cfg) if settings.enabled else None,
            confirm=yes,
        )
        options = backfill_history.CollateOptions.from_config(backfill)
        collation = backfill_history.collate_stage(
            corpus, baseline, directory, local=prefix, options=options, run=identify
        )
        backfill_history.write_collation_atomic(collation, files["collation"])
        warnings = _warn_ledgers(directory)
        manifest["collate"] = {
            "units": len(collation.units),
            "verdicts": len(collation.verdicts),
            "residual": len(collation.residual),
            "conflicts": len(collation.conflicts),
            "prior": prior is not None,
            "ledger_warnings": warnings,
        }
        summary["stages"]["collate"] = manifest["collate"]
        backfill_history.write_manifest(run, manifest)
        if last == 1:
            return _history_done(summary, files, as_json)

        # plan
        pass_ = _history_pass(
            cfg,
            hive,
            corpus,
            directory,
            group,
            options,
            backfill_history.replay_run(collation, settings.auto_accept),
            run,
            names,
        )
        engine = BackfillEngine(groups=[group], passes=[pass_])
        plan = engine.plan(pass_, _Snapshot(baseline))
        plan.write(files["plan"])
        review = int(plan.totals.get("review_required", 0))
        manifest["plan"] = {
            "plan_hash": plan.plan_hash,
            "changes": plan.changes,
            "events": len(plan.applicable_events()),
            "by_class": plan.totals.get("by_class", {}),
            "review_required": review,
            "create": options.create,
            "units": plan.report.get("units") or {},
            "creates_skipped": plan.report.get("creates_skipped", 0),
        }
        summary["stages"]["plan"] = manifest["plan"]
        backfill_history.write_manifest(run, manifest)
        if last == 2:
            return _history_done(summary, files, as_json)

        # apply: the existing envelope, behind the review gate
        if review and not yes:
            raise BackfillError(
                f"{review} verdict(s) need review: inspect them with `bh backfill verdicts "
                f"{files['plan']}`, then rerun with --yes to apply"
            )
        apply_loader = store_loader(cwd, group=group.id, pre_state_path=files["pre_state"])
        entry = engine.apply(
            plan, apply_loader, apply_loader, JsonFileAuditSink(files["audit"]),
            pre_state=apply_loader,
        )  # fmt: skip
        manifest["apply"] = {
            "completed": len(entry.completed),
            "audit": str(files["audit"]),
            "pre_state": str(entry.recovery.pre_state_artifact),
        }
        summary["stages"]["apply"] = manifest["apply"]
        backfill_history.write_manifest(run, manifest)
        gap = _gapfill_after_apply(cwd, group, plan, run)
        if gap is not None:
            summary["stages"]["gapfill"] = gap
            manifest["gapfill"] = gap
            backfill_history.write_manifest(run, manifest)
    except IdentifyConfirmationRequired as exc:
        raise _fail(RuntimeError(f"{exc}; rerun with --yes")) from None
    except _ERRORS as exc:
        raise _fail(exc) from None
    _record_cursor(hive, cwd, plan, files["audit"])
    refresh_coverage_after_apply(hive, as_json)
    _history_done(summary, files, as_json)


def _gapfill_after_apply(cwd: Path, group: Group, plan: Plan, run: Path) -> dict[str, Any] | None:
    """The ``backfill.timeline.gapfill.on_created`` pass: gap-fill exactly the beads this apply
    created (``None`` when it is off or nothing was created). The derived events go to
    ``<run>/gapfill.bdlg``; the store is only read."""
    settings = gapfill_settings()
    created = created_ids(e for e in plan.applicable_events() if e.bead.group == group.id)
    if not on_created_enabled(settings) or not created:
        return None
    baseline = tuple(store_loader(cwd, group=group.id).export())
    result = gapfill_created(
        cwd,
        group.id,
        created,
        settings,
        baseline=baseline,
        state=run / "gapfill.json",
        reader=gapfill_reader,
    )
    if result is None:
        return None
    out = run / "gapfill.bdlg"
    if result.events:
        write_archive(
            out,
            result.events,
            groups=[group],
            generator=GENERATOR,
            description="gap-fill history of the beads a backfill apply created",
        )
    (run / "gapfill.json").unlink(missing_ok=True)
    return {
        "beads": len(created),
        "events": len(result.events),
        "archive": str(out) if result.events else None,
    }


def _history_done(summary: dict[str, Any], files: Mapping[str, Path], as_json: bool) -> None:
    summary["files"] = {name: str(path) for name, path in files.items() if path.is_file()}
    if as_json:
        _emit(summary)
        return
    stages = summary["stages"]
    typer.echo(f"run directory: {summary['run_dir']}")
    if parse := stages.get("parse"):
        typer.echo(
            f"parse: {parse['artifacts']} artifacts from {', '.join(summary['sources'])}"
            + (" (evidence reused)" if parse["reused"] else "")
        )
    if collate_ := stages.get("collate"):
        typer.echo(
            f"collate: {collate_['units']} units, {collate_['verdicts']} verdicts, "
            f"{collate_['residual']} residual, {collate_['conflicts']} conflicts"
            + (" (prior collation reused)" if collate_["prior"] else "")
        )
    if plan := stages.get("plan"):
        classes = ", ".join(f"{k}={v}" for k, v in plan["by_class"].items()) or "none"
        units = ", ".join(f"{k}={v}" for k, v in plan["units"].items()) or "none"
        typer.echo(
            f"plan: {plan['changes']} beads would change ({plan['events']} events); "
            f"classes: {classes}; units: {units}; review required: {plan['review_required']}; "
            f"create: {plan['create']}"
        )
        if plan["creates_skipped"]:
            typer.echo(
                f"create is off: {plan['creates_skipped']} units matching no bead were not proposed"
            )
        typer.echo(f"plan hash: {plan['plan_hash']}")
    if applied := stages.get("apply"):
        typer.echo(
            f"applied {applied['completed']} beads; verification clean; second dry-run empty"
        )
        typer.echo(f"pre-state: {applied['pre_state']}")
        typer.echo(f"audit: {applied['audit']}")
    if gap := stages.get("gapfill"):
        typer.echo(f"gapfill: {gap['events']} events for the {gap['beads']} created beads")
    typer.echo(f"stopped after {summary['until']}; outputs in the run directory")


@app.command("history")
def history_cmd(
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
    sources: str | None = _SOURCES_OPTION,
    until: str = typer.Option(
        "plan",
        "--until",
        help="last stage to run: parse, collate, plan (default; writes nothing to the store) "
        "or apply",
    ),
    run_dir: Path | None = _RUN_DIR_OPTION,
    refresh: bool = _REFRESH_OPTION,
    yes: bool = _YES_OPTION,
    as_json: bool = typer.Option(False, "--json", help="emit the run summary as JSON"),
) -> None:
    """Read the hive's history (git, docs, planning, github) and link it to beads, stage by stage.

    Each --until stage stops and leaves its output in the run directory (evidence.jsonl,
    collation.json, plan.bdlg); a rerun resumes from it. apply goes through the same
    plan/verify/apply envelope as the other passes.
    """
    _history_run(hive, sources, until, run_dir, refresh, yes, as_json)


@app.command("collate")
def collate_cmd(
    hive: str = typer.Option("", "--hive", help="target hive (default: cwd's hive)"),
    sources: str | None = _SOURCES_OPTION,
    run_dir: Path | None = _RUN_DIR_OPTION,
    refresh: bool = _REFRESH_OPTION,
    yes: bool = _YES_OPTION,
    as_json: bool = typer.Option(False, "--json", help="emit the run summary as JSON"),
) -> None:
    """Run `bh backfill history` up to the collation (units, verdicts, conflicts), no plan."""
    _history_run(hive, sources, "collate", run_dir, refresh, yes, as_json)


view_app = typer.Typer(
    no_args_is_help=True,
    help="Review a history run: units, conflicts, cross references, judge cost, verdicts.",
)
app.add_typer(view_app, name="view")


def _show_view(build: Callable[[], dict[str, Any]], as_json: bool) -> None:
    try:
        view = build()
    except (*_ERRORS, backfill_views.ViewInputError) as exc:
        raise _fail(exc) from None
    if as_json:
        _emit(view)
        return
    for line in backfill_views.render_text(view):
        typer.echo(line)


def _cost_per_call() -> float | None:
    cfg = config.load()
    return BackfillConfig.model_validate(cfg.get("backfill") or {}).judge.cost_per_call_usd


@view_app.command("unit")
def view_unit_cmd(
    path: Path = _VIEW_PATH,
    decision: list[str] | None = _DECISION_OPTION,
    as_json: bool = typer.Option(False, "--json", help="emit the view as JSON"),
) -> None:
    """Units with their NEW / PRESENT / NOISE / residual decision, bead, status and dates."""
    _show_view(lambda: backfill_views.unit_view(path, decision=decision or ()), as_json)


@view_app.command("conflict")
def view_conflict_cmd(
    path: Path = _VIEW_PATH,
    as_json: bool = typer.Option(False, "--json", help="emit the view as JSON"),
) -> None:
    """Status conflicts between sources, both sides shown (none is auto-resolved)."""
    _show_view(lambda: backfill_views.conflict_view(path), as_json)


@view_app.command("xref")
def view_xref_cmd(
    path: Path = _VIEW_PATH,
    as_json: bool = typer.Option(False, "--json", help="emit the view as JSON"),
) -> None:
    """References to other ledgers' beads and to tracker items found in the evidence."""
    _show_view(lambda: backfill_views.xref_view(path), as_json)


@view_app.command("cost")
def view_cost_cmd(
    path: Path = _VIEW_PATH,
    as_json: bool = typer.Option(False, "--json", help="emit the view as JSON"),
) -> None:
    """Judge calls the collation recorded and their cost (backfill.judge.cost_per_call_usd)."""
    _show_view(lambda: backfill_views.cost_view(path, _cost_per_call()), as_json)


@view_app.command("verdicts")
def view_verdicts_cmd(
    path: Path = _VIEW_PATH,
    as_json: bool = typer.Option(False, "--json", help="emit the view as JSON"),
) -> None:
    """The judge verdicts of the run (the same view as `bh backfill verdicts`, unfiltered)."""
    _show_view(lambda: backfill_views.verdicts_view(path, _cost_per_call()), as_json)


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
    store: str | None = _STORE_OPTION,
) -> None:
    """Export the hive's current state as a BeadLog archive (the neutral bd strategy)."""
    if _store_name(store) != BUILTIN_STORE:
        _export_store(_store_name(store), hive, out, as_json, history)
        return
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


def _export_store(name: str, hive: str, out: Path | None, as_json: bool, history: bool) -> None:
    """``export --store <name>``: the store's current state for the hive's bead group, read
    through the store's as-of reader and written as a ``.bdlg``."""
    try:
        if history:
            raise BackfillError(
                f"--history reads the bd events table; it cannot be combined with --store {name}"
            )
        _cwd, group = _target(hive)
        bound = beadlog_store(name)
        try:
            snapshot = bound.snapshot(group=group.id, extracted_at=system_clock())
        finally:
            bound.close()
        out = out if out is not None else Path(f"{group.dir}.{name}.bdlg")
        annotations = {
            STORE_ANNOTATION: {
                "store": name,
                "target": snapshot.target,
                "read_notes": dict(sorted(snapshot.notes.items())),
            }
        }
        manifest = write_archive(
            out,
            snapshot.events,
            groups=[group],
            generator=GENERATOR,
            description=f"current state of {snapshot.target}",
            annotations=annotations,
        )
    except _ERRORS as exc:
        raise _fail(exc) from None
    summary: dict[str, Any] = {
        "path": str(out),
        "group": group.to_json(),
        "events": int(manifest.counts.get("events", 0)),
        "beads": int(manifest.counts.get("beads", 0)),
        "store": annotations[STORE_ANNOTATION],
    }
    if as_json:
        _emit(summary)
        return
    typer.echo(
        f"exported {summary['events']} events across {summary['beads']} beads from "
        f"{snapshot.target} to {out} (group {group.id})"
    )
    if snapshot.notes:
        notes = ", ".join(f"{code}={count}" for code, count in sorted(snapshot.notes.items()))
        typer.echo(f"read notes: {notes}")


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


@app.command("verdicts")
def verdicts_cmd(
    path: Path = _VERDICTS_ARGUMENT,
    verdict_class: list[str] | None = _CLASS_OPTION,
    engine: str | None = typer.Option(None, "--engine", help="only verdicts of this engine/model"),
    min_confidence: float | None = typer.Option(
        None, "--min-confidence", help="only verdicts at or above this confidence"
    ),
    max_confidence: float | None = typer.Option(
        None, "--max-confidence", help="only verdicts at or below this confidence"
    ),
    as_json: bool = typer.Option(False, "--json", help="emit the view as JSON"),
) -> None:
    """Review recorded judge verdicts by class with confidence, engine/model and fallback.

    Read-only. Reads a plan or extract archive, or the verdicts of a bdlg.collation/1 JSON; shows
    the cost when backfill.judge.cost_per_call_usd is set.
    """
    from . import backfill_verdicts as review

    cost = BackfillJudgeConfig.model_validate(
        (config.load().get("backfill") or {}).get("judge") or {}
    ).cost_per_call_usd
    try:
        view = review.build_view(
            path,
            classes=list(verdict_class or ()),
            engine=engine,
            min_confidence=min_confidence,
            max_confidence=max_confidence,
            cost_per_call_usd=cost,
        )
    except _ERRORS as exc:
        raise _fail(exc) from None
    if as_json:
        _emit(view)
        return
    for line in review.render_text(view):
        typer.echo(line)


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
    store: str | None = _STORE_OPTION,
) -> None:
    """Apply a BeadLog plan to the hive through the backfill safety envelope."""
    if _store_name(store) != BUILTIN_STORE:
        _apply_store(_store_name(store), plan_path, hive, dry_run, pre_state, audit, as_json)
        return
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
            passes.append(_timeline_pass_for_plan(cwd, hive, plan))
        elif plan.pass_name in SOURCE_PASS_NAMES:
            passes.append(source_pass_for_plan(plan.pass_name, cwd, group.id, plan.report))
        elif plan.pass_name == backfill_history.PASS_NAME:
            passes.append(history_pass_for_plan(plan, group, hive, cfg=config.load()))
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
    _record_cursor(hive, cwd, plan, audit)
    refresh_coverage_after_apply(hive, as_json)


def _apply_store(
    name: str,
    plan_path: Path,
    hive: str,
    dry_run: bool,
    pre_state: Path | None,
    audit: Path | None,
    as_json: bool,
) -> None:
    """``apply --store <name>``: the same envelope as the bd path, with the store's loader as the
    reader and the loader, and a ``.bdlg`` of the store's pre-apply history as the recovery
    artifact. The bd-only follow-ups (the incremental cursor and the coverage cache) do not
    apply: they describe the hive's bd store."""
    try:
        plan = Plan.read(plan_path)
        group = _plan_group(plan)
        cwd, _hive_group = _target(hive)
        pre_state = pre_state or plan_path.with_name(f"{plan_path.stem}.pre-state.bdlg")
        audit = audit or plan_path.with_name(f"{plan_path.stem}.audit.json")
        bound = beadlog_store(name)
        try:
            loader = bound.loader()
            if dry_run:
                report = loader.load(plan.applicable_events(), dry_run=True)
            else:
                passes: list[Any] = [complexity_backfill.complexity_pass()]
                if plan.pass_name == TIMELINE_PASS:
                    passes.append(_timeline_pass_for_plan(cwd, hive, plan))
                elif plan.pass_name in SOURCE_PASS_NAMES:
                    passes.append(source_pass_for_plan(plan.pass_name, cwd, group.id, plan.report))
                engine = BackfillEngine(groups=plan.groups, passes=passes)
                exporter = ArchivePreStateExporter(
                    pre_state, groups=plan.groups, generator=GENERATOR
                )
                entry = engine.apply(
                    plan, loader, loader, JsonFileAuditSink(audit), pre_state=exporter
                )
            target = loader.target
        finally:
            bound.close()
    except _ERRORS as exc:
        raise _fail(exc) from None
    if dry_run:
        summary = {
            "store": name,
            "target": target,
            "plan_hash": plan.plan_hash,
            "pass": plan.pass_name,
            "changes": plan.changes,
            "applicable_events": len(plan.applicable_events()),
            "loss_report": report,
        }
        if as_json:
            _emit(summary)
            return
        by_code = (report.get("summary") or {}).get("by_code") or {}
        losses = ", ".join(f"{k}={v}" for k, v in sorted(by_code.items())) or "none"
        typer.echo(
            f"plan {plan.plan_hash} ({plan.pass_name}) into {target}: {plan.changes} beads, "
            f"{summary['applicable_events']} events; losses: {losses}"
        )
        return
    if as_json:
        _emit(entry.to_json())
        return
    typer.echo(
        f"applied {len(entry.completed)} beads to {target}; verification clean; "
        "second dry-run empty"
    )
    typer.echo(f"pre-state: {entry.recovery.pre_state_artifact}")
    typer.echo(f"audit: {audit}")


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
    "beadlog_store",
    "gapfill_reader",
    "gapfill_settings",
    "gapfill_state_path",
    "hive_group",
    "hive_prefix",
    "history_inputs",
    "judge_settings",
    "refresh_coverage_after_apply",
    "refresh_coverage_at",
    "source_pass_for",
    "store_loader",
    "timeline_pass",
    "timeline_pass_for",
]
