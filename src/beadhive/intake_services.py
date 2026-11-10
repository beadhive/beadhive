"""Legacy composition root of :mod:`beadhive.modules.intake` (the ``hive_services.py`` precedent).

The module stays pure. This outer layer binds its ports to the established legacy surfaces: bd
(through ``bd_cli`` routes and ``bd.err_line``), the registry, ``validate`` and ``store_target``.
It is the ONLY legacy file that may import the module's ``domain`` / ``application`` layers
(gap G7); every other legacy caller (``report``, ``triage``, ``escalate``, ``work_intake``,
``mcp``) goes through the functions here or the module's public API.

What is composed here:

* the :class:`~beadhive.adapters.intake.BdReportStore` over a hive directory's bd routes. The
  store is bound to the hive's OPERATIONAL store, resolved with
  ``store_target.operational_store`` (which can never be the public projection store); nothing
  here ever resolves ``SCOPE_PUBLIC`` or writes a public store;
* the :class:`~beadhive.modules.intake.IntakeService` over that store and the legacy label
  validator (``validate.bead_violations``);
* the rendering of the module's structured refusals and store failures as the ``(code, error,
  ...)`` tuples the legacy callers have always returned, byte for byte;
* ``reroute --to <hive>`` as an orchestration (re-file in the target hive, then close the source
  as rerouted), which is what retired the ``triage -> report`` legacy cycle edge;
* link (I5): ``bh work link`` and ``accept --work`` / ``--new-work`` as :func:`link_report` and
  :func:`accept_to_work`, over the same operational store and the root lock, off unless
  ``intake.external.enabled`` and with no tracker (a link reads and writes no tracker);
* the supervised outbox sender (I9a): the root :class:`FlockIntakeLock` (``guard_primary`` for
  one host and ``guard.live_epoch`` as the epoch, both from ONE evaluation,
  ``guard.primary_epoch``; an ``fcntl.flock`` on a per-binding file in the hive's private
  ``.bh/`` state for one process; the store's ``seq`` re-check is the record half; a
  one-process batch reuses one host check and epoch read for at most
  ``intake.external.outbox.batch_recheck_seconds``, opt-in, default 0), a random
  :class:`RandomIdSource`, the outbox id index (:func:`outbox_index`, ``bh-pc9rf``), the
  :class:`LeakPolicy` and store posture, and :func:`outbox_service_for`; the outbox is off unless
  the caller passes an enabled :class:`OutboxPolicy`;
* the outbox's production wiring (:func:`outbox_wiring`, ``bh-uavc2``): the tracker, own identity
  (the bound project plus :func:`install_id`), marker codec (the bound port's optional
  ``MarkerCodec``, so this root never imports the plugin) and owned label prefixes, all from the
  hive's public-store binding, only when intake, the outbox and the tracker plugin are all on;
* the conversation (``bh-o9rep``): :func:`thread_service_for` and :func:`thread_call` compose
  ``ThreadService`` when ``intake.external.comments.enabled``, for ``bh work thread``;
* waiting for the reporter (``bh-61xkc``): :func:`needs_info` (on the outbox port, with the
  thread service) and :func:`wake` (on the thread port) when
  ``intake.external.needs_info.enabled``; the sweep form also needs ``needs_info.sweep``. A link
  catches a report up on already merged work when ``milestones.catch_up_on_link`` is on;
* the process ``TrackerAdapters`` (:func:`process_adapters`), built from the tracker-github plugin
  and ``run.github_token_source()``, only when ``intake.external.enabled`` and
  ``plugins.tracker-github.enabled`` are both set (``bh-kzs0b``).

Nothing is cached, so every test and integration patch point stays live for each command.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import logging
import os
import re
import socket
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from . import bd, bd_cli, intake_beads, phase_timings, registry, store_target, validate
from .adapters.intake import ApiReportStore, BdReportStore, FileCheckpointStore, FileOutboxIndex
from .modules.intake import (
    ORIGIN_GITHUB,
    ORIGIN_REPORT,
    REPORT_TYPES,
    ConsiderRequest,
    DeliverySweep,
    DeliverySweepRequest,
    DiscoveryOutcome,
    DiscoveryRequest,
    Disposition,
    DispositionOutcome,
    DispositionRequest,
    HiveTriplet,
    HoldSweep,
    HoldSweepRequest,
    ImportOutcome,
    ImportRequest,
    IntakeLock,
    IntakeRefused,
    IntakeService,
    IntakeStoreError,
    InvestigateOutcome,
    InvestigateRequest,
    IssueDiscoverer,
    IssueImporter,
    LinkOutcome,
    LinkRequest,
    NeedsInfoRequest,
    OwnIdentity,
    QueueQuery,
    RefusalReason,
    ReportDraft,
    ReportRelations,
    StorePosture,
    StoreScope,
    ThreadPolicy,
    ThreadRequest,
    WakeRequest,
    bind_intake,
)
from .modules.intake.application import (  # the composition root may (gap G7)
    MilestoneService,
    OutboxService,
    StoreOutboxView,
    ThreadService,
)
from .modules.intake.domain import (  # the composition root may (gap G7)
    BatchSendOutcome,
    CompletionPolicy,
    LeakPolicy,
    MergeFact,
    MilestoneOutcome,
    MilestonePolicy,
    NoticeFact,
    OutboxEvent,
    OutboxPolicy,
    ReleaseFact,
    ReopenOutcome,
    StartFact,
    dupes_touching,
    mint_grant_id,
    mint_outbox_id,
)


class ValidateLabelPolicy:
    """The closed label vocabulary a new bead is checked against (``validate.bead_violations``).

    Scoped to the new bead's own labels, exactly as ``report.file_report`` always did: a
    cross-hive reporter has no authority over the target hive's pre-existing label debt."""

    def __init__(self, cfg: Mapping[str, Any], prefix: str) -> None:
        self._cfg = cfg
        self._prefix = prefix

    def violations(self, labels) -> tuple[str, ...]:
        return tuple(validate.bead_violations(self._cfg, f"{self._prefix}-intake", list(labels)))


class _NoLabelPolicy:
    """A directory-only service never files a report, so it never checks labels."""

    def violations(self, labels) -> tuple[str, ...]:  # pragma: no cover - guards a misuse
        raise RuntimeError("a directory-scoped intake service cannot file a report")


def store_scope(cfg: Mapping[str, Any], entry: Mapping[str, Any]) -> StoreScope:
    """The scope of ``entry``'s OPERATIONAL store. ``store_target.operational_store`` is the only
    resolution used: it never answers with the public store."""
    target = store_target.operational_store(entry)
    return StoreScope(
        operational_prefix=target.prefix,
        hive=HiveTriplet(str(entry["provider"]), str(entry["org"]), str(entry["repo"])),
        public_prefixes=registry.public_store_prefixes(cfg),
    )


def report_store(
    directory, scope: StoreScope | Callable[[], StoreScope], *, entry=None, cli_only=False
) -> BdReportStore:
    """The report store for ``directory`` (a hive checkout, or the hub cache of a clone-on-demand
    hive): served from the hive's supervised Beads service or from bd's named routes, as
    ``work.beads.route`` says (:mod:`beadhive.intake_beads`). ``entry`` is the hive's registry
    entry when the caller holds it; a caller with only the directory leaves it ``None`` and the
    entry is looked up if the store ever needs the route. ``cli_only`` is the plain ``bd`` store
    whatever ``work.beads.route`` says, for a read-only diagnostic that must never depend on the
    service it may be diagnosing."""
    if cli_only:
        routes = phase_timings.timed_routes(bd_cli.routes(directory))
        return BdReportStore(routes, scope, error_line=bd.err_line)
    select = intake_beads.ApiSelector(
        directory,
        entry if entry is not None else (lambda: intake_beads.entry_of_directory(directory)),
    )
    routes = phase_timings.timed_routes(bd_cli.routes(directory))  # bh-17g6b: timed if measuring
    api = phase_timings.timed_selector(select)
    return ApiReportStore(routes, scope, api=api, error_line=bd.err_line)


def service_for(
    cfg: Mapping[str, Any], entry: Mapping[str, Any], directory, *, binary: str = "bh"
) -> IntakeService:
    """The intake service for ``entry``, writing into ``directory``. ``binary`` is the CLI alias
    the recorded history names (``filed via <binary> report``)."""
    store = report_store(directory, lambda: store_scope(cfg, entry), entry=entry)
    labels = ValidateLabelPolicy(cfg, str(entry["prefix"]))
    return IntakeService(store, labels, binary=binary)


def _no_scope() -> StoreScope:  # pragma: no cover - guards a misuse
    raise RuntimeError("a directory-scoped intake store has no store scope")


def directory_store(directory, *, entry=None, cli_only=False) -> BdReportStore:
    """A store for reads and dispositions of the hive at ``directory``: none of them reads the
    scope, so no hive entry is needed for that (``entry``, when the caller holds it, only spares
    the route a lookup)."""
    return report_store(directory, _no_scope, entry=entry, cli_only=cli_only)


def service_for_directory(directory, *, entry=None, cli_only=False) -> IntakeService:
    """A service for the queue and the dispositions of the hive at ``directory``."""
    return IntakeService(
        directory_store(directory, entry=entry, cli_only=cli_only), _NoLabelPolicy()
    )


# -- structured failures as the legacy messages ----------------------------------------------


def store_error_message(exc: IntakeStoreError) -> str:
    """The legacy one-line message for a store failure, keyed on the failed step."""
    operation, detail = exc.operation, exc.detail
    if operation.startswith("stamp:"):
        what = "origin" if operation == "stamp:origin" else "intake state"
        return f"filed {exc.bead} but could not set {what}: {detail}"
    if operation == "clear_intake":
        return f"could not clear intake state: {detail}"
    if operation == "assign":
        return f"bounce to superintendent failed: {detail}"
    if operation == "open_hold":
        return (
            f"created {exc.bead} but could not hold it: {detail} "
            "(it is open and unheld: `bh doctor` reports it, with the command that holds it)"
        )
    return detail


# -- filing (bh report, bh escalate) ---------------------------------------------------------


def file_report(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    title: str,
    report_type: str,
    actor: str,
    *,
    description: str = "",
    origin: str = ORIGIN_REPORT,
    binary: str = "bh",
) -> tuple[int, str, str]:
    """File a report into ``entry``'s store at ``directory``; ``(exit, error, new_id)``.

    ``new_id`` is set on a half-filed report (the bead exists but a stamp failed) so the caller
    can still name it. Hive resolution, clone-on-demand and the push stay with ``report``."""
    try:
        service = service_for(cfg, entry, directory, binary=binary)
        new_id = service.file_report(
            ReportDraft(title, report_type, origin, actor, description=description)
        )
    except IntakeRefused as exc:
        return 1, exc.detail, ""
    except IntakeStoreError as exc:
        return exc.returncode, store_error_message(exc), exc.bead
    except store_target.StoreResolutionError as exc:
        return 1, str(exc), ""
    return 0, "", new_id


def entry_dupes(directory, new_id: str, threshold: float = 0.5) -> list[dict]:
    """The ``bd find-duplicates`` pair records touching ``new_id`` in the store at ``directory``
    (dedup on entry). Empty when bd cannot answer."""
    pairs = directory_store(directory).duplicate_pairs(threshold=threshold)
    return [dict(pair.raw) for pair in dupes_touching(pairs, [new_id])]


# -- the queue and the duplicate reads -------------------------------------------------------


def queue_rows(directory, source: str = "") -> list[dict]:
    """The untriaged intake queue as the store's own rows, optionally narrowed to one resolved
    ``origin`` channel (source-agnostic otherwise)."""
    view = service_for_directory(directory).queue(
        QueueQuery(channel=source, include_duplicates=False)
    )
    return [dict(item.raw) for item in view.items]


def duplicate_rows(directory, threshold: float = 0.5, method: str = "mechanical") -> list[dict]:
    """Likely-duplicate pair records across a hive's open issues (``bd find-duplicates``)."""
    pairs = directory_store(directory).duplicate_pairs(threshold=threshold, method=method)
    return [dict(pair.raw) for pair in pairs]


# -- the lifetime intake-hold -----------------------------------------------------------------


def sweep_holds(
    directory, *, repair: bool = False, actor: str = "", entry=None, cli_only: bool = False
) -> HoldSweep:
    """Compare the bound reports of the store at ``directory`` with its open ``intake-hold:`` gates
    (and, with ``repair``, open a missing hold and resolve a stale one). Reads only the operational
    store; a hive that never imported a report has none and sweeps clean."""
    return service_for_directory(directory, entry=entry, cli_only=cli_only).sweep_holds(
        HoldSweepRequest(actor, repair)
    )


# -- the delivery a rejected report must not hold (bh-5kpwc) -----------------------------------


def sweep_delivery(
    directory, *, repair: bool = False, actor: str = "", entry=None, cli_only: bool = False
) -> DeliverySweep:
    """List the rejected (or duplicate-closed) reports of the store at ``directory`` that still
    carry a ``delivery`` value and, with ``repair``, clear it. The listing is the dry run. Writes
    only the bead store: ``bh work labels sync --include-closed`` then drafts the label removal on
    the issue, and nothing is sent."""
    return service_for_directory(directory, entry=entry, cli_only=cli_only).sweep_delivery(
        DeliverySweepRequest(actor, repair)
    )


# -- import (bh work import) ------------------------------------------------------------------


@dataclass(frozen=True)
class ImportResult:
    """The rendering input of one ``bh work import``: ``code`` is the exit status, ``reason`` the
    stable refusal code (empty on success, ``store_error`` for a store failure), ``error`` the
    one-line message naming the fix, ``outcome`` what was (or, for a dry run, would be) done."""

    code: int
    error: str = ""
    reason: str = ""
    outcome: ImportOutcome | None = None
    #: The report a failed import had already created and held, when it did.
    report: str = ""


def external_intake_settings(cfg: Mapping[str, Any]) -> tuple[bool, str]:
    """``(intake.external.enabled, intake.external.default_type)``: off, and ``bug``, by default.
    Host-owned and global (no per-hive override): only a literal ``true`` enables it."""
    section = ((cfg.get("intake") or {}).get("external")) or {}
    return section.get("enabled") is True, str(section.get("default_type") or "bug")


#: The one tracker plugin this build composes into the process (the only id in
#: ``KNOWN_TRACKER_ADAPTERS``); its ``plugins.<id>.enabled`` is the adapter-construction switch.
TRACKER_PLUGIN_ID = "tracker-github"

#: What an operator sets to make a tracker reachable; named in every refusal that needs it.
TRACKER_CONFIG_HINT = (
    f"set intake.external.enabled and plugins.{TRACKER_PLUGIN_ID}.enabled to true (both are off "
    "by default) and bind the hive's managed_repos[].stores.public.adapter"
)


def tracker_plugin_enabled(cfg: Mapping[str, Any]) -> bool:
    """``plugins.tracker-github.enabled`` is exactly ``true``, its fragment validates and
    ``plugin_kernel.enabled`` does not turn it off: the same fold the tracker composition root
    applies, so this never says "on" for a plugin that root would refuse as R6."""
    from .bootstrap import tracker as tracker_root

    settings = tracker_root.plugin_settings(TRACKER_PLUGIN_ID, cfg.get("plugins"))
    if not (settings.own_enabled and settings.valid):
        return False
    kernel = cfg.get("plugin_kernel") or {}
    switches = kernel.get("enabled") if isinstance(kernel, Mapping) else None
    return not (isinstance(switches, Mapping) and switches.get(TRACKER_PLUGIN_ID) is False)


def _credential_source_names(cfg: Mapping[str, Any]) -> tuple[str, ...]:
    """``plugins.tracker-github.credential.sources`` (``env`` and ``gh``, in precedence order;
    default both; empty means anonymous): names only, never a value."""
    from .bootstrap import tracker as tracker_root

    values = tracker_root.plugin_settings(TRACKER_PLUGIN_ID, cfg.get("plugins")).values
    return tuple(((values.get("credential") or {}).get("sources")) or ())


def _credential_cache(cfg: Mapping[str, Any]) -> bool:
    """``plugins.tracker-github.credential.per_invocation_cache`` (opt-in, default off,
    ``bh-25fvb``): keep the first token in memory for the rest of the command."""
    from .bootstrap import tracker as tracker_root

    values = tracker_root.plugin_settings(TRACKER_PLUGIN_ID, cfg.get("plugins")).values
    return ((values.get("credential") or {}).get("per_invocation_cache")) is True


def process_adapters(cfg: Mapping[str, Any], *, resident: bool = False) -> Any:
    """The process's ``TrackerAdapters`` for ``cfg``, or ``None`` unless the tracker is switched on.

    Two host-owned switches, both off by default, must both be ``true``: ``intake.external.enabled``
    and ``plugins.tracker-github.enabled``. Otherwise ``None``, and the verbs refuse (R0) naming
    :data:`TRACKER_CONFIG_HINT`. The binding, the per-operation enables and every other tracker
    rule are still decided per store by ``TrackerAdapters.for_store``.

    Composition reads no credential: the token source is ``run.github_token_source()`` over the
    configured ``credential.sources`` (default env, then ``gh auth token``), called by the adapter
    once per operation and never cached, logged or stored here, unless the operator opted into
    ``credential.per_invocation_cache`` (bh-25fvb), which keeps the first token in that source's
    memory for the one command that built it. The result is built fresh per call (nothing is
    cached at module level), so a command, or the daemon's timer, owns exactly one for its
    configuration snapshot. *resident* is for a process that keeps the adapters for its whole
    life (the host daemon's discovery timer): it never caches the token, whatever the key says,
    so a token rotated or revoked while the daemon runs is picked up by its next operation, and
    it never keeps an HTTP client or a memoized read across operations either (a command's
    adapters reuse one client and the org's issue-field definitions, bh-vy7dx and bh-n0h15; the
    token is still resolved per operation whenever the cache key is off). This is the one place
    the production wiring lands; tests inject ``adapters=`` or ``tracker=`` instead."""
    if not external_intake_settings(cfg)[0] or not tracker_plugin_enabled(cfg):
        return None
    from . import run
    from .bootstrap.tracker import TrackerAdapters

    return TrackerAdapters(
        plugins=cfg.get("plugins"),
        plugin_kernel=cfg.get("plugin_kernel"),
        token_source=run.github_token_source(
            names=_credential_source_names(cfg), cache=not resident and _credential_cache(cfg)
        ),
        command_scoped=not resident,
    )


