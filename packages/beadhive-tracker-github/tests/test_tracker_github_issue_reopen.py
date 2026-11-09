"""The ``issue.reopen`` writer (``bh-ih484``), offline: refusals before any request, the wire form
(the state only, never labels), the single-use grant, the ambiguous outcome and the probe.

Every test sends through the ``MockTransport`` fake. Nothing here reaches GitHub, reads a clock or
sleeps. The credentials are synthetic.
"""

from __future__ import annotations

import json

import pytest

from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    AuthenticationFailed,
    IssueKey,
    IssueReopener,
    IssueState,
    Operation,
    OperationNotEnabled,
    ProjectRef,
    Tri,
    WriteGrant,
    WriteOutcomeUnknown,
)
from beadhive_plugins.credentials import ResolvedToken
from beadhive_tracker_github.adapter import IssueReopening, adapter_class, build_adapter
from beadhive_tracker_github.adapter.identity import RepositoryFacts, probe_operation
from beadhive_tracker_github.adapter.reopening import reopen_payload
from beadhive_tracker_github.adapter.transport import API_VERSION
from tracker_github_fake import EPOCH, FAKE_TOKEN, FakeGitHub, adapter_for

REOPEN = Operation.ISSUE_REOPEN


def setup():
    """A world with one CLOSED issue carrying a human label and an owned one, and a
    reopen-enabled adapter (reads on too, so the fake can be observed through the port)."""
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH, labels=("bug", "bh:triage"))
    row = _row(world, issue)
    row.update(state="closed", state_reason="not_planned")
    world.set_write_time(EPOCH)
    return world, issue, adapter_for(world, enabled=READ_OPERATIONS | {REOPEN})


def grant(issue: IssueKey, grant_id: str = "g-1", op: Operation = REOPEN) -> WriteGrant:
    return WriteGrant(grant_id, op, issue)


def _row(world: FakeGitHub, issue: IssueKey) -> dict:
    return next(row for row in world.issues if f"#{row['number']}" == issue.identifier)


def test_the_switch_off_composes_no_reopener():
    world = FakeGitHub()
    adapter = adapter_for(world, enabled=READ_OPERATIONS)
    assert not isinstance(adapter, IssueReopener) and not hasattr(adapter, "reopen_issue")
    assert REOPEN in adapter.describe().supported  # implemented, just not enabled
    assert world.requests == []


def test_the_switch_folds_from_write_issue_reopen_enabled_and_defaults_off():
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

    assert not isinstance(build({}), IssueReopener)
    assert not isinstance(build({"write": {"issue_close": {"enabled": True}}}), IssueReopener)
    assert not isinstance(build({"write": {"issue_reopen": {"enabled": False}}}), IssueReopener)
    on = build({"write": {"issue_reopen": {"enabled": True}}})
    assert isinstance(on, IssueReopener) and REOPEN in on.enabled
    with pytest.raises(ValueError, match="write.issue_reopen.enabled"):
        build({"write": {"issue_reopen": {"enabled": "yes"}}})


def test_a_composed_reopener_still_refuses_when_its_switch_is_off():
    """Defence in depth: the mixin re-checks the adapter's own enabled set."""
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH)
    resolved: list[int] = []

    def tokens():
        resolved.append(1)
        return ResolvedToken(world.token, "env:GITHUB_TOKEN")

    adapter = adapter_class({REOPEN})(
        project=ProjectRef("github", f"{world.owner}/{world.repo}"),
        token_source=tokens,
        sleep=world.sleep,
        clock=world.clock.now,
        enabled=READ_OPERATIONS,  # issue.reopen is NOT enabled
        transport=world.transport(),
    )
    assert isinstance(adapter, IssueReopening)
    with pytest.raises(OperationNotEnabled, match="issue.reopen is not enabled"):
        adapter.reopen_issue(issue, grant=grant(issue))
    assert world.requests == [] and resolved == []


def _bad_grants(issue):
    """Each grant a reopen must refuse. The close's grant is minted for THIS issue, so only its
    operation is wrong: refusing it proves a close grant cannot reopen."""
    return {
        "missing": None,
        "string": "g-1",
        "the-closes-grant": WriteGrant("g-c", Operation.ISSUE_CLOSE, issue),
        "repository-scoped": WriteGrant("g-p", REOPEN, ProjectRef("github", "acme/widgets")),
    }


