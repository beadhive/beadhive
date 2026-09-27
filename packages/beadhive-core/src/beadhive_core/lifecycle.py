"""Assignment and claim lifecycle: Beadhive work-state policy over named Beads routes.

This module owns the policy of the ``assign``, ``claim``, ``resume``, ``abandon`` verbs and the
bead-state half of ``submit`` — which actor may take which bead, when a claim counts as won, what
must be undone when provisioning fails after a claim, when a release really released, and how
every outcome reads to an operator. It never performs a worktree, Git, validation or identity
effect itself: those are root-supplied capabilities behind :class:`Workspace` (the shell realises
them with the selected ``worktree.manager`` from ``beadhive-worktrees``), and the core only
decides *when* each one runs.

Every Beads operation takes exactly one named route from the bh-97fo0.3 matrix
(:mod:`beadhive_core.routing`):

* ``work.issue.get`` / ``work.issue.update`` are **api-ready**. :class:`SessionIssues` serves
  them over :class:`beadhive_beads_client.BeadsSession`; an assignment is a guarded update
  (``expected_version`` from the guard read), so a bead that changed between the guard and the
  write is refused rather than overwritten.
* ``work.lease.acquire`` / ``work.lease.release`` are **cli-compatibility**: ``issues.claim``
  does not grant the renewable CLI claim lease, and ``issues.release`` does not implement the
  complete Beadhive lease release. They are the :class:`Leases` port.
* ``work.state.get`` / ``work.state.update`` are **cli-compatibility** (no state-dimension route
  in v1.3): :class:`StateReads` and :class:`beadhive_core.review.StateOperations`.
* ``work.gate.lookup`` / ``work.gate.resolve`` are **cli-compatibility**:
  :class:`beadhive_core.review.GateOperations`.

The route constants below are resolved through the routing table at import time, so this module
fails loudly the day the installed matrix reclassifies one of them (the port would then be the
wrong seam, not a silently stale literal).

Output is streamed through :class:`Output` rather than collected: shell capabilities (worktree
provisioning, the dispatch convention gate, state sync) print their own lines between the
core's, and the operator must see them in the order they happened.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn, Protocol

from beadhive_beads_client import (
    BeadsSession,
    IncompatibleService,
    IndeterminateWrite,
    ServiceProblem,
    SessionTimeout,
)
from beads_v1_3.models import IssueDetails, IssuePatchBody, UpdateIssueRequest

from .review import (
    GATE_ROUTES,
    GateLookupFailed,
    GateOperations,
    GateResolveFailed,
    StateOperations,
    StateUpdateFailed,
)
from .routing import ApiRoute, RoutingObserver, default_table


def _api(name: str) -> str:
    route = default_table().route(name)
    if not isinstance(route, ApiRoute):
        raise RuntimeError(f"{name} is {route.kind.value}, not api-ready")
    return route.name


#: The api-ready issue routes :class:`SessionIssues` serves over ``BeadsSession``.
ISSUE_ROUTES = tuple(_api(name) for name in ("work.issue.get", "work.issue.update"))
#: The claim-lease rows the :class:`Leases` port stands in for.
LEASE_ROUTES = tuple(
    default_table().select_cli(name).name for name in ("work.lease.acquire", "work.lease.release")
)
#: The state-dimension rows :class:`StateReads` / ``StateOperations`` stand in for here.
LIFECYCLE_STATE_ROUTES = tuple(
    default_table().select_cli(name).name for name in ("work.state.get", "work.state.update")
)
#: Every named route this cohort touches, for traceability (see the package README).
LIFECYCLE_ROUTES = ISSUE_ROUTES + LEASE_ROUTES + LIFECYCLE_STATE_ROUTES + GATE_ROUTES

#: Capabilities a lifecycle session must negotiate before any read or write is attempted.
LIFECYCLE_CAPABILITIES = frozenset({"project.enforce", "issues.get", "issues.update"})

REVIEW_DIMENSION = "review"
DISPATCH_DIMENSION = "dispatch"
PROVISIONING_FAILED = "provisioning_failed"

DISP_PREFIX = "disp/"
DEV_PREFIX = "dev/"
DIRECTOR_PREFIX = "dir/"
#: Deprecated seat prefixes still honoured (roles/RBAC matrix rename coord/->disp/, crew/->dev/).
LEGACY_SEAT_PREFIXES: Mapping[str, tuple[str, str]] = {
    "coord/": ("dispatcher", DISP_PREFIX),
    "crew/": ("developer", DEV_PREFIX),
}
KNOWN_SEAT_PREFIXES = frozenset(
    {
        "super/",
        "dir/",
        "cust/",
        "ctrl/",
        "plan/",
        "analyst/",
        "disp/",
        "dev/",
        "rev/",
        "merge/",
        "warden/",
        "verify/",
        "release/",
        "contrib/",
        "ops/",
        "coord/",
        "crew/",
    }
)

#: A bead as the routes report it: the ``bd show --json`` / ``IssueDetails`` object shape.
Issue = Mapping[str, Any]


# ---- failures ------------------------------------------------------------------------------


class LifecycleFailed(Exception):
    """A fail-closed refusal. Every line to show was already streamed through :class:`Output`."""

    def __init__(self, exit_code: int = 1) -> None:
        self.exit_code = exit_code or 1
        super().__init__(f"lifecycle command failed (exit {self.exit_code})")


class IssueReadFailed(RuntimeError):
    """The issue route could not tell whether the bead exists (never "no such bead")."""


class WriteFailed(RuntimeError):
    """A route refused or failed one write.

    ``detail`` is empty when the route already reported the failure itself (a streamed ``bd``
    error), so the core adds nothing; otherwise it is the operator line to show.
    """

    def __init__(self, exit_code: int = 1, detail: str = "") -> None:
        self.exit_code = exit_code or 1
        self.detail = detail
        super().__init__(detail or f"write failed (exit {self.exit_code})")


class Refused(RuntimeError):
    """A shell-supplied policy gate (the dispatch convention gate) refused and reported why."""

    def __init__(self, exit_code: int = 1) -> None:
        self.exit_code = exit_code or 1
        super().__init__(f"refused (exit {self.exit_code})")


# ---- ports ---------------------------------------------------------------------------------


class Issues(Protocol):
    """``work.issue.get`` / ``work.issue.update`` (assignee)."""

    def get(self, bead: str) -> Issue | None:
        """The bead, or ``None`` when it does not exist; :class:`IssueReadFailed` otherwise."""
        ...

    def assign(self, bead: str, assignee: str, *, actor: str, read: Issue) -> None:
        """Set the assignee, guarded by the ``read`` the caller's guards ran on where the route
        supports it; raise :class:`WriteFailed`."""
        ...


class Leases(Protocol):
    """``work.lease.acquire`` / ``work.lease.release`` — the renewable CLI claim lease."""

    def acquire(self, bead: str, *, actor: str) -> None:
        """Claim ``bead`` for ``actor`` (→ in_progress); raise :class:`WriteFailed`. Not a
        compare-and-swap: the caller re-reads to learn whether it actually won."""
        ...

    def release(self, bead: str, *, actor: str) -> None:
        """Reopen and unassign ``bead``; raise :class:`WriteFailed`."""
        ...


class StateReads(Protocol):
    """``work.state.get``: the current value of one Beadhive state dimension ('' if unset)."""

    def get_state(self, bead: str, dimension: str) -> str: ...


@dataclass(frozen=True)
class Provisioned:
    """A provisioned or re-attached checkout. ``handle`` is opaque shell context (the registry
    entry the capability resolved), passed back to the same shell on later calls."""

    target: Path
    branch: str = ""
    handle: Any = None


class Workspace(Protocol):
    """Root-supplied operational capabilities. The core decides when; the shell does the work.

    Worktree mechanics go through the selected ``worktree.manager``; identity stamping, the
    claim-authority record, the Beads state sync, and the dispatch convention gate stay shell
    policy. Any exception a capability raises propagates unchanged after the core's cleanup.
    """

    def refresh(self) -> None:
        """Best-effort pull of remote bead state before acting on it."""
        ...

    def publish(self, actor: str, message: str) -> None:
        """Best-effort push of local bead state after a mutation."""
        ...

    def dispatch_gate(self, issue: Issue, bead: str) -> None:
        """Enforce the molecule's dispatch conventions; raise :class:`Refused`."""
        ...

    def open_container(self, bead: str) -> None:
        """Open (and refresh) a kicked-off epic's container before a child is provisioned."""
        ...

    def provision(self, bead: str, kind: str) -> Provisioned:
        """Create or attach the bead's worktree (``kind`` '' lets the shell infer it)."""
        ...

    def batch_checkout(self, group: str) -> Provisioned | None:
        """The shared ``wt/batch/<group>`` checkout, or ``None`` when it is absent."""
        ...

    def stamp(self, checkout: Provisioned, actor: str) -> None:
        """Stamp ``actor``'s commit identity and signing onto the checkout."""
        ...

    def record_claim(self, bead: str, actor: str, checkout: Provisioned) -> None:
        """Persist the fenced claim record naming ``actor`` as the checkout's holder."""
        ...

    def remove(self, bead: str) -> bool:
        """Remove the bead's worktree if it exists; return whether one existed."""
        ...