def _no_tracker_wiring() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED,
        f"no tracker adapter is wired: {TRACKER_CONFIG_HINT}, then run the verb again",
    )


def _no_outbox_wiring() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED,
        f"the outbox has no tracker wired: {TRACKER_CONFIG_HINT} (the outbox's tracker, own "
        "identity and marker codec all come from that binding), then run the verb again",
    )


def _no_thread_wiring() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED,
        f"comment ingestion has no tracker wired: {TRACKER_CONFIG_HINT}, and set "
        f"plugins.{TRACKER_PLUGIN_ID}.read.comments.enabled to true, then run the verb again",
    )


def _bound_tracker(entry: Mapping[str, Any], adapters: Any):
    """The tracker port the hive's PUBLIC-store binding serves, for ``issues.read`` only.

    ``adapters`` is the process's ``bootstrap.tracker.TrackerAdapters`` (injected: this root builds
    no credential wiring). The binding names the adapter; it is the tracker adapter's own store
    scope, never a bead-store resolution. R13 (no binding), a disabled plugin, a missing package
    and ``issues.read`` not enabled are all R0: the refusal names the key to set."""
    from .bootstrap import tracker as tracker_root  # lazy: the plugin kernel loads only on use
    from .modules.tracker.contracts import Operation

    if adapters is None:
        raise _no_tracker_wiring()
    try:
        bound = adapters.for_store(entry, requested=frozenset({Operation.ISSUES_READ}))
    except tracker_root.TrackerBindingError as exc:
        raise IntakeRefused(RefusalReason.INTAKE_DISABLED, exc.message) from exc
    return bound.port


def import_message(exc: IntakeStoreError) -> str:
    """The one-line message for a store failure during an import."""
    operation, detail = exc.operation, exc.detail
    if operation.startswith("stamp:") or operation in {"open_hold", "record_snapshot"}:
        return (
            f"imported {exc.bead} but could not finish it ({operation.removeprefix('stamp:')}): "
            f"{detail} (it is held, so it is not work; run the import again to complete it)"
        )
    if operation == "create":
        return f"nothing was imported: {detail}"
    return store_error_message(exc)


def import_issue(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    reference: str,
    actor: str,
    *,
    dry_run: bool = False,
    adapters: Any = None,
    tracker: Any = None,
    lock: IntakeLock | None = None,
    hive: str = "",
) -> ImportResult:
    """Import one external issue into ``entry``'s OPERATIONAL store as an untriaged, held report.

    Off unless ``intake.external.enabled`` (R0 ``intake_disabled`` through the null port). The
    tracker is the one the hive's public-store binding serves (``adapters``, an injected
    ``TrackerAdapters``) or an explicit ``tracker`` port; with neither (the process wiring is off:
    see :func:`process_adapters`), import refuses (R0). Import only reads the tracker, holds no
    write grant, resolves no public store and never writes one. A dry run classifies R0 to R8 and
    writes nothing."""
    enabled, default_type = external_intake_settings(cfg)
    if enabled and tracker is None and adapters is None:
        adapters = process_adapters(cfg)

    def build():
        port = tracker if tracker is not None else _bound_tracker(entry, adapters)
        store = report_store(directory, lambda: store_scope(cfg, entry), entry=entry)
        labels = ValidateLabelPolicy(cfg, str(entry["prefix"]))
        importer = IssueImporter(
            store,
            labels,
            port,
            lock
            if lock is not None
            else intake_lock(cfg, directory, hive=hive, verb="work import"),
            origin=ORIGIN_GITHUB,
            default_type=default_type,
        )
        return IntakeService(store, labels, importer=importer)

    try:
        outcome = bind_intake(enabled, build).import_issue(
            ImportRequest(reference, actor, dry_run=dry_run)
        )
    except IntakeRefused as exc:
        return ImportResult(1, exc.detail, exc.reason.value)
    except IntakeStoreError as exc:
        return ImportResult(exc.returncode, import_message(exc), "store_error", report=exc.bead)
    except store_target.StoreResolutionError as exc:
        return ImportResult(1, str(exc), "store_unresolved")
    return ImportResult(0, outcome=outcome)


# -- discovery (bh work discover, and the opt-in daemon timer) ----------------------------------


@dataclass(frozen=True)
class DiscoverResult:
    """The rendering input of one discovery run (``bh work discover`` or one timer run for one
    hive): ``code`` is the exit status, ``reason`` the stable refusal code (empty on success,
    ``store_error`` for a store failure), ``error`` the one-line message naming the fix."""

    code: int
    error: str = ""
    reason: str = ""
    outcome: DiscoveryOutcome | None = None
    full: bool = False
    #: The report a failed run was upserting when its store write failed, when there was one.
    report: str = ""


@dataclass(frozen=True)
class DiscoverySettings:
    """``intake.external.discovery`` as the timer reads it. ``enabled`` is true only when BOTH
    ``intake.external.enabled`` and ``discovery.enabled`` are literal ``true`` and a positive
    ``interval_seconds`` was chosen (there is no default cadence); everything else is off."""

    enabled: bool = False
    interval_seconds: float = 0.0
    full_reconcile_every: int = 24


_LOG = logging.getLogger(__name__)


def discovery_settings(cfg: Mapping[str, Any]) -> DiscoverySettings:
    """The discovery timer's settings from the host config: off unless explicitly enabled."""
    section = ((cfg.get("intake") or {}).get("external")) or {}
    node = section.get("discovery") or {}
    interval = node.get("interval_seconds")
    every = node.get("full_reconcile_every", 24)
    valid_interval = isinstance(interval, (int, float)) and not isinstance(interval, bool)
    every = every if isinstance(every, int) and not isinstance(every, bool) and every >= 1 else 24
    on = (
        section.get("enabled") is True
        and node.get("enabled") is True
        and valid_interval
        and interval > 0
    )
    return DiscoverySettings(on, float(interval) if on else 0.0, every)


def checkpoint_dir(directory) -> Path:
    """``<hive>/.bh/intake/checkpoints``: the discovery marks, in the hive's private state."""
    from . import private_paths

    root = private_paths.ensure_repo_private_root(directory)
    if root is None:
        raise IntakeStoreError(
            f"{directory} has no private .bh/ state for the intake checkpoints",
            operation="checkpoint",
        )
    return root / "intake" / "checkpoints"


def discover_message(exc: IntakeStoreError) -> str:
    """The one-line message for a store failure during discovery."""
    where = f" at {exc.bead}" if exc.bead else ""
    step = exc.operation.removeprefix("stamp:")
    return (
        f"discovery stopped{where} ({step}): {exc.detail} "
        "(the checkpoint did not move past it; run discover again)"
    )


def discover(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    actor: str,
    *,
    full: bool = False,
    adapters: Any = None,
    tracker: Any = None,
    lock: IntakeLock | None = None,
    checkpoints: Any = None,
    hive: str = "",
) -> DiscoverResult:
    """Pull-only discovery of ``entry``'s external issues into its OPERATIONAL store, through the
    module (the import upsert), never the adapter or ``bd github sync`` directly.

    Off unless ``intake.external.enabled`` (``intake_disabled`` through the null port). The tracker
    is the one the hive's public-store binding serves (``adapters``, asked for ``issues.read`` only)
    or an explicit ``tracker`` port; with neither (the process wiring is off: see
    :func:`process_adapters`), discovery refuses. It holds no write grant, resolves no public store
    and writes none; checkpoints are the hive's private ``.bh/`` files (``checkpoints`` injects
    another :class:`CheckpointStore`). ``full`` is ``--full``: every issue including closed, then
    the id reconcile."""
    enabled, default_type = external_intake_settings(cfg)
    if enabled and tracker is None and adapters is None:
        adapters = process_adapters(cfg)

    def build():
        port = tracker if tracker is not None else _bound_tracker(entry, adapters)
        store = report_store(directory, lambda: store_scope(cfg, entry), entry=entry)
        labels = ValidateLabelPolicy(cfg, str(entry["prefix"]))
        held = (
            lock
            if lock is not None
            else intake_lock(cfg, directory, hive=hive, verb="work discover")
        )
        importer = IssueImporter(
            store, labels, port, held, origin=ORIGIN_GITHUB, default_type=default_type
        )
        marks = (
            checkpoints
            if checkpoints is not None
            else FileCheckpointStore(lambda: checkpoint_dir(directory))
        )
        discoverer = IssueDiscoverer(store, port, importer, held, marks)
        return IntakeService(store, labels, importer=importer, discoverer=discoverer)

    started = time.monotonic()
    try:
        outcome = bind_intake(enabled, build).discover(DiscoveryRequest(actor, full_reconcile=full))
        # the whole run holds the binding's IntakeLock, which the outbox sender also needs
        _LOG.info(
            "discovery held the intake lock for %.1fs (created=%d updated=%d unchanged=%d)",
            time.monotonic() - started,
            outcome.created,
            outcome.updated,
            outcome.unchanged,
        )
    except IntakeRefused as exc:
        return DiscoverResult(1, exc.detail, exc.reason.value, full=full)
    except IntakeStoreError as exc:
        return DiscoverResult(
            exc.returncode, discover_message(exc), "store_error", full=full, report=exc.bead
        )
    except store_target.StoreResolutionError as exc:
        return DiscoverResult(1, str(exc), "store_unresolved", full=full)
    return DiscoverResult(0, outcome=outcome, full=full)


# -- link (bh work link, accept --work / --new-work) --------------------------------------------


@dataclass(frozen=True)
class LinkResult:
    """The rendering input of one link, unlink or linking accept: ``code`` is the exit status,
    ``reason`` the stable refusal code (empty on success, ``store_error`` for a store failure),
    ``error`` the one-line message naming the fix. ``outcome`` is set for a link or unlink and
    ``disposition`` for a linking accept (whose ``outcome`` is the link it made)."""

    code: int
    error: str = ""
    reason: str = ""
    outcome: LinkOutcome | None = None
    disposition: DispositionOutcome | None = None
    #: A bead a failed call had already written (the new work, for a half-finished accept).
    bead: str = ""


def link_message(exc: IntakeStoreError) -> str:
    """The one-line message for a store failure during a link, unlink or linking accept."""
    operation, detail = exc.operation, exc.detail
    if operation == "create_work":
        return f"nothing was linked: could not create the work bead: {detail}"
    if operation in {"track", "untrack"}:
        return f"could not {operation} {exc.bead}: {detail} (repeat the command; it is idempotent)"
    if operation == "stamp:delivery":
        return (
            f"{exc.bead} is linked but delivery could not be set: {detail} "
            "(repeat the link; it repairs the missing value)"
        )
    if operation in {"read_outbox", "write_outbox"}:
        return f"could not record the link on {exc.bead}: {detail}"
    return store_error_message(exc)


def _linking_port(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    lock: IntakeLock | None,
    hive: str,
    verb: str,
    ids: Any = None,
    sanitizer: Any = None,
):
    """The port a link runs on: the service over the OPERATIONAL store and the root lock when
    ``intake.external.enabled``, else :class:`NullIntake` (every write refuses ``intake_disabled``
    and no store is built). No tracker is composed: a link neither reads nor writes one. With
    ``intake.external.milestones.catch_up_on_link`` (off by default) a milestone service is bound
    too, so a link to work that already merged catches the report up (drafts only)."""
    enabled, _default_type = external_intake_settings(cfg)

    def build():
        store = report_store(directory, lambda: store_scope(cfg, entry), entry=entry)
        labels = ValidateLabelPolicy(cfg, str(entry["prefix"]))
        held = lock if lock is not None else intake_lock(cfg, directory, hive=hive, verb=verb)
        refresh = transition_refresh(
            cfg, entry, directory, store, held, ids=ids, sanitizer=sanitizer
        )
        if not catch_up_on_link(cfg):
            return IntakeService(store, labels, lock=held, **refresh)
        from .adapters.intake import ScrubSanitizer

        outbox, milestones = milestone_settings(cfg)
        service = MilestoneService(
            store,
            held,
            ids=ids if ids is not None else RandomIdSource(),
            leak_policy=leak_policy(cfg),
            sanitizer=sanitizer if sanitizer is not None else ScrubSanitizer(),
            posture=store_posture(entry),
            outbox=outbox,
            milestones=milestones,
        )
        return IntakeService(
            store, labels, lock=held, milestones=service, catch_up_on_link=True, **refresh
        )

    return bind_intake(enabled, build)


def link_report(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    report: str,
    work: str,
    actor: str,
    *,
    replace: bool = False,
    remove: bool = False,
    lock: IntakeLock | None = None,
    hive: str = "",
    ids: Any = None,
    sanitizer: Any = None,
) -> LinkResult:
    """``bh work link <report> --to <work> [--replace]`` or ``--remove``.

    A bound report ``tracks`` a work bead of the hive's OPERATIONAL store (the public store is
    never resolved, and no edge crosses stores); several reports may track one work bead and an
    unlink leaves the others intact. Off unless ``intake.external.enabled``."""
    try:
        port = _linking_port(cfg, entry, directory, lock, hive, "work link", ids, sanitizer)
        outcome = (
            port.unlink(report, actor=actor)
            if remove
            else port.link(LinkRequest(report, work, actor, replace=replace))
        )
    except IntakeRefused as exc:
        return LinkResult(1, exc.detail, exc.reason.value)
    except IntakeStoreError as exc:
        return LinkResult(exc.returncode, link_message(exc), "store_error", bead=exc.bead)
    except store_target.StoreResolutionError as exc:
        return LinkResult(1, str(exc), "store_unresolved")
    return LinkResult(0, outcome=outcome)


def accept_to_work(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    report: str,
    actor: str,
    *,
    work: str = "",
    new_work: bool = False,
    issue_type: str = "",
    priority: str = "",
    lock: IntakeLock | None = None,
    hive: str = "",
    ids: Any = None,
    sanitizer: Any = None,
) -> LinkResult:
    """``bh work accept <report> --work <id>`` or ``--new-work``: accept the report AND make it
    track work (an existing bead, or one triage creates now). Every refusal is decided before the
    first write. Off unless ``intake.external.enabled``."""
    request = DispositionRequest(
        report,
        Disposition.ACCEPT,
        actor,
        issue_type=issue_type,
        priority=priority,
        work=work,
        new_work=new_work,
    )
    try:
        port = _linking_port(cfg, entry, directory, lock, hive, "work accept", ids, sanitizer)
        outcome = port.dispose(request)
    except IntakeRefused as exc:
        return LinkResult(1, exc.detail, exc.reason.value)
    except IntakeStoreError as exc:
        return LinkResult(exc.returncode, link_message(exc), "store_error", bead=exc.bead)
    except store_target.StoreResolutionError as exc:
        return LinkResult(1, str(exc), "store_unresolved")
    return LinkResult(0, disposition=outcome)


# -- reproduction work (bh-u3a53): a bead that investigates a report, never tracks it ----------


@dataclass(frozen=True)
class InvestigateResult:
    """The rendering input of one ``bh work investigate``: ``code`` is the exit status, ``reason``
    the stable refusal code (empty on success, ``store_error`` for a store failure), ``error`` the
    one-line message naming the fix. ``outcome`` is set for an investigate or a removal,
    ``relations`` for the read-only listing."""

    code: int
    error: str = ""
    reason: str = ""
    outcome: InvestigateOutcome | None = None
    relations: ReportRelations | None = None
    #: A bead a failed call had already written (the new reproduction bead).
    bead: str = ""


def investigate_message(exc: IntakeStoreError) -> str:
    """The one-line message for a store failure during ``bh work investigate``."""
    operation, detail = exc.operation, exc.detail
    if operation == "create_work":
        return f"nothing was linked: could not create the reproduction bead: {detail}"
    if operation in {"investigate", "uninvestigate"}:
        return f"could not {operation} {exc.bead}: {detail} (repeat the command; it is idempotent)"
    return store_error_message(exc)


def investigate_report(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    report: str,
    actor: str,
    *,
    work: str = "",
    new_work: bool = False,
    remove: bool = False,
    issue_type: str = "",
    priority: str = "",
    lock: IntakeLock | None = None,
    hive: str = "",
) -> InvestigateResult:
    """``bh work investigate <report> (--work <id> | --new-work) [--remove]``, or with neither the
    report's relations (read-only).

    The port is the plain service over the OPERATIONAL store and the root lock, with NO milestone
    service bound whatever the hive's milestone keys say: an investigation drafts nothing and
    writes no ``delivery`` value, by construction as well as by rule. No tracker is composed. Off
    unless ``intake.external.enabled``: every form, the listing included, then refuses
    ``intake_disabled`` before any store is built. Argument shape is the caller's to check."""
    enabled, _default_type = external_intake_settings(cfg)

    def build():
        store = report_store(directory, lambda: store_scope(cfg, entry))
        labels = ValidateLabelPolicy(cfg, str(entry["prefix"]))
        held = (
            lock
            if lock is not None
            else intake_lock(cfg, directory, hive=hive, verb="work investigate")
        )
        return IntakeService(store, labels, lock=held)

    try:
        if not enabled:
            raise IntakeRefused(
                RefusalReason.INTAKE_DISABLED,
                "external intake is off for this hive (intake.external.enabled; it is opt-in)",
            )
        port = build()
        if remove:
            outcome = port.uninvestigate(report, work, actor=actor)
        elif work or new_work:
            outcome = port.investigate(
                InvestigateRequest(
                    report,
                    actor,
                    work=work,
                    new_work=new_work,
                    issue_type=issue_type,
                    priority=priority,
                )
            )
        else:
            return InvestigateResult(0, relations=port.relations(report))
    except IntakeRefused as exc:
        return InvestigateResult(1, exc.detail, exc.reason.value)
    except IntakeStoreError as exc:
        return InvestigateResult(
            exc.returncode, investigate_message(exc), "store_error", bead=exc.bead
        )
    except store_target.StoreResolutionError as exc:
        return InvestigateResult(1, str(exc), "store_unresolved")
    return InvestigateResult(0, outcome=outcome, relations=outcome.relations)


