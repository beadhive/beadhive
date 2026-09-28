"""Assign / claim / resume / abandon / submit-state policy over transport fixtures and fake ports.

The Beads side of the api-ready issue route is a real ``BeadsSession`` over ``httpx.MockTransport``
serving generated ``IssueDetails`` shapes. The CLI-compatibility rows (claim lease, state
dimensions, gates) and the root-supplied workspace capabilities are fake ports. The lease fake
updates the row the transport serves — standing in for what the service would report after the
real ``bd`` write — because the policy under test is the re-read verification, not the lease.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from beadhive_beads_client import (
    BeadsSession,
    ExpectedContext,
    RemoteEndpoint,
    cli_compatibility_operations,
    load_operation_matrix,
)
from beadhive_core import (
    ISSUE_ROUTES,
    LEASE_ROUTES,
    LIFECYCLE_CAPABILITIES,
    LIFECYCLE_ROUTES,
    LIFECYCLE_STATE_ROUTES,
    Gate,
    GateLookupFailed,
    IssueReadFailed,
    LifecycleCommands,
    LifecycleFailed,
    LifecyclePolicy,
    Provisioned,
    Refused,
    SessionIssues,
    StateUpdateFailed,
    WriteFailed,
    batch_group,
    batch_member_procedure,
    claim_residue,
    claim_won,
)

BEAD = "bh-7"


def _problem(status: int, code: str) -> httpx.Response:
    body = {"status": status, "title": code, "code": code, "request_id": "req-1"}
    return httpx.Response(status, json=body)


# ---- transport fixture ---------------------------------------------------------------------


@dataclass
class Beads:
    """Serves one bead's generated ``IssueDetails`` and records every write."""

    status: str = "open"
    assignee: str = ""
    issue_type: str = "task"
    labels: list[str] = field(default_factory=list)
    description: str = "the brief"
    revision: str = "rev-1"
    missing: bool = False
    fail: Mapping[str, Callable[[httpx.Request], httpx.Response]] = field(default_factory=dict)
    on_read: Callable[[Beads, int], None] | None = None
    writes: list[tuple[str, str, dict[str, Any]]] = field(default_factory=list)
    reads: int = 0

    def issue(self) -> dict[str, Any]:
        return {
            "id": BEAD,
            "title": "t",
            "priority": 2,
            "created_at": "2026-09-27T00:00:00Z",
            "updated_at": "2026-09-27T00:00:00Z",
            "revision": self.revision,
            "status": self.status,
            "assignee": self.assignee,
            "issue_type": self.issue_type,
            "description": self.description,
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
                    "project_id": "proj",
                    "capabilities": sorted(LIFECYCLE_CAPABILITIES),
                },
            )
        if path == "/v0/beads/ready":
            return httpx.Response(200, json={"items": [], "has_more": False})
        assert request.headers["Bd-Project-Id"] == "proj"
        key = f"{request.method} {path}"
        if key in self.fail:
            return self.fail[key](request)
        if request.method == "GET" and path == f"/v0/beads/issues/{BEAD}":
            self.reads += 1
            if self.on_read is not None:
                self.on_read(self, self.reads)
            if self.missing:
                return _problem(404, "not_found")
            return httpx.Response(200, json=self.issue())
        body = json.loads(request.content or b"{}")
        self.writes.append((request.method, path, body))
        if request.method == "PATCH" and path == f"/v0/beads/issues/{BEAD}":
            if body.get("expected_version") not in (None, self.revision):
                return _problem(409, "precondition_failed")
            self.assignee = body["patch"].get("assignee", self.assignee)
            self.revision = "rev-2"
            return httpx.Response(
                200, json={"issue": self.issue(), "changed": True, "revision": self.revision}
            )
        raise AssertionError(f"unexpected request {key}")


# ---- fake ports ----------------------------------------------------------------------------


