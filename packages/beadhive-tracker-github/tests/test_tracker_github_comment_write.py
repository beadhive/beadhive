"""``comment.create`` (W8, ``bh-03s4w``): the write gate, the ambiguous outcome, the diagnostics.

Offline by construction (``conftest.py``): every request goes to the ``httpx.MockTransport``
stand-in (``tracker_github_fake.FakeGitHub``) or to a replay of ``recorded/``. **No test writes
to GitHub.** The first real write is an operator-approved step recorded on bead ``bh-03s4w``,
outside this suite. No test reads a clock or sleeps: the adapter's clock and sleep are the fake
world's, and ``time.sleep`` itself is made to fail here.

What is asserted ON THE TRANSPORT (the requests the fake or the replay actually received):

* flag off, no grant, a mismatched grant or a spent grant: no request at all, not even a read,
  and the credential is not resolved;
* a timeout (or a ``5xx``, a dropped connection, an unconfirmed redirect, an unreadable ``201``)
  after the send raises ``WriteOutcomeUnknown`` after exactly ONE request, with no retry and no
  sleep, whatever the retry budget allows;
* each documented refusal, replayed from ``recorded/comment_create_refusals.json``, raises its
  port error with the exact one-line diagnostic, after one request (a rate limit within budget
  is the only retried refusal), and spends the grant.
"""

from __future__ import annotations

import json
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

import beadhive.modules.tracker.contracts as port
from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    AuthenticationFailed,
    CommentQuery,
    CommentWriter,
    IssueKey,
    Operation,
    OperationNotEnabled,
    OwnMarker,
    PermissionDenied,
    ProjectRef,
    RateLimited,
    RetryBudget,
    WriteGrant,
    WriteOutcomeUnknown,
)
from beadhive.testing.tracker import assert_contract_only
from beadhive_plugins.credentials import ResolvedToken
from beadhive_tracker_github.adapter import (
    CommentCreate,
    GitHubTrackerAdapter,
    adapter_class,
    build_adapter,
    decode_marker,
    new_adapter,
)
from beadhive_tracker_github.adapter.transport import API_VERSION
from tracker_github_fake import FAKE_TOKEN, GHES, FakeClock, FakeGitHub, adapter_for

COMMENT = Operation.COMMENT_CREATE
WRITE_ON = READ_OPERATIONS | {COMMENT}
MARKER = OwnMarker("acme/widgets", "inst-1", "ob1_01JC0000000000000000000000")
RECORDED = Path(__file__).resolve().parent / "recorded"
PROJECT = ProjectRef("github", "acme/widgets")
#: Generous on purpose: an ambiguous write must not be retried however much budget is left.
GENEROUS = RetryBudget(max_retries=9, max_single_wait=timedelta(seconds=60))


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    def refuse(_seconds):
        raise AssertionError("a test slept for real; waits go through the injected sleep")

    monkeypatch.setattr(time, "sleep", refuse)


class CountingTokens:
    """A token source that counts how often the credential was resolved."""

    def __init__(self, value: str | None = FAKE_TOKEN, source: str = "env:GITHUB_TOKEN"):
        self.value, self.source, self.calls = value, source, 0

    def __call__(self) -> ResolvedToken | None:
        self.calls += 1
        return None if self.value is None else ResolvedToken(self.value, self.source)


def grant(target: IssueKey, grant_id: str = "g-1", op: Operation = COMMENT) -> WriteGrant:
    return WriteGrant(grant_id=grant_id, operation=op, target=target)


def world_with_issue(**kwargs: Any) -> tuple[FakeGitHub, IssueKey]:
    world = FakeGitHub(**kwargs)
    issue = world.add_issue("thread", at=datetime(2026, 1, 2, tzinfo=UTC))
    world.set_write_time(datetime(2026, 1, 6, tzinfo=UTC))
    return world, issue


def writer(world: FakeGitHub, *, budget=None, tokens=None) -> GitHubTrackerAdapter:
    return adapter_for(world, enabled=WRITE_ON, budget=budget, tokens=tokens)


# ---- the gate: refused before any request ---------------------------------------------------


def test_with_the_flag_off_the_built_adapter_has_no_writer_and_sends_nothing():
    world, _ = world_with_issue()
    tokens = CountingTokens()
    settings = {"read": {"comments": {"enabled": True}}, "write": {"comment": {"enabled": False}}}
    adapter = build_adapter(
        settings, tokens, project=PROJECT, sleep=world.sleep, transport=world.transport()
    )

    assert not isinstance(adapter, CommentWriter) and not hasattr(adapter, "post_comment")
    assert world.requests == [] and tokens.calls == 0


