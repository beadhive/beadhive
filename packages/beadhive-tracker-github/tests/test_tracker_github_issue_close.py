"""The ``issue.close`` writer (``bh-pr5ll``), offline: refusals before any request, the wire form
(state and reason only, never labels), the single-use grant, the ambiguous outcome and the probe.

Every test sends through the ``MockTransport`` fake. Nothing here reaches GitHub, reads a clock or
sleeps; the retry wait goes to the fake's recorded ``sleep``. The credentials are synthetic.
"""

from __future__ import annotations

import json

import httpx
import pytest

from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    AuthenticationFailed,
    CloseReason,
    IssueCloser,
    IssueKey,
    IssueState,
    NotVisible,
    Operation,
    OperationNotEnabled,
    ProjectRef,
    TargetStateRefused,
    TrackerError,
    Tri,
    WriteGrant,
    WriteOutcomeUnknown,
)
from beadhive_plugins.credentials import ResolvedToken
from beadhive_tracker_github.adapter import IssueClosing, adapter_class, build_adapter
from beadhive_tracker_github.adapter.closing import close_payload
from beadhive_tracker_github.adapter.identity import RepositoryFacts, probe_operation
from beadhive_tracker_github.adapter.transport import API_VERSION
from tracker_github_fake import EPOCH, FAKE_TOKEN, FakeGitHub, adapter_for

CLOSE = Operation.ISSUE_CLOSE


def setup(**kwargs):
    """A world with one open issue carrying a human label and an owned one, and a close-enabled
    adapter (reads on too, so the fake can be observed through the port)."""
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH, labels=("bug", "bh:triage"))
    world.set_write_time(EPOCH)
    adapter = adapter_for(world, enabled=READ_OPERATIONS | {CLOSE}, **kwargs)
    return world, issue, adapter


def grant(issue: IssueKey, grant_id: str = "g-1", op: Operation = CLOSE) -> WriteGrant:
    return WriteGrant(grant_id, op, issue)


def close(adapter, issue, reason=CloseReason.COMPLETED, the_grant=None, duplicate_of=None):
    return adapter.close_issue(
        issue,
        reason=reason,
        grant=grant(issue) if the_grant is None else the_grant,
        duplicate_of=duplicate_of,
    )


def _row(world: FakeGitHub, issue: IssueKey) -> dict:
    return next(row for row in world.issues if f"#{row['number']}" == issue.identifier)


# ---- composition and refusals: no request, no credential --------------------------------------


def test_the_flag_off_composes_no_closer():
    world = FakeGitHub()
    adapter = adapter_for(world, enabled=READ_OPERATIONS)
    assert not isinstance(adapter, IssueCloser) and not hasattr(adapter, "close_issue")
    assert CLOSE in adapter.describe().supported  # implemented, just not enabled
    assert world.requests == []


def test_the_flag_folds_from_write_issue_close_enabled_and_defaults_off():
    world = FakeGitHub()
    project = ProjectRef("github", f"{world.owner}/{world.repo}")

    def build(settings):
        return build_adapter(
            settings,
            lambda: ResolvedToken(world.token, "env:GITHUB_TOKEN"),
            project=project,
            sleep=world.sleep,
            transport=world.transport(),
        )

    assert not isinstance(build({}), IssueCloser)
    assert not isinstance(build({"write": {"issue_close": {"enabled": False}}}), IssueCloser)
    on = build({"write": {"issue_close": {"enabled": True}}})
    assert isinstance(on, IssueCloser) and CLOSE in on.enabled
    with pytest.raises(ValueError, match="write.issue_close.enabled"):
        build({"write": {"issue_close": {"enabled": "yes"}}})


def test_a_composed_closer_still_refuses_when_its_flag_is_off():
    """Defence in depth: the mixin re-checks the adapter's own enabled set."""
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH)
    resolved: list[int] = []

    def tokens():
        resolved.append(1)
        return ResolvedToken(world.token, "env:GITHUB_TOKEN")

    adapter = adapter_class({CLOSE})(
        project=ProjectRef("github", f"{world.owner}/{world.repo}"),
        token_source=tokens,
        sleep=world.sleep,
        clock=world.clock.now,
        enabled=READ_OPERATIONS,  # issue.close is NOT enabled
        transport=world.transport(),
    )
    assert isinstance(adapter, IssueClosing)
    with pytest.raises(OperationNotEnabled, match="issue.close is not enabled"):
        close(adapter, issue)
    assert world.requests == [] and resolved == []


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "g-1",
        WriteGrant("g-c", Operation.COMMENT_CREATE, IssueKey("#1", "x")),
        WriteGrant("g-p", CLOSE, ProjectRef("github", "acme/widgets")),
    ],
    ids=["missing", "string", "another-operation", "repository-scoped"],
)
def test_a_missing_or_mismatched_grant_refuses_before_any_request(bad):
    world, issue, adapter = setup()
    with pytest.raises(OperationNotEnabled):
        adapter.close_issue(issue, reason=CloseReason.COMPLETED, grant=bad)
    assert world.requests == []


