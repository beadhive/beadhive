"""The test-only control surface for a tracker's server-side state, and its in-memory model.

:class:`TrackerWorld` is what a conformance case uses to seed, edit and delete server state and
to observe what the adapter sent. It is never part of the port: consumers cannot reach it
through an adapter. A real adapter's test suite supplies its own world (an offline stand-in of
the tracker's API); :class:`MemoryWorld` is the world behind
:class:`~beadhive.testing.tracker.memory.MemoryTracker`.

No clock: every timestamp is an explicit ``at=`` value, the server's write time is set with
``set_write_time``, and a retry wait is recorded by ``sleep`` without blocking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from beadhive.modules.tracker.contracts import (
    Actor,
    ActorKey,
    ActorKind,
    CommentKey,
    IntegrationRef,
    IssueKey,
    IssueState,
    OwnMarker,
)


def at(day: int, hour: int = 0) -> datetime:
    """An explicit, timezone-aware timestamp for scenarios (January 2026, UTC)."""
    return datetime(2026, 1, day, hour, tzinfo=UTC)


class TrackerWorld(Protocol):
    """Server-side state and observations every conformance subject's world provides."""

    sleeps: list[float]

    def add_issue(
        self,
        title: str,
        *,
        at: datetime,
        state: IssueState = IssueState.OPEN,
        labels: tuple[str, ...] = (),
    ) -> IssueKey: ...

    def update_issue(
        self, key: IssueKey, *, at: datetime, state: IssueState | None = None
    ) -> None: ...

    def delete_issue(self, key: IssueKey) -> None: ...

    def add_change_request(self, title: str, *, at: datetime) -> IssueKey: ...

    def add_comment(
        self,
        issue: IssueKey,
        body: str,
        *,
        at: datetime,
        author: str = "alice",
        integration: str | None = None,
    ) -> CommentKey: ...

    def edit_comment(self, key: CommentKey, body: str, *, at: datetime) -> None: ...

    def delete_comment(self, key: CommentKey) -> None: ...

    def add_human_label(self, issue: IssueKey, label: str) -> None: ...

    def server_labels(self, issue: IssueKey) -> frozenset[str]: ...

    def comment_count(self, issue: IssueKey) -> int: ...

    def throttle(self, requests: int, retry_after_seconds: float) -> None: ...

    def fail_next_write_ambiguously(self, applied: bool = True) -> None:
        """Make the next write raise ``WriteOutcomeUnknown``. With ``applied=True`` (the default)
        the tracker stores the write first ("applied, but the caller cannot know"); with
        ``applied=False`` the request is sent and cut off before anything is stored (kit
        extension K1)."""
        ...

    def write_requests(self) -> int: ...

    def read_requests(self) -> int: ...

    def set_write_time(self, at: datetime) -> None: ...

    def url_variants(self, key: IssueKey) -> tuple[str, ...]:
        """Pasted spellings of ``key``'s URL that must resolve to ``key.url`` (a trailing
        slash, a query, a fragment, and any tracker-specific equivalents)."""
        ...

    def foreign_issue_url(self) -> str:
        """An issue URL in the adapter's grammar that names another project."""
        ...

    def unrecognized_url(self) -> str:
        """A URL the adapter's grammar does not recognise as an issue."""
        ...


@dataclass
class _Issue:
    key: IssueKey
    title: str
    state: IssueState
    labels: set[str]
    created: datetime
    updated: datetime
    change_request: bool = False


@dataclass
class _Comment:
    key: CommentKey
    issue: IssueKey
    body: str
    author: Actor
    created: datetime
    updated: datetime
    via: IntegrationRef | None = None
    marker: OwnMarker | None = None


