"""Bounded generic queue and exact work-item HTTP contract coverage."""

from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jsonschema
import pytest
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from beadhive import (
    daemon_contract,
    host_daemon,
    operator_api,
    operator_feed,
    operator_sources,
    operator_work_items,
    state_stream,
)
from beadhive.agent_run_summary import Freshness
from beadhive.public_readers import AgentRunSnapshot, Coverage

HIVE = "github/beadhive/beadhive"
HIVE_PATH = "github%2Fbeadhive%2Fbeadhive"
NOW = datetime(2026, 8, 27, 12, tzinfo=UTC).isoformat().replace("+00:00", "Z")


def _validate(schema_name: str, payload: dict) -> None:
    document = operator_api.openapi_document()
    contract_uri = "urn:beadhive:host-openapi-v1"
    registry = Registry().with_resource(
        contract_uri,
        Resource.from_contents(document, default_specification=DRAFT202012),
    )
    jsonschema.Draft202012Validator(
        {"$ref": f"{contract_uri}#/components/schemas/{schema_name}"},
        registry=registry,
    ).validate(payload)


def _issue(
    issue_id: str,
    *,
    status: str = "open",
    priority: str = "P2",
    labels: tuple[str, ...] = (),
    assignee: str | None = None,
    parent: str | None = None,
    closed_at: str | None = None,
) -> state_stream.StreamIssue:
    return state_stream.StreamIssue(
        id=issue_id,
        hive=HIVE,
        issue_type="feature" if issue_id == "bh-ready-1" else "task",
        status=status,
        priority=priority,
        title=f"Title {issue_id}",
        updated_at=NOW,
        labels=labels,
        assignee=assignee,
        parent_id=parent,
        description=f"Description {issue_id}",
        design=f"Design {issue_id}",
        acceptance_criteria=f"Acceptance {issue_id}",
        notes=f"Notes {issue_id}",
        mol_type="workflow",
        owner="owner@example.test",
        created_by="creator@example.test",
        created_at="2026-08-20T12:00:00Z",
        closed_at=closed_at,
        due_at="2026-09-01T12:00:00Z",
        lease_expires_at="2026-08-27T13:00:00Z" if assignee else None,
    )


def _dependency(issue_id: str, depends_on_id: str) -> state_stream.WorkDependency:
    return state_stream.WorkDependency(
        id=state_stream.projection_id("work-dependency", (HIVE, issue_id, depends_on_id, "blocks")),
        hive=HIVE,
        issue_id=issue_id,
        depends_on_id=depends_on_id,
        type="blocks",
        created_at=NOW,
        created_by="planner@example.test",
    )


class _CountingTuple(tuple):
    """Tuple-compatible source collection that records complete projection scans."""

    def __new__(cls, values):
        instance = super().__new__(cls, values)
        instance.iterations = 0
        return instance

    def __iter__(self):
        self.iterations += 1
        return super().__iter__()


class Provider:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.revision = "beads-1"

    def refresh(self, _request: state_stream.StreamRequest) -> state_stream.ProviderSnapshot:
        if self.unavailable:
            raise RuntimeError("backend unavailable")
        issues = (
            _issue("bh-ready-1", priority="P0", labels=("api", "chosen"), parent="bh-epic"),
            _issue("bh-ready-2", priority="P1", labels=("api",)),
            _issue("bh-ready-3", priority="P2"),
            _issue("bh-active", status="in_progress", assignee="dev/one"),
            _issue("bh-blocked"),
            _issue("bh-prerequisite", status="in_progress"),
            _issue("bh-closed", status="closed", closed_at="2026-08-27T11:00:00Z"),
        )
        return state_stream.ProviderSnapshot(
            scope="hive",
            revision=self.revision,
            as_of=NOW,
            issues=issues,
            work_dependencies=(_dependency("bh-blocked", "bh-prerequisite"),),
        )


