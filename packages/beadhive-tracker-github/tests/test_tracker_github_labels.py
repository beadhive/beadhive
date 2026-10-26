"""The ``labels.change`` writer (W9), offline: refusals before any request, the wire form, the
single-use grant, the ambiguous outcome, rate limits and refusal diagnostics.

Every test sends through the ``MockTransport`` fake. Nothing here reaches GitHub, reads a clock or
sleeps; the retry wait goes to the fake's recorded ``sleep``. The credentials are synthetic.
"""

from __future__ import annotations

import threading
from datetime import timedelta

import httpx
import pytest

from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    AuthenticationFailed,
    IssueKey,
    LabelNotOwned,
    LabelOwnership,
    LabelWriter,
    NotVisible,
    Operation,
    OperationNotEnabled,
    PayloadRejected,
    PermissionDenied,
    ProjectRef,
    RateLimited,
    RetryBudget,
    TargetStateRefused,
    WriteGrant,
    WriteOutcomeUnknown,
)
from beadhive_plugins.credentials import ResolvedToken
from beadhive_tracker_github.adapter import build_adapter
from beadhive_tracker_github.adapter.labels import GrantLedger
from beadhive_tracker_github.adapter.transport import API_VERSION
from tracker_github_fake import EPOCH, FAKE_TOKEN, FakeGitHub, adapter_for

LABELS = Operation.LABELS_CHANGE
OWNED = LabelOwnership(("bh:",))


def setup(**kwargs):
    """A world with one issue carrying a human label and an owned one, and a labels-enabled
    adapter (reads on too, so the fake can be observed through the port)."""
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH, labels=("bug", "bh:triage"))
    adapter = adapter_for(world, enabled=READ_OPERATIONS | {LABELS}, **kwargs)
    return world, issue, adapter


def grant(issue: IssueKey, grant_id: str = "g-1", op: Operation = LABELS) -> WriteGrant:
    return WriteGrant(grant_id, op, issue)


_DEFAULT = object()


def change(adapter, issue, *, add=(), remove=(), ownership=OWNED, the_grant=_DEFAULT):
    return adapter.change_labels(
        issue,
        add=frozenset(add),
        remove=frozenset(remove),
        ownership=ownership,
        grant=grant(issue) if the_grant is _DEFAULT else the_grant,
    )


# ---- refusals: no request, no credential -----------------------------------------------------


def test_the_flag_off_refuses_before_any_request_or_credential():
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH, labels=("bh:triage",))
    resolved: list[int] = []

    def tokens():
        resolved.append(1)
        return ResolvedToken(world.token, "env:GITHUB_TOKEN")

    adapter = adapter_for(world, enabled=READ_OPERATIONS, tokens=tokens)

    with pytest.raises(OperationNotEnabled, match="labels.change is not enabled"):
        change(adapter, issue, add=["bh:accepted"])

    assert world.requests == [] and resolved == []
    assert world.server_labels(issue) == {"bh:triage"}


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "g-1",
        object(),
        WriteGrant("g-c", Operation.COMMENT_CREATE, IssueKey("#1", "x")),
    ],
    ids=["missing", "string", "object", "another-operation-and-issue"],
)
def test_a_missing_or_mismatched_grant_refuses_before_any_request(bad):
    world, issue, adapter = setup()
    with pytest.raises(OperationNotEnabled):
        change(adapter, issue, add=["bh:accepted"], the_grant=bad)
    assert world.requests == []


def test_a_grant_for_another_operation_or_issue_refuses_before_any_request():
    world, issue, adapter = setup()
    other = world.add_issue("another", at=EPOCH)
    for bad in (grant(issue, op=Operation.COMMENT_CREATE), grant(other)):
        with pytest.raises(OperationNotEnabled, match="does not cover"):
            change(adapter, issue, add=["bh:accepted"], the_grant=bad)
    assert world.requests == []
    # A refusal spends nothing: the same grant ids work on the issue they name.
    change(adapter, other, add=["bh:accepted"], the_grant=grant(other))
    assert world.write_requests() == 1


