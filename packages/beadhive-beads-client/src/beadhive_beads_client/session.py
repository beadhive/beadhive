"""One workspace-bound Beads v1.3 session over the generated wire client.

The session deliberately has no delete or sweep entry point. Callers select CLI
compatibility explicitly for operations whose HTTP semantics are not approved.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from functools import cache
from importlib.resources import files
from pathlib import Path
from typing import Any, TypeVar

import httpx

from beads_v1_3.api.default import (
    add_comment,
    add_dependencies,
    apply_batch,
    claim_issue,
    claim_next_issue,
    close_issue,
    compare_and_set_metadata,
    create_issue,
    get_context,
    get_issue,
    health,
    list_dependencies,
    list_issues,
    list_ready_work,
    release_issue,
    remove_dependency,
    reopen_issue,
    update_issue,
)
from beads_v1_3.client import AuthenticatedClient
from beads_v1_3.models import (
    AddCommentRequest,
    AddDependenciesRequest,
    AddDependenciesResponse,
    ApplyBatchRequest,
    ApplyBatchResponse,
    ClaimNextRequest,
    ClaimNextResponse,
    ClaimRequest,
    ClaimResponse,
    CloseIssueRequest,
    CloseIssueResponse,
    Comment,
    CompareAndSetMetadataRequest,
    CompareAndSetMetadataResponse,
    ContextResponse,
    CreateIssueRequest,
    DependencyEdges,
    Issue,
    IssueDetails,
    IssuesPage,
    ListIssuesSort,
    Problem,
    ReadyPage,
    ReleaseIssueRequest,
    ReleaseIssueResponse,
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


@cache
def load_operation_matrix() -> dict[str, Any]:
    """Load the installed routing evidence consumed by downstream core code."""
    payload = files("beadhive_beads_client").joinpath("operation_matrix_v1.json").read_text()
    return json.loads(payload)


def cli_compatibility_operations() -> frozenset[str]:
    """Return every explicitly approved CLI or administrative operation name."""
    return frozenset(
        row["name"]
        for row in load_operation_matrix()["operations"]
        if row["classification"] in {"cli-compatibility", "administrative"}
    )


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

    def get_issue(self, issue_id: str, *, include_comments: bool = False) -> IssueDetails:
        return self._read(
            "issues.get",
            get_issue.sync_detailed,
            issue_id,
            include_comments=include_comments,
        )  # type: ignore[return-value]

    def list_issues(
        self,
        *,
        limit: int = 100,
        cursor: str | None = None,
        parent: str | None = None,
        sort: str | None = None,
        status: list[str] | None = None,
        all_: bool = False,
        include_infra: bool = False,
    ) -> IssuesPage:
        """``GET /v0/beads/issues``. ``parent`` restricts to RECURSIVE descendants of the named
        issue (the OpenAPI spec's own wording — not the direct one-level edge ``IssueWithCounts``
        reports on each row); a caller that wants direct children only must narrow the returned
        rows itself (see ``beadhive_core.queue.direct_children``). ``sort`` is the closed
        ``created``/``priority`` vocabulary this operation publishes — absent, it defaults to
        ``created``, NOT ``bd list``'s flagless ``priority`` ordering, so a caller reproducing
        `bd list` output must pass ``sort="priority"`` explicitly.

        ``status`` / ``all_`` / ``include_infra`` (bh-sy36q.5) reproduce ``bd list``'s own
        ``--all`` / ``--include-infra`` override of its default exclusions: with all three left
        at their defaults this operation drops closed/done/frozen-status, template, gate and
        configured-infra rows exactly as `bd list` does unflagged, so a caller that needs a
        molecule's full RESTARTABLE membership — closed children and infra rows (gate/event
        beads) included, the same set ``bd list --parent <epic> --include-infra --all`` returns
        — must ask for them explicitly. ``all_=True`` drops the default status exclusions
        (``bd``'s ``--all``); ``include_infra=True`` admits the configured infrastructure issue
        types (``bd``'s ``--include-infra``); ``status`` is a narrower alternative to ``all_``
        when only specific statuses are wanted, matching this operation's own ``status`` query
        parameter (repeatable, replaces rather than adds to the default exclusions)."""
        kwargs: dict[str, object] = {"limit": limit}
        if cursor is not None:
            kwargs["cursor"] = cursor
        if parent is not None:
            kwargs["parent"] = parent
        if sort is not None:
            kwargs["sort"] = ListIssuesSort(sort)
        if status is not None:
            kwargs["status"] = status
        if all_:
            kwargs["all_"] = True
        if include_infra:
            kwargs["include_infra"] = True
        return self._read("issues.list", list_issues.sync_detailed, **kwargs)  # type: ignore[return-value]

    def list_ready(
        self,
        *,
        limit: int = 100,
        assignee: str | None = None,
        unassigned: bool | None = None,
        type_: str | None = None,
        exclude_type: list[str] | None = None,
        label: list[str] | None = None,
        label_any: list[str] | None = None,
        exclude_label: list[str] | None = None,
        priority: int | None = None,
        parent: str | None = None,
        has_metadata_key: str | None = None,
        metadata_field: list[str] | None = None,
    ) -> ReadyPage:
        """``GET /v0/beads/ready``, the same predicate ``bd ready`` and `work.claim-next` share.
        ``parent`` restricts to RECURSIVE descendants of the named issue (the OpenAPI spec's own
        wording), unlike the direct one-level edge ``IssueWithCounts.parent`` reports on each row.
        Every other parameter here is optional and omitted (not sent) when left ``None``, matching
        the generated client's own UNSET-by-default contract."""
        kwargs: dict[str, object] = {"limit": limit}
        if assignee is not None:
            kwargs["assignee"] = assignee
        if unassigned is not None:
            kwargs["unassigned"] = unassigned
        if type_ is not None:
            kwargs["type_"] = type_
        if exclude_type is not None:
            kwargs["exclude_type"] = exclude_type
        if label is not None:
            kwargs["label"] = label
        if label_any is not None:
            kwargs["label_any"] = label_any
        if exclude_label is not None:
            kwargs["exclude_label"] = exclude_label
        if priority is not None:
            kwargs["priority"] = priority
        if parent is not None:
            kwargs["parent"] = parent
        if has_metadata_key is not None:
            kwargs["has_metadata_key"] = has_metadata_key
        if metadata_field is not None:
            kwargs["metadata_field"] = metadata_field
        return self._read("ready.list", list_ready_work.sync_detailed, **kwargs)  # type: ignore[return-value]

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

    def release_issue(self, issue_id: str, body: ReleaseIssueRequest) -> ReleaseIssueResponse:
        return self._write("issues.release", release_issue.sync_detailed, issue_id, body=body)  # type: ignore[return-value]

    def close_issue(self, issue_id: str, body: CloseIssueRequest) -> CloseIssueResponse:
        return self._write("issues.close", close_issue.sync_detailed, issue_id, body=body)  # type: ignore[return-value]

    def reopen_issue(self, issue_id: str, body: ReopenIssueRequest) -> ReopenIssueResponse:
        return self._write("issues.reopen", reopen_issue.sync_detailed, issue_id, body=body)  # type: ignore[return-value]

    def add_dependencies(self, body: AddDependenciesRequest) -> AddDependenciesResponse:
        return self._write("dependencies.add", add_dependencies.sync_detailed, body=body)  # type: ignore[return-value]

    def batch_apply(self, body: ApplyBatchRequest) -> ApplyBatchResponse:
        """``POST /v0/beads/issues:batchApply`` — an ordered, all-or-nothing plan of creates and
        dependency edges. See :mod:`beadhive_core.planning` for the pure compiler that builds
        ``body`` from a validated Beadhive molecule spec."""
        return self._write("issues.batchApply", apply_batch.sync_detailed, body=body)  # type: ignore[return-value]

    def remove_dependency(self, body: RemoveDependencyRequest) -> RemoveDependencyResponse:
        return self._write("dependencies.remove", remove_dependency.sync_detailed, body=body)  # type: ignore[return-value]

    def add_comment(self, issue_id: str, body: AddCommentRequest) -> Comment:
        return self._write("issues.addComment", add_comment.sync_detailed, issue_id, body=body)  # type: ignore[return-value]

    def compare_and_set_metadata(
        self, issue_id: str, body: CompareAndSetMetadataRequest
    ) -> CompareAndSetMetadataResponse:
        return self._write(
            "issues.casMetadata", compare_and_set_metadata.sync_detailed, issue_id, body=body
        )  # type: ignore[return-value]

    @staticmethod
    def require_cli(operation: str) -> None:
        """Name an unsupported or administrative operation at its call site."""
        row = next(
            (
                candidate
                for candidate in load_operation_matrix()["operations"]
                if candidate["name"] == operation
            ),
            None,
        )
        if row is None or row["classification"] not in {"cli-compatibility", "administrative"}:
            raise ValueError(f"operation has no approved CLI compatibility path: {operation}")
        raise CliCompatibilityRequired(f"{operation}: {row['reason']}")

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
