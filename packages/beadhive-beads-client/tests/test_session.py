"""HTTP seam checks exercise policy around generated models, without emulating Beads."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from beadhive_beads_client import (
    BeadsSession,
    CliCompatibilityRequired,
    ExpectedContext,
    IncompatibleService,
    IndeterminateWrite,
    LocalEndpoint,
    RemoteEndpoint,
    ServiceProblem,
)
from beads_v1_3.models import (
    ApplyBatchRequest,
    ApplyCreateItem,
    ApplyDepAddItem,
    ApplyItem,
    ApplyItemKind,
    CreateIssueRequest,
    IssuesPage,
    Ref,
)

CAPABILITIES = [
    "project.enforce",
    "issues.get",
    "issues.list",
    "ready.list",
    "issues.create",
]


def context(*, project_id: str = "expected") -> dict[str, object]:
    return {
        "api_version": "v0",
        "bd_version": "1.3.0",
        "schema_version": 1,
        "backend": "dolt",
        "dolt_mode": "embedded",
        "database": "scratch",
        "project_id": project_id,
        "capabilities": CAPABILITIES,
    }


def test_negotiation_stamps_project_and_returns_generated_page() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v0/beads/context":
            return httpx.Response(200, json=context())
        assert request.headers["Bd-Project-Id"] == "expected"
        if request.url.path == "/v0/beads/ready":
            return httpx.Response(200, json={"items": [], "has_more": False})
        return httpx.Response(200, json={"items": [], "has_more": False})

    with BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext("expected", "scratch"),
        transport=httpx.MockTransport(handler),
    ) as session:
        page = session.list_issues(limit=2)
        assert isinstance(page, IssuesPage)
        assert page.items == []
    assert seen[-1].url.params["limit"] == "2"


def test_wrong_context_fails_closed_before_work_read() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        return httpx.Response(200, json=context(project_id="other"))

    session = BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext("expected", "scratch"),
        transport=httpx.MockTransport(handler),
    )
    with pytest.raises(IncompatibleService, match="identity mismatch"):
        session.open()


def test_typed_problem_and_ambiguous_write_are_distinct() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v0/beads/context":
            return httpx.Response(200, json=context())
        if request.url.path == "/v0/beads/ready":
            return httpx.Response(200, json={"items": [], "has_more": False})
        if request.method == "GET":
            return httpx.Response(
                404,
                json={"status": 404, "title": "Not Found", "code": "not_found", "request_id": "r1"},
            )
        raise httpx.ReadTimeout("reply lost")

    with BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext("expected", "scratch"),
        transport=httpx.MockTransport(handler),
    ) as session:
        with pytest.raises(ServiceProblem) as failure:
            session.get_issue("missing")
        assert failure.value.problem.code == "not_found"
        with pytest.raises(IndeterminateWrite):
            session.create_issue(CreateIssueRequest(actor="agent", title="one"))


def test_cli_compatibility_is_explicit() -> None:
    with pytest.raises(CliCompatibilityRequired, match="work.gate.resolve"):
        BeadsSession.require_cli("work.gate.resolve")
    with pytest.raises(CliCompatibilityRequired, match="admin.sync"):
        BeadsSession.require_cli("admin.sync")
    with pytest.raises(ValueError, match="no approved CLI"):
        BeadsSession.require_cli("issues.delete")
    with pytest.raises(ValueError, match="no approved CLI"):
        BeadsSession.require_cli("work.issue.get")


def test_local_transport_uses_same_contract_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_spawn(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("transport seam must not start bd")

    monkeypatch.setattr("beadhive_beads_client.session.subprocess.Popen", forbidden_spawn)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v0/beads/context":
            return httpx.Response(200, json=context())
        assert request.headers["Bd-Project-Id"] == "expected"
        return httpx.Response(200, json={"items": [], "has_more": False})

    with BeadsSession(
        LocalEndpoint(Path("/scratch"), port=8123),
        ExpectedContext("expected", "scratch"),
        transport=httpx.MockTransport(handler),
    ) as session:
        assert session.list_ready().items == []


def test_list_ready_forwards_narrowing_kwargs_as_query_params() -> None:
    """bh-mu5yb.1: `BeadsSession.list_ready` widened beyond `limit` — every added keyword must
    reach the wire under `GET /v0/beads/ready`'s own parameter names."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v0/beads/context":
            return httpx.Response(200, json=context())
        return httpx.Response(200, json={"items": [], "has_more": False})

    with BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext("expected", "scratch"),
        transport=httpx.MockTransport(handler),
    ) as session:
        session.list_ready(
            limit=0,
            assignee="dev/alice",
            unassigned=True,
            type_="task",
            exclude_type=["gate"],
            label=["size:l"],
            label_any=["model:opus"],
            exclude_label=["blocked"],
            priority=1,
            parent="ep-1",
            has_metadata_key="spec_id",
            metadata_field=["team=core"],
        )
    params = seen[-1].url.params
    assert params["limit"] == "0"
    assert params["assignee"] == "dev/alice"
    assert params["unassigned"] == "true"
    assert params["type"] == "task"
    assert params.get_list("exclude_type") == ["gate"]
    assert params.get_list("label") == ["size:l"]
    assert params.get_list("label_any") == ["model:opus"]
    assert params.get_list("exclude_label") == ["blocked"]
    assert params["priority"] == "1"
    assert params["parent"] == "ep-1"
    assert params["has_metadata_key"] == "spec_id"
    assert params.get_list("metadata_field") == ["team=core"]


