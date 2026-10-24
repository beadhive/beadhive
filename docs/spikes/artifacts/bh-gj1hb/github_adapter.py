"""GitHub adapter prototype behind the bead tracker adapter port (spike bh-gj1hb).

Proposed home: ``packages/beadhive-tracker-github/src/beadhive_tracker_github/adapter.py``, a
plugin package that imports only ``beadhive.modules.tracker.contracts`` from root. This prototype
uses plain ``httpx`` so it runs in a two-package scratch venv; the production adapter uses
githubkit (spike bh-a1svm's verdict) behind the SAME mapping, which is the point: everything
GitHub-shaped (numbers, ``html_url``, ``Link`` URLs, ``pull_request`` keys,
``performed_via_github_app``, ``X-OAuth-Scopes``, the footer marker) stops in this file.

The adapter knows nothing about plugin wiring (manifests, config namespaces, CLI, enable flags):
the composition root constructs it with explicit arguments. ``enabled`` is the operator's
already-resolved write configuration; the token is already on the injected client
(``TokenSource``, spike bh-mqvdq); ``sleep`` is injected so tests never wait.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from tracker_contracts import (
    READ_OPERATIONS,
    WRITE_OPERATIONS,
    Actor,
    ActorKey,
    ActorKind,
    AuthenticationFailed,
    CapabilityReport,
    CapabilityState,
    Checkpoint,
    CommentKey,
    CommentQuery,
    IdPage,
    IdQuery,
    IdSubject,
    IntegrationRef,
    IssueKey,
    IssueQuery,
    IssueState,
    LabelNotOwned,
    LabelOwnership,
    NotVisible,
    Operation,
    OperationNotEnabled,
    OwnMarker,
    Page,
    PageCursor,
    PayloadRejected,
    PermissionDenied,
    ProjectRef,
    RateLimited,
    RetryBudget,
    Revision,
    TargetStateRefused,
    TrackerComment,
    TrackerDescriptor,
    TrackerError,
    TrackerIssue,
    TransientFailure,
    Tri,
    WriteGrant,
    WriteOutcomeUnknown,
    WriteReceipt,
)

_NEXT = re.compile(r'<([^<>]+)>\s*;\s*rel="next"')
_MARKER = re.compile(
    r"^<!-- bh:v1 hive=(?P<hive>\S+) install=(?P<install>\S+) outbox=(?P<outbox>\S+) -->$"
)
_KINDS = {"User": ActorKind.HUMAN, "Bot": ActorKind.BOT}
_PREFIX = "github:"


def _ts(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _fmt(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def encode_marker(body: str, marker: OwnMarker) -> str:
    line = f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"
    return f"{body}\n\n{line}"


def decode_marker(body: str) -> tuple[str, OwnMarker | None]:
    """The marker counts only as the LAST non-empty line (a quoted copy is ignored)."""
    lines = body.rstrip().split("\n")
    match = _MARKER.match(lines[-1].strip()) if lines else None
    if not match:
        return body, None
    marker = OwnMarker(match["hive"], match["install"], match["outbox"])
    return "\n".join(lines[:-1]).rstrip(), marker


def _actor(user: dict[str, Any] | None) -> Actor | None:
    if not user:
        return None
    kind = _KINDS.get(user.get("type", ""), ActorKind.UNKNOWN)
    return Actor(ActorKey(str(user["id"]), kind), display=user.get("login", ""))


def _issue_key_from_comment(row: dict[str, Any]) -> IssueKey:
    # The repository-wide list names the parent only by URL; derive the key from the
    # comment's own web URL so a GHES host works unchanged.
    parent = row["html_url"].split("#", 1)[0]
    return IssueKey(f"#{parent.rsplit('/', 1)[1]}", parent)


class GitHubTrackerAdapter:
    """Implements ``TrackerReader``, ``TrackerIdentity``, ``CommentWriter``, ``LabelWriter``."""

    def __init__(
        self,
        client: httpx.Client,
        *,
        owner: str,
        repo: str,
        sleep: Callable[[float], None],
        enabled: frozenset[Operation] = frozenset(),
        budget: RetryBudget | None = None,
        page_size: int = 100,
        credential_source: str | None = None,
    ) -> None:
        self._client = client
        self._project = ProjectRef("github", f"{owner}/{repo}")
        self._root = f"/repos/{owner}/{repo}"
        self._sleep = sleep
        self._enabled = READ_OPERATIONS | (enabled & WRITE_OPERATIONS)
        self._budget = budget or RetryBudget()
        self._page_size = page_size
        self._source = credential_source
        self._used_grants: set[str] = set()

    # -- transport: retry within the budget, map every failure to a port error ----------------

    def _send(self, method: str, url: str, *, write: bool = False, **kwargs: Any) -> Any:
        waited, attempt = 0.0, 0
        while True:
            try:
                response = self._client.request(method, url, **kwargs)
            except httpx.TransportError as exc:
                if write:
                    detail = f"{method} did not complete: {type(exc).__name__}"
                    raise WriteOutcomeUnknown(detail) from exc
                response = None
            wait, error = self._classify(response, write=write)
            if error is None:
                return response
            if wait is None:
                raise error
            budget = self._budget
            if (
                attempt >= budget.max_retries
                or wait > budget.max_single_wait.total_seconds()
                or waited + wait > budget.max_total_wait.total_seconds()
            ):
                raise error
            self._sleep(wait)
            waited, attempt = waited + wait, attempt + 1

    def _classify(
        self, response: httpx.Response | None, *, write: bool
    ) -> tuple[float | None, TrackerError | None]:
        """``(wait, error)``: no error = success; a wait = retryable after that many seconds."""
        if response is None:
            return 1.0, TransientFailure("the GitHub connection failed")
        status = response.status_code
        if status < 400:
            return None, None
        message = str(response.json().get("message", "")) if response.content else ""
        retry_after = response.headers.get("retry-after")
        if status in (403, 429) and (
            retry_after or response.headers.get("x-ratelimit-remaining") == "0"
        ):
            # A rate-limit answer proves the write did NOT happen, so it may be retried.
            wait = float(retry_after) if retry_after else 60.0
            kind = "throttle" if retry_after else "quota"
            limited = RateLimited(message, retry_after=timedelta(seconds=wait), kind=kind)
            return wait, limited
        if status >= 500:
            if write:
                return None, WriteOutcomeUnknown(f"GitHub answered {status} to a write")
            return 1.0, TransientFailure(f"GitHub answered {status}")
        if status == 401:
            return None, AuthenticationFailed("bad credentials")
        if status == 403:
            needed = response.headers.get("x-accepted-github-permissions")
            return None, PermissionDenied(message, operation=None, needed=needed)
        if status == 404:
            return None, NotVisible("not found, or not visible to this credential")
        if status == 410:
            return None, TargetStateRefused("issues are disabled on this repository")
        if status == 422:
            return None, PayloadRejected(message)
        return None, TrackerError(f"GitHub answered {status}")

    def _page(
        self, path: str, params: dict[str, str], cursor: PageCursor | None, size: int | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        if cursor is not None:  # the server's own next URL, followed verbatim (bh-a1svm E5)
            response = self._send("GET", cursor.token)
        else:
            per_page = str(min(size or self._page_size, 100))
            response = self._send("GET", path, params={**params, "per_page": per_page})
        match = _NEXT.search(response.headers.get("link", ""))
        return response.json(), (match.group(1) if match else None)

    # -- TrackerReader ------------------------------------------------------------------------

    def describe(self) -> TrackerDescriptor:
        return TrackerDescriptor(self._project, 1, READ_OPERATIONS | WRITE_OPERATIONS)

    @staticmethod
    def _since(checkpoint: Checkpoint | None) -> dict[str, str]:
        if checkpoint is None:
            return {}
        if not checkpoint.token.startswith(_PREFIX):
            raise ValueError("a checkpoint from another adapter")
        return {"since": checkpoint.token.removeprefix(_PREFIX)}

    @staticmethod
    def _checkpoint(rows: list[dict[str, Any]], fallback: Checkpoint | None) -> Checkpoint | None:
        if not rows:
            return fallback
        return Checkpoint(_PREFIX + _fmt(max(_ts(r["updated_at"]) for r in rows)))

    def _issue(self, row: dict[str, Any]) -> TrackerIssue:
        return TrackerIssue(
            key=IssueKey(
                f"#{row['number']}",
                row["html_url"],
                internal_id=str(row["id"]),
                global_id=row.get("node_id"),
            ),
            title=row["title"],
            body=row.get("body") or "",
            state=IssueState.CLOSED if row["state"] == "closed" else IssueState.OPEN,
            labels=frozenset(label["name"] for label in row.get("labels", [])),
            author=_actor(row.get("user")),
            created_at=_ts(row["created_at"]),
            revision=Revision(_ts(row["updated_at"])),
            closed_at=_ts(row["closed_at"]) if row.get("closed_at") else None,
            closed_reason=row.get("state_reason"),
        )

    def list_issues(self, query: IssueQuery) -> Page[TrackerIssue]:
        params = {
            "state": "all" if query.include_closed else "open",  # GitHub's default hides closed
            "sort": "updated",
            "direction": "asc",  # oldest revision first: a per-page checkpoint is safe
            **self._since(query.since),
        }
        rows, nxt = self._page(f"{self._root}/issues", params, query.cursor, query.page_size)
        issues = tuple(self._issue(r) for r in rows if "pull_request" not in r)
        cursor = PageCursor(nxt) if nxt else None
        # The high-water mark counts pull requests too: they share the issues ordering.
        return Page(issues, cursor, self._checkpoint(rows, query.since))

    def _comment(self, row: dict[str, Any]) -> TrackerComment:
        body, marker = decode_marker(row.get("body") or "")
        app = row.get("performed_via_github_app")
        return TrackerComment(
            key=CommentKey(str(row["id"]), global_id=row.get("node_id")),
            issue=_issue_key_from_comment(row),
            author=_actor(row.get("user")),
            body=body,
            created_at=_ts(row["created_at"]),
            revision=Revision(_ts(row["updated_at"])),
            via=IntegrationRef(str(app["id"]), app.get("slug", "")) if app else None,
            marker=marker,
        )

    def list_comments(self, query: CommentQuery) -> Page[TrackerComment]:
        if query.issue is None:
            path = f"{self._root}/issues/comments"
            params = {"sort": "updated", "direction": "asc", **self._since(query.since)}
            ordered = True
        else:  # per-issue comments come in creation order and accept only `since`
            path = f"{self._root}/issues/{query.issue.identifier.lstrip('#')}/comments"
            params, ordered = self._since(query.since), False
        cursor_token, high_water = self._split_cursor(query.cursor)
        cursor = PageCursor(cursor_token) if cursor_token else None
        rows, nxt = self._page(path, params, cursor, query.page_size)
        comments = tuple(self._comment(r) for r in rows if "/pull/" not in r["html_url"])
        if ordered:
            return Page(
                comments, PageCursor(nxt) if nxt else None, self._checkpoint(rows, query.since)
            )
        # Unordered: carry the running maximum inside the opaque cursor; emit it at the end.
        seen = [r["updated_at"] for r in rows] + ([high_water] if high_water else [])
        top = max(seen, key=_ts) if seen else None
        if nxt:
            return Page(comments, PageCursor(json.dumps({"next": nxt, "hw": top})), None)
        final = Checkpoint(_PREFIX + _fmt(_ts(top))) if top else query.since
        return Page(comments, None, final)

    @staticmethod
    def _split_cursor(cursor: PageCursor | None) -> tuple[str | None, str | None]:
        if cursor is None:
            return None, None
        if cursor.token.startswith("{"):
            data = json.loads(cursor.token)
            return data["next"], data["hw"]
        return cursor.token, None

    def list_ids(self, query: IdQuery) -> IdPage:
        if query.subject is IdSubject.ISSUES:
            page = self.list_issues(IssueQuery(cursor=query.cursor, page_size=query.page_size))
            return IdPage(tuple(i.key.url for i in page.items), page.next_cursor)
        comments = self.list_comments(
            CommentQuery(issue=query.issue, cursor=query.cursor, page_size=query.page_size)
        )
        return IdPage(tuple(c.key.id for c in comments.items), comments.next_cursor)

    # -- TrackerIdentity ----------------------------------------------------------------------

    def whoami(self) -> Actor | None:
        return _actor(self._send("GET", "/user").json())

    def probe_capabilities(self) -> CapabilityReport:
        """GET-only (spike bh-mqvdq E3): NO is definitive, YES means 'not refuted'."""
        response = self._send("GET", self._root)
        repo = response.json()
        scopes = response.headers.get("x-oauth-scopes")
        role = repo.get("permissions") or {}
        blocked = repo.get("archived") or repo.get("disabled") or not repo.get("has_issues")

        def probe(op: Operation) -> tuple[Tri, str]:
            if op in READ_OPERATIONS:
                return Tri.YES, "repository readable"
            if blocked or not role:
                return Tri.NO, "archived, disabled, issues off, or no credential"
            if op is Operation.LABELS_CHANGE and not (role.get("triage") or role.get("push")):
                return Tri.NO, "role below triage"
            if scopes is None:
                return Tri.UNKNOWN, "token grants are not readable (fine-grained or App token)"
            granted = {s.strip() for s in scopes.split(",")}
            if "repo" in granted or ("public_repo" in granted and not repo.get("private")):
                return Tri.YES, "scope and role allow it (not proven until the first write)"
            return Tri.NO, "token scope lacks repo"

        states = {}
        for op in READ_OPERATIONS | WRITE_OPERATIONS:
            tri, reason = probe(op)
            states[op] = CapabilityState(op, True, op in self._enabled, tri, reason)
        return CapabilityReport(self.whoami(), self._source, states)

    # -- writes -------------------------------------------------------------------------------

    def _authorise(self, op: Operation, issue: IssueKey, grant: WriteGrant) -> str:
        if op not in self._enabled:
            raise OperationNotEnabled(f"{op.value} is not enabled for {self._project.locator}")
        if grant.operation is not op or grant.target != issue:
            raise OperationNotEnabled(f"grant {grant.grant_id} does not cover {op.value}")
        if grant.grant_id in self._used_grants:
            raise OperationNotEnabled(f"grant {grant.grant_id} was already used")
        self._used_grants.add(grant.grant_id)
        return f"{self._root}/issues/{issue.identifier.lstrip('#')}"

    def post_comment(
        self, issue: IssueKey, body: str, *, marker: OwnMarker, grant: WriteGrant
    ) -> WriteReceipt:
        path = self._authorise(Operation.COMMENT_CREATE, issue, grant)
        payload = {"body": encode_marker(body, marker)}
        row = self._send("POST", f"{path}/comments", write=True, json=payload).json()
        return WriteReceipt(grant.grant_id, grant.operation, issue, comment=self._comment(row))

    def change_labels(
        self,
        issue: IssueKey,
        *,
        add: frozenset[str],
        remove: frozenset[str],
        ownership: LabelOwnership,
        grant: WriteGrant,
    ) -> WriteReceipt:
        foreign = sorted(label for label in add | remove if not ownership.owns(label))
        if foreign:
            raise LabelNotOwned(f"labels outside the owned set: {', '.join(foreign)}")
        path = self._authorise(Operation.LABELS_CHANGE, issue, grant)
        labels: list[dict[str, Any]] | None = None
        if add:
            body = {"labels": sorted(add)}
            labels = self._send("POST", f"{path}/labels", write=True, json=body).json()
        for name in sorted(remove):
            try:
                labels = self._send("DELETE", f"{path}/labels/{name}", write=True).json()
            except NotVisible:  # already absent: the desired end state holds
                continue
        if labels is None:
            labels = self._send("GET", path).json()["labels"]
        after = frozenset(label["name"] for label in labels)
        return WriteReceipt(grant.grant_id, grant.operation, issue, labels_after=after)
