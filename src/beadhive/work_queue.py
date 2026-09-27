"""Top-level adapter selecting the atomic ``work.claim-next`` route for `bh work next` (bh-l5sxi.2).

The composition seam for this cohort, on the same pattern as :mod:`beadhive.work_review`
(bh-bwnys.1): it resolves the hive's one supervised Beads v1.3 service
(:mod:`beadhive.host_beads`), and SELECTS THE ROUTE BEFORE EXECUTION rather than catching an API
failure and retrying through `bd` — the "missing-capability fallback" the epic's design calls for.
``beadhive_core`` is resolved lazily by name; ``src/beadhive`` never imports a workspace package
statically (``scripts/check_package_imports.py``).

Two things make the atomic route inapplicable up front, not on failure, so this module refuses to
even try the API and reports ``None`` (meaning: run the existing CLI-compatibility pick/claim/
re-verify loop in :mod:`beadhive.work_dispatch`) for both:

* **``--epic`` scoping.** ``work.claim-next`` has no recursive molecule-membership filter — Beads'
  ``parent`` query parameter is the direct parent-child edge only, one level (see
  :mod:`beadhive_core.queue`'s module docstring), where the CLI-compatibility path's
  ``bd children --include-infra --all`` walk is recursive. Approximating that over HTTP would risk
  claiming a bead outside the intended molecule, so epic-scoped claims stay on the named CLI route
  unconditionally.
* **An undeclared (bare) actor.** Which seat-prefix an actor auto-resolves to
  (``dev/<name>`` vs ``disp/<name>``) depends on the TYPE of the bead actually claimed
  (:func:`beadhive.work_guards.kind_of`) — and the atomic claim commits its ``actor`` string in the
  same transaction that picks the bead, before the type is known. A caller that already declares
  its seat (``dev/alice`` / ``disp/alice``) carries no such ambiguity: the write's actor is fixed
  either way, and :func:`claim_next` verifies the claimed bead's type against the declared seat
  afterward, releasing (never leaving claimed) and reporting a refusal on a mismatch — the same
  outcome the CLI path's pre-claim seat check produces, reached by a compensating release instead
  of a pre-check. A bare actor has no such fixed point to verify against, so it stays CLI-compatible
  too, where `_next_seat_actor` resolves the prefix from the CANDIDATE's type before ever claiming.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import host_beads, log, otel, work_guards

_CORE_MODULE = "beadhive_core"


def _core() -> Any:
    return importlib.import_module(_CORE_MODULE)


class TelemetryRoutingObserver:
    """Map the routing table's selected-route notifications onto the existing structured log."""

    def selected(self, name: str, kind: str) -> None:
        log.get_logger("beadhive.work").info("route_selected", operation=name, route=kind)


def hive_session(main: Path, entry: Any) -> Any:
    """An unopened session against the hive's one supervised Beads service (``bh host beads``).

    Never starts ``bd serve``: an absent or stale service (or a hive that cannot be served at
    all) raises the client's ``ServiceUnavailable`` / ``beadhive_core.SessionUnavailable``, both
    of which :func:`claim_next` catches to select the CLI-compatibility route instead — always
    before any write is attempted, never as a retry after one fails.
    """
    try:
        return host_beads.resolve_session(main, _core().QUEUE_CAPABILITIES, entry=entry)
    except host_beads.HiveNotServable as exc:
        raise _core().SessionUnavailable(str(exc)) from exc


#: The session seam: ``(main, entry)`` -> an unopened ``BeadsSession``. Tests substitute a
#: transport-fixture session here; production resolves the hive's supervised service.
session_factory: Callable[[Path, Any], Any] = hive_session


@dataclass(frozen=True)
class ApiNextResult:
    """The atomic route's verdict, in the same vocabulary `work_dispatch.impl_next_` already
    speaks: a claimed bead id (empty when nothing was claimed), 0-or-1 auto-released seat
    mismatches, and — when nothing was claimed — the decline reason."""

    claimed: str = ""
    refused: tuple[str, ...] = ()
    reason: str = ""
    row: dict = field(default_factory=dict)


def _seat_mismatch(actor: str, claimed_row: dict) -> bool:
    declared = work_guards.seat_of(actor)
    wanted = "dispatcher" if work_guards.is_epic(claimed_row) else "developer"
    return bool(declared) and declared != wanted


def claim_next(main: Path, entry: Any, actor: str) -> ApiNextResult | None:
    """Attempt the atomic ``work.claim-next`` route for one actor.

    Returns ``None`` to mean "select the CLI-compatibility route instead": an undeclared actor or
    an unavailable service/capability, decided before any Beads write is attempted. Once the
    route IS selected, a genuine write failure (an ambiguous write, a service problem) propagates
    to the caller to report fail-closed — it is never silently retried through `bd`.
    """
    if not work_guards.seat_of(actor):
        return None  # auto-resolving seat prefix needs the candidate's type first — CLI-only
    core = _core()
    observer = TelemetryRoutingObserver()
    try:
        session_cm = session_factory(main, entry)
        with session_cm as session:
            commands = core.QueueCommands()
            outcome = commands.claim_next(session, actor, observer=observer)
            if outcome.claimed is None:
                page = commands.list_ready(session, limit=1, observer=observer)
                reason = core.decline(core.ready_rows(page))
                return ApiNextResult(reason=reason)
            row = outcome.claimed
            bead = str(row.get("id") or "")
            if _seat_mismatch(actor, row):
                models = importlib.import_module("beads_v1_3.models")
                session.release_issue(bead, models.ReleaseIssueRequest(actor=actor))
                return ApiNextResult(refused=(bead,), row=row)
            otel.set_bead(bead)
            otel.count_bead_transition("claimed")
            return ApiNextResult(claimed=bead, row=row)
    except (
        core.RouteMismatch,
        core.OperationDenied,
        core.UnknownOperation,
        OSError,
        ValueError,
    ) as exc:
        log.get_logger("beadhive.work").info("queue_route_fallback", detail=str(exc))
        return None
    except _incompatible_service_errors() as exc:
        log.get_logger("beadhive.work").info("queue_route_fallback", detail=str(exc))
        return None


def _incompatible_service_errors() -> tuple[type[BaseException], ...]:
    """``IncompatibleService`` (whose subclass ``CapabilityMissing`` is the missing-capability
    fallback) plus the supervised-service errors ``hive_session`` raises — imported lazily so
    this module never statically imports a workspace package."""
    beads_client = importlib.import_module("beadhive_beads_client")
    service_mod = importlib.import_module("beadhive_beads_client.service")
    core = _core()
    return (
        beads_client.IncompatibleService,
        service_mod.ServiceError,
        core.SessionUnavailable,
    )
