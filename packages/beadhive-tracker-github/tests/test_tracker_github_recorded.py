"""Recorded-response tests: the read path against fixed GitHub REST exchanges (``recorded/``).

Each fixture is an ordered list of request/response interactions in the documented shape of the
GitHub REST API (version 2022-11-28). They are SYNTHETIC (see each file's ``provenance``): the
constraint is that no test ever makes a live request, so the bodies are hand-assembled from the
documented field set with invented ids and users, never captured with a credential. A replay
transport serves them in order and checks that every request the adapter sent is exactly the
recorded one: method, path and query (order-insensitive), and the server's own ``Link`` URL
followed verbatim.

Covered (acceptance of bh-51fdo, and the re-scoped intake draft item 2):

* closed issues are listed (``state=all``) and a pull request is dropped;
* an issue that closes between two pulls comes back closed from the incremental pull;
* pagination follows ``Link: rel="next"`` verbatim, query string and ``after=`` cursor intact;
* a ``since`` checkpoint is GitHub's own high-water mark, inclusive, in the ``Z`` form;
* comment deletions are found only by id-set reconciliation, never by a ``since`` pull.
"""

from __future__ import annotations

import json
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import httpx
import pytest

from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    ActorKind,
    Checkpoint,
    CommentQuery,
    IdQuery,
    IdSubject,
    IssueQuery,
    IssueState,
    OwnMarker,
    ProjectRef,
)
from beadhive.testing.tracker import assert_contract_only
from beadhive_plugins.credentials import ResolvedToken
from beadhive_tracker_github.adapter import GitHubTrackerAdapter

RECORDED = Path(__file__).resolve().parent / "recorded"
TOKEN = "ghp_" + "Rec0rdedF1xtureT0ken" * 2


def _shape(url: str) -> tuple[str, str, str, tuple[tuple[str, str], ...]]:
    parts = urlsplit(url)
    return parts.scheme, parts.netloc, parts.path, tuple(sorted(parse_qsl(parts.query)))


@dataclass
class Replay:
    """Serve a recording in order; record every request and any mismatch."""

    name: str
    interactions: deque[dict[str, Any]] = field(default_factory=deque)
    sent: list[httpx.Request] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)

    @classmethod
    def load(cls, name: str) -> Replay:
        document = json.loads((RECORDED / name).read_text(encoding="utf-8"))
        assert document["provenance"].startswith("Synthetic"), name
        return cls(name, deque(document["interactions"]))

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.sent.append(request)
        if not self.interactions:
            self.mismatches.append(f"unexpected extra request {request.method} {request.url}")
            return httpx.Response(418, json={"message": "replay exhausted"})
        expected = self.interactions.popleft()
        want = expected["request"]
        if request.method != want["method"] or _shape(str(request.url)) != _shape(want["url"]):
            self.mismatches.append(f"sent {request.method} {request.url}, recorded {want['url']}")
            return httpx.Response(418, json={"message": "replay mismatch"})
        response = expected["response"]
        return httpx.Response(
            response["status"], headers=response["headers"], json=response["json"]
        )

    def assert_done(self) -> None:
        assert self.mismatches == []
        assert not self.interactions, f"{len(self.interactions)} recorded exchanges unused"


def full_ids(tracker: GitHubTrackerAdapter, query: IdQuery) -> frozenset[str]:
    """A full id snapshot, paged to the end (what the application's ``snapshot_ids`` does)."""
    ids: set[str] = set()
    cursor = None
    while True:
        page = tracker.list_ids(IdQuery(query.subject, query.issue, cursor=cursor))
        ids.update(page.ids)
        if page.next_cursor is None:
            return frozenset(ids)
        cursor = page.next_cursor


def adapter(replay: Replay, *, page_size: int | None = None) -> GitHubTrackerAdapter:
    return GitHubTrackerAdapter(
        project=ProjectRef("github", "acme/widgets"),
        token_source=lambda: ResolvedToken(TOKEN, "env:GITHUB_TOKEN"),
        sleep=lambda _seconds: pytest.fail("a recorded exchange never needs a retry"),
        enabled=READ_OPERATIONS,
        page_size=page_size,
        transport=httpx.MockTransport(replay.handler),
    )


def test_closed_issues_are_listed_and_pagination_follows_the_link_verbatim():
    replay = Replay.load("issues_closed_and_paginated.json")
    tracker = adapter(replay, page_size=2)

    first = tracker.list_issues(IssueQuery())
    assert [i.key.identifier for i in first.items] == ["#1"]  # the pull request #2 is dropped
    assert first.checkpoint == Checkpoint("github:2026-01-03T00:00:00Z")  # ... but moves it
    assert first.next_cursor is not None
    assert first.budget is not None and first.budget.remaining == 4987
    second = tracker.list_issues(IssueQuery(cursor=first.next_cursor))
    replay.assert_done()

    assert second.next_cursor is None
    closed = {i.key.identifier: i for i in second.items}
    assert set(closed) == {"#3", "#4"}
    assert all(i.state is IssueState.CLOSED and i.closed_at is not None for i in closed.values())
    assert closed["#3"].closed_reason == "completed"
    assert closed["#4"].closed_reason == "not_planned" and closed["#4"].labels == {"wontfix"}
    assert second.checkpoint == Checkpoint("github:2026-01-05T12:30:00Z")
    # The second request is the server's Link URL byte for byte, after= cursor included.
    assert str(replay.sent[1].url).endswith("&after=Y3Vyc29yOnYyOpLPAAABnHC2jADOq3Z0bg%3D%3D")
    assert replay.sent[0].url.params["state"] == "all"  # GitHub's default would hide closed
    for page in (first, second):
        assert_contract_only(page)


