"""The adapter-owned retry layer, asserted on a mocked clock (no test sleeps).

Two levels: the pure decision (:func:`classify` and :func:`next_wait`, no client at all), and the
transport through githubkit over a ``MockTransport``, where the injected ``sleep`` advances a
:class:`~tracker_github_fake.FakeClock` and githubkit's own ``time.sleep`` is replaced by a trap
that fails the test if it is ever reached (its retry is off: the clamp).
"""

from __future__ import annotations

from datetime import timedelta
from types import SimpleNamespace

import httpx
import pytest

from beadhive.modules.tracker.contracts import (
    IssueQuery,
    Operation,
    PermissionDenied,
    RateLimited,
    RetryBudget,
    TransientFailure,
    WriteOutcomeUnknown,
)
from beadhive_tracker_github.adapter.retry import (
    DEFAULT_LIMIT_WAIT,
    MIN_RESET_WAIT,
    backoff,
    classify,
    classify_transport_failure,
    next_wait,
)
from tracker_github_fake import EPOCH, FakeGitHub, adapter_for

BUDGET = RetryBudget(
    max_retries=3, max_single_wait=timedelta(seconds=60), max_total_wait=timedelta(seconds=120)
)
RESET_IN_30S = str(int(EPOCH.timestamp()) + 30)


def _classify(status, headers=None, message="", *, write=False):
    return classify(
        status,
        headers or {},
        message,
        write=write,
        now=EPOCH,
        source="env:GITHUB_TOKEN",
        operation=Operation.ISSUES_READ,
    )


@pytest.fixture(autouse=True)
def githubkit_never_sleeps(monkeypatch):
    """githubkit's retry sleep is a module attribute; trap it so reaching it fails the test."""
    import githubkit.core as core

    def trap(seconds):
        raise AssertionError(f"githubkit slept {seconds}s: its retry must be off")

    monkeypatch.setattr(core, "time", SimpleNamespace(sleep=trap))


# ---- the pure decision ------------------------------------------------------------------------


def test_a_secondary_limit_waits_exactly_the_retry_after():
    refusal = _classify(403, {"retry-after": "7"}, "You have exceeded a secondary rate limit.")
    assert isinstance(refusal.error, RateLimited) and refusal.error.kind == "throttle"
    assert refusal.retryable and refusal.wait == 7.0
    assert refusal.error.retry_after == timedelta(seconds=7)
    assert next_wait(refusal, attempt=0, waited=0, budget=BUDGET) == 7.0


def test_a_primary_limit_waits_until_the_reset_on_the_injected_clock():
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": RESET_IN_30S}
    refusal = _classify(403, headers, "API rate limit exceeded")
    assert isinstance(refusal.error, RateLimited) and refusal.error.kind == "quota"
    assert refusal.wait == 30.0


def test_a_reset_already_past_never_spins_and_never_waits_githubkits_sixty_seconds():
    """githubkit 0.16.1 compares the reset with the REAL clock and sleeps 60 s on a zero delay
    (spike bh-a1svm E7). Here the clock is injected and a past reset waits the floor only."""
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": str(int(EPOCH.timestamp()) - 5)}
    assert _classify(403, headers).wait == MIN_RESET_WAIT


@pytest.mark.parametrize(
    ("status", "headers", "message", "kind"),
    [
        (429, {}, "", "throttle"),  # a bare 429 names no time: GitHub says wait a minute
        (403, {"x-ratelimit-remaining": "0"}, "", "quota"),  # no reset header
        (
            403,
            {"x-ratelimit-remaining": "0"},
            "You have exceeded a secondary rate limit",
            "throttle",
        ),
    ],
)
def test_a_limit_without_a_stated_time_waits_the_documented_minute(status, headers, message, kind):
    refusal = _classify(status, headers, message)
    assert refusal.wait == DEFAULT_LIMIT_WAIT and refusal.error.kind == kind


def test_a_rate_limit_is_never_classified_as_a_permission_refusal():
    refusal = _classify(403, {"retry-after": "1"}, "Resource not accessible by integration")
    assert isinstance(refusal.error, RateLimited)
    assert "not a permission refusal" in str(refusal.error)
    assert isinstance(
        _classify(403, {}, "Resource not accessible by integration").error, PermissionDenied
    )


