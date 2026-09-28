"""Molecule progress, swarm inspection, dispatch polling, and local-loop bead-state access
(bh-sy36q.5): Beadhive reads over the pinned Beads v1.3 session for the `local` work-runtime
tier's poll loop.

Beads owns the durable lifecycle state this module reads; nothing here mirrors it into a second
store, a journal, or a generic event bus, and there is no event STREAMING here — polling is the
whole mechanism (see :mod:`beadhive.localloop`'s own module docstring: "everything it needs to
run [a pass] is re-derived from `bd` each time. That is what makes a RESTART a no-op by
construction"). This module keeps that property when the read is routed over the API instead of
`bd`: every method re-issues the read fresh and returns the same restartable snapshot.

The bh-97fo0.3 operation matrix names four routes for this cohort, each an api-ready wrapper over
one of two capabilities:

* ``work.molecule.progress`` / ``work.local-loop.state`` — ``issues.get`` (``get_issue``): a
  single bead's durable detail (status, dependencies), the restart source for one bead's own
  progress (an epic's own status; a leaf bead's state feeding a routing/dispatch decision).
* ``work.swarm.inspect`` / ``work.dispatch.poll`` — ``issues.list`` / ``ready.list``
  (``list_issues`` / ``list_ready``): a molecule's restartable membership (every recursive
  descendant, direct-edge narrowed, including closed and infra rows — the same set
  ``bd list --parent <epic> --include-infra --all`` returns) and the dispatch pass's ready-set
  poll (``bd ready --limit 0``), respectively.

Two named routes sharing one underlying capability (``work.molecule.progress`` and
``work.local-loop.state`` both ``issues.get``; ``work.swarm.inspect`` and ``work.dispatch.poll``
using ``issues.list``/``ready.list`` from the same ``DispatchCommands``) is deliberate: each name
is a distinct call site in the bh-97fo0.3 evidence, attributed separately in routing telemetry,
not a single generic "read a bead" abstraction wearing four names.

WHAT STAYS OUT OF SCOPE (bh-sy36q.5's own boundary): process scheduling and role execution stay
root-supplied runtime capabilities — spawning a seat, the CANCEL ladder, process-group reaping —
none of that is touched here. This module only owns the bead-STATE reads those flows depend on.
"""

from __future__ import annotations

from typing import Any

from .queue import direct_children, ready_rows
from .routing import ApiRoute, RoutingObserver, RoutingTable, default_table


def _api(name: str) -> str:
    route = default_table().route(name)
    if not isinstance(route, ApiRoute):
        raise RuntimeError(f"{name} is {route.kind.value}, not api-ready")
    return route.name


#: The api-ready routes this cohort touches, resolved through the routing table at import time
#: (bh-l5sxi.1's pattern): this module fails loudly the day the installed matrix reclassifies one
#: of them away from api-ready, rather than silently keeping a stale literal.
MOLECULE_PROGRESS_ROUTE = _api("work.molecule.progress")
SWARM_INSPECT_ROUTE = _api("work.swarm.inspect")
DISPATCH_POLL_ROUTE = _api("work.dispatch.poll")
LOCAL_LOOP_STATE_ROUTE = _api("work.local-loop.state")
#: Every named route this cohort touches, for traceability (see the package README).
DISPATCH_ROUTES = (
    MOLECULE_PROGRESS_ROUTE,
    SWARM_INSPECT_ROUTE,
    DISPATCH_POLL_ROUTE,
    LOCAL_LOOP_STATE_ROUTE,
)

#: Capabilities a dispatch session must negotiate before any read is attempted.
DISPATCH_CAPABILITIES = frozenset({"project.enforce", "issues.get", "issues.list", "ready.list"})


