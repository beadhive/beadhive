"""What a command-scoped adapter keeps for the life of ONE command (bh-vy7dx, bh-n0h15), offline.

* one HTTP client (and its connection pool) per kind for a whole batch, where the default builds
  one per operation; the token is still resolved once per operation and sent per request, so a
  rotated token is picked up and no token outlives its operation;
* the organization's issue-field definitions read once for a batch of ``issue.fields`` writes,
  re-read once (and the write retried once) when the memo proves stale, and never kept by an
  adapter that is not command-scoped (the host daemon's).

Every test sends through the ``MockTransport`` fake; nothing reaches GitHub, reads a clock or
sleeps. The credentials are synthetic.
"""

from __future__ import annotations

import httpx
import pytest

from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    IssueQuery,
    NotVisible,
    Operation,
    PayloadRejected,
    WriteGrant,
)
from beadhive_plugins.credentials import ResolvedToken
from beadhive_tracker_github.adapter.transport import Transport
from tracker_github_fake import EPOCH, FakeGitHub, adapter_for

FIELDS = Operation.ISSUE_FIELDS
DEFINITIONS = "/orgs/acme/issue-fields"


@pytest.fixture
def clients(monkeypatch):
    """Every githubkit client the transport builds, in order."""
    built: list[tuple[bool, object]] = []
    real = Transport._client

    def counting(self, token, *, write=False, auth=None):
        client = real(self, token, write=write, auth=auth)
        built.append((write, client))
        return client

    monkeypatch.setattr(Transport, "_client", counting)
    return built


def world_with_issues(count: int = 3, **kwargs):
    world = FakeGitHub()
    issues = [world.add_issue(f"thread {n}", at=EPOCH) for n in range(count)]
    world.set_write_time(EPOCH)
    return world, issues


def write(adapter, issue, value="Low", grant_id=None):
    grant = WriteGrant(grant_id or f"g-{issue.identifier}-{value}", FIELDS, issue)
    return adapter.set_issue_fields(issue, {"Priority": value}, grant=grant)


def reads(world: FakeGitHub) -> list[httpx.Request]:
    return [r for r in world.requests if r.method == "GET" and r.url.path == DEFINITIONS]


def posts(world: FakeGitHub) -> list[httpx.Request]:
    return [r for r in world.requests if r.method == "POST"]


# ---- one client per command ------------------------


def test_a_command_scoped_batch_builds_one_client_and_a_default_adapter_one_per_operation(
    clients,
):
    world, issues = world_with_issues(4)
    scoped = adapter_for(world, enabled=READ_OPERATIONS | {FIELDS}, command_scoped=True)
    for issue in issues:
        write(scoped, issue)
    assert [w for w, _ in clients] == [True]  # one write client for four writes
    assert len(posts(world)) == 4

    clients.clear()
    plain = adapter_for(world, enabled=READ_OPERATIONS | {FIELDS})
    for issue in issues:
        write(plain, issue, "High")
    assert len(clients) == 4  # the daemon's shape: a client per operation, nothing kept


def test_reads_and_writes_share_one_client_each_and_close_releases_them(clients):
    world, (issue, *_) = world_with_issues(2)
    adapter = adapter_for(world, enabled=READ_OPERATIONS | {FIELDS}, command_scoped=True)
    adapter.list_issues(IssueQuery())
    adapter.list_issues(IssueQuery())
    write(adapter, issue)
    assert [w for w, _ in clients] == [False, True]  # read client, write client: one each

    adapter.close()
    adapter.list_issues(IssueQuery())
    assert [w for w, _ in clients] == [False, True, False]  # a closed adapter rebuilds, lazily


def test_the_shared_write_client_still_never_follows_a_redirect(clients):
    world, (issue, *_) = world_with_issues(1)
    adapter = adapter_for(world, enabled=READ_OPERATIONS | {FIELDS}, command_scoped=True)
    write(adapter, issue)
    (_, client), *_rest = clients
    with client.get_sync_client() as http:
        assert http.follow_redirects is False


# ---- the token contract is unchanged ------------------------


def test_the_token_is_still_resolved_per_operation_and_sent_per_request_on_a_shared_client(
    clients,
):
    world, issues = world_with_issues(3)
    calls: list[str] = []

    def rotating() -> ResolvedToken:
        calls.append(f"ghp_rotated{len(calls)}")
        world.token = calls[-1]  # the fake accepts the token currently valid
        return ResolvedToken(calls[-1], "env:GITHUB_TOKEN")

    adapter = adapter_for(
        world, enabled=READ_OPERATIONS | {FIELDS}, command_scoped=True, tokens=rotating
    )
    for issue in issues:
        write(adapter, issue)

    assert len(clients) == 1  # one client ...
    assert len(calls) == 3  # ... and one resolution per operation, never cached
    sent = [r.headers["authorization"] for r in posts(world)]
    assert sent == [f"token {t}" for t in calls]  # each write carried its own operation's token


def test_no_token_outlives_its_operation_and_an_anonymous_one_sends_none(clients):
    world, _ = world_with_issues(1)
    tokens = iter([ResolvedToken(world.token, "env:GITHUB_TOKEN"), None])
    adapter = adapter_for(
        world, enabled=READ_OPERATIONS, command_scoped=True, tokens=lambda: next(tokens)
    )
    adapter.list_issues(IssueQuery())
    adapter.list_issues(IssueQuery())  # anonymous, on the same client

    assert len(clients) == 1
    signed, anonymous = world.requests[0], world.requests[1]
    assert signed.headers["authorization"] == f"token {world.token}"
    assert "authorization" not in anonymous.headers
    assert adapter._transport._held.current() is None


# ---- the field definitions are read once per command ------------------------