# -- dispositions ----------------------------------------------------------------------------


def _disposing_service(directory, cfg: Mapping[str, Any] | None, verb: str) -> IntakeService:
    """The service a disposition runs on: the directory's plain one, with the opt-in transition
    label refresh bound when ``cfg`` turns it on (``intake.external.labels.on_transition``)."""
    if cfg is None or not labels_config(cfg).on_transition:
        return service_for_directory(directory)
    entry = intake_beads.entry_of_directory(directory)
    if entry is None:
        return service_for_directory(directory)
    store = directory_store(directory, entry=entry)
    lock = intake_lock(cfg, directory, hive=str(entry.get("prefix") or ""), verb=verb)
    return IntakeService(
        store, _NoLabelPolicy(), lock=lock, **transition_refresh(cfg, entry, directory, store, lock)
    )


def dispose(
    directory, request: DispositionRequest, *, cfg: Mapping[str, Any] | None = None
) -> tuple[int, str, DispositionOutcome | None]:
    """Run one triage disposition; ``(exit, error, outcome)``. With ``cfg`` turning on
    ``intake.external.labels.on_transition`` the report's labels record is refreshed after it."""
    try:
        verb = f"work {request.disposition.value}"
        return 0, "", _disposing_service(directory, cfg, verb).dispose(request)
    except IntakeRefused as exc:
        return 1, exc.detail, None
    except IntakeStoreError as exc:
        return exc.returncode, store_error_message(exc), None


class _RefileFailed(Exception):
    """The target hive refused the re-filing: nothing was written to the source."""

    def __init__(self, code: int, message: str) -> None:
        self.code = code
        self.message = message
        super().__init__(message)


def reroute_to_hive(
    directory,
    bead: str,
    actor: str,
    to_hive: str,
    *,
    file_report: Callable[..., tuple[int, str, str]],
    cfg=None,
) -> tuple[int, str, str]:
    """Re-file a mis-routed report into ``to_hive`` and close the original as rerouted.

    An orchestration across two stores: a port instance is bound to one operational store and
    never reaches into another, so the target hive's filing is the caller's ``file_report``
    (type-preserving, the same write path ``bh report`` uses) and only the source side runs
    through this hive's :class:`IntakeService`. Returns ``(exit, error, message)``."""
    filed: list[str] = []

    def refile(view) -> str:
        rtype = view.issue_type if view.issue_type in REPORT_TYPES else "bug"
        code, err, new_id = file_report(to_hive, view.title, rtype, actor, cfg=cfg)
        if err:
            raise _RefileFailed(code, f"reroute re-file failed: {err}")
        filed.append(new_id)
        return f"rerouted to {to_hive} as {new_id}"

    try:
        _disposing_service(directory, cfg, "work reroute").reroute_elsewhere(
            DispositionRequest(bead, Disposition.REROUTE, actor), refile
        )
    except _RefileFailed as exc:
        return exc.code, exc.message, ""
    except IntakeRefused as exc:
        return 1, exc.detail, ""
    except IntakeStoreError as exc:
        return exc.returncode, store_error_message(exc), ""
    return 0, "", f"✓ rerouted {bead} → {to_hive} ({filed[0]})"


# -- the supervised outbox (I9a) --------------------------------------------------------------


