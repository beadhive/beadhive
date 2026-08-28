"""Bounded generic queue and exact work-item HTTP contract coverage."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import httpx
import jsonschema
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from beadhive import (
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


def test_pagination_filters_and_cursor_scope_and_revision_are_stable(tmp_path: Path) -> None:
    provider = Provider()

    async def action(client):
        first = await client.get(
            f"/api/v1/hives/{HIVE_PATH}/work-items",
            params={"queue": "ready", "limit": 1},
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
        return first, second, filtered, wrong_scope, stale

    first, second, filtered, wrong_scope, stale = _exercise(tmp_path, action, provider=provider)
    assert first.json()["truncated"] is True
    assert [item["id"] for item in first.json()["items"]] == ["bh-ready-1"]
    assert [item["id"] for item in second.json()["items"]] == ["bh-ready-2"]
    assert [item["id"] for item in filtered.json()["items"]] == ["bh-ready-1"]
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
    assert item["ref"] == {"hiveId": HIVE, "kind": "work-item", "id": "bh-ready-1"}
    assert item["description"] == "Description bh-ready-1"
    assert item["design"] == "Design bh-ready-1"
    assert item["acceptanceCriteria"] == "Acceptance bh-ready-1"
    assert item["notes"] == "Notes bh-ready-1"
    assert item["moleculeType"] == "workflow"
    assert item["labels"] == ["api", "chosen"]
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
