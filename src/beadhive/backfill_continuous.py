"""Continuous backfill: incremental passes after merges and syncs (``docs/BACKFILL.md``).

Root wiring only (``docs/design/backfill-platform-adr.md`` section 2). The cursor model, the
incremental passes and the plan/apply envelope live in :mod:`beadhive_backfill`; this module
supplies what is BeadHive-specific: the ``backfill.continuous`` settings, where a hive's cursor
file lives, and the **triggers**, run after ``bh work merge`` / ``finish`` lands on the
integration branch (:func:`on_work_merged`) and after ``bh hive sync`` pulls a hive
(:func:`after_sync`).

Everything is off until ``backfill.continuous.enabled``. A trigger **never fails** what started
it: the merge has landed and the sync has pulled, so any problem (a failing loader, a corrupt
cursor, a pass error) is one warning on stderr and the caller carries on. A run is small and
bounded:

- it runs after the merge slot is released, with no network and no prompt;
- it runs the configured passes, each through the same plan/apply envelope as the CLI, on the
  beads that moved: the merged beads (timeline) and the commits since the cursor or, with no
  cursor, since the merge's base (commits). A hook never starts a full run - with nothing to
  bound it, it says so and does nothing - and it writes its recovery export only for the beads
  it changes (never a corpus archive), its audit and recovery files replacing the previous
  run's;
- the bd store is read through a per-run read cache, so the envelope's repeated reads of the
  rows cost one export until a write invalidates it;
- when anything was applied the coverage cache is refreshed through the existing seam.

Cursors move only when the run started from a usable cursor (or was the first ``--since-cursor``
run); a hook with no cursor never creates one, so it cannot hide commits an earlier run never
read.

This module imports neither :mod:`beadhive.config` nor :mod:`beadhive.backfill_cli` (the CLI
imports this module): callers pass ``cfg`` and ``home`` as arguments, and the composition root
passes the CLI module as ``seams`` (``hive_group``, ``store_loader``, ``timeline_pass_for``,
``source_pass_for`` and ``refresh_coverage_at``), which tests replace.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError

from beadhive_backfill import (
    CURSOR_FILE,
    ArchivePreStateExporter,
    BackfillEngine,
    JsonFileAuditSink,
    PassCursor,
    Plan,
    cursor_to_record,
    read_cursor,
    system_clock,
    write_cursor,
)
from beadhive_backfill.incremental import INCREMENTAL_KEY

from . import backfill_coverage
from .backfill_effective import effective_backfill
from .modules.config.contracts import BackfillContinuousConfig

TRIGGER_POST_MERGE = "post-merge"
TRIGGER_SYNC = "sync"
WORK_DIR = "continuous"
Echo = Callable[[str], None]


@dataclass(frozen=True)
class ContinuousSettings:
    """``backfill.continuous``: whether it runs, which passes, after which triggers."""

    enabled: bool = False
    passes: tuple[str, ...] = ()
    triggers: tuple[str, ...] = ()

    def runs_on(self, trigger: str) -> bool:
        return self.enabled and trigger in self.triggers and bool(self.passes)


def settings(
    cfg: Mapping[str, Any], hive: Mapping[str, Any] | str | None = None
) -> ContinuousSettings:
    """The settings of a loaded config, as the hive's overlay (``backfill.hives.<prefix>``)
    resolves them when ``hive`` is given. Off when absent; a section that does not validate (or
    an overlay that cannot resolve) is off too (a trigger must never fail what started it -
    ``bh config`` reports the error)."""
    try:
        section = effective_backfill(cfg, hive).get("continuous") or {}
    except ValueError:
        return ContinuousSettings()
    try:
        model = BackfillContinuousConfig.model_validate(dict(section))
    except (ValidationError, TypeError, ValueError):
        return ContinuousSettings()
    return ContinuousSettings(model.enabled, tuple(model.passes), tuple(model.triggers))


def cursor_path(entry: Mapping[str, Any] | None, cwd: Path, home: Path) -> Path:
    """The hive's cursor file, beside its coverage cache (``<state>/backfill/<hive>/``)."""
    return backfill_coverage.state_path(entry, cwd, home).with_name(CURSOR_FILE)


