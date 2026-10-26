"""GitHub REST JSON to port records: everything GitHub-shaped stops here.

Ported from the ``bh-gj1hb`` prototype mapping (``github_adapter.py``) onto the decoded JSON the
githubkit transport returns. Pure: no I/O and no clock.

* An issue's key is ``IssueKey("#<number>", html_url)``; ``id`` and ``node_id`` ride along as
  the optional internal and global ids (they do not take part in equality).
* An actor is keyed ``(numeric id, kind)``: ``User`` is human, ``Bot`` is a bot, anything else
  (``Organization``, ``Mannequin``) is unknown. The ``login`` is display only and never
  compared, and an id that is not an integer is refused rather than guessed at.
* A comment names its parent issue only through its ``html_url``
  (``<issue url>#issuecomment-<id>``), so the parent key is derived from it; that works unchanged
  on a GHES host. The own-message marker is decoded out of the body (:mod:`.marker`).
* ``state_reason`` is passed through verbatim as ``closed_reason``.
* Pull requests are recognised (``pull_request`` on an issue row, ``/pull/`` in a comment's
  ``html_url``) so the adapter can drop them; they are never work items.

A row this module cannot read raises :class:`PayloadUnreadable` (a ``TrackerError``) naming only
the field, never its value.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from typing import Any

from beadhive.modules.tracker.contracts import (
    Actor,
    ActorKey,
    ActorKind,
    CommentKey,
    IntegrationRef,
    IssueKey,
    IssueState,
    Revision,
    TrackerComment,
    TrackerError,
    TrackerIssue,
)

from .marker import decode_marker

__all__ = [
    "PayloadUnreadable",
    "actor",
    "comment",
    "format_time",
    "is_change_request",
    "is_change_request_comment",
    "issue",
    "issue_key",
    "parent_issue",
    "parse_time",
    "updated_at",
]

_KINDS = {"User": ActorKind.HUMAN, "Bot": ActorKind.BOT}
_PULL_COMMENT = re.compile(r"/pull/[0-9]+/?(?:#|$)")


class PayloadUnreadable(TrackerError):
    """GitHub returned a row this adapter cannot map (never retried; names the field only)."""


def parse_time(stamp: Any, field: str = "timestamp") -> datetime:
    if not isinstance(stamp, str):
        raise PayloadUnreadable(f"GitHub returned no {field}")
    try:
        moment = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        raise PayloadUnreadable(f"GitHub returned an unreadable {field}") from None
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def updated_at(row: Any) -> datetime:
    """A row's ``updated_at``: the tracker's own revision clock, never the local one."""
    return parse_time(_field(row, "updated_at"), "updated_at")


def format_time(moment: datetime) -> str:
    """GitHub's ISO 8601 form (``2026-01-02T03:04:05Z``), the one ``since`` accepts."""
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _field(row: Any, name: str) -> Any:
    if not isinstance(row, dict) or name not in row:
        raise PayloadUnreadable(f"GitHub returned a row without {name}")
    return row[name]


def actor(user: Any) -> Actor | None:
    """``(id, kind)`` identity; ``None`` for a missing (deleted, "ghost") author."""
    if user is None:
        return None
    ident = _field(user, "id")
    if not isinstance(ident, int) or isinstance(ident, bool):
        raise PayloadUnreadable("GitHub returned an actor without a numeric id")
    kind = _KINDS.get(user.get("type") if isinstance(user.get("type"), str) else "")
    login = user.get("login")
    return Actor(
        ActorKey(str(ident), kind or ActorKind.UNKNOWN),
        display=login if isinstance(login, str) else "",
    )


def _number(row: Any) -> int:
    number = _field(row, "number")
    if not isinstance(number, int) or isinstance(number, bool) or number < 1:
        raise PayloadUnreadable("GitHub returned an issue without a number")
    return number


def _text(row: dict[str, Any], name: str) -> str:
    value = row.get(name)
    return value if isinstance(value, str) else ""


def _labels(row: dict[str, Any]) -> frozenset[str]:
    names: set[str] = set()
    for label in row.get("labels") or ():
        if isinstance(label, str):
            names.add(label)
        elif isinstance(label, dict) and isinstance(label.get("name"), str):
            names.add(label["name"])
    return frozenset(names)


def is_change_request(row: Any) -> bool:
    """An issues-endpoint row that is really a pull request."""
    return isinstance(row, dict) and row.get("pull_request") is not None


def is_change_request_comment(row: Any) -> bool:
    """A comment on a pull request (``.../pull/<n>#issuecomment-<id>``), never on an issue,
    even in a repository or under an owner that is itself named ``pull``."""
    url = row.get("html_url") if isinstance(row, dict) else None
    return isinstance(url, str) and _PULL_COMMENT.search(url) is not None


def issue_key(row: Any) -> IssueKey:
    url = _field(row, "html_url")
    if not isinstance(url, str) or not url:
        raise PayloadUnreadable("GitHub returned an issue without html_url")
    ident = row.get("id")
    node = row.get("node_id")
    return IssueKey(
        f"#{_number(row)}",
        url,
        internal_id=str(ident) if isinstance(ident, int) else None,
        global_id=node if isinstance(node, str) else None,
    )


def issue(row: Any) -> TrackerIssue:
    key = issue_key(row)
    state = _field(row, "state")
    if state not in ("open", "closed"):
        raise PayloadUnreadable("GitHub returned an issue with an unknown state")
    closed_at = row.get("closed_at")
    reason = row.get("state_reason")
    return TrackerIssue(
        key=key,
        title=_text(row, "title"),
        body=_text(row, "body"),
        state=IssueState.CLOSED if state == "closed" else IssueState.OPEN,
        labels=_labels(row),
        author=actor(row.get("user")),
        created_at=parse_time(row.get("created_at"), "created_at"),
        revision=Revision(parse_time(row.get("updated_at"), "updated_at")),
        closed_at=parse_time(closed_at, "closed_at") if closed_at is not None else None,
        closed_reason=reason if isinstance(reason, str) else None,
    )


def parent_issue(row: Any) -> IssueKey:
    """The issue a comment belongs to, from the comment's own ``html_url``."""
    url = _field(row, "html_url")
    if not isinstance(url, str):
        raise PayloadUnreadable("GitHub returned a comment without html_url")
    parent = url.split("#", 1)[0]
    number = parent.rstrip("/").rsplit("/", 1)[-1]
    if not number.isdigit():
        raise PayloadUnreadable("GitHub returned a comment without a parent issue")
    return IssueKey(f"#{int(number)}", parent)


def comment(row: Any) -> TrackerComment:
    ident = _field(row, "id")
    if not isinstance(ident, int) or isinstance(ident, bool):
        raise PayloadUnreadable("GitHub returned a comment without a numeric id")
    body, marker = decode_marker(_text(row, "body"))
    app = row.get("performed_via_github_app")
    via = None
    if isinstance(app, dict) and isinstance(app.get("id"), int):
        slug = app.get("slug")
        via = IntegrationRef(str(app["id"]), slug if isinstance(slug, str) else "")
    node = row.get("node_id")
    return TrackerComment(
        key=CommentKey(str(ident), global_id=node if isinstance(node, str) else None),
        issue=parent_issue(row),
        author=actor(row.get("user")),
        body=body,
        created_at=parse_time(row.get("created_at"), "created_at"),
        revision=Revision(parse_time(row.get("updated_at"), "updated_at")),
        via=via,
        marker=marker,
    )
