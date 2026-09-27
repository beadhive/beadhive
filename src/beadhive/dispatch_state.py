"""Top-level adapter selecting the molecule-progress, swarm-inspection, dispatch-poll, and
local-loop-state routes (bh-sy36q.5) for :mod:`beadhive.localloop` and
:mod:`beadhive.work_dispatch`.

The composition seam for this cohort, on the same pattern as :mod:`beadhive.work_queue`
(bh-l5sxi.2 / bh-mu5yb.1 / bh-p76tk.1): it resolves the hive's one supervised Beads v1.3 service
(:mod:`beadhive.host_beads`) and SELECTS THE ROUTE BEFORE EXECUTION rather than catching an API
failure and retrying through `bd`. ``beadhive_core`` is resolved lazily by name; ``src/beadhive``
never imports a workspace package statically (``scripts/check_package_imports.py``).

Every function here returns ``None`` to mean "select the CLI-compatibility route instead"
(:mod:`beadhive.bd`'s `show` / `children` / `child_rows` / `json(["ready", ...])` forwards, still
the fallback IMPLEMENTATION itself for a hive with no capable Beads service — including
`bh work loop`'s local no-server tier, which must keep working): an unavailable service or
capability, decided before any Beads read is attempted, never as a retry after one fails. Once a
route IS selected, a genuine read failure propagates to the caller to report fail-closed.

Restart-as-a-no-op (the property :mod:`beadhive.localloop` requires) is preserved either way:
neither this module nor :mod:`beadhive_core.dispatch` caches anything between calls — every
function re-derives its answer fresh, exactly like the `bd` forward it stands in for.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import host_beads, log

_CORE_MODULE = "beadhive_core"
_LOGGER_NAME = "beadhive.dispatch_state"


def _core() -> Any:
    return importlib.import_module(_CORE_MODULE)


class TelemetryRoutingObserver:
    """Map the routing table's selected-route notifications onto the existing structured log."""

    def selected(self, name: str, kind: str) -> None:
        log.get_logger(_LOGGER_NAME).info("route_selected", operation=name, route=kind)


def hive_session(main: Path, entry: Any) -> Any:
    """An unopened session against the hive's one supervised Beads service (``bh host beads``).

    Never starts ``bd serve``: an absent or stale service (or a hive that cannot be served at
    all) raises the client's ``ServiceUnavailable`` / ``beadhive_core.SessionUnavailable``, both
    of which every ``open_*`` function below catches to select the CLI-compatibility route
    instead — always before any read is attempted, never as a retry after one fails.
    """
    try:
        return host_beads.resolve_session(main, _core().DISPATCH_CAPABILITIES, entry=entry)
    except host_beads.HiveNotServable as exc:
        raise _core().SessionUnavailable(str(exc)) from exc
    except (KeyError, TypeError) as exc:
        raise _core().SessionUnavailable(
            f"cannot address hive from entry {entry!r}: {exc}"
        ) from exc


#: The session seam: ``(main, entry)`` -> an unopened ``BeadsSession``. Tests substitute a
#: transport-fixture session here; production resolves the hive's supervised service.
session_factory: Callable[[Path, Any], Any] = hive_session


def _incompatible_service_errors() -> tuple[type[BaseException], ...]:
    """``IncompatibleService`` (whose subclass ``CapabilityMissing`` is the missing-capability
    fallback) plus the supervised-service errors ``hive_session`` raises — imported lazily so
    this module never statically imports a workspace package. Mirrors
    :func:`beadhive.work_queue._incompatible_service_errors` exactly."""
    beads_client = importlib.import_module("beadhive_beads_client")
    service_mod = importlib.import_module("beadhive_beads_client.service")
    core = _core()
    return (
        beads_client.IncompatibleService,
        service_mod.ServiceError,
        core.SessionUnavailable,
    )


def _route_fallback_errors(core: Any) -> tuple[type[BaseException], ...]:
    return (core.RouteMismatch, core.OperationDenied, core.UnknownOperation, OSError, ValueError)


def open_molecule_progress(main: Path, entry: Any, bead: str) -> dict[str, Any] | None:
    """Attempt the ``work.molecule.progress`` route for one bead's durable detail — the fetch
    :meth:`beadhive.localloop.LocalLoop.load_molecule` makes for the epic's own row.

    Returns ``None`` to mean "select the CLI-compatibility route instead" (:func:`beadhive.bd.show`
    against the epic): an unavailable service or capability, decided before any Beads read is
    attempted."""
    core = _core()
    observer = TelemetryRoutingObserver()
    try:
        with session_factory(main, entry) as session:
            commands = core.DispatchCommands()
            return commands.molecule_progress(session, bead, observer=observer)
    except _route_fallback_errors(core) as exc:
        log.get_logger(_LOGGER_NAME).info("dispatch_route_fallback", detail=str(exc))
        return None
    except _incompatible_service_errors() as exc:
        log.get_logger(_LOGGER_NAME).info("dispatch_route_fallback", detail=str(exc))
        return None