def record_cursor(
    path: Path, plan: Plan, audit: Path | None, *, clock: Callable[[], str] = system_clock
) -> PassCursor | None:
    """Move the pass's cursor to where an applied ``plan`` read to, naming the plan and audit it
    came from. ``None`` (nothing written) for a plan that was not an incremental run, or a scoped
    one, which must not move the mark."""
    cursor = cursor_to_record(plan.report, plan.pass_name)
    if cursor is None:
        return None
    cursor = dataclasses.replace(
        cursor,
        recorded_at=clock(),
        plan_hash=plan.plan_hash,
        audit=None if audit is None else str(audit),
    )
    write_cursor(path, cursor)
    return cursor


# -- the read cache --------------------------------------------------------------------------

_WRITES = (
    "import_records",
    "apply_changes",
    "unset_metadata",
    "close",
    "add_dependency",
    "remove_dependency",
    "add_comment",
    "delete",
)


class CachedReadPort:
    """A bd port that serves ``export_rows`` from one read until the first write.

    The envelope reads the store before planning, before applying, once per written bead and
    after, and the loader reads it on every load; between writes those reads are identical, so a
    run pays for one export instead of one per call. Every write drops the cache, so a read after
    a write always reflects it."""

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self._rows: list[dict[str, Any]] | None = None

    def export_rows(self) -> list[dict[str, Any]]:
        if self._rows is None:
            self._rows = list(self._inner.export_rows())
        return list(self._rows)

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._inner, name)
        if name not in _WRITES:
            return attribute

        def write(*args: Any, **kwargs: Any) -> Any:
            self._rows = None
            return attribute(*args, **kwargs)

        return write


def _cached(loader: Any) -> Any:
    """``loader`` with its bd port wrapped in a :class:`CachedReadPort` (a loader without one,
    a test fake, is returned as it is)."""
    port = getattr(loader, "port", None)
    if port is not None and not isinstance(port, CachedReadPort):
        loader.port = CachedReadPort(port)
    return loader


# -- a run -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class PassOutcome:
    """What one pass did in a run."""

    name: str
    applied: int = 0
    mode: str = "none"
    note: str = ""


def _run_pass(
    name: str,
    *,
    seams: Any,
    entry: Mapping[str, Any] | None,
    cwd: Path,
    ref: str,
    home: Path,
    beads: Sequence[str],
    since: str | None,
    echo: Echo,
) -> PassOutcome:
    group = seams.hive_group(entry, cwd)
    cursors = cursor_path(entry, cwd, home)
    work = cursors.parent / WORK_DIR
    read = read_cursor(cursors, name)
    if read.warning:
        echo(f"! backfill {name}: {read.warning}; not using it")
    if name == "timeline":
        pass_: Any = seams.timeline_pass_for(
            cwd,
            entry,
            ref=ref,
            incremental=True,
            cursor=read.cursor,
            extra_scope=tuple(beads),
            full_ok=False,
        )
    else:
        pass_ = seams.source_pass_for(
            name,
            cwd,
            group,
            {},
            judging=False,
            incremental=True,
            cursor=read.cursor,
            since=since,
            full_ok=False,
        )
    engine = BackfillEngine(groups=[group], passes=[pass_])
    plan = engine.plan(pass_, _cached(seams.store_loader(cwd, group=group.id)))
    info = plan.report.get(INCREMENTAL_KEY) or {}
    for warning in info.get("warnings") or ():
        echo(f"! backfill {name}: {warning}")
    mode = str(info.get("mode") or "none")
    events = plan.applicable_events()
    if not events:
        record_cursor(cursors, plan, None)
        return PassOutcome(name, 0, mode, "up to date")
    changed = {event.bead.id for event in events}
    audit = work / f"{name}.audit.json"
    pre_state = work / f"{name}.pre-state.bdlg"
    loader = _cached(seams.store_loader(cwd, group=group.id, pre_state_path=pre_state))
    done = engine.apply(
        plan,
        loader,
        loader,
        JsonFileAuditSink(audit),
        pre_state=ArchivePreStateExporter(pre_state, groups=[group], only=changed),
    )
    record_cursor(cursors, plan, audit)
    return PassOutcome(name, len(done.completed), mode)