def test_the_budget_bounds_retries_single_waits_and_total_wait():
    limited = _classify(403, {"retry-after": "50"})
    assert next_wait(limited, attempt=0, waited=0, budget=BUDGET) == 50.0
    assert next_wait(limited, attempt=0, waited=80, budget=BUDGET) is None  # 130 > 120 total
    assert next_wait(limited, attempt=3, waited=0, budget=BUDGET) is None  # retries spent
    too_long = _classify(403, {"retry-after": "61"})
    assert next_wait(too_long, attempt=0, waited=0, budget=BUDGET) is None  # > single wait
    refused = _classify(404, {}, "Not Found")
    assert not refused.retryable and next_wait(refused, attempt=0, waited=0, budget=BUDGET) is None


def test_a_read_server_error_backs_off_exponentially_within_the_single_wait():
    refusal = _classify(502, {}, "Bad Gateway")
    assert isinstance(refusal.error, TransientFailure) and refusal.retryable
    assert [backoff(n, BUDGET) for n in range(4)] == [1.0, 2.0, 4.0, 8.0]
    capped = RetryBudget(max_retries=9, max_single_wait=timedelta(seconds=5))
    assert backoff(6, capped) == 5.0
    assert _classify(503, {"retry-after": "3"}).wait == 3.0  # GitHub's own hint wins


def test_a_write_cut_off_or_answered_5xx_is_unknown_and_never_retried():
    for refusal in (
        _classify(502, {}, "Bad Gateway", write=True),
        classify_transport_failure(write=True, timeout=False),
        classify_transport_failure(write=True, timeout=True),
    ):
        assert isinstance(refusal.error, WriteOutcomeUnknown) and not refusal.retryable
        assert next_wait(refusal, attempt=0, waited=0, budget=BUDGET) is None
    # A rate limit proves nothing was written, so a write refused by one may be retried.
    assert _classify(403, {"retry-after": "2"}, write=True).retryable


# ---- through the transport, on the mocked clock -----------------------------------------------


def _world_with(*responses: httpx.Response) -> FakeGitHub:
    world = FakeGitHub()
    world.add_issue("one", at=EPOCH)
    world.scripted.extend(responses)
    return world


def test_a_primary_limit_is_slept_on_the_injected_seam_and_the_clock_moves():
    headers = {"x-ratelimit-remaining": "0", "x-ratelimit-reset": RESET_IN_30S}
    world = _world_with(httpx.Response(403, json={"message": "API rate limit"}, headers=headers))
    page = adapter_for(world).list_issues(IssueQuery())

    assert [i.title for i in page.items] == ["one"]
    assert world.sleeps == [30.0]  # exactly until the reset, on the adapter's seam
    assert world.clock.now() == EPOCH + timedelta(seconds=30)
    assert len(world.requests) == 2


def test_a_wait_beyond_the_single_wait_is_surfaced_not_slept():
    headers = {
        "x-ratelimit-remaining": "0",
        "x-ratelimit-reset": str(int(EPOCH.timestamp()) + 3600),
    }
    world = _world_with(httpx.Response(403, json={"message": "API rate limit"}, headers=headers))
    with pytest.raises(RateLimited) as caught:
        adapter_for(world).list_issues(IssueQuery())

    assert caught.value.retry_after == timedelta(seconds=3600) and caught.value.kind == "quota"
    assert world.sleeps == [] and len(world.requests) == 1


def test_server_errors_on_a_read_are_retried_with_backoff_then_surfaced():
    world = _world_with(*(httpx.Response(502, json={"message": "Bad Gateway"}) for _ in range(4)))
    with pytest.raises(TransientFailure):
        adapter_for(world).list_issues(IssueQuery())
    assert world.sleeps == [1.0, 2.0, 4.0]  # three retries (the default budget), no fourth
    assert len(world.requests) == 4


def test_a_dropped_connection_on_a_read_is_transient_and_retried():
    world = FakeGitHub()
    world.add_issue("one", at=EPOCH)
    real = world.handler
    failures = iter([httpx.ConnectError("reset by peer"), httpx.ReadTimeout("slow")])

    def flaky(request):
        error = next(failures, None)
        if error is not None:
            world.requests.append(request)
            raise error
        return real(request)

    world.handler = flaky  # type: ignore[method-assign]
    page = adapter_for(world).list_issues(IssueQuery())
    assert [i.title for i in page.items] == ["one"]
    assert world.sleeps == [1.0, 2.0]


def test_the_retry_budget_from_the_settings_is_the_one_enforced():
    world = _world_with(httpx.Response(403, json={"message": "slow"}, headers={"retry-after": "5"}))
    budget = RetryBudget(max_retries=0)
    with pytest.raises(RateLimited):
        adapter_for(world, budget=budget).list_issues(IssueQuery())
    assert world.sleeps == []