class FlockIntakeLock:
    """The root :class:`~beadhive.modules.intake.IntakeLock` (external-intake-module-adr.md
    section 4, ``bh-6wj9y`` R6 item 4).

    ``hold(binding_key)`` takes, in order: the host (``primary()``, ``guard.guard_primary`` in
    production; a refusal is ``IntakeRefused(NOT_PRIMARY)``), the process (an exclusive,
    never-waiting ``fcntl.flock`` on ``<lock_dir>/<sha256(binding_key)>.lock``; held elsewhere is
    ``IntakeRefused(INTAKE_BUSY)``), and yields ``epoch()`` (``guard.live_epoch``). The record half
    is the store's ``seq`` re-check. Nothing expires by time: the kernel drops a dead holder's
    flock, and a moved epoch marks its attempts stale. The directory is created on first hold.

    ``batch()`` (:class:`~beadhive.modules.intake.contracts.BatchLock`, ``bh-pc9rf``) is the
    one-process batch's scope: inside it the host check and the epoch read (on the factory about
    13 s together, every hold) are taken on the first hold and reused by the holds that follow
    until they are ``recheck_seconds`` old, then taken again; every hold still takes its own
    flock. ``0`` re-checks on every hold, as outside a batch. ``clock`` is injectable (monotonic
    seconds) so the bound is tested without waiting.

    ``admit`` (production, ``bh-25fvb``) is the host check and the epoch read in ONE evaluation
    (``guard.primary_epoch``): taken before the flock, as ``primary`` is, and its epoch yielded,
    instead of evaluating the frame-eligibility reads twice. Without it ``primary()`` runs before
    the flock and ``epoch()`` under it."""

    def __init__(
        self,
        lock_dir: Path | Callable[[], Path],
        *,
        primary: Callable[[], None] | None = None,
        epoch: Callable[[], int] | None = None,
        admit: Callable[[], int] | None = None,
        recheck_seconds: float = 0.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if admit is None and (primary is None or epoch is None):
            raise TypeError("FlockIntakeLock needs admit, or both primary and epoch")
        self._lock_dir = lock_dir
        self._primary = primary
        self._epoch = epoch
        self._admit = admit
        self._recheck = max(0.0, float(recheck_seconds))
        self._clock = clock
        self._batch_depth = 0
        self._checked: tuple[float, int] | None = None  # (taken at, epoch), inside a batch only

    def _path(self, binding_key: str) -> Path:
        directory = self._lock_dir() if callable(self._lock_dir) else self._lock_dir
        directory.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(binding_key.encode("utf-8")).hexdigest()
        return directory / f"{name}.lock"

    @contextlib.contextmanager
    def batch(self) -> Iterator[None]:
        self._batch_depth += 1
        try:
            yield
        finally:
            self._batch_depth -= 1
            if not self._batch_depth:
                self._checked = None  # the next batch, or a single hold, checks afresh

    def _reused(self) -> int | None:
        """The batch's epoch when its host check is still fresh enough to reuse, else None."""
        if not self._batch_depth or self._checked is None:
            return None
        taken, epoch = self._checked
        return epoch if self._clock() - taken < self._recheck else None

    @contextlib.contextmanager
    def hold(self, binding_key: str) -> Iterator[int]:
        timings = phase_timings.current()
        if timings is None:
            with self._hold(binding_key) as epoch:
                yield epoch
            return
        with timings.hold(binding_key), self._hold(binding_key) as epoch:  # bh-17g6b
            yield epoch

    @contextlib.contextmanager
    def _hold(self, binding_key: str) -> Iterator[int]:
        reused = self._reused()
        taken = self._clock()
        if reused is None:
            if self._admit is not None:
                with phase_timings.phase("host_check"):
                    reused = self._admit()
                if self._batch_depth:
                    self._checked = (taken, reused)
            else:
                with phase_timings.phase("host_check"):
                    self._primary()  # type: ignore[misc]
        else:
            timings = phase_timings.current()
            if timings is not None:
                timings.record("host_check_reused", 0.0)
        with self._path(binding_key).open("a+") as handle:
            try:
                with phase_timings.phase("intake_lock"):
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise IntakeRefused(
                    RefusalReason.INTAKE_BUSY,
                    f"{binding_key} is held by another intake operation; try again when it ends",
                ) from None
            try:
                if reused is None:
                    with phase_timings.phase("host_check"):
                        reused = self._epoch()  # type: ignore[misc]
                    if self._batch_depth:
                        self._checked = (taken, reused)
                yield reused
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class RandomIdSource:
    """The outbox's opaque ids from the OS's random bytes (never derived from a bead id)."""

    def __init__(self, entropy: Callable[[int], bytes] = os.urandom) -> None:
        self._entropy = entropy

    def outbox_id(self) -> str:
        return mint_outbox_id(self._entropy(16))

    def grant_id(self) -> str:
        return mint_grant_id(self._entropy(16))


def _admitted_epoch(
    cfg: Mapping[str, Any], hive: str, verb: str = "work outbox"
) -> Callable[[], int]:
    """The host check and the epoch read in one evaluation (``guard.primary_epoch``,
    ``bh-25fvb``); a refusal (``guard_primary``'s ``typer.Exit``) is ``NOT_PRIMARY``."""

    def admit() -> int:
        import typer

        from . import guard

        try:
            return int(guard.primary_epoch(hive, cfg=cfg, verb=verb))
        except typer.Exit as exc:
            raise IntakeRefused(
                RefusalReason.NOT_PRIMARY,
                f"this host is not the hive's primary writer; run `bh {verb}` on the primary",
            ) from exc

    return admit


def _intake_state_dir(directory, what: str, operation: str) -> Path:
    """``<hive>/.bh/intake``: the intake module's private, never-published local state."""
    from . import private_paths

    root = private_paths.ensure_repo_private_root(directory)
    if root is None:
        raise IntakeStoreError(
            f"{directory} has no private .bh/ state for the {what}", operation=operation
        )
    return root / "intake"


def lock_dir(directory) -> Path:
    """``<hive>/.bh/intake/locks``: the per-binding lock files, in the hive's private state."""
    return _intake_state_dir(directory, "intake lock", "lock") / "locks"


def outbox_index(directory) -> FileOutboxIndex:
    """``<hive>/.bh/intake/outbox-index.json``: which report holds each outbox id (``bh-pc9rf``),
    a local cache the sender confirms on every read and rebuilds from any full scan. Resolved on
    first use, so building it touches nothing."""
    from .adapters.intake.outbox_index import INDEX_FILE

    return FileOutboxIndex(
        lambda: _intake_state_dir(directory, "outbox index", "outbox_index") / INDEX_FILE
    )


#: How long a one-process batch reuses its host check and epoch read (``bh-pc9rf``), unless
#: ``intake.external.outbox.batch_recheck_seconds`` says otherwise: 0, every hold checks (opt-in).
DEFAULT_BATCH_RECHECK_SECONDS = 0


def batch_recheck_seconds(cfg: Mapping[str, Any]) -> float:
    """``intake.external.outbox.batch_recheck_seconds`` (a non-negative number; anything else is
    the default)."""
    section = ((cfg.get("intake") or {}).get("external") or {}).get("outbox") or {}
    value = section.get("batch_recheck_seconds", DEFAULT_BATCH_RECHECK_SECONDS)
    if isinstance(value, bool) or not isinstance(value, int | float) or value < 0:
        return float(DEFAULT_BATCH_RECHECK_SECONDS)
    return float(value)


#: ``<hive>/.bh/intake/install-id``: 32 lowercase hex characters, nothing else.
INSTALL_ID_FILE = "install-id"
_INSTALL_ID = re.compile(r"[0-9a-f]{32}")


def install_id(directory, *, entropy: Callable[[int], bytes] = os.urandom) -> str:
    """The install UUID own-message markers carry (``OwnIdentity.install``; port ADR: local,
    non-secret state, never configuration).

    Minted once from ``entropy`` (the OS's random bytes) on first use and kept in the hive's
    private ``.bh/intake/install-id``; never derived from a bead id, a host id or a path, so a
    marker reveals nothing internal. Published with a hard link from a fully written temporary
    file, so two processes racing to mint it agree on the first and neither ever reads a partial
    file. A present but malformed file is refused (``IntakeStoreError``), never re-minted: a new
    id would silently stop this install recognising its own earlier comments."""
    path = _intake_state_dir(directory, "intake install id", "install_id") / INSTALL_ID_FILE
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        minted = entropy(16).hex()
        temporary = path.with_name(f".{INSTALL_ID_FILE}.{os.getpid()}.{minted}.tmp")
        temporary.write_text(minted + "\n", encoding="ascii")
        try:
            os.link(temporary, path)
        except FileExistsError:
            pass  # another process minted it first: theirs stands
        finally:
            temporary.unlink(missing_ok=True)
    value = path.read_text(encoding="ascii", errors="replace").strip()
    if not _INSTALL_ID.fullmatch(value):
        raise IntakeStoreError(
            f"{path} is not a 32-hex install id; restore it (deleting it makes this install "
            "stop recognising its own earlier comments)",
            operation="install_id",
        )
    return value


def intake_lock(
    cfg: Mapping[str, Any], directory, *, hive: str = "", verb: str = "work outbox"
) -> FlockIntakeLock:
    """The production lock for the hive at ``directory`` (``hive`` is the CLI's selector; ``verb``
    names the write verb in a primary-host refusal)."""
    return FlockIntakeLock(
        lambda: lock_dir(directory),
        admit=_admitted_epoch(cfg, hive, verb),
        recheck_seconds=batch_recheck_seconds(cfg),
    )


def fleet_prefixes(cfg: Mapping[str, Any]) -> frozenset[str]:
    """Every bead prefix in the fleet, operational and public (the leak check's ``bead_id``)."""
    prefixes: set[str] = set(registry.public_store_prefixes(cfg))
    for entry in registry.hives(cfg):
        prefix = str((entry or {}).get("prefix") or "")
        if prefix:
            prefixes.add(prefix)
    return frozenset(prefixes)


def leak_policy(
    cfg: Mapping[str, Any], *, owned_label_prefixes: tuple[str, ...] = ()
) -> LeakPolicy:
    """What counts as internal for this fleet: every prefix, every closed dimension, and the
    private-state roots (``.bh/`` and home directories are always private)."""
    return LeakPolicy(
        bead_prefixes=fleet_prefixes(cfg),
        private_dimensions=frozenset(registry.closed_dimensions(cfg)),
        owned_label_prefixes=tuple(owned_label_prefixes),
    )


def store_posture(entry: Mapping[str, Any], *, acknowledged: bool = False) -> StorePosture:
    """Whether ``entry``'s operational store would be published: a one-store (legacy) hive, or an
    internal store whose remote is the code origin. Never resolves the public store."""
    target = store_target.operational_store(entry)
    published = (
        target.scope == store_target.SCOPE_LEGACY
        or target.transport == store_target.TRANSPORT_GIT_REF
    )
    return StorePosture(published=published, acknowledged=acknowledged)


def outbox_service_for(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    *,
    tracker,
    identity: OwnIdentity,
    marker_line: Callable[..., str],
    policy: OutboxPolicy | None = None,
    hive: str = "",
    acknowledged: bool = False,
    sanitizer=None,
    lock=None,
    ids=None,
    owned_label_prefixes: tuple[str, ...] = (),
    threads: ThreadPolicy | None = None,
    close_enabled: bool = False,
    triage: Any = None,
    type_enabled: bool = False,
    fields_enabled: bool = False,
    index: Any = None,
    reopen_enabled: bool = False,
) -> IntakeService:
    """The intake service for ``entry`` with the supervised sender bound over the INJECTED
    ``tracker`` (and its ``marker_line`` codec). With ``threads`` (the comment-ingestion policy,
    passed only when ``intake.external.comments.enabled``) the same tracker, identity and lock
    also serve the thread operations (:func:`thread_service_for`). ``close_enabled`` is the bound
    adapter's own ``issue.close`` enable (:func:`tracker_wiring`): the sender's close gate, and
    ``type_enabled`` / ``fields_enabled`` its ``issue.type`` / ``issue.fields`` enables, the
    gates of those two operations, and ``reopen_enabled`` its ``issue.reopen`` enable (the reopen
    gate); ``triage`` (:func:`triage_binding`) lets the close backfill
    compute a missing reply's text; ``index`` is the outbox id index (default
    :func:`outbox_index` in the hive's private state).

    Off by default: with ``policy`` absent or disabled no sender is bound at all, so every outbox
    operation refuses with ``OUTBOX_DISABLED`` before the tracker is touched. Enabling it here is
    still never sufficient to post: the adapter's own per-operation enable and an operator
    approval minting a single-use grant are both required. ``sanitizer`` defaults to the shared
    :class:`~beadhive.adapters.intake.ScrubSanitizer`; the store is the OPERATIONAL one only."""
    policy = with_reopen(
        with_value_gates(
            with_close(policy or OutboxPolicy(), close_enabled), type_enabled, fields_enabled
        ),
        reopen_enabled,
    )
    store = report_store(directory, lambda: store_scope(cfg, entry), entry=entry)
    labels = ValidateLabelPolicy(cfg, str(entry["prefix"]))
    if not policy.enabled:
        return IntakeService(store, labels)
    from .adapters.intake import ScrubSanitizer

    held = lock if lock is not None else intake_lock(cfg, directory, hive=hive)
    source = ids if ids is not None else RandomIdSource()
    scrubber = sanitizer if sanitizer is not None else ScrubSanitizer()
    leaks = leak_policy(cfg, owned_label_prefixes=owned_label_prefixes)
    posture = store_posture(entry, acknowledged=acknowledged)
    # the completion policy (I10): a settled milestone record closes the report it completes
    milestones = MilestoneService(
        store,
        held,
        ids=source,
        leak_policy=leaks,
        sanitizer=scrubber,
        posture=posture,
        outbox=policy,
        milestones=milestone_settings(cfg)[1],
    )
    sender = OutboxService(
        store,
        tracker,
        held,
        ids=source,
        identity=identity,
        marker_line=marker_line,
        policy=policy,
        leak_policy=leaks,
        sanitizer=scrubber,
        posture=posture,
        holder=f"{socket.gethostname()}:{os.getpid()}",
        completion=milestones.complete,
        index=index if index is not None else outbox_index(directory),
    )
    conversation = (
        thread_service_for(store, tracker, held, posture=posture, identity=identity, policy=threads)
        if threads is not None
        else None
    )
    refresh = transition_refresh(
        cfg,
        entry,
        directory,
        store,
        held,
        ids=source,
        sanitizer=scrubber,
        gates=(policy.type_enabled, policy.fields_enabled),
    )
    return IntakeService(
        store,
        labels,
        outbox=sender,
        milestones=milestones,
        threads=conversation,
        triage=triage,
        **refresh,
    )


def with_close(policy: OutboxPolicy, close_enabled: Any) -> OutboxPolicy:
    """``policy`` with the ``issue.close`` gate set from the bound adapter's own enable: on only
    with the outbox on and the tracker plugin's ``write.issue_close.enabled`` resolved true for
    the hive's binding. There is no separate intake key: the adapter switch is the one gate."""
    return replace(policy, close_enabled=policy.enabled and close_enabled is True)


def with_reopen(policy: OutboxPolicy, reopen_enabled: Any) -> OutboxPolicy:
    """``policy`` with the ``issue.reopen`` gate set from the bound adapter's own enable
    (``write.issue_reopen.enabled``): on only with the outbox on. Like the close, there is no
    separate intake key (``bh-ih484``)."""
    return replace(policy, reopen_enabled=policy.enabled and reopen_enabled is True)


def with_value_gates(policy: OutboxPolicy, type_enabled: Any, fields_enabled: Any) -> OutboxPolicy:
    """``policy`` with the ``issue.type`` and ``issue.fields`` gates set from the bound adapter's
    own enables (``write.issue_type.enabled`` / ``write.issue_fields.enabled``), each on only with
    the outbox on. Like the close, there is no separate intake key."""
    return replace(
        policy,
        type_enabled=policy.enabled and type_enabled is True,
        fields_enabled=policy.enabled and fields_enabled is True,
    )


def value_gates(cfg: Mapping[str, Any], entry: Mapping[str, Any]) -> tuple[bool, bool]:
    """Whether the tracker plugin bound to ``entry``'s public store switches ``issue.type`` /
    ``issue.fields`` on, read from config alone (no binding, no request): the tracker-free
    transition refresh's gates. Off for an unbound store, a disabled or invalid plugin."""
    from .bootstrap import tracker as tracker_root  # lazy: only an on_transition hive gets here
    from .modules.tracker.contracts import Operation

    try:
        adapter_id = tracker_root.bound_adapter_id(entry, "public")
    except tracker_root.TrackerBindingError:
        return False, False
    if adapter_id is None:
        return False, False
    settings = tracker_root.plugin_settings(adapter_id, cfg.get("plugins"))
    if not (settings.own_enabled and settings.valid):
        return False, False
    operations = settings.operations()
    return Operation.ISSUE_TYPE in operations, Operation.ISSUE_FIELDS in operations


# -- the merge and release milestones (I10): they draft, they never send ---------------------


def milestone_settings(cfg: Mapping[str, Any]) -> tuple[OutboxPolicy, MilestonePolicy]:
    """The outbox policy and the milestone switches from ``intake.external``: every draft off,
    ``close-on-merge``, and the outbox off, by default. Host-owned like the rest of the section;
    only a literal ``true`` switches anything on (the ``labels.change`` gate,
    ``outbox.labels_enabled``, additionally needs ``outbox.enabled``), and an unrecognised
    completion policy takes the conservative ``close-on-release`` (the report stays open) rather
    than closing early."""
    section = ((cfg.get("intake") or {}).get("external")) or {}
    milestones = section.get("milestones") or {}
    outbox = section.get("outbox") or {}
    try:
        completion = CompletionPolicy(section.get("completion_policy") or "close-on-merge")
    except ValueError:
        completion = CompletionPolicy.CLOSE_ON_RELEASE
    return (
        OutboxPolicy(
            enabled=outbox.get("enabled") is True,
            labels_enabled=outbox.get("enabled") is True and outbox.get("labels_enabled") is True,
        ),
        MilestonePolicy(
            draft_on_merge=milestones.get("draft_on_merge") is True,
            draft_on_release=milestones.get("draft_on_release") is True,
            append_release_to_closed=milestones.get("append_release_to_closed") is True,
            completion=completion,
            draft_on_start=milestones.get("draft_on_start") is True,
            draft_on_blocked=milestones.get("draft_on_blocked") is True,
            draft_on_deprioritised=milestones.get("draft_on_deprioritised") is True,
        ),
    )


def catch_up_on_link(cfg: Mapping[str, Any]) -> bool:
    """``intake.external.milestones.catch_up_on_link`` (host-owned, off): whether a link to work
    that already merged gives the report the milestones it missed. Only a literal ``true``."""
    section = (((cfg.get("intake") or {}).get("external")) or {}).get("milestones") or {}
    return section.get("catch_up_on_link") is True


def needs_info_settings(cfg: Mapping[str, Any]) -> tuple[bool, bool]:
    """``(enabled, sweep)`` from ``intake.external.needs_info`` (host-owned, both off): whether
    `bh work needs-info` / `bh work wake <report>` may run, and whether `bh work wake --all` may.
    Only a literal ``true`` enables; ``sweep`` is only effective with ``enabled``."""
    section = (((cfg.get("intake") or {}).get("external")) or {}).get("needs_info") or {}
    enabled = section.get("enabled") is True
    return enabled, enabled and section.get("sweep") is True


def comments_settings(cfg: Mapping[str, Any]) -> tuple[bool, ThreadPolicy]:
    """``(intake.external.comments.enabled, the ThreadPolicy)`` from ``intake.external.comments``:
    off, with the ADR's defaults (two agreeing complete snapshots before a tombstone, purge the
    body on tombstone, no body cap). Host-owned like the rest of the section; only a literal
    ``true`` enables ingestion, and it still needs the tracker plugin's ``read.comments``."""
    section = (((cfg.get("intake") or {}).get("external")) or {}).get("comments") or {}
    confirmations = section.get("deletion_confirmations")
    cap = section.get("max_body_chars")
    policy = ThreadPolicy(
        deletion_confirmations=confirmations
        if isinstance(confirmations, int)
        and not isinstance(confirmations, bool)
        and confirmations >= 1
        else 2,
        purge_deleted_bodies=section.get("purge_deleted_bodies") is not False,
        max_body_chars=cap
        if isinstance(cap, int) and not isinstance(cap, bool) and cap >= 0
        else None,
    )
    return section.get("enabled") is True, policy


@dataclass(frozen=True)
class MilestoneResult:
    """The rendering input of one milestone trigger: ``code`` is the exit status, ``reason`` the
    stable refusal code (empty on success, ``store_error`` for a store failure), ``error`` the
    one-line message, ``outcome`` what the trigger did per report. ``tag`` and ``commit`` are the
    verified release (the release verb only)."""

    code: int
    error: str = ""
    reason: str = ""
    outcome: MilestoneOutcome | None = None
    tag: str = ""
    commit: str = ""


def _milestone_port(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    lock: IntakeLock | None,
    hive: str,
    verb: str,
    verifier: Any,
    ids: Any = None,
    sanitizer: Any = None,
):
    """The port a milestone runs on: the service over the OPERATIONAL store and the root lock when
    ``intake.external.enabled``, else :class:`NullIntake` (free: nothing is built or read). No
    tracker is composed and no sender is bound: a milestone can only draft."""
    enabled, _default_type = external_intake_settings(cfg)

    def build():
        from .adapters.intake import ScrubSanitizer

        store = report_store(directory, lambda: store_scope(cfg, entry), entry=entry)
        labels = ValidateLabelPolicy(cfg, str(entry["prefix"]))
        outbox, milestones = milestone_settings(cfg)
        held = lock if lock is not None else intake_lock(cfg, directory, hive=hive, verb=verb)
        service = MilestoneService(
            store,
            held,
            ids=ids if ids is not None else RandomIdSource(),
            leak_policy=leak_policy(cfg),
            sanitizer=sanitizer if sanitizer is not None else ScrubSanitizer(),
            posture=store_posture(entry),
            outbox=outbox,
            milestones=milestones,
            verifier=verifier,
        )
        refresh = transition_refresh(
            cfg, entry, directory, store, held, ids=ids, sanitizer=sanitizer
        )
        return IntakeService(store, labels, milestones=service, **refresh)

    return bind_intake(enabled, build)


def milestone_message(exc: IntakeStoreError) -> str:
    """The one-line message for a store failure during a milestone."""
    return f"milestone could not finish {exc.bead or 'a report'} ({exc.operation}): {exc.detail}"


def work_merged(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    works: Sequence[str],
    commit: str,
    actor: str,
    *,
    lock: IntakeLock | None = None,
    hive: str = "",
    ids: Any = None,
    sanitizer: Any = None,
) -> MilestoneResult:
    """The merge trigger for each of ``works`` (a bead, or a molecule's epic and children) landed
    as ``commit``: ``delivery:merged`` on every report that tracks one, a record drafted when the
    hive turned that on, the completion policy. Nothing is sent. Off unless
    ``intake.external.enabled``; the caller wraps this so a failure never fails the merge."""
    try:
        port = _milestone_port(
            cfg, entry, directory, lock, hive, "work merge", None, ids, sanitizer
        )
        outcome = MilestoneOutcome()
        for work in works:
            outcome = outcome.merged_with(port.on_work_merged(MergeFact(work, commit, actor)))
    except IntakeRefused as exc:
        return MilestoneResult(1, exc.detail, exc.reason.value)
    except IntakeStoreError as exc:
        return MilestoneResult(exc.returncode, milestone_message(exc), "store_error")
    except store_target.StoreResolutionError as exc:
        return MilestoneResult(1, str(exc), "store_unresolved")
    return MilestoneResult(0, outcome=outcome, commit=commit)


def delivery_release(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    tag: str,
    actor: str,
    *,
    verifier: Any = None,
    lock: IntakeLock | None = None,
    hive: str = "",
    ids: Any = None,
    sanitizer: Any = None,
) -> MilestoneResult:
    """``bh work delivery release <tag>``: verify the tag exists (read-only against git), then
    move every report whose merge the release contains to ``delivery:released`` and draft a
    record when the hive turned that on. A merge the verifier cannot show in the release stays
    ``merged`` and is reported as not verified. Nothing is sent. Off unless
    ``intake.external.enabled``."""
    enabled, _default_type = external_intake_settings(cfg)
    if not enabled:
        return MilestoneResult(
            1,
            "external intake is off for this hive (intake.external.enabled; it is opt-in)",
            RefusalReason.INTAKE_DISABLED.value,
        )
    if verifier is None:
        from .adapters.intake import GitReleaseVerifier

        verifier = GitReleaseVerifier(directory)
    commit = verifier.resolve(tag)
    if commit is None:
        return MilestoneResult(
            1,
            f"no tag {tag!r} in this hive's repository; nothing was verified or recorded "
            "(fetch tags, or check the name)",
            RefusalReason.UNKNOWN_TAG.value,
            tag=tag,
        )
    try:
        port = _milestone_port(
            cfg, entry, directory, lock, hive, "work delivery release", verifier, ids, sanitizer
        )
        outcome = port.on_release(ReleaseFact(tag, commit, actor))
    except IntakeRefused as exc:
        return MilestoneResult(1, exc.detail, exc.reason.value, tag=tag, commit=commit)
    except IntakeStoreError as exc:
        return MilestoneResult(
            exc.returncode, milestone_message(exc), "store_error", tag=tag, commit=commit
        )
    except store_target.StoreResolutionError as exc:
        return MilestoneResult(1, str(exc), "store_unresolved", tag=tag, commit=commit)
    return MilestoneResult(0, outcome=outcome, tag=tag, commit=commit)


# -- the progress milestones (bh-dev42): started from the claim hook, blocked and deprioritised
# -- from operator verbs. They draft, they never send ------------------------------------------


def draft_on_start(cfg: Mapping[str, Any]) -> bool:
    """Whether the claim hook can draft anything at all: ``intake.external.enabled``, the outbox
    and ``milestones.draft_on_start`` all on (each off by default). A pure config read, so a claim
    on a hive that did not opt in pays for nothing else."""
    enabled, _default_type = external_intake_settings(cfg)
    if not enabled:
        return False
    outbox, milestones = milestone_settings(cfg)
    return outbox.enabled and milestones.draft_on_start


def work_started(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    work: str,
    actor: str,
    *,
    lock: IntakeLock | None = None,
    hive: str = "",
    ids: Any = None,
    sanitizer: Any = None,
) -> MilestoneResult:
    """The claim hook: a ``started`` record for every report that tracks ``work`` whose delivery
    is still ``pending``, once per work (a re-claim finds the record's key and drafts nothing).
    Nothing is sent. The caller checks :func:`draft_on_start` first and wraps this so a failure
    never fails the claim."""
    try:
        port = _milestone_port(
            cfg, entry, directory, lock, hive, "work claim", None, ids, sanitizer
        )
        outcome = port.on_work_started(StartFact(work, actor))
    except IntakeRefused as exc:
        return MilestoneResult(1, exc.detail, exc.reason.value)
    except IntakeStoreError as exc:
        return MilestoneResult(exc.returncode, milestone_message(exc), "store_error")
    except store_target.StoreResolutionError as exc:
        return MilestoneResult(1, str(exc), "store_unresolved")
    return MilestoneResult(0, outcome=outcome)


#: The operator notices ``bh work delivery <event>`` drafts, by verb.
NOTICE_VERBS = {"blocked": OutboxEvent.BLOCKED, "deprioritised": OutboxEvent.DEPRIORITISED}


def delivery_notice(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    work: str,
    verb: str,
    reason: str,
    actor: str,
    *,
    lock: IntakeLock | None = None,
    hive: str = "",
    ids: Any = None,
    sanitizer: Any = None,
) -> MilestoneResult:
    """``bh work delivery blocked|deprioritised <work> --reason <text>``: draft the operator's
    public reason, under the event's fixed wording, for every report that tracks ``work`` whose
    delivery is still ``pending``. A reason the leak check flags (a bead id, a branch, a path)
    leaves the record held. Nothing is sent. Off unless ``intake.external.enabled``, the outbox
    and ``milestones.draft_on_<verb>`` are all on; each off switch refuses naming its key before
    any store is built."""
    event = NOTICE_VERBS.get(verb)
    if event is None:
        return MilestoneResult(
            1, f"unknown delivery notice {verb!r}", RefusalReason.INVALID_REQUEST.value
        )
    enabled, _default_type = external_intake_settings(cfg)
    if not enabled:
        return _intake_off_call(MilestoneResult)
    outbox, milestones = milestone_settings(cfg)
    if not outbox.enabled:
        return MilestoneResult(
            1,
            "the outbox is off for this hive (intake.external.outbox.enabled; it is opt-in), so "
            "nothing can be drafted",
            RefusalReason.OUTBOX_DISABLED.value,
        )
    if not milestones.drafts(event.value):
        return MilestoneResult(
            1,
            f"{verb} notices are off for this hive "
            f"(intake.external.milestones.draft_on_{verb}; it is opt-in)",
            RefusalReason.INTAKE_DISABLED.value,
        )
    try:
        port = _milestone_port(
            cfg, entry, directory, lock, hive, f"work delivery {verb}", None, ids, sanitizer
        )
        outcome = port.delivery_notice(NoticeFact(work, event, reason, actor))
    except IntakeRefused as exc:
        return MilestoneResult(1, exc.detail, exc.reason.value)
    except IntakeStoreError as exc:
        return MilestoneResult(exc.returncode, milestone_message(exc), "store_error")
    except store_target.StoreResolutionError as exc:
        return MilestoneResult(1, str(exc), "store_unresolved")
    return MilestoneResult(0, outcome=outcome)


# -- the supervised outbox verbs (bh work outbox ...): operator seat, off by default -------------


@dataclass(frozen=True)
class OutboxCall:
    """The rendering input of one ``bh work outbox`` verb: ``code`` is the exit status, ``reason``
    the stable refusal code (empty on success, ``store_error`` for a store failure), ``error`` the
    one-line message naming the fix, ``value`` what the port returned (a result, a tuple of
    results, a listing or a rendered record)."""

    code: int
    error: str = ""
    reason: str = ""
    value: Any = None


class _UnwiredTracker:
    """Stands in for the tracker on the reads that make no tracker request (the outbox listing,
    the stored record, the stored thread): any use refuses with ``refusal()``, naming what to
    configure, so an unwired build can never reach a tracker."""

    def __init__(self, refusal: Callable[[], IntakeRefused] = _no_outbox_wiring) -> None:
        self._refusal = refusal

    def __getattr__(self, name: str) -> Any:
        if name.startswith("__"):
            raise AttributeError(name)
        raise self._refusal()


def _owned_label_prefixes(cfg: Mapping[str, Any], bound: Any) -> tuple[str, ...]:
    """``plugins.<id>.write.labels.owned_prefixes`` when the bound adapter's ``labels.change`` is
    enabled, else none (no label can be written). Configuration only: no plugin is imported."""
    from .bootstrap import tracker as tracker_root
    from .modules.tracker.contracts import Operation

    if Operation.LABELS_CHANGE not in bound.enabled:
        return ()
    node: Any = tracker_root.plugin_settings(bound.adapter_id, cfg.get("plugins")).values
    for part in tracker_root.LABEL_OWNERSHIP_KEY.split("."):
        node = node.get(part) if isinstance(node, Mapping) else None
    return tuple(str(prefix) for prefix in node or ())


def tracker_wiring(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    adapters: Any,
    *,
    requested: frozenset = frozenset(),
    needs_marker: bool = True,
) -> dict[str, Any]:
    """``tracker``, ``identity``, ``marker_line`` and ``owned_label_prefixes`` for ``entry``, all
    from the plugin binding of the hive's PUBLIC store (the ``tracker.adapter`` capability).

    ``adapters`` is the process's ``TrackerAdapters`` (:func:`process_adapters`). The tracker is
    the bound, narrowed port: a writer is present on it only when its write flag is on, and each
    write still needs an operator-approved single-use grant. The marker codec is the port's own
    optional ``MarkerCodec`` (so root never imports the plugin package); ``identity`` is the bound
    project (``owner/repo``) and this hive's :func:`install_id`. Any binding refusal (R6 to R13,
    including ``requested`` operations not enabled) is ``IntakeRefused(INTAKE_DISABLED)`` with
    the binding's message, which names the key to set. Building sends no tracker request."""
    from .bootstrap import tracker as tracker_root  # lazy: the plugin kernel loads only on use
    from .modules.tracker.contracts import MarkerCodec, Operation

    try:
        bound = adapters.for_store(entry, requested=frozenset(requested))
    except tracker_root.TrackerBindingError as exc:
        raise IntakeRefused(RefusalReason.INTAKE_DISABLED, exc.message) from exc
    port = bound.port
    codec = port.marker_line if isinstance(port, MarkerCodec) else None
    if needs_marker and codec is None:
        raise IntakeRefused(
            RefusalReason.INTAKE_DISABLED,
            f"tracker adapter {bound.adapter_id!r} provides no own-message marker codec, so the "
            "outbox cannot show the exact bytes a send would post; upgrade the plugin package",
        )
    return {
        "tracker": port,
        "identity": OwnIdentity(bound.project.locator, install_id(directory)),
        "marker_line": codec,
        "owned_label_prefixes": _owned_label_prefixes(cfg, bound),
        # the sender's close gate: the binding's own issue.close enable (no separate intake key)
        "close_enabled": Operation.ISSUE_CLOSE in bound.enabled,
        # likewise the issue.type / issue.fields gates (no separate intake keys either)
        "type_enabled": Operation.ISSUE_TYPE in bound.enabled,
        "fields_enabled": Operation.ISSUE_FIELDS in bound.enabled,
        # and the issue.reopen gate (bh-ih484)
        "reopen_enabled": Operation.ISSUE_REOPEN in bound.enabled,
    }


def outbox_wiring(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory=None,
    *,
    adapters: Any = None,
) -> dict[str, Any]:
    """The outbox's tracker wiring for ``entry`` (:func:`tracker_wiring`), built from the bound
    tracker plugin; this is the one place the production outbox wiring lands.

    Empty (``{}``) unless every switch is on: ``intake.external.enabled``,
    ``intake.external.outbox.enabled`` and ``plugins.tracker-github.enabled`` (through
    :func:`process_adapters`; all three off by default). Then a binding that refuses yields
    ``{"refusal": IntakeRefused}`` instead of raising, so a read that does not need the tracker
    (the stored record of ``outbox show <id>``) still works and can say why the live render is
    unavailable. Nothing here approves, mints a grant or posts: the sender's operator-only O5
    and O9 are unchanged. ``directory`` is the hive checkout (the install id's home); it
    defaults to ``registry.hive_dir(entry)``. Tests patch this seam, or pass the keywords to
    :func:`outbox_call`."""
    if not external_intake_settings(cfg)[0] or not milestone_settings(cfg)[0].enabled:
        return {}
    if adapters is None:
        adapters = process_adapters(cfg)
    if adapters is None:
        return {}
    home = directory if directory is not None else registry.hive_dir(entry)
    try:
        return tracker_wiring(cfg, entry, home, adapters)
    except IntakeRefused as exc:
        return {"refusal": exc}
    except IntakeStoreError as exc:  # the install id's private state
        return {"refusal": IntakeRefused(RefusalReason.INTAKE_DISABLED, outbox_message(exc))}


def outbox_message(exc: IntakeStoreError) -> str:
    """The one-line message for a store failure during an outbox verb."""
    return f"outbox could not finish {exc.bead or 'a report'} ({exc.operation}): {exc.detail}"


def _outbox_port(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    *,
    hive: str,
    lock: IntakeLock | None,
    tracker: Any,
    identity: OwnIdentity | None,
    marker_line: Callable[..., str] | None,
    ids: Any,
    sanitizer: Any,
    owned_label_prefixes: tuple[str, ...],
    needs_tracker: bool,
    refusal: IntakeRefused | None = None,
    close_enabled: bool = False,
    triage: Any = None,
    type_enabled: bool = False,
    fields_enabled: bool = False,
    reopen_enabled: bool = False,
):
    """The port an outbox verb runs on. Three switches, each a distinct refusal and each decided
    before any store write or tracker request: ``intake.external.enabled`` (``NullIntake``,
    ``intake_disabled``), ``intake.external.outbox.enabled`` (no sender bound,
    ``outbox_disabled``; the ``labels.change`` gate is ``intake.external.outbox.labels_enabled``,
    off by default and only effective with the outbox on, else ``labels_disabled``), then
    the tracker: a verb that needs it and is not handed ``tracker``, ``identity`` and
    ``marker_line`` refuses with ``refusal`` (the plugin binding's own message) or, when the
    tracker plugin is simply not switched on, naming the keys to set. With
    ``intake.external.comments.enabled`` and a wired tracker the same service also carries the
    thread operations."""
    enabled, _default_type = external_intake_settings(cfg)

    def build():
        policy, _milestones = milestone_settings(cfg)
        wired = tracker is not None and identity is not None and marker_line is not None
        if policy.enabled and not wired and needs_tracker:
            raise refusal if refusal is not None else _no_outbox_wiring()
        comments_on, thread_policy = comments_settings(cfg)
        return outbox_service_for(
            cfg,
            entry,
            directory,
            tracker=tracker if tracker is not None else _UnwiredTracker(),
            identity=identity if identity is not None else OwnIdentity("", ""),
            marker_line=marker_line if marker_line is not None else _unwired_marker,
            policy=policy,
            hive=hive,
            sanitizer=sanitizer,
            lock=lock,
            ids=ids,
            owned_label_prefixes=owned_label_prefixes,
            threads=thread_policy if comments_on and wired else None,
            close_enabled=close_enabled,
            triage=triage() if callable(triage) else triage,
            type_enabled=type_enabled,
            fields_enabled=fields_enabled,
            reopen_enabled=reopen_enabled,
        )

    return bind_intake(enabled, build)


def _unwired_marker(*_args: Any, **_kwargs: Any) -> str:
    raise _no_outbox_wiring()


def outbox_call(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    run: Callable[[Any], Any],
    *,
    hive: str = "",
    needs_tracker: bool = True,
    lock: IntakeLock | None = None,
    tracker: Any = None,
    identity: OwnIdentity | None = None,
    marker_line: Callable[..., str] | None = None,
    ids: Any = None,
    sanitizer: Any = None,
    owned_label_prefixes: tuple[str, ...] = (),
    wiring: Mapping[str, Any] | None = None,
    close_enabled: bool = False,
    triage: Any = None,
    type_enabled: bool = False,
    fields_enabled: bool = False,
    reopen_enabled: bool = False,
) -> OutboxCall:
    """Run ``run(port)`` on the outbox port of ``entry``'s hive and fold every refusal and store
    failure into an :class:`OutboxCall`. Off unless ``intake.external.enabled`` AND
    ``intake.external.outbox.enabled``; it never posts without the adapter's own enable and an
    operator approval minting a single-use grant.

    The tracker, identity and marker codec come from :func:`outbox_wiring` (the plugin binding)
    unless injected as keywords; ``wiring`` passes an already-built one. A verb that makes no
    tracker request (``needs_tracker=False``: the listing) builds no wiring at all."""
    if tracker is not None or not needs_tracker:
        wiring = {}
    elif wiring is None:
        with phase_timings.phase("wiring"):
            wiring = outbox_wiring(cfg, entry, directory)
    try:
        port = _outbox_port(
            cfg,
            entry,
            directory,
            hive=hive,
            lock=lock,
            tracker=tracker if tracker is not None else wiring.get("tracker"),
            identity=identity if identity is not None else wiring.get("identity"),
            marker_line=marker_line if marker_line is not None else wiring.get("marker_line"),
            ids=ids,
            sanitizer=sanitizer,
            owned_label_prefixes=owned_label_prefixes
            or tuple(wiring.get("owned_label_prefixes", ())),
            needs_tracker=needs_tracker,
            refusal=wiring.get("refusal"),
            close_enabled=close_enabled or wiring.get("close_enabled") is True,
            triage=triage,
            type_enabled=type_enabled or wiring.get("type_enabled") is True,
            fields_enabled=fields_enabled or wiring.get("fields_enabled") is True,
            reopen_enabled=reopen_enabled or wiring.get("reopen_enabled") is True,
        )
        value = run(port)
    except IntakeRefused as exc:
        return OutboxCall(1, exc.detail, exc.reason.value)
    except IntakeStoreError as exc:
        return OutboxCall(exc.returncode, outbox_message(exc), "store_error")
    except store_target.StoreResolutionError as exc:
        return OutboxCall(1, str(exc), "store_unresolved")
    return OutboxCall(0, value=value)


_OUTBOX_KINDS = {
    "comment": "comment.create",
    "comment.create": "comment.create",
    "labels": "labels.change",
    "labels.change": "labels.change",
    "close": "issue.close",
    "issue.close": "issue.close",
    "reopen": "issue.reopen",
    "issue.reopen": "issue.reopen",
    "type": "issue.type",
    "issue.type": "issue.type",
    "fields": "issue.fields",
    "issue.fields": "issue.fields",
}


def outbox_kind(text: str) -> Any:
    """The :class:`OpKind` an operator typed (``comment`` | ``labels`` | ``close`` | ``reopen`` |
    ``type`` | ``fields``, or the full operation name), or ``None`` when it names none. An empty
    string is the default, ``comment.create``."""
    from .modules.intake.domain import OpKind

    name = _OUTBOX_KINDS.get((text or "comment").strip().lower())
    return OpKind(name) if name else None


def _bad_kind(text: str) -> OutboxCall:
    return OutboxCall(
        2,
        f"unknown operation {text!r}: use comment, labels, close, reopen, type or fields",
        "bad_argument",
    )


def outbox_draft(cfg, entry, directory, report: str, text: str, actor: str, **kwargs) -> OutboxCall:
    """``bh work outbox draft``: O1 for an operator reply to ``report``'s issue. Never sends."""
    from .modules.intake.contracts import ReplyDraft

    return outbox_call(
        cfg,
        entry,
        directory,
        lambda port: port.draft_reply(ReplyDraft(report, text, actor)),
        **kwargs,
    )


@dataclass(frozen=True)
class StoredOutboxRecord:
    """``outbox show <id>`` without a wired tracker (``bh-fehna``): the record as stored, its
    message ``text`` (public facts filled in, without the marker line) and its operations' states.
    It carries no digest: only the live render (which needs the adapter's marker codec) shows the
    exact bytes and the digest an approval must name. ``note`` says why there is no live render."""

    report: str
    record: Any
    text: str
    note: str


def _stored_record(
    outbox_id: str, report: str, note: str, index: Any = None
) -> Callable[[Any], StoredOutboxRecord]:
    """The stored record of ``outbox_id``: ONE report's listing when ``report`` is given or the
    outbox index names it (``bh-pc9rf``), else (or when that report does not hold it) every
    report's, which also rebuilds the index."""
    from .modules.intake.contracts import OutboxQuery
    from .modules.intake.domain import is_outbox_id
    from .modules.intake.domain.outbox import render_text

    def run(port) -> StoredOutboxRecord:
        if not is_outbox_id(outbox_id):
            raise IntakeRefused(
                RefusalReason.INVALID_OUTBOX_ID, f"{outbox_id!r} is not an outbox id (ob1_...)"
            )
        hint = report or (index.report_of(outbox_id) if index is not None else None) or ""
        scopes = [hint, ""] if hint and not report else [report]
        for scope in scopes:
            for found in port.outbox(OutboxQuery(scope, False)).entries:
                if found.record.outbox_id == outbox_id:
                    text = render_text(found.record.message, found.record.facts)
                    return StoredOutboxRecord(found.report, found.record, text, note)
        raise IntakeRefused(RefusalReason.UNKNOWN_OUTBOX, f"no outbox record {outbox_id}")

    return run


def outbox_show(
    cfg,
    entry,
    directory,
    outbox_id: str = "",
    report: str = "",
    open_only: bool = False,
    **kwargs,
) -> OutboxCall:
    """``bh work outbox show [<id>]``: one record, or without an id the listing. Neither makes a
    tracker request.

    With a wired tracker (:func:`outbox_wiring`, or an injected one) ``<id>`` is the dry-run
    render: every operation exactly as it would be posted, with the digest the operator types.
    Without one (the plugin off, or its binding refused) it is the stored record instead
    (:class:`StoredOutboxRecord`: the draft text, the event and every operation's state, no
    digest), so a milestone draft can be inspected before any credential is wired."""
    from .modules.intake.contracts import OutboxQuery

    if not outbox_id:
        return outbox_call(
            cfg,
            entry,
            directory,
            lambda port: port.outbox(OutboxQuery(report, open_only)),
            needs_tracker=False,
            **kwargs,
        )
    switched_on = external_intake_settings(cfg)[0] and milestone_settings(cfg)[0].enabled
    if kwargs.get("tracker") is not None or not switched_on:  # a switch off refuses as before
        return outbox_call(cfg, entry, directory, lambda port: port.render(outbox_id), **kwargs)
    wiring = outbox_wiring(cfg, entry, directory)
    if wiring.get("tracker") is not None:
        return outbox_call(
            cfg, entry, directory, lambda port: port.render(outbox_id), wiring=wiring, **kwargs
        )
    refusal = wiring.get("refusal") or _no_outbox_wiring()
    note = f"no live render or digest (approving needs one): {refusal.detail}"
    return outbox_call(
        cfg,
        entry,
        directory,
        _stored_record(outbox_id, report, note, outbox_index(directory)),
        needs_tracker=False,
        **kwargs,
    )


def outbox_approve(
    cfg,
    entry,
    directory,
    outbox_id: str,
    ops: Sequence[str],
    publish: Sequence[str],
    actor: str,
    **kwargs,
) -> OutboxCall:
    """``bh work outbox approve <id> --op <kind>:<digest> [--op ...] [--publish <finding>]``.

    Each ``--op`` is its own approval and mints its own single-use grant (approving the comment
    never approves the labels or the close). ``--publish`` names a finding to publish deliberately
    and is only accepted with exactly one ``--op``, so the finding belongs to that operation. An
    approval already made stays made when a later one is refused: the result carries those so
    far."""
    from .modules.intake.contracts import OutboxApprove

    if not ops:
        return OutboxCall(
            2, "pass at least one --op <comment|labels|close|type|fields>:<digest>", "bad_argument"
        )
    if publish and len(ops) != 1:
        return OutboxCall(2, "--publish needs exactly one --op", "bad_argument")
    requests = []
    for op in ops:
        kind_text, _, digest = op.partition(":")
        kind = outbox_kind(kind_text)
        if kind is None:
            return _bad_kind(kind_text)
        if not digest:
            return OutboxCall(
                2, f"--op {op!r} has no digest: use {kind_text}:<digest>", "bad_argument"
            )
        requests.append(OutboxApprove(outbox_id, digest, actor, tuple(publish), kind))

    done: list[Any] = []

    def run(port):
        for request in requests:
            done.append(port.approve(request))
        return tuple(done)

    call = outbox_call(cfg, entry, directory, run, **kwargs)
    if call.code and done:  # an earlier approval stays made: report it with the refusal
        return OutboxCall(call.code, call.error, call.reason, tuple(done))
    return call


def outbox_send(
    cfg, entry, directory, outbox_id: str, kind: str, actor: str, **kwargs
) -> OutboxCall:
    """``bh work outbox send <id>``: O9 to O13 for one approved operation (the only verb that can
    reach a tracker, and only with the adapter's own enable and an unspent grant)."""
    op = outbox_kind(kind)
    if op is None:
        return _bad_kind(kind)
    return outbox_call(
        cfg, entry, directory, lambda port: port.send(outbox_id, actor=actor, kind=op), **kwargs
    )


def outbox_send_approved(
    cfg, entry, directory, kind: str, reports: Sequence[str], actor: str, **kwargs
) -> OutboxCall:
    """``bh work outbox send-approved [--kind K] [--bead <id>]...``: every approved operation in
    ONE process and one store session (``bh-pc9rf``), ordered per report reopen, comment, labels,
    close; each still spends its own single-use grant and writes its own receipt. ``kind`` empty
    means every kind. The value is a :class:`~beadhive.modules.intake.domain.BatchSendOutcome`; a
    refused or unsettled item is reported there and the rest still go."""
    from .modules.intake.domain import BatchSend

    kinds: tuple[Any, ...] = ()
    if kind:
        op = outbox_kind(kind)
        if op is None:
            return _bad_kind(kind)
        kinds = (op,)
    request = BatchSend(actor, kinds, tuple(dict.fromkeys(r for r in reports if r)))
    return outbox_call(cfg, entry, directory, lambda port: port.send_approved(request), **kwargs)


def outbox_backfill_close(
    cfg,
    entry,
    directory,
    actor: str,
    dry_run: bool = False,
    *,
    branch: str = "main",
    repo: Any = None,
    landed: Any = None,
    **kwargs,
) -> OutboxCall:
    """``bh work outbox backfill-close [--dry-run]``: draft the ``issue.close`` of every report
    already closed in the store whose bound issue is still open and whose applied triage outcome
    ends the thread, on its latest reply; a report that never got its reply gets one drafted too,
    from the reviewed ``reporter_text`` or the template apply uses (the fix version computed over
    ``branch`` through :func:`triage_binding`). One issue listing; drafts only, never approves or
    sends. Off unless the outbox is on and the tracker plugin's ``write.issue_close.enabled``."""
    from .modules.intake.contracts import CloseBackfill

    def binding():  # built only once the switches let the port be bound
        return triage_binding(cfg, entry, directory, branch=branch, repo=repo, landed=landed)

    return outbox_call(
        cfg,
        entry,
        directory,
        lambda port: port.backfill_closes(CloseBackfill(actor, dry_run=dry_run)),
        triage=binding,
        **kwargs,
    )


def outbox_reopen(
    cfg,
    entry,
    directory,
    report: str,
    actor: str,
    text: str = "",
    dry_run: bool = False,
    **kwargs,
) -> OutboxCall:
    """``bh work outbox reopen <report> [--text T] [--dry-run]``: draft the reopen of ONE report
    whose bound issue reads closed, on the reply that explains it (``text``, or the template), and
    never approve or send it (``bh-ih484``). ``dry_run`` makes every check and writes nothing. Off
    unless the outbox is on and the tracker plugin's ``write.issue_reopen.enabled``."""
    from .modules.intake.contracts import ReopenRequest

    return outbox_call(
        cfg,
        entry,
        directory,
        lambda port: port.reopen(ReopenRequest(report, actor, text, dry_run=dry_run)),
        **kwargs,
    )


def outbox_reconcile(cfg, entry, directory, outbox_id: str, actor: str, **kwargs) -> OutboxCall:
    """``bh work outbox reconcile [<id>]``: read the comment listing (or labels) back and settle
    an unknown outcome by its marker; never sends."""
    return outbox_call(
        cfg,
        entry,
        directory,
        lambda port: port.reconcile(outbox_id or None, actor=actor),
        **kwargs,
    )


def outbox_discard(
    cfg,
    entry,
    directory,
    outbox_id: str,
    reason: str,
    accept_unknown: bool,
    kind: str,
    actor: str,
    **kwargs,
) -> OutboxCall:
    """``bh work outbox discard <id> --reason ...``: abandon an unsent operation (O8), or an
    unknown one when ``accept_unknown`` (O18). ``kind`` empty means every operation."""
    op = None
    if kind:
        op = outbox_kind(kind)
        if op is None:
            return _bad_kind(kind)
    return outbox_call(
        cfg,
        entry,
        directory,
        lambda port: port.discard(
            outbox_id, reason=reason, actor=actor, accept_unknown=accept_unknown, kind=op
        ),
        **kwargs,
    )


def outbox_retry(
    cfg, entry, directory, outbox_id: str, kind: str, actor: str, **kwargs
) -> OutboxCall:
    """``bh work outbox retry <id>``: O17, a failed operation back to drafted (a new render, a new
    digest and a new approval are needed before it can be sent again)."""
    op = outbox_kind(kind)
    if op is None:
        return _bad_kind(kind)
    return outbox_call(
        cfg, entry, directory, lambda port: port.retry(outbox_id, actor=actor, kind=op), **kwargs
    )


# -- the conversation (bh work thread ...): comment ingestion, off by default -------------------


def _comments_off() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED,
        "comment ingestion is off for this hive (intake.external.comments.enabled; it is opt-in)",
    )