def test_a_spent_grant_is_refused_without_a_request_and_a_fresh_one_works():
    world, issue, adapter = setup()
    change(adapter, issue, add=["bh:accepted"], the_grant=grant(issue, "g-once"))
    sent = len(world.requests)
    assert sent == 1

    with pytest.raises(OperationNotEnabled, match="already used"):
        change(adapter, issue, add=["bh:again"], the_grant=grant(issue, "g-once"))

    assert len(world.requests) == sent
    assert "bh:again" not in world.server_labels(issue)
    change(adapter, issue, add=["bh:again"], the_grant=grant(issue, "g-fresh"))
    assert "bh:again" in world.server_labels(issue)


@pytest.mark.parametrize(
    ("add", "remove"),
    [
        (["wontfix"], []),
        ([], ["bug"]),
        (["bh:ok", "wontfix"], ["bh:triage"]),  # one foreign label refuses the whole change
        (["BH:upper"], []),  # prefixes are case-sensitive: no widening by spelling
    ],
)
def test_a_label_outside_the_owned_prefixes_is_refused_before_any_request(add, remove):
    world, issue, adapter = setup()
    with pytest.raises(LabelNotOwned):
        change(adapter, issue, add=add, remove=remove)
    assert world.requests == []
    assert world.server_labels(issue) == {"bug", "bh:triage"}  # human labels untouched
    change(adapter, issue, add=["bh:ok"])  # the refusal did not spend the grant
    assert world.write_requests() == 1


def test_an_empty_ownership_owns_nothing():
    world, issue, adapter = setup()
    with pytest.raises(LabelNotOwned):
        change(adapter, issue, add=["bh:x"], ownership=LabelOwnership(()))
    assert world.requests == []


def test_the_callers_ownership_can_narrow_but_never_widen_the_configured_prefixes():
    world, issue, adapter = setup(label_prefixes=("bh:",))
    everything = LabelOwnership(("",))  # a consumer bug: owns every label
    with pytest.raises(LabelNotOwned, match="wontfix"):
        change(adapter, issue, add=["wontfix"], ownership=everything)
    assert world.requests == []
    narrower = LabelOwnership(("bh:triage",))
    with pytest.raises(LabelNotOwned, match="bh:accepted"):
        change(adapter, issue, add=["bh:accepted"], ownership=narrower)
    assert world.requests == []
    change(adapter, issue, add=["bh:accepted"], ownership=OWNED)
    assert world.write_requests() == 1


@pytest.mark.parametrize(
    ("add", "remove"),
    [([], []), (["bh:a"], ["bh:a"]), ([""], []), (["  "], [])],
    ids=["nothing", "both-add-and-remove", "empty-name", "blank-name"],
)
def test_a_malformed_change_is_refused_before_any_request_and_spends_nothing(add, remove):
    world, issue, adapter = setup()
    with pytest.raises(ValueError):
        change(adapter, issue, add=add, remove=remove)
    assert world.requests == []
    change(adapter, issue, add=["bh:ok"])
    assert world.write_requests() == 1


def test_another_repositorys_issue_is_refused_before_any_request():
    world, issue, adapter = setup()
    foreign = IssueKey("#1", "https://github.com/someone-else/elsewhere/issues/1")
    with pytest.raises(Exception) as caught:  # noqa: PT011 - the exact port error is asserted
        change(adapter, foreign, add=["bh:x"], the_grant=grant(foreign))
    assert world.requests == []
    assert not isinstance(caught.value, (LabelNotOwned, WriteOutcomeUnknown))


def test_no_credential_refuses_before_any_request_and_does_not_spend_the_grant():
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH, labels=("bh:triage",))
    holder: list[ResolvedToken | None] = [None]
    adapter = adapter_for(world, enabled={LABELS}, tokens=lambda: holder[0])

    with pytest.raises(AuthenticationFailed, match="needs a credential"):
        change(adapter, issue, add=["bh:x"])
    assert world.requests == []

    holder[0] = ResolvedToken(world.token, "env:GITHUB_TOKEN")  # the same grant, now credentialed
    change(adapter, issue, add=["bh:x"])
    assert world.write_requests() == 1


