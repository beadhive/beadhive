"""Ready selection and atomic claim-next: Beadhive queue policy over the pinned Beads v1.3 session.

Beads owns readiness and claim atomicity — the whole point of routing through
``work.ready.list`` / ``work.claim-next`` (see :mod:`beadhive_core.routing`) is that this module
never reproduces Beads' own state machine to decide what is ready or who wins a race. What stays
Beadhive policy is much narrower: which of the rows Beads reports counts as a candidate for one
actor (:func:`eligible`), which closed-set reason a decline carries (:func:`decline`), and the
direct parent-child narrowing (:func:`by_parent`) a caller can apply to an already-fetched page
without a second HTTP round trip.

``work.claim-next`` (``POST /v0/beads/ready:claimNext``) is one transaction: the ready predicate,
the compare-and-set that wins a row, and the hydration of the row that was won. It replaces the
optimistic "list ready, try to claim the first candidate, re-read to see who actually won, retry
the next candidate on a lost race" loop the CLI-compatibility path still runs
(:mod:`beadhive.work_next`'s ``eligible`` / ``claim_won`` / ``decline`` and
:mod:`beadhive.work_dispatch`'s ``impl_next_``) — that loop exists ONLY because ``bd update
--claim`` is not a hard compare-and-swap. Once a session negotiates ``issues.claimNext``, there is
nothing left to retry: :class:`QueueCommands.claim_next` is a single call, and its result is
final.

``work.claim-next`` has no server-side "recursive molecule membership" filter (Beads' `parent`
query parameter is the direct parent-child edge only, one level — the same field
:class:`IssueWithCounts` reports). The CLI compatibility path's ``--epic`` scoping
(:func:`beadhive.work_dispatch.impl__molecule_members`) walks ``bd children --include-infra
--all`` recursively, which has no HTTP equivalent. The shell composition seam therefore selects
the CLI-compatibility route explicitly, before execution, whenever a caller scopes to an epic —
never by narrowing this module's contract to approximate it.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from beads_v1_3.models import ClaimNextRequest, IssueWithCounts, ReadyPage
from beads_v1_3.types import UNSET

from .routing import RoutingObserver, RoutingTable, default_table

#: Capabilities a queue session must negotiate before any read or write is attempted.
#: `issues.release` is required alongside `issues.claimNext`, not because a successful claim
#: releases anything, but because a seat-mismatched claim (see `beadhive.work_queue`) must be able
#: to release it back in the SAME session — a capability check that fails only after the claim
#: already committed would leave a wrongly-typed bead claimed with nobody able to undo it.
QUEUE_CAPABILITIES = frozenset(
    {
        "project.enforce",
        "issues.get",
        "issues.list",
        "ready.list",
        "issues.claimNext",
        "issues.release",
    }
)

#: Infra bead types never offered as claimable work: a gate is a blocker record and an event is
#: the audit trail — claiming either is a category error. Mirrors `work_next.INFRA_TYPES`.
INFRA_TYPES = frozenset({"gate", "event"})

#: Why a claim-next request came back with nothing claimed (see `decline`). Distinct codes because
#: they mean different things to a driver, exactly as the legacy CLI-compatibility loop's did.
DECLINE_EMPTY_QUEUE = "empty_queue"  # the ready front was empty
DECLINE_NONE_ELIGIBLE = "none_eligible"  # ready rows existed, none claimable by this actor
DECLINES: tuple[str, ...] = (DECLINE_EMPTY_QUEUE, DECLINE_NONE_ELIGIBLE)


def ready_row(item: IssueWithCounts) -> dict[str, Any]:
    """One ready row as a plain dict, in the exact shape ``bd ready --json`` emits.

    ``IssueWithCounts`` is documented (see the generated client) as the same element type both
    ``GET /v0/beads/ready`` and ``bd ready --json`` return, so no field is renamed or reshaped
    here — this is a type-erasure, not a translation.
    """
    return item.to_dict()


def ready_rows(page: ReadyPage) -> list[dict[str, Any]]:
    """Every row of one ``ReadyPage``, in ``bd ready --json`` shape."""
    return [ready_row(item) for item in page.items]


def _is_infra(row: Mapping[str, Any]) -> bool:
    return str(row.get("issue_type") or "") in INFRA_TYPES


def _is_closed(row: Mapping[str, Any]) -> bool:
    return str(row.get("status") or "") == "closed"


def _is_in_flight(row: Mapping[str, Any]) -> bool:
    return str(row.get("status") or "") == "in_progress"


def eligible(rows: Sequence[Mapping[str, Any]], actor: str) -> tuple[str, ...]:
    """The claim candidates from a ready page, in the order Beads returned them.

    Order is preserved deliberately — the ready route is already priority-ordered, so re-sorting
    here would silently override it. This only FILTERS: infra rows, closed rows, anything already
    in flight, and anything assigned to somebody else. A bead already assigned to ``actor`` stays
    eligible (the idempotent resume case, not a race). Mirrors
    :func:`beadhive.work_next.eligible` exactly, over API rows instead of `bd ready --json` rows —
    the two are the same shape, so the same policy applies unchanged.
    """
    out: list[str] = []
    for row in rows:
        if _is_infra(row) or _is_closed(row) or _is_in_flight(row):
            continue
        assignee = str(row.get("assignee") or "")
        if assignee and assignee != actor:
            continue
        bead = str(row.get("id") or "")
        if bead:
            out.append(bead)
    return tuple(out)


def decline(rows: Sequence[Mapping[str, Any]]) -> str:
    """Which decline code a claim-next request that claimed nothing should report.

    Only two codes apply here (unlike the CLI-compatibility loop's three): the atomic route never
    "tries and loses a race" — it either wins one transactionally or reports nothing eligible —
    so `all_lost` has no equivalent. A caller distinguishes an empty ready front from "ready rows
    exist but none are eligible for this actor" by first peeking `list_ready` (see
    `beadhive.work_queue`), which is exactly what this function classifies.
    """
    return DECLINE_EMPTY_QUEUE if not rows else DECLINE_NONE_ELIGIBLE


def by_parent(rows: Sequence[Mapping[str, Any]], parent: str) -> list[dict[str, Any]]:
    """Rows whose direct ``parent`` field equals ``parent`` (Beads' parent-child edge, one level).

    NOT a recursive molecule-membership traversal — see the module docstring. An empty ``parent``
    returns every row unfiltered.
    """
    if not parent:
        return list(rows)
    return [dict(row) for row in rows if str(row.get("parent") or "") == parent]


@dataclass(frozen=True)
class ClaimNextOutcome:
    """The result of one atomic ``work.claim-next`` transaction.

    ``claimed`` is ``None`` exactly when nothing was eligible — mirroring
    ``ClaimNextResponse.claimed``'s own UNSET-is-the-signal contract (see its docstring): there is
    no separate boolean that could disagree with it.
    """

    claimed: dict[str, Any] | None


class QueueCommands:
    """The one place Beadhive calls the ready-selection and claim-next routes.

    Every method goes through :meth:`~beadhive_core.routing.RoutingTable.call_api`, so a missing
    ``ready.list`` / ``issues.claimNext`` capability raises
    :class:`beadhive_beads_client.CapabilityMissing` before any request reaches Beads — the shell
    composition seam catches that (and a session it could not open at all) to select the
    CLI-compatibility route instead, always before execution, never by retrying an API failure
    through `bd`.
    """

    def __init__(self, routing: RoutingTable | None = None) -> None:
        self._routing = routing or default_table()

    def list_ready(
        self, session: object, *, limit: int = 100, observer: RoutingObserver | None = None
    ) -> ReadyPage:
        """The ready page for the negotiated session, priority-ordered, Beads-authoritative."""
        return self._routing.call_api(  # type: ignore[return-value]
            session, "work.ready.list", limit=limit, observer=observer
        )

    def claim_next(
        self, session: object, actor: str, *, observer: RoutingObserver | None = None
    ) -> ClaimNextOutcome:
        """Atomically take the next ready issue for ``actor``, or report nothing was eligible.

        One HTTP transaction: the ready predicate, the compare-and-set, and the hydration of the
        won row all commit together. There is no retry here because there is nothing left to
        retry — a caller that loses is a caller that reports `claimed=None`, not one that raced
        and lost.
        """
        response = self._routing.call_api(
            session, "work.claim-next", ClaimNextRequest(actor=actor), observer=observer
        )
        claimed = response.claimed  # type: ignore[attr-defined]
        return ClaimNextOutcome(None if claimed is UNSET else claimed.to_dict())