def test_a_writer_whose_operation_is_not_enabled_refuses_before_any_request():
    """Defence in depth: even a composed writer re-checks its own enabled set."""
    world, issue = world_with_issue()
    tokens = CountingTokens()
    cls = adapter_class({COMMENT})
    adapter = cls(
        project=PROJECT,
        token_source=tokens,
        sleep=world.sleep,
        clock=world.clock.now,
        enabled=READ_OPERATIONS,  # comment.create is NOT enabled
        transport=world.transport(),
    )
    assert isinstance(adapter, CommentCreate)

    with pytest.raises(OperationNotEnabled, match="comment.create is not enabled"):
        adapter.post_comment(issue, "hi", marker=MARKER, grant=grant(issue))
    assert world.requests == [] and tokens.calls == 0
    assert world.comment_count(issue) == 0


@pytest.mark.parametrize("bad", ["none", "a-string", "a-dict", "other-operation", "other-issue"])
def test_no_grant_or_a_mismatched_grant_is_refused_before_any_request(bad):
    world, issue = world_with_issue()
    other = world.add_issue("other", at=datetime(2026, 1, 2, tzinfo=UTC))
    tokens = CountingTokens()
    adapter = writer(world, tokens=tokens)
    presented = {
        "none": None,
        "a-string": "g-1",
        "a-dict": {"grant_id": "g-1", "operation": "comment.create", "target": issue},
        "other-operation": grant(issue, op=Operation.LABELS_CHANGE),
        "other-issue": grant(other),
    }[bad]

    with pytest.raises(OperationNotEnabled):
        adapter.post_comment(issue, "hi", marker=MARKER, grant=presented)
    assert world.requests == [] and tokens.calls == 0

    # A refused grant was never spent: the matching grant with the same id still works once.
    adapter.post_comment(issue, "hi", marker=MARKER, grant=grant(issue, "g-1"))
    assert world.write_requests() == 1 and world.comment_count(issue) == 1


def test_a_key_of_another_repository_is_refused_before_any_request():
    world, _ = world_with_issue()
    adapter = writer(world)
    foreign = IssueKey("#1", "https://github.com/someone-else/elsewhere/issues/1")

    with pytest.raises(port.OtherProject):
        adapter.post_comment(foreign, "hi", marker=MARKER, grant=grant(foreign))
    assert world.requests == []


def test_an_unencodable_marker_is_refused_before_the_grant_is_spent():
    world, issue = world_with_issue()
    adapter = writer(world)
    broken = OwnMarker("acme/widgets", "inst 1", "ob-1")  # a space breaks the grammar

    with pytest.raises(ValueError, match="marker install"):
        adapter.post_comment(issue, "hi", marker=broken, grant=grant(issue))
    assert world.requests == []
    adapter.post_comment(issue, "hi", marker=MARKER, grant=grant(issue))  # still unspent
    assert world.write_requests() == 1


def test_a_spent_grant_is_refused_before_any_request():
    world, issue = world_with_issue()
    tokens = CountingTokens()
    adapter = writer(world, tokens=tokens)
    once = grant(issue, "g-once")

    adapter.post_comment(issue, "first", marker=MARKER, grant=once)
    sent, resolved = list(world.requests), tokens.calls
    assert [r.method for r in sent] == ["POST"]

    with pytest.raises(OperationNotEnabled, match="already used"):
        adapter.post_comment(issue, "again", marker=MARKER, grant=once)
    assert world.requests == sent and tokens.calls == resolved  # nothing more was sent
    assert world.comment_count(issue) == 1


def test_a_grant_spent_by_a_refused_write_cannot_be_reused():
    world, issue = world_with_issue()
    adapter = writer(world)
    world.scripted.append(
        httpx.Response(
            403,
            json={"message": "Resource not accessible by personal access token"},
            headers={"x-accepted-github-permissions": "issues=write"},
        )
    )
    first = grant(issue, "g-refused")
    with pytest.raises(PermissionDenied):
        adapter.post_comment(issue, "hi", marker=MARKER, grant=first)
    assert world.write_requests() == 1

    with pytest.raises(OperationNotEnabled, match="already used"):
        adapter.post_comment(issue, "hi", marker=MARKER, grant=first)
    assert world.write_requests() == 1 and world.comment_count(issue) == 0