def thread_service_for(
    store,
    tracker,
    lock,
    *,
    posture: StorePosture,
    identity: OwnIdentity | None,
    policy: ThreadPolicy | None = None,
) -> ThreadService:
    """The :class:`ThreadService` over ``store`` and the bound ``tracker``: own-message
    recognition reads ``identity`` (the marker's hive and install), the tracker's ``whoami`` and
    the store-backed outbox (:class:`StoreOutboxView`: receipts, pending and issued records), so
    a comment this install posted is ``own`` and never new evidence. ``posture`` is required, so
    an unwired service fails closed on a published operational store."""
    return ThreadService(
        store,
        tracker,
        lock,
        posture=posture,
        identity=identity,
        outbox=StoreOutboxView(store),
        policy=policy,
    )


def thread_wiring(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory=None,
    *,
    adapters: Any = None,
) -> dict[str, Any]:
    """The thread service's tracker wiring (``tracker``, ``identity``), from the same plugin
    binding as the outbox, requesting ``comments.read``. Empty unless ``intake.external.enabled``,
    ``intake.external.comments.enabled`` and ``plugins.tracker-github.enabled`` are all on; a
    binding that refuses (``read.comments`` not enabled, R6 to R13) is ``{"refusal": ...}``. No
    marker codec is needed to read. Tests patch this seam, or pass the keywords to
    :func:`thread_call`."""
    from .modules.tracker.contracts import Operation

    if not external_intake_settings(cfg)[0] or not comments_settings(cfg)[0]:
        return {}
    if adapters is None:
        adapters = process_adapters(cfg)
    if adapters is None:
        return {}
    home = directory if directory is not None else registry.hive_dir(entry)
    try:
        return tracker_wiring(
            cfg,
            entry,
            home,
            adapters,
            requested=frozenset({Operation.COMMENTS_READ}),
            needs_marker=False,
        )
    except IntakeRefused as exc:
        return {"refusal": exc}
    except IntakeStoreError as exc:  # the install id's private state
        return {"refusal": IntakeRefused(RefusalReason.INTAKE_DISABLED, thread_message(exc))}