class Output(Protocol):
    """Operator output, streamed in order with the shell capabilities' own lines."""

    def say(self, text: str, *, error: bool = False) -> None: ...

    def feedback(self, bead: str) -> None:
        """Render the bead's review feedback (shell presentation of the comment thread)."""
        ...


class LifecycleObserver(Protocol):
    """Telemetry for completed transitions, agent dispatch spans and deprecated seat names."""

    def transition(self, name: str) -> None: ...

    def dispatch(self, *, agent: str, bead: str, brief: str) -> AbstractContextManager[None]: ...

    def legacy_seat(self, *, deprecated: str, replacement: str, seat: str) -> None: ...


class NullLifecycleObserver:
    def transition(self, name: str) -> None:
        return None

    @contextmanager
    def dispatch(self, *, agent: str, bead: str, brief: str) -> Iterator[None]:
        yield

    def legacy_seat(self, *, deprecated: str, replacement: str, seat: str) -> None:
        return None


# ---- the api-ready issue route -------------------------------------------------------------


class SessionIssues:
    """:class:`Issues` over an open ``BeadsSession`` — the api-ready route."""

    route = "api-ready"

    def __init__(self, session: BeadsSession, *, observer: RoutingObserver | None = None) -> None:
        self._session = session
        self._observer = observer

    def get(self, bead: str) -> Issue | None:
        try:
            issue = default_table().call_api(
                self._session, "work.issue.get", bead, observer=self._observer
            )
        except ServiceProblem as exc:
            if exc.problem.code == "not_found":
                return None
            raise IssueReadFailed(f"Beads refused reading {bead}: {exc}") from exc
        except (SessionTimeout, IncompatibleService, RuntimeError) as exc:
            raise IssueReadFailed(f"could not read {bead} from Beads: {exc}") from exc
        if not isinstance(issue, IssueDetails):
            raise IssueReadFailed(f"unexpected {type(issue).__name__} reading {bead}")
        return issue.to_dict()

    def assign(self, bead: str, assignee: str, *, actor: str, read: Issue) -> None:
        revision = str(read.get("revision") or "")
        request = UpdateIssueRequest(
            actor=actor,
            patch=IssuePatchBody(assignee=assignee),
            **({"expected_version": revision} if revision else {}),
        )
        try:
            default_table().call_api(
                self._session, "work.issue.update", bead, request, observer=self._observer
            )
        except IndeterminateWrite as exc:
            raise WriteFailed(
                detail=f"assign {bead}: outcome unknown — the write may have committed; read the "
                "bead before retrying (never replayed automatically)"
            ) from exc
        except ServiceProblem as exc:
            if exc.problem.code == "precondition_failed":
                raise WriteFailed(
                    detail=f"{bead} changed since it was read — not assigned; re-run the assign"
                ) from exc
            raise WriteFailed(detail=f"assign {bead} refused by Beads: {exc}") from exc
        except (SessionTimeout, IncompatibleService, RuntimeError) as exc:
            raise WriteFailed(detail=f"assign {bead} failed: {exc}") from exc


