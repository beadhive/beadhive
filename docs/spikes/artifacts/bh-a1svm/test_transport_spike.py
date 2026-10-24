"""Offline tests for spike bh-a1svm. httpx.MockTransport only: no network, no GitHub writes, no
real sleeps (``time.sleep`` and ``anyio.sleep`` are made fatal for every test, and retry timing is
asserted against ``FakeClock``).

Run against whichever candidates the interpreter has installed (githubkit tests skip cleanly when
the package is absent)::

    python -m pytest docs/spikes/artifacts/bh-a1svm -p no:cacheprovider -q

Not collected by the project suite (``testpaths = ["tests"]``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
from fake_github import API, OWNER, REPO, TOKEN, failure, make_comment, make_issue, ts
from sync_port import Mirror, pull, reconcile_comment_ids
from transport_httpx import HttpxTransport, RetryPolicy

EPOCH_2026_01_01 = 1_767_225_600.0
KINDS = ["httpx", "githubkit"]


@pytest.fixture(params=KINDS)
def tp(request, fake, clock):
    """Build the transport under test (per_page=2 so the fixture spans several pages)."""
    kind = request.param
    if kind == "httpx":
        client = httpx.Client(
            base_url=API,
            transport=fake.transport(),
            headers={"Authorization": f"Bearer {TOKEN}", "Accept": "application/vnd.github+json"},
        )
        yield HttpxTransport(
            client,
            OWNER,
            REPO,
            sleep=clock.sleep,
            now=lambda: EPOCH_2026_01_01 + clock.now,
            per_page=2,
        )
        client.close()
    else:
        pytest.importorskip("githubkit")
        from transport_githubkit import GithubkitTransport, build_client, patch_sleep

        gh = build_client(TOKEN, transport=fake.transport())
        with patch_sleep(clock.sleep):
            yield GithubkitTransport(gh, OWNER, REPO, per_page=2)


def _request_errors() -> tuple[type[Exception], ...]:
    """The exception types a failed request surfaces as, per candidate."""
    try:
        from githubkit.exception import RequestFailed
    except ImportError:  # httpx-only interpreter
        return (httpx.HTTPStatusError,)
    return (httpx.HTTPStatusError, RequestFailed)


def _query(request: httpx.Request) -> dict[str, str]:
    return {k: v[-1] for k, v in parse_qs(request.url.query.decode()).items()}


# --- acceptance (2): issues incl. closed, comments, Link pagination, since checkpoint ---------


def test_fixture_fidelity_default_state_hides_closed(fake):
    """Sanity: the fake reproduces GitHub's default - closed issues need an explicit state=all."""
    response = httpx.Client(transport=fake.transport()).get(
        f"{API}/repos/{OWNER}/{REPO}/issues", params={"per_page": 100}
    )
    assert [i["number"] for i in response.json()] == [1, 4]


def test_lists_closed_issues_and_follows_link_header(tp, fake):
    issues = tp.list_issues(since=None)
    assert [(i.number, i.state, i.is_pull_request) for i in issues] == [
        (1, "open", False),
        (2, "closed", False),
        (3, "closed", True),
        (4, "open", False),
        (5, "closed", False),
    ]
    assert len(fake.requests) == 3  # 5 rows / per_page=2
    assert all(_query(r)["state"] == "all" for r in fake.requests)
    # pages 2 and 3 were reached by following the server's rel="next" URL verbatim
    assert [_query(r).get("page", "1") for r in fake.requests] == ["1", "2", "3"]
    assert all(r.headers["authorization"].endswith(TOKEN) for r in fake.requests)


def test_lists_comments_paginated(tp, fake):
    comments = tp.list_comments(since=None)
    assert [(c.id, c.issue_number) for c in comments] == [
        (501, 1), (502, 2), (503, 3), (504, 4), (505, 5),
    ]  # fmt: skip
    assert len(fake.requests) == 3


