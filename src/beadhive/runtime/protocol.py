"""The runtime protocol surface — a LEAF module (bh-03rvd).

Everything a caller needs to drive a runtime, and nothing that resolves one: the transient
:class:`RoleHandle` / :class:`RoleOutcome` values, the :class:`Runtime` protocol, the optional
capability protocols and their detectors, and :func:`schedule_accepts`. It imports no other
``beadhive`` module at run time, so ``beadhive.localloop`` (which implements and drives
runtimes) can depend on it without closing an import cycle through ``beadhive.runtime``'s
registry, whose ``local`` factory imports ``localloop`` lazily. ``beadhive.runtime`` re-exports
every name here; import from there unless you are inside that cycle.
"""

from __future__ import annotations

from dataclasses import KW_ONLY, dataclass
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from ..gate_resolution import GateResolution

# The bead's own no-runtime-only-state invariant (ADR Decision 1) means nothing in this module
# may persist scheduling state anywhere but beads. `RoleHandle`/`RoleOutcome` below are transient
# in-process values a caller holds for the lifetime of one `schedule`/`observe` pair — never
# written to disk, never the source of truth for whether a bead is claimed, blocked, approved,
# or done. That answer always comes from `bd` (gates, leases, the merge slot).


@dataclass(frozen=True)
class RoleHandle:
    """Opaque token identifying one dispatched role run, returned by `schedule()` and passed
    back into `observe()`. Deliberately carries no process internals (no PID, no asyncio Task,
    no Temporal `workflow_id`) — those are tier-specific and must not leak into the protocol
    surface; a tier that needs to correlate a handle to its own bookkeeping does so on its own
    concrete type, which callers only ever see through this seam."""

    bead_id: str
    session_id: str


@dataclass(frozen=True)
class RoleOutcome:
    """Outcome of one role run, as observed via `observe()`. `status` mirrors the vocabulary
    the role-binary contract settles on (`docs/design/work-runtime-tiers-adr.md` Amendment 2
    §1's `SeatRun`/`RoleOutcome.status`: done | blocked | handoff) plus `running`, the
    not-finished-yet case every poll-based tier needs to express, and `failed` for the
    other-exit-code / infra-failure case (Decision 4's *failure vs judgment* split). This is
    bh's OWN result type, not a re-export of baml-harness's `RoleOutcome` — the two are allowed
    to diverge; nothing here depends on baml-harness being installed."""

    status: str  # "running" | "done" | "blocked" | "handoff" | "failed"
    summary: str = ""
    routing: dict | None = None
    # The fields below are ADDITIVE (bh-lhxy8) and KEYWORD-ONLY (bh-03rvd): every one has a
    # default, so a runtime that builds ``RoleOutcome(status=..., summary=...)`` keeps working,
    # positional construction of the first three is unchanged, and adding another field later
    # can never silently shift a positional argument. They carry what the loop's own harvest
    # reads off a ``seatrun.Classification`` (its cause mapping and cause reason), so a caller
    # driving seats through this seam loses nothing the native spawn path had.
    # :meth:`from_classification` is the one mapping that fills them; ``LocalRuntime.observe``
    # and ``outcome.role_outcome`` both go through it.
    _: KW_ONLY
    #: The seat echoed a different ``bead_id`` than it was handed. A failure even when
    #: ``status`` says done: the seat advanced something other than its bead.
    bead_id_mismatch: bool = False
    #: The classifier's prose detail (why a run is ``failed``, or "stdout parsed ...").
    detail: str = ""
    #: The direct child's exit code when the runtime held a process; ``None`` when there was no
    #: process to read one from (an outcome-sink record, a not-yet-finished run).
    exit_code: int | None = None
    #: Cost reported by the seat's ``SeatRun`` or by a killed run's priced envelope.
    cost_usd: float = 0.0
    #: The provider continuation the seat REPORTED (``SeatRun.session_id`` or the envelope's);
    #: ``None`` when the run produced neither. Not the handle's session id: a mismatch between
    #: the two is evidence, not something this type papers over.
    session_id: str | None = None
    #: A killed run's machine-readable ``terminal_reason`` from its envelope, when one survived.
    terminal_reason: str | None = None
    #: A killed run's priced envelope survived (and no ``SeatRun`` parsed). Distinct from
    #: ``session_id is not None``: an envelope may carry ``"session_id": null``, and the loop's
    #: cause reason quotes the envelope (``session None; terminal_reason ...``) either way.
    envelope_present: bool = False
    #: The CANCEL-ladder rung that stopped the run, set only on an outcome returned by
    #: :meth:`CancellableRuntime.cancel` (``cooperative`` / ``hard`` / ``signal``, or ``exited``
    #: when it finished on its own first). The loop records it in ``PassReport.cancelled`` and the
    #: cause reason; ``None`` from a runtime that does not report one.
    cancel_rung: str | None = None

    @classmethod
    def from_classification(
        cls, verdict: Any, *, routing: dict | None = None, exit_code: int | None = None
    ) -> RoleOutcome:
        """Map a ``seatrun.classify_run`` verdict to a :class:`RoleOutcome` — THE mapping.

        ``LocalRuntime.observe`` (the held-pipe path) and ``outcome.role_outcome`` (the
        outcome-sink path) both go through it, and a parity test pins ``observe`` to it field by
        field. It lives on the type, not in either caller, so neither has to import the other
        (``localloop`` -> ``outcome`` would close an import cycle through this module).
        *verdict* is read structurally (``outcome`` / ``seat_run`` / ``envelope`` /
        ``bead_id_mismatch`` / ``detail``), so the seam needs nothing from ``seatrun`` for it.

        A run that produced no parseable ``SeatRun`` is ``failed`` with the classifier's detail
        as its summary; otherwise the seat's own ``status`` and ``summary`` stand. The additive
        fields carry the bead-id mismatch, the detail, the reported session and cost (from the
        ``SeatRun`` or a killed run's envelope) and the envelope's ``terminal_reason``, plus
        *exit_code* when the caller held a process (``None`` for a metadata record)."""
        seat_run, envelope = verdict.seat_run, verdict.envelope
        if seat_run is not None:
            session_id, cost, terminal_reason = seat_run.session_id, seat_run.cost_usd, None
        elif envelope is not None:
            session_id, cost = envelope.session_id, envelope.cost_usd
            terminal_reason = envelope.terminal_reason
        else:
            session_id, cost, terminal_reason = None, 0.0, None
        if str(verdict.outcome) == "incomplete":
            status, summary = "failed", verdict.detail
        else:
            status = str(verdict.outcome)
            summary = seat_run.outcome.summary if seat_run is not None else ""
        return cls(
            status=status,
            summary=summary,
            routing=routing,
            bead_id_mismatch=verdict.bead_id_mismatch,
            detail=verdict.detail,
            exit_code=exit_code,
            cost_usd=cost,
            session_id=session_id,
            terminal_reason=terminal_reason,
            envelope_present=seat_run is None and envelope is not None,
        )