@dataclass
class Leases:
    beads: Beads
    log: list[str]
    refuse: int = 0
    release_fails: bool = False
    takes: bool = True

    def acquire(self, bead: str, *, actor: str) -> None:
        self.log.append(f"lease.acquire {actor}")
        if self.refuse:
            raise WriteFailed(self.refuse)
        if self.takes:
            self.beads.assignee, self.beads.status = actor, "in_progress"

    def release(self, bead: str, *, actor: str) -> None:
        self.log.append(f"lease.release {actor}")
        if self.release_fails:
            raise WriteFailed(1)
        if self.takes:
            self.beads.assignee, self.beads.status = "", "open"


@dataclass
class States:
    log: list[str]
    values: dict[str, str] = field(default_factory=dict)
    fail: int = 0
    calls: list[tuple[str, str, str, str]] = field(default_factory=list)

    def set_state(self, bead: str, dimension: str, value: str, *, reason: str, actor: str) -> None:
        self.log.append(f"state {dimension}={value}")
        if self.fail:
            raise StateUpdateFailed(self.fail)
        self.calls.append((dimension, value, reason, actor))

    def get_state(self, bead: str, dimension: str) -> str:
        return self.values.get(dimension, "")


@dataclass
class Gates:
    rows: list[Gate] = field(default_factory=list)
    fail_lookup: bool = False
    resolved: list[tuple[str, str, str]] = field(default_factory=list)

    def gates_for(self, bead: str) -> list[Gate]:
        if self.fail_lookup:
            raise GateLookupFailed("bd gate list failed")
        return list(self.rows)

    def resolve(self, gate_id: str, *, reason: str, actor: str) -> None:
        self.resolved.append((gate_id, reason, actor))


@dataclass
class Workspace:
    log: list[str]
    refuse_gate: int = 0
    fail_provision: Callable[[], None] | None = None
    batch: dict[str, Provisioned] = field(default_factory=dict)
    exists: bool = True

    def refresh(self) -> None:
        self.log.append("refresh")

    def publish(self, actor: str, message: str) -> None:
        self.log.append(f"publish {message}")

    def dispatch_gate(self, issue: Mapping[str, Any], bead: str) -> None:
        self.log.append("dispatch_gate")
        if self.refuse_gate:
            raise Refused(self.refuse_gate)

    def open_container(self, bead: str) -> None:
        self.log.append("open_container")

    def provision(self, bead: str, kind: str) -> Provisioned:
        self.log.append(f"provision {kind or '-'}")
        if self.fail_provision is not None:
            self.fail_provision()
        return Provisioned(Path(f"/wts/{bead}"), f"wt/bead/issue/{bead}", {"entry": 1})

    def batch_checkout(self, group: str) -> Provisioned | None:
        self.log.append(f"batch_checkout {group}")
        return self.batch.get(group)

    def stamp(self, checkout: Provisioned, actor: str) -> None:
        self.log.append(f"stamp {actor} {checkout.target}")

    def record_claim(self, bead: str, actor: str, checkout: Provisioned) -> None:
        self.log.append(f"record_claim {actor}")

    def remove(self, bead: str) -> bool:
        self.log.append("remove")
        return self.exists


@dataclass
class Output:
    lines: list[tuple[str, bool]] = field(default_factory=list)
    log: list[str] = field(default_factory=list)

    def say(self, text: str, *, error: bool = False) -> None:
        self.lines.append((text, error))
        self.log.append(f"say {text.splitlines()[0]}")

    def feedback(self, bead: str) -> None:
        self.log.append("feedback")

    @property
    def errors(self) -> list[str]:
        return [text for text, error in self.lines if error]

    @property
    def out(self) -> list[str]:
        return [text for text, error in self.lines if not error]


@dataclass
class Observer:
    log: list[str]
    transitions: list[str] = field(default_factory=list)
    legacy: list[dict[str, str]] = field(default_factory=list)
    dispatched: list[dict[str, str]] = field(default_factory=list)

    def transition(self, name: str) -> None:
        self.transitions.append(name)

    @contextmanager
    def dispatch(self, *, agent: str, bead: str, brief: str) -> Iterator[None]:
        self.dispatched.append({"agent": agent, "bead": bead, "brief": brief})
        self.log.append("span.open")
        yield
        self.log.append("span.close")

    def legacy_seat(self, **kwargs: str) -> None:
        self.legacy.append(kwargs)


