"""Ready selection and claim-next policy over generated-client transport fixtures.

The Beads side is a real ``BeadsSession`` over ``httpx.MockTransport`` serving generated response
shapes (``IssueWithCounts`` / ``ReadyPage`` / ``ClaimNextResponse``) — nothing here emulates
Beads' own readiness or claim state machine. No ``bd`` process, no network, no Dolt.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

import httpx
import pytest

from beadhive_beads_client import (
    BeadsSession,
    CapabilityMissing,
    ExpectedContext,
    RemoteEndpoint,
    ServiceProblem,
)
from beadhive_core import (
    DECLINE_ALL_LOST,
    DECLINE_EMPTY_QUEUE,
    DECLINE_NONE_ELIGIBLE,
    DECLINES,
    EPIC_DECLINES,
    QUEUE_CAPABILITIES,
    ClaimNextOutcome,
    EpicClaimOutcome,
    QueueCommands,
    RoutingTable,
    by_parent,
    decline,
    decline_after,
    direct_children,
    eligible,
    molecule_scope,
    ready_rows,
    to_bd_json,
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


# ---- pure policy: direct_children (bh-mu5yb.1) -----------------------------------------------


def test_direct_children_trusts_top_level_parent_field():
    rows = [_row("bh-1", parent="ep-1"), _row("bh-2", parent="ep-2"), _row("bh-3")]
    assert [row["id"] for row in direct_children(rows, "ep-1")] == ["bh-1"]
    assert direct_children(rows, "") == rows


def test_direct_children_also_trusts_the_dependencies_representation():
    """A row can carry the edge ONLY as a `parent-child` dependency entry (e.g. a closed parent —
    Beads omits the top-level `parent` field then) — `by_parent` alone would miss it."""
    rows = [
        _row("bh-1", dependencies=[{"type": "parent-child", "depends_on_id": "ep-1"}]),
        _row("bh-2", dependencies=[{"type": "blocks", "depends_on_id": "ep-1"}]),  # wrong edge type
        _row("bh-3"),
    ]
    assert [row["id"] for row in direct_children(rows, "ep-1")] == ["bh-1"]
    assert by_parent(rows, "ep-1") == []  # the field-only check misses it — the gap this closes


def test_direct_children_excludes_a_recursive_grandchild():
    """The row a `parent=<epic>` HTTP fetch returns for a GRANDCHILD (the query parameter is
    documented as recursive-descendant, unlike this row-level check) is excluded: its own `parent`
    names the intermediate child, not the top-level epic."""
    rows = [
        _row("bh-1", parent="ep-1"),  # direct child
        _row("bh-1.1", parent="bh-1"),  # grandchild — recursively under ep-1, but not direct
    ]
    assert [row["id"] for row in direct_children(rows, "ep-1")] == ["bh-1"]


# ---- pure policy: to_bd_json (bh-mu5yb.1) --------------------------------------------------


def test_to_bd_json_matches_a_captured_real_bd_sample():
    """Frozen fixture captured verbatim from `bd ready --json` against a real disposable `bd
    serve` 1.3.0 (f45b249ce) scratch hive as part of verifying this bead — see
    `packages/beadhive-core/README.md`. Guards the encoder against regressing on the two
    Go/Python JSON divergences (HTML-escaping, non-ASCII) it exists to reconcile."""
    rows = [
        {
            "id": "shp-00h.1",
            "title": 'emoji \U0001f600 title <tag> & "quote" café line sep',
            "status": "open",
            "priority": 2,
            "issue_type": "task",
            "created_at": "2026-09-27T09:00:00Z",
            "created_by": "Brian Cripe",
            "updated_at": "2026-09-27T09:00:00Z",
            "dependency_count": 0,
            "dependent_count": 0,
            "comment_count": 0,
        }
    ]
    expected = (
        "[\n"
        "  {\n"
        '    "id": "shp-00h.1",\n'
        '    "title": "emoji \U0001f600 title \\u003ctag\\u003e \\u0026 \\"quote\\" '
        'café line\\u2028sep",\n'
        '    "status": "open",\n'
        '    "priority": 2,\n'
        '    "issue_type": "task",\n'
        '    "created_at": "2026-09-27T09:00:00Z",\n'
        '    "created_by": "Brian Cripe",\n'
        '    "updated_at": "2026-09-27T09:00:00Z",\n'
        '    "dependency_count": 0,\n'
        '    "dependent_count": 0,\n'
        '    "comment_count": 0\n'
        "  }\n"
        "]\n"
    )
    assert to_bd_json(rows) == expected


def test_to_bd_json_empty_list_matches_bd_ready_empty_output():
    assert to_bd_json([]) == "[]\n"


# ---- transport fixture: a real BeadsSession over httpx.MockTransport ------------------------


@dataclass
class FakeQueue:
    """Serves ``/v0/beads/ready``, ``/v0/beads/issues``, and ``/v0/beads/issues:claimNext`` from a
    fixed row set. ``seen_params`` records the last query string per path, so a test can assert a
    caller's narrowing flags actually reached the request."""

    rows: list[dict] = field(default_factory=list)
    children_rows: list[dict] = field(default_factory=list)
    claim_calls: list[str] = field(default_factory=list)
    claimed_id: str | None = None
    capabilities: frozenset[str] = field(default_factory=lambda: QUEUE_CAPABILITIES)
    seen_params: dict[str, httpx.QueryParams] = field(default_factory=dict)

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
            rows = self.rows[:limit] if limit else self.rows
            return httpx.Response(
                200, json={"items": rows, "has_more": bool(limit) and len(self.rows) > limit}
            )
        if path == "/v0/beads/issues" and request.method == "GET":
            self.seen_params[path] = request.url.params
            return httpx.Response(200, json={"items": self.children_rows, "has_more": False})
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