def test_a_failing_credential_source_is_scrubbed_and_sends_nothing():
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH)

    def boom():
        raise RuntimeError(f"vault said {FAKE_TOKEN}")

    adapter = adapter_for(world, enabled={LABELS}, tokens=boom)
    with pytest.raises(AuthenticationFailed) as caught:
        change(adapter, issue, add=["bh:x"])
    assert FAKE_TOKEN not in str(caught.value) and world.requests == []


# ---- the wire form and the receipt -----------------------------------------------------------


def test_add_and_remove_become_one_post_and_one_delete_and_keep_human_labels():
    world, issue, adapter = setup()
    world.add_human_label(issue, "needs-love")

    receipt = change(adapter, issue, add=["bh:accepted"], remove=["bh:triage"])

    assert receipt.labels_after == {"bug", "needs-love", "bh:accepted"}
    assert receipt.labels_after == world.server_labels(issue)
    assert (receipt.grant_id, receipt.operation, receipt.target) == ("g-1", LABELS, issue)
    post, delete = world.requests
    assert (post.method, post.url.path) == ("POST", "/repos/acme/widgets/issues/1/labels")
    assert post.content == b'{"labels":["bh:accepted"]}'
    assert (delete.method, delete.url.path) == (
        "DELETE",
        "/repos/acme/widgets/issues/1/labels/bh:triage",
    )
    for request in world.requests:
        assert request.headers["x-github-api-version"] == API_VERSION
        assert request.headers["authorization"].endswith(FAKE_TOKEN)


def test_a_remove_only_change_sends_one_delete_and_an_add_only_change_one_post():
    world, issue, adapter = setup()
    change(adapter, issue, remove=["bh:triage"], the_grant=grant(issue, "g-r"))
    change(adapter, issue, add=["bh:b", "bh:a"], the_grant=grant(issue, "g-a"))
    assert [r.method for r in world.requests] == ["DELETE", "POST"]
    assert world.requests[1].content == b'{"labels":["bh:a","bh:b"]}'  # sorted, deterministic
    assert world.server_labels(issue) == {"bug", "bh:a", "bh:b"}


def test_a_label_name_is_percent_encoded_in_the_delete_path():
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH, labels=("bh:needs review/now",))
    adapter = adapter_for(world, enabled={LABELS})
    receipt = change(adapter, issue, remove=["bh:needs review/now"])
    (delete,) = world.requests
    assert delete.url.raw_path.endswith(b"/labels/bh%3Aneeds%20review%2Fnow")
    assert receipt.labels_after == frozenset()


def test_adding_an_already_present_label_is_idempotent():
    world, issue, adapter = setup()
    receipt = change(adapter, issue, add=["bh:triage"])
    assert receipt.labels_after == {"bug", "bh:triage"}


def test_removing_an_absent_label_is_the_desired_state_and_reads_the_issue_back():
    world, issue, adapter = setup()
    receipt = change(adapter, issue, add=["bh:accepted"], remove=["bh:gone"])
    assert receipt.labels_after == {"bug", "bh:triage", "bh:accepted"}
    assert [r.method for r in world.requests] == ["POST", "DELETE", "GET"]


def test_removing_from_an_invisible_issue_is_not_mistaken_for_an_absent_label():
    world, issue, adapter = setup()
    world.delete_issue(issue)
    with pytest.raises(NotVisible):
        change(adapter, issue, remove=["bh:triage"])
    # DELETE answered 410; the read-back of the issue answers 410 too, so it is the issue.
    assert [r.method for r in world.requests] == ["DELETE", "GET"]
    assert world.write_requests() == 1


