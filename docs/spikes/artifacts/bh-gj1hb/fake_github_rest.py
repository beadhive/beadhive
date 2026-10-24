"""Offline GitHub REST stand-in, with the WRITE endpoints, for spike bh-gj1hb.

Extends the read-only fake of spike bh-a1svm (``docs/spikes/artifacts/bh-a1svm/fake_github.py``)
with the behaviour the port contract needs: comment authors and Apps, edits and deletions,
pull-request conversation on the repository-wide comment list, ``sort``/``direction``, the
comment and label writes, ``GET /user`` and ``GET /repos/{o}/{r}`` for identity and capability,
a secondary rate limit, and a write that is applied but answered ``502``.

Documented GitHub behaviour it reproduces (REST, API version 2022-11-28 and later):

* issues: ``state`` defaults to ``open``; ``sort`` defaults to ``created``, ``direction`` to
  ``desc``; ``since`` is inclusive on ``updated_at``; pull requests are listed too;
* repository comments: ``sort`` ``created``/``updated``, ascending by default; they include
  comments on pull requests (their ``html_url`` is ``/pull/<n>#issuecomment-<id>``);
* per-issue comments: ``since`` only, always in creation order;
* ``Link: rel="next"`` absolute URLs; a deleted comment simply disappears.

It also implements ``conformance.TrackerWorld`` so the shared cases can seed it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from urllib.parse import parse_qs, unquote, urlencode

import httpx
from tracker_contracts import CommentKey, IssueKey, IssueState

OWNER, REPO = "acme", "widgets"
API = "https://api.github.com"
WEB = "https://github.com"
ROOT = f"/repos/{OWNER}/{REPO}"
ME = {"login": "maintainer", "id": 42, "type": "User"}
_APPS = {"ci-app": {"id": 77, "slug": "ci-app", "name": "CI app"}}


def iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def _user(name: str, *, bot: bool = False) -> dict[str, Any]:
    if bot:
        return {"login": f"{name}[bot]", "id": 9000 + len(name), "type": "Bot"}
    return {"login": name, "id": 100 + len(name), "type": "User"}


@dataclass
class FakeGitHubREST:
    issues: list[dict[str, Any]] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    requests: list[httpx.Request] = field(default_factory=list)
    sleeps: list[float] = field(default_factory=list)
    repo: dict[str, Any] = field(
        default_factory=lambda: {
            "full_name": f"{OWNER}/{REPO}",
            "private": False,
            "archived": False,
            "disabled": False,
            "has_issues": True,
            "permissions": {"admin": False, "maintain": False, "push": True, "triage": True},
        }
    )
    scopes: str | None = "repo"  # None models a fine-grained token (no X-OAuth-Scopes header)
    throttled: int = 0
    retry_after: float = 0.0
    ambiguous_next_write: bool = False
    write_time: datetime | None = None
    _ids: int = 0

    def sleep(self, seconds: float) -> None:  # the adapter's injected sleep: never blocks
        self.sleeps.append(seconds)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)

    # -- TrackerWorld -------------------------------------------------------------------------

    def _issue_row(self, number: int) -> dict[str, Any]:
        return next(i for i in self.issues if i["number"] == number)

    def _key(self, row: dict[str, Any]) -> IssueKey:
        return IssueKey(f"#{row['number']}", row["html_url"])

    def add_issue(self, title, *, at, state=IssueState.OPEN, labels=()) -> IssueKey:
        self._ids += 1
        n = self._ids
        row = {
            "id": 1000 + n,
            "node_id": f"I_{n}",
            "number": n,
            "html_url": f"{WEB}/{OWNER}/{REPO}/issues/{n}",
            "title": title,
            "body": f"body of {n}",
            "state": state.value,
            "state_reason": "completed" if state is IssueState.CLOSED else None,
            "labels": [{"name": label} for label in labels],
            "user": _user("reporter"),
            "created_at": iso(at),
            "updated_at": iso(at),
            "closed_at": iso(at) if state is IssueState.CLOSED else None,
        }
        self.issues.append(row)
        return self._key(row)

    def update_issue(self, key, *, at, state=None) -> None:
        row = self._issue_row(int(key.identifier[1:]))
        row["updated_at"] = iso(at)
        if state is not None:
            row["state"] = state.value
            row["closed_at"] = iso(at) if state is IssueState.CLOSED else None

    def add_change_request(self, title, *, at) -> IssueKey:
        key = self.add_issue(title, at=at)
        row = self._issue_row(int(key.identifier[1:]))
        row["html_url"] = row["html_url"].replace("/issues/", "/pull/")
        row["pull_request"] = {"url": f"{API}{ROOT}/pulls/{row['number']}"}
        return self._key(row)

    def _comment_row(self, number: int, body: str, user, at, app=None) -> dict[str, Any]:
        self._ids += 1
        cid = 500 + self._ids
        parent = self._issue_row(number)["html_url"]
        return {
            "id": cid,
            "node_id": f"IC_{cid}",
            "html_url": f"{parent}#issuecomment-{cid}",
            "issue_url": f"{API}{ROOT}/issues/{number}",
            "body": body,
            "user": user,
            "performed_via_github_app": app,
            "author_association": "NONE",
            "created_at": iso(at),
            "updated_at": iso(at),
        }

    def add_comment(self, issue, body, *, at, author="alice", integration=None) -> CommentKey:
        app = _APPS.get(integration) if integration else None
        user = _user(author, bot=integration is not None)
        row = self._comment_row(int(issue.identifier[1:]), body, user, at, app)
        self.comments.append(row)
        return CommentKey(str(row["id"]))

    def edit_comment(self, key, body, *, at) -> None:
        row = next(c for c in self.comments if str(c["id"]) == key.id)
        row["body"], row["updated_at"] = body, iso(at)

    def delete_comment(self, key) -> None:
        self.comments = [c for c in self.comments if str(c["id"]) != key.id]

    def add_human_label(self, issue, label) -> None:
        self._issue_row(int(issue.identifier[1:]))["labels"].append({"name": label})

    def server_labels(self, issue) -> frozenset[str]:
        row = self._issue_row(int(issue.identifier[1:]))
        return frozenset(label["name"] for label in row["labels"])

    def comment_count(self, issue) -> int:
        url = f"{API}{ROOT}/issues/{issue.identifier[1:]}"
        return sum(1 for c in self.comments if c["issue_url"] == url)

    def throttle(self, requests, retry_after_seconds) -> None:
        self.throttled, self.retry_after = requests, retry_after_seconds

    def fail_next_write_ambiguously(self) -> None:
        self.ambiguous_next_write = True

    def write_requests(self) -> int:
        return sum(1 for r in self.requests if r.method != "GET")

    def set_write_time(self, at) -> None:
        self.write_time = at

    # -- HTTP ---------------------------------------------------------------------------------

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.throttled:
            self.throttled -= 1
            return httpx.Response(
                403,
                json={"message": "You have exceeded a secondary rate limit."},
                headers={"retry-after": str(int(self.retry_after))},
            )
        path = unquote(request.url.path)
        query = {k: v[-1] for k, v in parse_qs(request.url.query.decode()).items()}
        headers = {"x-oauth-scopes": self.scopes} if self.scopes is not None else {}
        if request.method == "GET" and path == "/user":
            return httpx.Response(200, json=ME, headers=headers)
        if request.method == "GET" and path == ROOT:
            return httpx.Response(200, json=self.repo, headers=headers)
        if request.method == "GET" and path == f"{ROOT}/issues":
            return self._list_issues(request, query)
        if request.method == "GET" and path == f"{ROOT}/issues/comments":
            rows = list(self.comments)
            return self._list(request, rows, query, sort_default="created", dir_default="asc")
        parts = path.removeprefix(f"{ROOT}/issues/").split("/")
        if not parts[0].isdigit() or not any(i["number"] == int(parts[0]) for i in self.issues):
            return httpx.Response(404, json={"message": "Not Found"})
        number = int(parts[0])
        row = self._issue_row(number)
        if parts[1:] == [] and request.method == "GET":
            return httpx.Response(200, json=row)
        if parts[1:] == ["comments"] and request.method == "GET":
            url = f"{API}{ROOT}/issues/{number}"
            rows = [c for c in self.comments if c["issue_url"] == url]
            return self._list(request, rows, {**query, "sort": "created", "direction": "asc"})
        if parts[1:] == ["comments"] and request.method == "POST":
            return self._post_comment(number, json.loads(request.content))
        if parts[1:] == ["labels"] and request.method == "POST":
            names = json.loads(request.content)["labels"]
            current = [label["name"] for label in row["labels"]]
            row["labels"] += [{"name": n} for n in names if n not in current]
            return httpx.Response(200, json=row["labels"])
        if len(parts) == 3 and parts[1] == "labels" and request.method == "DELETE":
            if not any(label["name"] == parts[2] for label in row["labels"]):
                return httpx.Response(404, json={"message": "Label does not exist"})
            row["labels"] = [label for label in row["labels"] if label["name"] != parts[2]]
            return httpx.Response(200, json=row["labels"])
        return httpx.Response(404, json={"message": "Not Found"})

    def _post_comment(self, number: int, payload: dict[str, Any]) -> httpx.Response:
        assert self.write_time is not None, "a test must set the server's write time"
        row = self._comment_row(number, payload["body"], ME, self.write_time)
        self.comments.append(row)
        if self.ambiguous_next_write:  # applied, then the gateway failed
            self.ambiguous_next_write = False
            return httpx.Response(502, json={"message": "Bad Gateway"})
        return httpx.Response(201, json=row)

    def _list_issues(self, request: httpx.Request, query: dict[str, str]) -> httpx.Response:
        state = query.get("state", "open")
        rows = [i for i in self.issues if state == "all" or i["state"] == state]
        return self._list(request, rows, query, sort_default="created", dir_default="desc")

    def _list(
        self,
        request: httpx.Request,
        rows: list[dict[str, Any]],
        query: dict[str, str],
        *,
        sort_default: str = "created",
        dir_default: str = "asc",
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
        headers = {"content-type": "application/json"}
        if page * per_page < len(rows):
            params = {**query, "per_page": str(per_page), "page": str(page + 1)}
            headers["link"] = f'<{API}{request.url.path}?{urlencode(params)}>; rel="next"'
        return httpx.Response(200, json=window, headers=headers)
