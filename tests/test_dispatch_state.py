"""`beadhive.dispatch_state`'s composition seams (bh-sy36q.5): the molecule-progress,
swarm-inspection, dispatch-poll, and local-loop-state routes for `beadhive.localloop` and
`beadhive.work_dispatch` — each selected before execution, with an explicit fallback to `None`
(meaning: run the existing CLI-compatibility `bd` forward) this cohort leaves in place.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from beadhive import dispatch_state
from beadhive_beads_client import BeadsSession, ExpectedContext, RemoteEndpoint
from beadhive_beads_client.service import ServiceUnavailable
from beadhive_core import DISPATCH_CAPABILITIES

PROJECT = "proj"


def _row(bead_id, **kw):
    row = {
        "id": bead_id,
        "title": f"title {bead_id}",
        "priority": 2,
        "created_at": "2026-09-26T00:00:00Z",
        "updated_at": "2026-09-26T00:00:00Z",
        "revision": "rev-1",
        "dependency_count": 0,
        "dependent_count": 0,
        "comment_count": 0,
        "status": "open",
        "issue_type": "task",
    }
    row.update(kw)
    return row


class FakeDispatchService:
    """Serves `/v0/beads/issues/{id}`, `/v0/beads/issues`, and `/v0/beads/ready` for one fixed row
    set — the same transport fixture `packages/beadhive-core`'s own policy tests use."""

    def __init__(self, issues=None, list_rows=None, ready_rows=None):
        self.issues = dict(issues or {})
        self.list_rows = list(list_rows or [])
        self.ready_rows = list(ready_rows or [])
        self.list_calls: list[str] = []
        self.ready_calls: int = 0
        self.get_issue_calls: list[str] = []

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
                    "capabilities": sorted(DISPATCH_CAPABILITIES),
                },
            )
        if path == "/v0/beads/ready":
            self.ready_calls += 1
            return httpx.Response(200, json={"items": self.ready_rows, "has_more": False})
        if path == "/v0/beads/issues" and request.method == "GET":
            self.list_calls.append(str(request.url.params.get("parent", "")))
            return httpx.Response(200, json={"items": self.list_rows, "has_more": False})
        if path.startswith("/v0/beads/issues/") and request.method == "GET":
            bead_id = path.rsplit("/", 1)[-1]
            self.get_issue_calls.append(bead_id)
            row = self.issues.get(bead_id)
            if row is None:
                return httpx.Response(404, json={"status": 404, "code": "not_found"})
            return httpx.Response(200, json=row)
        raise AssertionError(f"unexpected request: {request.method} {path}")


def _api_session_factory(fixture: FakeDispatchService):
    def factory(_main, _entry):
        return BeadsSession(
            RemoteEndpoint("http://127.0.0.1:8080"),
            ExpectedContext(PROJECT, "bh", required_capabilities=DISPATCH_CAPABILITIES),
            transport=httpx.MockTransport(fixture.handle),
        )

    return factory


def _unavailable_factory(_main, _entry):
    raise ServiceUnavailable("no service", state="absent", start_command="bh host beads start")


# ---- molecule_progress / local_loop_state ------------------------------------------------------


def test_open_molecule_progress_routes_through_the_api(monkeypatch):
    fixture = FakeDispatchService(issues={"epic-1": _row("epic-1", status="in_progress")})
    monkeypatch.setattr(dispatch_state, "session_factory", _api_session_factory(fixture))

    row = dispatch_state.open_molecule_progress(Path("/fake/main"), {"prefix": "mr"}, "epic-1")

    assert row is not None
    assert row["status"] == "in_progress"
    assert fixture.get_issue_calls == ["epic-1"]


def test_open_molecule_progress_falls_back_to_none_when_the_service_is_unavailable(monkeypatch):
    monkeypatch.setattr(dispatch_state, "session_factory", _unavailable_factory)
    assert dispatch_state.open_molecule_progress(Path("/fake/main"), {"prefix": "mr"}, "e") is None


def test_open_local_loop_state_routes_through_the_api(monkeypatch):
    fixture = FakeDispatchService(issues={"bh-1": _row("bh-1")})
    monkeypatch.setattr(dispatch_state, "session_factory", _api_session_factory(fixture))

    row = dispatch_state.open_local_loop_state(Path("/fake/main"), {"prefix": "mr"}, "bh-1")

    assert row is not None
    assert row["id"] == "bh-1"