def open_local_loop_state(main: Path, entry: Any, bead: str) -> dict[str, Any] | None:
    """Attempt the ``work.local-loop.state`` route for one bead's durable detail — the fetch
    :meth:`beadhive.localloop.LocalLoop._default_routing` makes to decide per-bead model routing.

    Returns ``None`` to mean "select the CLI-compatibility route instead"
    (:func:`beadhive.bd.show` with ``strict=True``): an unavailable service or capability, decided
    before any Beads read is attempted."""
    core = _core()
    observer = TelemetryRoutingObserver()
    try:
        with session_factory(main, entry) as session:
            commands = core.DispatchCommands()
            return commands.local_loop_state(session, bead, observer=observer)
    except _route_fallback_errors(core) as exc:
        log.get_logger(_LOGGER_NAME).info("dispatch_route_fallback", detail=str(exc))
        return None
    except _incompatible_service_errors() as exc:
        log.get_logger(_LOGGER_NAME).info("dispatch_route_fallback", detail=str(exc))
        return None


def open_swarm_members(main: Path, entry: Any, epic: str) -> list[dict[str, Any]] | None:
    """Attempt the ``work.swarm.inspect`` route for one epic's full RESTARTABLE membership
    (closed children and infra rows included) — the fetch
    :meth:`beadhive.localloop.LocalLoop.load_molecule` and
    :func:`beadhive.work_dispatch.impl__molecule_members` make (`bd list --parent <epic>
    --include-infra --all`, narrowed to the direct parent edge).

    Returns ``None`` to mean "select the CLI-compatibility route instead"
    (:func:`beadhive.bd.children`): an unavailable service or capability, decided before any Beads
    read is attempted."""
    core = _core()
    observer = TelemetryRoutingObserver()
    try:
        with session_factory(main, entry) as session:
            commands = core.DispatchCommands()
            return commands.swarm_members(session, epic, observer=observer)
    except _route_fallback_errors(core) as exc:
        log.get_logger(_LOGGER_NAME).info("dispatch_route_fallback", detail=str(exc))
        return None
    except _incompatible_service_errors() as exc:
        log.get_logger(_LOGGER_NAME).info("dispatch_route_fallback", detail=str(exc))
        return None


def open_event_rows(main: Path, entry: Any, bead: str) -> list[dict[str, Any]] | None:
    """Attempt the ``work.swarm.inspect`` route for one bead's own dotted-id event stream — the
    fetch :meth:`beadhive.localloop.LocalLoop.load_molecule` makes per child
    (:func:`beadhive.bd.child_rows`, NOT narrowed to the direct parent edge — see
    :meth:`beadhive_core.dispatch.DispatchCommands.event_rows`'s docstring for why).

    Returns ``None`` to mean "select the CLI-compatibility route instead"
    (:func:`beadhive.bd.child_rows`): an unavailable service or capability, decided before any
    Beads read is attempted."""
    core = _core()
    observer = TelemetryRoutingObserver()
    try:
        with session_factory(main, entry) as session:
            commands = core.DispatchCommands()
            return commands.event_rows(session, bead, observer=observer)
    except _route_fallback_errors(core) as exc:
        log.get_logger(_LOGGER_NAME).info("dispatch_route_fallback", detail=str(exc))
        return None
    except _incompatible_service_errors() as exc:
        log.get_logger(_LOGGER_NAME).info("dispatch_route_fallback", detail=str(exc))
        return None


def open_poll_ready(
    main: Path, entry: Any, *, parent: str | None = None
) -> list[dict[str, Any]] | None:
    """Attempt the ``work.dispatch.poll`` route for the dispatch pass's ready-set poll — the fetch
    :meth:`beadhive.localloop.LocalLoop.claimable_now` makes (`bd ready --limit 0`).

    Returns ``None`` to mean "select the CLI-compatibility route instead"
    (:func:`beadhive.bd.json` with ``["ready", "--limit", "0"]``): an unavailable service or
    capability, decided before any Beads read is attempted."""
    core = _core()
    observer = TelemetryRoutingObserver()
    try:
        with session_factory(main, entry) as session:
            commands = core.DispatchCommands()
            return commands.poll_ready(session, parent=parent, observer=observer)
    except _route_fallback_errors(core) as exc:
        log.get_logger(_LOGGER_NAME).info("dispatch_route_fallback", detail=str(exc))
        return None
    except _incompatible_service_errors() as exc:
        log.get_logger(_LOGGER_NAME).info("dispatch_route_fallback", detail=str(exc))
        return None