def _runtime(host: str, source: str) -> AgentRunSnapshot:
    return AgentRunSnapshot(
        host_id=host,
        source_id=source,
        revision="runtime-1",
        summaries=(),
        coverage=Coverage.COMPLETE,
        coverage_reason=None,
        freshness=Freshness(state="fresh", as_of=NOW),
    )


def _app(tmp_path: Path, provider: Provider | None = None):
    cfg = {
        "managed_repos": [
            {
                "provider": "github",
                "org": "beadhive",
                "repo": "beadhive",
                "prefix": "bh",
                "kind": "org-native",
            }
        ]
    }
    sources = operator_sources.OperatorSources(
        cfg=cfg,
        host_id="host-1",
        provider=provider or Provider(),
        summary_reader=lambda _path, host, source: _runtime(host, source),
        journal_base=tmp_path,
        dispatch_sink_for_entry=lambda _cfg, _entry: tmp_path / "dispatch.jsonl",
    )
    runtime = host_daemon.DaemonRuntime()
    api = operator_api.OperatorAPI(
        sources=sources,
        feed=operator_feed.OperatorFeed(sources, now_millis=lambda: 1000),
        host_id="host-1",
        instance_id="instance-1",
        ready=lambda: runtime.ready,
    )
    return host_daemon.build_application(runtime=runtime, routes=api.routes())


def _exercise(tmp_path: Path, action, *, provider: Provider | None = None):
    app = _app(tmp_path, provider)

    async def run():
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 5000))
            async with httpx.AsyncClient(
                transport=transport, base_url="http://127.0.0.1:8420"
            ) as client:
                return await action(client)

    return asyncio.run(run())


def test_ready_active_blocked_and_recent_are_distinct_bounded_queues(tmp_path: Path) -> None:
    async def action(client):
        responses = {}
        for queue in ("ready", "active", "blocked", "recent"):
            responses[queue] = await client.get(
                f"/api/v1/hives/{HIVE_PATH}/work-items", params={"queue": queue}
            )
        invalid = await client.get(
            f"/api/v1/hives/{HIVE_PATH}/work-items",
            params={"queue": "ready", "limit": 201},
        )
        return responses, invalid

    responses, invalid = _exercise(tmp_path, action)
    assert {
        queue: [item["id"] for item in response.json()["items"]]
        for queue, response in responses.items()
    } == {
        "ready": ["bh-ready-1", "bh-ready-2", "bh-ready-3"],
        "active": ["bh-active", "bh-prerequisite"],
        "blocked": ["bh-blocked"],
        "recent": ["bh-closed"],
    }
    assert all(response.json()["limit"] == 50 for response in responses.values())
    for response in responses.values():
        _validate("WorkItemQueue", response.json())
    assert (invalid.status_code, invalid.json()["error"]["code"]) == (
        400,
        "invalid_work_items_limit",
    )


def test_ready_queue_can_use_the_same_configured_release_order_as_bh_work_ready() -> None:
    provider = Provider()
    beads = provider.refresh(state_stream.StreamRequest("hive", hive=HIVE))
    issues = tuple(
        item
        if item.id == "bh-ready-1"
        else state_stream.StreamIssue(
            **{
                **item.__dict__,
                "labels": (
                    ("release:breaking",)
                    if item.id == "bh-ready-2"
                    else ("release:fix",)
                    if item.id == "bh-ready-3"
                    else item.labels
                ),
            }
        )
        for item in beads.issues
    )
    beads = state_stream.ProviderSnapshot(
        scope=beads.scope,
        revision=beads.revision,
        as_of=beads.as_of,
        issues=issues,
        work_dependencies=beads.work_dependencies,
    )
    payload = operator_work_items.queue_payload(
        hive_id=HIVE,
        beads=beads,
        runtime=_runtime("host-1", "runtime"),
        query=operator_work_items.WorkItemQuery(queue="ready"),
        ready_policy=("stable-versioning", 3),
    )

    assert [item["id"] for item in payload["items"]] == [
        "bh-ready-3",
        "bh-ready-2",
        "bh-ready-1",
    ]