def test_open_local_loop_state_falls_back_to_none_when_the_service_is_unavailable(monkeypatch):
    monkeypatch.setattr(dispatch_state, "session_factory", _unavailable_factory)
    assert dispatch_state.open_local_loop_state(Path("/fake/main"), {"prefix": "mr"}, "b") is None


# ---- swarm_members / event_rows -----------------------------------------------------------------


def test_open_swarm_members_routes_through_the_api_and_narrows_to_direct_edge(monkeypatch):
    """Mirrors `test_open_children_routes_through_the_api_and_narrows_to_direct_edge`: a
    grandchild the recursive fetch also returns must not leak into the molecule's direct
    membership."""
    fixture = FakeDispatchService(
        list_rows=[
            _row("ep-1.2", parent="ep-1", priority=1),
            _row("ep-1.1.1", parent="ep-1.1"),  # grandchild — excluded
            _row("ep-1.1", parent="ep-1", issue_type="epic", priority=0),
        ]
    )
    monkeypatch.setattr(dispatch_state, "session_factory", _api_session_factory(fixture))

    members = dispatch_state.open_swarm_members(Path("/fake/main"), {"prefix": "mr"}, "ep-1")

    assert members is not None
    assert [row["id"] for row in members] == ["ep-1.2", "ep-1.1"]
    assert fixture.list_calls == ["ep-1"]


def test_open_swarm_members_falls_back_to_none_when_the_service_is_unavailable(monkeypatch):
    monkeypatch.setattr(dispatch_state, "session_factory", _unavailable_factory)
    assert dispatch_state.open_swarm_members(Path("/fake/main"), {"prefix": "mr"}, "ep-1") is None


def test_open_event_rows_routes_through_the_api_and_does_not_narrow(monkeypatch):
    """`event_rows` must NOT apply `swarm_members`'s direct-edge narrowing: an event bead can
    retain bd's historical dotted-id match without carrying a `parent` edge at all."""
    fixture = FakeDispatchService(list_rows=[_row("bh-1.1.1", issue_type="event", status="closed")])
    monkeypatch.setattr(dispatch_state, "session_factory", _api_session_factory(fixture))

    rows = dispatch_state.open_event_rows(Path("/fake/main"), {"prefix": "mr"}, "bh-1")

    assert rows is not None
    assert [row["id"] for row in rows] == ["bh-1.1.1"]


def test_open_event_rows_falls_back_to_none_when_the_service_is_unavailable(monkeypatch):
    monkeypatch.setattr(dispatch_state, "session_factory", _unavailable_factory)
    assert dispatch_state.open_event_rows(Path("/fake/main"), {"prefix": "mr"}, "bh-1") is None


# ---- poll_ready ----------------------------------------------------------------------------------


def test_open_poll_ready_routes_through_the_api(monkeypatch):
    fixture = FakeDispatchService(ready_rows=[_row("bh-1"), _row("bh-2")])
    monkeypatch.setattr(dispatch_state, "session_factory", _api_session_factory(fixture))

    rows = dispatch_state.open_poll_ready(Path("/fake/main"), {"prefix": "mr"})

    assert rows is not None
    assert [row["id"] for row in rows] == ["bh-1", "bh-2"]
    # Session negotiation itself probes `ready.list` once (`BeadsSession._negotiate`, limit=1);
    # the actual poll is the second call.
    assert fixture.ready_calls == 2


def test_open_poll_ready_falls_back_to_none_when_the_service_is_unavailable(monkeypatch):
    monkeypatch.setattr(dispatch_state, "session_factory", _unavailable_factory)
    assert dispatch_state.open_poll_ready(Path("/fake/main"), {"prefix": "mr"}) is None


def test_open_poll_ready_forwards_parent_scoping(monkeypatch):
    fixture = FakeDispatchService(ready_rows=[_row("bh-1")])
    monkeypatch.setattr(dispatch_state, "session_factory", _api_session_factory(fixture))

    dispatch_state.open_poll_ready(Path("/fake/main"), {"prefix": "mr"}, parent="ep-1")

    # No direct assertion surface on the request params here (see the package-local policy tests
    # for that); this only proves the seam forwards the kwarg through without raising. The second
    # call is the actual poll (the first is session negotiation's own `ready.list` probe).
    assert fixture.ready_calls == 2
