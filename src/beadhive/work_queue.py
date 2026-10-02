"""Top-level adapter selecting the atomic ``work.claim-next`` route for `bh work next` (bh-l5sxi.2),
the epic-scoped guarded-claim route for `bh work next --epic` (bh-7ip8t), the ``work.issue.list``
children route for `bh work schedule` (bh-mu5yb.1), and the
``work.ready.list`` route for `bh work ready`'s unbounded `--json` reads (bh-p76tk.1).

The composition seam for this cohort, on the same pattern as :mod:`beadhive.work_review`
(bh-bwnys.1): it resolves the hive's one supervised Beads v1.3 service
(:mod:`beadhive.host_beads`), and SELECTS THE ROUTE BEFORE EXECUTION rather than catching an API
failure and retrying through `bd` — the "missing-capability fallback" the epic's design calls for.
``beadhive_core`` is resolved lazily by name; ``src/beadhive`` never imports a workspace package
statically (``scripts/check_package_imports.py``).

Two things make the atomic ``work.claim-next`` route inapplicable up front, not on failure. The
first has its own API route; the second stays CLI-compatibility:

* **``--epic`` scoping** (bh-7ip8t) selects :func:`claim_in_epic` instead — the epic-scoped
  guarded-claim route (:meth:`beadhive_core.queue.QueueCommands.claim_next_in_epic`), not the
  atomic op. The molecule is the epic plus its DIRECT children, closed and infra rows included
  (one level, deliberately — bh-sh6yt), and ``work.claim-next``'s ``parent`` filter is recursive
  (bh-mu5yb.1), so it cannot express that scope; the epic route fetches the scoped candidates and
  takes them through the ``issues.claim`` compare-and-set instead. It is selected before
  execution exactly like :func:`claim_next`, and — when no capable session opens — returns
  ``None`` to mean "run the named CLI-compatibility pick/claim/re-verify loop in
  :mod:`beadhive.work_dispatch`", gated by ``work.beads.route`` like every other seam here.
* **An undeclared (bare) actor.** Which seat-prefix an actor auto-resolves to
  (``dev/<name>`` vs ``disp/<name>``) depends on the TYPE of the bead actually claimed
  (:func:`beadhive.work_guards.kind_of`) — and the atomic claim commits its ``actor`` string in the
  same transaction that picks the bead, before the type is known. A caller that already declares
  its seat (``dev/alice`` / ``disp/alice``) carries no such ambiguity: the write's actor is fixed
  either way, and :func:`claim_next` verifies the claimed bead's type against the declared seat
  afterward, releasing (never leaving claimed) and reporting a refusal on a mismatch — the same
  outcome the CLI path's pre-claim seat check produces, reached by a compensating release instead
  of a pre-check. A bare actor has no such fixed point to verify against, so an UNSCOPED bare-actor
  claim stays CLI-compatible, where `_next_seat_actor` resolves the prefix from the CANDIDATE's
  type before ever claiming (the ``--epic`` route resolves it the same way, per candidate, before
  its compare-and-set — so it needs no such carve-out).

:func:`open_children` (bh-mu5yb.1) and :func:`open_ready` (bh-p76tk.1) are the analogous seams for
`bh work schedule`'s epic-children fetch and `bh work ready`'s unbounded `--json` reads: neither
has an actor/seat carve-out (both are reads), so the only reason to fall back is the
service/capability being genuinely unavailable, exactly the same set of exceptions
:func:`claim_next` already catches. Every such error-driven fallback is gated by the hive's
``work.beads.route`` (:func:`beadhive.beads_routing.allow_cli_route`, bh-m36pc): under the
default ``api`` route it fails the command closed instead of selecting `bd`. `bh work ready`'s
CAPPED reads (bd's own default, or an explicit non-zero `--limit`) stay CLI-compatible
unconditionally instead — not a fallback, a pre-execution route choice:
:class:`beadhive_beads_client.ReadyPage` carries no total-count field to reproduce bd's own
"Showing X of Y ready issues" truncation notice byte-for-byte, so only a `limit=0` (fully
unbounded) read is asked of :func:`open_ready` at all — see :mod:`beadhive.work_reads` and
`packages/beadhive-core/README.md`.
"""

from __future__ import annotations

import importlib
from collections.abc import Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import beads_routing, log, otel, work_guards

_CORE_MODULE = "beadhive_core"


def _core() -> Any:
    return importlib.import_module(_CORE_MODULE)


class TelemetryRoutingObserver:
    """Map the routing table's selected-route notifications onto the existing structured log."""

    def selected(self, name: str, kind: str) -> None:
        log.get_logger("beadhive.work").info("route_selected", operation=name, route=kind)


def hive_session(main: Path, entry: Any) -> Any:
    """An unopened session for this cohort's ``QUEUE_CAPABILITIES`` — see
    :func:`beadhive.beads_routing.hive_session` (the one composition decision, including the
    ``work.beads.route`` cli route)."""
    return beads_routing.hive_session(main, entry, _core().QUEUE_CAPABILITIES)


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
    from . import frame_eligibility

    try:
        session_cm = session_factory(main, entry)
        with session_cm as session:
            commands = core.QueueCommands()
            from .frame_eligibility import GuardedClaimSession

            outcome = commands.claim_next(
                GuardedClaimSession(session, main), actor, observer=observer
            )
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
    except frame_eligibility.EligibilityError:
        raise
    except (
        core.RouteMismatch,
        core.OperationDenied,
        core.UnknownOperation,
        OSError,
        ValueError,
    ) as exc:
        beads_routing.allow_cli_route(entry, exc)
        log.get_logger("beadhive.work").info("queue_route_fallback", detail=str(exc))
        return None
    except _incompatible_service_errors() as exc:
        beads_routing.allow_cli_route(entry, exc)
        log.get_logger("beadhive.work").info("queue_route_fallback", detail=str(exc))
        return None


