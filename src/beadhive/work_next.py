"""`bh work next` — the deterministic decision core, CLI-free and subprocess-free.

Two halves compose in one verb (bh-qczj.1, which absorbed bh-bh2h.2.2):

1. **What to do next** — a 12-row FIRST-MATCH priority table (:func:`decide`) over a molecule's
   beads, plus a loop-breaker that turns "the driver keeps trying the same thing" into a single
   `escalate` with a closed-set reason code. This is what lets an unattended dispatcher exercise
   minimal judgement instead of improvising.
2. **How to take it safely** — the optimistic pick/verify predicates (:func:`eligible`,
   :func:`claim_won`, :func:`decline`) the `bh work next` claim loop drives: `bd update --claim`
   is NOT a hard compare-and-swap, so the caller re-reads the bead and re-verifies the holder,
   retrying the next candidate when it lost the race.

Purity is the point (mirrors `schedule.py` / `molecule.py`): no typer, no `bd`, no git. Inputs are
plain `bd` JSON dicts, outputs are dataclasses. The impure edges — reading `bd ready`, running the
claim, provisioning a worktree (bh-qczj.2) — stay in `work.py`, so the decision table is testable
as a table.

Counting failures — DERIVED, never stored
-----------------------------------------
The loop-breaker fires on the Nth identical `action:bead`, but there is **no attempts counter
anywhere in this module or on disk**. Failure causes are written to beads with
`bd set-state <bead> <dim>=<value> --reason …`, which atomically records an event bead AND
refreshes the `<dim>:<value>` label cache; the count is DERIVED by counting those event beads
(:func:`attempt_count`). A stored counter would be runtime state living outside beads — which the
epic's invariant forbids — and would need a staleness/reconcile rule that derivation does not.
It is already proven on live data: beads in this hive carry repeated `review -> changes-requested`
events, and counting them *is* the retry count.

KNOWN LIMIT, deliberately not papered over: the event record is incomplete today (of 453
`issue_type='gate'` rows only 406 carry a created event and 232 a closed event). A dropped event
makes a derived count UNDER-count, so the loop-breaker fires late or never — it can never fire
early, which is the safe direction to be wrong in. `bh-gj0v9.2` owns classifying that as defect or
by-design; until it lands, treat :func:`attempt_count` as a lower bound.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

# ---- closed sets --------------------------------------------------------------------------

# The 12 priority-table rows, in FIRST-MATCH order. The names are the contract: a caller (or a
# test, or a log line) identifies *which rule fired*, not merely what action came out — two rows
# can emit the same action for very different reasons (`wrap_up` and `dispatch-up-to-budget` both
# dispatch work; only one of them means "the implementation phase is over").
ROWS: tuple[str, ...] = (
    "done",
    "not_dispatchable",
    "halt-on-escalation",
    "start",
    "resume-changes-requested",
    "merge-exactly-one",
    "review",
    "finish",
    "wrap_up",
    "dispatch-up-to-budget",
    "wait",
    "deadlock-escalate",
)

# Every action a decision may carry. A driver that cannot execute one of these must escalate
# rather than substitute its own idea — that substitution is exactly the judgement this table
# exists to remove.
ACTIONS: tuple[str, ...] = (
    "done",
    "halt",
    "start",
    "resume",
    "merge",
    "review",
    "finish",
    "wrap_up",
    "dispatch",
    "wait",
    "escalate",
)

# The CLOSED set of escalation reason codes. `not_dispatchable` / `deadlock` come straight from
# their rows; the other four are what the loop-breaker maps a repeated action to.
REASONS: tuple[str, ...] = (
    "not_dispatchable",
    "deadlock",
    "repeated_changes_requested",
    "repeated_merge_failure",
    "ambiguous_gate",
    "stuck",
)

# Why a pick-claim-verify loop came back empty-handed. Distinct codes because they mean different
# things to a driver: nothing to do (back off and poll) vs lost every race (another worker is
# live, retry immediately is pointless but the hive is healthy).
DECLINE_EMPTY_QUEUE = "empty_queue"  # `bd ready` returned nothing at all
DECLINE_NONE_ELIGIBLE = "none_eligible"  # ready rows existed, none claimable by this actor
DECLINE_ALL_LOST = "all_lost"  # every candidate was claimed out from under us
DECLINES: tuple[str, ...] = (DECLINE_EMPTY_QUEUE, DECLINE_NONE_ELIGIBLE, DECLINE_ALL_LOST)

# `work.dispatch.max_action_retries` default: the SECOND identical action escalates. One retry is
# ordinary (a flaky validate, a rebase); a third attempt at the same thing has never once been the
# thing that worked.
DEFAULT_MAX_ACTION_RETRIES = 2

# The `review` state dimension's label-cache prefix (`bd set-state review=<v>` writes `review:<v>`).
_REVIEW = "review:"
REVIEW_PENDING = "pending"
REVIEW_CHANGES_REQUESTED = "changes-requested"
REVIEW_APPROVED = "approved"

# Labels marking a bead as molecule wrap-up work (changelog / docs / release notes) rather than
# implementation. They are dispatched as their own phase, after the implementation beads close.
WRAP_UP_LABELS = frozenset({"wrap-up", "wrap_up", "phase:wrap-up", "phase:wrap_up"})

# Infra bead types that are never dispatchable work: a gate is a blocker record, an event is the
# audit trail this module *counts*. Claiming either would be a category error.
INFRA_TYPES = frozenset({"gate", "event"})

# Per-action failure signatures found in an event bead's text, and the reason code a repeat of
# that action escalates with. An action absent here is still loop-broken (any repeat is a repeat)
# but maps to the generic `stuck`.
_ACTION_SIGNATURES: dict[str, tuple[tuple[str, ...], str]] = {
    "resume": (("changes-requested", "changes_requested"), "repeated_changes_requested"),
    "merge": (("merge conflict", "merge failed", "merge_failure"), "repeated_merge_failure"),
    "review": (("ambiguous gate", "ambiguous_gate", "multiple review gates"), "ambiguous_gate"),
}


# ---- decision -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Decision:
    """One tick's verdict: which row fired, what to do, and to which beads."""

    row: str
    action: str
    beads: tuple[str, ...] = ()
    reason: str = ""
    detail: str = ""

    def __post_init__(self) -> None:
        # Assert the closed sets at construction: a typo'd row/action/reason is a bug in THIS
        # module, and a driver keying off these strings must never have to defend against one.
        if self.row not in ROWS:
            raise ValueError(f"unknown decision row: {self.row!r}")
        if self.action not in ACTIONS:
            raise ValueError(f"unknown decision action: {self.action!r}")
        if self.reason and self.reason not in REASONS:
            raise ValueError(f"unknown escalation reason: {self.reason!r}")
        if self.action == "escalate" and not self.reason:
            raise ValueError("an escalate decision must carry a reason code")

    def as_dict(self) -> dict:
        """The machine shape (stable key set — absent fields are emitted, not omitted)."""
        return {
            "row": self.row,
            "action": self.action,
            "beads": list(self.beads),
            "reason": self.reason,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Molecule:
    """The decision input: one molecule as plain `bd` JSON, plus the two facts that don't live on
    a bead row — whether the epic passed its dispatch conventions, and this tick's dispatch budget.

    `events` maps bead id -> that bead's `issue_type='event'` infra children. It is the ONLY
    retry-history input; nothing here persists a count (see the module docstring).
    """

    epic: str = ""
    epic_status: str = "in_progress"
    dispatchable: bool = True
    beads: tuple[Mapping, ...] = ()
    events: Mapping[str, Sequence[Mapping]] = field(default_factory=dict)
    escalations: tuple[str, ...] = ()
    budget: int = 1
    max_action_retries: int = DEFAULT_MAX_ACTION_RETRIES


def decide(mol: Molecule) -> Decision:
    """The 12-row FIRST-MATCH priority table. Returns the first row that matches — order IS the
    policy, so read it top to bottom:

    1.  `done` — the epic is closed (or has no children left): the molecule is over.
    2.  `not_dispatchable` — the epic failed its dispatch conventions (no kickoff approval, plan
        doesn't verify). Escalate rather than dispatch into a molecule the tooling will refuse.
    3.  `halt-on-escalation` — an escalation is open against this molecule. A human owns the next
        move; a driver that kept dispatching would be racing the person fixing it.
    4.  `start` — the epic hasn't been started. Nothing else can happen until its container exists.
    5.  `resume-changes-requested` — a bead came back from review. Finishing work already begun
        beats starting more of it, and a changes-requested bead is blocking a merge slot.
    6.  `merge-exactly-one` — an approved bead is waiting. EXACTLY one merges per tick, by design:
        integration is serialized (one merge slot), so a batch would just queue behind itself.
    7.  `review` — a submitted bead is awaiting review. Ahead of dispatch because review is what
        unblocks the merge slot that everything downstream waits on.
    8.  `finish` — every child is closed but the epic is not. Land the assembled molecule.
    9.  `wrap_up` — implementation is done and only wrap-up beads (changelog/docs) remain: a
        distinct phase, so a driver can dispatch it differently (and so "we're nearly done" is
        legible) rather than seeing it as ordinary dispatch.
    10. `dispatch-up-to-budget` — ready beads exist and the in-flight count is under budget:
        dispatch up to the remaining budget, in ready order.
    11. `wait` — work is in flight but nothing is actionable this tick. Not an error: the correct
        move is to do nothing and look again.
    12. `deadlock-escalate` — open work remains, nothing is in flight, and nothing is ready. The
        DAG cannot advance on its own; a human must break it.

    Every bead-naming decision then passes through the loop-breaker (:func:`loop_break`), so a row
    that keeps re-firing on the same bead converts into one `escalate`.
    """
    beads = [b for b in mol.beads if not _is_infra(b)]
    closed = {_bid(b) for b in beads if _is_closed(b)}
    live = [b for b in beads if not _is_closed(b)]

    # 1. done
    if mol.epic_status == "closed" or (mol.epic and not beads):
        return Decision("done", "done", detail=f"{mol.epic or 'molecule'} is complete")
    # 2. not_dispatchable
    if not mol.dispatchable:
        return Decision(
            "not_dispatchable",
            "escalate",
            beads=(mol.epic,) if mol.epic else (),
            reason="not_dispatchable",
            detail="epic failed its dispatch conventions (kickoff / plan verify)",
        )
    # 3. halt-on-escalation
    if mol.escalations:
        return Decision(
            "halt-on-escalation",
            "halt",
            beads=tuple(mol.escalations),
            detail="open escalation — a human owns the next move",
        )
    # 4. start
    if mol.epic and mol.epic_status == "open":
        return Decision("start", "start", beads=(mol.epic,), detail="epic not started")
    # 5. resume-changes-requested
    bounced = _by_review_state(live, REVIEW_CHANGES_REQUESTED)
    if bounced:
        return loop_break(mol, Decision("resume-changes-requested", "resume", beads=bounced[:1]))
    # 6. merge-exactly-one
    approved = _by_review_state(live, REVIEW_APPROVED)
    if approved:
        return loop_break(
            mol,
            Decision(
                "merge-exactly-one",
                "merge",
                beads=approved[:1],
                detail="integration is serialized — one merge per tick",
            ),
        )
    # 7. review
    submitted = _by_review_state(live, REVIEW_PENDING)
    if submitted:
        return loop_break(mol, Decision("review", "review", beads=submitted[:1]))
    # 8. finish
    if mol.epic and not live:
        return Decision("finish", "finish", beads=(mol.epic,), detail="every child is closed")
    # 9. wrap_up
    ready = _ready(live, closed)
    if live and all(is_wrap_up(b) for b in live):
        return loop_break(
            mol,
            Decision(
                "wrap_up",
                "wrap_up",
                beads=tuple(_bid(b) for b in (ready or live)),
                detail="implementation closed — only wrap-up beads remain",
            ),
        )
    # 10. dispatch-up-to-budget
    in_flight = [b for b in live if _is_in_flight(b)]
    room = max(mol.budget - len(in_flight), 0)
    if ready and room:
        return loop_break(
            mol,
            Decision(
                "dispatch-up-to-budget",
                "dispatch",
                beads=tuple(_bid(b) for b in ready[:room]),
                detail=f"{len(in_flight)} in flight, budget {mol.budget}",
            ),
        )
    # 11. wait
    if in_flight:
        return Decision(
            "wait", "wait", detail=f"{len(in_flight)} in flight, nothing else actionable"
        )
    # 12. deadlock-escalate
    return Decision(
        "deadlock-escalate",
        "escalate",
        beads=tuple(_bid(b) for b in live),
        reason="deadlock",
        detail="open beads remain, none ready and none in flight",
    )


def loop_break(mol: Molecule, decision: Decision) -> Decision:
    """Convert a decision the driver has already tried `max_action_retries` times into an
    `escalate` carrying a closed-set reason code.

    The count is DERIVED (:func:`attempt_count`) from the bead's own event children — nothing is
    persisted, incremented, or reconciled here. The threshold is inclusive: with the default 2, the
    decision escalates once the record already shows two identical attempts, i.e. on the third.

    A decision naming several beads (a budgeted dispatch) escalates on the FIRST looping member
    rather than the whole set — the other beads are innocent, and a driver that dropped them would
    turn one stuck bead into a stalled molecule.

    A bead whose review gate is currently open (`review:pending`) is NEVER escalated by a
    non-`review` action here, no matter what its event count says: it is waiting on a human, not
    stuck on dispatch/resume/merge. This is a belt-and-suspenders check alongside the row 7
    (`review`) ordering, not a substitute for it — a bead can only reach here via a stale/
    pre-submit count. The `review` action itself is exempt: a bead can be genuinely stuck IN
    review (`ambiguous_gate`), and that escalation must still fire.
    """
    limit = max(int(mol.max_action_retries), 1)
    by_id = {_bid(b): b for b in mol.beads}
    for bead in decision.beads:
        row = by_id.get(bead)
        if decision.action != "review" and row is not None and review_state(row) == REVIEW_PENDING:
            continue
        seen = attempt_count(mol.events.get(bead) or (), decision.action)
        if seen >= limit:
            _markers, reason = _ACTION_SIGNATURES.get(decision.action, ((), "stuck"))
            return Decision(
                decision.row,
                "escalate",
                beads=(bead,),
                reason=reason,
                detail=f"{decision.action} attempted {seen}x on {bead} (limit {limit})",
            )
    return decision


def attempt_count(events: Iterable[Mapping], action: str) -> int:
    """How many times `action` has already been attempted-and-failed on a bead, DERIVED by counting
    its `issue_type='event'` children whose text carries that action's failure signature.

    A lower bound, honestly: an event that was never written (see the module docstring's known
    limit) simply isn't counted, so the loop-breaker fires late rather than early. An action with
    no registered signature counts every event on the bead — a bead accumulating events while the
    driver re-picks the same action is the same evidence, read coarsely.

    Terminal success ends the sequence: events at or before the most recent `review -> pending`
    (submit) event are excluded, so an earlier failure can never outlive a later success — a bead
    that failed once and then submitted cleanly starts its next dispatch cycle at zero, rather than
    carrying a strike from before the submit forever (bh-7679k).
    """
    events = list(events)
    for i in range(len(events) - 1, -1, -1):
        if _is_success_event(events[i]):
            events = events[i + 1 :]
            break
    markers, _reason = _ACTION_SIGNATURES.get(action, ((), "stuck"))
    total = 0
    for ev in events:
        text = event_text(ev)
        if not markers or any(m in text for m in markers):
            total += 1
    return total


def _is_success_event(ev: Mapping) -> bool:
    """True for the event bead that records a submit (`bd set-state review=pending`) — the
    terminal SUCCESS of a dispatch turn. Mirrors `work._is_review_pending`'s text match."""
    text = event_text(ev)
    return "review" in text and "pending" in text


def event_text(ev: Mapping) -> str:
    """Lower-cased haystack of an event bead's human/text fields (mirrors `work._event_text`)."""
    return " ".join(
        str(ev.get(k) or "") for k in ("title", "description", "reason", "to_state", "state")
    ).lower()


# ---- optimistic pick / claim / verify -------------------------------------------------------


def eligible(rows: Sequence[Mapping], actor: str) -> tuple[str, ...]:
    """The claim candidates from a `bd ready --json` result, in the ready order bd gave us.

    Order is preserved deliberately: `bd ready` is already dependency-ordered (and may be
    release-scored), so re-sorting here would silently override the hive's chosen policy. This
    only FILTERS — infra rows, closed rows, anything already in flight, and anything assigned to
    somebody else. A bead assigned to `actor` stays eligible: re-claiming your own bead is the
    idempotent resume case, not a race.
    """
    out = []
    for row in rows:
        if _is_infra(row) or _is_closed(row) or _is_in_flight(row):
            continue
        assignee = str(row.get("assignee") or "")
        if assignee and assignee != actor:
            continue
        bead = _bid(row)
        if bead:
            out.append(bead)
    return tuple(out)


def claim_won(data: Mapping | None, actor: str) -> bool:
    """Did `actor` actually end up holding this bead? The RE-VERIFY half of pick-claim-verify.

    `bd update --claim` is not a hard compare-and-swap: two drivers can both get exit 0 for the
    same bead, and the last writer wins. So the claim is not believed on its return code — the
    caller re-reads the bead and asks this. Requires BOTH that the assignee is us and that the
    bead actually left `open`; a row that still reads open didn't transition, and treating that as
    a win would hand the caller a bead nothing is holding.
    """
    if not isinstance(data, Mapping):
        return False
    if str(data.get("assignee") or "") != actor:
        return False
    return str(data.get("status") or "") not in ("", "open", "closed")


def decline(rows: Sequence[Mapping], tried: Sequence[str]) -> str:
    """Which decline code a loop that claimed nothing should report (see `DECLINES`)."""
    if not rows:
        return DECLINE_EMPTY_QUEUE
    if not tried:
        return DECLINE_NONE_ELIGIBLE
    return DECLINE_ALL_LOST


# ---- bead row helpers ---------------------------------------------------------------------


def _bid(bead: Mapping) -> str:
    return str(bead.get("id") or "")


def _labels(bead: Mapping) -> list[str]:
    return [str(x) for x in (bead.get("labels") or [])]


def review_state(bead: Mapping) -> str:
    """The bead's `review` dimension value from its `review:<v>` label cache ("" when unset)."""
    for label in _labels(bead):
        if label.startswith(_REVIEW):
            return label[len(_REVIEW) :]
    return ""


def is_wrap_up(bead: Mapping) -> bool:
    """True for molecule wrap-up work (changelog / docs / release notes), not implementation."""
    return any(label in WRAP_UP_LABELS for label in _labels(bead))


def _is_infra(bead: Mapping) -> bool:
    return str(bead.get("issue_type") or "") in INFRA_TYPES


def _is_closed(bead: Mapping) -> bool:
    return str(bead.get("status") or "") == "closed"


def _is_in_flight(bead: Mapping) -> bool:
    return str(bead.get("status") or "") == "in_progress"


def _by_review_state(beads: Sequence[Mapping], value: str) -> tuple[str, ...]:
    return tuple(_bid(b) for b in beads if review_state(b) == value)


def _ready(beads: Sequence[Mapping], closed: set[str]) -> list[Mapping]:
    """Open beads whose every in-molecule dependency is closed. A dependency on a bead outside
    this molecule is not resolvable from this input and is assumed satisfied — `bd ready` is the
    authority on cross-molecule blocking; guessing here would invent a deadlock that isn't one."""
    known = {_bid(b) for b in beads} | closed
    out = []
    for bead in beads:
        if str(bead.get("status") or "") != "open":
            continue
        deps = [str(d) for d in (bead.get("dependencies") or [])]
        if all(d in closed or d not in known for d in deps):
            out.append(bead)
    return out
