"""An offline GitHub REST stand-in behind ``httpx.MockTransport`` (no socket, no clock, no sleep).

Grown from the ``bh-gj1hb`` prototype ``docs/spikes/artifacts/bh-gj1hb/fake_github_rest.py``
(ported, never imported) into a :class:`~beadhive.testing.tracker.world.TrackerWorld`, so the
shared conformance cases seed it and observe what the adapter sent. Documented GitHub REST
behaviour it reproduces (API version 2022-11-28):

* issues: ``state`` defaults to ``open``; ``sort`` to ``created`` and ``direction`` to ``desc``;
  ``since`` is inclusive on ``updated_at``; pull requests are listed too (with ``pull_request``);
  a deleted issue disappears from listings and ``GET`` on it answers ``410 This issue was
  deleted``; a transferred issue answers ``301`` to its new home;
* repository comments: ``sort`` ``created`` or ``updated``, ascending by default, and they
  include comments on pull requests (``html_url`` ``/pull/<n>#issuecomment-<id>``);
* per-issue comments: ``since`` only, always in creation order;
* ``Link: rel="next"`` absolute URLs on the numeric ``/repositories/<id>/...`` form, carrying
  every query parameter; ``per_page`` defaults to 30, at most 100;
* ``GET /user`` (``X-OAuth-Scopes`` on scoped tokens; ``401 Bad credentials``; ``403 Resource not
  accessible by integration`` for an installation token) and ``GET /repos/{o}/{r}`` (the
  ``permissions`` object only when a credential was sent);
* a secondary rate limit (``403`` + ``Retry-After``) on demand, and any scripted answer.

Every write answers ``405``: the write endpoints land with the write beads. Requests are kept so a
test can assert what was sent (only ``GET``, the verbatim ``next`` URL, the API version header).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode, urlsplit

import httpx

from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    CommentKey,
    IssueKey,
    IssueState,
    ProjectRef,
)
from beadhive.testing.tracker import TrackerSubject
from beadhive_plugins.credentials import ResolvedToken
from beadhive_tracker_github.adapter import GitHubTrackerAdapter

#: A synthetic credential: token-shaped so the scrub pattern recognises it, never a real one.
FAKE_TOKEN = "ghp_" + "F4keT0kenF0rTe5ts" * 2 + "XY"
EPOCH = datetime(2026, 1, 1, tzinfo=UTC)
REPOSITORY_ID = 9001
_APPS = {"ci-app": {"id": 77, "slug": "ci-app", "name": "CI app", "node_id": "A_77"}}


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


@dataclass
class FakeClock:
    """The mocked clock: ``now`` only moves when the adapter's injected ``sleep`` is called."""

    moment: datetime = EPOCH
    sleeps: list[float] = field(default_factory=list)

    def now(self) -> datetime:
        return self.moment

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.moment += timedelta(seconds=seconds)


def user_row(api: str, web: str, login: str, ident: int, kind: str = "User") -> dict[str, Any]:
    return {
        "login": login,
        "id": ident,
        "node_id": f"U_{ident}",
        "avatar_url": f"https://avatars.githubusercontent.com/u/{ident}?v=4",
        "url": f"{api}/users/{login}",
        "html_url": f"{web}/{login}",
        "type": kind,
        "site_admin": False,
    }