@pytest.mark.parametrize("which", ["missing", "string", "the-closes-grant", "repository-scoped"])
def test_a_missing_or_mismatched_grant_refuses_before_any_request(which):
    world, issue, adapter = setup()
    with pytest.raises(OperationNotEnabled):
        adapter.reopen_issue(issue, grant=_bad_grants(issue)[which])
    assert world.requests == []


def test_another_repositorys_issue_is_refused_before_any_request():
    world, _issue, adapter = setup()
    foreign = IssueKey("#1", "https://github.com/someone-else/elsewhere/issues/1")
    with pytest.raises(Exception) as caught:  # noqa: PT011 - the exact port error is asserted
        adapter.reopen_issue(foreign, grant=grant(foreign))
    assert world.requests == []
    assert not isinstance(caught.value, WriteOutcomeUnknown)


def test_no_credential_refuses_before_any_request_and_keeps_the_grant():
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH)
    world.set_write_time(EPOCH)
    holder: list[ResolvedToken | None] = [None]
    adapter = adapter_for(world, enabled={REOPEN}, tokens=lambda: holder[0])
    once = grant(issue, "g-anon")
    with pytest.raises(AuthenticationFailed, match="needs a credential"):
        adapter.reopen_issue(issue, grant=once)
    assert world.requests == []
    holder[0] = ResolvedToken(world.token, "env:GITHUB_TOKEN")
    adapter.reopen_issue(issue, grant=once)
    assert world.write_requests() == 1
    with pytest.raises(OperationNotEnabled, match="already used"):
        adapter.reopen_issue(issue, grant=once)
    assert world.write_requests() == 1


def test_one_patch_with_the_state_only_and_never_the_labels():
    world, issue, adapter = setup()
    world.add_human_label(issue, "needs-love")

    receipt = adapter.reopen_issue(issue, grant=grant(issue))

    (patch,) = world.requests
    assert (patch.method, patch.url.path) == ("PATCH", "/repos/acme/widgets/issues/1")
    assert json.loads(patch.content) == {"state": "open"}
    assert patch.headers["x-github-api-version"] == API_VERSION
    assert patch.headers["authorization"].endswith(FAKE_TOKEN)
    assert (receipt.grant_id, receipt.operation, receipt.target) == ("g-1", REOPEN, issue)
    assert receipt.state_after is IssueState.OPEN and receipt.closed_reason is None
    assert world.server_labels(issue) == {"bug", "bh:triage", "needs-love"}
    assert _row(world, issue)["state"] == "open"


def test_the_payload_is_the_state_alone():
    assert reopen_payload() == {"state": "open"}


def test_reopening_an_open_issue_succeeds():
    world, issue, adapter = setup()
    _row(world, issue).update(state="open", state_reason=None)
    receipt = adapter.reopen_issue(issue, grant=grant(issue))
    assert receipt.state_after is IssueState.OPEN and world.write_requests() == 1


@pytest.mark.parametrize("applied", [True, False])
def test_an_ambiguous_reopen_is_unknown_never_retried_and_spends_the_grant(applied):
    world, issue, adapter = setup()
    world.fail_next_write_ambiguously(applied=applied)
    with pytest.raises(WriteOutcomeUnknown):
        adapter.reopen_issue(issue, grant=grant(issue, "g-amb"))
    assert world.write_requests() == 1 and world.sleeps == []
    assert (_row(world, issue)["state"] == "open") is applied
    with pytest.raises(OperationNotEnabled, match="already used"):
        adapter.reopen_issue(issue, grant=grant(issue, "g-amb"))
    assert world.write_requests() == 1


def test_the_probe_refutes_reopen_below_triage_and_allows_triage():
    def probe(**permissions):
        facts = RepositoryFacts(readable=True, permissions=permissions, scopes=frozenset({"repo"}))
        return probe_operation(REOPEN, facts)[0]

    assert probe(pull=True) is Tri.NO
    assert probe(pull=True, triage=True) is Tri.YES
    archived = RepositoryFacts(readable=True, archived=True, permissions={"admin": True})
    assert probe_operation(REOPEN, archived)[0] is Tri.NO
