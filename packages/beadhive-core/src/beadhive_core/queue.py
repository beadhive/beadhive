"""Ready selection and atomic claim-next: Beadhive queue policy over the pinned Beads v1.3 session.

Beads owns readiness and claim atomicity — the whole point of routing through
``work.ready.list`` / ``work.claim-next`` / ``work.issue.list`` (see :mod:`beadhive_core.routing`)
is that this module never reproduces Beads' own state machine to decide what is ready, open, or
who wins a race. What stays Beadhive policy is much narrower: which of the rows Beads reports
counts as a candidate for one actor (:func:`eligible`), which closed-set reason a decline carries
(:func:`decline`), and the direct parent-child narrowing (:func:`by_parent` /
:func:`direct_children`) a caller can apply to an already-fetched page without a second HTTP
round trip.

``QueueCommands.list_ready`` is widened (bh-mu5yb.1) to accept every one of `bh work ready`'s
narrowing flags that ``GET /v0/beads/ready`` also accepts — real, tested infrastructure a future
composition seam can use (see ``packages/beadhive-core/README.md`` for why `bh work ready`'s own
CLI composition is not switched over to it yet). ``bh work schedule`` DOES route through this
module today: it fetches one epic's DIRECT open children through
:meth:`QueueCommands.list_children`, which asks Beads for every RECURSIVE descendant under
``parent`` (`GET /v0/beads/issues?parent=<epic>` — see :func:`direct_children`'s docstring for why
that query parameter is not the same thing as the one-level row field) and then narrows locally to
the direct edge, reproducing :func:`beadhive.bd.children`'s exact selection without a second
process.

``work.claim-next`` (``POST /v0/beads/ready:claimNext``) is one transaction: the ready predicate,
the compare-and-set that wins a row, and the hydration of the row that was won. It replaces the
optimistic "list ready, try to claim the first candidate, re-read to see who actually won, retry
the next candidate on a lost race" loop the CLI-compatibility path still runs
(:mod:`beadhive.work_next`'s ``eligible`` / ``claim_won`` / ``decline`` and
:mod:`beadhive.work_dispatch`'s ``impl_next_``) — that loop exists ONLY because ``bd update
--claim`` is not a hard compare-and-swap. Once a session negotiates ``issues.claimNext``, there is
nothing left to retry: :class:`QueueCommands.claim_next` is a single call, and its result is
final.

``bh work next --epic <id>`` (bh-7ip8t) routes through :meth:`QueueCommands.claim_next_in_epic`,
NOT the atomic ``work.claim-next``: an epic-scoped claim needs a candidate set the atomic op
cannot express. The molecule is the epic PLUS its DIRECT children, one level, deliberately
(bh-sh6yt: a nested epic is dispatched as a bead and then driven by its own loop, so recursing
would let an outer loop claim a grandchild out from under the loop that owns it), and that
membership includes closed and infra rows (``bd list --parent <epic> --include-infra --all``).
``work.claim-next``'s ``parent`` filter is recursive (bh-mu5yb.1) and cannot narrow to one level,
so the epic route is the pre-atomic shape instead: fetch the scoped candidates
(:meth:`QueueCommands.epic_candidates` — the unbounded ready front narrowed to
:meth:`QueueCommands.molecule_members`), then take them in ready order through
``work.claim.acquire`` (``issues.claim``), which IS a server-side compare-and-set: a claim held by
another actor is a 409, never a silent overwrite, so a lost race is a definite answer and the next
candidate is tried. Each win is still believed only through :func:`claim_won` over the row Beads
returns (or a re-read, when the write's outcome is unknown) — the guarded-update discipline the
CLI-compatibility loop applies to ``bd update --claim``.

CORRECTION (bh-mu5yb.1): an earlier revision of this docstring claimed Beads' `parent` QUERY
PARAMETER on `GET /v0/beads/ready` / `work.claim-next` was the direct parent-child edge only, one
level — that is false. The pinned v1.3 OpenAPI spec (`spec/openapi.v0.yaml`) documents `parent` on
both `listReadyWork` and `claimNextIssue` as "Restrict to recursive descendants of this issue", and
a real disposable-hive probe against `bd`/`bd serve` 1.3.0 (f45b249ce) confirms it: a leaf three
levels under an epic is returned by `GET /v0/beads/ready?parent=<epic>`. What IS one level only is
the ROW field :class:`IssueWithCounts` reports back on each item (`row["parent"]`, what
:func:`by_parent` and :func:`direct_children` filter on) — a different thing from the query
parameter that selects which rows come back in the first place. `GET /v0/beads/issues?parent=`,
by contrast, returns only DIRECT children on the same server build despite identical OpenAPI
wording (bh-sy36q.5's real-service finding); :func:`direct_children` narrows either way, so no
caller here depends on which one the server does.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from beadhive_beads_client import IndeterminateWrite, ServiceProblem
from beads_v1_3.models import ClaimNextRequest, ClaimRequest, IssueWithCounts, ReadyPage
from beads_v1_3.types import UNSET

from .lifecycle import claim_won
from .routing import RoutingObserver, RoutingTable, default_table

#: Capabilities a queue session must negotiate before any read or write is attempted.
#: `issues.release` is required alongside `issues.claimNext`, not because a successful claim
#: releases anything, but because a seat-mismatched claim (see `beadhive.work_queue`) must be able
#: to release it back in the SAME session — a capability check that fails only after the claim
#: already committed would leave a wrongly-typed bead claimed with nobody able to undo it.
#: `issues.claim` is the compare-and-set `bh work next --epic` takes its scoped candidates through
#: (:meth:`QueueCommands.claim_next_in_epic`, bh-7ip8t) — negotiated up front for the same reason.
QUEUE_CAPABILITIES = frozenset(
    {
        "project.enforce",
        "issues.get",
        "issues.list",
        "ready.list",
        "issues.claim",
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
#: The third code only the epic-scoped guarded-claim loop can report: candidates existed and were
#: tried, and every one was claimed out from under us (a 409 from the compare-and-set). Mirrors
#: `work_next.DECLINE_ALL_LOST`; the atomic route never "tries and loses" (see `decline`).
DECLINE_ALL_LOST = "all_lost"
EPIC_DECLINES: tuple[str, ...] = (*DECLINES, DECLINE_ALL_LOST)

#: `issues.claim` refusals that mean "this candidate is not ours to take" — lost to another actor
#: (`already_claimed`), no longer claimable (`not_claimable`: closed/in flight since the ready read)
#: or gone (`not_found`). Each is a definite, committed-nothing answer, so the loop moves on to the
#: next candidate; any OTHER refusal is a genuine failure and propagates, fail-closed.
_LOST_CLAIM_CODES = frozenset({"already_claimed", "not_claimable", "not_found"})


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


#: Characters Go's `encoding/json` HTML-escapes by default (`SetEscapeHTML(true)`, the mode every
#: `bd` JSON writer uses) that Python's `json.dumps` does not. `bd_json` escapes these explicitly
#: after Python's own encoding, since neither encoder's default alone reproduces the other's bytes.
_GO_HTML_ESCAPES: tuple[tuple[str, str], ...] = (
    ("&", "\\u0026"),
    ("<", "\\u003c"),
    (">", "\\u003e"),
    (" ", "\\u2028"),
    (" ", "\\u2029"),
)


def to_bd_json(rows: Sequence[Mapping[str, Any]]) -> str:
    """Serialize ``rows`` byte-identically to what ``bd ready --json`` / ``bd list --json``
    themselves emit for the same rows — the hard constraint a caller routing `bh work ready` /
    `bh work schedule` through the API rather than `bd` must meet.

    Two encoder defaults disagree in opposite directions and BOTH have to be corrected for:
    Go's `encoding/json` (every `bd` JSON writer) leaves non-ASCII as literal UTF-8 but HTML-escapes
    `<`, `>`, `&`, U+2028 and U+2029 to `\\uXXXX`; Python's `json.dumps` default
    (``ensure_ascii=True``) is the mirror image — it escapes non-ASCII and leaves those five
    characters alone. ``ensure_ascii=False`` undoes the first disagreement; the explicit replace
    pass undoes the second. Key order is NOT a third disagreement: `IssueWithCounts.to_dict()`
    already emits fields in the same order the OpenAPI schema (hence bd's own Go struct) declares
    them, proven byte-identical against a real disposable `bd serve` 1.3.0 (f45b249ce) for rows
    carrying dependencies, labels, multi-line/quoted descriptions, and non-ASCII/emoji/HTML-special
    titles — see `packages/beadhive-core/README.md`'s ready/schedule section and
    `test_core_queue_policy.py`.
    """
    text = json.dumps(list(rows), indent=2, ensure_ascii=False)
    for needle, escaped in _GO_HTML_ESCAPES:
        text = text.replace(needle, escaped)
    return text + "\n"


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


def decline_after(rows: Sequence[Mapping[str, Any]], tried: Sequence[str]) -> str:
    """The decline code for the epic-scoped guarded-claim loop, which CAN try and lose — the
    same three-way split as :func:`beadhive.work_next.decline`: an empty scoped front, a front
    with nothing eligible for this actor, or every tried candidate lost to a racer."""
    if not rows:
        return DECLINE_EMPTY_QUEUE
    if not tried:
        return DECLINE_NONE_ELIGIBLE
    return DECLINE_ALL_LOST


def molecule_scope(
    ready: Sequence[Mapping[str, Any]], members: Sequence[Mapping[str, Any]], epic: str
) -> list[dict[str, Any]]:
    """``ready`` narrowed to one molecule: the epic itself plus ``members`` (its direct children
    by the parent edge), in the ready order Beads returned — a filter that only ever REMOVES rows,
    so the ready predicate stays Beads' alone and a member it did not report is still not
    claimable. Mirrors the CLI-compatibility route's ``_molecule_members`` filter exactly
    (``beadhive.work_dispatch``)."""
    ids = {str(row.get("id") or "") for row in members if isinstance(row, Mapping)}
    ids.add(epic)
    ids.discard("")
    return [dict(row) for row in ready if str(row.get("id") or "") in ids]


def by_parent(rows: Sequence[Mapping[str, Any]], parent: str) -> list[dict[str, Any]]:
    """Rows whose direct ``parent`` field equals ``parent`` (Beads' parent-child edge, one level).

    NOT a recursive molecule-membership traversal — see the module docstring. An empty ``parent``
    returns every row unfiltered.
    """
    if not parent:
        return list(rows)
    return [dict(row) for row in rows if str(row.get("parent") or "") == parent]


def _has_parent_edge(row: Mapping[str, Any], parent: str) -> bool:
    """True iff ``row`` carries a parent edge to ``parent`` in either representation Beads emits:
    a top-level ``parent`` field, or a ``parent-child`` entry in ``dependencies``. Mirrors
    :func:`beadhive.bd._has_parent_edge` exactly, over API rows instead of `bd list --json` rows —
    the two are the same shape (:class:`IssueWithCounts` — see :func:`ready_row`), so the same
    policy applies unchanged. A row's top-level ``parent`` is ABSENT when the parent is closed
    (measured against a real hive: 439 of 723 rows carried no such key), so a reader trusting only
    one representation would decide membership on which field Beads happened to emit for that row.
    """
    if str(row.get("parent") or "") == parent:
        return True
    deps = row.get("dependencies")
    return isinstance(deps, list) and any(
        isinstance(dep, Mapping)
        and dep.get("type") == "parent-child"
        and str(dep.get("depends_on_id") or "") == parent
        for dep in deps
    )


def direct_children(rows: Sequence[Mapping[str, Any]], parent: str) -> list[dict[str, Any]]:
    """``rows`` narrowed to Beads' direct parent-child edge to ``parent`` — one level, trusting the
    edge over either representation Beads reports it in (see :func:`_has_parent_edge`).

    This is the counterpart to :func:`by_parent` for a page fetched with the ``parent`` QUERY
    PARAMETER (``QueueCommands.list_children``'s ``GET /v0/beads/issues?parent=<epic>``), which the
    pinned v1.3 OpenAPI spec documents as "Restrict to recursive descendants of this issue" — NOT
    the one-level edge the ROW field of the same name reports (see the module docstring's
    correction). A caller that wants only an epic's direct children — `bh work schedule`'s
    contract, one dispatch level at a time; a child epic is itself a separate molecule dispatched
    to its own nested seat, never flattened into this one — fetches the recursive page once and
    narrows it locally with this function, rather than trusting the query parameter's name to mean
    what it means on the row.
    """
    if not parent:
        return list(rows)
    return [dict(row) for row in rows if _has_parent_edge(row, parent)]


@dataclass(frozen=True)
class ClaimNextOutcome:
    """The result of one atomic ``work.claim-next`` transaction.

    ``claimed`` is ``None`` exactly when nothing was eligible — mirroring
    ``ClaimNextResponse.claimed``'s own UNSET-is-the-signal contract (see its docstring): there is
    no separate boolean that could disagree with it.
    """

    claimed: dict[str, Any] | None


@dataclass(frozen=True)
class EpicClaimOutcome:
    """The result of one epic-scoped guarded-claim pass, in the CLI-compatibility loop's own
    vocabulary (``beadhive.work_dispatch``): the claimed bead and the seat-qualified actor that
    holds it (both empty when nothing was claimed), every candidate a claim was attempted on,
    every candidate refused for a seat mismatch before any write, the scoped rows the pass chose
    from, and — when nothing was claimed — the decline reason (:data:`EPIC_DECLINES`)."""

    claimed: str = ""
    claim_actor: str = ""
    tried: tuple[str, ...] = ()
    refused: tuple[str, ...] = ()
    rows: tuple[dict[str, Any], ...] = ()
    reason: str = ""


#: Resolves the actor to claim one candidate row under, or ``None`` to refuse it before any write
#: (a seat mismatch). Beadhive's seat rule lives in the shell (``work_dispatch``'s
#: ``_next_seat_actor``); the core loop takes it as a port so there is one rule, not two.
SeatResolver = Callable[[Mapping[str, Any]], str | None]


class QueueCommands:
    """The one place Beadhive calls the ready-selection, claim-next, and children-listing routes.

    Every method goes through :meth:`~beadhive_core.routing.RoutingTable.call_api`, so a missing
    ``ready.list`` / ``issues.claimNext`` / ``issues.list`` capability raises
    :class:`beadhive_beads_client.CapabilityMissing` before any request reaches Beads — the shell
    composition seam catches that (and a session it could not open at all) to select the
    CLI-compatibility route instead, always before execution, never by retrying an API failure
    through `bd`.
    """

    def __init__(self, routing: RoutingTable | None = None) -> None:
        self._routing = routing or default_table()

    def list_ready(
        self,
        session: object,
        *,
        limit: int = 100,
        assignee: str | None = None,
        unassigned: bool | None = None,
        type_: str | None = None,
        exclude_type: list[str] | None = None,
        label: list[str] | None = None,
        label_any: list[str] | None = None,
        exclude_label: list[str] | None = None,
        priority: int | None = None,
        parent: str | None = None,
        has_metadata_key: str | None = None,
        metadata_field: list[str] | None = None,
        observer: RoutingObserver | None = None,
    ) -> ReadyPage:
        """The ready page for the negotiated session, priority-ordered, Beads-authoritative.

        Every keyword beyond ``limit`` is one of `bh work ready`'s narrowing flags, forwarded
        verbatim to ``GET /v0/beads/ready`` (see
        :meth:`beadhive_beads_client.BeadsSession.list_ready` for exactly what each means,
        including ``parent``'s recursive-descendant semantics). Omitted (``None``) keywords are
        not sent, matching bd's own "unset means default" contract.
        """
        return self._routing.call_api(  # type: ignore[return-value]
            session,
            "work.ready.list",
            limit=limit,
            assignee=assignee,
            unassigned=unassigned,
            type_=type_,
            exclude_type=exclude_type,
            label=label,
            label_any=label_any,
            exclude_label=exclude_label,
            priority=priority,
            parent=parent,
            has_metadata_key=has_metadata_key,
            metadata_field=metadata_field,
            observer=observer,
        )

    def list_children(
        self,
        session: object,
        parent: str,
        *,
        observer: RoutingObserver | None = None,
    ) -> list[dict[str, Any]]:
        """``parent``'s DIRECT open children, in `bd list --parent <parent> --limit 0`'s own
        priority order — the fetch `bh work schedule` needs (every non-closed child, not just the
        unblocked ones `list_ready` would report, so a blocked or in-flight child still schedules).

        Beads has no server-side one-level-only children filter, so this asks for every RECURSIVE
        descendant (`GET /v0/beads/issues?parent=<parent>&sort=priority&limit=0` — see
        :meth:`beadhive_beads_client.BeadsSession.list_issues`) and narrows the page to the direct
        edge locally with :func:`direct_children`, reproducing `bd.children`'s exact selection
        without a second process. ``sort=priority`` is required explicitly: this operation's own
        default order is `created`, NOT `bd list`'s flagless `priority` ordering.
        """
        page = self._routing.call_api(
            session, "work.issue.list", parent=parent, sort="priority", limit=0, observer=observer
        )
        rows = [item.to_dict() for item in page.items]  # type: ignore[attr-defined]
        return direct_children(rows, parent)

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

    def molecule_members(
        self, session: object, epic: str, *, observer: RoutingObserver | None = None
    ) -> list[dict[str, Any]]:
        """``epic``'s full membership as the CLI-compatibility route reads it — ``bd list --parent
        <epic> --include-infra --all --limit 0`` narrowed to the direct parent edge: closed rows
        and infra types (gate/event) INCLUDED, one level only (a nested epic is a member; its own
        children are not — see the module docstring). ``all_`` / ``include_infra`` are the
        ``listIssues`` default-exclusion overrides (bh-sy36q.5); :func:`direct_children` narrows
        whatever the server's ``parent`` filter returns (direct-only on bd 1.3.0) to the edge."""
        page = self._routing.call_api(
            session,
            "work.issue.list",
            parent=epic,
            sort="priority",
            limit=0,
            all_=True,
            include_infra=True,
            observer=observer,
        )
        rows = [item.to_dict() for item in page.items]  # type: ignore[attr-defined]
        return direct_children(rows, epic)

    def epic_candidates(
        self, session: object, epic: str, *, observer: RoutingObserver | None = None
    ) -> list[dict[str, Any]]:
        """The ready rows ``bh work next --epic <epic>`` chooses from: the UNBOUNDED ready front
        (``bd ready --limit 0``'s set — a truncated read would silently hide every candidate past
        the window, bh-fruer) narrowed to :meth:`molecule_members` plus the epic itself, ready
        order preserved (:func:`molecule_scope`)."""
        members = self.molecule_members(session, epic, observer=observer)
        ready = ready_rows(self.list_ready(session, limit=0, observer=observer))
        return molecule_scope(ready, members, epic)

    def claim_guarded(
        self, session: object, bead: str, actor: str, *, observer: RoutingObserver | None = None
    ) -> bool:
        """Claim ``bead`` for ``actor`` through ``work.claim.acquire``; true only when ``actor``
        verifiably holds it.

        ``issues.claim`` is a server-side compare-and-set: another holder is a 409
        (``already_claimed``) and a no-longer-claimable bead a 409 (``not_claimable``), each a
        definite lost race reported as ``False``. A 200 is still believed only through
        :func:`claim_won` over the row Beads returns (assignee is ``actor`` AND the bead left
        ``open``); ``already_claimed: true`` on a 200 is the idempotent re-claim of a bead
        ``actor`` already held, which ``claim_won`` accepts. When the write's outcome is unknown
        (:class:`beadhive_beads_client.IndeterminateWrite`) it is reconciled by READING the bead,
        never by replaying the write."""
        try:
            response = self._routing.call_api(
                session, "work.claim.acquire", bead, ClaimRequest(actor=actor), observer=observer
            )
        except ServiceProblem as exc:
            if str(exc.problem.code) in _LOST_CLAIM_CODES:
                return False
            raise
        except IndeterminateWrite:
            detail = self._routing.call_api(session, "work.issue.get", bead, observer=observer)
            return claim_won(detail.to_dict(), actor)  # type: ignore[attr-defined]
        return claim_won(response.issue.to_dict(), actor)  # type: ignore[attr-defined]

    def claim_next_in_epic(
        self,
        session: object,
        epic: str,
        actor: str,
        *,
        seat_actor: SeatResolver | None = None,
        observer: RoutingObserver | None = None,
    ) -> EpicClaimOutcome:
        """Take the next claimable bead in ``epic``'s molecule for ``actor``.

        :meth:`epic_candidates` once, then each :func:`eligible` candidate in ready order:
        ``seat_actor`` resolves the seat-qualified actor for that row (``None`` refuses it with no
        write — a declared seat that mismatches the bead's type), and :meth:`claim_guarded` takes
        it. A lost race moves on to the next candidate; the first verified win ends the pass. The
        outcome is the same vocabulary the CLI-compatibility loop reports, so the caller renders
        either route identically."""
        rows = self.epic_candidates(session, epic, observer=observer)
        resolve = seat_actor or (lambda _row: actor)
        by_id = {str(row.get("id") or ""): row for row in rows}
        tried: list[str] = []
        refused: list[str] = []
        for bead in eligible(rows, actor):
            claim_actor = resolve(by_id.get(bead, {}))
            if claim_actor is None:
                refused.append(bead)
                continue
            tried.append(bead)
            if self.claim_guarded(session, bead, claim_actor, observer=observer):
                return EpicClaimOutcome(
                    claimed=bead,
                    claim_actor=claim_actor,
                    tried=tuple(tried),
                    refused=tuple(refused),
                    rows=tuple(rows),
                )
        return EpicClaimOutcome(
            tried=tuple(tried),
            refused=tuple(refused),
            rows=tuple(rows),
            reason=decline_after(rows, tried),
        )
