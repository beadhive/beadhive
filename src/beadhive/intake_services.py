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
  one host, an ``fcntl.flock`` on a per-binding file in the hive's private ``.bh/`` state for one
  process, ``guard.live_epoch`` as the epoch; the store's ``seq`` re-check is the record half), a
  random :class:`RandomIdSource`, the :class:`LeakPolicy` and store posture, and
  :func:`outbox_service_for`. The tracker adapter and its marker codec are INJECTED: building
  them from the plugin binding and credentials is not this root's yet (``bh-kzs0b``), and the
  outbox is off unless the caller passes an enabled :class:`OutboxPolicy`.

Nothing is cached, so every test and integration patch point stays live for each command.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import os
import socket
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import bd, bd_cli, registry, store_target, validate
from .adapters.intake import BdReportStore, FileCheckpointStore
from .modules.intake import (
    ORIGIN_GITHUB,
    ORIGIN_REPORT,
    REPORT_TYPES,
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
    IssueDiscoverer,
    IssueImporter,
    LinkOutcome,
    LinkRequest,
    OwnIdentity,
    QueueQuery,
    RefusalReason,
    ReportDraft,
    StorePosture,
    StoreScope,
    bind_intake,
)
from .modules.intake.application import (  # the composition root may (gap G7)
    MilestoneService,
    OutboxService,
)
from .modules.intake.domain import (  # the composition root may (gap G7)
    CompletionPolicy,
    LeakPolicy,
    MergeFact,
    MilestoneOutcome,
    MilestonePolicy,
    OutboxPolicy,
    ReleaseFact,
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


def report_store(directory, scope: StoreScope | Callable[[], StoreScope]) -> BdReportStore:
    """A :class:`BdReportStore` over bd's named routes for ``directory`` (a hive checkout, or the
    hub cache of a clone-on-demand hive)."""
    return BdReportStore(bd_cli.routes(directory), scope, error_line=bd.err_line)


def service_for(
    cfg: Mapping[str, Any], entry: Mapping[str, Any], directory, *, binary: str = "bh"
) -> IntakeService:
    """The intake service for ``entry``, writing into ``directory``. ``binary`` is the CLI alias
    the recorded history names (``filed via <binary> report``)."""
    store = report_store(directory, lambda: store_scope(cfg, entry))
    labels = ValidateLabelPolicy(cfg, str(entry["prefix"]))
    return IntakeService(store, labels, binary=binary)


def _no_scope() -> StoreScope:  # pragma: no cover - guards a misuse
    raise RuntimeError("a directory-scoped intake store has no store scope")


def directory_store(directory) -> BdReportStore:
    """A store for reads and dispositions of the hive at ``directory``: none of them reads the
    scope, so no hive entry is needed."""
    return report_store(directory, _no_scope)


def service_for_directory(directory) -> IntakeService:
    """A service for the queue and the dispositions of the hive at ``directory``."""
    return IntakeService(directory_store(directory), _NoLabelPolicy())


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


def sweep_holds(directory, *, repair: bool = False, actor: str = "") -> HoldSweep:
    """Compare the bound reports of the store at ``directory`` with its open ``intake-hold:`` gates
    (and, with ``repair``, open a missing hold and resolve a stale one). Reads only the operational
    store; a hive that never imported a report has none and sweeps clean."""
    return service_for_directory(directory).sweep_holds(HoldSweepRequest(actor, repair))


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


def process_adapters(cfg: Mapping[str, Any]) -> Any:
    """The process's ``TrackerAdapters`` for ``cfg``, or ``None`` while production credential
    wiring (``bh-kzs0b``) does not exist: then import refuses with a clear R0. This is the one
    place that wiring lands; tests inject ``adapters=`` or ``tracker=`` instead."""
    return None


def _no_tracker_wiring() -> IntakeRefused:
    return IntakeRefused(
        RefusalReason.INTAKE_DISABLED,
        "no tracker adapter is wired into this build of bh (production credential wiring is "
        "bh-kzs0b); bind and enable the hive's tracker, then import",
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
    ``TrackerAdapters``) or an explicit ``tracker`` port; with neither, import refuses (R0): no
    production credential wiring exists yet. Import only reads the tracker, holds no write grant,
    resolves no public store and never writes one. A dry run classifies R0 to R8 and writes
    nothing."""
    enabled, default_type = external_intake_settings(cfg)
    if enabled and tracker is None and adapters is None:
        adapters = process_adapters(cfg)

    def build():
        port = tracker if tracker is not None else _bound_tracker(entry, adapters)
        store = report_store(directory, lambda: store_scope(cfg, entry))
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

    Off unless ``intake.external.enabled`` (``intake_disabled`` through the null port). The
    tracker is the one the hive's public-store binding serves (``adapters``, asked for
    ``issues.read`` only) or an explicit ``tracker`` port; with neither, discovery refuses (no
    production credential wiring yet). It holds no write grant, resolves no public store and
    writes none; checkpoints are the hive's private ``.bh/`` files (``checkpoints`` injects
    another :class:`CheckpointStore`). ``full`` is ``--full``: every issue including closed, then
    the id reconcile."""
    enabled, default_type = external_intake_settings(cfg)
    if enabled and tracker is None and adapters is None:
        adapters = process_adapters(cfg)

    def build():
        port = tracker if tracker is not None else _bound_tracker(entry, adapters)
        store = report_store(directory, lambda: store_scope(cfg, entry))
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

    try:
        outcome = bind_intake(enabled, build).discover(DiscoveryRequest(actor, full_reconcile=full))
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
):
    """The port a link runs on: the service over the OPERATIONAL store and the root lock when
    ``intake.external.enabled``, else :class:`NullIntake` (every write refuses ``intake_disabled``
    and no store is built). No tracker is composed: a link neither reads nor writes one."""
    enabled, _default_type = external_intake_settings(cfg)

    def build():
        store = report_store(directory, lambda: store_scope(cfg, entry))
        labels = ValidateLabelPolicy(cfg, str(entry["prefix"]))
        held = lock if lock is not None else intake_lock(cfg, directory, hive=hive, verb=verb)
        return IntakeService(store, labels, lock=held)

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
) -> LinkResult:
    """``bh work link <report> --to <work> [--replace]`` or ``--remove``.

    A bound report ``tracks`` a work bead of the hive's OPERATIONAL store (the public store is
    never resolved, and no edge crosses stores); several reports may track one work bead and an
    unlink leaves the others intact. Off unless ``intake.external.enabled``."""
    try:
        port = _linking_port(cfg, entry, directory, lock, hive, "work link")
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
        outcome = _linking_port(cfg, entry, directory, lock, hive, "work accept").dispose(request)
    except IntakeRefused as exc:
        return LinkResult(1, exc.detail, exc.reason.value)
    except IntakeStoreError as exc:
        return LinkResult(exc.returncode, link_message(exc), "store_error", bead=exc.bead)
    except store_target.StoreResolutionError as exc:
        return LinkResult(1, str(exc), "store_unresolved")
    return LinkResult(0, disposition=outcome)