# ---- pure policy ---------------------------------------------------------------------------


def text(issue: Issue | None, key: str) -> str:
    value = (issue or {}).get(key)
    return "" if value is None else str(value)


def is_epic(issue: Issue | None) -> bool:
    return text(issue, "issue_type") == "epic"


def kind_of(issue: Issue | None) -> str:
    return "epic" if is_epic(issue) else "issue"


def batch_group(issue: Issue | None) -> str:
    """The ``<group>`` of the bead's ``batch:<group>`` label ('' when it carries none)."""
    for label in (issue or {}).get("labels") or []:
        if str(label).startswith("batch:"):
            return str(label)[len("batch:") :]
    return ""


def claim_won(issue: Issue | None, actor: str) -> bool:
    """Did ``actor`` actually end up holding this bead? The re-verify half of claim-then-read.

    The claim lease write is not a compare-and-swap, so a claim is never believed on its exit
    code: the assignee must be ``actor`` AND the bead must have left ``open`` (a row still open
    did not transition; a closed one is not held).
    """
    if not isinstance(issue, Mapping):
        return False
    if text(issue, "assignee") != actor:
        return False
    return text(issue, "status") not in ("", "open", "closed")


def claim_residue(issue: Issue | None) -> str:
    """What of a claim survived its release ('' when the bead is genuinely free).

    The mirror of :func:`claim_won`: giving a claim back is not believed on exit codes either.
    """
    if not isinstance(issue, Mapping):
        return "the bead could not be re-read, so its claim state is unknown"
    residue = []
    status = text(issue, "status")
    if status not in ("", "open"):
        residue.append(f"status is still {status}")
    holder = text(issue, "assignee")
    if holder:
        residue.append(f"still assigned to {holder}")
    return "; ".join(residue)