@runtime_checkable
class Runtime(Protocol):
    """The operations `bh` needs from a scheduler. Three verbs, matching the ADR's framing of
    the gap a runtime fills (Evidence 2: "which processes should be running right now, and
    what happens when one dies?") plus the one push bh needs when beads state changes out from
    under a sleeping scheduler. Nothing else — no retry policy, no budget governance, no
    workflow modeling; those stay out of scope for the same reason `engine.Engine` doesn't grow
    a method per tracker verb.

    Cancel, notify, close and session adoption are OPTIONAL capabilities, each its own protocol
    below (:class:`CancellableRuntime`, :class:`NotifiableRuntime`, :class:`ClosableRuntime`,
    :func:`is_adoptable`), so adding them never invalidates a runtime written against these
    three verbs."""

    name: str

    def schedule(
        self,
        bead_id: str,
        role: str,
        *,
        workspace,
        instructions,
        session_id: str,
        model: str | None = None,
        decision=None,
    ) -> RoleHandle:
        """Start (or hand off to a worker that will start) the role binary
        (`bh-<role> --workspace <workspace> --bead <bead_id> --instructions <instructions>
        --session_id <session_id>`, the contract `docs/design/work-runtime-tiers-adr.md`
        Amendment 2 §1 settles) against `bead_id`, and return a handle to observe it by.
        ``decision`` carries the canonical shared routing verdict when the caller has resolved
        one; concrete runtimes translate its model only at their harness launch boundary.
        Idempotent on the caller's side the same way the contract requires the binary itself to
        be: scheduling an already-advanced bead is a no-op a tier is free to detect however it
        likes (immediately-`done` `observe()`, a dedup on `session_id`, ...)."""
        ...

    def observe(self, handle: RoleHandle) -> RoleOutcome:
        """Report what is known right now about the run `handle` identifies. `running` is a
        legitimate, expected answer for a poll-based tier — this is a status read, not a
        blocking wait. Never authoritative about bead lifecycle state; a caller that needs to
        know whether the bead itself is claimed/blocked/approved/done asks `bd`, not this."""
        ...

    def on_gate_resolved(self, resolution: GateResolution) -> None:
        """React to a review gate having resolved (approved or bounced) — e.g. wake a sleeping
        poll loop early, wind a session down, or keep a long-lived one warm and attach
        `resolution.feedback_ref`. The loop's `gate_check` pass calls this for each gate it
        observes resolving: delivery is AT-LEAST-ONCE (an implementation must be idempotent) and
        latency is bounded by `work.dispatch.poll_interval` — there is no push doorbell. A hook
        that raises is logged and never breaks the pass. Implementations must not re-dispatch:
        re-scheduling bounced work is the loop's decision, never the runtime's. A tier that has
        no use for the signal may implement this as a no-op; the point of naming it in the
        protocol is that EVERY tier gets asked, not that every tier must act."""
        ...