@dataclass
class World:
    log: list[str] = field(default_factory=list)
    beads: Beads = field(default_factory=Beads)

    def __post_init__(self) -> None:
        self.leases = Leases(self.beads, self.log)
        self.states = States(self.log)
        self.gates = Gates()
        self.workspace = Workspace(self.log)
        self.output = Output(log=self.log)
        self.observer = Observer(self.log)

    @contextmanager
    def commands(self) -> Iterator[LifecycleCommands]:
        session = BeadsSession(
            RemoteEndpoint("http://127.0.0.1:8080"),
            ExpectedContext("proj", "bh", required_capabilities=LIFECYCLE_CAPABILITIES),
            transport=httpx.MockTransport(self.beads.handle),
        )
        with session as opened:
            yield LifecycleCommands(
                SessionIssues(opened),
                self.leases,
                self.states,
                self.states,
                self.gates,
                self.workspace,
                self.output,
                observer=self.observer,
                policy=LifecyclePolicy(cli_name="bh"),
            )

    def run(self, call: Callable[[LifecycleCommands], Any]) -> Any:
        with self.commands() as commands:
            return call(commands)

    def refused(self, call: Callable[[LifecycleCommands], Any]) -> LifecycleFailed:
        with pytest.raises(LifecycleFailed) as failure:
            self.run(call)
        return failure.value

    @property
    def patches(self) -> list[dict[str, Any]]:
        return [body for method, _path, body in self.beads.writes if method == "PATCH"]


@pytest.fixture
def world() -> World:
    return World()


# ---- routes --------------------------------------------------------------------------------


def test_every_operation_this_cohort_touches_is_a_named_matrix_route() -> None:
    rows = {row["name"]: row for row in load_operation_matrix()["operations"]}
    cli_routes = cli_compatibility_operations()
    assert set(LIFECYCLE_ROUTES) <= set(rows)
    for name in ISSUE_ROUTES:
        assert rows[name]["classification"] == "api-ready"
        assert rows[name]["capability"] in LIFECYCLE_CAPABILITIES
    for name in LEASE_ROUTES + LIFECYCLE_STATE_ROUTES:
        assert name in cli_routes
    # issues.claim / issues.release exist over HTTP but do not carry the renewable lease.
    assert rows["work.lease.acquire"]["classification"] == "cli-compatibility"
    assert rows["work.lease.release"]["classification"] == "cli-compatibility"


# ---- assign --------------------------------------------------------------------------------


def test_assign_is_a_guarded_update_then_the_supplied_provisioning(world: World) -> None:
    outcome = world.run(lambda c: c.assign(BEAD, "dev/carol", "disp/lead"))

    assert world.patches == [
        {
            "actor": "disp/lead",
            "patch": {"assignee": "dev/carol"},
            "expected_version": "rev-1",
            "force_close_policy": False,
            "force_assignee_transfer": False,
        }
    ]
    assert world.log == [
        "dispatch_gate",
        "span.open",
        f"publish assign {BEAD} -> dev/carol",
        "open_container",
        "provision issue",
        f"stamp dev/carol /wts/{BEAD}",
        "span.close",
        f"say ✓ assigned {BEAD} → dev/carol; worktree /wts/{BEAD}",
    ]
    assert world.observer.dispatched == [{"agent": "dev/carol", "bead": BEAD, "brief": "the brief"}]
    assert world.observer.transitions == ["assigned"]
    assert outcome.checkout.target == Path(f"/wts/{BEAD}")
    assert world.beads.status == "open"  # assign never claims


@pytest.mark.parametrize("actor", ["dev/alice", "rev/rob", "warden/w"])
def test_assign_is_orchestrator_only_before_any_read(world: World, actor: str) -> None:
    failure = world.refused(lambda c: c.assign(BEAD, "dev/carol", actor))

    assert failure.exit_code == 1
    assert "orchestrator-only" in world.output.errors[-1]
    assert actor in world.output.errors[-1]
    assert world.beads.reads == 0 and world.beads.writes == [] and world.log[-1].startswith("say")


