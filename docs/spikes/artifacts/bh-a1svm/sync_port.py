"""Transport-neutral shape of the pull side of a GitHub tracker adapter (spike bh-a1svm).

The adapter's domain logic needs three read operations. Everything below the ``GitHubTransport``
protocol (httpx, githubkit, ``gh api``) is interchangeable; everything above it never sees a
transport type - that is the one-way dependency rule the recommendation relies on.

Two GitHub behaviours shape the logic here, both reproduced by ``fake_github.FakeGitHub``:

* ``since`` is an inclusive ``updated_at >= since`` filter, so the checkpoint is the maximum
  ``updated_at`` already seen and the boundary item is fetched again (upserts must be idempotent);
* ``since`` cannot report a deleted comment, so deletions are found only by comparing the
  complete id set (``reconcile_comment_ids``) against the mirror.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol


@dataclass(frozen=True)
class IssueRec:
    number: int
    state: str
    title: str
    updated_at: datetime
    is_pull_request: bool


@dataclass(frozen=True)
class CommentRec:
    id: int
    issue_number: int
    updated_at: datetime


class GitHubTransport(Protocol):
    def list_issues(self, *, since: datetime | None) -> list[IssueRec]:
        """Issues INCLUDING closed (``state=all``), following every ``Link: rel=next``."""
        ...

    def list_comments(self, *, since: datetime | None) -> list[CommentRec]:
        """Repo-wide issue comments, following every ``Link: rel=next``."""
        ...


@dataclass
class Mirror:
    issues: dict[int, IssueRec] = field(default_factory=dict)
    comments: dict[int, CommentRec] = field(default_factory=dict)
    issues_checkpoint: datetime | None = None
    comments_checkpoint: datetime | None = None


def pull(transport: GitHubTransport, mirror: Mirror) -> None:
    """Incremental pull: fetch everything updated since each checkpoint, then advance it."""
    for issue in transport.list_issues(since=mirror.issues_checkpoint):
        if issue.is_pull_request:  # the issues endpoint also lists PRs; they are not issues
            continue
        mirror.issues[issue.number] = issue
        mirror.issues_checkpoint = max(
            mirror.issues_checkpoint or issue.updated_at, issue.updated_at
        )
    for comment in transport.list_comments(since=mirror.comments_checkpoint):
        mirror.comments[comment.id] = comment
        mirror.comments_checkpoint = max(
            mirror.comments_checkpoint or comment.updated_at, comment.updated_at
        )


def reconcile_comment_ids(transport: GitHubTransport, mirror: Mirror) -> set[int]:
    """Full-id-set pass (no ``since``): drop mirrored comments GitHub no longer has."""
    live = {c.id for c in transport.list_comments(since=None)}
    gone = set(mirror.comments) - live
    for comment_id in gone:
        del mirror.comments[comment_id]
    return gone