def test_a_reason_outside_the_port_vocabulary_refuses_and_spends_nothing():
    world, issue, adapter = setup()
    with pytest.raises(ValueError, match="CloseReason"):
        adapter.close_issue(issue, reason="wontfix", grant=grant(issue))  # type: ignore[arg-type]
    assert world.requests == []
    close(adapter, issue)  # the same grant still works
    assert world.write_requests() == 1


def test_another_repositorys_issue_is_refused_before_any_request():
    world, issue, adapter = setup()
    foreign = IssueKey("#1", "https://github.com/someone-else/elsewhere/issues/1")
    with pytest.raises(Exception) as caught:  # noqa: PT011 - the exact port error is asserted
        close(adapter, foreign, the_grant=grant(foreign))
    assert world.requests == []
    assert not isinstance(caught.value, WriteOutcomeUnknown)


def test_no_credential_refuses_before_any_request_and_keeps_the_grant():
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH)
    world.set_write_time(EPOCH)
    holder: list[ResolvedToken | None] = [None]
    adapter = adapter_for(world, enabled={CLOSE}, tokens=lambda: holder[0])
    once = grant(issue, "g-anon")
    with pytest.raises(AuthenticationFailed, match="needs a credential"):
        close(adapter, issue, the_grant=once)
    assert world.requests == []
    holder[0] = ResolvedToken(world.token, "env:GITHUB_TOKEN")
    close(adapter, issue, the_grant=once)
    assert world.write_requests() == 1
    with pytest.raises(OperationNotEnabled, match="already used"):
        close(adapter, issue, the_grant=once)
    assert world.write_requests() == 1


# ---- the wire form and the receipt ----------------------------------------------------------


@pytest.mark.parametrize(
    ("reason", "wire"),
    [
        (CloseReason.COMPLETED, "completed"),
        (CloseReason.NOT_PLANNED, "not_planned"),
        (CloseReason.DUPLICATE, "duplicate"),
    ],
)
def test_one_patch_with_the_state_and_its_reason_and_never_the_labels(reason, wire):
    world, issue, adapter = setup()
    world.add_human_label(issue, "needs-love")

    receipt = close(adapter, issue, reason)

    (patch,) = world.requests
    assert (patch.method, patch.url.path) == ("PATCH", "/repos/acme/widgets/issues/1")
    assert json.loads(patch.content) == {"state": "closed", "state_reason": wire}
    assert "labels" not in json.loads(patch.content)
    assert patch.headers["x-github-api-version"] == API_VERSION
    assert patch.headers["authorization"].endswith(FAKE_TOKEN)
    assert (receipt.grant_id, receipt.operation, receipt.target) == ("g-1", CLOSE, issue)
    assert receipt.state_after is IssueState.CLOSED and receipt.closed_reason == wire
    assert world.server_labels(issue) == {"bug", "bh:triage", "needs-love"}
    assert _row(world, issue)["state"] == "closed"


def test_the_payload_is_built_from_the_reason_alone():
    assert close_payload(CloseReason.NOT_PLANNED) == {
        "state": "closed",
        "state_reason": "not_planned",
    }


def test_closing_an_already_closed_issue_succeeds_with_the_new_reason():
    world, issue, adapter = setup()
    _row(world, issue).update(state="closed", state_reason="completed")
    receipt = close(adapter, issue, CloseReason.DUPLICATE)
    assert receipt.state_after is IssueState.CLOSED and receipt.closed_reason == "duplicate"
    assert world.write_requests() == 1


def test_a_deleted_issue_is_refused_and_spends_the_grant():
    world, issue, adapter = setup()
    world.delete_issue(issue)
    with pytest.raises(NotVisible):
        close(adapter, issue, the_grant=grant(issue, "g-gone"))
    assert world.write_requests() == 1
    with pytest.raises(OperationNotEnabled, match="already used"):
        close(adapter, issue, the_grant=grant(issue, "g-gone"))


def test_an_archived_repository_refuses_the_close():
    world, issue, adapter = setup()
    world.repo_meta["archived"] = True
    with pytest.raises(TargetStateRefused, match="archived"):
        close(adapter, issue)
    assert world.write_requests() == 1 and _row(world, issue)["state"] == "open"


# ---- a duplicate names its canonical (bh-iamru) -----------------------------------------------


def test_a_duplicate_names_its_canonical_by_issue_id_after_one_read():
    world, issue, adapter = setup()
    canonical = world.add_issue("canonical", at=EPOCH)

    receipt = close(adapter, issue, CloseReason.DUPLICATE, duplicate_of=canonical)

    read, patch = world.requests
    assert (read.method, read.url.path) == ("GET", "/repos/acme/widgets/issues/2")
    assert (patch.method, patch.url.path) == ("PATCH", "/repos/acme/widgets/issues/1")
    canonical_id = _row(world, canonical)["id"]
    assert canonical_id != 2  # the issue id, never the number
    assert json.loads(patch.content) == {
        "state": "closed",
        "state_reason": "duplicate",
        "duplicate_issue_id": canonical_id,
    }
    assert receipt.state_after is IssueState.CLOSED and receipt.closed_reason == "duplicate"
    assert world.duplicate_of(issue) == canonical.url
    assert world.server_labels(issue) == {"bug", "bh:triage"}


