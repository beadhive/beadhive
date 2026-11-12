"""``bh backfill history``: the BeadLog history pipeline behind a persistent run directory.

The orchestration half of ``docs/design/backfill-history-pipeline-adr.md`` (phases A to C and the
upsert); ``backfill_cli`` is its thin command wiring. The stages, in order, each stop point
(``--until``) leaving its output in the run directory:

=========  ===============================================================================
stage      leaves in the run directory
=========  ===============================================================================
parse      ``evidence.jsonl`` (``bdlg.evidence/1``: every reader's artifacts)
collate    ``collation.json`` (``bdlg.collation/1``: refs, units, verdicts, conflicts ...)
plan       ``plan.bdlg`` (projected events reconciled against the baseline, sealed)
apply      ``audit.json`` and ``pre-state.jsonl`` (the existing apply envelope's artifacts)
=========  ===============================================================================

plus ``run.json``, the run's manifest (what each stage did, what it reused).

**Resume.** A rerun reuses ``evidence.jsonl`` when the sources, their options and the checkout's
``HEAD`` are unchanged (``--refresh`` reads again), and feeds the previous ``collation.json`` to B5
as its ``prior`` (``backfill.judge.resume``), so judged queries are answered from it and a run
stopped by ``max_calls_per_run`` continues where it stopped. Collation and planning are cheap and
deterministic, so they are recomputed against the current baseline.

**One apply path.** The plan is the existing reconcile -> plan -> verify envelope:
:class:`HistoryPass` is a :class:`~beadhive_backfill.HivePass` whose ``propose`` recollates and
projects, so the engine's second dry run proves idempotency exactly as for the other passes, and
:meth:`~beadhive_backfill.BackfillEngine.apply` is the only writer.

**Create mode.** ``create="off"`` (default) is link-only: a unit that matches no bead projects
nothing. ``create="propose"`` is wired in :func:`project_for_create`.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from beadhive_backfill import BackfillError
from beadlog import BeadEvent, BeadRef, Group, canonical_json
from beadlog_sources import (
    BUILTIN_READERS,
    AttachOptions,
    ClusterOptions,
    Collation,
    CollationFormatError,
    EvidenceCorpus,
    IdentifyOptions,
    IdentifyRun,
    JudgeSettings,
    LedgerDirectory,
    ProjectOptions,
    ReaderHeader,
    ReadRequest,
    Registration,
    StatusOptions,
    build_corpus,
    collate,
    discover_readers,
    load_reader,
    project,
    read_collation,
    read_evidence,
    write_collation,
    write_evidence,
)
from beadlog_sources.collate import dumps_collation
from beadlog_sources.identify import prior_verdicts
from beadlog_sources.reconcile import corpus_hash

# ``Backfill`` is the validated ``backfill`` config section (``BackfillConfig``); the root CLI owns
# the schema import so this module adds no importer of the config module.
Backfill = Any

PASS_NAME = "history"
PASS_SCHEMA_VERSION = "1"
RUN_SCHEMA = "beadhive.backfill.history-run/1"
STAGES = ("parse", "collate", "plan", "apply")
EVIDENCE_FILE = "evidence.jsonl"
COLLATION_FILE = "collation.json"
PLAN_FILE = "plan.bdlg"
AUDIT_FILE = "audit.json"
PRE_STATE_FILE = "pre-state.jsonl"
RUN_FILE = "run.json"
STATE_DIRS = ("state", "backfill-history")

# The github reader registers itself in the tracker-github plugin (never in the beadlog-sources
# built-ins). The bh wheel vendors that import package but never installs its distribution, so
# the ``beadlog.readers`` entry point is invisible in an installed bh: this root-side table makes
# the reader discoverable exactly when the plugin is enabled (bh-cyqgr).
PLUGIN_READER_PLUGIN = "tracker-github"
PLUGIN_READERS: Mapping[str, str] = {"github": "beadhive_tracker_github.reader:GitHubReader"}

# What the projector reads from the baseline: which beads exist, and the fields it stamps or
# declines to overwrite. A change to either since planning invalidates the plan.
FACETS = frozenset({"exists", "deleted", "erased", "phase", "fields"})


# -- readers -----------------------------------------------------------------------------------


def plugin_reader_table(cfg: Mapping[str, Any]) -> dict[str, str]:
    """The plugin readers reachable on this host: ``github`` when ``plugins.tracker-github`` is
    enabled (its own flag exactly ``true``, and not switched off by ``plugin_kernel``)."""
    from .bootstrap.tracker import plugin_settings

    kernel = (cfg.get("plugin_kernel") or {}).get("enabled") or {}
    if isinstance(kernel, Mapping) and kernel.get(PLUGIN_READER_PLUGIN) is False:
        return {}
    settings = plugin_settings(PLUGIN_READER_PLUGIN, cfg.get("plugins"))
    return dict(PLUGIN_READERS) if settings.own_enabled and settings.valid else {}


def discover(cfg: Mapping[str, Any]) -> dict[str, Registration]:
    """Every reader this host can run: the ``beadlog.readers`` entry points, the built-ins and the
    enabled plugin readers (the same target from two places is one registration)."""
    return discover_readers(builtin={**BUILTIN_READERS, **plugin_reader_table(cfg)})


def resolve_sources(
    requested: Sequence[str] | None, backfill: Backfill, cfg: Mapping[str, Any]
) -> list[tuple[str, Registration]]:
    """The readers of a run, in order. ``requested`` (``--sources``) falls back to
    ``backfill.history.sources``. A name no reader answers to, or ``github`` while
    ``backfill.sources.github.enabled`` is off, is a :class:`BackfillError`."""
    names = list(dict.fromkeys(requested if requested else backfill.history.sources))
    if not names:
        raise BackfillError("no sources: name some with --sources or backfill.history.sources")
    found = discover(cfg)
    out: list[tuple[str, Registration]] = []
    for name in names:
        if name == "github" and not backfill.sources.github.enabled:
            raise BackfillError(
                "the github reader is off: set backfill.sources.github.enabled = true "
                "(it reads a snapshot the tracker-github plugin wrote)"
            )
        if name not in found:
            hint = (
                "; enable plugins.tracker-github.enabled to make it discoverable"
                if name in PLUGIN_READERS
                else ""
            )
            raise BackfillError(
                f"no reader named {name!r} (found: {', '.join(found) or 'none'}){hint}"
            )
        out.append((name, found[name]))
    return out


def _csv(values: Iterable[str]) -> str:
    return ",".join(values)


def source_options(name: str, backfill: Backfill, namespaces: Sequence[str]) -> dict[str, str]:
    """The options of reader ``name``: the typed ``backfill.sources`` keys, then the free-form
    ``options`` (which win). ``namespaces`` (the registered ledger names, never a guess) anchors
    bead tokens unless the options already say."""
    section = getattr(backfill.sources, name, None)
    options: dict[str, str] = {}
    if name == "docs":
        options["formats"] = _csv(backfill.sources.docs.formats)
        if backfill.sources.docs.layouts:
            options["layouts"] = _csv(backfill.sources.docs.layouts)
    elif name == "github":
        options["snapshot"] = backfill.sources.github.snapshot
    options.update(getattr(section, "options", {}) or {})
    if namespaces and "namespaces" not in options:
        options["namespaces"] = _csv(namespaces)
    return options


def _git_text(cwd: Path, *args: str) -> str:
    try:
        done = subprocess.run(  # noqa: S603, S607 - fixed argv, read-only
            ["git", "-C", str(cwd), *args], capture_output=True, text=True, check=False
        )
    except OSError:
        return ""
    return done.stdout if done.returncode == 0 else ""


def parse_key(cwd: Path, plan: Sequence[tuple[str, Mapping[str, str]]]) -> str:
    """What the evidence depends on: the readers, their options, the checkout's ``HEAD`` and its
    uncommitted changes (docs are read from the working tree)."""
    text = canonical_json(
        {
            "head": _git_text(cwd, "rev-parse", "HEAD").strip(),
            "dirty": hashlib.sha256(_git_text(cwd, "status", "--porcelain").encode()).hexdigest(),
            "sources": [[name, dict(options)] for name, options in plan],
        }
    )
    return "sha256:" + hashlib.sha256(text.encode()).hexdigest()


def read_sources(
    cwd: Path,
    sources: Sequence[tuple[str, Registration]],
    backfill: Backfill,
    namespaces: Sequence[str],
) -> EvidenceCorpus:
    """Stage ``parse``: every reader's artifacts as one evidence corpus."""
    artifacts: list[Any] = []
    headers: list[ReaderHeader] = []
    for name, registration in sources:
        reader = load_reader(registration)
        options = source_options(name, backfill, namespaces)
        artifacts.extend(reader.read(ReadRequest(root=cwd, options=options)))
        hook = getattr(reader, "header_options", None)
        recorded = hook(options) if callable(hook) else options
        headers.append(ReaderHeader(reader.name, reader.version, recorded))
    return build_corpus(artifacts, headers)