def test_a_field_batch_reads_the_definitions_once_per_command():
    world, issues = world_with_issues(5)
    adapter = adapter_for(world, enabled=READ_OPERATIONS | {FIELDS}, command_scoped=True)
    for issue in issues:
        write(adapter, issue)
    assert len(reads(world)) == 1
    assert len(posts(world)) == 5
    assert all(world.server_issue_fields(i) == {"Priority": "Low"} for i in issues)


def test_an_adapter_that_is_not_command_scoped_reads_them_every_write():
    world, issues = world_with_issues(3)
    adapter = adapter_for(world, enabled=READ_OPERATIONS | {FIELDS})  # the daemon's shape
    for issue in issues:
        write(adapter, issue)
    assert len(reads(world)) == 3  # nothing is kept across operations


def test_close_drops_the_memo():
    world, (first, second, *_) = world_with_issues(2)
    adapter = adapter_for(world, enabled=READ_OPERATIONS | {FIELDS}, command_scoped=True)
    write(adapter, first)
    adapter.close()
    write(adapter, second)
    assert len(reads(world)) == 2


# ---- staleness: unknown field or option ------------------------


def test_an_option_unknown_to_the_memo_re_reads_once_and_then_writes():
    world, (first, second, *_) = world_with_issues(2)
    adapter = adapter_for(world, enabled=READ_OPERATIONS | {FIELDS}, command_scoped=True)
    write(adapter, first)
    world.seed_issue_field("Priority", ("Low", "Medium", "High", "Critical"))  # added since
    write(adapter, second, "Critical")

    assert len(reads(world)) == 2  # the memo missed the option: dropped, re-read once
    assert world.server_issue_fields(second) == {"Priority": "Critical"}
    write(adapter, first, "Medium")
    assert len(reads(world)) == 2  # and the fresh read is the memo again


def test_a_field_still_unknown_after_the_re_read_is_the_normal_error_and_keeps_the_grant():
    world, (first, second, *_) = world_with_issues(2)
    adapter = adapter_for(world, enabled=READ_OPERATIONS | {FIELDS}, command_scoped=True)
    write(adapter, first)
    kept = WriteGrant("g-kept", FIELDS, second)
    with pytest.raises(PayloadRejected):
        adapter.set_issue_fields(second, {"Severity": "High"}, grant=kept)

    assert len(reads(world)) == 2  # one re-read, not a loop
    assert len(posts(world)) == 1  # nothing was sent for the refused write
    world.seed_issue_field("Severity", ("High",))
    adapter.set_issue_fields(second, {"Severity": "High"}, grant=kept)  # the grant was kept


def test_a_fresh_read_that_refuses_is_not_read_again():
    world, (issue, *_) = world_with_issues(1)
    adapter = adapter_for(world, enabled=READ_OPERATIONS | {FIELDS}, command_scoped=True)
    with pytest.raises(PayloadRejected):
        write(adapter, issue, "Whenever")
    assert len(reads(world)) == 1  # the answer just read is authoritative


# ---- staleness: 404 / 422 from the write ------------------------


@pytest.mark.parametrize(
    ("status", "message"), [(422, "Validation Failed"), (404, "Not Found")], ids=["422", "404"]
)
def test_a_rejected_write_on_remembered_ids_re_reads_once_and_retries_once(status, message):
    world, (first, second, *_) = world_with_issues(2)
    adapter = adapter_for(world, enabled=READ_OPERATIONS | {FIELDS}, command_scoped=True)
    write(adapter, first)
    world.requests.clear()
    world.scripted.append(httpx.Response(status, json={"message": message}))  # the next POST

    receipt = write(adapter, second)

    assert [r.method for r in world.requests] == ["POST", "GET", "POST"]
    assert receipt.fields_after == (("Priority", "Low"),)
    assert world.server_issue_fields(second) == {"Priority": "Low"}


@pytest.mark.parametrize(("status", "error"), [(422, PayloadRejected), (404, NotVisible)])
def test_a_second_refusal_is_the_normal_error_and_the_grant_stays_spent(status, error):
    world, (first, second, *_) = world_with_issues(2)
    inner = world.transport()

    def refusing(request: httpx.Request) -> httpx.Response:
        if request.method == "POST" and world.refuse_posts:
            world.requests.append(request)
            return httpx.Response(status, json={"message": "no"})
        return inner.handle_request(request)

    world.refuse_posts = False
    adapter = adapter_for(
        world,
        enabled=READ_OPERATIONS | {FIELDS},
        command_scoped=True,
        transport=httpx.MockTransport(refusing),
    )
    write(adapter, first)
    world.requests.clear()
    world.refuse_posts = True

    spent = WriteGrant("g-spent", FIELDS, second)
    with pytest.raises(error):
        adapter.set_issue_fields(second, {"Priority": "Low"}, grant=spent)

    assert [r.method for r in world.requests] == ["POST", "GET", "POST"]  # once, then the error
    world.refuse_posts = False
    with pytest.raises(Exception, match="spent|already"):  # noqa: PT011 - the ledger's refusal
        adapter.set_issue_fields(second, {"Priority": "Low"}, grant=spent)


def test_a_rejected_write_after_a_fresh_read_is_not_retried():
    world, (issue, *_) = world_with_issues(1)
    adapter = adapter_for(world, enabled=READ_OPERATIONS | {FIELDS}, command_scoped=True)
    world.scripted.append(httpx.Response(200, json=world._field_rows()))  # the one read
    world.scripted.append(httpx.Response(422, json={"message": "Validation Failed"}))
    with pytest.raises(PayloadRejected):
        write(adapter, issue)
    assert [r.method for r in world.requests] == ["GET", "POST"]  # no re-read, no retry