def test_list_ready_forwards_every_narrowing_flag_to_the_request():
    """Every `bh work ready` narrowing flag this bead routes lands on the wire exactly once,
    under the name `GET /v0/beads/ready` (and `bd ready`) both use."""
    fixture = FakeQueue(rows=[_row("bh-1")])
    commands = QueueCommands()
    with _session(fixture) as session:
        commands.list_ready(
            session,
            limit=0,
            assignee="dev/alice",
            unassigned=True,
            type_="task",
            exclude_type=["gate", "event"],
            label=["size:l"],
            label_any=["model:opus", "model:sonnet"],
            exclude_label=["blocked"],
            priority=2,
            parent="ep-1",
            has_metadata_key="spec_id",
            metadata_field=["team=core"],
        )
    seen = fixture.seen_params["/v0/beads/ready"]
    assert seen["limit"] == "0"
    assert seen["assignee"] == "dev/alice"
    assert seen["unassigned"] == "true"
    assert seen["type"] == "task"
    assert seen.get_list("exclude_type") == ["gate", "event"]
    assert seen.get_list("label") == ["size:l"]
    assert seen.get_list("label_any") == ["model:opus", "model:sonnet"]
    assert seen.get_list("exclude_label") == ["blocked"]
    assert seen["priority"] == "2"
    assert seen["parent"] == "ep-1"
    assert seen["has_metadata_key"] == "spec_id"
    assert seen.get_list("metadata_field") == ["team=core"]


def test_list_ready_omits_unset_narrowing_flags_entirely():
    """A caller that passes nothing beyond `limit` sends nothing beyond the generated client's
    OWN unconditional defaults (`sort=priority`, the three off-by-default booleans) — none of
    `bh work ready`'s optional narrowing flags (assignee, label, parent, ...) ride along unasked,
    an explicit empty/false value never confused with "not asked for"."""
    fixture = FakeQueue(rows=[_row("bh-1")])
    commands = QueueCommands()
    with _session(fixture) as session:
        commands.list_ready(session, limit=50)
    seen = fixture.seen_params["/v0/beads/ready"]
    assert set(seen.keys()) == {"limit", "sort", "brief", "include_ephemeral", "include_deferred"}
    assert seen["sort"] == "priority"


# ---- transport fixture: list_children (bh-mu5yb.1) -------------------------------------------


def test_list_children_narrows_a_recursive_fetch_to_the_direct_edge():
    fixture = FakeQueue(
        children_rows=[
            _row("ep-1.2", parent="ep-1", priority=1),
            _row("ep-1.1.1", parent="ep-1.1"),  # grandchild the recursive fetch also returns
            _row("ep-1.1", parent="ep-1", issue_type="epic", priority=0),
        ]
    )
    commands = QueueCommands()
    with _session(fixture) as session:
        children = commands.list_children(session, "ep-1")
    assert [row["id"] for row in children] == ["ep-1.2", "ep-1.1"]  # grandchild excluded
    seen = fixture.seen_params["/v0/beads/issues"]
    assert seen["parent"] == "ep-1"
    assert seen["sort"] == "priority"
    assert seen["limit"] == "0"