def test_queue_projection_indexes_sparse_dependencies_and_gates_once() -> None:
    issues = tuple(_issue(f"bh-scale-{index:03}") for index in range(240))
    dependencies = tuple(_dependency(f"bh-scale-{index:03}", "bh-scale-239") for index in range(80))
    gates = tuple(
        state_stream.GateRequest(
            id=state_stream.projection_id("gate-request", (HIVE, f"approval-{index:03}")),
            hive=HIVE,
            gate_id=f"approval-{index:03}",
            blocks=(f"bh-scale-{index + 80:03}",),
            gate_type=None,
            gate_kind="approval",
            status="open",
            reason="Awaiting approval",
            opened_at=NOW,
            resolved_at=None,
        )
        for index in range(40)
    )
    beads = state_stream.ProviderSnapshot(
        scope="hive",
        revision="beads-scale",
        as_of=NOW,
        issues=issues,
        work_dependencies=dependencies,
        gate_requests=gates,
    )
    counted_dependencies = _CountingTuple(beads.work_dependencies)
    counted_gates = _CountingTuple(beads.gate_requests)
    object.__setattr__(beads, "work_dependencies", counted_dependencies)
    object.__setattr__(beads, "gate_requests", counted_gates)

    payload = operator_work_items.queue_payload(
        hive_id=HIVE,
        beads=beads,
        runtime=_runtime("host-1", "runtime"),
        query=operator_work_items.WorkItemQuery(queue="blocked", limit=200),
    )

    assert payload["returned"] == 120
    assert counted_dependencies.iterations == 1
    assert counted_gates.iterations == 1


def test_pagination_filters_and_cursor_scope_and_revision_are_stable(tmp_path: Path) -> None:
    provider = Provider()

    async def action(client):
        first = await client.get(
            f"/api/v1/hives/{HIVE_PATH}/work-items",
            params={"queue": "ready", "limit": 1},
        )
        unchanged = await client.get(
            f"/api/v1/hives/{HIVE_PATH}/work-items",
            params={"queue": "ready", "limit": 1},
            headers={"If-None-Match": first.headers["etag"]},
        )
        cursor = first.json()["nextCursor"]
        second = await client.get(
            f"/api/v1/hives/{HIVE_PATH}/work-items",
            params={"queue": "ready", "limit": 1, "cursor": cursor},
        )
        filtered = await client.get(
            f"/api/v1/hives/{HIVE_PATH}/work-items",
            params=[("queue", "ready"), ("label", "chosen"), ("priority", "0")],
        )
        wrong_scope = await client.get(
            f"/api/v1/hives/{HIVE_PATH}/work-items",
            params={"queue": "blocked", "limit": 1, "cursor": cursor},
        )
        provider.revision = "beads-2"
        stale = await client.get(
            f"/api/v1/hives/{HIVE_PATH}/work-items",
            params={"queue": "ready", "limit": 1, "cursor": cursor},
        )
        return first, unchanged, second, filtered, wrong_scope, stale

    first, unchanged, second, filtered, wrong_scope, stale = _exercise(
        tmp_path, action, provider=provider
    )
    assert first.json()["truncated"] is True
    assert [item["id"] for item in first.json()["items"]] == ["bh-ready-1"]
    assert [item["id"] for item in second.json()["items"]] == ["bh-ready-2"]
    assert [item["id"] for item in filtered.json()["items"]] == ["bh-ready-1"]
    assert unchanged.status_code == 304
    assert unchanged.content == b""
    assert unchanged.headers["etag"] == first.headers["etag"]
    assert unchanged.headers["cache-control"] == "no-cache"
    assert wrong_scope.json()["error"]["code"] == "work_items_cursor_scope_mismatch"
    assert wrong_scope.status_code == 409
    assert stale.json()["error"]["code"] == "work_items_cursor_revision_mismatch"
    assert stale.status_code == 409