@dataclass(frozen=True)
class ThreadCall:
    """The rendering input of one ``bh work thread`` verb: ``code`` is the exit status, ``reason``
    the stable refusal code (empty on success, ``store_error`` for a store failure), ``error`` the
    one-line message naming the fix, ``value`` what the port returned (a ``ThreadView`` or
    ``None`` for ``show``; a tuple of ``ThreadOutcome`` for ``ingest``; one for
    ``considered``)."""

    code: int
    error: str = ""
    reason: str = ""
    value: Any = None
    #: ``show`` only: the report's links (``tracks`` and the beads that investigate it, bh-u3a53).
    links: ReportRelations | None = None


def thread_message(exc: IntakeStoreError) -> str:
    """The one-line message for a store failure during a thread verb."""
    return f"thread could not finish {exc.bead or 'a report'} ({exc.operation}): {exc.detail}"


def thread_call(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    run: Callable[[Any], Any],
    *,
    hive: str = "",
    verb: str = "work thread",
    needs_tracker: bool = True,
    lock: IntakeLock | None = None,
    tracker: Any = None,
    identity: OwnIdentity | None = None,
) -> ThreadCall:
    """Run ``run(port)`` on the thread port of ``entry``'s hive and fold every refusal and store
    failure into a :class:`ThreadCall`.

    Three switches, each a distinct refusal decided before any store write or tracker request:
    ``intake.external.enabled`` (``intake_disabled``, "opt-in"),
    ``intake.external.comments.enabled`` (``intake_disabled``, naming the key), then, for a verb
    that reads the tracker, the plugin binding with ``comments.read`` (:func:`thread_wiring`; the
    binding's own message, or the keys to set). ``show`` and ``considered`` make no tracker
    request, so they run without the tracker. Nothing here writes to a tracker: ingestion reads
    comments and comment ids only."""
    if not external_intake_settings(cfg)[0]:
        return ThreadCall(
            1,
            "external intake is off for this hive (intake.external.enabled; it is opt-in)",
            RefusalReason.INTAKE_DISABLED.value,
        )
    comments_on, policy = comments_settings(cfg)
    if not comments_on:
        refused = _comments_off()
        return ThreadCall(1, refused.detail, refused.reason.value)
    wiring: Mapping[str, Any] = {}
    if tracker is None and needs_tracker:
        wiring = thread_wiring(cfg, entry, directory)
    port_tracker = tracker if tracker is not None else wiring.get("tracker")
    try:
        if needs_tracker and port_tracker is None:
            raise wiring.get("refusal") or _no_thread_wiring()
        store = report_store(directory, lambda: store_scope(cfg, entry))
        held = lock if lock is not None else intake_lock(cfg, directory, hive=hive, verb=verb)
        service = thread_service_for(
            store,
            port_tracker if port_tracker is not None else _UnwiredTracker(_no_thread_wiring),
            held,
            posture=store_posture(entry),
            identity=identity if identity is not None else wiring.get("identity"),
            policy=policy,
        )
        refresh = transition_refresh(cfg, entry, directory, store, held)
        port = IntakeService(
            store, ValidateLabelPolicy(cfg, str(entry["prefix"])), threads=service, **refresh
        )
        value = run(port)
    except IntakeRefused as exc:
        return ThreadCall(1, exc.detail, exc.reason.value)
    except IntakeStoreError as exc:
        return ThreadCall(exc.returncode, thread_message(exc), "store_error")
    except store_target.StoreResolutionError as exc:
        return ThreadCall(1, str(exc), "store_unresolved")
    return ThreadCall(0, value=value)


def thread_show(cfg, entry, directory, report: str, **kwargs) -> ThreadCall:
    """``bh work thread show <report>``: the stored thread with its derived reads (``seq``, new
    evidence, flagged), or ``None`` when nothing was ingested yet, plus the report's links (the
    work it tracks and the beads that investigate it). No tracker request, no lock."""

    def run(port):
        view = port.thread_of(report)
        try:
            links = port.relations(report)
        except IntakeRefused as exc:  # an unknown report still reads "no thread", as before
            if exc.reason is not RefusalReason.NOT_FOUND:
                raise
            links = None
        return view, links

    call = thread_call(cfg, entry, directory, run, needs_tracker=False, **kwargs)
    if call.code:
        return call
    view, links = call.value
    return ThreadCall(0, value=view, links=links)


def thread_ingest(cfg, entry, directory, report: str, actor: str, **kwargs) -> ThreadCall:
    """``bh work thread ingest <report>``: ingest the report's comments since its checkpoint, then
    reconcile deletions from one complete comment-id snapshot. Reads only; each step is at most
    one ``intake.thread`` write under the binding's lock."""

    def run(port):
        ingested = port.ingest_thread(ThreadRequest(report, actor))
        reconciled = port.reconcile_thread(ThreadRequest(report, actor))
        return (ingested, reconciled)

    return thread_call(cfg, entry, directory, run, verb="work thread ingest", **kwargs)


def thread_considered(
    cfg, entry, directory, report: str, through: int, actor: str, **kwargs
) -> ThreadCall:
    """``bh work thread considered <report> --through <seq>``: record that a triage pass read the
    thread as it stood at ``through`` (the ``seq`` ``thread show`` printed), so a comment that
    arrived after the read stays new evidence. Idempotent; never touches the ``intake``
    dimension and is never a side effect of a disposition. No tracker request."""
    if through < 0:
        return ThreadCall(
            2, f"--through must be a thread seq (0 or more), not {through}", "bad_argument"
        )
    return thread_call(
        cfg,
        entry,
        directory,
        lambda port: port.mark_considered(ConsiderRequest(report, actor, through)),
        verb="work thread considered",
        needs_tracker=False,
        **kwargs,
    )


# -- waiting for the reporter (bh-61xkc): needs-info and wake-on-reply, off by default -----------


def _intake_off_call(make: Callable[..., Any]) -> Any:
    return make(
        1,
        "external intake is off for this hive (intake.external.enabled; it is opt-in)",
        RefusalReason.INTAKE_DISABLED.value,
    )


def _needs_info_off(make: Callable[..., Any], key: str = "enabled") -> Any:
    what = "waking every waiting report" if key == "sweep" else "waiting for the reporter"
    return make(
        1,
        f"{what} is off for this hive (intake.external.needs_info.{key}; it is opt-in)",
        RefusalReason.INTAKE_DISABLED.value,
    )


def needs_info(cfg, entry, directory, report: str, text: str, actor: str, **kwargs) -> OutboxCall:
    """``bh work needs-info <report> --ask <text>``: draft the question as a supervised reply to
    the report's issue and move the report to ``intake:needs-info``. Nothing is sent: the reply
    posts only after an operator approves and sends it (``bh work outbox``). Off unless
    ``intake.external.enabled`` and ``intake.external.needs_info.enabled``; it also needs the
    outbox (``outbox.enabled``, the tracker wiring for the dry-run bytes) and comment ingestion
    (``comments.enabled``), each refused naming its key before anything is written."""
    if not external_intake_settings(cfg)[0]:
        return _intake_off_call(OutboxCall)
    if not needs_info_settings(cfg)[0]:
        return _needs_info_off(OutboxCall)
    return outbox_call(
        cfg,
        entry,
        directory,
        lambda port: port.needs_info(NeedsInfoRequest(report, text, actor)),
        **kwargs,
    )


def wake(
    cfg, entry, directory, report: str, actor: str, *, all_: bool = False, **kwargs
) -> ThreadCall:
    """``bh work wake <report>`` (or ``--all``): move a report waiting on ``intake:needs-info``
    back to ``intake:untriaged`` when its stored thread holds a reporter's reply; the reply stays
    new evidence for triage. Reads the stored thread only (no tracker request; ``thread ingest``
    refreshes it). Off unless ``intake.external.enabled``, ``intake.external.needs_info.enabled``
    and ``intake.external.comments.enabled``; ``--all`` also needs ``needs_info.sweep``."""
    if not external_intake_settings(cfg)[0]:
        return _intake_off_call(ThreadCall)
    enabled, sweep = needs_info_settings(cfg)
    if not enabled:
        return _needs_info_off(ThreadCall)
    if all_ and not sweep:
        return _needs_info_off(ThreadCall, "sweep")
    if all_:

        def run(port):
            return port.wake_all(actor=actor)

    else:

        def run(port):
            return port.wake(WakeRequest(report, actor))

    return thread_call(cfg, entry, directory, run, verb="work wake", needs_tracker=False, **kwargs)


# -- the label projection and review-gated triage (bh-hrx2r): bh work labels / bh work triage ---


@dataclass(frozen=True)
class LabelsConfig:
    """``intake.external.labels`` as the root reads it. ``enabled`` and ``on_transition`` are only
    a literal ``true`` (``on_transition`` only acts with ``enabled``); ``settings`` is the module's
    ``LabelSettings`` (``None`` with ``error`` when the section has a shape the module refuses)."""

    enabled: bool = False
    on_transition: bool = False
    settings: Any = None
    error: str = ""
    #: the issue type / priority field maps (``ValueSettings``; ``None`` with ``error``)
    values: Any = None


#: The keys of ``intake.external.labels`` that need it on, named in every refusal.
LABELS_ENABLED_KEY = "intake.external.labels.enabled"


def labels_config(cfg: Mapping[str, Any]) -> LabelsConfig:
    """``intake.external.labels`` translated into the module's ``LabelSettings`` (the prefix, the
    projected dimensions, aliases, colors and per-value overrides). Host-owned and off: only a
    literal ``true`` enables the projection or the transition refresh."""
    from .modules.intake.domain import (
        DEFAULT_ISSUE_TYPES,
        DEFAULT_PREFIX,
        DEFAULT_PRIORITIES,
        DEFAULT_PRIORITY_FIELD,
        DEFAULT_PROJECTED,
        LabelOverride,
        LabelSettings,
        ValueSettings,
    )

    section = (((cfg.get("intake") or {}).get("external")) or {}).get("labels") or {}
    enabled = section.get("enabled") is True
    on_transition = enabled and section.get("on_transition") is True
    try:
        overrides = {
            str(key): LabelOverride(
                name=str((value or {}).get("name") or ""),
                description=str((value or {}).get("description") or ""),
                color=str((value or {}).get("color") or "").lower(),
            )
            for key, value in (section.get("overrides") or {}).items()
        }
        settings = LabelSettings(
            prefix=str(section.get("prefix") or DEFAULT_PREFIX),
            project=tuple(
                str(d) for d in (section["project"] if "project" in section else DEFAULT_PROJECTED)
            ),
            aliases={str(k): str(v) for k, v in (section.get("aliases") or {}).items()},
            colors={str(k): str(v).lower() for k, v in (section.get("colors") or {}).items()},
            overrides=overrides,
        )
        kinds = section.get("issue_type") or {}
        priority = section.get("priority_field") or {}
        values = ValueSettings(
            issue_types={
                str(k): str(v)
                for k, v in (kinds["map"] if "map" in kinds else DEFAULT_ISSUE_TYPES).items()
            },
            priority_field=str(priority.get("name") or DEFAULT_PRIORITY_FIELD),
            priorities={
                str(k): str(v)
                for k, v in (priority["map"] if "map" in priority else DEFAULT_PRIORITIES).items()
            },
        )
    except (AttributeError, TypeError, ValueError) as exc:
        return LabelsConfig(enabled, on_transition, None, f"intake.external.labels: {exc}")
    return LabelsConfig(enabled, on_transition, settings, values=values)


def registry_dimensions(cfg: Mapping[str, Any]) -> dict[str, Any]:
    """The dimension registry (``dimensions.*``) as the module's ``RegistryDimension`` records:
    each dimension's description and, when it declares ``values``, its closed set (``values:
    []`` is a locked one). The built-in intake, delivery, type, priority and progress vocabulary
    is the module's own; the registry only adds to it."""
    from .modules.intake.domain import RegistryDimension

    out: dict[str, Any] = {}
    for name, spec in (cfg.get("dimensions") or {}).items():
        node = spec if isinstance(spec, Mapping) else {}
        values = node.get("values")
        out[str(name)] = RegistryDimension(
            str(name),
            str(node.get("description") or ""),
            None if values is None else frozenset(str(v) for v in values),
        )
    return out


#: ``<hive>/.bh/intake/audit.log``: the local audit trail of approved repository-level writes.
AUDIT_LOG_FILE = "audit.log"


def audit_path(directory) -> Path:
    """``<hive>/.bh/intake/audit.log``, in the hive's private, never-published state."""
    return _intake_state_dir(directory, "intake audit log", "audit") / AUDIT_LOG_FILE


class FileAuditLog:
    """The root :class:`~beadhive.modules.intake.contracts.AuditLog`: one JSON line per note
    (``at``, ``actor``, ``note``), appended to a private file (mode 0600) that never reaches a
    tracker. ``clock`` stamps each line (UTC); tests inject one."""

    def __init__(
        self,
        path: Path | Callable[[], Path],
        *,
        clock: Callable[[], Any] | None = None,
    ) -> None:
        self._path = path
        self._clock = clock

    def note(self, text: str, *, actor: str) -> None:
        import datetime as _dt
        import json as _json

        path = self._path() if callable(self._path) else self._path
        path.parent.mkdir(parents=True, exist_ok=True)
        moment = self._clock() if self._clock is not None else _dt.datetime.now(_dt.UTC)
        line = _json.dumps({"at": moment.isoformat(), "actor": actor, "note": text}, sort_keys=True)
        fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
        with os.fdopen(fd, "a", encoding="utf-8") as handle:
            handle.write(line + "\n")


class LinkageLandedCommits:
    """The root :class:`~beadhive.modules.intake.contracts.LandedCommits`: a work bead's
    ``git.commits`` linkage (``git_linkage.read_commits``, recorded at submit and merge), oldest
    first. Unreadable or absent linkage is ``()``: a ``fixed`` claim then needs its own
    ``fix_commit``. ``read`` is the injected reader (tests pass one)."""

    def __init__(self, directory, *, read: Callable[[str], Sequence[str]] | None = None) -> None:
        self._directory = Path(directory)
        self._read = read

    def landed_commits(self, bead: str) -> tuple[str, ...]:
        if self._read is not None:
            return tuple(self._read(bead))
        from . import git_linkage

        return tuple(git_linkage.read_commits(bead, self._directory))


def _labels_off() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED,
        f"the label projection is off for this hive ({LABELS_ENABLED_KEY}; it is opt-in)",
    )