def test_list_children_empty_parent_returns_every_row_unfiltered():
    fixture = FakeQueue(children_rows=[_row("bh-1"), _row("bh-2")])
    commands = QueueCommands()
    with _session(fixture) as session:
        children = commands.list_children(session, "")
    assert [row["id"] for row in children] == ["bh-1", "bh-2"]


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


# ---- epic-scoped guarded claim (bh-7ip8t) ----------------------------------------------------


def test_molecule_scope_keeps_ready_order_admits_the_epic_and_only_removes_rows():
    ready = [_row("out-1"), _row("ep-1.2"), _row("ep-1"), _row("ep-1.1")]
    members = [_row("ep-1.1"), _row("ep-1.2"), _row("ep-1.3", status="closed")]
    scoped = molecule_scope(ready, members, "ep-1")
    assert [row["id"] for row in scoped] == ["ep-1.2", "ep-1", "ep-1.1"]


def test_decline_after_distinguishes_empty_ineligible_and_all_lost():
    assert decline_after([], []) == DECLINE_EMPTY_QUEUE
    assert decline_after([_row("bh-1")], []) == DECLINE_NONE_ELIGIBLE
    assert decline_after([_row("bh-1")], ["bh-1"]) == DECLINE_ALL_LOST
    assert DECLINE_ALL_LOST in EPIC_DECLINES and DECLINE_ALL_LOST not in DECLINES


@dataclass
class FakeEpicQueue(FakeQueue):
    """`FakeQueue` plus `POST /v0/beads/issues/{id}:claim` (a compare-and-set: ids in ``held``
    answer 409 with ``code``) and `GET /v0/beads/issues/{id}` (for the indeterminate-write
    re-read)."""

    held: dict[str, str] = field(default_factory=dict)  # issue id -> 409 problem code
    indeterminate: set[str] = field(default_factory=set)
    issue_claims: list[tuple[str, str]] = field(default_factory=list)

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith(":claim") and request.method == "POST":
            issue_id = path.removeprefix("/v0/beads/issues/").removesuffix(":claim")
            actor = json.loads(request.content or b"{}")["actor"]
            self.issue_claims.append((issue_id, actor))
            if issue_id in self.indeterminate:
                raise httpx.ReadTimeout("lost response", request=request)
            if issue_id in self.held:
                return httpx.Response(
                    409,
                    json={
                        "status": 409,
                        "title": "Conflict",
                        "code": self.held[issue_id],
                        "request_id": "req-1",
                    },
                    headers={"content-type": "application/problem+json"},
                )
            row = next(r for r in self.rows if r["id"] == issue_id)
            issue = dict(row, assignee=actor, status="in_progress")
            return httpx.Response(200, json={"issue": issue, "already_claimed": False})
        if path.startswith("/v0/beads/issues/") and request.method == "GET":
            issue_id = path.removeprefix("/v0/beads/issues/")
            row = next(r for r in self.rows if r["id"] == issue_id)
            claimer = next((a for i, a in self.issue_claims if i == issue_id), "")
            detail = dict(row, assignee=claimer, status="in_progress", revision="rev-1")
            return httpx.Response(200, json=detail)
        return super().handle(request)


def _epic_queue(**kw) -> FakeEpicQueue:
    return FakeEpicQueue(
        rows=[_row("out-1"), _row("ep-1.1"), _row("ep-1.2"), _row("ep-1.1.1")],
        children_rows=[
            _row("ep-1.1", parent="ep-1"),
            _row("ep-1.2", parent="ep-1"),
            _row("ep-1.3", parent="ep-1", status="closed"),
            _row("ep-1.4", parent="ep-1", issue_type="gate"),
            _row("ep-1.1.1", parent="ep-1.1"),  # a grandchild: NOT a member (bh-sh6yt)
        ],
        **kw,
    )


def test_molecule_members_asks_for_closed_and_infra_rows_and_narrows_to_the_edge():
    fixture = _epic_queue()
    with _session(fixture) as session:
        members = QueueCommands().molecule_members(session, "ep-1")
    assert [row["id"] for row in members] == ["ep-1.1", "ep-1.2", "ep-1.3", "ep-1.4"]
    seen = fixture.seen_params["/v0/beads/issues"]
    assert (seen["parent"], seen["all"], seen["include_infra"], seen["limit"]) == (
        "ep-1",
        "true",
        "true",
        "0",
    )


