"""Routing table policy: every operation resolves to exactly one named route.

Pure classification/capability tests read the installed operation matrix directly — no Beads
state emulator. The one dispatch test below proves ``call_api`` truly invokes the named
``BeadsSession`` method (not a stub duck-type) using the same generated-client transport fixture
pattern as ``test_core_review_policy.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from beadhive_beads_client import (
    BeadsSession,
    CapabilityMissing,
    ExpectedContext,
    RemoteEndpoint,
    load_operation_matrix,
)
from beadhive_core import (
    COORDINATION_OPERATIONS,
    ApiRoute,
    CompatibilityRoute,
    DeniedRoute,
    OperationDenied,
    RouteKind,
    RouteMismatch,
    RoutingTable,
    UnknownOperation,
    default_table,
)

MATRIX = load_operation_matrix()
TABLE = default_table()


# ---- one route per operation, matching the matrix exactly ----------------------------------


def test_every_matrix_row_resolves_to_exactly_one_route_of_the_matching_type() -> None:
    assert TABLE.names() == {row["name"] for row in MATRIX["operations"]}
    for row in MATRIX["operations"]:
        route = TABLE.route(row["name"])
        kind = RouteKind(row["classification"])
        assert route.kind is kind
        if kind is RouteKind.API_READY:
            assert isinstance(route, ApiRoute)
            assert route.capability == row["capability"]
            assert route.session_method == row["session_method"]
        elif kind is RouteKind.DENIED:
            assert isinstance(route, DeniedRoute)
            assert route.reason == row["reason"]
        else:
            assert isinstance(route, CompatibilityRoute)
            assert route.reason == row["reason"]


def test_from_matrix_rejects_a_fifth_classification() -> None:
    bogus = {"operations": [{"name": "x.y", "classification": "mystery"}]}
    with pytest.raises(ValueError):
        RoutingTable.from_matrix(bogus)


def test_unknown_operation_is_a_key_error_not_a_silent_default() -> None:
    with pytest.raises(UnknownOperation):
        TABLE.route("work.does-not-exist")


def test_default_table_is_a_cached_singleton() -> None:
    assert default_table() is TABLE


# ---- denied is unroutable, period --------------------------------------------------------


@pytest.mark.parametrize("name", ["issues.delete", "issues.sweep"])
def test_denied_operations_are_unroutable_through_every_selector(name: str) -> None:
    with pytest.raises(OperationDenied):
        TABLE.select_api(name, capabilities=frozenset({"issues.delete", "issues.sweep"}))
    with pytest.raises(OperationDenied):
        TABLE.select_cli(name)
    with pytest.raises(OperationDenied):
        TABLE.select_administrative(name)


# ---- api-ready: capability-checked before any mutation is reachable ------------------------


def test_select_api_fails_before_mutation_when_the_session_lacks_the_capability() -> None:
    with pytest.raises(CapabilityMissing):
        TABLE.select_api("work.claim.acquire", capabilities=frozenset({"issues.get"}))


def test_select_api_returns_the_named_session_method_when_capable() -> None:
    route = TABLE.select_api("work.ready.list", capabilities=frozenset({"ready.list"}))
    assert route.session_method == "list_ready"
    assert route.capability == "ready.list"


def test_select_api_notifies_the_observer_with_the_selected_route() -> None:
    @dataclass
    class Observer:
        calls: list[tuple[str, str]] = field(default_factory=list)

        def selected(self, name: str, kind: str) -> None:
            self.calls.append((name, kind))

    observer = Observer()
    TABLE.select_api("work.ready.list", capabilities=frozenset({"ready.list"}), observer=observer)
    assert observer.calls == [("work.ready.list", "api-ready")]


def test_select_api_never_falls_back_to_a_cli_route() -> None:
    """Asking the API selector for a cli-compatibility operation is a caller bug, not a retry."""
    with pytest.raises(RouteMismatch):
        TABLE.select_api("work.gate.resolve", capabilities=frozenset({"issues.update"}))


# ---- cli-compatibility / administrative: named, never executed here ------------------------


def test_select_cli_rejects_an_api_ready_operation() -> None:
    with pytest.raises(RouteMismatch):
        TABLE.select_cli("work.ready.list")


def test_select_cli_rejects_an_administrative_operation() -> None:
    with pytest.raises(RouteMismatch):
        TABLE.select_cli("admin.sync")


def test_select_administrative_rejects_a_cli_compatibility_operation() -> None:
    with pytest.raises(RouteMismatch):
        TABLE.select_administrative("work.gate.resolve")


def test_select_administrative_names_the_reason_for_admin_sync() -> None:
    route = TABLE.select_administrative("admin.sync")
    assert route.kind is RouteKind.ADMINISTRATIVE
    assert "Dolt publication" in route.reason


@pytest.mark.parametrize(
    "name", ["work.gate.lookup", "work.gate.create", "work.gate.resolve", "work.state.update"]
)
def test_select_cli_names_the_reason_it_has_no_http_route(name: str) -> None:
    route = TABLE.select_cli(name)
    assert route.kind is RouteKind.CLI_COMPATIBILITY
    assert route.reason


# ---- concurrency-sensitive coordination ops never move to api-ready silently ----------------


@pytest.mark.parametrize("name", COORDINATION_OPERATIONS)
def test_coordination_operations_stay_on_the_proven_cli_path(name: str) -> None:
    """Lease, heartbeat, reclaim, merge-slot and gate guarantees come from real bd, never an
    in-memory stand-in — this fails the day the matrix moves one of these to api-ready without a
    deliberate, evidenced bead."""
    assert TABLE.route(name).kind is RouteKind.CLI_COMPATIBILITY


# ---- call_api: the one dispatch point, proven against a real generated-client transport ----


@dataclass
class FakeReady:
    hits: list[int] = field(default_factory=list)

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
                    "capabilities": ["project.enforce", "issues.get", "issues.list", "ready.list"],
                },
            )
        assert path == "/v0/beads/ready"
        self.hits.append(int(request.url.params.get("limit", "0")))
        return httpx.Response(200, json={"items": [], "has_more": False})


def _session(fixture: FakeReady) -> BeadsSession:
    return BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext(
            "proj",
            "bh",
            required_capabilities=frozenset(
                {"project.enforce", "issues.get", "issues.list", "ready.list"}
            ),
        ),
        transport=httpx.MockTransport(fixture.handle),
    )


def test_call_api_dispatches_the_named_session_method_not_a_generic_transport() -> None:
    fixture = FakeReady()
    with _session(fixture) as session:
        page = TABLE.call_api(session, "work.ready.list", limit=7)
    assert page.items == []
    # negotiation itself calls list_ready once with limit=1; call_api's own call carries limit=7.
    assert fixture.hits[-1] == 7


def test_call_api_refuses_before_the_session_is_open() -> None:
    fixture = FakeReady()
    session = _session(fixture)
    with pytest.raises(RuntimeError):
        TABLE.call_api(session, "work.ready.list", limit=1)


def test_call_api_fails_before_mutation_when_the_capability_is_missing() -> None:
    fixture = FakeReady()
    with _session(fixture) as session:
        with pytest.raises(CapabilityMissing):
            TABLE.call_api(session, "work.claim.acquire", "bh-1", body=None)


def test_call_api_never_selects_a_cli_compatibility_route() -> None:
    fixture = FakeReady()
    with _session(fixture) as session:
        with pytest.raises(RouteMismatch):
            TABLE.call_api(session, "work.gate.resolve", "bh-1")