def _no_labels_wiring() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED,
        f"the label projection has no tracker wired: {TRACKER_CONFIG_HINT}, and set "
        f"plugins.{TRACKER_PLUGIN_ID}.read.labels.enabled to true, then run the verb again",
    )


def labels_wiring(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory=None,
    *,
    requested: frozenset = frozenset(),
    adapters: Any = None,
) -> dict[str, Any]:
    """The label projection's tracker wiring (``tracker`` and the bound label prefixes), from the
    same plugin binding as the outbox, requesting ``requested`` (``issues.read`` and
    ``labels.read`` for a sync, plus ``labels.define`` for an approved define). Empty unless
    ``intake.external.enabled``, :data:`LABELS_ENABLED_KEY` and ``plugins.tracker-github.enabled``
    are all on; a binding that refuses (an operation not enabled, R6 to R13) is
    ``{"refusal": ...}``. Building sends no tracker request."""
    if not external_intake_settings(cfg)[0] or not labels_config(cfg).enabled:
        return {}
    if adapters is None:
        adapters = process_adapters(cfg)
    if adapters is None:
        return {}
    home = directory if directory is not None else registry.hive_dir(entry)
    try:
        return tracker_wiring(cfg, entry, home, adapters, requested=requested, needs_marker=False)
    except IntakeRefused as exc:
        return {"refusal": exc}
    except IntakeStoreError as exc:  # the install id's private state
        return {"refusal": IntakeRefused(RefusalReason.INTAKE_DISABLED, outbox_message(exc))}


def label_projection_for(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    store,
    lock: IntakeLock,
    *,
    tracker: Any,
    ids: Any = None,
    sanitizer: Any = None,
    audit: Any = None,
    owned_label_prefixes: tuple[str, ...] = (),
    gates: tuple[bool, bool] | None = None,
):
    """The ``LabelProjection`` over ``store`` and ``tracker`` (an ``_UnwiredTracker`` for the
    tracker-free transition refresh), or ``None`` unless :data:`LABELS_ENABLED_KEY` is on. Its
    leak policy owns the projection's prefix (plus ``owned_label_prefixes``, the plugin's), its
    outbox policy is the hive's (drafting needs ``outbox.enabled`` and ``outbox.labels_enabled``)
    and its audit trail is :class:`FileAuditLog` in the hive's private state. ``gates`` are the
    bound adapter's ``issue.type`` / ``issue.fields`` enables (``None``: read from config,
    :func:`value_gates`): with one on, the projection also drafts that value from the maps.
    Raises ``IntakeRefused`` for a labels section the module refuses."""
    from .adapters.intake import ScrubSanitizer
    from .modules.intake.application import LabelProjection

    setting = labels_config(cfg)
    if not setting.enabled:
        return None
    if setting.settings is None:
        raise IntakeRefused(RefusalReason.INVALID_REQUEST, setting.error)
    owned = tuple(dict.fromkeys((*owned_label_prefixes, setting.settings.prefix)))
    type_on, fields_on = gates if gates is not None else value_gates(cfg, entry)
    return LabelProjection(
        store,
        tracker,
        lock,
        ids=ids if ids is not None else RandomIdSource(),
        registry=registry_dimensions(cfg),
        settings=setting.settings,
        leak_policy=leak_policy(cfg, owned_label_prefixes=owned),
        sanitizer=sanitizer if sanitizer is not None else ScrubSanitizer(),
        posture=store_posture(entry),
        outbox=with_value_gates(milestone_settings(cfg)[0], type_on, fields_on),
        audit=audit if audit is not None else FileAuditLog(lambda: audit_path(directory)),
        values=setting.values,
    )


def transition_refresh(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any] | None,
    directory,
    store,
    lock: IntakeLock,
    *,
    ids: Any = None,
    sanitizer: Any = None,
    gates: tuple[bool, bool] | None = None,
) -> dict[str, Any]:
    """The ``IntakeService`` keywords of the opt-in transition hook (bh-gjb5w): a TRACKER-FREE
    projection and ``refresh_on_transition``, so a disposition, needs-info, wake, link, unlink or
    milestone drafts or refreshes the report's ONE ``labels`` record from stored state. Empty
    (the hook is off, the default) unless ``intake.external.labels.enabled`` AND
    ``labels.on_transition``; a refresh never fails the transition. ``gates`` are the
    ``issue.type`` / ``issue.fields`` gates (``None``: read from config, :func:`value_gates`)."""
    setting = labels_config(cfg)
    if entry is None or not setting.on_transition or setting.settings is None:
        return {}
    projection = label_projection_for(
        cfg,
        entry,
        directory,
        store,
        lock,
        tracker=_UnwiredTracker(_no_labels_wiring),
        ids=ids,
        sanitizer=sanitizer,
        gates=gates,
    )
    return {"projection": projection, "refresh_on_transition": True}


@dataclass(frozen=True)
class IntakeCall:
    """The rendering input of one ``bh work labels`` / ``bh work triage`` verb: ``code`` is the
    exit status, ``reason`` the stable refusal code (empty on success, ``store_error`` for a store
    failure), ``error`` the one-line message naming the fix, ``value`` what the port returned."""

    code: int
    error: str = ""
    reason: str = ""
    value: Any = None


def intake_message(exc: IntakeStoreError) -> str:
    """The one-line message for a store failure during a labels or triage verb."""
    return f"intake could not finish {exc.bead or 'a report'} ({exc.operation}): {exc.detail}"


def _intake_call(run: Callable[[], Any]) -> IntakeCall:
    try:
        value = run()
    except IntakeRefused as exc:
        return IntakeCall(1, exc.detail, exc.reason.value)
    except IntakeStoreError as exc:
        return IntakeCall(exc.returncode, intake_message(exc), "store_error")
    except store_target.StoreResolutionError as exc:
        return IntakeCall(1, str(exc), "store_unresolved")
    return IntakeCall(0, value=value)


@dataclass(frozen=True)
class _Composed:
    service: IntakeService
    store: Any


def _compose(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    *,
    hive: str,
    verb: str,
    lock: IntakeLock | None = None,
    ids: Any = None,
    sanitizer: Any = None,
    audit: Any = None,
    outbox: Mapping[str, Any] | None = None,
    threads: Mapping[str, Any] | None = None,
    labels_tracker: Any = None,
    owned_label_prefixes: tuple[str, ...] = (),
    triage: Any = None,
    review: Any = None,
    gates: tuple[bool, bool] | None = None,
) -> _Composed:
    """One ``IntakeService`` over the hive's OPERATIONAL store with what a labels or triage verb
    needs bound: the supervised sender (and the milestone service) over ``outbox`` (an outbox
    wiring; only while ``outbox.enabled``), the thread service over ``threads`` (a thread wiring),
    the label projection over ``labels_tracker`` (only while :data:`LABELS_ENABLED_KEY`; else the
    transition hook's tracker-free one when ``labels.on_transition``), the ``TriageBinding`` and
    the ``ReviewPolicy``. Nothing here reads a tracker or writes a store."""
    from .adapters.intake import ScrubSanitizer

    store = report_store(directory, lambda: store_scope(cfg, entry), entry=entry)
    labels = ValidateLabelPolicy(cfg, str(entry["prefix"]))
    held = lock if lock is not None else intake_lock(cfg, directory, hive=hive, verb=verb)
    source = ids if ids is not None else RandomIdSource()
    scrubber = sanitizer if sanitizer is not None else ScrubSanitizer()
    policy, milestone_policy = milestone_settings(cfg)
    posture = store_posture(entry)
    owned = tuple((outbox or {}).get("owned_label_prefixes", ()))
    sender = milestones = conversation = None
    if outbox is not None and policy.enabled:
        leaks = leak_policy(cfg, owned_label_prefixes=owned)
        milestones = MilestoneService(
            store,
            held,
            ids=source,
            leak_policy=leaks,
            sanitizer=scrubber,
            posture=posture,
            outbox=policy,
            milestones=milestone_policy,
        )
        sender = OutboxService(
            store,
            outbox["tracker"],
            held,
            ids=source,
            identity=outbox["identity"],
            marker_line=outbox["marker_line"],
            policy=with_reopen(
                with_value_gates(
                    with_close(policy, outbox.get("close_enabled")),
                    outbox.get("type_enabled"),
                    outbox.get("fields_enabled"),
                ),
                outbox.get("reopen_enabled"),
            ),
            leak_policy=leaks,
            sanitizer=scrubber,
            posture=posture,
            holder=f"{socket.gethostname()}:{os.getpid()}",
            completion=milestones.complete,
        )
    if threads is not None:
        conversation = thread_service_for(
            store,
            threads["tracker"],
            held,
            posture=posture,
            identity=threads.get("identity"),
            policy=comments_settings(cfg)[1],
        )
    keywords: dict[str, Any] = {}
    if labels_tracker is not None:
        projection = label_projection_for(
            cfg,
            entry,
            directory,
            store,
            held,
            tracker=labels_tracker,
            ids=source,
            sanitizer=scrubber,
            audit=audit,
            owned_label_prefixes=(*owned, *owned_label_prefixes),
            gates=gates,
        )
        if projection is not None:
            keywords = {
                "projection": projection,
                "refresh_on_transition": labels_config(cfg).on_transition,
            }
    else:
        keywords = transition_refresh(
            cfg,
            entry,
            directory,
            store,
            held,
            ids=source,
            sanitizer=scrubber,
            gates=(
                None
                if outbox is None
                else (outbox.get("type_enabled") is True, outbox.get("fields_enabled") is True)
            ),
        )
    service = IntakeService(
        store,
        labels,
        outbox=sender,
        milestones=milestones,
        threads=conversation,
        catch_up_on_link=catch_up_on_link(cfg) and milestones is not None,
        lock=held,
        triage=triage,
        review=review,
        **keywords,
    )
    return _Composed(service, store)


def _require_intake() -> None:
    raise IntakeRefused(
        RefusalReason.INTAKE_DISABLED,
        "external intake is off for this hive (intake.external.enabled; it is opt-in)",
    )


def _require_labels(cfg: Mapping[str, Any]) -> None:
    """Refuse, naming the key, unless intake and the label projection are on and valid."""
    if not external_intake_settings(cfg)[0]:
        _require_intake()
    setting = labels_config(cfg)
    if not setting.enabled:
        raise _labels_off()
    if setting.settings is None:
        raise IntakeRefused(RefusalReason.INVALID_REQUEST, setting.error)


def _labels_tracker(
    cfg, entry, directory, requested: frozenset, tracker: Any
) -> tuple[Any, tuple[str, ...], tuple[bool, bool] | None]:
    """The projection's tracker (injected, or from :func:`labels_wiring`), the plugin's owned
    label prefixes and the binding's ``issue.type`` / ``issue.fields`` gates (``None`` for an
    injected tracker: read from config); a binding refusal is raised (it names the key to
    set)."""
    if tracker is not None:
        return tracker, (), None
    with phase_timings.phase("wiring"):
        wiring = labels_wiring(cfg, entry, directory, requested=requested)
    if wiring.get("tracker") is None:
        raise wiring.get("refusal") or _no_labels_wiring()
    gates = (wiring.get("type_enabled") is True, wiring.get("fields_enabled") is True)
    return wiring["tracker"], tuple(wiring.get("owned_label_prefixes", ())), gates


def labels_sync(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    actor: str,
    *,
    report: str = "",
    include_closed: bool = False,
    dry_run: bool = False,
    prune_unmapped: bool = False,
    hive: str = "",
    tracker: Any = None,
    lock: IntakeLock | None = None,
    ids: Any = None,
    sanitizer: Any = None,
) -> IntakeCall:
    """``bh work labels sync [<report>] [--include-closed] [--dry-run] [--prune-unmapped]``: the
    projection's sync (one issue listing and one ``labels.read`` per run) over the bound tracker
    (``issues.read`` and ``labels.read``). It DRAFTS at most one ``labels`` record per issue and
    never sends; ``dry_run`` writes nothing. Off unless ``intake.external.enabled`` and
    :data:`LABELS_ENABLED_KEY`; drafting also needs ``outbox.enabled`` and
    ``outbox.labels_enabled``."""
    from .modules.intake.domain import LabelSyncRequest
    from .modules.tracker.contracts import Operation

    def run():
        _require_labels(cfg)
        port, owned, gates = _labels_tracker(
            cfg,
            entry,
            directory,
            frozenset({Operation.ISSUES_READ, Operation.LABELS_READ}),
            tracker,
        )
        composed = _compose(
            cfg,
            entry,
            directory,
            hive=hive,
            verb="work labels sync",
            lock=lock,
            ids=ids,
            sanitizer=sanitizer,
            labels_tracker=port,
            owned_label_prefixes=owned,
            gates=gates,
        )
        return composed.service.sync_labels(
            LabelSyncRequest(
                actor,
                reports=(report,) if report else (),
                include_closed=include_closed,
                dry_run=dry_run,
                prune_unmapped=prune_unmapped,
            )
        )

    return _intake_call(run)


def labels_define(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    actor: str,
    *,
    approve: str = "",
    prune: bool = False,
    dry_run: bool = False,
    hive: str = "",
    tracker: Any = None,
    lock: IntakeLock | None = None,
    ids: Any = None,
    sanitizer: Any = None,
    audit: Any = None,
) -> IntakeCall:
    """``bh work labels define [--dry-run] [--prune] [--approve <digest>]``: the repository's label
    definitions against the registry-derived catalog, rendered with a digest. With ``approve``
    (an operator seat only, the digest the operator reviewed) it mints ONE single-use grant per
    tracker write and applies the plan, noting it in the local audit log; a converged repository
    needs none. Reads ``labels.read`` (and, for ``prune``, ``issues.read``); applying also needs
    ``labels.define``. Off unless ``intake.external.enabled`` and :data:`LABELS_ENABLED_KEY`."""
    from .modules.intake.domain import LabelDefineRequest
    from .modules.tracker.contracts import Operation

    requested = {Operation.LABELS_READ}
    if prune:
        requested.add(Operation.ISSUES_READ)
    if approve:
        requested.add(Operation.LABELS_DEFINE)

    def run():
        _require_labels(cfg)
        port, owned, gates = _labels_tracker(cfg, entry, directory, frozenset(requested), tracker)
        composed = _compose(
            cfg,
            entry,
            directory,
            hive=hive,
            verb="work labels define",
            lock=lock,
            ids=ids,
            sanitizer=sanitizer,
            audit=audit,
            labels_tracker=port,
            owned_label_prefixes=owned,
            gates=gates,
        )
        return composed.service.define_labels(
            LabelDefineRequest(actor, approve=approve, prune=prune, dry_run=dry_run)
        )

    return _intake_call(run)


def labels_review(cfg, entry, directory, **kwargs) -> OutboxCall:
    """``bh work labels review``: the rendered batch of every drafted ``labels.change`` operation,
    and every drafted ``issue.type`` / ``issue.fields`` one while its gate is on (one pass over
    the outbox documents and one issue listing), with the digest an operator
    approves. A read; off unless ``intake.external.enabled`` and ``outbox.enabled``."""
    return outbox_call(cfg, entry, directory, lambda port: port.review_label_batch(), **kwargs)


def labels_approve(cfg, entry, directory, digest: str, actor: str, **kwargs) -> OutboxCall:
    """``bh work labels approve --batch <digest>``: approve the reviewed batch (an operator seat
    only): one single-use grant per operation, a stale item skipped for re-draft. Sends nothing.
    Needs ``outbox.enabled`` and ``outbox.labels_enabled``."""
    from .modules.intake.domain import LabelBatchApprove

    return outbox_call(
        cfg,
        entry,
        directory,
        lambda port: port.approve_label_batch(LabelBatchApprove(digest, actor)),
        **kwargs,
    )


#: The pending review batches hold at most this many reports by default (whole clusters).
DEFAULT_REVIEW_BATCH_SIZE = 20


def review_policy(cfg: Mapping[str, Any], *, comments_read: bool) -> Any:
    """The host's ``ReviewPolicy``: ``comments_enabled`` is ``intake.external.comments.enabled``,
    ``comments_read`` whether the bound tracker serves ``comments.read`` (the binding enabled it),
    so review refuses, naming both switches, unless every pending thread can be read."""
    from .modules.intake.domain import ReviewPolicy

    return ReviewPolicy(comments_enabled=comments_settings(cfg)[0], comments_read=comments_read)


def project_issues(
    cfg: Mapping[str, Any], entry: Mapping[str, Any], directory=None, *, adapters: Any = None
) -> Any:
    """The bound project's issues for the pre-apply mention check (``bh-iamru``): a
    :class:`~beadhive.modules.intake.application.TrackerProjectIssues` over the hive's tracker
    binding, requesting ``issues.read`` only. LAZY: nothing is bound or read until a run's
    ``reporter_text`` mentions a ``#N``, and then ONE listing serves the whole run. A binding that
    is off or refuses raises ``IntakeRefused`` at that point, which the checks report as an
    unverified mention (fail closed)."""
    from .modules.intake.application import TrackerProjectIssues
    from .modules.tracker.contracts import Operation

    def tracker() -> Any:
        bound = adapters if adapters is not None else process_adapters(cfg)
        if bound is None:
            raise IntakeRefused(
                RefusalReason.INTAKE_DISABLED,
                f"no tracker is bound to list the project's issues ({TRACKER_CONFIG_HINT})",
            )
        home = directory if directory is not None else registry.hive_dir(entry)
        requested = frozenset({Operation.ISSUES_READ})
        try:
            wired = tracker_wiring(cfg, entry, home, bound, requested=requested, needs_marker=False)
        except IntakeStoreError as exc:  # the install id's private state
            raise IntakeRefused(RefusalReason.INTAKE_DISABLED, outbox_message(exc)) from exc
        return wired["tracker"]

    return TrackerProjectIssues(tracker)


