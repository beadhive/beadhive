"""Top-level adapter selecting the atomic ``work.claim-next`` route for `bh work next` (bh-l5sxi.2)
and the ``work.issue.list`` children route for `bh work schedule` (bh-mu5yb.1).

The composition seam for this cohort, on the same pattern as :mod:`beadhive.work_review`
(bh-bwnys.1): it resolves the hive's one supervised Beads v1.3 service
(:mod:`beadhive.host_beads`), and SELECTS THE ROUTE BEFORE EXECUTION rather than catching an API
failure and retrying through `bd` — the "missing-capability fallback" the epic's design calls for.
``beadhive_core`` is resolved lazily by name; ``src/beadhive`` never imports a workspace package
statically (``scripts/check_package_imports.py``).

Two things make the atomic ``work.claim-next`` route inapplicable up front, not on failure, so
this module refuses to even try the API and reports ``None`` (meaning: run the existing
CLI-compatibility pick/claim/re-verify loop in :mod:`beadhive.work_dispatch`) for both:

* **``--epic`` scoping.** ``work.claim-next``'s ``parent`` filter is not used for this: the CLI
  path's ``bd children --include-infra --all`` recursive walk has no HTTP equivalent that also
  narrows to durable-vs-ephemeral / infra inclusion the way that flag combination does, so
  epic-scoped claims stay on the named CLI route unconditionally. (CORRECTION, bh-mu5yb.1: an
  earlier revision of this docstring claimed Beads' `parent` QUERY PARAMETER was the direct
  parent-child edge only, one level — that is false; the pinned v1.3 OpenAPI spec documents it,
  and a real disposable-hive probe confirms it, as "restrict to recursive descendants of this
  issue" on both `GET /v0/beads/ready` and `work.claim-next`. See
  :mod:`beadhive_core.queue`'s module docstring and `packages/beadhive-core/README.md` for the
  full correction and what was and was not re-evaluated as a result.)
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

:func:`open_children` (bh-mu5yb.1) is the analogous seam for `bh work schedule`'s epic-children
fetch: it has no such carve-outs (no actor, no seat ambiguity — a read), so the only reason to
fall back is the service/capability being genuinely unavailable, exactly the same set of
exceptions :func:`claim_next` already catches.
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
    of which :func:`claim_next` / :func:`open_children` catch to select the CLI-compatibility
    route instead — always before any write or read is attempted, never as a retry after one
    fails. A caller carrying a minimal/synthesized ``entry`` missing the registry triplet (several
    read-only surfaces pass ``{"prefix": "..."}`` deliberately, to prove a `bd`-absent failure is
    reported honestly rather than misattributed — see `tests/test_mcp_strict_bd_reads.py`) is the
    same "cannot even address this hive" condition as `HiveNotServable`, not a bug in this seam.
    """
    try:
        return host_beads.resolve_session(main, _core().QUEUE_CAPABILITIES, entry=entry)
    except host_beads.HiveNotServable as exc:
        raise _core().SessionUnavailable(str(exc)) from exc
    except (KeyError, TypeError) as exc:
        raise _core().SessionUnavailable(
            f"cannot address hive from entry {entry!r}: {exc}"
        ) from exc


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


def open_children(main: Path, entry: Any, epic: str) -> list[dict] | None:
    """Attempt the ``work.issue.list`` route for one epic's DIRECT open children (`bh work
    schedule`'s fetch — every non-closed child, not just the unblocked ones ``work.ready.list``
    would report).

    Returns ``None`` to mean "select the CLI-compatibility route instead"
    (:func:`beadhive.bd.children`): an unavailable service or capability, decided before any Beads
    read is attempted — the same fallback set :func:`claim_next` already catches, since this is a
    read with none of that function's actor/seat carve-outs. Once the route IS selected, a genuine
    read failure propagates to the caller to report fail-closed, never silently retried through
    `bd`.
    """
    core = _core()
    observer = TelemetryRoutingObserver()
    try:
        session_cm = session_factory(main, entry)
        with session_cm as session:
            commands = core.QueueCommands()
            return commands.list_children(session, epic, observer=observer)
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