def test_exact_detail_is_complete_and_supports_conditional_get(tmp_path: Path) -> None:
    async def action(client):
        response = await client.get(f"/api/v1/hives/{HIVE_PATH}/work-items/bh-ready-1")
        blocked = await client.get(f"/api/v1/hives/{HIVE_PATH}/work-items/bh-blocked")
        cached = await client.get(
            f"/api/v1/hives/{HIVE_PATH}/work-items/bh-ready-1",
            headers={"If-None-Match": response.headers["etag"]},
        )
        missing = await client.get(f"/api/v1/hives/{HIVE_PATH}/work-items/bh-missing")
        return response, blocked, cached, missing

    response, blocked, cached, missing = _exercise(tmp_path, action)
    item = response.json()["item"]
    _validate("WorkItemDetail", response.json())
    assert response.status_code == 200
    assert response.headers["etag"].startswith('"sha256:')
    assert response.headers["cache-control"] == "no-cache"
    assert item["ref"] == {"hiveId": HIVE, "kind": "work-item", "id": "bh-ready-1"}
    assert item["description"] == "Description bh-ready-1"
    assert item["design"] == "Design bh-ready-1"
    assert item["acceptanceCriteria"] == "Acceptance bh-ready-1"
    assert item["notes"] == "Notes bh-ready-1"
    assert item["moleculeType"] == "workflow"
    assert item["labels"] == ["api", "chosen"]
    ready_actions = {action["id"]: action for action in item["advertisedActions"]}
    assert ready_actions["work-item.launch"]["availability"] == "allowed"
    assert ready_actions["work-item.launch"]["preconditions"] == {
        "sourceRevision": response.json()["revision"],
        "mustMatch": True,
    }
    assert ready_actions["work-item.launch"]["target"] == item["ref"]
    blocked_actions = {
        action["id"]: action for action in blocked.json()["item"]["advertisedActions"]
    }
    assert blocked_actions["work-item.launch"]["availability"] == "forbidden"
    assert blocked_actions["work-item.launch"]["reasonCode"] == "work_item_blocked"
    assert blocked.json()["item"]["dependencies"] == [
        {
            "id": "bh-prerequisite",
            "title": "Title bh-prerequisite",
            "type": "blocks",
            "state": "in_progress",
            "direction": "prerequisite",
        }
    ]
    assert cached.status_code == 304
    assert cached.content == b""
    assert cached.headers["cache-control"] == "no-cache"
    assert (missing.status_code, missing.json()["error"]["code"]) == (
        404,
        "work_item_not_found",
    )


def test_missing_and_unavailable_hives_are_not_empty_successes(tmp_path: Path) -> None:
    async def missing_action(client):
        return await client.get(
            "/api/v1/hives/github%2Fbeadhive%2Fmissing/work-items", params={"queue": "ready"}
        )

    missing = _exercise(tmp_path, missing_action)
    assert (missing.status_code, missing.json()["error"]["code"]) == (404, "hive_not_found")

    async def unavailable_action(client):
        return await client.get(f"/api/v1/hives/{HIVE_PATH}/work-items", params={"queue": "ready"})

    unavailable = _exercise(tmp_path, unavailable_action, provider=Provider(unavailable=True))
    assert (unavailable.status_code, unavailable.json()["error"]["code"]) == (
        503,
        "snapshot_source_unavailable",
    )
    assert unavailable.headers["retry-after"] == "1"


