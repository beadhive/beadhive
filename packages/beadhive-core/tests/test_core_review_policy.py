"""Approval and bounce policy over generated-client transport fixtures and fake ports.

The Beads side is a real ``BeadsSession`` over ``httpx.MockTransport`` serving generated
response shapes; nothing here emulates Beads state. Gates and state dimensions are fake ports,
because Beads v1.3 exposes neither over HTTP.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from beadhive_beads_client import BeadsSession, ExpectedContext, RemoteEndpoint
from beadhive_beads_client.service import ServiceError
from beadhive_core import (
    REVIEW_CAPABILITIES,
    Gate,
    GateLookupFailed,
    GateResolveFailed,
    ReviewCommands,
    ReviewFailed,
    ReviewPolicy,
    SessionUnavailable,
    StateUpdateFailed,
)

BEAD = "bh-7"
REVIEW = Gate("g1", "open", f"blocks {BEAD}\n\nReason: bh:review abc1234", await_type="human")
REVIEW_DUP = Gate("g2", "open", f"blocks {BEAD}\n\nReason: review 0ddba11", await_type="human")
RESOLVED = Gate("g0", "closed", f"blocks {BEAD}\n\nReason: bh:review 1111111")
KICKOFF = Gate("k1", "open", f"blocks {BEAD}\n\nReason: kickoff {BEAD}", await_type="human")
PROSE = Gate("p1", "open", f"blocks {BEAD}\n\nReason: review the rollout plan with ops")
SECURITY = Gate("s1", "open", f"blocks {BEAD}\n\nReason: security:secret-scan")
HOLD = Gate("h1", "open", f"blocks {BEAD}\n\nReason: release-hold: bh-epic")


# ---- transport fixture ---------------------------------------------------------------------


def _problem(status: int, code: str) -> httpx.Response:
    body = {"status": status, "title": code, "code": code, "request_id": "req-1"}
    return httpx.Response(status, json=body)


@dataclass
class Beads:
    """Serves one bead's generated ``IssueDetails`` and records every write."""

    status: str = "in_progress"
    assignee: str = "dev/alice"
    labels: list[str] = field(default_factory=lambda: ["review:pending"])
    revision: str = "rev-1"
    project: str = "proj"
    missing: bool = False
    fail: Mapping[str, Callable[[httpx.Request], httpx.Response]] = field(default_factory=dict)
    writes: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    reads: int = 0

    def issue(self) -> dict[str, Any]:
        return {
            "id": BEAD,
            "title": "t",
            "priority": 2,
            "created_at": "2026-09-26T00:00:00Z",
            "updated_at": "2026-09-26T00:00:00Z",
            "revision": self.revision,
            "status": self.status,
            "assignee": self.assignee,
            "labels": list(self.labels),
        }

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if path == "/v0/beads/context":
            return httpx.Response(
                200,
                json={
                    "api_version": "v0",
                    "bd_version": "1.3.0",
                    "schema_version": 1,
                    "backend": "dolt",
                    "dolt_mode": "server",
                    "database": "bh",
                    "project_id": self.project,
                    "capabilities": sorted(REVIEW_CAPABILITIES),
                },
            )
        if path == "/v0/beads/ready":
            return httpx.Response(200, json={"items": [], "has_more": False})
        assert request.headers["Bd-Project-Id"] == self.project
        key = f"{request.method} {path}"
        if key in self.fail:
            return self.fail[key](request)
        if request.method == "GET" and path == f"/v0/beads/issues/{BEAD}":
            self.reads += 1
            return (
                _problem(404, "not_found")
                if self.missing
                else httpx.Response(200, json=self.issue())
            )
        body = json.loads(request.content or b"{}")
        self.writes.append((request.method, path, body))
        if request.method == "PATCH" and path == f"/v0/beads/issues/{BEAD}":
            removed = set(body["patch"].get("remove_labels", []))
            self.labels = [label for label in self.labels if label not in removed]
            self.revision = "rev-2"
            issue = self.issue()
            return httpx.Response(200, json={"issue": issue, "changed": True, "revision": "rev-2"})
        if request.method == "POST" and path == f"/v0/beads/issues/{BEAD}/comments":
            return httpx.Response(
                200,
                json={
                    "id": "c1",
                    "issue_id": BEAD,
                    "author": body["author"],
                    "text": body["text"],
                    "created_at": "2026-09-26T00:00:00Z",
                },
            )
        raise AssertionError(f"unexpected request {key}")


# ---- fake ports ----------------------------------------------------------------------------