def test_the_payload_carries_a_canonical_only_for_a_duplicate():
    assert close_payload(CloseReason.DUPLICATE, 7) == {
        "state": "closed",
        "state_reason": "duplicate",
        "duplicate_issue_id": 7,
    }
    with pytest.raises(ValueError, match="duplicate"):
        close_payload(CloseReason.COMPLETED, 7)


@pytest.mark.parametrize(
    "shape",
    ["another-reason", "itself", "another-repository"],
)
def test_a_misnamed_canonical_is_refused_before_any_request(shape):
    world, issue, adapter = setup()
    canonical = world.add_issue("canonical", at=EPOCH)
    reason, of = {
        "another-reason": (CloseReason.NOT_PLANNED, canonical),
        "itself": (CloseReason.DUPLICATE, issue),
        "another-repository": (
            CloseReason.DUPLICATE,
            IssueKey("#2", "https://github.com/someone-else/elsewhere/issues/2"),
        ),
    }[shape]
    with pytest.raises((ValueError, TrackerError)):  # TrackerError: OtherProject, pure
        close(adapter, issue, reason, the_grant=grant(issue, "g-shape"), duplicate_of=of)
    assert world.requests == []
    close(adapter, issue, CloseReason.DUPLICATE, the_grant=grant(issue, "g-shape"))
    assert world.write_requests() == 1  # the grant was kept


@pytest.mark.parametrize("shape", ["deleted", "pull-request", "transferred", "unreadable"])
def test_an_unreadable_canonical_is_refused_before_the_write_and_keeps_the_grant(shape):
    world, issue, adapter = setup()
    canonical = world.add_issue("canonical", at=EPOCH)
    if shape == "deleted":
        world.delete_issue(canonical)
    elif shape == "pull-request":  # an issue URL whose number GitHub answers with a pull request
        pull = world.add_change_request("a pull request", at=EPOCH)
        number = pull.identifier.removeprefix("#")
        canonical = IssueKey(pull.identifier, f"{world.web}/acme/widgets/issues/{number}")
    elif shape == "transferred":
        world.transfer_issue(canonical, "acme/elsewhere")
    else:
        world.scripted.append(httpx.Response(200, json={"id": 1}))
    with pytest.raises(NotVisible):
        close(adapter, issue, CloseReason.DUPLICATE, grant(issue, "g-dup"), duplicate_of=canonical)
    assert world.write_requests() == 0 and _row(world, issue)["state"] == "open"
    close(adapter, issue, CloseReason.DUPLICATE, the_grant=grant(issue, "g-dup"))
    assert world.write_requests() == 1 and world.duplicate_of(issue) is None


# ---- the ambiguous outcome ------------------------------------------------------------------


@pytest.mark.parametrize("applied", [True, False])
def test_an_ambiguous_close_is_unknown_never_retried_and_spends_the_grant(applied):
    world, issue, adapter = setup()
    world.fail_next_write_ambiguously(applied=applied)
    with pytest.raises(WriteOutcomeUnknown):
        close(adapter, issue, the_grant=grant(issue, "g-amb"))
    assert world.write_requests() == 1 and world.sleeps == []
    assert (_row(world, issue)["state"] == "closed") is applied
    with pytest.raises(OperationNotEnabled, match="already used"):
        close(adapter, issue, the_grant=grant(issue, "g-amb"))
    assert world.write_requests() == 1


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"id": 1}),
        httpx.Response(503),
    ],
    ids=["not-json", "not-an-issue", "server-error"],
)
def test_an_unreadable_or_failed_answer_is_unknown(reply):
    world, issue, adapter = setup()
    world.scripted.append(reply)
    with pytest.raises(WriteOutcomeUnknown):
        close(adapter, issue)
    assert world.write_requests() == 1 and world.sleeps == []


def test_an_answer_that_is_still_open_is_unknown():
    world, issue, adapter = setup()
    still_open = dict(_row(world, issue))
    world.scripted.append(httpx.Response(200, json=still_open))
    with pytest.raises(WriteOutcomeUnknown, match="not closed"):
        close(adapter, issue)


# ---- the read-only probe ----------------------------------------------------------------------


def _facts(**permissions) -> RepositoryFacts:
    return RepositoryFacts(readable=True, permissions=permissions, scopes=frozenset({"repo"}))


def test_the_probe_refutes_a_close_below_triage_and_allows_it_from_triage():
    assert probe_operation(CLOSE, _facts(pull=True))[0] is Tri.NO
    assert probe_operation(CLOSE, _facts(pull=True, triage=True))[0] is Tri.YES
    archived = RepositoryFacts(readable=True, archived=True, permissions={"admin": True})
    assert probe_operation(CLOSE, archived)[0] is Tri.NO