def test_the_grant_ledger_is_atomic_across_threads():
    ledger = GrantLedger()
    barrier = threading.Barrier(8)
    won: list[int] = []

    def attempt(i: int) -> None:
        barrier.wait()
        try:
            ledger.spend("g-race")
            won.append(i)
        except OperationNotEnabled:
            pass

    threads = [threading.Thread(target=attempt, args=(i,)) for i in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(won) == 1


# ---- the ambiguous outcome -------------------------------------------------------------------


@pytest.mark.parametrize("applied", [True, False])
def test_an_ambiguous_write_is_unknown_never_retried_and_spends_the_grant(applied):
    world, issue, adapter = setup()
    world.fail_next_write_ambiguously(applied=applied)

    with pytest.raises(WriteOutcomeUnknown):
        change(adapter, issue, add=["bh:accepted"], the_grant=grant(issue, "g-amb"))

    assert world.write_requests() == 1 and world.sleeps == []  # not retried, not slept
    assert ("bh:accepted" in world.server_labels(issue)) is applied
    with pytest.raises(OperationNotEnabled, match="already used"):
        change(adapter, issue, add=["bh:accepted"], the_grant=grant(issue, "g-amb"))
    assert world.write_requests() == 1
    change(adapter, issue, add=["bh:accepted"], the_grant=grant(issue, "g-retry"))  # fresh grant
    assert "bh:accepted" in world.server_labels(issue)


def test_an_ambiguous_add_sends_no_remove_afterwards():
    world, issue, adapter = setup()
    world.fail_next_write_ambiguously(applied=True)
    with pytest.raises(WriteOutcomeUnknown):
        change(adapter, issue, add=["bh:accepted"], remove=["bh:triage"])
    assert [r.method for r in world.requests] == ["POST"]


def test_an_ambiguous_remove_after_a_successful_add_is_unknown_and_not_retried():
    world, issue, adapter = setup()
    world.scripted.extend(
        [httpx.Response(200, json=[{"name": "bh:accepted"}]), httpx.Response(503)]
    )
    with pytest.raises(WriteOutcomeUnknown):
        change(adapter, issue, add=["bh:accepted"], remove=["bh:triage"])
    assert [r.method for r in world.requests] == ["POST", "DELETE"] and world.sleeps == []


@pytest.mark.parametrize("error", [httpx.ReadTimeout("slow"), httpx.ConnectError("reset")])
def test_a_timeout_or_dropped_connection_is_unknown_and_not_retried(error):
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH)
    sent: list[httpx.Request] = []

    def handler(request):
        sent.append(request)
        raise error

    adapter = adapter_for(world, enabled={LABELS}, transport=httpx.MockTransport(handler))
    with pytest.raises(WriteOutcomeUnknown):
        change(adapter, issue, add=["bh:x"])
    assert len(sent) == 1 and world.sleeps == []


@pytest.mark.parametrize(
    "reply",
    [httpx.Response(200, content=b"not json"), httpx.Response(200, json={"id": 1})],
    ids=["not-json", "not-a-label-list"],
)
def test_an_unreadable_success_answer_is_unknown(reply):
    world, issue, adapter = setup()
    world.scripted.append(reply)
    with pytest.raises(WriteOutcomeUnknown):
        change(adapter, issue, add=["bh:x"])
    assert world.write_requests() == 1


# ---- rate limits: nothing was done, so the request is retried on the injected sleep ----------


def test_a_rate_limited_write_waits_on_the_injected_sleep_then_succeeds():
    world, issue, adapter = setup()
    world.throttle(2, 7)

    receipt = change(adapter, issue, add=["bh:accepted"])

    assert world.sleeps == [7.0, 7.0]
    assert [r.method for r in world.requests] == ["POST"] * 3
    assert "bh:accepted" in receipt.labels_after


def test_a_rate_limit_past_the_budget_raises_without_waiting_and_without_writing():
    budget = RetryBudget(max_retries=3, max_single_wait=timedelta(seconds=5))
    world, issue, adapter = setup(budget=budget)
    world.throttle(5, 30)

    with pytest.raises(RateLimited) as caught:
        change(adapter, issue, add=["bh:accepted"])

    assert caught.value.retry_after == timedelta(seconds=30) and world.sleeps == []
    assert world.write_requests() == 1
    assert "bh:accepted" not in world.server_labels(issue)


