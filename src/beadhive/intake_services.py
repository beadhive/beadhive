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
  as rerouted), which is what retired the ``triage -> report`` legacy cycle edge.

Nothing is cached, so every test and integration patch point stays live for each command.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Any

from . import bd, bd_cli, registry, store_target, validate
from .adapters.intake import BdReportStore
from .modules.intake import (
    ORIGIN_REPORT,
    REPORT_TYPES,
    Disposition,
    DispositionOutcome,
    DispositionRequest,
    HiveTriplet,
    IntakeRefused,
    IntakeService,
    IntakeStoreError,
    QueueQuery,
    ReportDraft,
    StoreScope,
)
from .modules.intake.domain import dupes_touching  # the composition root may (gap G7)


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


__all__ = [
    "ValidateLabelPolicy",
    "directory_store",
    "dispose",
    "duplicate_rows",
    "entry_dupes",
    "file_report",
    "queue_rows",
    "report_store",
    "reroute_to_hive",
    "service_for",
    "service_for_directory",
    "store_error_message",
    "store_scope",
]