def test_list_issues_forwards_parent_and_sort() -> None:
    """bh-mu5yb.1: `BeadsSession.list_issues` widened with `parent` (recursive-descendant) and
    `sort` (the closed `created`/`priority` vocabulary) for `bh work schedule`'s children fetch."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v0/beads/context":
            return httpx.Response(200, json=context())
        return httpx.Response(200, json={"items": [], "has_more": False})

    with BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext("expected", "scratch"),
        transport=httpx.MockTransport(handler),
    ) as session:
        session.list_issues(limit=0, parent="ep-1", sort="priority")
    params = seen[-1].url.params
    assert params["limit"] == "0"
    assert params["parent"] == "ep-1"
    assert params["sort"] == "priority"


def test_list_issues_forwards_status_all_and_include_infra() -> None:
    """bh-sy36q.5: widened so a restartable molecule/swarm read can ask for the SAME full
    membership `bd list --parent <epic> --include-infra --all` returns — closed children and
    infra rows included, not just the default-exclusion view."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v0/beads/context":
            return httpx.Response(200, json=context())
        return httpx.Response(200, json={"items": [], "has_more": False})

    with BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext("expected", "scratch"),
        transport=httpx.MockTransport(handler),
    ) as session:
        session.list_issues(limit=0, parent="ep-1", all_=True, include_infra=True)
    params = seen[-1].url.params
    assert params["all"] == "true"
    assert params["include_infra"] == "true"

    del seen[:]
    with BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext("expected", "scratch"),
        transport=httpx.MockTransport(handler),
    ) as session:
        session.list_issues(limit=0)
    params = seen[-1].url.params
    assert params["all"] == "false"
    assert params["include_infra"] == "false"

    del seen[:]
    with BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext("expected", "scratch"),
        transport=httpx.MockTransport(handler),
    ) as session:
        session.list_issues(limit=0, status=["closed", "open"])
    params = seen[-1].url.params
    assert params.get_list("status") == ["closed", "open"]


def test_list_issues_rejects_an_unrecognized_sort_value() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v0/beads/context":
            return httpx.Response(200, json=context())
        return httpx.Response(200, json={"items": [], "has_more": False})

    with BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext("expected", "scratch"),
        transport=httpx.MockTransport(handler),
    ) as session:
        with pytest.raises(ValueError):
            session.list_issues(sort="not-a-real-sort")