@dataclass
class Gates:
    rows: list[Gate] = field(default_factory=lambda: [REVIEW])
    fail_lookup: bool = False
    fail_resolve: dict[str, int] = field(default_factory=dict)
    resolved: list[tuple[str, str, str]] = field(default_factory=list)

    def gates_for(self, bead: str) -> list[Gate]:
        assert bead == BEAD
        if self.fail_lookup:
            raise GateLookupFailed("bd gate list failed")
        return list(self.rows)

    def resolve(self, gate_id: str, *, reason: str, actor: str) -> None:
        if gate_id in self.fail_resolve:
            raise GateResolveFailed(gate_id, self.fail_resolve[gate_id])
        self.resolved.append((gate_id, reason, actor))


@dataclass
class States:
    fail: int = 0
    calls: list[tuple[str, str, str, str, str]] = field(default_factory=list)

    def set_state(self, bead: str, dimension: str, value: str, *, reason: str, actor: str) -> None:
        if self.fail:
            raise StateUpdateFailed(self.fail)
        self.calls.append((bead, dimension, value, reason, actor))


@dataclass
class Observer:
    events: list[tuple[str, dict[str, str]]] = field(default_factory=list)
    advised: list[dict[str, str]] = field(default_factory=list)

    def transition(self, name: str, attributes: Mapping[str, str]) -> None:
        self.events.append((name, dict(attributes)))

    def self_review_advised(self, **kwargs: str) -> None:
        self.advised.append(kwargs)


@dataclass
class World:
    beads: Beads = field(default_factory=Beads)
    gates: Gates = field(default_factory=Gates)
    states: States = field(default_factory=States)
    observer: Observer = field(default_factory=Observer)
    policy: ReviewPolicy = field(default_factory=ReviewPolicy)
    sessions: int = 0

    def commands(self) -> ReviewCommands:
        def open_session() -> BeadsSession:
            self.sessions += 1
            return BeadsSession(
                RemoteEndpoint("http://127.0.0.1:8080"),
                ExpectedContext("proj", "bh", required_capabilities=REVIEW_CAPABILITIES),
                transport=httpx.MockTransport(self.beads.handle),
            )

        return ReviewCommands(
            open_session, self.gates, self.states, observer=self.observer, policy=self.policy
        )


def _texts(failure: pytest.ExceptionInfo[ReviewFailed]) -> list[str]:
    return [notice.text for notice in failure.value.notices]


# ---- approve -------------------------------------------------------------------------------


def test_approve_resolves_every_open_review_gate_and_clears_review_labels_as_actor() -> None:
    world = World(gates=Gates([RESOLVED, REVIEW, KICKOFF, REVIEW_DUP]))

    outcome = world.commands().approve(BEAD, "rev/bob")

    assert world.gates.resolved == [
        ("g1", "approved by rev/bob", "rev/bob"),
        ("g2", "approved by rev/bob", "rev/bob"),
    ]
    assert world.beads.writes == [
        (
            "PATCH",
            f"/v0/beads/issues/{BEAD}",
            {
                "actor": "rev/bob",
                "patch": {"remove_labels": ["review:pending"]},
                "expected_version": "rev-1",
                "force_close_policy": False,
                "force_assignee_transfer": False,
            },
        )
    ]
    assert world.states.calls == []
    assert outcome.gates == ("g1", "g2")
    assert [n.text for n in outcome.notices] == [
        f"✓ approved {BEAD}: resolved review gate(s) g1, g2 as rev/bob"
    ]
    assert world.observer.events == [("approved", {"bh.review.gate": "human"})]


def test_approve_without_review_labels_writes_nothing_over_http() -> None:
    world = World(beads=Beads(labels=["component:x"]))

    world.commands().approve(BEAD, "rev/bob")

    assert world.beads.writes == []
    assert world.gates.resolved == [("g1", "approved by rev/bob", "rev/bob")]


def test_approve_moves_stale_changes_requested_through_the_state_route() -> None:
    world = World(beads=Beads(labels=["review:changes-requested"]))

    world.commands().approve(BEAD, "rev/bob")

    assert world.states.calls == [
        (
            BEAD,
            "review",
            "approved",
            "approved by rev/bob (clears stale changes-requested)",
            "rev/bob",
        )
    ]
    assert world.beads.writes == []


@pytest.mark.parametrize("gates", [[], [KICKOFF], [PROSE], [RESOLVED]])
def test_approve_refuses_without_an_open_review_gate(gates: list[Gate]) -> None:
    world = World(gates=Gates(gates))

    with pytest.raises(ReviewFailed) as failure:
        world.commands().approve(BEAD, "rev/bob")

    assert _texts(failure) == [f"✗ no open review gate for {BEAD} — nothing to approve"]
    assert failure.value.exit_code == 1
    assert world.gates.resolved == [] and world.beads.writes == []


