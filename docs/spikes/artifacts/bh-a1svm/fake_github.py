"""Offline GitHub REST stand-in for spike bh-a1svm. No network, no writes, no real clock.

``FakeGitHub.handler`` is an ``httpx.MockTransport`` handler that reproduces the parts of the
REST contract the tracker adapter depends on, each checked against the documented behaviour of
``GET /repos/{owner}/{repo}/issues`` and ``GET /repos/{owner}/{repo}/issues/comments``:

* ``state`` defaults to ``open`` (a closed issue is invisible unless ``state=all`` is sent);
* ``since`` is an inclusive ``updated_at >= since`` filter on both endpoints;
* ``per_page`` / ``page`` pagination, with an RFC 8288 ``Link`` header carrying absolute
  ``rel="next"`` / ``"last"`` URLs (the client must follow ``next``, never compute page numbers);
* the issues endpoint also returns pull requests (marked by a ``pull_request`` key);
* a deleted comment simply stops appearing - ``since`` can never report a deletion;
* a weak ``ETag`` (as GitHub serves) plus ``If-None-Match`` -> ``304``, for HTTP-cache probes;
* scripted one-shot failures (``script``) for retry-decision probes.

Payloads are full-fidelity enough for a typed SDK to validate them (githubkit parses the
responses into pydantic models), which is itself part of the evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import parse_qs, urlencode

import httpx

OWNER = "acme"
REPO = "widgets"
API = "https://api.github.com"
TOKEN = "ghp_not_a_real_token"  # a placeholder: no credential ever appears in this spike
_ISSUES_PATH = f"/repos/{OWNER}/{REPO}/issues"
_COMMENTS_PATH = f"/repos/{OWNER}/{REPO}/issues/comments"

_USER = {
    "login": "octo",
    "id": 1,
    "node_id": "U_1",
    "avatar_url": f"{API}/avatars/1",
    "gravatar_id": "",
    "url": f"{API}/users/octo",
    "html_url": "https://github.com/octo",
    "followers_url": f"{API}/users/octo/followers",
    "following_url": f"{API}/users/octo/following{{/other_user}}",
    "gists_url": f"{API}/users/octo/gists{{/gist_id}}",
    "starred_url": f"{API}/users/octo/starred{{/owner}}{{/repo}}",
    "subscriptions_url": f"{API}/users/octo/subscriptions",
    "organizations_url": f"{API}/users/octo/orgs",
    "repos_url": f"{API}/users/octo/repos",
    "events_url": f"{API}/users/octo/events{{/privacy}}",
    "received_events_url": f"{API}/users/octo/received_events",
    "type": "User",
    "site_admin": False,
}


def ts(day: int, hour: int = 0) -> str:
    """A fixed ISO-8601 UTC timestamp in January 2026 (``day`` 1..31)."""
    return datetime(2026, 1, day, hour, tzinfo=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def make_issue(
    number: int,
    *,
    state: str = "open",
    updated: str,
    created: str | None = None,
    is_pr: bool = False,
    title: str | None = None,
) -> dict[str, Any]:
    url = f"{API}{_ISSUES_PATH}/{number}"
    issue: dict[str, Any] = {
        "id": 1000 + number,
        "node_id": f"I_{number}",
        "url": url,
        "repository_url": f"{API}/repos/{OWNER}/{REPO}",
        "labels_url": f"{url}/labels{{/name}}",
        "comments_url": f"{url}/comments",
        "events_url": f"{url}/events",
        "html_url": f"https://github.com/{OWNER}/{REPO}/issues/{number}",
        "number": number,
        "state": state,
        "title": title or f"issue {number}",
        "body": f"body of {number}",
        "user": _USER,
        "labels": [],
        "assignee": None,
        "assignees": [],
        "milestone": None,
        "locked": False,
        "comments": 0,
        "closed_at": updated if state == "closed" else None,
        "created_at": created or updated,
        "updated_at": updated,
        "author_association": "OWNER",
    }
    if is_pr:
        issue["pull_request"] = {
            "url": f"{API}/repos/{OWNER}/{REPO}/pulls/{number}",
            "html_url": f"https://github.com/{OWNER}/{REPO}/pull/{number}",
            "diff_url": f"https://github.com/{OWNER}/{REPO}/pull/{number}.diff",
            "patch_url": f"https://github.com/{OWNER}/{REPO}/pull/{number}.patch",
        }
    return issue


def make_comment(comment_id: int, issue_number: int, *, updated: str) -> dict[str, Any]:
    return {
        "id": comment_id,
        "node_id": f"IC_{comment_id}",
        "url": f"{API}{_COMMENTS_PATH}/{comment_id}",
        "html_url": f"https://github.com/{OWNER}/{REPO}/issues/{issue_number}#c{comment_id}",
        "body": f"comment {comment_id}",
        "user": _USER,
        "created_at": updated,
        "updated_at": updated,
        "issue_url": f"{API}{_ISSUES_PATH}/{issue_number}",
        "author_association": "OWNER",
    }


def standard_issues() -> list[dict[str, Any]]:
    """Five items: open, closed, a closed PR, open, closed. Newest update is #5 (day 20)."""
    return [
        make_issue(1, state="open", updated=ts(2)),
        make_issue(2, state="closed", updated=ts(3)),
        make_issue(3, state="closed", updated=ts(10), is_pr=True),
        make_issue(4, state="open", updated=ts(15)),
        make_issue(5, state="closed", updated=ts(20)),
    ]