# -- dispositions ----------------------------------------------------------------------------


def dispose(directory, request: DispositionRequest) -> tuple[int, str, DispositionOutcome | None]:
    """Run one triage disposition; ``(exit, error, outcome)``."""
    try:
        return 0, "", service_for_directory(directory).dispose(request)
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
        service_for_directory(directory).reroute_elsewhere(
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
    flock, and a moved epoch marks its attempts stale. The directory is created on first hold."""

    def __init__(
        self,
        lock_dir: Path | Callable[[], Path],
        *,
        primary: Callable[[], None],
        epoch: Callable[[], int],
    ) -> None:
        self._lock_dir = lock_dir
        self._primary = primary
        self._epoch = epoch

    def _path(self, binding_key: str) -> Path:
        directory = self._lock_dir() if callable(self._lock_dir) else self._lock_dir
        directory.mkdir(parents=True, exist_ok=True)
        name = hashlib.sha256(binding_key.encode("utf-8")).hexdigest()
        return directory / f"{name}.lock"

    @contextlib.contextmanager
    def hold(self, binding_key: str) -> Iterator[int]:
        self._primary()
        with self._path(binding_key).open("a+") as handle:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise IntakeRefused(
                    RefusalReason.INTAKE_BUSY,
                    f"{binding_key} is held by another intake operation; try again when it ends",
                ) from None
            try:
                yield self._epoch()
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


def _primary_guard(
    cfg: Mapping[str, Any], hive: str, verb: str = "work outbox"
) -> Callable[[], None]:
    def check() -> None:
        import typer

        from . import guard

        try:
            guard.guard_primary(hive, cfg=cfg, verb=verb)
        except typer.Exit as exc:
            raise IntakeRefused(
                RefusalReason.NOT_PRIMARY,
                f"this host is not the hive's primary writer; run `bh {verb}` on the primary",
            ) from exc

    return check


def _live_epoch(cfg: Mapping[str, Any], hive: str) -> Callable[[], int]:
    def epoch() -> int:
        from . import guard

        return int(guard.live_epoch(hive, cfg=cfg))

    return epoch


def lock_dir(directory) -> Path:
    """``<hive>/.bh/intake/locks``: the per-binding lock files, in the hive's private state."""
    from . import private_paths

    root = private_paths.ensure_repo_private_root(directory)
    if root is None:
        raise IntakeStoreError(
            f"{directory} has no private .bh/ state for the intake lock", operation="lock"
        )
    return root / "intake" / "locks"


def intake_lock(
    cfg: Mapping[str, Any], directory, *, hive: str = "", verb: str = "work outbox"
) -> FlockIntakeLock:
    """The production lock for the hive at ``directory`` (``hive`` is the CLI's selector; ``verb``
    names the write verb in a primary-host refusal)."""
    return FlockIntakeLock(
        lambda: lock_dir(directory),
        primary=_primary_guard(cfg, hive, verb),
        epoch=_live_epoch(cfg, hive),
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
) -> IntakeService:
    """The intake service for ``entry`` with the supervised sender bound over the INJECTED
    ``tracker`` (and its ``marker_line`` codec).

    Off by default: with ``policy`` absent or disabled no sender is bound at all, so every outbox
    operation refuses with ``OUTBOX_DISABLED`` before the tracker is touched. Enabling it here is
    still never sufficient to post: the adapter's own per-operation enable and an operator
    approval minting a single-use grant are both required. ``sanitizer`` defaults to the shared
    :class:`~beadhive.adapters.intake.ScrubSanitizer`; the store is the OPERATIONAL one only."""
    policy = policy or OutboxPolicy()
    store = report_store(directory, lambda: store_scope(cfg, entry))
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
    )
    return IntakeService(store, labels, outbox=sender, milestones=milestones)


