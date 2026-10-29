"""One canonical ``IssueKey.url`` per issue, whatever case GitHub or the paster spells.

GitHub reports an ``html_url`` in the repository's own owner/name case, which need not be the
bound project's spelling (the hive triplet). The adapter settles that in ONE place
(``GitHubLocation.canonical_url``), so ``resolve`` and every read return the same URL and the
consumers that join on it (``bh work import`` idempotency, the other-repository refusal) cannot
see a second spelling. Offline: the fake GitHub world only (the importer itself is tracker-neutral
and a plugin package may not import it, so the importer's steps are asserted on the adapter).
"""

from __future__ import annotations

import pytest

from beadhive.modules.tracker.contracts import (
    CommentQuery,
    IdQuery,
    IdSubject,
    IssueQuery,
    OtherProject,
)
from tracker_github_fake import EPOCH, FakeGitHub, adapter_for

CANONICAL = "https://github.com/acme/widgets/issues/1"
SPELLINGS = [
    "https://github.com/acme/widgets/issues/1",
    "https://github.com/Acme/Widgets/issues/1",
    "https://GitHub.com/ACME/WIDGETS/issues/1/",
]


def respell(world: FakeGitHub, owner_name: str) -> None:
    """Make GitHub report every ``html_url`` in ``owner_name`` case (the routes keep serving)."""
    for row in world.issues:
        row["html_url"] = f"{world.web}/{owner_name}/issues/{row['number']}"
    for row in world.comments:
        row["html_url"] = row["html_url"].replace("acme/widgets", owner_name)


def world_with_issue() -> FakeGitHub:
    world = FakeGitHub()
    world.add_issue("Login is broken", at=EPOCH)
    respell(world, "Acme/Widgets")
    return world


def test_every_read_carries_the_bound_spelling_not_githubs_case():
    world = world_with_issue()
    adapter = adapter_for(world)
    key = adapter.resolve("https://github.com/Acme/Widgets/issues/1")
    assert key.url == CANONICAL
    assert adapter.get(key).key.url == CANONICAL
    assert adapter.list_issues(IssueQuery()).items[0].key.url == CANONICAL
    assert adapter.list_ids(IdQuery(IdSubject.ISSUES)).ids == (CANONICAL,)


def test_a_comments_parent_issue_carries_the_bound_spelling():
    world = world_with_issue()
    adapter = adapter_for(world)
    key = adapter.resolve(CANONICAL)
    world.add_comment(key, "hello", at=EPOCH)
    respell(world, "Acme/Widgets")
    comment = adapter.list_comments(CommentQuery()).items[0]
    assert comment.issue.url == CANONICAL


def test_a_transferred_issue_is_still_refused_whatever_case():
    world = FakeGitHub()
    key = world.add_issue("moving", at=EPOCH)
    world.transfer_issue(key, "Someone/Else")
    with pytest.raises(OtherProject):
        adapter_for(world).get(key)


@pytest.mark.parametrize("pasted", SPELLINGS)
def test_the_importers_steps_agree_for_every_pasted_spelling(pasted):
    """What ``bh work import`` joins on: the key it asked for (``resolve``) must equal the
    canonical key the read answers with, or it takes its transfer branch and re-resolves; and the
    stored external ref must be one URL per issue, so a second import finds the first."""
    world = world_with_issue()
    adapter = adapter_for(world)
    asked = adapter.resolve(pasted)
    answered = adapter.get(asked).key
    assert asked.url == answered.url == CANONICAL  # no "moved" branch, one stored spelling
    adapter.resolve(answered.url)  # the R7 re-resolve of the canonical URL never refuses
    assert world.requests and all(r.method == "GET" for r in world.requests)


@pytest.mark.parametrize(
    "pasted",
    [
        "https://github.com/someone/else/issues/1",
        "https://github.com/Someone/Else/issues/1",
        "https://github.com/ACME/widgets-fork/issues/1",
    ],
)
def test_another_repository_is_refused_in_any_spelling(pasted):
    world = world_with_issue()
    with pytest.raises(OtherProject):
        adapter_for(world).resolve(pasted)
    assert world.requests == []