def standard_comments() -> list[dict[str, Any]]:
    """Five comments across issues, updated on days 2, 4, 11, 16, 21."""
    return [
        make_comment(501, 1, updated=ts(2)),
        make_comment(502, 2, updated=ts(4)),
        make_comment(503, 3, updated=ts(11)),
        make_comment(504, 4, updated=ts(16)),
        make_comment(505, 5, updated=ts(21)),
    ]


@dataclass
class FakeGitHub:
    issues: list[dict[str, Any]] = field(default_factory=standard_issues)
    comments: list[dict[str, Any]] = field(default_factory=standard_comments)
    # Each entry builds the response for the NEXT request; consumed one per request, in order.
    script: deque[Callable[[httpx.Request], httpx.Response]] = field(default_factory=deque)
    requests: list[httpx.Request] = field(default_factory=list)

    def delete_comment(self, comment_id: int) -> None:
        self.comments = [c for c in self.comments if c["id"] != comment_id]

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.script:
            return self.script.popleft()(request)
        if request.method != "GET":  # the spike is read-only; a write is a bug in the probe
            raise AssertionError(f"unexpected non-GET request: {request.method} {request.url}")
        query = {k: v[-1] for k, v in parse_qs(request.url.query.decode()).items()}
        if request.url.path == _ISSUES_PATH:
            state = query.get("state", "open")
            rows = [i for i in self.issues if state == "all" or i["state"] == state]
        elif request.url.path == _COMMENTS_PATH:
            rows = list(self.comments)
        else:
            return httpx.Response(404, json={"message": "Not Found"})
        if since := query.get("since"):
            rows = [r for r in rows if _parse(r["updated_at"]) >= _parse(since)]
        rows.sort(key=lambda r: (r["created_at"], r["id"]))
        return self._page(request, rows, query)

    def _page(self, request: httpx.Request, rows: list, query: dict[str, str]) -> httpx.Response:
        per_page = min(int(query.get("per_page", "30")), 100)
        page = int(query.get("page", "1"))
        last = max(1, -(-len(rows) // per_page))
        window = rows[(page - 1) * per_page : page * per_page]
        links = []
        for rel, target in (("next", page + 1), ("last", last)):
            if rel == "next" and page >= last:
                continue
            params = {**query, "per_page": str(per_page), "page": str(target)}
            links.append(f'<{API}{request.url.path}?{urlencode(params)}>; rel="{rel}"')
        body = json.dumps(window, sort_keys=True).encode()
        etag = 'W/"' + hashlib.sha256(body).hexdigest()[:16] + '"'  # GitHub serves weak ETags
        headers = {"content-type": "application/json; charset=utf-8", "etag": etag}
        if links:
            headers["link"] = ", ".join(links)
        if request.headers.get("if-none-match") == etag:
            return httpx.Response(304, headers={"etag": etag})
        return httpx.Response(200, content=body, headers=headers)

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handler)


def _parse(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def failure(status: int, *, headers: dict[str, str] | None = None, message: str = "boom"):
    """Script entry: answer the next request with ``status`` and a GitHub-shaped error body."""
    return lambda _req: httpx.Response(status, json={"message": message}, headers=headers or {})


class FakeClock:
    """Deterministic clock + sleep. ``sleep`` records and advances; it never blocks."""

    def __init__(self, start: float = 0.0) -> None:
        self.now = start
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds
