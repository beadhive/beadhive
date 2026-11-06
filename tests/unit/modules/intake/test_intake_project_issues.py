"""``TrackerProjectIssues`` (bh-iamru): the bound project's issues for the mention check.

ONE listing per call, closed issues included, through the full id snapshot when the tracker can
resolve URLs and through the issue listing when it cannot; a failed listing is refused, never
answered partially; a factory binds the tracker only when the listing is asked for. Memory fakes
only: no clock, no sleep, no socket.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from beadhive.modules.intake.application import TrackerProjectIssues
from beadhive.modules.intake.contracts import ProjectIssues
from beadhive.modules.intake.domain import IntakeRefused, RefusalReason
from beadhive.modules.tracker.contracts import READ_OPERATIONS, IssueState, RetryBudget
from beadhive.testing.tracker import MemoryTracker, MemoryWorld, at
from beadhive.testing.tracker.memory import without_resolver


def _world() -> tuple[MemoryWorld, dict[str, str]]:
    world = MemoryWorld()
    open_one = world.add_issue("open", at=at(1))
    closed = world.add_issue("closed", at=at(1), state=IssueState.CLOSED)
    world.add_change_request("a pull request", at=at(1))  # never an issue
    return world, {open_one.identifier: open_one.url, closed.identifier: closed.url}


@pytest.mark.parametrize("resolver", [True, False], ids=["id-snapshot", "issue-listing"])
def test_every_issue_open_or_closed_by_identifier(resolver) -> None:
    world, expected = _world()
    tracker = MemoryTracker(world, enabled=READ_OPERATIONS)
    issues = TrackerProjectIssues(tracker if resolver else without_resolver(tracker))
    assert isinstance(issues, ProjectIssues)
    reads = world.read_requests()
    assert issues.identifiers() == expected
    assert world.read_requests() - reads == 1  # one listing (one page here)


def test_a_failed_listing_is_refused_never_answered_partially() -> None:
    world, _ = _world()
    budget = RetryBudget(max_retries=0, max_single_wait=timedelta(0), max_total_wait=timedelta(0))
    tracker = MemoryTracker(world, enabled=READ_OPERATIONS, budget=budget)
    world.throttle(5, retry_after_seconds=60)
    for source in (tracker, without_resolver(tracker)):
        with pytest.raises(IntakeRefused) as caught:
            TrackerProjectIssues(source).identifiers()
        assert caught.value.reason is RefusalReason.TRACKER_ERROR


def test_a_factory_binds_the_tracker_only_when_asked() -> None:
    world, expected = _world()
    bound: list[int] = []

    def factory():
        bound.append(1)
        return MemoryTracker(world, enabled=READ_OPERATIONS)

    issues = TrackerProjectIssues(factory)
    assert bound == []
    assert issues.identifiers() == expected and bound == [1]