@pytest.mark.parametrize("actor", ["disp/lead", "dir/dana", "coord/lead", "brian"])
def test_orchestrators_and_bare_humans_may_assign(world: World, actor: str) -> None:
    world.run(lambda c: c.assign(BEAD, "dev/carol", actor))
    assert world.beads.assignee == "dev/carol"


def test_an_epic_is_assigned_only_to_a_dispatcher(world: World) -> None:
    world.beads.issue_type = "epic"
    world.refused(lambda c: c.assign(BEAD, "dev/dev", "disp/lead"))
    assert world.output.errors == [
        f"✗ {BEAD} is an epic — it may only be assigned to a dispatcher (disp/<name>), not "
        "'dev/dev'"
    ]
    assert world.beads.writes == [] and "provision epic" not in world.log

    world.run(lambda c: c.assign(BEAD, "disp/lead", "disp/lead"))
    assert "provision epic" in world.log


def test_a_leaf_is_assigned_only_to_a_developer(world: World) -> None:
    world.refused(lambda c: c.assign(BEAD, "disp/lead", "disp/lead"))
    assert "may only be assigned to a developer (dev/<name>)" in world.output.errors[-1]
    assert world.beads.writes == []


def test_assign_refuses_to_steal_and_refuses_closed_or_missing(world: World) -> None:
    world.beads.assignee = "dev/bob"
    world.refused(lambda c: c.assign(BEAD, "dev/carol", "disp/lead"))
    assert world.output.errors[-1] == (
        f"✗ bead {BEAD} assigned to dev/bob (not dev/carol) — refusing to steal"
    )
    world.beads.assignee, world.beads.status = "", "closed"
    world.refused(lambda c: c.assign(BEAD, "dev/carol", "disp/lead"))
    assert world.output.errors[-1] == f"✗ bead {BEAD} is closed"
    world.beads.missing = True
    world.refused(lambda c: c.assign(BEAD, "dev/carol", "disp/lead"))
    assert world.output.errors[-1] == f"✗ no such bead: {BEAD}"
    assert world.beads.writes == []


def test_assign_dispatch_gate_refusal_stops_before_the_write(world: World) -> None:
    world.workspace.refuse_gate = 3
    failure = world.refused(lambda c: c.assign(BEAD, "dev/carol", "disp/lead"))
    assert failure.exit_code == 3
    assert world.beads.writes == [] and world.output.lines == []  # the gate printed its own


def test_assign_conflict_since_the_guard_read_is_refused_not_overwritten(world: World) -> None:
    def moved(beads: Beads, reads: int) -> None:
        beads.revision = "rev-concurrent"

    world.beads.on_read = moved  # the row moves between the guard read and the write
    world.beads.revision = "rev-1"
    world.beads.fail = {
        f"PATCH /v0/beads/issues/{BEAD}": lambda _r: _problem(409, "precondition_failed")
    }
    world.refused(lambda c: c.assign(BEAD, "dev/carol", "disp/lead"))
    assert world.output.errors == [
        f"✗ {BEAD} changed since it was read — not assigned; re-run the assign"
    ]
    assert not any(entry.startswith("provision") for entry in world.log)
    assert world.observer.transitions == []