def test_an_anonymous_write_is_refused_locally_and_keeps_the_grant():
    world, issue = world_with_issue()
    tokens = CountingTokens(value=None)
    adapter = writer(world, tokens=tokens)
    anon = grant(issue, "g-anon")

    for attempt in (1, 2):  # a missing token never burns the approval, however often it recurs
        with pytest.raises(AuthenticationFailed, match="anonymous"):
            adapter.post_comment(issue, "hi", marker=MARKER, grant=anon)
        assert world.requests == [] and tokens.calls == attempt

    tokens.value = FAKE_TOKEN  # the same grant, now credentialed: it still works, exactly once
    adapter.post_comment(issue, "hi", marker=MARKER, grant=anon)
    assert world.write_requests() == 1 and world.comment_count(issue) == 1
    with pytest.raises(OperationNotEnabled, match="already used"):
        adapter.post_comment(issue, "hi", marker=MARKER, grant=anon)
    assert world.write_requests() == 1


def test_a_failing_credential_source_is_scrubbed_and_keeps_the_grant():
    world, issue = world_with_issue()
    failing = [True]

    def source():
        if failing[0]:
            raise RuntimeError(f"vault said {FAKE_TOKEN}")
        return ResolvedToken(FAKE_TOKEN, "env:GITHUB_TOKEN")

    adapter = writer(world, tokens=source)
    once = grant(issue, "g-vault")
    with pytest.raises(AuthenticationFailed) as caught:
        adapter.post_comment(issue, "hi", marker=MARKER, grant=once)
    assert FAKE_TOKEN not in str(caught.value) and world.requests == []

    failing[0] = False
    adapter.post_comment(issue, "hi", marker=MARKER, grant=once)
    assert world.write_requests() == 1


def test_the_spent_grant_set_is_never_shown():
    world, issue = world_with_issue()
    adapter = writer(world)
    adapter.post_comment(issue, "hi", marker=MARKER, grant=grant(issue, "g-secretish"))
    assert "g-secretish" not in repr(adapter) and "g-secretish" not in repr(adapter._grants)


# ---- the send: one request, shaped exactly ----------------------------------------------------


@pytest.mark.parametrize("ghes", [False, True])
def test_the_write_is_one_post_of_the_marked_body_with_the_pinned_api_version(ghes):
    world, issue = world_with_issue(**(GHES if ghes else {}))
    adapter = writer(world)

    receipt = adapter.post_comment(
        issue, "We reproduced this.\n", marker=MARKER, grant=grant(issue)
    )

    (request,) = world.requests
    assert request.method == "POST"
    assert request.url.path == f"{world.api_path}/repos/acme/widgets/issues/1/comments"
    assert request.headers["x-github-api-version"] == API_VERSION
    assert request.headers["authorization"] in (f"token {FAKE_TOKEN}", f"Bearer {FAKE_TOKEN}")
    sent = json.loads(request.content)
    assert set(sent) == {"body"}
    assert decode_marker(sent["body"]) == ("We reproduced this.", MARKER)
    assert sent["body"].splitlines()[-1].startswith("<!-- bh:v1 hive=acme/widgets ")

    assert_contract_only(receipt)
    assert receipt.grant_id == "g-1" and receipt.operation is COMMENT and receipt.target == issue
    assert receipt.comment is not None and receipt.comment.issue == issue
    assert receipt.comment.body == "We reproduced this." and receipt.comment.marker == MARKER
    me = adapter.whoami()
    assert me is not None and receipt.comment.author is not None
    assert receipt.comment.author.key == me.key


def test_a_rate_limited_write_is_retried_within_budget_on_the_injected_sleep():
    """A rate-limit answer proves nothing was written, so the same write may be sent again."""
    world, issue = world_with_issue()
    adapter = writer(world, budget=RetryBudget(max_retries=3))
    world.throttle(requests=2, retry_after_seconds=7)

    adapter.post_comment(issue, "hi", marker=MARKER, grant=grant(issue))

    assert world.sleeps == [7.0, 7.0]  # waited exactly as told, on the injected sleep
    assert world.write_requests() == 3 and world.comment_count(issue) == 1