def test_since_checkpoint_is_incremental_and_idempotent(tp, fake):
    mirror = Mirror()
    pull(tp, mirror)
    assert sorted(mirror.issues) == [1, 2, 4, 5]  # PR #3 excluded
    assert mirror.issues_checkpoint == datetime(2026, 1, 20, tzinfo=UTC)
    assert mirror.comments_checkpoint == datetime(2026, 1, 21, tzinfo=UTC)

    # upstream moves: #2 reopened, new issue #6, new comment 506
    fake.issues[1] = make_issue(2, state="open", updated=ts(26))
    fake.issues.append(make_issue(6, state="closed", updated=ts(25)))

    fake.comments.append(make_comment(506, 6, updated=ts(27)))
    fake.requests.clear()

    pull(tp, mirror)
    sent = [_query(r) for r in fake.requests]
    assert {q["since"][:10] for q in sent} == {"2026-01-20", "2026-01-21"}
    assert mirror.issues[2].state == "open"  # reopened issue refreshed
    assert sorted(mirror.issues) == [1, 2, 4, 5, 6]  # closed #6 arrived, no duplicates
    assert sorted(mirror.comments) == [501, 502, 503, 504, 505, 506]
    # boundary item (#5, updated exactly at the checkpoint) is re-fetched: since is inclusive
    boundary = tp.list_issues(since=datetime(2026, 1, 20, tzinfo=UTC))
    assert {i.number for i in boundary} == {2, 5, 6}


def test_deletion_is_invisible_to_since_but_found_by_full_id_pass(tp, fake):
    mirror = Mirror()
    pull(tp, mirror)
    fake.delete_comment(502)
    pull(tp, mirror)  # incremental pull cannot learn about the deletion
    assert 502 in mirror.comments
    assert reconcile_comment_ids(tp, mirror) == {502}
    assert 502 not in mirror.comments


# --- acceptance (4): retry decisions with a mocked clock, never a real sleep ------------------


def test_server_error_retried_with_backoff_on_the_mock_clock(tp, fake, clock):
    fake.script.extend([failure(502), failure(502)])
    assert [i.number for i in tp.list_issues(since=None)] == [1, 2, 3, 4, 5]
    assert clock.sleeps == [1.0, 4.0]  # (n+1)**2 for retry n; identical in both candidates
    assert clock.now == 5.0


def test_secondary_rate_limit_honours_retry_after(tp, fake, clock):
    fake.script.append(failure(403, headers={"retry-after": "7"}, message="slow down"))
    tp.list_issues(since=None)
    assert clock.sleeps == [7.0]


def test_client_error_is_not_retried(tp, fake, clock):
    fake.script.append(failure(404, message="Not Found"))
    with pytest.raises(_request_errors()) as err:
        tp.list_issues(since=None)
    assert "404" in str(err.value) or "Not Found" in str(err.value)
    assert clock.sleeps == []
    assert len(fake.requests) == 1


def test_server_error_gives_up_after_the_retry_budget(tp, fake, clock):
    fake.script.extend([failure(500)] * 4)
    with pytest.raises(_request_errors()):
        tp.list_issues(since=None)
    assert clock.sleeps == [1.0, 4.0, 9.0]  # three retries, then the fourth failure surfaces
    assert len(fake.requests) == 4


def test_httpx_primary_rate_limit_uses_the_injected_clock(fake, clock):
    """Plain httpx: x-ratelimit-reset is compared with an INJECTED epoch clock."""
    client = httpx.Client(base_url=API, transport=fake.transport())
    tp = HttpxTransport(
        client,
        OWNER,
        REPO,
        sleep=clock.sleep,
        now=lambda: EPOCH_2026_01_01 + clock.now,
    )
    reset = str(int(EPOCH_2026_01_01 + 30))
    fake.script.append(
        failure(403, headers={"x-ratelimit-remaining": "0", "x-ratelimit-reset": reset})
    )
    tp.list_issues(since=None)
    assert clock.sleeps == [30.0]
    assert RetryPolicy().delay(httpx.Response(403), 0, 0.0) is None


# --- plain httpx hand-written ETag handling ----------------------------------------------------


def test_httpx_etag_cache_is_hand_written_but_works(fake, clock):
    client = httpx.Client(base_url=API, transport=fake.transport())
    tp = HttpxTransport(client, OWNER, REPO, sleep=clock.sleep, now=lambda: 0.0, per_page=2)
    first = tp.list_issues(since=None)
    second = tp.list_issues(since=None)
    assert first == second
    assert [r.headers.get("if-none-match") is not None for r in fake.requests] == [
        False, False, False, True, True, True,
    ]  # fmt: skip