def test_approve_refuses_an_out_of_process_review_gate() -> None:
    ci_gate = Gate("g9", "open", f"blocks {BEAD}\n\nReason: bh:review abc1234", await_type="gh:pr")
    world = World(gates=Gates([ci_gate]), policy=ReviewPolicy(cli_name="ws"))

    with pytest.raises(ReviewFailed) as failure:
        world.commands().approve(BEAD, "rev/bob")

    assert _texts(failure) == [
        f"✗ {BEAD}'s review gate is a gh:pr gate — resolve it through its own channel "
        "(CI / PR merge), not `ws work approve`"
    ]
    assert world.gates.resolved == []


@pytest.mark.parametrize("approver", ["dev/alice", "rev/alice", "alice"])
def test_self_review_is_blocked_by_person_under_the_default_policy(approver: str) -> None:
    world = World()

    with pytest.raises(ReviewFailed) as failure:
        world.commands().approve(BEAD, approver)

    assert "self-review blocked" in _texts(failure)[-1]
    assert world.gates.resolved == []


def test_self_review_advise_warns_notifies_and_still_approves() -> None:
    world = World(policy=ReviewPolicy(self_review="advise"))

    outcome = world.commands().approve(BEAD, "dev/alice")

    first, last = outcome.notices
    assert first.error and "self-review" in first.text and "Advisory only" in first.text
    assert last.text.startswith(f"✓ approved {BEAD}")
    assert world.observer.advised == [
        {"bead": BEAD, "actor": "dev/alice", "author": "dev/alice", "policy": "advise"}
    ]


def test_warden_clears_security_gate_while_others_clear_only_review() -> None:
    world = World(gates=Gates([REVIEW, SECURITY]))

    world.commands().approve(BEAD, "rev/bob")
    assert world.gates.resolved == [("g1", "approved by rev/bob", "rev/bob")]

    world.gates = Gates([SECURITY])
    outcome = world.commands().approve(BEAD, "warden/sec")
    assert world.gates.resolved == [("s1", "security cleared by warden/sec", "warden/sec")]
    assert [n.text for n in outcome.notices] == [
        f"✓ cleared {BEAD}: resolved security gate s1 as warden/sec"
    ]
    assert world.observer.events[-1] == ("security_cleared", {"bh.assurance.gate": "security"})


def test_non_owner_targeting_the_only_open_assurance_gate_is_refused() -> None:
    world = World(gates=Gates([SECURITY]))
    with pytest.raises(ReviewFailed) as failure:
        world.commands().approve(BEAD, "dev/dev")
    assert "warden-only" in _texts(failure)[-1] and "warden/<name>" in _texts(failure)[-1]

    world = World(gates=Gates([HOLD]))
    with pytest.raises(ReviewFailed) as failure:
        world.commands().approve(BEAD, "warden/sec")
    assert "releaser-only" in _texts(failure)[-1]
    assert world.gates.resolved == []


def test_releaser_clears_release_hold_gate() -> None:
    world = World(gates=Gates([REVIEW, HOLD]))

    outcome = world.commands().approve(BEAD, "releaser/rel")

    assert world.gates.resolved == [("h1", "release-hold cleared by releaser/rel", "releaser/rel")]
    assert [n.text for n in outcome.notices] == [
        f"✓ cleared {BEAD}: resolved release-hold gate h1 as releaser/rel"
    ]


# ---- bounce --------------------------------------------------------------------------------


def test_bounce_resolves_gates_records_changes_requested_and_feedback_comment() -> None:
    world = World(gates=Gates([REVIEW, REVIEW_DUP, KICKOFF]))

    outcome = world.commands().bounce(BEAD, "rev/bob", "  fix the edge case ")

    feedback = "changes requested by rev/bob: fix the edge case"
    assert world.gates.resolved == [("g1", feedback, "rev/bob"), ("g2", feedback, "rev/bob")]
    assert world.states.calls == [(BEAD, "review", "changes-requested", feedback, "rev/bob")]
    assert world.beads.writes == [
        ("POST", f"/v0/beads/issues/{BEAD}/comments", {"author": "rev/bob", "text": feedback})
    ]
    assert [n.text for n in outcome.notices] == [
        f"✓ bounced {BEAD} (review=changes-requested) as rev/bob — developer picks it up with "
        f"`bh work resume {BEAD}`"
    ]
    assert world.observer.events == [("changes_requested", {"bh.review.gate": "human"})]