def test_a_rate_limited_write_beyond_budget_raises_rate_limited_and_writes_nothing():
    world, issue = world_with_issue()
    adapter = writer(world, budget=RetryBudget(max_retries=3, max_single_wait=timedelta(seconds=5)))
    world.throttle(requests=1, retry_after_seconds=30)

    with pytest.raises(RateLimited) as caught:
        adapter.post_comment(issue, "hi", marker=MARKER, grant=grant(issue))
    assert caught.value.retry_after == timedelta(seconds=30) and caught.value.kind == "throttle"
    assert world.sleeps == [] and world.write_requests() == 1 and world.comment_count(issue) == 0


# ---- an ambiguous outcome: WriteOutcomeUnknown, never retried ---------------------------------


@pytest.mark.parametrize("applied", [True, False])
def test_a_timeout_after_the_send_is_unknown_and_never_retried(applied):
    world, issue = world_with_issue()
    adapter = writer(world, budget=GENEROUS)
    world.fail_next_write_ambiguously(applied=applied)

    with pytest.raises(WriteOutcomeUnknown, match="timed out"):
        adapter.post_comment(issue, "hello", marker=MARKER, grant=grant(issue))

    assert [r.method for r in world.requests] == ["POST"]  # exactly one request, no retry
    assert world.sleeps == []
    assert world.comment_count(issue) == (1 if applied else 0)
    # The consumer's reconcile path: a listing finds the comment by its decoded marker.
    listed = adapter.list_comments(CommentQuery(issue=issue)).items
    assert [c.marker for c in listed] == ([MARKER] if applied else [])


@pytest.mark.parametrize(
    "answer",
    [
        httpx.Response(500, json={"message": "Server Error"}),
        httpx.Response(502, json={"message": "Bad Gateway"}),
        httpx.Response(503, json={"message": "unavailable"}, headers={"retry-after": "1"}),
        httpx.Response(504, text="gateway timeout"),
        httpx.Response(301, headers={"location": "https://api.github.com/repositories/1/x"}),
        httpx.Response(201, text="<html>accepted</html>"),
        httpx.Response(201, json={"id": "not-a-number"}),
        httpx.Response(202, json=[]),
    ],
    ids=["500", "502", "503-retry-after", "504-text", "301", "201-html", "201-bad-id", "202"],
)
def test_an_unconfirmed_answer_after_the_send_is_unknown_and_never_retried(answer):
    world, issue = world_with_issue()
    adapter = writer(world, budget=GENEROUS)
    world.scripted.append(answer)
    world.scripted.append(httpx.Response(418, json={"message": "a retry would land here"}))

    with pytest.raises(WriteOutcomeUnknown):
        adapter.post_comment(issue, "hello", marker=MARKER, grant=grant(issue))
    assert [r.method for r in world.requests] == ["POST"]  # never followed, never retried
    assert world.sleeps == []


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("read timed out"),
        httpx.WriteTimeout("write timed out"),
        httpx.RemoteProtocolError("server disconnected"),
        httpx.ReadError("connection reset"),
        httpx.ConnectError("connection refused"),
    ],
    ids=["read-timeout", "write-timeout", "disconnected", "reset", "connect"],
)
def test_a_request_cut_off_in_transit_is_unknown_and_never_retried(error):
    """Conservative by design: no answer means the write may exist (even a connect failure is
    reported as unknown; the consumer's reconcile settles it)."""
    sent: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        raise error

    sleeps: list[float] = []
    adapter = new_adapter(
        project=PROJECT,
        token_source=CountingTokens(),
        sleep=sleeps.append,
        clock=FakeClock().now,
        enabled=WRITE_ON,
        budget=GENEROUS,
        transport=httpx.MockTransport(handler),
    )
    issue = adapter.resolve("https://github.com/acme/widgets/issues/7")

    with pytest.raises(WriteOutcomeUnknown):
        adapter.post_comment(issue, "hello", marker=MARKER, grant=grant(issue))
    assert [r.method for r in sent] == ["POST"] and sleeps == []


# ---- refusal diagnostics against the recorded responses ---------------------------------------


def _load(name: str) -> dict[str, Any]:
    return json.loads((RECORDED / name).read_text(encoding="utf-8"))


REFUSALS = _load("comment_create_refusals.json")["interactions"]


