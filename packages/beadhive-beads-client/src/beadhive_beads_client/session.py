"""One workspace-bound Beads v1.3 session over the generated wire client.

The session deliberately has no delete or sweep entry point. Callers select CLI
compatibility explicitly for operations whose HTTP semantics are not approved.
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TypeVar

import httpx

from beads_v1_3.api.default import (
    add_dependencies,
    claim_issue,
    claim_next_issue,
    close_issue,
    create_issue,
    get_context,
    get_issue,
    health,
    list_dependencies,
    list_issues,
    list_ready_work,
    remove_dependency,
    reopen_issue,
    update_issue,
)
from beads_v1_3.client import AuthenticatedClient
from beads_v1_3.models import (
    AddDependenciesRequest,
    AddDependenciesResponse,
    ClaimNextRequest,
    ClaimNextResponse,
    ClaimRequest,
    ClaimResponse,
    CloseIssueRequest,
    CloseIssueResponse,
    ContextResponse,
    CreateIssueRequest,
    DependencyEdges,
    Issue,
    IssueDetails,
    IssuesPage,
    Problem,
    ReadyPage,
    RemoveDependencyRequest,
    RemoveDependencyResponse,
    ReopenIssueRequest,
    ReopenIssueResponse,
    UpdateIssueRequest,
    UpdateIssueResponse,
)
from beads_v1_3.types import Response

T = TypeVar("T")
_BASE_CAPABILITIES = frozenset({"project.enforce", "issues.get", "issues.list", "ready.list"})
_CLI_COMPATIBILITY = {
    "gate.resolve": "Beads v1.3 has no HTTP gate route",
    "state.update": "Beads v1.3 has no HTTP state-dimension route",
    "merge_slot.acquire": "Beads v1.3 has no HTTP merge-slot route",
    "sync.push": "Beads v1.3 has no HTTP Dolt publication route",
    "backup": "Beads v1.3 has no HTTP backup route",
    "migration": "Beads v1.3 has no HTTP migration route",
}


class IncompatibleService(RuntimeError):
    """The service identity or wire contract differs from the pinned v1.3 contract."""


class CapabilityMissing(IncompatibleService):
    """The negotiated service does not advertise a required operation."""


class SessionTimeout(RuntimeError):
    """A read exceeded its request deadline."""


class IndeterminateWrite(RuntimeError):
    """A write may have committed; reconcile by reading before deciding what to do."""


class CliCompatibilityRequired(RuntimeError):
    """A named operation must be performed through an explicitly selected CLI path."""


class ServiceProblem(RuntimeError):
    """A typed RFC 9457 refusal from Beads."""

    def __init__(self, problem: Problem) -> None:
        self.problem = problem
        super().__init__(f"Beads {problem.status} {problem.code} (request {problem.request_id})")


@dataclass(frozen=True)
class ExpectedContext:
    project_id: str
    database: str
    required_capabilities: frozenset[str] = _BASE_CAPABILITIES
    repo_root: Path | None = None


@dataclass(frozen=True)
class RemoteEndpoint:
    url: str
    token: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0


@dataclass(frozen=True)
class LocalEndpoint:
    repo_root: Path
    port: int
    bd_executable: Path = Path("bd")
    token_file: Path | None = None
    startup_seconds: float = 15.0
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not 1 <= self.port <= 65535:
            raise ValueError("local Beads service requires a fixed TCP port")
        if self.startup_seconds <= 0 or self.timeout_seconds <= 0:
            raise ValueError("Beads service deadlines must be positive")


class BeadsSession:
    """Generated SDK composition for one verified Beads workspace.

    ``transport`` is an HTTP test seam. It cannot change the pinned wire types.
    There is intentionally no automatic HTTP-to-CLI retry after a write.
    """

    def __init__(
        self,
        endpoint: RemoteEndpoint | LocalEndpoint,
        expected: ExpectedContext,
        *,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.endpoint = endpoint
        self.expected = expected
        self._transport = transport
        self._process: subprocess.Popen[bytes] | None = None
        self._http: httpx.Client | None = None
        self._sdk: AuthenticatedClient | None = None
        self.context: ContextResponse | None = None

    def __enter__(self) -> BeadsSession:
        self.open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def open(self) -> BeadsSession:
        if self._sdk is not None:
            return self
        endpoint = self.endpoint
        if isinstance(endpoint, LocalEndpoint):
            token = self._read_token(endpoint.token_file) if endpoint.token_file else None
            url = f"http://127.0.0.1:{endpoint.port}"
            if self._transport is None:
                argv = [
                    str(endpoint.bd_executable),
                    "-C",
                    str(endpoint.repo_root),
                    "serve",
                    "--addr",
                    f"127.0.0.1:{endpoint.port}",
                ]
                if endpoint.token_file:
                    argv.extend(["--auth-token-file", str(endpoint.token_file)])
                self._process = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    close_fds=True,
                )
            startup_deadline = time.monotonic() + endpoint.startup_seconds
        else:
            url, token = endpoint.url, endpoint.token
            if not url.startswith("https://") and not url.startswith("http://127.0.0.1:"):
                raise ValueError("remote Beads endpoint requires HTTPS or explicit loopback")
            if token is None and not url.startswith("http://127.0.0.1:"):
                raise ValueError("remote Beads endpoint requires a bearer token")
            startup_deadline = time.monotonic()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        timeout = httpx.Timeout(endpoint.timeout_seconds)
        self._http = httpx.Client(
            base_url=url, headers=headers, timeout=timeout, transport=self._transport
        )
        self._sdk = AuthenticatedClient(base_url=url, token=token or "", timeout=timeout)
        self._sdk.set_httpx_client(self._http)
        try:
            self._negotiate(startup_deadline)
        except Exception:
            self.close()
            raise
        return self

    @staticmethod
    def _read_token(path: Path) -> str:
        token = next((line.strip() for line in path.read_text().splitlines() if line.strip()), "")
        if not token:
            raise ValueError("Beads token file has no token")
        return token

    def _negotiate(self, deadline: float) -> None:
        while True:
            try:
                assert self._sdk is not None
                live = health.sync_detailed(client=self._sdk)
                if live.status_code != 200:
                    raise IncompatibleService(f"Beads health probe returned {live.status_code}")
                context = self._unwrap(get_context.sync_detailed(client=self._sdk))
                if not isinstance(context, ContextResponse):
                    raise IncompatibleService("Beads context response has an unexpected shape")
                self._verify_context(context)
                self.context = context
                assert self._http is not None
                self._http.headers["Bd-Project-Id"] = context.project_id
                self._unwrap(list_ready_work.sync_detailed(client=self._sdk, limit=1))
                return
            except (httpx.ConnectError, httpx.TimeoutException, ServiceProblem) as exc:
                if time.monotonic() >= deadline:
                    raise IncompatibleService("Beads service did not become ready") from exc
                if self._process is not None and self._process.poll() is not None:
                    raise IncompatibleService(
                        f"Beads service exited during startup (status {self._process.returncode})"
                    ) from exc
                time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))

    def _verify_context(self, context: ContextResponse) -> None:
        if context.api_version != "v0" or context.bd_version != "1.3.0":
            raise IncompatibleService("Beads API or release version differs from pinned v1.3.0")
        if (
            context.project_id != self.expected.project_id
            or context.database != self.expected.database
        ):
            raise IncompatibleService("Beads project or database identity mismatch")
        if self.expected.repo_root is not None and context.repo_root != str(
            self.expected.repo_root
        ):
            raise IncompatibleService("Beads repository identity mismatch")
        missing = self.expected.required_capabilities.difference(context.capabilities)
        if missing:
            raise CapabilityMissing(f"Beads capability missing: {', '.join(sorted(missing))}")

    def _require(self, capability: str) -> AuthenticatedClient:
        if self.context is None or self._sdk is None:
            raise RuntimeError("Beads session is not open")
        if capability not in self.context.capabilities:
            raise CapabilityMissing(f"Beads capability missing: {capability}")
        return self._sdk

    @staticmethod
    def _unwrap(response: Response[T | Problem]) -> T:
        if isinstance(response.parsed, Problem):
            raise ServiceProblem(response.parsed)
        if response.parsed is None or response.status_code >= 400:
            raise IncompatibleService(f"Beads returned undocumented HTTP {response.status_code}")
        return response.parsed

    def _read(self, capability: str, call: object, *args: object, **kwargs: object) -> object:
        client = self._require(capability)
        try:
            return self._unwrap(call(*args, client=client, **kwargs))  # type: ignore[operator]
        except httpx.TimeoutException as exc:
            raise SessionTimeout(f"Beads read timed out: {capability}") from exc

    def _write(self, capability: str, call: object, *args: object, **kwargs: object) -> object:
        client = self._require(capability)
        try:
            return self._unwrap(call(*args, client=client, **kwargs))  # type: ignore[operator]
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise IndeterminateWrite(f"Beads write outcome unknown: {capability}") from exc

    def get_issue(self, issue_id: str) -> IssueDetails:
        return self._read("issues.get", get_issue.sync_detailed, issue_id)  # type: ignore[return-value]

    def list_issues(self, *, limit: int = 100, cursor: str | None = None) -> IssuesPage:
        kwargs: dict[str, object] = {"limit": limit}
        if cursor is not None:
            kwargs["cursor"] = cursor
        return self._read("issues.list", list_issues.sync_detailed, **kwargs)  # type: ignore[return-value]

    def list_ready(self, *, limit: int = 100) -> ReadyPage:
        return self._read("ready.list", list_ready_work.sync_detailed, limit=limit)  # type: ignore[return-value]

    def list_dependencies(self, issue_id: str) -> DependencyEdges:
        return self._read("dependencies.list", list_dependencies.sync_detailed, issue_id=[issue_id])  # type: ignore[return-value]

    def create_issue(self, body: CreateIssueRequest) -> Issue:
        return self._write("issues.create", create_issue.sync_detailed, body=body)  # type: ignore[return-value]

    def update_issue(self, issue_id: str, body: UpdateIssueRequest) -> UpdateIssueResponse:
        return self._write("issues.update", update_issue.sync_detailed, issue_id, body=body)  # type: ignore[return-value]

    def claim_issue(self, issue_id: str, body: ClaimRequest) -> ClaimResponse:
        return self._write("issues.claim", claim_issue.sync_detailed, issue_id, body=body)  # type: ignore[return-value]

    def claim_next(self, body: ClaimNextRequest) -> ClaimNextResponse:
        return self._write("issues.claimNext", claim_next_issue.sync_detailed, body=body)  # type: ignore[return-value]

    def close_issue(self, issue_id: str, body: CloseIssueRequest) -> CloseIssueResponse:
        return self._write("issues.close", close_issue.sync_detailed, issue_id, body=body)  # type: ignore[return-value]

    def reopen_issue(self, issue_id: str, body: ReopenIssueRequest) -> ReopenIssueResponse:
        return self._write("issues.reopen", reopen_issue.sync_detailed, issue_id, body=body)  # type: ignore[return-value]

    def add_dependencies(self, body: AddDependenciesRequest) -> AddDependenciesResponse:
        return self._write("dependencies.add", add_dependencies.sync_detailed, body=body)  # type: ignore[return-value]

    def remove_dependency(self, body: RemoveDependencyRequest) -> RemoveDependencyResponse:
        return self._write("dependencies.remove", remove_dependency.sync_detailed, body=body)  # type: ignore[return-value]

    @staticmethod
    def require_cli(operation: str) -> None:
        """Name an unsupported or administrative operation at its call site."""
        if operation not in _CLI_COMPATIBILITY:
            raise ValueError(f"operation has no approved CLI compatibility path: {operation}")
        raise CliCompatibilityRequired(f"{operation}: {_CLI_COMPATIBILITY[operation]}")

    def close(self) -> None:
        self.context = None
        if self._http is not None:
            self._http.close()
            self._http = None
        self._sdk = None
        if self._process is not None:
            process = self._process
            self._process = None
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=22)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)