@dataclass
class MemoryWorld:
    """In-memory server state for :class:`~beadhive.testing.tracker.memory.MemoryTracker`."""

    locator: str = "acme/widgets"
    issues: dict[str, _Issue] = field(default_factory=dict)
    comments: dict[str, _Comment] = field(default_factory=dict)
    sleeps: list[float] = field(default_factory=list)
    writes: int = 0
    reads: int = 0
    throttled: int = 0
    retry_after: float = 0.0
    ambiguous_next_write: bool | None = None  # None: off; True: applied; False: not applied
    write_time: datetime | None = None
    _ids: int = 0

    def _next(self) -> int:
        self._ids += 1
        return self._ids

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)

    @staticmethod
    def actor(name: str, *, bot: bool = False) -> Actor:
        kind = ActorKind.BOT if bot else ActorKind.HUMAN
        return Actor(ActorKey(f"{kind.value}-{name}", kind), display=name)

    def issue_url(self, number: int, *, change_request: bool = False) -> str:
        kind = "changes" if change_request else "issues"
        return f"memory://{self.locator}/{kind}/{number}"

    # -- TrackerWorld -------------------------------------------------------------------------

    def add_issue(
        self,
        title: str,
        *,
        at: datetime,
        state: IssueState = IssueState.OPEN,
        labels: tuple[str, ...] = (),
        change_request: bool = False,
    ) -> IssueKey:
        n = self._next()
        url = self.issue_url(n, change_request=change_request)
        key = IssueKey(f"#{n}", url, internal_id=str(n))
        self.issues[key.identifier] = _Issue(
            key, title, state, set(labels), at, at, change_request=change_request
        )
        return key

    def update_issue(self, key: IssueKey, *, at: datetime, state: IssueState | None = None) -> None:
        issue = self.issues[key.identifier]
        issue.updated = at
        issue.state = state or issue.state

    def delete_issue(self, key: IssueKey) -> None:
        del self.issues[key.identifier]
        for comment_id in [c.key.id for c in self.comments.values() if c.issue == key]:
            del self.comments[comment_id]

    def add_change_request(self, title: str, *, at: datetime) -> IssueKey:
        return self.add_issue(title, at=at, change_request=True)

    def add_comment(
        self,
        issue: IssueKey,
        body: str,
        *,
        at: datetime,
        author: str = "alice",
        integration: str | None = None,
    ) -> CommentKey:
        key = CommentKey(f"c{self._next()}")
        via = IntegrationRef(integration, integration) if integration else None
        who = self.actor(author, bot=integration is not None)
        self.comments[key.id] = _Comment(key, issue, body, who, at, at, via=via)
        return key

    def edit_comment(self, key: CommentKey, body: str, *, at: datetime) -> None:
        self.comments[key.id].body = body
        self.comments[key.id].updated = at

    def delete_comment(self, key: CommentKey) -> None:
        del self.comments[key.id]

    def add_human_label(self, issue: IssueKey, label: str) -> None:
        self.issues[issue.identifier].labels.add(label)

    def server_labels(self, issue: IssueKey) -> frozenset[str]:
        return frozenset(self.issues[issue.identifier].labels)

    def comment_count(self, issue: IssueKey) -> int:
        return sum(1 for c in self.comments.values() if c.issue == issue)

    def throttle(self, requests: int, retry_after_seconds: float) -> None:
        self.throttled, self.retry_after = requests, retry_after_seconds

    def fail_next_write_ambiguously(self, applied: bool = True) -> None:
        self.ambiguous_next_write = applied

    def write_requests(self) -> int:
        return self.writes

    def read_requests(self) -> int:
        return self.reads

    def set_write_time(self, at: datetime) -> None:
        self.write_time = at

    def url_variants(self, key: IssueKey) -> tuple[str, ...]:
        return (f"{key.url}/", f"{key.url}?view=full", f"{key.url}#c1", f"{key.url}/?a=b#c")

    def foreign_issue_url(self) -> str:
        return "memory://someone-else/elsewhere/issues/1"

    def unrecognized_url(self) -> str:
        return f"memory://{self.locator}/wiki/home"


__all__ = ["MemoryWorld", "TrackerWorld", "at"]
