"""Candidate A: githubkit transport for spike bh-a1svm.

Imports ``githubkit`` and therefore lives ONLY under docs/spikes/artifacts/ - it is never
imported by ``src/``. The point of the spike is how little glue the SDK needs and how its two
risky built-ins (retry, HTTP cache) are controlled:

* ``auto_retry`` is ``False`` | ``True`` | a pure ``(exception, retry_count) -> RetryOption``
  decision function; the *sleep* that follows a positive decision is hard-wired
  (``time.sleep`` / ``anyio.sleep`` inside ``githubkit.core``), so tests either disable retry or
  patch that one module attribute - see ``patch_sleep``.
* ``http_cache`` is a boolean; ``False`` builds a plain ``httpx.Client`` with no hishel layer.
* ``transport`` / ``async_transport`` inject any ``httpx`` transport (``MockTransport`` here).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from types import SimpleNamespace

import httpx
from githubkit import GitHub
from githubkit.typing import RetryDecisionFunc
from sync_port import CommentRec, IssueRec


def build_client(
    token: str,
    *,
    transport: httpx.BaseTransport | None = None,
    base_url: str = "https://api.github.com",
    auto_retry: bool | RetryDecisionFunc = True,
    http_cache: bool = False,
) -> GitHub:
    """Defaults here are the spike's recommendation: cache OFF until a bead opts in."""
    return GitHub(
        token,
        base_url=base_url,
        transport=transport,
        auto_retry=auto_retry,
        http_cache=http_cache,
    )


class GithubkitTransport:
    def __init__(self, gh: GitHub, owner: str, repo: str, *, per_page: int = 100) -> None:
        self._gh = gh
        self._owner = owner
        self._repo = repo
        self._per_page = per_page

    def list_issues(self, *, since: datetime | None) -> list[IssueRec]:
        rest = self._gh.rest
        pages = rest.paginate(
            rest.issues.list_for_repo,
            owner=self._owner,
            repo=self._repo,
            state="all",
            per_page=self._per_page,
            **({"since": since} if since else {}),
        )
        return [
            IssueRec(
                number=i.number,
                state=i.state,
                title=i.title,
                updated_at=i.updated_at,
                is_pull_request=bool(i.pull_request),
            )
            for i in pages
        ]

    def list_comments(self, *, since: datetime | None) -> list[CommentRec]:
        rest = self._gh.rest
        pages = rest.paginate(
            rest.issues.list_comments_for_repo,
            owner=self._owner,
            repo=self._repo,
            per_page=self._per_page,
            **({"since": since} if since else {}),
        )
        return [
            CommentRec(
                id=c.id,
                issue_number=int(c.issue_url.rsplit("/", 1)[1]),
                updated_at=c.updated_at,
            )
            for c in pages
        ]


@contextmanager
def patch_sleep(sleep: Callable[[float], None]) -> Iterator[None]:
    """Route githubkit's retry sleep to ``sleep`` (sync path) for the duration of the block.

    ``githubkit.core`` does ``import time`` and calls ``time.sleep(...)``; replacing the module
    attribute ``githubkit.core.time`` with a namespace affects only that module - the real
    ``time`` module, and every other importer of it, are untouched.
    """
    import githubkit.core as core

    original = core.time
    core.time = SimpleNamespace(sleep=sleep)  # type: ignore[assignment]
    try:
        yield
    finally:
        core.time = original  # type: ignore[assignment]