# -- options -----------------------------------------------------------------------------------


@dataclass(frozen=True)
class CollateOptions:
    """The B3 to B6 options of a run, built from ``backfill.collate``."""

    cluster: ClusterOptions
    attach: AttachOptions
    identify: IdentifyOptions
    status: StatusOptions
    create: str = "off"

    @classmethod
    def from_config(cls, backfill: Backfill) -> CollateOptions:
        section = backfill.collate
        cluster = section.model_dump(
            include={"rules", "include_types", "epics", "areas", "path_depth", "min_epic_units"}
        )
        return cls(
            ClusterOptions.from_mapping(cluster),
            AttachOptions.from_mapping(section.attach.model_dump()),
            IdentifyOptions.from_mapping(section.identify.model_dump()),
            StatusOptions.from_mapping(section.status.model_dump()),
            section.create,
        )


def identify_run(
    settings: JudgeSettings,
    *,
    prior: Collation | None,
    cost_per_call_usd: float | None,
    runner: Any = None,
    confirm: bool = False,
    preview: Callable[[Any], None] | None = None,
) -> IdentifyRun:
    """The B5 runtime: the configured judge, the previous collation's verdicts as ``prior``
    (the resume), ``backfill.judge.cost_per_call_usd`` for the cost preview, and ``--yes``."""
    return IdentifyRun.from_settings(
        settings,
        runner=runner,
        prior=prior_verdicts(prior) if prior is not None else None,
        cost_per_call_usd=cost_per_call_usd,
        confirm=confirm,
        preview=preview,
    )


def collate_stage(
    corpus: EvidenceCorpus,
    baseline: Sequence[BeadEvent],
    directory: LedgerDirectory,
    *,
    local: str,
    options: CollateOptions,
    run: IdentifyRun | None,
) -> Collation:
    """Stage ``collate``: B1 to B6 over ``corpus`` against ``baseline``."""
    return collate(
        corpus,
        baseline,
        directory,
        local=local,
        options=options.cluster,
        attach_options=options.attach,
        identify_options=options.identify,
        identify_run=run,
        status_options=options.status,
    )


def load_prior(path: Path) -> tuple[Collation | None, str | None]:
    """The run directory's previous collation, or ``(None, why)`` when absent or unreadable."""
    if not path.is_file():
        return None, None
    try:
        return read_collation(path), None
    except (CollationFormatError, OSError, ValueError) as exc:
        return None, f"previous collation ignored: {exc}"


# -- create mode -------------------------------------------------------------------------------


def project_for_create(
    create: str,
    collation: Collation,
    corpus: EvidenceCorpus,
    baseline: Sequence[BeadEvent],
    *,
    prefix: str,
    group: str,
    extracted_at: str,
) -> tuple[tuple[BeadEvent, ...], dict[str, Any]]:
    """Phase C under ``create``: ``(events, report)``.

    ``off``: link only. A NEW unit's create, labels, phase and edges are dropped, and so is any
    edge into a bead that would have been created, so nothing the plan applies can name a bead
    that does not exist; PRESENT units' appends and stamps stay. ``propose`` is wired by the
    create-mode bead (bh-iv61r): until then it is refused rather than half done."""
    if create == "propose":
        raise BackfillError(
            "backfill.collate.create = propose is not wired in this build (bh-iv61r); "
            "use create = off (link only)"
        )
    if create != "off":
        raise BackfillError(f"unknown create mode {create!r}; known: off, propose")
    projection = project(
        collation,
        corpus,
        baseline,
        prefix=prefix,
        group=group,
        extracted_at=extracted_at,
        options=ProjectOptions(),
    )
    new_beads = {unit.bead for unit in projection.units if unit.decision == "NEW" and unit.bead}
    kept: list[BeadEvent] = []
    for event in projection.events:
        target = (event.data.get("target") or {}) if isinstance(event.data, Mapping) else {}
        into = target.get("id") if isinstance(target, Mapping) else None
        if event.bead.id in new_beads or into in new_beads:
            continue
        kept.append(event)
    report = {
        "create": create,
        "counts": projection.counts(),
        "residual": list(projection.residual),
        "creates_suppressed": len(projection.events) - len(kept),
    }
    return tuple(kept), report


# -- the pass ----------------------------------------------------------------------------------