def test_assign_ambiguous_write_is_reported_never_replayed(world: World) -> None:
    def reply_lost(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("reply lost")

    world.beads.fail = {f"PATCH /v0/beads/issues/{BEAD}": reply_lost}
    world.refused(lambda c: c.assign(BEAD, "dev/carol", "disp/lead"))
    assert world.output.errors == [
        f"✗ assign {BEAD}: outcome unknown — the write may have committed; read the bead "
        "before retrying (never replayed automatically)"
    ]
    assert not any(entry.startswith("provision") for entry in world.log)


# ---- claim ---------------------------------------------------------------------------------


def test_fresh_claim_takes_the_lease_then_verifies_then_provisions(world: World) -> None:
    outcome = world.run(lambda c: c.claim(BEAD, "dev/alice"))

    assert world.log == [
        "refresh",
        "dispatch_gate",
        "open_container",
        "lease.acquire dev/alice",
        "provision issue",
        f"stamp dev/alice /wts/{BEAD}",
        "record_claim dev/alice",
    ]
    assert outcome.disposition == "claimed"
    assert outcome.issue["assignee"] == "dev/alice"
    assert outcome.issue["status"] == "in_progress"
    assert world.observer.transitions == ["claimed"]
    assert world.output.lines == []  # the caller renders the envelope
    assert world.beads.reads == 3  # guard, post-lease verify, post-provision verify


def test_reclaim_by_the_holder_reattaches_without_a_lease_or_dispatch_gate(world: World) -> None:
    world.beads.assignee, world.beads.status = "dev/alice", "in_progress"
    outcome = world.run(lambda c: c.claim(BEAD, "dev/alice"))
    assert outcome.disposition == "reattached"
    assert "lease.acquire dev/alice" not in world.log
    assert "dispatch_gate" not in world.log and "open_container" not in world.log
    assert "record_claim dev/alice" in world.log


def test_claim_refuses_another_actors_bead_before_provisioning(world: World) -> None:
    world.beads.assignee = "dev/bob"
    world.refused(lambda c: c.claim(BEAD, "dev/alice"))
    assert "refusing to steal" in world.output.errors[-1]
    assert world.log[:1] == ["refresh"] and not any("provision" in e for e in world.log)


def test_an_epic_is_claimed_only_by_a_dispatcher(world: World) -> None:
    world.beads.issue_type = "epic"
    world.refused(lambda c: c.claim(BEAD, "dev/dev"))
    assert "may only be claimed by a dispatcher (disp/<name>)" in world.output.errors[-1]
    outcome = world.run(lambda c: c.claim(BEAD, "disp/lead"))
    assert outcome.issue["status"] == "in_progress"
    assert "provision epic" in world.log


def test_legacy_seat_prefixes_satisfy_the_seat_guard_and_are_reported(world: World) -> None:
    world.beads.issue_type = "epic"
    world.run(lambda c: c.claim(BEAD, "coord/lead"))
    assert world.observer.legacy[0] == {
        "deprecated": "coord/",
        "replacement": "disp/",
        "seat": "dispatcher",
    }
    other = World()
    other.run(lambda c: c.claim(BEAD, "crew/dev"))
    assert other.beads.status == "in_progress"


def test_a_refused_lease_propagates_its_exit_code_and_provisions_nothing(world: World) -> None:
    world.leases.refuse = 7
    assert world.refused(lambda c: c.claim(BEAD, "dev/alice")).exit_code == 7
    assert not any("provision" in e for e in world.log)


def test_a_lease_that_did_not_take_is_a_lost_race_not_a_win(world: World) -> None:
    world.leases.takes = False  # exit 0, but the store never moved: not a compare-and-swap
    assert world.refused(lambda c: c.claim(BEAD, "dev/alice")).exit_code == 1
    assert not any("provision" in e for e in world.log)
    assert world.observer.transitions == []


def test_a_reassignment_during_provisioning_never_returns_success(world: World) -> None:
    def reassign_on_final_read(beads: Beads, reads: int) -> None:
        if reads == 3:
            beads.assignee = "dev/other"

    world.beads.on_read = reassign_on_final_read
    world.refused(lambda c: c.claim(BEAD, "dev/alice"))
    assert world.beads.assignee == "dev/other" and world.beads.status == "in_progress"
    assert "lease.release dev/alice" not in world.log
    assert world.observer.transitions == []


def test_failed_provisioning_releases_a_claim_it_still_holds(world: World) -> None:
    def disk_full() -> None:
        raise OSError("disk full")

    world.workspace.fail_provision = disk_full
    with pytest.raises(OSError, match="disk full"):
        world.run(lambda c: c.claim(BEAD, "dev/alice"))
    assert world.states.calls == [("dispatch", "provisioning_failed", "disk full", "dev/alice")]
    assert world.log[-2:] == ["state dispatch=provisioning_failed", "lease.release dev/alice"]
    assert (world.beads.status, world.beads.assignee) == ("open", "")


def test_failed_provisioning_never_clobbers_a_concurrent_winner(world: World) -> None:
    def reassigned_then_failed() -> None:
        world.beads.assignee = "dev/other"
        raise OSError("disk full")

    world.workspace.fail_provision = reassigned_then_failed
    with pytest.raises(OSError):
        world.run(lambda c: c.claim(BEAD, "dev/alice"))
    assert "lease.release dev/alice" not in world.log
    assert (world.beads.status, world.beads.assignee) == ("in_progress", "dev/other")


def test_failed_reattach_provisioning_never_releases_the_held_claim(world: World) -> None:
    world.beads.assignee, world.beads.status = "dev/alice", "in_progress"

    def boom() -> None:
        raise OSError("boom")

    world.workspace.fail_provision = boom
    with pytest.raises(OSError):
        world.run(lambda c: c.claim(BEAD, "dev/alice"))
    assert "lease.release dev/alice" not in world.log


def test_an_unreadable_bead_fails_closed_with_the_route_error(world: World) -> None:
    world.beads.fail = {f"GET /v0/beads/issues/{BEAD}": lambda _r: _problem(500, "internal")}
    world.refused(lambda c: c.claim(BEAD, "dev/alice"))
    assert world.output.errors[-1].startswith(f"✗ Beads refused reading {BEAD}")
    assert "lease.acquire dev/alice" not in world.log


def test_try_claim_believes_only_the_re_read(world: World) -> None:
    assert world.run(lambda c: c.try_claim(BEAD, "dev/alice")) is True
    lost = World()
    lost.leases.takes = False
    assert lost.run(lambda c: c.try_claim(BEAD, "dev/alice")) is False
    refused = World()
    refused.leases.refuse = 1
    assert refused.run(lambda c: c.try_claim(BEAD, "dev/alice")) is False
    assert refused.beads.reads == 0


def test_provision_claim_releases_unconditionally_on_failure(world: World) -> None:
    world.beads.assignee, world.beads.status = "dev/alice", "in_progress"
    checkout = world.run(lambda c: c.provision_claim(BEAD, "dev/alice"))
    assert checkout.target == Path(f"/wts/{BEAD}")
    assert world.log[-2:] == [f"stamp dev/alice /wts/{BEAD}", "record_claim dev/alice"]

    failing = World()

    def boom() -> None:
        raise RuntimeError("ensure failed")

    failing.workspace.fail_provision = boom
    with pytest.raises(RuntimeError):
        failing.run(lambda c: c.provision_claim(BEAD, "dev/alice"))
    assert failing.states.calls == [
        ("dispatch", "provisioning_failed", "ensure failed", "dev/alice")
    ]
    assert failing.log[-1] == "lease.release dev/alice"


def test_release_defaults_the_reason_and_is_best_effort(world: World) -> None:
    world.states.fail = 1
    world.leases.release_fails = True
    world.run(lambda c: c.release(BEAD, "dev/x"))  # never raises: the caller reports the cause
    assert world.log == ["state dispatch=provisioning_failed", "lease.release dev/x"]
    ok = World()
    ok.run(lambda c: c.release(BEAD, "dev/x"))
    assert ok.states.calls == [("dispatch", "provisioning_failed", "provisioning_failed", "dev/x")]


# ---- resume --------------------------------------------------------------------------------


REVIEW_GATE = Gate("g1", "open", f"blocks {BEAD}\n\nReason: bh:review abc1234")
KICKOFF = Gate("k1", "open", f"blocks {BEAD}\n\nReason: kickoff {BEAD}")
CLOSED_REVIEW = Gate("g0", "closed", f"blocks {BEAD}\n\nReason: bh:review 1111111")


def test_resume_refuses_unless_changes_were_requested(world: World) -> None:
    world.states.values["review"] = "pending"
    world.refused(lambda c: c.resume(BEAD, "dev/alice"))
    assert world.output.errors == [f"✗ {BEAD} not in review:changes-requested (now: pending)"]
    assert not any("provision" in e for e in world.log)
    del world.states.values["review"]
    world.refused(lambda c: c.resume(BEAD, "dev/alice"))
    assert world.output.errors[-1].endswith("(now: none)")


def test_resume_gcs_orphaned_review_gates_reattaches_and_reasserts(world: World) -> None:
    world.states.values["review"] = "changes-requested"
    world.beads.assignee, world.beads.status = "dev/alice", "in_progress"
    world.gates.rows = [REVIEW_GATE, KICKOFF, CLOSED_REVIEW]

    world.run(lambda c: c.resume(BEAD, "dev/alice"))

    assert world.gates.resolved == [("g1", "orphaned by bounce — cleared on resume", "dev/alice")]
    assert world.log == [
        "refresh",
        "provision -",
        f"stamp dev/alice /wts/{BEAD}",
        "record_claim dev/alice",
        "say ── review feedback ──",
        "feedback",
        "lease.acquire dev/alice",
        f"say ✓ resumed {BEAD} as dev/alice; worktree /wts/{BEAD}",
    ]


def test_resume_of_a_batch_member_reattaches_the_shared_checkout(world: World) -> None:
    world.states.values["review"] = "changes-requested"
    world.beads.labels = ["batch:grp"]
    shared = Provisioned(Path("/wts/batch-grp"), "wt/batch/grp")
    world.workspace.batch["grp"] = shared

    outcome = world.run(lambda c: c.resume(BEAD, "dev/alice"))

    assert outcome.group == "grp" and outcome.checkout == shared
    assert "stamp dev/alice /wts/batch-grp" in world.log
    assert not any(e.startswith("provision") or e.startswith("record_claim") for e in world.log)


def test_resume_of_a_batch_member_without_its_shared_checkout_names_the_procedure(
    world: World,
) -> None:
    world.states.values["review"] = "changes-requested"
    world.beads.labels = ["batch:grp"]
    world.refused(lambda c: c.resume(BEAD, "dev/alice"))
    assert world.output.errors == [batch_member_procedure(BEAD, "grp", "bh")]
    assert "wt/batch/grp" in world.output.errors[0]
    assert not any(e.startswith("provision") for e in world.log)


def test_resume_warns_when_the_gate_lookup_fails_and_still_resumes(world: World) -> None:
    world.states.values["review"] = "changes-requested"
    world.gates.fail_lookup = True
    world.run(lambda c: c.resume(BEAD, "dev/alice"))
    assert "could not list review gates" in world.output.errors[0]
    assert world.output.out[-1].startswith(f"✓ resumed {BEAD}")


# ---- abandon -------------------------------------------------------------------------------


def test_abandon_records_releases_and_proves_the_release(world: World) -> None:
    world.beads.assignee, world.beads.status = "dev/carol", "in_progress"
    outcome = world.run(lambda c: c.abandon(BEAD, "disp/lead"))
    assert world.states.calls == [("review", "abandoned", "abandoned", "disp/lead")]
    assert world.output.out == [f"✓ abandoned {BEAD}; worktree kept"]
    assert world.observer.transitions == ["abandoned"]
    assert "remove" not in world.log and outcome.removed is False


def test_abandon_rm_removes_the_worktree(world: World) -> None:
    world.run(lambda c: c.abandon(BEAD, "disp/lead", remove=True))
    assert "remove" in world.log
    assert world.output.out == [f"✓ abandoned {BEAD}; worktree removed"]


def test_abandon_refuses_to_report_success_while_the_bead_is_still_held(world: World) -> None:
    world.beads.assignee, world.beads.status = "dev/carol", "in_progress"
    world.leases.takes = False  # both writes exit 0, the store does not move

    world.refused(lambda c: c.abandon(BEAD, "disp/lead"))

    error = world.output.errors[-1]
    assert "NOT released" in error
    assert "status is still in_progress; still assigned to dev/carol" in error
    assert "bh bd reclaim" in error and f"bh bd update {BEAD} --status open" in error
    assert world.observer.transitions == []


def test_abandon_surfaces_route_failures_instead_of_success(world: World) -> None:
    world.states.fail = 1
    world.refused(lambda c: c.abandon(BEAD, "disp/lead", remove=True))
    assert world.output.errors == [f"⚠ abandoned {BEAD} with bd errors (see above)"]
    assert "remove" in world.log  # the recovery still removes what it was asked to


def test_an_unreadable_bead_is_never_reported_as_released(world: World) -> None:
    world.beads.fail = {f"GET /v0/beads/issues/{BEAD}": lambda _r: _problem(500, "internal")}
    world.refused(lambda c: c.abandon(BEAD, "disp/lead"))
    assert "could not be re-read" in world.output.errors[-1]


def test_claim_residue_names_each_surviving_half_separately() -> None:
    assert claim_residue({"status": "open", "assignee": ""}) == ""
    assert claim_residue({"status": "open", "assignee": None}) == ""
    assert "still assigned to dev/x" in claim_residue({"status": "open", "assignee": "dev/x"})
    assert "status is still in_progress" in claim_residue({"status": "in_progress", "assignee": ""})
    assert "could not be re-read" in claim_residue(None)


def test_claim_won_requires_the_actor_and_a_transition() -> None:
    assert claim_won({"assignee": "dev/a", "status": "in_progress"}, "dev/a")
    assert not claim_won({"assignee": "dev/a", "status": "open"}, "dev/a")
    assert not claim_won({"assignee": "dev/a", "status": "closed"}, "dev/a")
    assert not claim_won({"assignee": "dev/b", "status": "in_progress"}, "dev/a")
    assert not claim_won(None, "dev/a")
    assert batch_group({"labels": ["x", "batch:g1"]}) == "g1"
    assert batch_group({}) == ""


# ---- submit state --------------------------------------------------------------------------


def test_submission_is_admitted_only_for_the_current_claim_holder(world: World) -> None:
    world.beads.assignee, world.beads.status = "dev/alice", "in_progress"
    issue = world.run(lambda c: c.admit_submission(BEAD, "dev/alice"))
    assert issue["id"] == BEAD and world.beads.reads == 1

    world.beads.assignee = ""
    world.refused(lambda c: c.admit_submission(BEAD, "dev/alice"))
    assert world.output.errors[-1] == (
        f"✗ bead {BEAD} is not currently claimed — dev/alice no longer holds the claim; "
        "not submitting (re-claim it first)"
    )
    world.beads.assignee = "dev/someone-else"
    world.refused(lambda c: c.admit_submission(BEAD, "dev/alice"))
    assert "is held by dev/someone-else — dev/alice" in world.output.errors[-1]
    assert world.beads.writes == []


def test_mark_submitted_moves_review_to_pending_as_the_submitter(world: World) -> None:
    world.run(lambda c: c.mark_submitted(BEAD, "abc1234", "dev/alice"))
    assert world.states.calls == [("review", "pending", "submitted abc1234", "dev/alice")]
    world.states.fail = 4
    assert world.refused(lambda c: c.mark_submitted(BEAD, "abc1234", "dev/alice")).exit_code == 4
    assert world.output.errors == ["✗ failed to set review state — nothing submitted"]


def test_the_issue_route_maps_not_found_and_failures_distinctly(world: World) -> None:
    world.beads.missing = True
    assert world.run(lambda c: c._issues.get(BEAD)) is None
    world.beads.missing = False
    world.beads.fail = {f"GET /v0/beads/issues/{BEAD}": lambda _r: _problem(500, "internal")}
    with pytest.raises(IssueReadFailed):
        world.run(lambda c: c._issues.get(BEAD))