# -- the merge and release milestones (I10): they draft, they never send ---------------------


def milestone_settings(cfg: Mapping[str, Any]) -> tuple[OutboxPolicy, MilestonePolicy]:
    """The outbox policy and the milestone switches from ``intake.external``: every draft off,
    ``close-on-merge``, and the outbox off, by default. Host-owned like the rest of the section;
    only a literal ``true`` switches anything on, and an unrecognised completion policy takes
    the conservative ``close-on-release`` (the report stays open) rather than closing early."""
    section = ((cfg.get("intake") or {}).get("external")) or {}
    milestones = section.get("milestones") or {}
    outbox = section.get("outbox") or {}
    try:
        completion = CompletionPolicy(section.get("completion_policy") or "close-on-merge")
    except ValueError:
        completion = CompletionPolicy.CLOSE_ON_RELEASE
    return (
        OutboxPolicy(enabled=outbox.get("enabled") is True),
        MilestonePolicy(
            draft_on_merge=milestones.get("draft_on_merge") is True,
            draft_on_release=milestones.get("draft_on_release") is True,
            append_release_to_closed=milestones.get("append_release_to_closed") is True,
            completion=completion,
        ),
    )


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

        store = report_store(directory, lambda: store_scope(cfg, entry))
        labels = ValidateLabelPolicy(cfg, str(entry["prefix"]))
        outbox, milestones = milestone_settings(cfg)
        service = MilestoneService(
            store,
            lock if lock is not None else intake_lock(cfg, directory, hive=hive, verb=verb),
            ids=ids if ids is not None else RandomIdSource(),
            leak_policy=leak_policy(cfg),
            sanitizer=sanitizer if sanitizer is not None else ScrubSanitizer(),
            posture=store_posture(entry),
            outbox=outbox,
            milestones=milestones,
            verifier=verifier,
        )
        return IntakeService(store, labels, milestones=service)

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


__all__ = [
    "FlockIntakeLock",
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
    "delivery_release",
    "milestone_message",
    "milestone_settings",
    "outbox_service_for",
    "process_adapters",
    "queue_rows",
    "report_store",
    "reroute_to_hive",
    "sweep_holds",
    "service_for",
    "service_for_directory",
    "store_error_message",
    "store_posture",
    "store_scope",
    "work_merged",
]