@dataclass
class HistoryPass:
    """The ``history`` pass: collation projected to proposed events (module doc).

    ``propose`` recollates against the baseline it is given (reusing the stage's own collation
    when the baseline is the one that collation was made from) and projects it, so planning and
    the engine's second dry run share one code path. ``directory`` is the ledger directory,
    ``run`` the B5 runtime (judge and prior), ``settings`` the recorded run settings."""

    corpus: EvidenceCorpus
    directory: LedgerDirectory
    group: Group
    prefix: str
    options: CollateOptions
    run: IdentifyRun | None = None
    run_dir: str = ""
    sources: tuple[str, ...] = ()
    name: str = PASS_NAME
    schema_version: str = PASS_SCHEMA_VERSION
    drift_applicable: bool = True
    collation: Collation | None = field(default=None, repr=False)
    _report: dict[str, Any] = field(default_factory=dict, repr=False)

    def replay(self) -> None:
        """Answer from the current collation's recorded verdicts only: no judge, no spend. The
        engine's second dry run and a standalone apply propose through this."""
        prior = prior_verdicts(self.collation) if self.collation is not None else {}
        auto = dict(self.run.auto_accept) if self.run is not None else {}
        self.run = IdentifyRun(prior=prior, auto_accept=auto)

    @property
    def invalidating_fields(self) -> frozenset[str]:
        return FACETS

    def propose(
        self, baseline: Sequence[BeadEvent], *, extracted_at: str, judge: Any
    ) -> tuple[BeadEvent, ...]:
        baseline = tuple(baseline)
        stamp = corpus_hash(baseline)
        if self.collation is None or self.collation.baseline != stamp:
            self.collation = collate_stage(
                self.corpus,
                baseline,
                self.directory,
                local=self.prefix,
                options=self.options,
                run=self.run,
            )
        events, projected = project_for_create(
            self.options.create,
            self.collation,
            self.corpus,
            baseline,
            prefix=self.prefix,
            group=self.group.id,
            extracted_at=extracted_at,
        )
        self._report = projected
        return events

    def report(
        self, states: Mapping[BeadRef, Any], proposed: Sequence[BeadEvent]
    ) -> dict[str, Any]:
        collation = self.collation
        return {
            "run_dir": self.run_dir,
            "sources": list(self.sources),
            "prefix": self.prefix,
            "create": self.options.create,
            "judging": bool(self.run is not None and self.run.judging),
            "corpus": collation.corpus if collation else None,
            "baseline": collation.baseline if collation else None,
            "units": dict(self._report.get("counts") or {}),
            "unit_count": len(collation.units) if collation else 0,
            "conflicts": len(collation.conflicts) if collation else 0,
            "residual_units": len(collation.residual) if collation else 0,
            "projection": dict(self._report),
        }


# -- the run directory -------------------------------------------------------------------------


def default_run_dir(home: Path, hive_key: str) -> Path:
    """``<home>/state/backfill-history/<hive key>``: one persistent run directory per hive."""
    return Path(home).joinpath(*STATE_DIRS, hive_key)


def run_paths(run_dir: Path) -> dict[str, Path]:
    return {
        "evidence": run_dir / EVIDENCE_FILE,
        "collation": run_dir / COLLATION_FILE,
        "plan": run_dir / PLAN_FILE,
        "audit": run_dir / AUDIT_FILE,
        "pre_state": run_dir / PRE_STATE_FILE,
        "manifest": run_dir / RUN_FILE,
    }


def read_manifest(run_dir: Path) -> dict[str, Any]:
    path = run_dir / RUN_FILE
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) and data.get("schema") == RUN_SCHEMA else {}


def write_manifest(run_dir: Path, manifest: Mapping[str, Any]) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / RUN_FILE).write_text(
        json.dumps({"schema": RUN_SCHEMA, **manifest}, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


def stage_index(until: str) -> int:
    if until not in STAGES:
        raise BackfillError(f"--until must be one of {', '.join(STAGES)}, got {until!r}")
    return STAGES.index(until)


def write_collation_atomic(collation: Collation, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    write_collation(collation, path)


__all__ = [
    "AUDIT_FILE",
    "COLLATION_FILE",
    "EVIDENCE_FILE",
    "FACETS",
    "PASS_NAME",
    "PLAN_FILE",
    "PLUGIN_READERS",
    "PRE_STATE_FILE",
    "RUN_FILE",
    "RUN_SCHEMA",
    "STAGES",
    "CollateOptions",
    "HistoryPass",
    "collate_stage",
    "default_run_dir",
    "discover",
    "dumps_collation",
    "identify_run",
    "load_prior",
    "parse_key",
    "plugin_reader_table",
    "project_for_create",
    "read_evidence",
    "read_manifest",
    "read_sources",
    "resolve_sources",
    "run_paths",
    "source_options",
    "stage_index",
    "write_collation_atomic",
    "write_evidence",
    "write_manifest",
]