def test_byte_budget_materializes_each_bounded_row_once_and_preserves_cursor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    labels = tuple(f"label-{index:02}-" + "x" * 240 for index in range(12))
    issues = tuple(
        _issue(f"bh-wide-{index:03}", priority="P0", labels=labels) for index in range(250)
    )
    issues = tuple(
        state_stream.StreamIssue(**{**issue.__dict__, "title": "x" * 4_096}) for issue in issues
    )
    beads = state_stream.ProviderSnapshot(
        scope="hive", revision="beads-wide", as_of=NOW, issues=issues
    )
    runtime = _runtime("host-1", "runtime")
    query = operator_work_items.WorkItemQuery(queue="ready", limit=200)
    original = operator_work_items._row
    projected = 0

    def counted(*args, **kwargs):
        nonlocal projected
        projected += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(operator_work_items, "_row", counted)

    first = operator_work_items.remote_queue_payload(
        hive_id=HIVE, beads=beads, runtime=runtime, query=query
    )
    first_projected = projected
    second = operator_work_items.remote_queue_payload(
        hive_id=HIVE,
        beads=beads,
        runtime=runtime,
        query=operator_work_items.WorkItemQuery(
            queue="ready", limit=200, cursor=first["nextCursor"]
        ),
    )

    assert 0 < first["returned"] < 200
    assert len(operator_work_items.encoded_bytes(first)) <= operator_work_items.QUEUE_MAX_BYTES
    assert first["items"][-1]["id"] != second["items"][0]["id"]
    assert second["items"][0]["id"] == f"bh-wide-{first['returned']:03}"
    assert first_projected <= query.limit


def test_exact_detail_rejects_dependency_overflow_after_only_max_plus_one_projections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dependency_count = operator_work_items.DETAIL_MAX_DEPENDENCIES + 500
    beads = state_stream.ProviderSnapshot(
        scope="hive",
        revision="beads-hostile",
        as_of=NOW,
        issues=(_issue("bh-target"),),
        work_dependencies=tuple(
            _dependency("bh-target", f"bh-prerequisite-{index:04}")
            for index in range(dependency_count)
        ),
    )
    original = operator_work_items._remote_dependency_detail
    projected = 0

    def counted(*args, **kwargs):
        nonlocal projected
        projected += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(operator_work_items, "_remote_dependency_detail", counted)

    with pytest.raises(operator_sources.OperatorSourceError) as raised:
        operator_work_items.remote_detail_payload(
            hive_id=HIVE,
            bead_id="bh-target",
            beads=beads,
            runtime=_runtime("host-1", "runtime"),
        )

    assert raised.value.code == "work_item_detail_too_large"
    assert raised.value.status_code == 413
    assert projected == operator_work_items.DETAIL_MAX_DEPENDENCIES + 1


def test_exact_detail_rejects_oversized_rich_text_with_stable_413() -> None:
    issue = _issue("bh-huge")
    issue = state_stream.StreamIssue(
        **{
            **issue.__dict__,
            "description": "x" * (operator_work_items.DETAIL_TEXT_MAX_BYTES + 1),
        }
    )
    beads = state_stream.ProviderSnapshot(
        scope="hive", revision="beads-huge", as_of=NOW, issues=(issue,)
    )

    with pytest.raises(operator_sources.OperatorSourceError) as raised:
        operator_work_items.remote_detail_payload(
            hive_id=HIVE,
            bead_id=issue.id,
            beads=beads,
            runtime=_runtime("host-1", "runtime"),
        )

    assert raised.value.code == "work_item_detail_too_large"
    assert raised.value.status_code == 413


def test_exact_detail_contract_rejects_nested_action_command_or_path_fields() -> None:
    issue = _issue("bh-safe-action")
    payload = operator_work_items.remote_detail_payload(
        hive_id=HIVE,
        bead_id=issue.id,
        beads=state_stream.ProviderSnapshot(
            scope="hive", revision="beads-action", as_of=NOW, issues=(issue,)
        ),
        runtime=_runtime("host-1", "runtime"),
    )
    launch = payload["item"]["advertisedActions"][2]
    launch["input"]["schema"]["properties"]["command"] = {"type": "string"}

    with pytest.raises(ValueError, match="fixed contract"):
        daemon_contract.RemoteWorkItemDetail.model_validate(payload)