@dataclass
class FakeGitHub:
    """The world: server-side state plus the ``httpx`` handler that serves it."""

    owner: str = "acme"
    repo: str = "widgets"
    api: str = "https://api.github.com"
    web: str = "https://github.com"
    token: str = FAKE_TOKEN
    clock: FakeClock = field(default_factory=FakeClock)
    issues: list[dict[str, Any]] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    deleted: set[int] = field(default_factory=set)
    transferred: dict[int, str] = field(default_factory=dict)  # number -> "owner/name"
    moved: dict[int, dict[str, Any]] = field(default_factory=dict)  # number -> its new row
    requests: list[httpx.Request] = field(default_factory=list)
    scripted: deque[httpx.Response] = field(default_factory=deque)
    repo_meta: dict[str, Any] = field(
        default_factory=lambda: {
            "private": False,
            "archived": False,
            "disabled": False,
            "has_issues": True,
            "permissions": {
                "admin": False,
                "maintain": False,
                "push": True,
                "triage": True,
                "pull": True,
            },
        }
    )
    scopes: str | None = "repo"  # None models a fine-grained or installation token
    installation_token: bool = False
    me_login: str = "maintainer"
    me_id: int = 42
    throttled: int = 0
    retry_after: float = 0.0
    ambiguous_next_write: bool | None = None
    write_time: datetime | None = None
    _ids: int = 0

    # -- plumbing ---------------------------------------------------------------------------

    @property
    def sleeps(self) -> list[float]:
        return self.clock.sleeps

    def sleep(self, seconds: float) -> None:
        self.clock.sleep(seconds)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    @property
    def api_path(self) -> str:
        return urlsplit(self.api).path.rstrip("/")

    @property
    def root(self) -> str:
        return f"{self.api_path}/repos/{self.owner}/{self.repo}"

    def _next_id(self) -> int:
        self._ids += 1
        return self._ids

    def _user(self, name: str, *, bot: bool = False) -> dict[str, Any]:
        if bot:
            return user_row(self.api, self.web, f"{name}[bot]", 9000 + len(name), "Bot")
        return user_row(self.api, self.web, name, 100 + sum(map(ord, name)), "User")

    def me(self) -> dict[str, Any]:
        return user_row(self.api, self.web, self.me_login, self.me_id)

    def _row(self, number: int) -> dict[str, Any]:
        return next(i for i in self.issues if i["number"] == number)

    def _key(self, row: dict[str, Any]) -> IssueKey:
        return IssueKey(f"#{row['number']}", row["html_url"])

    # -- TrackerWorld -------------------------------------------------------------------------

    def add_issue(
        self,
        title: str,
        *,
        at: datetime,
        state: IssueState = IssueState.OPEN,
        labels: tuple[str, ...] = (),
    ) -> IssueKey:
        n = self._next_id()
        api_issue = f"{self.api}/repos/{self.owner}/{self.repo}/issues/{n}"
        closed = state is IssueState.CLOSED
        row = {
            "url": api_issue,
            "repository_url": f"{self.api}/repos/{self.owner}/{self.repo}",
            "labels_url": f"{api_issue}/labels{{/name}}",
            "comments_url": f"{api_issue}/comments",
            "events_url": f"{api_issue}/events",
            "html_url": f"{self.web}/{self.owner}/{self.repo}/issues/{n}",
            "id": 1000 + n,
            "node_id": f"I_kwDO{n:04d}",
            "number": n,
            "title": title,
            "user": self._user("reporter"),
            "labels": [self._label(name) for name in labels],
            "state": state.value,
            "locked": False,
            "assignee": None,
            "assignees": [],
            "milestone": None,
            "comments": 0,
            "created_at": iso(at),
            "updated_at": iso(at),
            "closed_at": iso(at) if closed else None,
            "author_association": "NONE",
            "active_lock_reason": None,
            "body": f"body of {n}",
            "timeline_url": f"{api_issue}/timeline",
            "performed_via_github_app": None,
            "state_reason": "completed" if closed else None,
        }
        self.issues.append(row)
        return self._key(row)

    def _label(self, name: str) -> dict[str, Any]:
        return {
            "id": 5000 + sum(map(ord, name)),
            "node_id": f"LA_{name}",
            "url": f"{self.api}/repos/{self.owner}/{self.repo}/labels/{name}",
            "name": name,
            "color": "ededed",
            "default": False,
            "description": None,
        }

    def update_issue(self, key: IssueKey, *, at: datetime, state: IssueState | None = None) -> None:
        row = self._row(int(key.identifier[1:]))
        row["updated_at"] = iso(at)
        if state is not None:
            row["state"] = state.value
            closed = state is IssueState.CLOSED
            row["closed_at"] = iso(at) if closed else None
            row["state_reason"] = "completed" if closed else "reopened"

    def delete_issue(self, key: IssueKey) -> None:
        number = int(key.identifier[1:])
        self.issues = [i for i in self.issues if i["number"] != number]
        self.comments = [c for c in self.comments if not c["issue_url"].endswith(f"/{number}")]
        self.deleted.add(number)

    def transfer_issue(self, key: IssueKey, to: str) -> None:
        """Move the issue to another repository: GitHub then answers 301 to its new home."""
        number = int(key.identifier[1:])
        row = self._row(number)
        self.issues.remove(row)
        self.transferred[number] = to
        row["html_url"] = f"{self.web}/{to}/issues/{number}"
        row["repository_url"] = f"{self.api}/repos/{to}"
        self.moved[number] = row

    def add_change_request(self, title: str, *, at: datetime) -> IssueKey:
        key = self.add_issue(title, at=at)
        row = self._row(int(key.identifier[1:]))
        row["html_url"] = row["html_url"].replace("/issues/", "/pull/")
        number = row["number"]
        pulls = f"{self.api}/repos/{self.owner}/{self.repo}/pulls/{number}"
        row["pull_request"] = {
            "url": pulls,
            "html_url": row["html_url"],
            "diff_url": f"{row['html_url']}.diff",
            "patch_url": f"{row['html_url']}.patch",
            "merged_at": None,
        }
        return self._key(row)

    def _comment_row(
        self, number: int, body: str, user: dict[str, Any], at: datetime, app=None
    ) -> dict[str, Any]:
        cid = 500 + self._next_id()
        parent = self._row(number)["html_url"]
        return {
            "url": f"{self.api}/repos/{self.owner}/{self.repo}/issues/comments/{cid}",
            "html_url": f"{parent}#issuecomment-{cid}",
            "issue_url": f"{self.api}/repos/{self.owner}/{self.repo}/issues/{number}",
            "id": cid,
            "node_id": f"IC_kwDO{cid:05d}",
            "user": user,
            "created_at": iso(at),
            "updated_at": iso(at),
            "author_association": "NONE",
            "body": body,
            "performed_via_github_app": app,
        }

    def add_comment(
        self,
        issue: IssueKey,
        body: str,
        *,
        at: datetime,
        author: str = "alice",
        integration: str | None = None,
    ) -> CommentKey:
        app = _APPS.get(integration) if integration else None
        user = self._user(author, bot=integration is not None)
        row = self._comment_row(int(issue.identifier[1:]), body, user, at, app)
        self.comments.append(row)
        return CommentKey(str(row["id"]))

    def add_own_comment(self, issue: IssueKey, body: str, *, at: datetime) -> CommentKey:
        """A comment authored by the credential's own actor (for marker decoding)."""
        row = self._comment_row(int(issue.identifier[1:]), body, self.me(), at)
        self.comments.append(row)
        return CommentKey(str(row["id"]))

    def edit_comment(self, key: CommentKey, body: str, *, at: datetime) -> None:
        row = next(c for c in self.comments if str(c["id"]) == key.id)
        row["body"], row["updated_at"] = body, iso(at)

    def delete_comment(self, key: CommentKey) -> None:
        self.comments = [c for c in self.comments if str(c["id"]) != key.id]

    def add_human_label(self, issue: IssueKey, label: str) -> None:
        self._row(int(issue.identifier[1:]))["labels"].append(self._label(label))

    def server_labels(self, issue: IssueKey) -> frozenset[str]:
        row = self._row(int(issue.identifier[1:]))
        return frozenset(label["name"] for label in row["labels"])

    def comment_count(self, issue: IssueKey) -> int:
        suffix = f"/issues/{issue.identifier[1:]}"
        return sum(1 for c in self.comments if c["issue_url"].endswith(suffix))

    def throttle(self, requests: int, retry_after_seconds: float) -> None:
        self.throttled, self.retry_after = requests, retry_after_seconds

    def fail_next_write_ambiguously(self, applied: bool = True) -> None:
        self.ambiguous_next_write = applied

    def write_requests(self) -> int:
        return sum(1 for r in self.requests if r.method != "GET")

    def read_requests(self) -> int:
        return sum(1 for r in self.requests if r.method == "GET")

    def set_write_time(self, at: datetime) -> None:
        self.write_time = at

    def url_variants(self, key: IssueKey) -> tuple[str, ...]:
        number = key.identifier[1:]
        upper = f"{self.web}/{self.owner.upper()}/{self.repo.capitalize()}/issues/{number}"
        return (
            f"{key.url}/",
            f"{key.url}?view=full",
            f"{key.url}#issuecomment-1",
            f"{key.url}/?a=b#c",
            f"  {key.url}  ",
            upper,
            key.url.replace("https://", "http://", 1),
            f"{self.api}/repos/{self.owner}/{self.repo}/issues/{number}",
        )

    def foreign_issue_url(self) -> str:
        return f"{self.web}/someone-else/elsewhere/issues/1"

    def unrecognized_url(self) -> str:
        return f"{self.web}/{self.owner}/{self.repo}/wiki/Home"

    # -- HTTP ---------------------------------------------------------------------------------

    def _auth(self, request: httpx.Request) -> str | None:
        """``None``: anonymous; ``"ok"``: the expected token; ``"bad"``: any other token."""
        header = request.headers.get("authorization")
        if header is None:
            return None
        return "ok" if header in (f"token {self.token}", f"Bearer {self.token}") else "bad"

    def _scope_headers(self, auth: str | None) -> dict[str, str]:
        if auth == "ok" and self.scopes is not None:
            return {"x-oauth-scopes": self.scopes}
        return {}

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.scripted:
            return self.scripted.popleft()
        if self.throttled:
            self.throttled -= 1
            return httpx.Response(
                403,
                json={"message": "You have exceeded a secondary rate limit."},
                headers={"retry-after": str(int(self.retry_after))},
            )
        auth = self._auth(request)
        if auth == "bad":
            return httpx.Response(401, json={"message": "Bad credentials"})
        if request.method != "GET":
            return httpx.Response(405, json={"message": "fake: writes land with the write beads"})
        path = unquote(request.url.path)
        query = {k: v[-1] for k, v in parse_qs(request.url.query.decode()).items()}
        headers = self._scope_headers(auth)
        if path == f"{self.api_path}/user":
            if auth is None:
                return httpx.Response(401, json={"message": "Requires authentication"})
            if self.installation_token:
                message = "Resource not accessible by integration"
                return httpx.Response(403, json={"message": message})
            return httpx.Response(200, json=self.me(), headers=headers)
        repo_paths = (self.root, f"{self.api_path}/repositories/{REPOSITORY_ID}")
        base = next((p for p in repo_paths if path == p or path.startswith(p + "/")), None)
        if base is None:
            return self._moved_or_missing(path)
        rest = path[len(base) :]
        if rest == "":
            meta = dict(self.repo_meta)
            if auth is None:
                meta.pop("permissions", None)
            body = {"id": REPOSITORY_ID, "full_name": f"{self.owner}/{self.repo}", **meta}
            return httpx.Response(200, json=body, headers=headers)
        if not self.repo_meta.get("has_issues", True):
            return httpx.Response(410, json={"message": "Issues are disabled for this repo"})
        if rest == "/issues":
            state = query.get("state", "open")
            rows = [i for i in self.issues if state == "all" or i["state"] == state]
            return self._list(rest, rows, query, sort_default="created", dir_default="desc")
        if rest == "/issues/comments":
            return self._list(rest, list(self.comments), query)
        parts = rest.removeprefix("/issues/").split("/")
        if not parts[0].isdigit():
            return httpx.Response(404, json={"message": "Not Found"})
        number = int(parts[0])
        if number in self.deleted:
            return httpx.Response(410, json={"message": "This issue was deleted"})
        if number in self.transferred:
            to = self.transferred[number]
            location = f"{self.api}/repos/{to}/issues/{number}"
            return httpx.Response(301, headers={"location": location})
        if not any(i["number"] == number for i in self.issues):
            return httpx.Response(404, json={"message": "Not Found"})
        if parts[1:] == []:
            return httpx.Response(200, json=self._row(number))
        if parts[1:] == ["comments"]:
            suffix = f"/issues/{number}"
            rows = [c for c in self.comments if c["issue_url"].endswith(suffix)]
            fixed = {**query, "sort": "created", "direction": "asc"}
            return self._list(rest, rows, fixed, keep=query)
        return httpx.Response(404, json={"message": "Not Found"})

    def _moved_or_missing(self, path: str) -> httpx.Response:
        for number, row in self.moved.items():
            to = self.transferred[number]
            if path == f"{self.api_path}/repos/{to}/issues/{number}":
                return httpx.Response(200, json=row)
        return httpx.Response(404, json={"message": "Not Found"})

    def _list(
        self,
        rest: str,
        rows: list[dict[str, Any]],
        query: dict[str, str],
        *,
        sort_default: str = "created",
        dir_default: str = "asc",
        keep: dict[str, str] | None = None,
    ) -> httpx.Response:
        if since := query.get("since"):
            rows = [r for r in rows if _parse(r["updated_at"]) >= _parse(since)]
        sort = query.get("sort", sort_default)
        field_name = "updated_at" if sort == "updated" else "created_at"
        reverse = query.get("direction", dir_default) == "desc"
        rows = sorted(rows, key=lambda r: (r[field_name], r["id"]), reverse=reverse)
        per_page = min(int(query.get("per_page", "30")), 100)
        page = int(query.get("page", "1"))
        window = rows[(page - 1) * per_page : page * per_page]
        headers = {"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "1767229200"}
        if page * per_page < len(rows):
            params = {**(keep if keep is not None else query), "page": str(page + 1)}
            params.setdefault("per_page", str(per_page))
            target = f"{self.api}/repositories/{REPOSITORY_ID}{rest}"
            headers["link"] = (
                f'<{target}?{urlencode(params)}>; rel="next", '
                f'<{target}?{urlencode({**params, "page": "99"})}>; rel="last"'
            )
        return httpx.Response(200, json=window, headers=headers)


# -- building adapters over the world ------------------------------------------------------------


GHES = {"api": "https://ghe.example/api/v3", "web": "https://ghe.example"}


def token_source(world: FakeGitHub, source: str = "env:GITHUB_TOKEN"):
    """A zero-argument token source that hands out the world's synthetic token."""
    return lambda: ResolvedToken(world.token, source)


def adapter_for(
    world: FakeGitHub,
    *,
    enabled=None,
    page_size: int | None = None,
    budget=None,
    include_closed: bool = True,
    http_cache: bool = False,
    tokens=None,
):
    """A ``GitHubTrackerAdapter`` sending through ``world`` (both reads enabled by default)."""
    base_url = None if world.api == "https://api.github.com" else world.api
    return GitHubTrackerAdapter(
        project=ProjectRef("github", f"{world.owner}/{world.repo}"),
        token_source=tokens if tokens is not None else token_source(world),
        sleep=world.sleep,
        clock=world.clock.now,
        enabled=READ_OPERATIONS if enabled is None else enabled,
        budget=budget,
        page_size=page_size,
        include_closed=include_closed,
        base_url=base_url,
        http_cache=http_cache,
        transport=world.transport(),
    )


def github_subject(*, enabled=frozenset(), page_size: int = 2, budget=None, ghes: bool = False):
    """A conformance ``TrackerFactory``: both reads on, plus the WRITE operations named."""
    world = FakeGitHub(**GHES) if ghes else FakeGitHub()
    adapter = adapter_for(
        world, enabled=READ_OPERATIONS | enabled, page_size=page_size, budget=budget
    )
    return TrackerSubject(adapter, world)