class DispatchCommands:
    """The one place Beadhive calls the molecule-progress, swarm-inspection, dispatch-poll and
    local-loop-state routes.

    Every method goes through :meth:`~beadhive_core.routing.RoutingTable.call_api`, so a missing
    capability raises :class:`beadhive_beads_client.CapabilityMissing` before any request reaches
    Beads — the composition seam catches that (and a session it could not open at all) to select
    the CLI-compatibility fallback instead, always before execution, never by retrying an API
    failure through `bd`.
    """

    def __init__(self, routing: RoutingTable | None = None) -> None:
        self._routing = routing or default_table()

    def molecule_progress(
        self, session: object, bead_id: str, *, observer: RoutingObserver | None = None
    ) -> dict[str, Any]:
        """One bead's durable detail (status, dependencies) — the restart source for tracking an
        epic's own molecule-level progress across passes. Mirrors `bd show <id> --json`'s shape
        closely enough for status/dependency reads; it is not asserted byte-identical, since
        nothing here reproduces `bd`'s own output bytes (unlike `beadhive_core.queue.to_bd_json`,
        this cohort feeds decisions, not a rendered CLI contract)."""
        detail = self._routing.call_api(
            session, "work.molecule.progress", bead_id, observer=observer
        )
        return detail.to_dict()  # type: ignore[attr-defined]

    def local_loop_state(
        self, session: object, bead_id: str, *, observer: RoutingObserver | None = None
    ) -> dict[str, Any]:
        """One bead's durable detail, read for a local-loop decision (e.g. per-bead model routing)
        rather than for epic-level progress — a distinct named call site over the same
        `issues.get` capability, attributed separately in routing telemetry."""
        detail = self._routing.call_api(
            session, "work.local-loop.state", bead_id, observer=observer
        )
        return detail.to_dict()  # type: ignore[attr-defined]

    def swarm_members(
        self, session: object, epic_id: str, *, observer: RoutingObserver | None = None
    ) -> list[dict[str, Any]]:
        """An epic's full RESTARTABLE membership: every recursive descendant, narrowed to the
        direct parent-child edge, including CLOSED rows and INFRA types (gate/event) — the same
        set `bd list --parent <epic> --include-infra --all` returns and
        `beadhive.localloop.LocalLoop.load_molecule` needs (closed children so the ready/blocked
        computation sees finished work as finished; infra rows so gate/event beads are visible to
        the decision table and the escalation latch).

        Unlike :meth:`beadhive_core.queue.QueueCommands.list_children` (open children only, the
        `bh work schedule` fetch), this asks for the full default-exclusion override."""
        page = self._routing.call_api(
            session,
            "work.swarm.inspect",
            parent=epic_id,
            sort="priority",
            limit=0,
            all_=True,
            include_infra=True,
            observer=observer,
        )
        rows = [item.to_dict() for item in page.items]  # type: ignore[attr-defined]
        return direct_children(rows, epic_id)

    def event_rows(
        self, session: object, bead_id: str, *, observer: RoutingObserver | None = None
    ) -> list[dict[str, Any]]:
        """One bead's dotted-id event stream, NOT narrowed to the direct parent edge — mirrors
        `bd.child_rows`/`bd list --parent <bead> --include-infra --all` exactly: an event bead can
        retain bd's historical dotted-id-prefix match even after it no longer carries a parent
        edge, and narrowing here the way :meth:`swarm_members` does would silently turn a
        populated history into an empty one, breaking `work_next.attempt_count`'s loop-breaker."""
        page = self._routing.call_api(
            session,
            "work.swarm.inspect",
            parent=bead_id,
            sort="priority",
            limit=0,
            all_=True,
            include_infra=True,
            observer=observer,
        )
        return [item.to_dict() for item in page.items]  # type: ignore[attr-defined]

    def poll_ready(
        self,
        session: object,
        *,
        limit: int = 0,
        parent: str | None = None,
        observer: RoutingObserver | None = None,
    ) -> list[dict[str, Any]]:
        """The dispatch pass's ready-set poll (`bd ready --limit 0`, optionally `--parent`):
        repeated reads reflect dependency transitions with no mirrored journal — polling is the
        whole mechanism (see the module docstring)."""
        page = self._routing.call_api(
            session, "work.dispatch.poll", limit=limit, parent=parent, observer=observer
        )
        return ready_rows(page)  # type: ignore[arg-type]
