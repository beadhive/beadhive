"""Candidate B: plain ``httpx`` transport (no new dependency) for spike bh-a1svm.

This is what "hand-written pagination, rate-limit and ETag handling" costs. It is deliberately
complete enough to be a fair comparison: ``Link`` parsing, ``state=all``, ``since``, a retry
policy and a conditional-request cache. The clock (``now``) and ``sleep`` are constructor
arguments, so a test drives retry timing with ``fake_github.FakeClock`` and nothing ever blocks.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx
from sync_port import CommentRec, IssueRec

_NEXT = re.compile(r'<([^<>]+)>\s*;\s*rel="next"')


def parse_ts(stamp: str) -> datetime:
    return datetime.fromisoformat(stamp.replace("Z", "+00:00"))


def fmt_ts(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@dataclass(frozen=True)
class RetryPolicy:
    """Pure decision: seconds to wait before retrying this response, or ``None`` for 'do not'."""

    max_attempts: int = 3  # retries after the first try

    def delay(self, response: httpx.Response, attempt: int, now_epoch: float) -> float | None:
        if attempt >= self.max_attempts:
            return None
        if response.status_code >= 500:
            return float((attempt + 1) ** 2)
        if response.status_code in (403, 429):
            if "retry-after" in response.headers:  # secondary rate limit
                return float(response.headers["retry-after"])
            if response.headers.get("x-ratelimit-remaining") == "0":  # primary rate limit
                return max(0.0, float(response.headers["x-ratelimit-reset"]) - now_epoch)
        return None


class HttpxTransport:
    def __init__(
        self,
        client: httpx.Client,
        owner: str,
        repo: str,
        *,
        retry: RetryPolicy | None = None,
        sleep: Callable[[float], None],
        now: Callable[[], float],
        per_page: int = 100,
    ) -> None:
        self._client = client
        self._root = f"/repos/{owner}/{repo}"
        self._retry = retry or RetryPolicy()
        self._sleep = sleep
        self._now = now
        self._per_page = per_page
        self._etags: dict[str, tuple[str, list[Any], str]] = {}  # url -> (etag, page, link)

    def _pages(self, path: str, params: dict[str, str]) -> Iterator[list[dict[str, Any]]]:
        url: str | None = f"{self._root}/{path}"
        query: dict[str, str] | None = {**params, "per_page": str(self._per_page)}
        while url:
            cached = self._etags.get(self._key(url, query))
            headers = {"If-None-Match": cached[0]} if cached else {}
            response = self._client.build_request("GET", url, params=query, headers=headers)
            response = self._send(response)
            if response.status_code == 304 and cached:
                # A 304 has no body and (as modelled by the fake) no Link header either: the
                # cache MUST keep the Link value too, or pagination silently stops after page 1.
                _, page, link = cached
            else:
                page, link = response.json(), response.headers.get("link", "")
                if etag := response.headers.get("etag"):
                    self._etags[self._key(url, query)] = (etag, page, link)
            yield page
            match = _NEXT.search(link)
            url, query = (match.group(1), None) if match else (None, None)  # next URL is complete

    def _send(self, request: httpx.Request) -> httpx.Response:
        attempt = 0
        while True:
            response = self._client.send(request)
            if response.status_code < 400 or response.status_code == 304:
                return response
            delay = self._retry.delay(response, attempt, self._now())
            if delay is None:
                response.raise_for_status()
            self._sleep(delay)
            attempt += 1

    @staticmethod
    def _key(url: str, query: dict[str, str] | None) -> str:
        # NB: httpx.URL(url, params=None) silently DROPS the query string; guard it.
        return str(httpx.URL(url, params=query)) if query else url

    def list_issues(self, *, since: datetime | None) -> list[IssueRec]:
        params = {"state": "all", **({"since": fmt_ts(since)} if since else {})}
        return [
            IssueRec(
                number=row["number"],
                state=row["state"],
                title=row["title"],
                updated_at=parse_ts(row["updated_at"]),
                is_pull_request="pull_request" in row,
            )
            for page in self._pages("issues", params)
            for row in page
        ]

    def list_comments(self, *, since: datetime | None) -> list[CommentRec]:
        params = {**({"since": fmt_ts(since)} if since else {})}
        return [
            CommentRec(
                id=row["id"],
                issue_number=int(row["issue_url"].rsplit("/", 1)[1]),
                updated_at=parse_ts(row["updated_at"]),
            )
            for page in self._pages("issues/comments", params)
            for row in page
        ]
