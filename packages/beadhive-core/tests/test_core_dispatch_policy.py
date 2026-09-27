"""Molecule progress, swarm inspection, dispatch polling, and local-loop bead-state access
(bh-sy36q.5) over generated-client transport fixtures.

The Beads side is a real ``BeadsSession`` over ``httpx.MockTransport`` serving generated response
shapes (``IssueDetails`` / ``IssuesPage`` / ``ReadyPage``) — nothing here emulates a FakeBd state
machine or Beads' own storage. No ``bd`` process, no network, no Dolt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from beadhive_beads_client import BeadsSession, CapabilityMissing, ExpectedContext, RemoteEndpoint
from beadhive_core import (
    DISPATCH_CAPABILITIES,
    DISPATCH_POLL_ROUTE,
    DISPATCH_ROUTES,
    LOCAL_LOOP_STATE_ROUTE,
    MOLECULE_PROGRESS_ROUTE,
    SWARM_INSPECT_ROUTE,
    DispatchCommands,
    RoutingTable,
)

PROJECT = "proj"


def _issue_row(bead_id, **kw):
    row = {
        "id": bead_id,
        "title": f"title {bead_id}",
        "priority": 2,
        "created_at": "2026-09-26T00:00:00Z",
        "updated_at": "2026-09-26T00:00:00Z",
        "revision": "rev-1",
        "status": "open",
        "issue_type": "task",
        "dependency_count": 0,
        "dependent_count": 0,
        "comment_count": 0,
    }
    row.update(kw)
    return row


def test_route_names_are_named_in_the_installed_matrix():
    """Traceability (bh-sy36q.5's own acceptance): every operation this cohort touches is one of
    the bh-97fo0.3 matrix's named routes, resolved through the routing table at import time — this
    fails loudly rather than silently the day the installed matrix reclassifies one."""
    assert DISPATCH_ROUTES == (
        "work.molecule.progress",
        "work.swarm.inspect",
        "work.dispatch.poll",
        "work.local-loop.state",
    )
    assert MOLECULE_PROGRESS_ROUTE == "work.molecule.progress"
    assert SWARM_INSPECT_ROUTE == "work.swarm.inspect"
    assert DISPATCH_POLL_ROUTE == "work.dispatch.poll"
    assert LOCAL_LOOP_STATE_ROUTE == "work.local-loop.state"


# ---- transport fixture: a real BeadsSession over httpx.MockTransport ------------------------


@dataclass
class FakeDispatch:
    """Serves ``/v0/beads/issues/{id}``, ``/v0/beads/issues``, and ``/v0/beads/ready`` from a
    fixed row set. ``seen_params`` records the last query string per path so a test can assert a
    caller's parameters actually reached the request."""

    issues: dict[str, dict] = field(default_factory=dict)
    list_rows: list[dict] = field(default_factory=list)
    ready_rows: list[dict] = field(default_factory=list)
    capabilities: frozenset[str] = field(default_factory=lambda: DISPATCH_CAPABILITIES)
    seen_params: dict[str, httpx.QueryParams] = field(default_factory=dict)
    get_issue_calls: list[str] = field(default_factory=list)

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
                    "project_id": PROJECT,
                    "capabilities": sorted(self.capabilities),
                },
            )
        if path == "/v0/beads/ready":
            self.seen_params[path] = request.url.params
            limit = int(request.url.params.get("limit", "100"))
            rows = self.ready_rows[:limit] if limit else self.ready_rows
            return httpx.Response(200, json={"items": rows, "has_more": False})
        if path == "/v0/beads/issues" and request.method == "GET":
            self.seen_params[path] = request.url.params
            return httpx.Response(200, json={"items": self.list_rows, "has_more": False})
        if path.startswith("/v0/beads/issues/") and request.method == "GET":
            bead_id = path.rsplit("/", 1)[-1]
            self.get_issue_calls.append(bead_id)
            row = self.issues.get(bead_id)
            if row is None:
                return httpx.Response(404, json={"status": 404, "code": "not_found"})
            return httpx.Response(200, json=row)
        raise AssertionError(f"unexpected request: {request.method} {path}")


def _session(fixture: FakeDispatch) -> BeadsSession:
    return BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext(PROJECT, "bh", required_capabilities=DISPATCH_CAPABILITIES),
        transport=httpx.MockTransport(fixture.handle),
    )


# ---- molecule_progress / local_loop_state (issues.get) ---------------------------------------


def test_molecule_progress_reads_one_bead_detail():
    fixture = FakeDispatch(issues={"epic-1": _issue_row("epic-1", issue_type="epic")})
    commands = DispatchCommands()
    with _session(fixture) as session:
        row = commands.molecule_progress(session, "epic-1")
    assert row["id"] == "epic-1"
    assert row["status"] == "open"
    assert fixture.get_issue_calls == ["epic-1"]


def test_molecule_progress_is_re_derived_every_call_not_cached():
    """Restart is a no-op: two calls against a bead whose status changed between them both hit the
    wire and both answer fresh — nothing here memoizes across passes."""
    fixture = FakeDispatch(issues={"epic-1": _issue_row("epic-1", status="in_progress")})
    commands = DispatchCommands()
    with _session(fixture) as session:
        first = commands.molecule_progress(session, "epic-1")
        fixture.issues["epic-1"] = _issue_row("epic-1", status="closed")
        second = commands.molecule_progress(session, "epic-1")
    assert first["status"] == "in_progress"
    assert second["status"] == "closed"
    assert fixture.get_issue_calls == ["epic-1", "epic-1"]


def test_local_loop_state_is_a_distinct_named_route_over_the_same_capability():
    fixture = FakeDispatch(issues={"b1": _issue_row("b1")})
    commands = DispatchCommands()
    calls = []

    class Observer:
        def selected(self, name, kind):
            calls.append((name, kind))

    with _session(fixture) as session:
        commands.local_loop_state(session, "b1", observer=Observer())
    assert calls == [("work.local-loop.state", "api-ready")]


def test_molecule_progress_and_local_loop_state_are_attributed_separately():
    fixture = FakeDispatch(issues={"b1": _issue_row("b1")})
    commands = DispatchCommands()
    calls = []

    class Observer:
        def selected(self, name, kind):
            calls.append(name)

    with _session(fixture) as session:
        commands.molecule_progress(session, "b1", observer=Observer())
        commands.local_loop_state(session, "b1", observer=Observer())
    assert calls == ["work.molecule.progress", "work.local-loop.state"]


# ---- swarm_members / event_rows (issues.list) -------------------------------------------------


def test_swarm_members_asks_for_the_full_default_exclusion_override():
    fixture = FakeDispatch(
        list_rows=[_issue_row("epic-1.1", parent="epic-1"), _issue_row("epic-1.2", parent="epic-1")]
    )
    commands = DispatchCommands()
    with _session(fixture) as session:
        rows = commands.swarm_members(session, "epic-1")
    assert [row["id"] for row in rows] == ["epic-1.1", "epic-1.2"]
    seen = fixture.seen_params["/v0/beads/issues"]
    assert seen["parent"] == "epic-1"
    assert seen["all"] == "true"
    assert seen["include_infra"] == "true"
    assert seen["sort"] == "priority"
    assert seen["limit"] == "0"


def test_swarm_members_narrows_a_recursive_fetch_to_the_direct_edge():
    """`GET /v0/beads/issues?parent=<epic>` is recursive; a grandchild must not be counted as a
    direct member of the epic's own molecule."""
    fixture = FakeDispatch(
        list_rows=[
            _issue_row("bh-1", parent="epic-1"),
            _issue_row("bh-1.1", parent="bh-1"),  # grandchild — excluded
        ]
    )
    commands = DispatchCommands()
    with _session(fixture) as session:
        rows = commands.swarm_members(session, "epic-1")
    assert [row["id"] for row in rows] == ["bh-1"]


def test_swarm_members_includes_closed_and_infra_rows():
    fixture = FakeDispatch(
        list_rows=[
            _issue_row("bh-1", parent="epic-1", status="closed"),
            _issue_row("bh-1.1.1", parent="epic-1", issue_type="event", status="closed"),
        ]
    )
    commands = DispatchCommands()
    with _session(fixture) as session:
        rows = commands.swarm_members(session, "epic-1")
    assert {row["id"] for row in rows} == {"bh-1", "bh-1.1.1"}


def test_event_rows_does_not_narrow_to_the_direct_edge():
    """An event bead can retain bd's historical dotted-id-prefix match even without a `parent`
    edge — narrowing here (the way `swarm_members` does) would silently drop it."""
    fixture = FakeDispatch(list_rows=[_issue_row("bh-1.1.1", issue_type="event", status="closed")])
    commands = DispatchCommands()
    with _session(fixture) as session:
        rows = commands.event_rows(session, "bh-1")
    assert [row["id"] for row in rows] == ["bh-1.1.1"]


# ---- poll_ready (ready.list) -------------------------------------------------------------------


def test_poll_ready_returns_bd_ready_json_shaped_rows():
    fixture = FakeDispatch(ready_rows=[_issue_row("bh-1"), _issue_row("bh-2")])
    commands = DispatchCommands()
    with _session(fixture) as session:
        rows = commands.poll_ready(session)
    assert [row["id"] for row in rows] == ["bh-1", "bh-2"]
    assert fixture.seen_params["/v0/beads/ready"]["limit"] == "0"


def test_poll_ready_is_re_derived_every_call():
    """`bd ready` transitions are seen by re-polling, never by a mirrored journal (module
    docstring): the same call against a wire whose ready set shrank sees the shrink immediately."""
    fixture = FakeDispatch(ready_rows=[_issue_row("bh-1"), _issue_row("bh-2")])
    commands = DispatchCommands()
    with _session(fixture) as session:
        first = commands.poll_ready(session)
        fixture.ready_rows = [_issue_row("bh-2")]
        second = commands.poll_ready(session)
    assert [row["id"] for row in first] == ["bh-1", "bh-2"]
    assert [row["id"] for row in second] == ["bh-2"]


def test_poll_ready_forwards_parent_scoping():
    fixture = FakeDispatch(ready_rows=[_issue_row("bh-1")])
    commands = DispatchCommands()
    with _session(fixture) as session:
        commands.poll_ready(session, parent="epic-1")
    assert fixture.seen_params["/v0/beads/ready"]["parent"] == "epic-1"


# ---- capability negotiation --------------------------------------------------------------------


@pytest.mark.parametrize(
    "missing",
    ["issues.get", "issues.list", "ready.list"],
)
def test_missing_capability_is_refused_before_any_request(missing):
    """The pre-execution selection: a session negotiated without the route's capability never
    reaches the read — `select_api` raises before `call_api` invokes the session method, exactly
    what the shell composition seam catches to select the CLI-compatibility route instead."""
    narrow_capabilities = DISPATCH_CAPABILITIES - {missing}
    fixture = FakeDispatch(capabilities=narrow_capabilities)
    commands = DispatchCommands()
    narrow = BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext(PROJECT, "bh", required_capabilities=narrow_capabilities),
        transport=httpx.MockTransport(fixture.handle),
    )
    with narrow as session:
        with pytest.raises(CapabilityMissing):
            if missing == "issues.get":
                commands.molecule_progress(session, "b1")
            elif missing == "issues.list":
                commands.swarm_members(session, "epic-1")
            else:
                commands.poll_ready(session)
    assert fixture.get_issue_calls == []


def test_commands_accept_an_explicit_routing_table():
    """The same constructor-injection seam `QueueCommands` has, so a test can substitute a
    fixture matrix without touching the process-wide default."""
    fixture = FakeDispatch(issues={"b1": _issue_row("b1")})
    commands = DispatchCommands(routing=RoutingTable.from_matrix())
    with _session(fixture) as session:
        row = commands.molecule_progress(session, "b1")
    assert row["id"] == "b1"