def run(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any] | None,
    cwd: Path,
    *,
    seams: Any,
    ref: str,
    home: Path,
    passes: Iterable[str],
    beads: Sequence[str] = (),
    since: str | None = None,
    echo: Echo | None = None,
) -> list[PassOutcome]:
    """Run ``passes`` incrementally on the hive at ``cwd`` and refresh the coverage cache when
    anything was applied. A pass that fails is reported through ``echo`` and skipped; the others
    still run. Never raises for a pass."""
    say: Echo = echo or (lambda line: typer.echo(line, err=True))
    outcomes: list[PassOutcome] = []
    for name in passes:
        try:
            outcomes.append(
                _run_pass(
                    name,
                    seams=seams,
                    entry=entry,
                    cwd=cwd,
                    ref=ref,
                    home=home,
                    beads=beads,
                    since=since,
                    echo=say,
                )
            )
        except Exception as exc:  # noqa: BLE001 - a trigger never fails what started it
            say(f"! backfill {name} not updated: {exc}")
            outcomes.append(PassOutcome(name, 0, "failed", str(exc)))
    if any(outcome.applied for outcome in outcomes):
        group = seams.hive_group(entry, cwd)
        seams.refresh_coverage_at(cfg, entry, cwd, group, ref=ref, quiet=True)
    return outcomes


def _summary(trigger: str, outcomes: Sequence[PassOutcome]) -> str:
    parts = [
        f"{o.name}: {o.applied} bead(s) updated" if o.applied else f"{o.name}: {o.note or o.mode}"
        for o in outcomes
        if o.mode != "failed"
    ]
    return f"backfill ({trigger}): " + "; ".join(parts) if parts else ""


def on_work_merged(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any] | None,
    main: Path | str,
    base: str,
    beads: Sequence[str],
    *,
    seams: Any,
    home: Path,
    since: str | None = None,
) -> None:
    """The post-merge trigger, called by ``work.merge`` / ``finish`` after the beads close on
    the integration branch ``base``. A no-op - no bd call, no git call - unless
    ``backfill.continuous`` is enabled with the ``post-merge`` trigger. **Never fails the
    merge**: any problem, an unexpected exception included, is a warning on stderr.

    ``since`` is the integration tip before the merge (where the commits of this merge start)."""
    try:
        conf = settings(cfg, entry)
        if not conf.runs_on(TRIGGER_POST_MERGE):
            return
        outcomes = run(
            cfg,
            entry,
            Path(main),
            seams=seams,
            ref=base,
            home=home,
            passes=conf.passes,
            beads=tuple(beads),
            since=since,
        )
        if line := _summary(TRIGGER_POST_MERGE, outcomes):
            typer.echo(line)
    except Exception as exc:  # noqa: BLE001 - the merge has landed; this is bookkeeping
        typer.echo(
            f"! backfill (post-merge) did not run (the merge is unaffected): {exc}", err=True
        )


def after_sync(
    cfg: Mapping[str, Any],
    home: Path,
    seams: Any,
    hives: Iterable[tuple[Mapping[str, Any] | None, Path, str]],
) -> None:
    """The sync trigger, called by ``bh hive sync`` after it pulled ``hives`` (each a
    ``(registry entry, checkout, integration branch)``). A no-op unless ``backfill.continuous``
    is enabled with the ``sync`` trigger. A pulled hive has no merge to scope to, so the passes
    run from their cursors only; a pass with no usable cursor says so and waits for one
    ``--since-cursor`` run. Never fails the sync."""
    try:
        for entry, cwd, ref in hives:
            try:
                conf = settings(cfg, entry)
                if not conf.runs_on(TRIGGER_SYNC):
                    continue
                outcomes = run(
                    cfg,
                    entry,
                    cwd,
                    seams=seams,
                    ref=ref,
                    home=home,
                    passes=conf.passes,
                    beads=(),
                    since=None,
                )
            except Exception as exc:  # noqa: BLE001 - one hive never blocks the next
                typer.echo(f"! backfill (sync) did not run for {cwd}: {exc}", err=True)
                continue
            if line := _summary(TRIGGER_SYNC, outcomes):
                typer.echo(line)
    except Exception as exc:  # noqa: BLE001 - the sync has pulled; this is bookkeeping
        typer.echo(f"! backfill (sync) did not run (the sync is unaffected): {exc}", err=True)


__all__ = [
    "INCREMENTAL_KEY",
    "TRIGGER_POST_MERGE",
    "TRIGGER_SYNC",
    "CachedReadPort",
    "ContinuousSettings",
    "PassOutcome",
    "after_sync",
    "cursor_path",
    "on_work_merged",
    "record_cursor",
    "run",
    "settings",
]
