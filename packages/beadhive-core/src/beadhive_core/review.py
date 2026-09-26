"""Review approval and bounce: Beadhive policy over the pinned Beads v1.3 session.

Beadhive owns only the review policy here — which gate an actor may resolve, which review
transitions are allowed, and how each outcome reads to an operator. Durable issue behavior is
Beads': the bead read, the review-label cleanup and the bounce feedback comment travel over
:class:`beadhive_beads_client.BeadsSession`.

Beads v1.3 has no gate lookup/create/resolve route and no state-dimension route (the
``work.gate.*`` and ``work.state.update`` rows of the installed operation matrix are
``cli-compatibility``). Those two concerns are therefore narrow ports — :class:`GateOperations`
and :class:`StateOperations` — that the compatibility shell implements over its named CLI routes.
Gate semantics are never fabricated from generic issue operations: no generic close stands in
for a gate resolution, and nothing here creates a gate.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from typing import Literal, Protocol

from beadhive_beads_client import (
    BeadsSession,
    IncompatibleService,
    IndeterminateWrite,
    ServiceProblem,
    SessionTimeout,
)
from beadhive_beads_client.service import ServiceError
from beads_v1_3.models import (
    AddCommentRequest,
    IssueDetails,
    IssuePatchBody,
    UpdateIssueRequest,
)
from beads_v1_3.types import UNSET

#: Operation-matrix rows the ports stand in for. Every one must stay a named CLI route; a test
#: fails if the installed matrix ever reclassifies one (the port would then be the wrong seam).
GATE_ROUTES = ("work.gate.lookup", "work.gate.resolve")
STATE_ROUTES = ("work.state.update",)

#: Capabilities a review session must negotiate before any read or write is attempted.
REVIEW_CAPABILITIES = frozenset(
    {
        "project.enforce",
        "issues.get",
        "issues.list",
        "ready.list",
        "issues.update",
        "issues.addComment",
    }
)

REVIEW_DIMENSION = "review"
CHANGES_REQUESTED = "changes-requested"
APPROVED = "approved"

# A convention review gate's reason marker: `reason: [bh:]review <sha>`. The trailing hex sha is
# what separates a submit review gate from an ad-hoc human gate reasoned "review the plan".
_REVIEW_REASON = re.compile(r"reason: (?:bh:)?review [0-9a-f]{7,40}\b")
_SECURITY_MARKER = "security:"
_RELEASE_HOLD_MARKER = "release-hold:"
_WARDEN_PREFIX = "warden/"
_RELEASER_PREFIX = "releaser/"

SelfReviewPolicy = Literal["hard", "advise"]


# ---- ports ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Gate:
    """One gate row as the compatibility route reports it."""

    id: str
    status: str
    description: str = ""
    reason: str = ""
    await_type: str = ""

    @property
    def is_open(self) -> bool:
        return self.status == "open"

    def _text(self) -> tuple[str, str]:
        return self.reason.lower(), self.description.lower()

    @property
    def is_review(self) -> bool:
        return bool(_REVIEW_REASON.search(self.description.lower()))

    @property
    def is_security(self) -> bool:
        reason, description = self._text()
        return _SECURITY_MARKER in reason or f"reason: {_SECURITY_MARKER}" in description

    @property
    def is_release_hold(self) -> bool:
        reason, description = self._text()
        return _RELEASE_HOLD_MARKER in reason or f"reason: {_RELEASE_HOLD_MARKER}" in description


class GateLookupFailed(RuntimeError):
    """The gate route could not list the gates naming a bead."""


class GateResolveFailed(RuntimeError):
    """The gate route refused or failed to resolve one gate."""

    def __init__(self, gate_id: str, exit_code: int = 1, detail: str = "") -> None:
        self.gate_id = gate_id
        self.exit_code = exit_code or 1
        super().__init__(detail or f"gate {gate_id} was not resolved")


class SessionUnavailable(RuntimeError):
    """The shell could not provide a Beads session for this hive (e.g. it cannot be served).

    Session factories raise it (or the client's ``ServiceError``) instead of starting a
    service: the core never owns ``bd serve``.
    """


class StateUpdateFailed(RuntimeError):
    """The state-dimension route did not record a transition."""

    def __init__(self, exit_code: int = 1, detail: str = "") -> None:
        self.exit_code = exit_code or 1
        super().__init__(detail or "state transition was not recorded")


class GateOperations(Protocol):
    """The only way this core reaches gates (``work.gate.lookup`` / ``work.gate.resolve``)."""

    def gates_for(self, bead: str) -> Sequence[Gate]:
        """Every gate — open and resolved — whose description names ``bead`` as a whole id.

        Raises :class:`GateLookupFailed` when the lookup itself fails; an empty result must mean
        "no gates", never "could not tell".
        """
        ...

    def resolve(self, gate_id: str, *, reason: str, actor: str) -> None:
        """Resolve one gate attributed to ``actor``; raise :class:`GateResolveFailed`."""
        ...


class StateOperations(Protocol):
    """Record a Beadhive state-dimension transition (``work.state.update``).

    The CLI route writes the transition event *and* the ``<dimension>:<value>`` label
    atomically; rework metrics and dispatch retry detection read those events, so the
    transition must not be reproduced as a bare HTTP label write.
    """

    def set_state(
        self, bead: str, dimension: str, value: str, *, reason: str, actor: str
    ) -> None: ...


class ReviewObserver(Protocol):
    """Observer notification for completed review transitions and advisory policy hits."""

    def transition(self, name: str, attributes: Mapping[str, str]) -> None: ...

    def self_review_advised(self, *, bead: str, actor: str, author: str, policy: str) -> None: ...


class NullObserver:
    def transition(self, name: str, attributes: Mapping[str, str]) -> None:
        return None

    def self_review_advised(self, *, bead: str, actor: str, author: str, policy: str) -> None:
        return None


# ---- results -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Notice:
    """One operator-facing line; ``error`` routes it to stderr."""

    text: str
    error: bool = False


@dataclass(frozen=True)
class ReviewOutcome:
    action: str
    bead: str
    actor: str
    gates: tuple[str, ...] = ()
    notices: tuple[Notice, ...] = ()


class ReviewFailed(Exception):
    """A fail-closed refusal. ``notices`` carries every line to show, the error last."""

    def __init__(self, notices: Sequence[Notice], exit_code: int = 1) -> None:
        self.notices = tuple(notices)
        self.exit_code = exit_code or 1
        super().__init__(self.notices[-1].text if self.notices else "review failed")


@dataclass(frozen=True)
class ReviewPolicy:
    """Shell-resolved policy inputs (the core never reads config or environment)."""

    cli_name: str = "bh"
    self_review: SelfReviewPolicy = "hard"


SessionFactory = Callable[[], AbstractContextManager[BeadsSession]]


def is_warden(actor: str) -> bool:
    return actor.startswith(_WARDEN_PREFIX)


def is_releaser(actor: str) -> bool:
    return actor.startswith(_RELEASER_PREFIX)


def person_of(name: str) -> str:
    """The person part of a seat identity (``dev/alice`` -> ``alice``)."""
    return name.split("/", 1)[1] if "/" in name else name


def review_state(labels: Sequence[str]) -> str:
    """The ``review`` dimension value carried by the bead's ``review:<value>`` label."""
    prefix = f"{REVIEW_DIMENSION}:"
    return next((label[len(prefix) :] for label in labels if label.startswith(prefix)), "")


def _labels(issue: IssueDetails) -> list[str]:
    return [str(label) for label in issue.labels] if issue.labels is not UNSET else []


def _text(value: object) -> str:
    return "" if value is UNSET or value is None else str(value)


# ---- handlers ------------------------------------------------------------------------------


@dataclass
class _Run:
    """Notice accumulator for one command; ``fail`` raises with everything said so far."""

    notices: list[Notice] = field(default_factory=list)

    def say(self, text: str, *, error: bool = False) -> None:
        self.notices.append(Notice(text, error))

    def fail(self, text: str, exit_code: int = 1) -> ReviewFailed:
        return ReviewFailed([*self.notices, Notice(text, True)], exit_code)


class ReviewCommands:
    """Thin ``approve`` / ``bounce`` handlers.

    Every guard read (context, bead, gates) happens before the first mutation, so a context,
    lookup or transition refusal leaves nothing half-done. Writes run in a fixed order and stop
    at the first failure; an ambiguous HTTP write is reported, never replayed.
    """

    def __init__(
        self,
        open_session: SessionFactory,
        gates: GateOperations,
        states: StateOperations,
        *,
        observer: ReviewObserver | None = None,
        policy: ReviewPolicy | None = None,
    ) -> None:
        self._open_session = open_session
        self._gates = gates
        self._states = states
        self._observer = observer or NullObserver()
        self._policy = policy or ReviewPolicy()

    # -- approve --

    def approve(self, bead: str, actor: str) -> ReviewOutcome:
        run = _Run()
        self._require_actor(run, actor, "approve")
        with self._session(run) as session:
            issue = self._read_open(run, session, bead)
            gates = self._lookup(run, bead)
            open_review = [gate for gate in gates if gate.is_review and gate.is_open]
            cleared = self._clear_assurance_gate(run, bead, actor, gates, open_review)
            if cleared is not None:
                return cleared
            if not open_review:
                raise run.fail(f"✗ no open review gate for {bead} — nothing to approve")
            self._guard_human(run, bead, open_review)
            self._guard_self_review(run, bead, actor, _text(issue.assignee).strip())
            resolved = self._resolve_all(run, bead, open_review, f"approved by {actor}", actor)
            self._clear_review_state(run, session, bead, actor)
        self._observer.transition("approved", {"bh.review.gate": "human"})
        run.say(f"✓ approved {bead}: resolved review gate(s) {', '.join(resolved)} as {actor}")
        return ReviewOutcome("approve", bead, actor, tuple(resolved), tuple(run.notices))

    def _clear_assurance_gate(
        self,
        run: _Run,
        bead: str,
        actor: str,
        gates: Sequence[Gate],
        open_review: Sequence[Gate],
    ) -> ReviewOutcome | None:
        """Resolve a warden-owned security gate or a releaser-owned release-hold gate.

        Handled when its owner is clearing it, or when it is the only thing left open — so a
        non-owner hits the owner-only refusal instead of a misleading "no review gate".
        """
        security = next((gate for gate in gates if gate.is_security), None)
        if security is not None and security.is_open and (is_warden(actor) or not open_review):
            if not is_warden(actor):
                raise run.fail(
                    f"✗ security gate {security.id or '?'} is warden-only to resolve — "
                    f"{actor!r} is not a warden (warden/<name>).\n"
                    "  The security:* gate is the Assurance verdict (secret-scan / SBOM / "
                    "policy-as-code); it blocks the merge in parallel with review until a warden "
                    "clears it."
                )
            self._resolve_one(
                run,
                security,
                f"security cleared by {actor}",
                actor,
                f"✗ failed to resolve security gate {security.id} for {bead}",
            )
            self._observer.transition("security_cleared", {"bh.assurance.gate": "security"})
            run.say(f"✓ cleared {bead}: resolved security gate {security.id} as {actor}")
            return ReviewOutcome("approve", bead, actor, (security.id,), tuple(run.notices))
        hold = next((gate for gate in gates if gate.is_release_hold), None)
        if hold is not None and hold.is_open and (is_releaser(actor) or not open_review):
            if not is_releaser(actor):
                raise run.fail(
                    f"✗ release-hold gate {hold.id or '?'} is releaser-only to resolve — "
                    f"{actor!r} is not a releaser (releaser/<name>).\n"
                    "  The release-hold: gate holds a release:breaking change out of the current "
                    "release window; only the releaser seat clears it for merge."
                )
            self._resolve_one(
                run,
                hold,
                f"release-hold cleared by {actor}",
                actor,
                f"✗ failed to resolve release-hold gate {hold.id} for {bead}",
            )
            run.say(f"✓ cleared {bead}: resolved release-hold gate {hold.id} as {actor}")
            return ReviewOutcome("approve", bead, actor, (hold.id,), tuple(run.notices))
        return None

    def _guard_human(self, run: _Run, bead: str, open_review: Sequence[Gate]) -> None:
        non_human = next(
            (gate for gate in open_review if (gate.await_type or "human") != "human"), None
        )
        if non_human is not None:
            raise run.fail(
                f"✗ {bead}'s review gate is a {non_human.await_type} gate — resolve it through "
                f"its own channel (CI / PR merge), not `{self._policy.cli_name} work approve`"
            )

    def _guard_self_review(self, run: _Run, bead: str, actor: str, author: str) -> None:
        if not author or person_of(actor) != person_of(author):
            return
        policy = self._policy.self_review
        if policy != "advise":
            raise run.fail(
                f"✗ {bead}: self-review blocked — {actor!r} authored this bead "
                f"(as {author!r}); the reviewer cross-seat policy is `hard` (default). A "
                "different seat/person must approve; set "
                "`work.dispatch.reviewer_cross_seat: advise` to opt back into a warning."
            )
        self._observer.self_review_advised(bead=bead, actor=actor, author=author, policy=policy)
        run.say(
            f"⚠ {bead}: self-review — {actor!r} authored this bead (as {author!r}). "
            "Advisory only (reviewer cross-seat policy explicitly set to `advise`); the "
            "default `hard` policy would block this.",
            error=True,
        )

    def _clear_review_state(self, run: _Run, session: BeadsSession, bead: str, actor: str) -> None:
        """Move review out of changes-requested, or drop stale ``review:*`` labels.

        Re-reads the bead so the guarded label update carries the revision current after the
        gate resolutions, not the one read before them.
        """
        issue = self._read(run, session, bead)
        labels = _labels(issue)
        if review_state(labels) == CHANGES_REQUESTED:
            reason = f"approved by {actor} (clears stale changes-requested)"
            self._set_state(run, bead, APPROVED, reason, actor)
            return
        stale = [label for label in labels if label.startswith(f"{REVIEW_DIMENSION}:")]
        if not stale:
            return
        request = UpdateIssueRequest(
            actor=actor,
            patch=IssuePatchBody(remove_labels=stale),
            expected_version=issue.revision,
        )
        self._write(run, bead, "review label cleanup", lambda: session.update_issue(bead, request))

    # -- bounce --

    def bounce(self, bead: str, actor: str, reason: str = "") -> ReviewOutcome:
        run = _Run()
        self._require_actor(run, actor, "bounce")
        reason = reason.strip()
        feedback = f"changes requested by {actor}" + (f": {reason}" if reason else "")
        with self._session(run) as session:
            self._read_open(run, session, bead)
            open_review = [
                gate for gate in self._lookup(run, bead) if gate.is_review and gate.is_open
            ]
            if not open_review:
                run.say(
                    f"⚠ {bead}: no open review gate to resolve — recording the bounce anyway",
                    error=True,
                )
            resolved = self._resolve_all(run, bead, open_review, feedback, actor)
            self._set_state(
                run,
                bead,
                CHANGES_REQUESTED,
                feedback,
                actor,
                failure=f"✗ failed to set review state on {bead}",
            )
            comment = AddCommentRequest(author=actor, text=feedback)
            self._write(run, bead, "feedback comment", lambda: session.add_comment(bead, comment))
        self._observer.transition("changes_requested", {"bh.review.gate": "human"})
        run.say(
            f"✓ bounced {bead} (review=changes-requested) as {actor} — developer picks it "
            f"up with `{self._policy.cli_name} work resume {bead}`"
        )
        return ReviewOutcome("bounce", bead, actor, tuple(resolved), tuple(run.notices))

    # -- shared steps --

    @staticmethod
    def _require_actor(run: _Run, actor: str, action: str) -> None:
        if not actor.strip():
            raise run.fail(f"✗ no actor identity resolved for {action} — pass --as <seat>/<name>")

    def _session(self, run: _Run) -> AbstractContextManager[BeadsSession]:
        try:
            return _Opened(self._open_session(), run)
        except (IncompatibleService, ServiceError, SessionUnavailable, OSError, ValueError) as exc:
            raise run.fail(f"✗ Beads service unavailable: {exc}") from exc

    def _read(self, run: _Run, session: BeadsSession, bead: str) -> IssueDetails:
        try:
            return session.get_issue(bead)
        except ServiceProblem as exc:
            if exc.problem.code == "not_found":
                raise run.fail(f"✗ no such bead: {bead}") from exc
            raise run.fail(f"✗ Beads refused reading {bead}: {exc}") from exc
        except (SessionTimeout, IncompatibleService, RuntimeError) as exc:
            raise run.fail(f"✗ could not read {bead} from Beads: {exc}") from exc

    def _read_open(self, run: _Run, session: BeadsSession, bead: str) -> IssueDetails:
        issue = self._read(run, session, bead)
        if _text(issue.status) == "closed":
            raise run.fail(f"✗ bead {bead} is closed")
        return issue

    def _lookup(self, run: _Run, bead: str) -> list[Gate]:
        try:
            return list(self._gates.gates_for(bead))
        except GateLookupFailed as exc:
            raise run.fail(f"✗ could not list gates for {bead}: {exc}") from exc

    def _resolve_one(self, run: _Run, gate: Gate, reason: str, actor: str, failure: str) -> None:
        try:
            self._gates.resolve(gate.id, reason=reason, actor=actor)
        except GateResolveFailed as exc:
            raise run.fail(failure, exc.exit_code) from exc

    def _resolve_all(
        self, run: _Run, bead: str, gates: Sequence[Gate], reason: str, actor: str
    ) -> list[str]:
        """Resolve EVERY open review gate — never first-match a possibly stale duplicate."""
        resolved = []
        for gate in gates:
            failure = f"✗ failed to resolve review gate {gate.id} for {bead}"
            self._resolve_one(run, gate, reason, actor, failure)
            resolved.append(gate.id)
        return resolved

    def _set_state(
        self,
        run: _Run,
        bead: str,
        value: str,
        reason: str,
        actor: str,
        *,
        failure: str = "",
    ) -> None:
        try:
            self._states.set_state(bead, REVIEW_DIMENSION, value, reason=reason, actor=actor)
        except StateUpdateFailed as exc:
            text = failure or f"✗ failed to set review={value} on {bead}"
            raise run.fail(text, exc.exit_code) from exc

    def _write(self, run: _Run, bead: str, what: str, call: Callable[[], object]) -> None:
        try:
            call()
        except IndeterminateWrite as exc:
            raise run.fail(
                f"✗ {what} on {bead}: outcome unknown — the write may have committed; read the "
                "bead before retrying (never replayed automatically)"
            ) from exc
        except ServiceProblem as exc:
            raise run.fail(f"✗ {what} on {bead} refused by Beads: {exc}") from exc
        except (SessionTimeout, IncompatibleService, RuntimeError) as exc:
            raise run.fail(f"✗ {what} on {bead} failed: {exc}") from exc


class _Opened(AbstractContextManager[BeadsSession]):
    """Enter a factory-made session, mapping a failed negotiation to a fail-closed refusal."""

    def __init__(self, manager: AbstractContextManager[BeadsSession], run: _Run) -> None:
        self._manager = manager
        self._run = run

    def __enter__(self) -> BeadsSession:
        try:
            return self._manager.__enter__()
        except (IncompatibleService, ServiceError, SessionUnavailable, OSError, ValueError) as exc:
            raise self._run.fail(f"✗ Beads service unavailable: {exc}") from exc

    def __exit__(self, *exc: object) -> bool | None:
        return self._manager.__exit__(*exc)  # type: ignore[arg-type]