def _replay_adapter(responses: list[dict[str, Any]], sent: list[httpx.Request], **kwargs):
    queue = list(responses)

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(request)
        if not queue:
            return httpx.Response(418, json={"message": "replay exhausted"})
        response = queue.pop(0)
        return httpx.Response(
            response["status"], headers=response["headers"], json=response["json"]
        )

    clock = FakeClock()
    return new_adapter(
        project=PROJECT,
        token_source=CountingTokens(),
        sleep=lambda _s: pytest.fail("no recorded refusal may be waited on"),
        clock=clock.now,
        enabled=WRITE_ON,
        budget=RetryBudget(max_retries=3, max_single_wait=timedelta(seconds=60)),
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def test_every_recorded_refusal_names_its_evidence_honestly():
    document = _load("comment_create_refusals.json")
    assert "NOTHING here was captured from a live write" in document["provenance"]
    live = [i["name"] for i in REFUSALS if i["provenance"].startswith("Live-observed")]
    assert live == ["bad-credentials", "invisible-to-the-token"]  # bh-mqvdq Evidence 10 only
    for interaction in REFUSALS:
        assert interaction["provenance"].startswith(("Live-observed", "Documented form only"))
        assert "re-verify on the first" in interaction["provenance"]
        assert interaction["request"]["method"] == "POST"


@pytest.mark.parametrize("interaction", REFUSALS, ids=lambda i: i["name"])
def test_each_recorded_refusal_raises_its_exact_diagnostic_after_one_request(interaction):
    sent: list[httpx.Request] = []
    adapter = _replay_adapter([interaction["response"]], sent)
    issue = adapter.resolve("https://github.com/acme/widgets/issues/7")
    expect = interaction["expect"]
    refused = grant(issue, "g-" + interaction["name"])

    with pytest.raises(port.TrackerError) as caught:
        adapter.post_comment(issue, "hello", marker=MARKER, grant=refused)

    error = caught.value
    assert type(error) is getattr(port, expect["error"])
    assert str(error) == expect["diagnostic"]
    assert "\n" not in str(error) and FAKE_TOKEN not in str(error)
    assert [(r.method, str(r.url)) for r in sent] == [("POST", interaction["request"]["url"])]
    if isinstance(error, PermissionDenied):
        assert error.operation is COMMENT and error.needed == expect.get("needed")
    if isinstance(error, RateLimited):
        assert error.kind == expect["kind"]
    if expect["error"] == "PayloadRejected":
        assert not isinstance(error, (PermissionDenied, AuthenticationFailed))  # was permitted
    # Whatever the refusal, the grant is spent and nothing more is sent.
    with pytest.raises(OperationNotEnabled):
        adapter.post_comment(issue, "hello", marker=MARKER, grant=refused)
    assert len(sent) == 1


def test_a_refusal_that_echoes_the_token_is_scrubbed():
    sent: list[httpx.Request] = []
    echo = {
        "status": 403,
        "headers": {"content-type": "application/json"},
        "json": {"message": f"token {FAKE_TOKEN} refused; also github_pat_{'R' * 40}"},
    }
    adapter = _replay_adapter([echo], sent)
    issue = adapter.resolve("https://github.com/acme/widgets/issues/7")

    with pytest.raises(PermissionDenied) as caught:
        adapter.post_comment(issue, "hello", marker=MARKER, grant=grant(issue))
    text = str(caught.value)
    assert FAKE_TOKEN not in text and "github_pat_" not in text and "***" in text


def test_the_recorded_accepted_write_round_trips_through_the_marker():
    document = _load("comment_create_accepted.json")
    assert document["provenance"].startswith("Synthetic")
    post, listing = document["interactions"]
    sent: list[httpx.Request] = []
    adapter = _replay_adapter([post["response"], listing["response"]], sent)
    issue = adapter.resolve("https://github.com/acme/widgets/issues/7")
    marker = OwnMarker(
        "acme/widgets", "6f1c2a7e-0b4d-4c5e-9a1b-2c3d4e5f6a7b", "ob1_01JC0000000000000000000000"
    )
    text = "Thanks for the report. A fix is merged and ships in the next release."

    receipt = adapter.post_comment(issue, text, marker=marker, grant=grant(issue))
    listed = adapter.list_comments(CommentQuery(issue=issue)).items

    assert json.loads(sent[0].content) == post["request"]["json"]  # byte-for-byte the recording
    assert [(r.method, str(r.url)) for r in sent] == [
        ("POST", post["request"]["url"]),
        ("GET", listing["request"]["url"]),
    ]
    assert receipt.comment is not None and receipt.comment.key.id == "701"
    assert receipt.comment.body == text and receipt.comment.marker == marker
    assert [c.key for c in listed if c.marker == marker] == [receipt.comment.key]
