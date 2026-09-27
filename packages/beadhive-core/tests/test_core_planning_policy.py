"""Molecule filing policy: the pure compiler, plus the BatchApply route over a transport fixture
and fake gate/kickoff/swarm ports.

The Beads side of the api-ready ``plan.batch-apply.atomic`` route is a real ``BeadsSession`` over
``httpx.MockTransport`` serving a generated ``ApplyBatchResponse``. The gate/kickoff/swarm
compatibility rows are a fake :class:`~beadhive_core.planning.PlanningGates` — they have no v1.3
HTTP route at all (bh-sy36q.2 narrows verify/repair out; only filing lands here).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest

from beadhive_beads_client import (
    BeadsSession,
    ExpectedContext,
    RemoteEndpoint,
    cli_compatibility_operations,
)
from beadhive_core import (
    BATCH_APPLY_ROUTE,
    PLANNING_CAPABILITIES,
    PLANNING_GATE_ROUTES,
    FileOutcome,
    GateCreateFailed,
    MoleculeFilingFailed,
    MoleculeTooLarge,
    PlanningCommands,
    PlanningError,
    SessionMoleculeFiler,
    compile_molecule,
)

DIMENSIONS = ("complexity", "component")
TRIPLET = ("provider:acme", "org:beadhive", "repo:beadhive")


def _spec(**overrides: Any) -> dict[str, Any]:
    spec = {
        "epic": {"title": "Epic", "description": "epic desc"},
        "issues": [
            {"handle": "root", "title": "Root", "type": "task", "acceptance": "a", "deps": []},
            {
                "handle": "leaf",
                "title": "Leaf",
                "type": "task",
                "acceptance": "b",
                "deps": ["root"],
                "release": "breaking",
            },
        ],
    }
    spec.update(overrides)
    return spec


# ---- pure compiler ---------------------------------------------------------------------------


def test_compile_molecule_emits_one_request_with_keyed_creates_then_edges() -> None:
    compiled = compile_molecule(_spec(), dimension_fields=DIMENSIONS, identity_labels=TRIPLET)
    kinds = [item.kind.value for item in compiled.items]
    assert kinds == ["create", "create", "create", "dep_add", "dep_add", "dep_add"]
    assert compiled.create_count == 3  # epic + 2 issues
    assert compiled.edge_count == 3  # 2 parent-child + 1 blocks
    creates = [item.create for item in compiled.items if item.kind.value == "create"]
    assert [c.key for c in creates] == ["epic", "issue:root", "issue:leaf"]
    assert creates[0].issue_type == "epic"
    assert creates[1].labels == list(TRIPLET)  # no complexity/component set on "root"
    edges = [item.dep_add for item in compiled.items if item.kind.value == "dep_add"]
    assert (edges[0].source.key, edges[0].target.key, edges[0].type_) == (
        "issue:root",
        "epic",
        "parent-child",
    )
    assert (edges[1].source.key, edges[1].target.key, edges[1].type_) == (
        "issue:leaf",
        "epic",
        "parent-child",
    )
    assert (edges[2].source.key, edges[2].target.key, edges[2].type_) == (
        "issue:leaf",
        "issue:root",
        "blocks",
    )


def test_compile_molecule_carries_dimension_and_identity_labels() -> None:
    spec = _spec()
    spec["issues"][0]["complexity"] = "MEDIUM"
    spec["issues"][0]["component"] = "planner"
    compiled = compile_molecule(spec, dimension_fields=DIMENSIONS, identity_labels=TRIPLET)
    root_create = compiled.items[1].create
    assert root_create.labels == ["complexity:MEDIUM", "component:planner", *TRIPLET]


def test_compile_molecule_preview_and_apply_share_the_exact_lowering() -> None:
    """The compiler is pure: the same spec compiles to a byte-identical request twice."""
    spec = _spec()
    first = compile_molecule(spec, dimension_fields=DIMENSIONS, identity_labels=TRIPLET)
    second = compile_molecule(spec, dimension_fields=DIMENSIONS, identity_labels=TRIPLET)
    assert first.request(actor="dev/alice").to_dict() == second.request(actor="dev/alice").to_dict()


def test_compile_molecule_with_existing_epic_id_has_no_epic_create_and_links_reports() -> None:
    compiled = compile_molecule(
        _spec(),
        dimension_fields=DIMENSIONS,
        epic_id="bh-9",
        adopted_reports=["bh-report-1"],
    )
    creates = [item.create.key for item in compiled.items if item.kind.value == "create"]
    assert "epic" not in creates
    assert compiled.create_count == 2  # only the two issues
    parent_edges = [
        item.dep_add
        for item in compiled.items
        if item.kind.value == "dep_add" and item.dep_add.type_ == "parent-child"
    ]
    targets = {(edge.source.id or edge.source.key, edge.target.id) for edge in parent_edges}
    assert ("bh-report-1", "bh-9") in targets
    assert ("issue:root", "bh-9") in {(e.source.key, e.target.id) for e in parent_edges}


def test_compile_molecule_refuses_an_oversized_molecule_without_chunking() -> None:
    big = _spec(
        issues=[
            {"handle": f"h{i}", "title": f"issue {i}", "type": "task", "deps": []}
            for i in range(60)
        ]
    )
    with pytest.raises(MoleculeTooLarge) as failure:
        compile_molecule(big, dimension_fields=DIMENSIONS)
    assert failure.value.total == 61 + 60  # 61 creates (epic+60) + 60 parent-child edges
    assert failure.value.cap == 100


def test_compile_molecule_rejects_a_cyclic_or_unknown_dependency_graph() -> None:
    with pytest.raises(PlanningError, match="unknown handles"):
        compile_molecule(
            _spec(issues=[{"handle": "a", "title": "A", "deps": ["missing"]}]), dimension_fields=()
        )
    cyclic = _spec(
        issues=[
            {"handle": "a", "title": "A", "deps": ["b"]},
            {"handle": "b", "title": "B", "deps": ["a"]},
        ]
    )
    with pytest.raises(PlanningError, match="cycle"):
        compile_molecule(cyclic, dimension_fields=())


# ---- routing traceability -------------------------------------------------------------------


def test_batch_apply_route_is_api_ready_and_gate_routes_stay_cli_compatibility() -> None:
    assert BATCH_APPLY_ROUTE == "plan.batch-apply.atomic"
    assert BATCH_APPLY_ROUTE not in cli_compatibility_operations()
    assert set(PLANNING_GATE_ROUTES) <= cli_compatibility_operations()


# ---- the api-ready BatchApply route over a transport fixture ---------------------------------


@dataclass
class BatchService:
    """Serves ``issues:batchApply``: mints sequential ids for every create item, in order."""

    next_id: int = 1
    fail: dict = field(default_factory=dict)
    requests: list[dict] = field(default_factory=list)

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
                    "capabilities": sorted(PLANNING_CAPABILITIES),
                },
            )
        if path == "/v0/beads/ready":
            return httpx.Response(200, json={"items": [], "has_more": False})
        assert path == "/v0/beads/issues:batchApply"
        body = json.loads(request.content or b"{}")
        self.requests.append(body)
        if "batch" in self.fail:
            return self.fail["batch"](request)
        keys: dict[str, str] = {}
        items = []
        for item in body["items"]:
            if item["kind"] == "create":
                new_id = f"bh-{self.next_id}"
                self.next_id += 1
                if "key" in item["create"]:
                    keys[item["create"]["key"]] = new_id
                items.append(
                    {"kind": "create", "issue_id": new_id, "changed": True, "revision": "1"}
                )
            else:
                items.append(
                    {
                        "kind": "dep_add",
                        "issue_id": "n/a",
                        "changed": True,
                        "revision": "0",
                        "depends_on_id": "n/a",
                    }
                )
        return httpx.Response(200, json={"keys": keys, "items": items})


@dataclass
class Gates:
    swarm: list[str] = field(default_factory=list)
    kickoff_gates: list[tuple[str, str]] = field(default_factory=list)
    pending: list[str] = field(default_factory=list)
    release_holds: list[tuple[str, str]] = field(default_factory=list)
    swarm_fails: bool = False

    def create_swarm(self, epic_id: str, *, actor: str) -> bool:
        self.swarm.append(epic_id)
        return not self.swarm_fails

    def create_kickoff_gate(self, root_id: str, epic_id: str, *, actor: str) -> None:
        self.kickoff_gates.append((root_id, epic_id))

    def set_kickoff_pending(self, epic_id: str, *, actor: str) -> None:
        self.pending.append(epic_id)

    def create_release_hold_gate(self, bead_id: str, epic_id: str, *, actor: str) -> None:
        self.release_holds.append((bead_id, epic_id))


def _session(service: BatchService) -> BeadsSession:
    return BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext("proj", "bh", required_capabilities=PLANNING_CAPABILITIES),
        transport=httpx.MockTransport(service.handle),
    )


def test_planning_commands_file_compiles_and_applies_one_request_then_opens_conventions() -> None:
    service = BatchService()
    gates = Gates()
    with _session(service) as session:
        commands = PlanningCommands(SessionMoleculeFiler(session), gates)
        outcome = commands.file(
            _spec(),
            actor="dev/alice",
            dimension_fields=DIMENSIONS,
            identity_labels=TRIPLET,
            release_breaking_handles=("leaf",),
        )
    assert len(service.requests) == 1  # ONE BatchApply request for the whole molecule
    assert isinstance(outcome, FileOutcome)
    assert outcome.epic_id == "bh-1"
    assert outcome.issue_count == 2
    assert outcome.root_count == 1
    assert gates.swarm == ["bh-1"]
    assert gates.kickoff_gates == [("bh-2", "bh-1")]  # only the root, not the leaf
    assert gates.pending == ["bh-1"]
    assert gates.release_holds == [("bh-3", "bh-1")]  # leaf is release:breaking


def test_planning_commands_file_with_existing_epic_id_skips_the_epic_create() -> None:
    service = BatchService(next_id=5)
    gates = Gates()
    with _session(service) as session:
        commands = PlanningCommands(SessionMoleculeFiler(session), gates)
        outcome = commands.file(
            _spec(),
            actor="dev/alice",
            dimension_fields=DIMENSIONS,
            epic_id="bh-imported",
            adopted_reports=("bh-report-1",),
        )
    assert outcome.epic_id == "bh-imported"
    assert outcome.adopt_count == 1
    assert gates.swarm == ["bh-imported"]
    request_items = service.requests[0]["items"]
    assert not any(
        i["kind"] == "create" and i["create"].get("issue_type") == "epic" for i in request_items
    )


def test_planning_commands_file_refuses_before_any_http_call_when_oversized() -> None:
    service = BatchService()
    gates = Gates()
    big = _spec(
        issues=[
            {"handle": f"h{i}", "title": f"issue {i}", "type": "task", "deps": []}
            for i in range(60)
        ]
    )
    with _session(service) as session:
        commands = PlanningCommands(SessionMoleculeFiler(session), gates)
        with pytest.raises(MoleculeTooLarge):
            commands.file(big, actor="dev/alice", dimension_fields=())
    assert service.requests == []  # never sent


def test_planning_commands_file_raises_when_swarm_creation_fails() -> None:
    service = BatchService()
    gates = Gates(swarm_fails=True)
    with _session(service) as session:
        commands = PlanningCommands(SessionMoleculeFiler(session), gates)
        with pytest.raises(GateCreateFailed):
            commands.file(_spec(), actor="dev/alice", dimension_fields=DIMENSIONS)


def test_session_molecule_filer_maps_a_refused_batch_to_molecule_filing_failed() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"status": 400, "title": "bad", "code": "invalid", "request_id": "r1"},
        )

    service = BatchService(fail={"batch": refuse})
    gates = Gates()
    with _session(service) as session:
        commands = PlanningCommands(SessionMoleculeFiler(session), gates)
        with pytest.raises(MoleculeFilingFailed):
            commands.file(_spec(), actor="dev/alice", dimension_fields=DIMENSIONS)