# ---- refusal diagnostics: scrubbed, typed ----------------------------------------------------


@pytest.mark.parametrize(
    ("status", "body", "headers", "expected"),
    [
        (401, {"message": "Bad credentials"}, {}, AuthenticationFailed),
        (
            403,
            {"message": "Resource not accessible by personal access token"},
            {"x-accepted-github-permissions": "issues=write"},
            PermissionDenied,
        ),
        (403, {"message": "Repository was archived so is read-only."}, {}, TargetStateRefused),
        (404, {"message": "Not Found"}, {}, NotVisible),
        (422, {"message": "Validation Failed"}, {}, PayloadRejected),
    ],
)
def test_a_refused_write_is_its_typed_scrubbed_port_error_and_not_retried(
    status, body, headers, expected
):
    world, issue, adapter = setup()
    body = {**body, "documentation_url": FAKE_TOKEN}  # a planted secret in the upstream body
    world.scripted.append(httpx.Response(status, json=body, headers=headers))

    with pytest.raises(expected) as caught:
        change(adapter, issue, add=["bh:accepted"])

    assert FAKE_TOKEN not in str(caught.value) and "\n" not in str(caught.value)
    assert world.write_requests() == 1 and world.sleeps == []
    assert "bh:accepted" not in world.server_labels(issue)


def test_a_permission_refusal_names_the_needed_permission_on_the_first_gated_write():
    world, issue, adapter = setup()
    world.scripted.append(
        httpx.Response(
            403,
            json={"message": "Resource not accessible by personal access token"},
            headers={"x-accepted-github-permissions": "issues=write; pull_requests=write"},
        )
    )
    with pytest.raises(PermissionDenied) as caught:
        change(adapter, issue, add=["bh:accepted"])
    assert "issues=write" in str(caught.value)
    assert caught.value.operation is LABELS


def test_the_credential_never_appears_in_a_repr_or_a_receipt():
    world, issue, adapter = setup()
    receipt = change(adapter, issue, add=["bh:accepted"])
    for shown in (repr(adapter), repr(receipt), repr(adapter.settings_summary)):
        assert FAKE_TOKEN not in shown


# ---- settings and the writer's presence ------------------------------------------------------


def _from_settings(world, labels):
    settings = {"enabled": True, "write": {"labels": labels}}
    return build_adapter(
        settings,
        lambda: ResolvedToken(world.token, "env:GH_TOKEN"),
        project=ProjectRef("github", f"{world.owner}/{world.repo}"),
        sleep=world.sleep,
        clock=world.clock.now,
        transport=world.transport(),
    )


def test_the_configured_prefixes_bound_every_write_of_a_settings_built_adapter():
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH, labels=("bh:triage",))
    adapter = _from_settings(world, {"enabled": True, "owned_prefixes": ["bh:"]})

    assert LABELS in adapter.enabled and isinstance(adapter, LabelWriter)
    with pytest.raises(LabelNotOwned):
        change(adapter, issue, add=["other:x"], ownership=LabelOwnership(("",)))
    assert world.requests == []
    receipt = change(adapter, issue, add=["bh:accepted"])
    assert receipt.labels_after == {"bh:triage", "bh:accepted"}


@pytest.mark.parametrize(
    "labels",
    [
        {"enabled": True, "owned_prefixes": []},
        {"enabled": True},
        {"enabled": False, "owned_prefixes": ["bh:"]},
        {},
    ],
    ids=["empty-prefixes", "no-prefixes", "flag-off", "default"],
)
def test_labels_change_is_not_enabled_without_the_flag_and_a_prefix(labels):
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH)
    adapter = _from_settings(world, labels)

    assert LABELS not in adapter.enabled
    with pytest.raises(OperationNotEnabled):
        change(adapter, issue, add=["bh:x"])
    assert world.requests == []


def test_a_malformed_prefix_list_is_refused_when_the_adapter_is_built():
    world = FakeGitHub()
    with pytest.raises(ValueError, match="owned_prefixes"):
        _from_settings(world, {"enabled": True, "owned_prefixes": ["bh:", 7]})