def batch_member_procedure(bead: str, group: str, cli_name: str = "bh") -> str:
    """The refusal a per-bead verb on a batch member gets: a batch member has no per-bead
    worktree; the batch lives in ONE shared ``wt/batch/<group>`` checkout and completes as a unit.
    """
    return (
        f"✗ {bead} is a batch member (batch:{group}) — it has no per-bead worktree.\n"
        f"  Batch work happens in the ONE shared worktree wt/batch/{group} and completes as a "
        f"UNIT:\n      {cli_name} work submit --group <ids>   # one review gate for the whole "
        f"batch\n      {cli_name} work merge --group <ids>    # after approval"
    )


@dataclass(frozen=True)
class LifecyclePolicy:
    """Shell-resolved policy inputs (the core never reads config or environment)."""

    cli_name: str = "bh"


# ---- results -------------------------------------------------------------------------------


@dataclass(frozen=True)
class ClaimOutcome:
    bead: str
    actor: str
    issue: Issue
    disposition: str  # "claimed" | "reattached"
    checkout: Provisioned


@dataclass(frozen=True)
class AssignOutcome:
    bead: str
    assignee: str
    checkout: Provisioned


@dataclass(frozen=True)
class ResumeOutcome:
    bead: str
    actor: str
    checkout: Provisioned
    group: str = ""


@dataclass(frozen=True)
class AbandonOutcome:
    bead: str
    removed: bool


# ---- handlers ------------------------------------------------------------------------------


