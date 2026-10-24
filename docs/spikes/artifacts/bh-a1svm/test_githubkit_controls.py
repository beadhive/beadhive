"""githubkit-only control points for spike bh-a1svm: how its retry and HTTP cache are disabled or
injected so tests assert retry decisions on a mocked clock. Skipped when githubkit is absent."""

from __future__ import annotations

import asyncio
import datetime as dt

import httpx
import pytest
from fake_github import API, OWNER, REPO, TOKEN, failure

pytest.importorskip("githubkit", reason="githubkit not installed in this interpreter")


def _gk(fake, **opts):
    from transport_githubkit import GithubkitTransport, build_client

    return GithubkitTransport(build_client(TOKEN, transport=fake.transport(), **opts), OWNER, REPO)


def test_githubkit_auto_retry_false_disables_retry(fake, clock):
    from githubkit.exception import RequestFailed
    from transport_githubkit import patch_sleep

    fake.script.append(failure(502))
    with patch_sleep(clock.sleep), pytest.raises(RequestFailed):
        _gk(fake, auto_retry=False).list_issues(since=None)
    assert len(fake.requests) == 1
    assert clock.sleeps == []


def test_githubkit_custom_decision_function_is_injected_and_pure(fake, clock):
    """The retry DECISION is a pure function of (exception, retry_count): inject and record it."""
    from githubkit.retry import RetryOption
    from transport_githubkit import patch_sleep

    seen: list[tuple[str, int, int]] = []

    def decide(exc, retry_count):
        seen.append((type(exc).__name__, exc.response.status_code, retry_count))
        return RetryOption(retry_count < 2, dt.timedelta(seconds=0.5))

    fake.script.extend([failure(503), failure(503)])
    with patch_sleep(clock.sleep):
        _gk(fake, auto_retry=decide).list_issues(since=None)
    assert seen == [("RequestFailed", 503, 0), ("RequestFailed", 503, 1)]
    assert clock.sleeps == [0.5, 0.5]


def test_githubkit_default_decision_functions_are_pure_and_importable():
    """Decision logic can be asserted with NO client, NO transport and NO sleep at all."""
    from githubkit.exception import PrimaryRateLimitExceeded, RequestFailed
    from githubkit.retry import RETRY_DEFAULT, RETRY_RATE_LIMIT, RETRY_SERVER_ERROR

    req = httpx.Request("GET", f"{API}/x")

    def raw(status, **headers):
        from githubkit.response import Response

        return Response(httpx.Response(status, request=req, headers=headers, json={}), dict)

    assert RETRY_SERVER_ERROR(RequestFailed(raw(500)), 0).retry_after.total_seconds() == 1
    assert RETRY_SERVER_ERROR(RequestFailed(raw(500)), 3).do_retry is False
    assert RETRY_SERVER_ERROR(RequestFailed(raw(404)), 0).do_retry is False
    limited = PrimaryRateLimitExceeded(raw(403), dt.timedelta(seconds=9))
    assert RETRY_RATE_LIMIT(limited, 0).retry_after.total_seconds() == 9
    assert RETRY_RATE_LIMIT(limited, 1).do_retry is False  # budget: one retry
    assert RETRY_DEFAULT(limited, 0).do_retry is True


def test_githubkit_primary_limit_reset_in_the_past_sleeps_sixty_by_default(fake, clock):
    """OBSERVED GOTCHA (githubkit 0.16.1): x-ratelimit-reset is compared with the REAL wall clock
    (not injectable) and a zero wait is falsy, so ``time.sleep(... if retry_after else 60)``
    sleeps a full minute. Pin the behaviour so an upgrade that fixes it is noticed."""
    from transport_githubkit import patch_sleep

    fake.script.append(
        failure(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": "1"})
    )
    with patch_sleep(clock.sleep):
        _gk(fake).list_issues(since=None)
    assert clock.sleeps == [60.0]


def test_githubkit_async_retry_sleeps_through_anyio_only(fake, monkeypatch):
    import anyio
    from githubkit import GitHub

    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(anyio, "sleep", fake_sleep)
    fake.script.append(failure(502))

    async def run():
        gh = GitHub(TOKEN, async_transport=fake.transport(), http_cache=False)
        issues = []
        async for issue in gh.rest.paginate(
            gh.rest.issues.async_list_for_repo, owner=OWNER, repo=REPO, state="all", per_page=100
        ):
            issues.append(issue.number)
        return issues

    assert asyncio.run(run()) == [1, 2, 3, 4, 5]
    assert slept == [1.0]


def test_githubkit_http_cache_off_means_plain_httpx_client_and_no_conditional_headers(fake):
    from githubkit.hishel import SyncCacheClient

    gh_off = __import__("transport_githubkit").build_client(TOKEN, transport=fake.transport())
    client = gh_off._create_sync_client()
    assert type(client) is httpx.Client and not isinstance(client, SyncCacheClient)
    client.close()
    tp = _gk(fake, http_cache=False)
    tp.list_issues(since=None)
    tp.list_issues(since=None)
    assert all("if-none-match" not in r.headers for r in fake.requests)


def test_githubkit_http_cache_on_revalidates_with_etag(fake):
    """With the library default (http_cache=True) a repeat request is conditional and a 304 is
    answered from the cache. Opt-in only: the first thing a cache bug costs is stale tracker
    state, so the recommendation keeps it OFF until a bead enables it."""
    from githubkit import GitHub

    with GitHub(TOKEN, transport=fake.transport(), http_cache=True) as gh:
        first = gh.rest.issues.list_for_repo(OWNER, REPO, state="all", per_page=100)
        second = gh.rest.issues.list_for_repo(OWNER, REPO, state="all", per_page=100)
    assert len(fake.requests) == 2
    assert "if-none-match" not in fake.requests[0].headers
    assert fake.requests[1].headers["if-none-match"].startswith('W/"')
    assert [i.number for i in second.parsed_data] == [i.number for i in first.parsed_data]


def test_githubkit_base_url_supports_ghes(fake):
    seen: list[httpx.URL] = []

    def handler(request):
        seen.append(request.url)
        return httpx.Response(200, json=[])

    from transport_githubkit import GithubkitTransport, build_client

    gh = build_client(
        TOKEN, transport=httpx.MockTransport(handler), base_url="https://ghe.example/api/v3"
    )
    GithubkitTransport(gh, OWNER, REPO).list_issues(since=None)
    assert seen[0].host == "ghe.example"
    assert seen[0].path == f"/api/v3/repos/{OWNER}/{REPO}/issues"
