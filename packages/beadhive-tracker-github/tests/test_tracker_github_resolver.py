"""The optional ``IssueResolver`` for GitHub (amendment A1, external-intake-module-adr.md
section 5): ``resolve`` is pure and canonical; each pasted URL that is not an issue of the bound
repository gets its own refusal (another repository, a pull request, not a GitHub issue URL);
``get`` is one read that also refuses a pull-request number and a transferred issue."""

from __future__ import annotations

import pytest

from beadhive.modules.tracker.contracts import (
    ChangeRequestUrl,
    IssueKey,
    IssueState,
    NotVisible,
    Operation,
    OperationNotEnabled,
    OtherProject,
    UrlNotRecognized,
)
from tracker_github_fake import EPOCH, GHES, FakeGitHub, adapter_for

ISSUE = "https://github.com/acme/widgets/issues/12"


@pytest.mark.parametrize(
    "url",
    [
        ISSUE,
        ISSUE + "/",
        ISSUE + "?notification_referrer_id=1",
        ISSUE + "#issuecomment-99",
        "https://GitHub.com/ACME/Widgets/issues/12",
        "http://github.com/acme/widgets/issues/12",
        "https://api.github.com/repos/acme/widgets/issues/12",
        f"\t{ISSUE}\n",
    ],
)
def test_resolve_canonicalises_every_spelling_without_a_request(url):
    world = FakeGitHub()
    key = adapter_for(world).resolve(url)
    assert key == IssueKey("#12", ISSUE)
    assert world.requests == []


@pytest.mark.parametrize(
    ("url", "error"),
    [
        ("https://github.com/someone/else/issues/12", OtherProject),
        ("https://github.com/acme/widgets-fork/issues/12", OtherProject),
        ("https://api.github.com/repos/someone/else/issues/12", OtherProject),
        ("https://github.com/acme/widgets/pull/12", ChangeRequestUrl),
        ("https://github.com/acme/widgets/pull/12/files", ChangeRequestUrl),
        ("https://api.github.com/repos/acme/widgets/pulls/12", ChangeRequestUrl),
        ("https://github.com/acme/widgets/wiki/Home", UrlNotRecognized),
        ("https://github.com/acme/widgets/issues", UrlNotRecognized),
        ("https://github.com/acme/widgets/issues/0", UrlNotRecognized),
        ("https://github.com/acme/widgets/issues/12/edit", UrlNotRecognized),
        ("https://gitlab.com/acme/widgets/-/issues/12", UrlNotRecognized),
        ("https://ghe.example/acme/widgets/issues/12", UrlNotRecognized),  # another host
        ("ftp://github.com/acme/widgets/issues/12", UrlNotRecognized),
        ("#12", UrlNotRecognized),
        ("not a url at all", UrlNotRecognized),
    ],
)
def test_each_refusal_is_distinct_and_never_repeats_the_url(url, error):
    world = FakeGitHub()
    with pytest.raises(error) as caught:
        adapter_for(world).resolve(url)
    assert type(caught.value) is error
    assert url.strip() not in str(caught.value)
    assert world.requests == []


def test_other_repository_is_checked_before_the_pull_request_shape():
    with pytest.raises(OtherProject):
        adapter_for(FakeGitHub()).resolve("https://github.com/someone/else/pull/3")


def test_a_ghes_host_resolves_its_own_web_and_api_urls():
    world = FakeGitHub(**GHES)
    adapter = adapter_for(world)
    expected = IssueKey("#5", "https://ghe.example/acme/widgets/issues/5")
    assert adapter.resolve("https://ghe.example/acme/widgets/issues/5#x") == expected
    assert adapter.resolve("https://ghe.example/api/v3/repos/acme/widgets/issues/5") == expected
    with pytest.raises(UrlNotRecognized):
        adapter.resolve("https://github.com/acme/widgets/issues/5")


def test_get_reads_one_issue_open_or_closed():
    world = FakeGitHub()
    world.add_issue("open", at=EPOCH)
    closed = world.add_issue("closed", at=EPOCH, state=IssueState.CLOSED)
    adapter = adapter_for(world)
    issue = adapter.get(adapter.resolve(closed.url))
    assert issue.key == closed and issue.state is IssueState.CLOSED
    assert [r.url.path for r in world.requests] == ["/repos/acme/widgets/issues/2"]


def test_get_refuses_a_pull_request_number_a_deleted_and_a_missing_issue():
    world = FakeGitHub()
    change = world.add_change_request("a pull request", at=EPOCH)
    gone = world.add_issue("gone", at=EPOCH)
    world.delete_issue(gone)
    adapter = adapter_for(world)
    # A pasted /issues/<n> URL for a pull request resolves; the read reveals what it is.
    with pytest.raises(ChangeRequestUrl):
        adapter.get(adapter.resolve(change.url.replace("/pull/", "/issues/")))
    with pytest.raises(NotVisible, match="410 the issue was deleted"):
        adapter.get(gone)
    with pytest.raises(NotVisible, match="404"):
        adapter.get(adapter.resolve("https://github.com/acme/widgets/issues/99"))


def test_get_refuses_an_issue_transferred_to_another_repository():
    world = FakeGitHub()
    key = world.add_issue("moving", at=EPOCH)
    world.transfer_issue(key, "acme/other")
    with pytest.raises(OtherProject, match="transferred"):
        adapter_for(world).get(key)
    assert [r.url.path for r in world.requests] == [
        "/repos/acme/widgets/issues/1",
        "/repos/acme/other/issues/1",  # GitHub's 301, followed on the same API origin
    ]


def test_get_refuses_a_key_of_another_project_or_a_mismatched_key_before_any_request():
    world = FakeGitHub()
    adapter = adapter_for(world)
    with pytest.raises(OtherProject):
        adapter.get(IssueKey("#1", "https://github.com/someone/else/issues/1"))
    with pytest.raises(NotVisible):
        adapter.get(IssueKey("#2", "https://github.com/acme/widgets/issues/1"))
    assert world.requests == []


def test_get_needs_issues_read_and_resolve_does_not():
    world = FakeGitHub()
    adapter = adapter_for(world, enabled=frozenset({Operation.COMMENTS_READ}))
    key = adapter.resolve(ISSUE)  # pure: no read is involved
    with pytest.raises(OperationNotEnabled):
        adapter.get(key)
    assert world.requests == []
