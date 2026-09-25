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
from beads_v1_3.models import CreateIssueRequest, IssuesPage

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
    with pytest.raises(CliCompatibilityRequired, match="gate.resolve"):
        BeadsSession.require_cli("gate.resolve")
    with pytest.raises(ValueError, match="no approved CLI"):
        BeadsSession.require_cli("issues.delete")


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