def test_legacy_detail_preserves_unbounded_dependency_type_contract() -> None:
    issue = _issue("bh-legacy")
    dependency_type = "x" * 4_097
    dependency = state_stream.WorkDependency(
        id=state_stream.projection_id(
            "work-dependency", (HIVE, issue.id, "bh-other", dependency_type)
        ),
        hive=HIVE,
        issue_id=issue.id,
        depends_on_id="bh-other",
        type=dependency_type,
        created_at=NOW,
        created_by="planner@example.test",
    )
    payload = operator_work_items.detail_payload(
        hive_id=HIVE,
        bead_id=issue.id,
        beads=state_stream.ProviderSnapshot(
            scope="hive",
            revision="beads-legacy",
            as_of=NOW,
            issues=(issue,),
            work_dependencies=(dependency,),
        ),
        runtime=_runtime("host-1", "runtime"),
    )

    assert payload["item"]["dependencies"][0]["type"] == dependency_type
    daemon_contract.WorkItemDetail.model_validate(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("limit", "1"),
        lambda payload: payload.__setitem__("returned", 1.0),
        lambda payload: payload["filters"]["priorities"].__setitem__(0, "urgent"),
        lambda payload: payload["items"][0].__setitem__("priority", "1"),
        lambda payload: payload["items"][0].__setitem__("priority", True),
        lambda payload: payload["warnings"].append("x" * 4_097),
        lambda payload: payload.__setitem__("unexpected", True),
        lambda payload: payload["coverage"]["sources"]["beads"].__setitem__("detail", "x" * 4_097),
    ],
)
def test_remote_queue_contract_rejects_coercion_unbounded_text_and_extras(mutate) -> None:
    issue = _issue("bh-strict", priority="P1")
    payload = operator_work_items.remote_queue_payload(
        hive_id=HIVE,
        beads=state_stream.ProviderSnapshot(
            scope="hive", revision="beads-strict", as_of=NOW, issues=(issue,)
        ),
        runtime=_runtime("host-1", "runtime"),
        query=operator_work_items.WorkItemQuery(queue="ready", limit=1, priorities=("P1",)),
    )
    mutate(payload)

    with pytest.raises(ValueError):
        daemon_contract.RemoteWorkItemQueue.model_validate(payload)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda payload: payload.__setitem__("hiveId", "github/other/repo"),
        lambda payload: payload["item"].__setitem__("revision", "sha256:other"),
        lambda payload: payload["item"]["ref"].__setitem__("id", "bh-other"),
        lambda payload: payload["item"]["advertisedActions"][0].__setitem__(
            "availability", "forbidden"
        ),
        lambda payload: payload["item"]["advertisedActions"][0].__setitem__("reason", "x" * 4_097),
        lambda payload: payload["item"]["advertisedActions"][0].__setitem__("advertisedAt", "1"),
        lambda payload: payload["item"]["advertisedActions"][0].__setitem__("advertisedAt", 2**53),
        lambda payload: payload["item"]["advertisedActions"][2].__setitem__(
            "reasonCode", "x" * 257
        ),
        lambda payload: payload["item"]["advertisedActions"][0]["preconditions"].__setitem__(
            "mustMatch", "false"
        ),
        lambda payload: payload["item"]["advertisedActions"][0].__setitem__(
            "command", "bh work claim"
        ),
        lambda payload: payload["item"]["dependencies"].append(
            {
                "id": "bh-other",
                "title": None,
                "type": "blocks",
                "state": "open",
                "direction": "prerequisite",
                "path": "/private/repo",
            }
        ),
    ],
)
def test_remote_detail_contract_rejects_identity_action_and_nested_drift(mutate) -> None:
    issue = _issue("bh-strict-detail")
    payload = operator_work_items.remote_detail_payload(
        hive_id=HIVE,
        bead_id=issue.id,
        beads=state_stream.ProviderSnapshot(
            scope="hive", revision="beads-detail", as_of=NOW, issues=(issue,)
        ),
        runtime=_runtime("host-1", "runtime"),
    )
    candidate = copy.deepcopy(payload)
    mutate(candidate)

    with pytest.raises(ValueError):
        daemon_contract.RemoteWorkItemDetail.model_validate(candidate)