def test_an_issue_that_closes_between_pulls_is_updated_by_the_incremental_pull():
    replay = Replay.load("issue_closes_between_pulls.json")
    tracker = adapter(replay)

    before = tracker.list_issues(IssueQuery())
    (opened,) = before.items
    assert opened.state is IssueState.OPEN and opened.closed_at is None
    after = tracker.list_issues(IssueQuery(since=before.checkpoint))
    replay.assert_done()

    (closed,) = after.items
    assert closed.key == opened.key
    assert closed.state is IssueState.CLOSED and closed.closed_reason == "completed"
    assert closed.closed_at == closed.revision.updated_at
    assert closed.revision > opened.revision
    assert after.checkpoint == Checkpoint("github:2026-02-03T09:30:00Z")
    assert replay.sent[1].url.params["since"] == "2026-02-01T10:00:00Z"  # ISO 8601, Z form


def test_a_since_checkpoint_repeats_the_boundary_shows_edits_and_decodes_the_marker():
    replay = Replay.load("comments_since_checkpoint.json")
    tracker = adapter(replay)

    full = tracker.list_comments(CommentQuery())
    assert [c.key.id for c in full.items] == ["501", "503"]  # pull-request comment 502 dropped
    assert full.checkpoint == Checkpoint("github:2026-02-02T00:00:00Z")
    bot = next(c for c in full.items if c.key.id == "503")
    assert bot.author is not None and bot.author.key.kind is ActorKind.BOT
    assert bot.via is not None and bot.via.id == "77"
    delta = tracker.list_comments(CommentQuery(since=full.checkpoint))
    replay.assert_done()

    by_id = {c.key.id: c for c in delta.items}
    assert set(by_id) == {"501", "503", "504"}  # the boundary item 503 repeats, never skips
    assert by_id["501"].edited and by_id["501"].body == "I see this too, on 2.3 as well."
    own = by_id["504"]
    assert own.body == "We reproduced this."  # the marker encoding is not in the body
    assert own.marker == OwnMarker("acme/widgets", "6f1c2a7e-0b4d-4c5e-9a1b-2c3d4e5f6a7b", "ob-17")
    assert own.author is not None and own.author.key.id == "42"
    assert delta.checkpoint == Checkpoint("github:2026-02-05T09:00:00Z")
    assert all(c.issue.url == "https://github.com/acme/widgets/issues/7" for c in delta.items)


def test_comment_deletions_are_found_only_by_id_set_reconciliation():
    replay = Replay.load("comment_deletions_reconcile.json")
    tracker = adapter(replay, page_size=2)

    before = full_ids(tracker, IdQuery(IdSubject.COMMENTS))
    assert before == {"501", "503", "504"}  # 502 is on a pull request
    delta = tracker.list_comments(CommentQuery(since=Checkpoint("github:2026-02-03T11:00:00Z")))
    assert [c.key.id for c in delta.items] == ["504"]  # a since pull cannot report the deletion
    after = full_ids(tracker, IdQuery(IdSubject.COMMENTS))
    replay.assert_done()

    assert after == {"501", "504"}
    assert before - after == {"503"}  # only complete snapshots may infer the deletion


def test_per_issue_comments_withhold_the_checkpoint_until_the_last_page():
    replay = Replay.load("issue_comments_creation_order.json")
    tracker = adapter(replay, page_size=2)
    issue = tracker.resolve("https://github.com/acme/widgets/issues/7")

    first = tracker.list_comments(CommentQuery(issue=issue))
    assert [c.key.id for c in first.items] == ["601", "602"]
    assert first.checkpoint is None and first.next_cursor is not None  # creation order: unsafe
    last = tracker.list_comments(CommentQuery(issue=issue, cursor=first.next_cursor))
    replay.assert_done()

    assert [c.key.id for c in last.items] == ["603"] and last.next_cursor is None
    # The running maximum travelled in the cursor: the edit on page 1 is the high-water mark.
    assert last.checkpoint == Checkpoint("github:2026-03-09T00:00:00Z")


def test_every_recorded_fixture_is_exercised_and_carries_no_credential():
    names = sorted(path.name for path in RECORDED.glob("*.json"))
    assert names == [
        "comment_create_accepted.json",  # test_tracker_github_comment_write.py
        "comment_create_refusals.json",  # test_tracker_github_comment_write.py
        "comment_deletions_reconcile.json",
        "comments_since_checkpoint.json",
        "issue_closes_between_pulls.json",
        "issue_comments_creation_order.json",
        "issues_closed_and_paginated.json",
    ]
    for name in names:
        text = (RECORDED / name).read_text(encoding="utf-8")
        assert '"authorization":' not in text.lower()  # no recorded credential header
        assert "bearer " not in text.lower() and '"token ' not in text.lower()
        assert "ghp_" not in text and "github_pat_" not in text
