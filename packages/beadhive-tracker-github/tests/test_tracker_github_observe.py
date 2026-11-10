"""The adapter's blocking I/O is observable by the host (``bh-17g6b``): one mark per credential
resolution, per client construction and per HTTP request, and nothing at all when the host does
not install an observer. Counts only: nothing here reads a clock."""

from __future__ import annotations

from collections import Counter
from datetime import timedelta

from beadhive.modules.tracker.contracts import IssueQuery, observing
from tracker_github_fake import EPOCH, FakeGitHub, adapter_for


class Counting:
    """An observer that counts each named call and keeps the opened ones balanced."""

    def __init__(self) -> None:
        self.began: Counter[str] = Counter()
        self.open = 0

    def begin(self, name: str) -> str:
        self.began[name] += 1
        self.open += 1
        return name

    def end(self, handle: str) -> None:
        assert handle in self.began
        self.open -= 1


def test_one_operation_marks_its_credential_client_and_every_request():
    world = FakeGitHub()
    world.add_issue("one", at=EPOCH + timedelta(days=1))
    adapter = adapter_for(world)
    observer = Counting()

    with observing(observer):
        adapter.list_issues(IssueQuery())

    assert observer.began["github_token"] == 1 and observer.began["github_client"] == 1
    assert observer.began["github_request"] == len(world.requests) >= 1
    assert observer.open == 0  # every mark was closed


def test_nothing_is_marked_without_an_observer_and_none_outlives_the_context():
    world = FakeGitHub()
    adapter = adapter_for(world)
    observer = Counting()

    adapter.list_issues(IssueQuery())  # not observed
    with observing(observer):
        pass
    adapter.list_issues(IssueQuery())  # after the context: not observed either

    assert not observer.began