def claim_in_epic(
    main: Path,
    entry: Any,
    epic: str,
    actor: str,
    seat_actor: Callable[[Mapping[str, Any]], str | None],
) -> Any | None:
    """Attempt the epic-scoped guarded-claim route for `bh work next --epic <epic>` (bh-7ip8t):
    :meth:`beadhive_core.queue.QueueCommands.claim_next_in_epic`, with ``seat_actor`` (the
    shell's one seat rule) resolving each candidate's seat before any write — so, unlike
    :func:`claim_next`, an undeclared actor needs no carve-out here.

    Returns the core ``EpicClaimOutcome``, or ``None`` to mean "select the CLI-compatibility route
    instead" — decided while OPENING the session (no service, a missing capability, an
    unaddressable hive), before any Beads read or write, and only when
    :func:`beadhive.beads_routing.allow_cli_route` permits it. Once the session is open the route
    is selected: any failure after that propagates fail-closed and is never replayed through
    `bd` — a scoped pass may already have claimed a bead by then.
    """
    core = _core()
    with ExitStack() as stack:
        try:
            session = stack.enter_context(session_factory(main, entry))
        except (
            core.RouteMismatch,
            core.OperationDenied,
            core.UnknownOperation,
            OSError,
            ValueError,
            *_incompatible_service_errors(),
        ) as exc:
            beads_routing.allow_cli_route(entry, exc)
            log.get_logger("beadhive.work").info("queue_route_fallback", detail=str(exc))
            return None
        from .frame_eligibility import GuardedClaimSession

        outcome = core.QueueCommands().claim_next_in_epic(
            GuardedClaimSession(session, main),
            epic,
            actor,
            seat_actor=seat_actor,
            observer=TelemetryRoutingObserver(),
        )
    if outcome.claimed:
        otel.set_bead(outcome.claimed)
        otel.count_bead_transition("claimed")
    return outcome


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
        beads_routing.allow_cli_route(entry, exc)
        log.get_logger("beadhive.work").info("queue_route_fallback", detail=str(exc))
        return None
    except _incompatible_service_errors() as exc:
        beads_routing.allow_cli_route(entry, exc)
        log.get_logger("beadhive.work").info("queue_route_fallback", detail=str(exc))
        return None


def open_ready(main: Path, entry: Any, **kwargs: Any) -> list[dict] | None:
    """Attempt the ``work.ready.list`` route for one `bh work ready --json` read (bh-p76tk.1):
    every keyword is one of :meth:`beadhive_core.queue.QueueCommands.list_ready`'s typed
    narrowing parameters, forwarded verbatim.

    Returns ``None`` to mean "select the CLI-compatibility route instead" (:mod:`beadhive.bd`'s
    forward through `bd ready`): an unavailable service or capability, decided before any Beads
    read is attempted — the same fallback set :func:`claim_next` / :func:`open_children` already
    catch, since this is a read with none of :func:`claim_next`'s actor/seat carve-outs. Once the
    route IS selected, a genuine read failure propagates to the caller to report fail-closed,
    never silently retried through `bd`.

    :mod:`beadhive.work_reads` selects this seam only for a read whose truncation cannot silently
    diverge from `bd ready`'s own byte-for-byte report: :class:`beadhive_beads_client.ReadyPage`
    carries no total-count field to reproduce bd's "Showing X of Y ready issues" text, so an
    UNBOUNDED read (``limit=0``) is the only shape asked for here — see
    `packages/beadhive-core/README.md`'s "bh work ready" section for the full reasoning and the
    narrowing flags that still stay CLI-compatibility unconditionally (`--mol`/`--mol-type`,
    `--gated`, and every other non-narrowing `bd ready` flag `bh` forwards verbatim).
    """
    core = _core()
    observer = TelemetryRoutingObserver()
    try:
        session_cm = session_factory(main, entry)
        with session_cm as session:
            commands = core.QueueCommands()
            page = commands.list_ready(session, observer=observer, **kwargs)
            return core.ready_rows(page)
    except (
        core.RouteMismatch,
        core.OperationDenied,
        core.UnknownOperation,
        OSError,
        ValueError,
    ) as exc:
        beads_routing.allow_cli_route(entry, exc)
        log.get_logger("beadhive.work").info("queue_route_fallback", detail=str(exc))
        return None
    except _incompatible_service_errors() as exc:
        beads_routing.allow_cli_route(entry, exc)
        log.get_logger("beadhive.work").info("queue_route_fallback", detail=str(exc))
        return None


def encode_ready_rows(rows: list[dict[str, Any]]) -> str:
    """`bd ready --json`-byte-identical encoding for :func:`open_ready`'s rows — a thin forward to
    :func:`beadhive_core.to_bd_json`, kept here so `work_reads` never imports `beadhive_core`
    statically (this module already resolves it lazily by name; see the module docstring)."""
    return _core().to_bd_json(rows)


def _incompatible_service_errors() -> tuple[type[BaseException], ...]:
    return beads_routing.unavailable_errors()