# ---- Optional capabilities (bh-lhxy8; operator decision: optional, not required) ---------------
#
# A runtime MAY offer more than the three protocol verbs. Each extra is its own small protocol so
# a caller detects it per runtime instead of assuming it — a third-party runtime that implements
# only ``schedule`` / ``observe`` / ``on_gate_resolved`` stays a valid :class:`Runtime` and passes
# the conformance kit unchanged. Detect with :func:`can_cancel`, :func:`can_notify`,
# :func:`can_close` and :func:`is_adoptable`, never by importing a concrete runtime type.


@runtime_checkable
class CancellableRuntime(Protocol):
    """Can stop a scheduled run on request (the loop's wall-time cap, shutdown)."""

    def cancel(self, handle: RoleHandle) -> RoleOutcome | None:
        """Stop the run ``handle`` names and return its terminal outcome, or ``None`` when the
        runtime holds no run for that handle (unknown bead, or a stale session id). Idempotent:
        cancelling a run that already finished returns its outcome without acting again. A
        runtime with a graded stop reports the step that worked in ``RoleOutcome.cancel_rung``."""
        ...


@runtime_checkable
class NotifiableRuntime(Protocol):
    """Can deliver a one-line message to a live run (sibling notification)."""

    def notify(self, handle: RoleHandle, message: str) -> bool:
        """Deliver ``message`` to the live run ``handle`` names. ``False`` when there is no live
        run to deliver to; never raises for a run that already ended."""
        ...


@runtime_checkable
class ClosableRuntime(Protocol):
    """Owns resources (processes, threads, connections) its caller must release."""

    def close(self) -> None:
        """Release everything the runtime owns. Idempotent."""
        ...


def can_cancel(runtime: object) -> bool:
    """Does *runtime* offer :class:`CancellableRuntime`'s ``cancel(handle)``?"""
    return isinstance(runtime, CancellableRuntime) and callable(getattr(runtime, "cancel", None))


def can_notify(runtime: object) -> bool:
    """Does *runtime* offer :class:`NotifiableRuntime`'s ``notify(handle, message)``?"""
    return isinstance(runtime, NotifiableRuntime) and callable(getattr(runtime, "notify", None))


def can_close(runtime: object) -> bool:
    """Does *runtime* offer :class:`ClosableRuntime`'s ``close()``?"""
    return isinstance(runtime, ClosableRuntime) and callable(getattr(runtime, "close", None))


def is_adoptable(runtime: object) -> bool:
    """Do *runtime*'s sessions outlive the process that scheduled them, so a restarted loop may
    adopt a live handle instead of rescheduling (ADR Decision 4)? Opt-in: only a literal
    ``adoptable = True`` counts; absent or anything else means no."""
    return getattr(runtime, "adoptable", False) is True


def schedule_accepts(runtime: object, keyword: str) -> bool:
    """Does *runtime*'s ``schedule`` take the optional *keyword* (``decision``, or an extension
    such as ``LocalRuntime``'s ``action``)? Read off the signature, so a caller passes an extra
    only to a runtime that declares it (or takes ``**kwargs``) and never needs the concrete type.
    ``False`` when the signature cannot be read."""
    import inspect

    try:
        params = inspect.signature(runtime.schedule).parameters  # type: ignore[attr-defined]
    except (AttributeError, TypeError, ValueError):
        return False
    if keyword in params:
        return params[keyword].kind in (
            inspect.Parameter.KEYWORD_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
    return any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values())