def test_epic_candidates_read_the_unbounded_ready_front_narrowed_to_one_level():
    fixture = _epic_queue()
    with _session(fixture) as session:
        rows = QueueCommands().epic_candidates(session, "ep-1")
    assert [row["id"] for row in rows] == ["ep-1.1", "ep-1.2"]  # no outsider, no grandchild
    assert fixture.seen_params["/v0/beads/ready"]["limit"] == "0"


def test_claim_next_in_epic_takes_the_first_in_scope_candidate():
    fixture = _epic_queue()
    with _session(fixture) as session:
        outcome = QueueCommands().claim_next_in_epic(session, "ep-1", "dev/alice")
    assert isinstance(outcome, EpicClaimOutcome)
    assert (outcome.claimed, outcome.claim_actor, outcome.tried) == (
        "ep-1.1",
        "dev/alice",
        ("ep-1.1",),
    )
    assert fixture.issue_claims == [("ep-1.1", "dev/alice")]
    assert fixture.claim_calls == [], "never the atomic claim-next op"


@pytest.mark.parametrize("code", ["already_claimed", "not_claimable", "not_found"])
def test_claim_next_in_epic_moves_past_a_definite_lost_claim(code):
    fixture = _epic_queue(held={"ep-1.1": code})
    with _session(fixture) as session:
        outcome = QueueCommands().claim_next_in_epic(session, "ep-1", "dev/alice")
    assert (outcome.claimed, outcome.tried) == ("ep-1.2", ("ep-1.1", "ep-1.2"))


def test_claim_next_in_epic_reports_all_lost_when_every_candidate_is_held():
    fixture = _epic_queue(held={"ep-1.1": "already_claimed", "ep-1.2": "already_claimed"})
    with _session(fixture) as session:
        outcome = QueueCommands().claim_next_in_epic(session, "ep-1", "dev/alice")
    assert (outcome.claimed, outcome.reason) == ("", DECLINE_ALL_LOST)
    assert [row["id"] for row in outcome.rows] == ["ep-1.1", "ep-1.2"]


def test_claim_next_in_epic_refuses_a_seat_mismatch_before_any_write():
    fixture = _epic_queue()
    seat = {"ep-1.1": None, "ep-1.2": "dev/alice"}
    with _session(fixture) as session:
        outcome = QueueCommands().claim_next_in_epic(
            session, "ep-1", "dev/alice", seat_actor=lambda row: seat[row["id"]]
        )
    assert (outcome.claimed, outcome.refused, outcome.tried) == (
        "ep-1.2",
        ("ep-1.1",),
        ("ep-1.2",),
    )
    assert fixture.issue_claims == [("ep-1.2", "dev/alice")]


def test_claim_next_in_epic_claims_under_the_resolved_seat_actor():
    fixture = _epic_queue()
    with _session(fixture) as session:
        outcome = QueueCommands().claim_next_in_epic(
            session, "ep-1", "alice", seat_actor=lambda _row: "dev/alice"
        )
    assert (outcome.claimed, outcome.claim_actor) == ("ep-1.1", "dev/alice")
    assert fixture.issue_claims == [("ep-1.1", "dev/alice")]


def test_claim_guarded_propagates_an_unexpected_refusal():
    fixture = _epic_queue(held={"ep-1.1": "invalid_argument"})
    with _session(fixture) as session, pytest.raises(ServiceProblem):
        QueueCommands().claim_guarded(session, "ep-1.1", "dev/alice")


def test_claim_guarded_reconciles_an_indeterminate_write_by_reading():
    fixture = _epic_queue(indeterminate={"ep-1.1"})
    with _session(fixture) as session:
        assert QueueCommands().claim_guarded(session, "ep-1.1", "dev/alice") is True
    assert fixture.issue_claims == [("ep-1.1", "dev/alice")], "never replayed"


def test_claim_next_in_epic_refuses_before_any_request_without_the_claim_capability():
    fixture = _epic_queue(capabilities=QUEUE_CAPABILITIES - {"issues.claim"})
    session = BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext(PROJECT, "bh", required_capabilities=QUEUE_CAPABILITIES - {"issues.claim"}),
        transport=httpx.MockTransport(fixture.handle),
    )
    with session, pytest.raises(CapabilityMissing):
        QueueCommands().claim_next_in_epic(session, "ep-1", "dev/alice")
    assert fixture.issue_claims == []