class LifecycleCommands:
    """Thin ``assign`` / ``claim`` / ``resume`` / ``abandon`` handlers plus submit's bead state.

    Guards read before the first mutation, so a refusal leaves nothing half-done. A claim that
    was taken and then could not be provisioned is released — but only after an authoritative
    re-read proves this actor still holds it, so a concurrent winner is never clobbered.
    """

    def __init__(
        self,
        issues: Issues,
        leases: Leases,
        states: StateOperations,
        state_reads: StateReads,
        gates: GateOperations,
        workspace: Workspace,
        output: Output,
        *,
        observer: LifecycleObserver | None = None,
        policy: LifecyclePolicy | None = None,
    ) -> None:
        self._issues = issues
        self._leases = leases
        self._states = states
        self._state_reads = state_reads
        self._gates = gates
        self._workspace = workspace
        self._out = output
        self._observer = observer or NullLifecycleObserver()
        self._policy = policy or LifecyclePolicy()

    # -- seats --

    def seat_of(self, name: str) -> str:
        """``dispatcher`` / ``developer`` for a seat-prefixed actor, '' for any other name."""
        if name.startswith(DISP_PREFIX):
            return "dispatcher"
        if name.startswith(DEV_PREFIX):
            return "developer"
        for legacy, (seat, replacement) in LEGACY_SEAT_PREFIXES.items():
            if name.startswith(legacy):
                self._observer.legacy_seat(deprecated=legacy, replacement=replacement, seat=seat)
                return seat
        return ""

    @staticmethod
    def is_orchestrator(name: str) -> bool:
        if name.startswith(DISP_PREFIX) or name.startswith(DIRECTOR_PREFIX):
            return True
        return any(
            name.startswith(prefix) and seat == "dispatcher"
            for prefix, (seat, _) in LEGACY_SEAT_PREFIXES.items()
        )

    @staticmethod
    def names_a_seat(name: str) -> bool:
        return any(name.startswith(prefix) for prefix in KNOWN_SEAT_PREFIXES)

    # -- assign --

    def assign(self, bead: str, assignee: str, actor: str) -> AssignOutcome:
        """Orchestrator-only: stamp the assignee and provision its worktree. Leaves status open."""
        self._guard_orchestrator(actor, bead)
        issue = self._read_open(bead)
        self._guard_not_other(issue, assignee, bead)
        self._guard_seat(issue, assignee, bead, verb="assigned to")
        self._dispatch_gate(issue, bead)
        with self._observer.dispatch(
            agent=assignee, bead=bead, brief=text(issue, "description") or ""
        ):
            self._write(lambda: self._issues.assign(bead, assignee, actor=actor, read=issue))
            self._workspace.publish(actor, f"assign {bead} -> {assignee}")
            self._workspace.open_container(bead)
            checkout = self._workspace.provision(bead, kind_of(issue))
            self._workspace.stamp(checkout, assignee)
        self._observer.transition("assigned")
        self._out.say(f"✓ assigned {bead} → {assignee}; worktree {checkout.target}")
        return AssignOutcome(bead, assignee, checkout)

    # -- claim --

    def claim(self, bead: str, actor: str) -> ClaimOutcome:
        """Claim one bead and return its provisioning envelope (no success line: the caller
        renders it, so composite callers can consume the envelope silently)."""
        self._workspace.refresh()
        issue = self._read_open(bead)
        self._guard_not_other(issue, actor, bead)
        self._guard_seat(issue, actor, bead, verb="claimed by")
        already_held = claim_won(issue, actor)
        if not already_held:
            self._dispatch_gate(issue, bead)
            self._workspace.open_container(bead)
            self._write(lambda: self._leases.acquire(bead, actor=actor))
            reread = self._read(bead)
            if reread is None or not claim_won(reread, actor):
                raise LifecycleFailed(1)
            issue = reread
        try:
            # Provisioning is idempotent and may reattach an existing checkout for a same-actor
            # retry.
            checkout = self._workspace.provision(bead, kind_of(issue))
            self._workspace.stamp(checkout, actor)
            self._workspace.record_claim(bead, actor, checkout)
        except Exception as exc:
            if not already_held:
                self.release_if_held(bead, actor, detail=str(exc))
            raise
        # Provisioning can race with a reassignment too: re-read after BOTH fresh and idempotent
        # paths so the returned envelope carries authoritative ownership.
        final = self._read(bead)
        if final is None or not claim_won(final, actor):
            raise LifecycleFailed(1)
        self._observer.transition("claimed")
        return ClaimOutcome(
            bead, actor, final, "reattached" if already_held else "claimed", checkout
        )

    def try_claim(self, bead: str, actor: str) -> bool:
        """Take the lease, then RE-VERIFY by re-reading: true only when ``actor`` holds it.

        A refused lease means the race was lost outright; an accepted one proves nothing (the
        lease write is not a compare-and-swap), so the verdict comes from the re-read alone.
        """
        try:
            self._leases.acquire(bead, actor=actor)
        except WriteFailed:
            return False
        try:
            return claim_won(self._issues.get(bead), actor)
        except IssueReadFailed:
            return False

    def provision_claim(self, bead: str, actor: str) -> Provisioned:
        """Provision a just-won claim's checkout, stamp it and record the holder.

        Any failure releases the claim before re-raising, so a bead never sits claimed with no
        worktree behind it.
        """
        try:
            issue = self._issues.get(bead)
            checkout = self._workspace.provision(bead, kind_of(issue))
            self._workspace.stamp(checkout, actor)
            self._workspace.record_claim(bead, actor, checkout)
        except Exception as exc:
            self.release(bead, actor, detail=str(exc))
            raise
        return checkout

    def release_if_held(self, bead: str, actor: str, *, detail: str = "") -> bool:
        """Undo a claim whose provisioning failed — only when a re-read proves ``actor`` still
        holds it (a provisioning error may race a reassignment; never clobber the winner)."""
        try:
            current = self._issues.get(bead)
        except IssueReadFailed:
            current = None
        if not claim_won(current, actor):
            return False
        self.release(bead, actor, detail=detail)
        return True

    def release(self, bead: str, actor: str, *, detail: str = "") -> None:
        """Release a just-taken claim and record why, under the ``dispatch`` dimension.

        A provisioning failure is an infrastructure failure on the dispatch path, not a review
        outcome, so it is never filed under ``review``. Best-effort, like the claim it undoes:
        the provisioning error that triggered it is what the caller reports.
        """
        reason = detail or PROVISIONING_FAILED
        try:
            self._states.set_state(
                bead, DISPATCH_DIMENSION, PROVISIONING_FAILED, reason=reason, actor=actor
            )
        except StateUpdateFailed:
            pass
        try:
            self._leases.release(bead, actor=actor)
        except WriteFailed:
            pass

    # -- resume --

    def resume(self, bead: str, actor: str) -> ResumeOutcome:
        """After changes-requested: GC orphaned review gates, re-attach the checkout, show the
        feedback, and re-assert the claim."""
        self._workspace.refresh()
        state = self._state_reads.get_state(bead, REVIEW_DIMENSION)
        if state != "changes-requested":
            self._fail(f"✗ {bead} not in review:changes-requested (now: {state or 'none'})")
        # A raw ``set-state`` bounce leaves the review gate open; resolve it here so a same-sha
        # resubmit cannot resurrect a stale gate that would deadlock merge against approve.
        self._resolve_orphaned_review_gates(bead, actor)
        issue = self._read(bead)
        group = batch_group(issue)
        if group:
            # A batch member re-attaches the ONE shared worktree and never provisions its own;
            # the group's claim stands, so no per-bead claim record is written.
            checkout = self._workspace.batch_checkout(group)
            if checkout is None:
                self._fail(batch_member_procedure(bead, group, self._policy.cli_name))
        else:
            checkout = self._workspace.provision(bead, "")
        self._workspace.stamp(checkout, actor)
        if not group:
            self._workspace.record_claim(bead, actor, checkout)
        self._out.say("── review feedback ──")
        self._out.feedback(bead)
        try:
            self._leases.acquire(bead, actor=actor)
        except WriteFailed as exc:
            # Re-asserting an already-held claim; the route streamed its own error.
            if exc.detail:
                self._out.say(f"⚠ {exc.detail}", error=True)
        self._out.say(f"✓ resumed {bead} as {actor}; worktree {checkout.target}")
        return ResumeOutcome(bead, actor, checkout, group)

    def _resolve_orphaned_review_gates(self, bead: str, actor: str) -> None:
        try:
            gates = self._gates.gates_for(bead)
        except GateLookupFailed as exc:
            self._out.say(
                f"⚠ could not list review gates for {bead} ({exc}); a gate orphaned by a raw "
                "bounce may still be open",
                error=True,
            )
            return
        for gate in gates:
            if not (gate.is_review and gate.is_open):
                continue
            try:
                self._gates.resolve(
                    gate.id, reason="orphaned by bounce — cleared on resume", actor=actor
                )
            except GateResolveFailed:
                self._out.say(f"⚠ could not resolve orphaned review gate {gate.id}", error=True)

    # -- abandon --

    def abandon(self, bead: str, actor: str, *, remove: bool = False) -> AbandonOutcome:
        """Release the claim and record the abandon, then RE-READ to prove it.

        Deliberately no refuse-if-other guard: this is the recovery path for a bead a stalled or
        dead agent left claimed. Route failures are surfaced instead of reporting success, and a
        release that did not take names the remaining step rather than printing a bare ✓.
        """
        failed = False
        try:
            self._states.set_state(
                bead, REVIEW_DIMENSION, "abandoned", reason="abandoned", actor=actor
            )
        except StateUpdateFailed:
            failed = True
        try:
            self._leases.release(bead, actor=actor)
        except WriteFailed:
            failed = True
        removed = self._workspace.remove(bead) if remove else False
        if failed:
            self._fail(f"⚠ abandoned {bead} with bd errors (see above)")
        try:
            current = self._issues.get(bead)
        except IssueReadFailed:
            current = None
        residue = claim_residue(current)
        if residue:
            self._fail(
                f"⚠ {bead}: review=abandoned was recorded, but the claim was NOT released "
                f"({residue}).\n"
                f"  The bead is still held, so nothing else can take it. Release it with:\n"
                f"    bh bd reclaim            # reverts claims whose lease has expired\n"
                f"    bh bd update {bead} --status open --assignee ''   # or force it directly"
            )
        self._observer.transition("abandoned")
        kept = "; worktree removed" if remove else "; worktree kept"
        self._out.say(f"✓ abandoned {bead}{kept}")
        return AbandonOutcome(bead, removed)

    # -- submit state --

    def admit_submission(self, bead: str, actor: str) -> Issue:
        """The pre-mutation bead read submit's policy checks share, guarded so only the current
        claim holder may hand off (an abandoned or reassigned bead is refused, not adopted)."""
        issue = self._read(bead)
        holder = text(issue, "assignee")
        if holder != actor:
            held = f"held by {holder}" if holder else "not currently claimed"
            self._fail(
                f"✗ bead {bead} is {held} — {actor} no longer holds the claim; "
                "not submitting (re-claim it first)"
            )
        return issue or {}

    def mark_submitted(self, bead: str, sha: str, actor: str) -> None:
        """Move review to pending once the review gate is open (never before: a bead must not
        read review=pending with nothing blocking it)."""
        try:
            self._states.set_state(
                bead, REVIEW_DIMENSION, "pending", reason=f"submitted {sha}", actor=actor
            )
        except StateUpdateFailed as exc:
            self._fail("✗ failed to set review state — nothing submitted", exc.exit_code)

    # -- shared steps --

    def _fail(self, line: str, exit_code: int = 1) -> NoReturn:
        self._out.say(line, error=True)
        raise LifecycleFailed(exit_code)

    def _read(self, bead: str) -> Issue | None:
        try:
            return self._issues.get(bead)
        except IssueReadFailed as exc:
            self._fail(f"✗ {exc}")

    def _read_open(self, bead: str) -> Issue:
        issue = self._read(bead)
        if issue is None:
            self._fail(f"✗ no such bead: {bead}")
        if text(issue, "status") == "closed":
            self._fail(f"✗ bead {bead} is closed")
        return issue

    def _guard_not_other(self, issue: Issue, actor: str, bead: str) -> None:
        current = text(issue, "assignee")
        if current and current != actor:
            self._fail(f"✗ bead {bead} assigned to {current} (not {actor}) — refusing to steal")

    def _guard_seat(self, issue: Issue, name: str, bead: str, *, verb: str) -> None:
        want = "dispatcher" if is_epic(issue) else "developer"
        if self.seat_of(name) in ("", want):
            return
        prefix = DISP_PREFIX if want == "dispatcher" else DEV_PREFIX
        self._fail(
            f"✗ {bead} is an {kind_of(issue)} — it may only be {verb} a {want} ({prefix}<name>), "
            f"not {name!r}"
        )

    def _guard_orchestrator(self, actor: str, bead: str) -> None:
        if self.is_orchestrator(actor) or not self.names_a_seat(actor):
            return
        self._fail(
            f"✗ {bead}: `{self._policy.cli_name} work assign` is orchestrator-only — "
            "only a dispatcher (disp/<name>) or "
            f"director (dir/<name>) may assign work, not {actor!r}."
        )

    def _dispatch_gate(self, issue: Issue, bead: str) -> None:
        try:
            self._workspace.dispatch_gate(issue, bead)
        except Refused as exc:
            raise LifecycleFailed(exc.exit_code) from exc

    def _write(self, call: Callable[[], object]) -> None:
        try:
            call()
        except WriteFailed as exc:
            if exc.detail:
                self._out.say(f"✗ {exc.detail}", error=True)
            raise LifecycleFailed(exc.exit_code) from exc


__all__ = [
    "DISPATCH_DIMENSION",
    "ISSUE_ROUTES",
    "LEASE_ROUTES",
    "LIFECYCLE_CAPABILITIES",
    "LIFECYCLE_ROUTES",
    "LIFECYCLE_STATE_ROUTES",
    "PROVISIONING_FAILED",
    "AbandonOutcome",
    "AssignOutcome",
    "ClaimOutcome",
    "Issue",
    "IssueReadFailed",
    "Issues",
    "Leases",
    "LifecycleCommands",
    "LifecycleFailed",
    "LifecycleObserver",
    "LifecyclePolicy",
    "NullLifecycleObserver",
    "Output",
    "Provisioned",
    "Refused",
    "ResumeOutcome",
    "SessionIssues",
    "StateReads",
    "Workspace",
    "WriteFailed",
    "batch_group",
    "batch_member_procedure",
    "claim_residue",
    "claim_won",
    "is_epic",
    "kind_of",
]