def triage_binding(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    *,
    branch: str = "main",
    repo: Any = None,
    landed: Any = None,
    sanitizer: Any = None,
    refile: Callable[..., str] | None = None,
    issues: Any = None,
) -> Any:
    """The ``TriageBinding`` propose, review and apply read: the repository facts over the hive
    checkout (:class:`~beadhive.adapters.intake.GitRepoFacts`, read-only git), the landed commits
    from the ``git.commits`` linkage (:class:`LinkageLandedCommits`), the integration ``branch``,
    the outbox's leak policy and sanitizer, the reroute re-filer (``None``: a reroute is
    blocked) and the bound project's issues for the mention check (:func:`project_issues`,
    lazy)."""
    from .adapters.intake import GitRepoFacts, ScrubSanitizer
    from .modules.intake.application import TriageBinding

    return TriageBinding(
        repo=repo if repo is not None else GitRepoFacts(directory),
        leak_policy=leak_policy(cfg),
        sanitizer=sanitizer if sanitizer is not None else ScrubSanitizer(),
        branch=branch,
        landed=landed if landed is not None else LinkageLandedCommits(directory),
        refile=refile,
        issues=issues if issues is not None else project_issues(cfg, entry, directory),
    )


def reroute_refiler(
    destinations: Mapping[str, str],
    actor: str,
    *,
    file_report: Callable[..., tuple[int, str, str]],
    cfg: Mapping[str, Any] | None = None,
) -> Callable[..., str] | None:
    """The apply-time reroute re-filer (``--reroute-to <report>=<hive>``): files the rerouted
    report into its destination hive through ``file_report`` (the ``bh report`` write path,
    type-preserving, with the bound issue's URL as the description) and returns the reason the
    source records. A report with no destination refuses before anything is written, so the
    report is left untouched. ``None`` (no destinations): every reroute is blocked."""
    if not destinations:
        return None

    def refile(view, _proposal) -> str:
        to_hive = destinations.get(view.id, "")
        if not to_hive:
            raise IntakeRefused(
                RefusalReason.INVALID_REQUEST,
                f"no reroute destination for {view.id}: pass --reroute-to {view.id}=<hive>",
            )
        rtype = view.issue_type if view.issue_type in REPORT_TYPES else "bug"
        description = f"Rerouted from {view.binding.url}" if view.binding is not None else ""
        code, err, new_id = file_report(
            to_hive, view.title, rtype, actor, cfg=cfg, description=description
        )
        if err:
            raise IntakeRefused(
                RefusalReason.INVALID_REQUEST, f"reroute re-file into {to_hive} failed: {err}"
            )
        return f"rerouted to {to_hive} as {new_id}"

    return refile


def triage_review(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    actor: str,
    *,
    batch_size: int = DEFAULT_REVIEW_BATCH_SIZE,
    hive: str = "",
    tracker: Any = None,
    identity: OwnIdentity | None = None,
    lock: IntakeLock | None = None,
) -> IntakeCall:
    """``bh work triage review --pending``: ingest every pending report's thread, then build
    cluster-aware batches of review packets (body, full thread, proposal and reviews, cluster and
    the global index). The only write is the thread ingest. Off unless
    ``intake.external.enabled``; refused, naming both switches, unless
    ``intake.external.comments.enabled`` and the tracker's ``read.comments.enabled`` are on."""
    from .modules.intake.domain import ReviewRequest

    def run():
        if not external_intake_settings(cfg)[0]:
            _require_intake()
        threads: dict[str, Any] | None = None
        if tracker is not None:
            threads = {"tracker": tracker, "identity": identity}
        elif comments_settings(cfg)[0]:
            wiring = thread_wiring(cfg, entry, directory)
            if wiring.get("refusal") is not None:
                raise wiring["refusal"]
            if wiring.get("tracker") is not None:
                threads = wiring
        composed = _compose(
            cfg,
            entry,
            directory,
            hive=hive,
            verb="work triage review",
            lock=lock,
            threads=threads,
            review=review_policy(cfg, comments_read=threads is not None),
        )
        return composed.service.review_packets(ReviewRequest(actor, batch_size=batch_size))

    return _intake_call(run)


#: ``--review-of`` letters as the batch skill names its passes (bh-skls-p0v): pass A is the
#: analyst's proposal itself, B the first verifier pass and C the tie-break, i.e. review passes
#: 1 and 2 of a proposal's first round.
REVIEW_PASS_ALIASES = {"B": 1, "C": 2}


def review_pass(text: str) -> int:
    """The review pass number ``--review-of`` names: a positive number, or the batch skill's
    ``B`` / ``C`` (passes 1 and 2). ``A`` is refused: it is the proposal, not a review. Raises
    ``ValueError`` with the fix."""
    value = str(text).strip()
    if value.upper() == "A":
        raise ValueError("pass A is the analyst's proposal: store it without --review-of")
    if value.upper() in REVIEW_PASS_ALIASES:
        return REVIEW_PASS_ALIASES[value.upper()]
    if value.isdigit() and int(value) >= 1:
        return int(value)
    raise ValueError(f"--review-of takes a review pass number (1, 2, ...) or B / C, not {text!r}")


def triage_propose(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    report: str,
    document: Any,
    actor: str,
    *,
    review_of: int = 0,
    branch: str = "main",
    hive: str = "",
    lock: IntakeLock | None = None,
    repo: Any = None,
    landed: Any = None,
    sanitizer: Any = None,
) -> IntakeCall:
    """``bh work triage propose <report> <file|->``: store the analyst's proposal (or, with
    ``review_of``, the verifier's verdict for that pass) on ``report``. The codec and the leak
    lint of ``reporter_text`` run first, so a refusal writes nothing; nothing reaches a tracker.
    Off unless ``intake.external.enabled``."""
    from .modules.intake.domain import ProposalReviewRequest, ProposeRequest

    def run():
        if not external_intake_settings(cfg)[0]:
            _require_intake()
        binding = triage_binding(
            cfg, entry, directory, branch=branch, repo=repo, landed=landed, sanitizer=sanitizer
        )
        service = _compose(
            cfg, entry, directory, hive=hive, verb="work triage propose", lock=lock, triage=binding
        ).service
        if review_of:
            verdict = dict(document) if isinstance(document, Mapping) else document
            if isinstance(verdict, dict):
                stated = verdict.get("pass")
                if isinstance(stated, str):
                    try:
                        stated = review_pass(stated)
                    except ValueError as exc:
                        raise IntakeRefused(RefusalReason.PROPOSAL_INVALID, str(exc)) from exc
                if stated not in (None, 0, review_of):
                    raise IntakeRefused(
                        RefusalReason.PROPOSAL_INVALID,
                        f"the verdict says pass {stated}, but --review-of names pass {review_of}",
                    )
                verdict["pass"] = review_of
            return service.review_proposal(ProposalReviewRequest(report, verdict, actor))
        return service.propose(ProposeRequest(report, document, actor))

    return _intake_call(run)


@dataclass(frozen=True)
class TriageChecks:
    """``bh work triage check``: the dry run's checks of every open proposal, its plan per report
    (``items``, for the verification gap) and, with ``--pending``, the reports kept (still
    awaiting triage)."""

    outcome: Any
    pending: tuple[str, ...] | None = None

    @property
    def checks(self) -> tuple[Any, ...]:
        if self.pending is None:
            return tuple(self.outcome.checks)
        keep = set(self.pending)
        return tuple(c for c in self.outcome.checks if c.report in keep)

    @property
    def failed(self) -> tuple[str, ...]:
        return tuple(c.report for c in self.checks if not c.passed)


def triage_check(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    actor: str,
    *,
    pending: bool = False,
    branch: str = "main",
    hive: str = "",
    lock: IntakeLock | None = None,
    repo: Any = None,
    landed: Any = None,
    sanitizer: Any = None,
    issues: Any = None,
) -> IntakeCall:
    """``bh work triage check [--pending]``: every deterministic pre-apply check of every open
    proposal (targets, fix claims with the COMPUTED fix version, families, field consistency,
    reporter text, the ``#N`` it mentions, staleness), as ``apply --reviewed --dry-run`` runs
    them. Writes nothing; a ``#N`` costs ONE issue listing for the run (``issues``, else
    :func:`project_issues`).
    ``pending`` keeps only the reports still awaiting triage. Off unless
    ``intake.external.enabled``."""
    from .modules.intake.domain import ApplyRequest, may_disposition

    def run():
        if not external_intake_settings(cfg)[0]:
            _require_intake()
        binding = triage_binding(
            cfg,
            entry,
            directory,
            branch=branch,
            repo=repo,
            landed=landed,
            sanitizer=sanitizer,
            issues=issues,
        )
        composed = _compose(
            cfg, entry, directory, hive=hive, verb="work triage check", lock=lock, triage=binding
        )
        outcome = composed.service.apply_reviewed(ApplyRequest(actor, dry_run=True))
        if not pending:
            return TriageChecks(outcome)
        kept = []
        for check in outcome.checks:
            view = composed.store.get(check.report)
            if view is not None and view.status != "closed" and may_disposition(view.labels):
                kept.append(check.report)
        return TriageChecks(outcome, tuple(kept))

    return _intake_call(run)


def _operator_only(cfg: Mapping[str, Any], actor: str, doing: str) -> None:
    """Refuse an agent seat (or no actor) exactly as an outbox approval does."""
    from .modules.intake.domain import is_operator_seat

    if not is_operator_seat(actor, milestone_settings(cfg)[0]):
        raise IntakeRefused(
            RefusalReason.OPERATOR_SEAT_REQUIRED,
            f"{doing} needs a human operator seat, not {actor or 'no actor'!r}",
        )


def triage_apply(
    cfg: Mapping[str, Any],
    entry: Mapping[str, Any],
    directory,
    actor: str,
    *,
    dry_run: bool = False,
    branch: str = "main",
    refile: Callable[..., str] | None = None,
    hive: str = "",
    lock: IntakeLock | None = None,
    repo: Any = None,
    landed: Any = None,
    ids: Any = None,
    sanitizer: Any = None,
    outbox: Mapping[str, Any] | None = None,
    threads: Mapping[str, Any] | None = None,
    labels_tracker: Any = None,
    issues: Any = None,
) -> IntakeCall:
    """``bh work triage apply --reviewed [--dry-run]``: check every open proposal, then apply the
    reviewed ones canonicals first through the existing triage verbs. A reply or a question is
    DRAFTED (never sent); the touched reports' labels are refreshed (drafted) when the projection
    is on. An operator seat only, exactly as an outbox approval; ``dry_run`` (any seat) checks and
    plans and writes nothing. Off unless ``intake.external.enabled``.

    Unless injected, the sender comes from :func:`outbox_wiring` (while ``outbox.enabled``), the
    thread service from :func:`thread_wiring` (while ``comments.enabled``) and the projection's
    tracker from :func:`labels_wiring`; a verb whose wiring is missing refuses on its own report,
    which is then reported as failed with nothing written for it."""
    from .modules.intake.domain import ApplyRequest
    from .modules.tracker.contracts import Operation

    def run():
        if not external_intake_settings(cfg)[0]:
            _require_intake()
        if not dry_run:
            _operator_only(cfg, actor, "applying reviewed proposals")
        binding = triage_binding(
            cfg,
            entry,
            directory,
            branch=branch,
            repo=repo,
            landed=landed,
            sanitizer=sanitizer,
            refile=refile,
            issues=issues,
        )
        sender = outbox
        conversation = threads
        projection_tracker = labels_tracker
        owned: tuple[str, ...] = ()
        if not dry_run:
            if sender is None:
                sender = _apply_outbox(cfg, entry, directory)
            if conversation is None and comments_settings(cfg)[0]:
                conversation = _apply_threads(cfg, entry, directory)
            if projection_tracker is None and labels_config(cfg).enabled:
                wired = labels_wiring(
                    cfg,
                    entry,
                    directory,
                    requested=frozenset({Operation.ISSUES_READ, Operation.LABELS_READ}),
                )
                refusal = wired.get("refusal") or _no_labels_wiring()
                projection_tracker = wired.get("tracker") or _UnwiredTracker(lambda: refusal)
                owned = tuple(wired.get("owned_label_prefixes", ()))
        composed = _compose(
            cfg,
            entry,
            directory,
            hive=hive,
            verb="work triage apply",
            lock=lock,
            ids=ids,
            sanitizer=sanitizer,
            outbox=sender,
            threads=conversation,
            labels_tracker=projection_tracker,
            owned_label_prefixes=owned,
            triage=binding,
        )
        return composed.service.apply_reviewed(ApplyRequest(actor, dry_run=dry_run))

    return _intake_call(run)


def _apply_threads(cfg, entry, directory) -> dict[str, Any]:
    """The thread service's wiring for an apply (comment ingestion on): the thread wiring, or,
    when its tracker is not wired, an unwired stand-in. ``needs-info`` only reads the STORED
    thread, so it still runs; any tracker read refuses with the binding's reason."""
    wiring = thread_wiring(cfg, entry, directory)
    if wiring.get("tracker") is not None:
        return wiring
    refusal = wiring.get("refusal") or _no_thread_wiring()
    return {"tracker": _UnwiredTracker(lambda: refusal), "identity": None}


def _apply_outbox(cfg, entry, directory) -> dict[str, Any] | None:
    """The sender's wiring for an apply: the outbox wiring, or, while the outbox is on but its
    tracker is not wired, an unwired stand-in whose every use refuses with the binding's reason
    (a draft then fails its report, with nothing written for it). ``None``: the outbox is off."""
    if not (external_intake_settings(cfg)[0] and milestone_settings(cfg)[0].enabled):
        return None
    wiring = outbox_wiring(cfg, entry, directory)
    if wiring.get("tracker") is not None and wiring.get("marker_line") is not None:
        return wiring
    refusal = wiring.get("refusal") or _no_outbox_wiring()

    def refuse(*_args: Any, **_kwargs: Any) -> str:
        raise refusal

    return {
        "tracker": _UnwiredTracker(lambda: refusal),
        "identity": OwnIdentity("", ""),
        "marker_line": refuse,
    }


def projected_label_rows(cfg: Mapping[str, Any]) -> tuple[dict[str, str], ...]:
    """The projected GitHub label table for ``bh label docs``: one row per label the projection
    defines up front (``name``, ``color``, ``description``, ``dimension``, ``value``), plus one
    ``(open)`` row per open dimension, whose labels are defined as values appear. Empty while
    :data:`LABELS_ENABLED_KEY` is off or the section is invalid. Configuration only: nothing is
    read from a tracker or a store."""
    from .modules.intake.domain import LabelCatalog

    setting = labels_config(cfg)
    if not setting.enabled or setting.settings is None:
        return ()
    catalog = LabelCatalog.build(registry_dimensions(cfg), setting.settings)
    rows = [
        {
            "name": spec.name,
            "color": spec.color,
            "description": spec.description,
            "dimension": spec.dimension,
            "value": spec.value,
        }
        for spec in catalog.specs
    ]
    settings = setting.settings
    for dim in settings.project:
        if catalog.openness(dim) is not None and catalog.openness(dim).value == "open":
            rows.append(
                {
                    "name": f"{settings.prefix}{settings.group(dim)}/<value>",
                    "color": "",
                    "description": "(open: defined as values appear on projected beads)",
                    "dimension": dim,
                    "value": "",
                }
            )
    return tuple(rows)


def projected_label_lines(cfg: Mapping[str, Any]) -> list[str]:
    """``bh label docs``' "Projected GitHub labels" section (:func:`projected_label_rows`); no
    section at all while the projection is off."""
    rows = projected_label_rows(cfg)
    if not rows:
        return []  # the projection is off: the page is unchanged
    out = ["## Projected GitHub labels", ""]
    out += [
        "Derived from the dimension registry by `intake.external.labels` "
        "(`bh work labels define` creates them).",
        "",
        "| Label | Color | Description |",
        "|---|---|---|",
    ]
    for row in rows:
        color = f"`#{row['color']}`" if row["color"] else ""
        out.append(f"| `{row['name']}` | {color} | {row['description']} |")
    out.append("")
    return out


__all__ = [
    "AUDIT_LOG_FILE",
    "BatchSendOutcome",
    "DEFAULT_REVIEW_BATCH_SIZE",
    "LABELS_ENABLED_KEY",
    "REVIEW_PASS_ALIASES",
    "FileAuditLog",
    "IntakeCall",
    "LabelsConfig",
    "LinkageLandedCommits",
    "ReopenOutcome",
    "TriageChecks",
    "audit_path",
    "intake_message",
    "label_projection_for",
    "labels_approve",
    "labels_config",
    "labels_define",
    "labels_review",
    "labels_sync",
    "labels_wiring",
    "projected_label_lines",
    "projected_label_rows",
    "registry_dimensions",
    "reroute_refiler",
    "review_pass",
    "review_policy",
    "transition_refresh",
    "triage_apply",
    "triage_binding",
    "project_issues",
    "triage_check",
    "triage_propose",
    "triage_review",
    "catch_up_on_link",
    "needs_info",
    "needs_info_settings",
    "wake",
    "FlockIntakeLock",
    "INSTALL_ID_FILE",
    "StoredOutboxRecord",
    "ThreadCall",
    "comments_settings",
    "install_id",
    "thread_call",
    "thread_considered",
    "thread_ingest",
    "thread_message",
    "thread_service_for",
    "thread_show",
    "thread_wiring",
    "tracker_wiring",
    "ImportResult",
    "LinkResult",
    "RandomIdSource",
    "ValidateLabelPolicy",
    "directory_store",
    "dispose",
    "duplicate_rows",
    "entry_dupes",
    "external_intake_settings",
    "file_report",
    "fleet_prefixes",
    "import_issue",
    "import_message",
    "accept_to_work",
    "intake_lock",
    "leak_policy",
    "link_message",
    "link_report",
    "lock_dir",
    "MilestoneResult",
    "OutboxCall",
    "outbox_approve",
    "outbox_discard",
    "outbox_draft",
    "outbox_kind",
    "outbox_reconcile",
    "outbox_retry",
    "outbox_send",
    "outbox_show",
    "outbox_call",
    "outbox_message",
    "outbox_wiring",
    "delivery_release",
    "milestone_message",
    "milestone_settings",
    "outbox_service_for",
    "process_adapters",
    "queue_rows",
    "report_store",
    "reroute_to_hive",
    "sweep_delivery",
    "sweep_holds",
    "service_for",
    "service_for_directory",
    "store_error_message",
    "store_posture",
    "store_scope",
    "work_merged",
]
