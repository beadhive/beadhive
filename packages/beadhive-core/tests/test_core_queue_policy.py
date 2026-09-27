"""Ready selection and claim-next policy over generated-client transport fixtures.

The Beads side is a real ``BeadsSession`` over ``httpx.MockTransport`` serving generated response
shapes (``IssueWithCounts`` / ``ReadyPage`` / ``ClaimNextResponse``) — nothing here emulates
Beads' own readiness or claim state machine. No ``bd`` process, no network, no Dolt.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx
import pytest

from beadhive_beads_client import BeadsSession, CapabilityMissing, ExpectedContext, RemoteEndpoint
from beadhive_core import (
    DECLINE_EMPTY_QUEUE,
    DECLINE_NONE_ELIGIBLE,
    QUEUE_CAPABILITIES,
    ClaimNextOutcome,
    QueueCommands,
    RoutingTable,
    by_parent,
    decline,
    eligible,
    ready_rows,
)

PROJECT = "proj"


def _row(bead_id, **kw):
    row = {
        "id": bead_id,
        "title": f"title {bead_id}",
        "priority": 2,
        "created_at": "2026-09-26T00:00:00Z",
        "updated_at": "2026-09-26T00:00:00Z",
        "dependency_count": 0,
        "dependent_count": 0,
        "comment_count": 0,
        "status": "open",
        "issue_type": "task",
    }
    row.update(kw)
    return row


# ---- pure policy: eligible / decline / by_parent --------------------------------------------


def test_eligible_preserves_ready_order_and_filters_the_untakeable():
    rows = [
        _row("bh-1", status="in_progress"),  # in flight — excluded
        _row("bh-2"),  # open, unassigned — eligible
        _row("bh-3", assignee="dev/someone-else"),  # someone else's — excluded
        _row("bh-4", assignee="dev/me"),  # already mine — eligible (idempotent resume)
        _row("bh-5", status="closed"),  # closed — excluded
        _row("bh-6", issue_type="gate"),  # infra — excluded
        _row("bh-7", issue_type="event"),  # infra — excluded
    ]
    assert eligible(rows, "dev/me") == ("bh-2", "bh-4")


def test_decline_distinguishes_empty_from_none_eligible():
    assert decline([]) == DECLINE_EMPTY_QUEUE
    assert decline([_row("bh-1", assignee="dev/someone-else")]) == DECLINE_NONE_ELIGIBLE


def test_by_parent_is_direct_edge_only_not_recursive():
    rows = [_row("bh-1", parent="ep-1"), _row("bh-2", parent="ep-2"), _row("bh-3")]
    assert [row["id"] for row in by_parent(rows, "ep-1")] == ["bh-1"]
    assert by_parent(rows, "") == rows


# ---- transport fixture: a real BeadsSession over httpx.MockTransport ------------------------


@dataclass
class FakeQueue:
    """Serves ``/v0/beads/ready`` and ``/v0/beads/issues:claimNext`` from a fixed row set."""

    rows: list[dict] = field(default_factory=list)
    claim_calls: list[str] = field(default_factory=list)
    claimed_id: str | None = None
    capabilities: frozenset[str] = field(default_factory=lambda: QUEUE_CAPABILITIES)

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
            limit = int(request.url.params.get("limit", "100"))
            return httpx.Response(
                200, json={"items": self.rows[:limit], "has_more": len(self.rows) > limit}
            )
        if path == "/v0/beads/issues:claimNext" and request.method == "POST":
            import json as _json

            body = _json.loads(request.content or b"{}")
            self.claim_calls.append(body["actor"])
            if self.claimed_id is None:
                return httpx.Response(200, json={})
            claimed = next((r for r in self.rows if r["id"] == self.claimed_id), None)
            assert claimed is not None
            row = dict(claimed, assignee=body["actor"], status="in_progress")
            return httpx.Response(200, json={"claimed": row})
        raise AssertionError(f"unexpected request: {request.method} {path}")


def _session(fixture: FakeQueue) -> BeadsSession:
    return BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext(PROJECT, "bh", required_capabilities=QUEUE_CAPABILITIES),
        transport=httpx.MockTransport(fixture.handle),
    )


def test_list_ready_returns_rows_in_bd_ready_json_shape():
    fixture = FakeQueue(rows=[_row("bh-1"), _row("bh-2")])
    commands = QueueCommands()
    with _session(fixture) as session:
        page = commands.list_ready(session, limit=50)
    assert [row["id"] for row in ready_rows(page)] == ["bh-1", "bh-2"]
    assert ready_rows(page)[0]["issue_type"] == "task"


def test_claim_next_returns_the_claimed_row_and_carries_the_actor():
    fixture = FakeQueue(rows=[_row("bh-1")], claimed_id="bh-1")
    commands = QueueCommands()
    with _session(fixture) as session:
        outcome = commands.claim_next(session, "dev/alice")
    assert outcome.claimed is not None
    assert outcome.claimed["id"] == "bh-1"
    assert outcome.claimed["assignee"] == "dev/alice"
    assert outcome.claimed["status"] == "in_progress"
    assert fixture.claim_calls == ["dev/alice"]


def test_claim_next_reports_none_when_nothing_was_eligible():
    fixture = FakeQueue(rows=[], claimed_id=None)
    commands = QueueCommands()
    with _session(fixture) as session:
        outcome = commands.claim_next(session, "dev/alice")
    assert outcome == ClaimNextOutcome(None)


def test_claim_next_fails_before_any_request_when_the_capability_is_missing():
    """The pre-execution selection: a session negotiated without `issues.claimNext` never
    reaches the write — `select_api` raises before `call_api` invokes the session method, which
    is exactly what the shell composition seam catches to pick the CLI-compatibility route."""
    narrow_capabilities = frozenset({"project.enforce", "issues.get", "ready.list"})
    fixture = FakeQueue(rows=[_row("bh-1")], claimed_id="bh-1", capabilities=narrow_capabilities)
    commands = QueueCommands(RoutingTable.from_matrix())
    narrow = BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext(PROJECT, "bh", required_capabilities=narrow_capabilities),
        transport=httpx.MockTransport(fixture.handle),
    )
    with narrow as session:
        with pytest.raises(CapabilityMissing):
            commands.claim_next(session, "dev/alice")
    assert fixture.claim_calls == []  # never reached: refused before the write, not after it