def test_bounce_without_an_open_gate_warns_and_still_records() -> None:
    world = World(gates=Gates([]))

    outcome = world.commands().bounce(BEAD, "rev/bob", "")

    assert outcome.notices[0] == outcome.notices[0].__class__(
        f"⚠ {BEAD}: no open review gate to resolve — recording the bounce anyway", True
    )
    assert world.states.calls == [
        (BEAD, "review", "changes-requested", "changes requested by rev/bob", "rev/bob")
    ]


# ---- fail closed ---------------------------------------------------------------------------


def test_context_mismatch_fails_before_any_gate_or_state_access() -> None:
    world = World(beads=Beads(project="someone-else"))

    with pytest.raises(ReviewFailed) as failure:
        world.commands().approve(BEAD, "rev/bob")

    assert _texts(failure)[-1].startswith("✗ Beads service unavailable:")
    assert world.gates.resolved == [] and world.states.calls == []


@pytest.mark.parametrize(
    ("beads", "message"),
    [
        (Beads(missing=True), f"✗ no such bead: {BEAD}"),
        (Beads(status="closed"), f"✗ bead {BEAD} is closed"),
    ],
)
def test_transition_refusals_leave_gates_untouched(beads: Beads, message: str) -> None:
    for action in ("approve", "bounce"):
        world = World(beads=beads)
        with pytest.raises(ReviewFailed) as failure:
            getattr(world.commands(), action)(BEAD, "rev/bob")
        assert _texts(failure) == [message]
        assert world.gates.resolved == [] and world.states.calls == []


def test_gate_lookup_failure_is_not_read_as_no_gates() -> None:
    world = World(gates=Gates(fail_lookup=True))

    with pytest.raises(ReviewFailed) as failure:
        world.commands().bounce(BEAD, "rev/bob", "x")

    assert _texts(failure) == [f"✗ could not list gates for {BEAD}: bd gate list failed"]
    assert world.states.calls == [] and world.beads.writes == []


def test_gate_resolve_failure_propagates_its_exit_code_and_stops() -> None:
    world = World(gates=Gates([REVIEW, REVIEW_DUP], fail_resolve={"g1": 3}))

    with pytest.raises(ReviewFailed) as failure:
        world.commands().approve(BEAD, "rev/bob")

    assert _texts(failure) == [f"✗ failed to resolve review gate g1 for {BEAD}"]
    assert failure.value.exit_code == 3
    assert world.gates.resolved == [] and world.beads.writes == []


def test_bounce_state_failure_stops_before_the_feedback_comment() -> None:
    world = World(states=States(fail=4))

    with pytest.raises(ReviewFailed) as failure:
        world.commands().bounce(BEAD, "rev/bob", "x")

    assert _texts(failure) == [f"✗ failed to set review state on {BEAD}"]
    assert failure.value.exit_code == 4
    assert world.beads.writes == []


def test_ambiguous_http_write_is_reported_and_never_replayed() -> None:
    attempts: list[httpx.Request] = []

    def lost(request: httpx.Request) -> httpx.Response:
        attempts.append(request)
        raise httpx.ReadTimeout("reply lost")

    world = World(beads=Beads(fail={f"POST /v0/beads/issues/{BEAD}/comments": lost}))

    with pytest.raises(ReviewFailed) as failure:
        world.commands().bounce(BEAD, "rev/bob", "x")

    assert "outcome unknown" in _texts(failure)[-1]
    assert len(attempts) == 1


def test_stale_revision_on_label_cleanup_fails_closed() -> None:
    world = World(
        beads=Beads(fail={f"PATCH /v0/beads/issues/{BEAD}": lambda _r: _problem(409, "conflict")})
    )

    with pytest.raises(ReviewFailed) as failure:
        world.commands().approve(BEAD, "rev/bob")

    assert "review label cleanup" in _texts(failure)[-1]
    assert "refused by Beads" in _texts(failure)[-1]


def test_an_unattributed_review_is_refused_before_opening_beads() -> None:
    world = World()

    with pytest.raises(ReviewFailed):
        world.commands().approve(BEAD, " ")

    assert world.sessions == 0


@pytest.mark.parametrize(
    "refusal",
    [
        ServiceError("no verified service; start it with `bh host beads start --hive h`"),
        SessionUnavailable("h uses embedded Dolt"),
    ],
)
def test_an_unavailable_service_fails_closed_without_touching_gates(refusal: Exception) -> None:
    world = World()

    def unavailable() -> BeadsSession:
        raise refusal

    commands = ReviewCommands(unavailable, world.gates, world.states)
    for action in (commands.approve, commands.bounce):
        with pytest.raises(ReviewFailed) as failure:
            action(BEAD, "rev/bob")
        assert _texts(failure) == [f"✗ Beads service unavailable: {refusal}"]
    assert world.gates.resolved == [] and world.states.calls == []