def test_batch_apply_posts_the_ordered_plan_and_resolves_keys() -> None:
    """bh-sy36q.2: ``BeadsSession.batch_apply`` posts ``issues:batchApply`` verbatim and returns
    the generated ``ApplyBatchResponse`` (key->id map + per-item results) untouched — the seam
    :mod:`beadhive_core.planning` builds its request/response handling over."""
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v0/beads/context":
            ctx = context()
            ctx["capabilities"] = [*CAPABILITIES, "issues.batchApply"]
            return httpx.Response(200, json=ctx)
        if request.url.path == "/v0/beads/ready":
            return httpx.Response(200, json={"items": [], "has_more": False})
        assert request.url.path == "/v0/beads/issues:batchApply"
        assert request.method == "POST"
        return httpx.Response(
            200,
            json={
                "keys": {"epic": "bh-1", "issue:root": "bh-1.1"},
                "items": [
                    {"kind": "create", "issue_id": "bh-1", "changed": True, "revision": "1"},
                    {"kind": "create", "issue_id": "bh-1.1", "changed": True, "revision": "1"},
                    {
                        "kind": "dep_add",
                        "issue_id": "bh-1.1",
                        "changed": True,
                        "revision": "0",
                        "depends_on_id": "bh-1",
                    },
                ],
            },
        )

    body = ApplyBatchRequest(
        actor="dev/alice",
        items=[
            ApplyItem(
                kind=ApplyItemKind.CREATE,
                create=ApplyCreateItem(title="epic", key="epic", issue_type="epic"),
            ),
            ApplyItem(
                kind=ApplyItemKind.CREATE,
                create=ApplyCreateItem(title="root", key="issue:root", issue_type="task"),
            ),
            ApplyItem(
                kind=ApplyItemKind.DEP_ADD,
                dep_add=ApplyDepAddItem(
                    source=Ref(key="issue:root"), target=Ref(key="epic"), type_="parent-child"
                ),
            ),
        ],
    )
    with BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080"),
        ExpectedContext("expected", "scratch"),
        transport=httpx.MockTransport(handler),
    ) as session:
        response = session.batch_apply(body)
    assert dict(response.keys.to_dict()) == {"epic": "bh-1", "issue:root": "bh-1.1"}
    assert [item.issue_id for item in response.items if item.kind.value == "create"] == [
        "bh-1",
        "bh-1.1",
    ]
    posted = seen[-1]
    assert posted.headers["Content-Type"] == "application/json"


def test_writes_use_the_write_deadline_and_reads_the_request_deadline() -> None:
    """bh-t0con: a write must not inherit the (short) read/probe deadline."""
    deadlines: dict[str, float] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        deadlines[f"{request.method} {request.url.path}"] = request.extensions["timeout"]["read"]
        if request.url.path == "/healthz":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/v0/beads/context":
            return httpx.Response(200, json=context())
        if request.method == "POST":
            raise httpx.ReadTimeout("slow")
        return httpx.Response(200, json={"items": [], "has_more": False})

    with BeadsSession(
        RemoteEndpoint("http://127.0.0.1:8080", timeout_seconds=5.0, write_timeout_seconds=90.0),
        ExpectedContext("expected", "scratch"),
        transport=httpx.MockTransport(handler),
    ) as session:
        session.list_issues(limit=1)
        with pytest.raises(IndeterminateWrite):
            session.create_issue(CreateIssueRequest(actor="agent", title="one"))
    assert deadlines["GET /healthz"] == 5.0
    assert deadlines["GET /v0/beads/issues"] == 5.0
    assert deadlines["POST /v0/beads/issues"] == 90.0


def test_service_spec_separates_probe_and_write_deadlines() -> None:
    from beadhive_beads_client import service

    fields = service.ServiceSpec.__dataclass_fields__
    assert fields["probe_seconds"].default == 5.0
    assert fields["write_seconds"].default >= 60.0
    assert fields["write_seconds"].default > fields["request_seconds"].default
    assert RemoteEndpoint("http://127.0.0.1:1").write_timeout_seconds >= 60.0
