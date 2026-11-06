"""The ``issue.type`` and ``issue.fields`` writers (``bh-poqy0``), offline: the switch that also
gates the read, refusals before any request, the wire form (the type, or the named fields, only;
never the labels), the single-use grant, the ambiguous outcome and the probe.

Every test sends through the ``MockTransport`` fake. Nothing here reaches GitHub, reads a clock or
sleeps. The credentials are synthetic.
"""

from __future__ import annotations

import json

import httpx
import pytest

from beadhive.modules.tracker.contracts import (
    READ_OPERATIONS,
    AuthenticationFailed,
    IssueFieldWriter,
    IssueKey,
    IssueQuery,
    IssueTypeWriter,
    Operation,
    OperationNotEnabled,
    PayloadRejected,
    ProjectRef,
    TargetStateRefused,
    Tri,
    WriteGrant,
    WriteOutcomeUnknown,
)
from beadhive_plugins.credentials import ResolvedToken
from beadhive_tracker_github.adapter import build_adapter
from beadhive_tracker_github.adapter.identity import RepositoryFacts, probe_operation
from beadhive_tracker_github.adapter.issue_values import field_payload, type_payload
from beadhive_tracker_github.adapter.mapping import field_values, issue_type
from tracker_github_fake import EPOCH, FakeGitHub, adapter_for

TYPE = Operation.ISSUE_TYPE
FIELDS = Operation.ISSUE_FIELDS


def setup(*ops: Operation, **kwargs):
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH, labels=("bug", "bh:type/bug"))
    world.set_write_time(EPOCH)
    adapter = adapter_for(world, enabled=READ_OPERATIONS | set(ops or (TYPE, FIELDS)), **kwargs)
    return world, issue, adapter


def grant(issue, op: Operation, grant_id: str = "g-1") -> WriteGrant:
    return WriteGrant(grant_id, op, issue)


def _row(world: FakeGitHub, issue: IssueKey) -> dict:
    return next(row for row in world.issues if f"#{row['number']}" == issue.identifier)


def _listed(adapter, issue):
    (found,) = [i for i in adapter.list_issues(IssueQuery()).items if i.key == issue]
    return found


# ---- the switch: composition, folding, and the read it gates ----------------------------------


@pytest.mark.parametrize(
    ("key", "op", "protocol"),
    [("issue_type", TYPE, IssueTypeWriter), ("issue_fields", FIELDS, IssueFieldWriter)],
)
def test_each_flag_folds_from_its_own_key_and_defaults_off(key, op, protocol):
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

    assert not isinstance(build({}), protocol)
    assert not isinstance(build({"write": {key: {"enabled": False}}}), protocol)
    on = build({"write": {key: {"enabled": True}}})
    assert isinstance(on, protocol) and op in on.enabled
    other = IssueFieldWriter if protocol is IssueTypeWriter else IssueTypeWriter
    assert not isinstance(on, other)  # one switch never enables the other operation
    with pytest.raises(ValueError, match=f"write.{key}.enabled"):
        build({"write": {key: {"enabled": "yes"}}})


def test_the_read_is_gated_by_the_same_switch():
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH)
    _row(world, issue)["type"] = world._type_row("Bug")
    off = _listed(adapter_for(world), issue)
    assert off.issue_type is None and off.fields is None  # not read, never a guess
    on = _listed(adapter_for(world, enabled=READ_OPERATIONS | {TYPE, FIELDS}), issue)
    assert on.issue_type == "Bug" and on.fields == ()
    assert world.write_requests() == 0


def test_mapping_reads_the_type_and_each_field_value():
    assert issue_type({"type": None}) == "" and issue_type({}) is None
    assert issue_type({"type": {"name": "Task"}}) == "Task"
    values = [
        {"issue_field_name": "Priority", "single_select_option": {"name": "High"}, "value": "x"},
        {"issue_field_name": "Start date", "value": "2026-11-06"},
        {"issue_field_name": "Effort", "value": 3},
        {"value": "nameless"},
    ]
    assert field_values(values) == (
        ("Effort", "3"),
        ("Priority", "High"),
        ("Start date", "2026-11-06"),
    )


# ---- issue.type: refusals and the wire form ----------------------------------------------------


def test_one_patch_with_the_type_alone_and_never_the_labels():
    world, issue, adapter = setup(TYPE)
    world.add_human_label(issue, "needs-love")
    receipt = adapter.set_issue_type(issue, "Bug", grant=grant(issue, TYPE))
    (patch,) = [r for r in world.requests if r.method != "GET"]
    assert (patch.method, patch.url.path) == ("PATCH", "/repos/acme/widgets/issues/1")
    assert json.loads(patch.content) == {"type": "Bug"}
    assert (receipt.grant_id, receipt.operation, receipt.type_after) == ("g-1", TYPE, "Bug")
    assert world.server_labels(issue) == {"bug", "bh:type/bug", "needs-love"}
    assert _row(world, issue)["state"] == "open"


def test_the_type_payload_is_built_from_the_name_alone():
    assert type_payload("Feature") == {"type": "Feature"}
    with pytest.raises(ValueError):
        type_payload(" ")


@pytest.mark.parametrize(
    "bad",
    [
        None,
        WriteGrant("g-c", FIELDS, IssueKey("#1", "x")),
        WriteGrant("g-p", TYPE, ProjectRef("github", "acme/widgets")),
    ],
    ids=["missing", "another-operation", "repository-scoped"],
)
def test_a_mismatched_type_grant_refuses_before_any_request(bad):
    world, issue, adapter = setup(TYPE)
    with pytest.raises(OperationNotEnabled):
        adapter.set_issue_type(issue, "Bug", grant=bad)
    assert world.requests == []


def test_an_undefined_type_is_githubs_422_and_spends_the_grant():
    world, issue, adapter = setup(TYPE)
    with pytest.raises(PayloadRejected):
        adapter.set_issue_type(issue, "Chore", grant=grant(issue, TYPE, "g-chore"))
    assert world.server_issue_type(issue) == "" and world.write_requests() == 1
    with pytest.raises(OperationNotEnabled, match="already used"):
        adapter.set_issue_type(issue, "Task", grant=grant(issue, TYPE, "g-chore"))


def test_no_credential_refuses_before_any_request_and_keeps_the_grant():
    world = FakeGitHub()
    issue = world.add_issue("thread", at=EPOCH)
    holder: list[ResolvedToken | None] = [None]
    adapter = adapter_for(world, enabled={TYPE, FIELDS}, tokens=lambda: holder[0])
    for op, call in (
        (TYPE, lambda g: adapter.set_issue_type(issue, "Bug", grant=g)),
        (FIELDS, lambda g: adapter.set_issue_fields(issue, {"Priority": "Low"}, grant=g)),
    ):
        once = grant(issue, op, f"g-anon-{op.value}")
        holder[0] = None
        with pytest.raises(AuthenticationFailed, match="needs a credential"):
            call(once)
        assert world.requests == []
        holder[0] = ResolvedToken(world.token, "env:GITHUB_TOKEN")
        call(once)
        world.requests.clear()


def test_an_archived_repository_refuses_both_writes():
    world, issue, adapter = setup()
    world.repo_meta["archived"] = True
    with pytest.raises(TargetStateRefused, match="archived"):
        adapter.set_issue_type(issue, "Bug", grant=grant(issue, TYPE))
    with pytest.raises(TargetStateRefused, match="archived"):
        adapter.set_issue_fields(issue, {"Priority": "Low"}, grant=grant(issue, FIELDS))
    assert world.server_issue_type(issue) == "" and world.server_issue_fields(issue) == {}


# ---- issue.fields: the organization's field ids, the wire form ---------------------------------


def test_one_read_of_the_org_fields_then_one_post_of_the_named_value():
    world, issue, adapter = setup(FIELDS)
    world.seed_issue_field("Effort", ("High", "Low"))
    adapter.set_issue_fields(issue, {"Effort": "Low"}, grant=grant(issue, FIELDS, "g-0"))
    world.requests.clear()

    receipt = adapter.set_issue_fields(issue, {"Priority": "Urgent"}, grant=grant(issue, FIELDS))

    read, post = world.requests
    assert (read.method, read.url.path) == ("GET", "/orgs/acme/issue-fields")
    assert (post.method, post.url.path) == (
        "POST",
        "/repos/acme/widgets/issues/1/issue-field-values",
    )
    priority_id = next(f["id"] for f in world._field_rows() if f["name"] == "Priority")
    assert json.loads(post.content) == {
        "issue_field_values": [{"field_id": priority_id, "value": "Urgent"}]
    }
    assert receipt.fields_after == (("Effort", "Low"), ("Priority", "Urgent"))
    assert world.server_issue_fields(issue) == {"Effort": "Low", "Priority": "Urgent"}
    assert world.server_labels(issue) == {"bug", "bh:type/bug"}


@pytest.mark.parametrize("values", [{"Priority": "Whenever"}, {"Severity": "High"}])
def test_an_undefined_field_or_option_is_refused_after_the_read_and_keeps_the_grant(values):
    world, issue, adapter = setup(FIELDS)
    kept = grant(issue, FIELDS, "g-kept")
    with pytest.raises(PayloadRejected):
        adapter.set_issue_fields(issue, values, grant=kept)
    assert world.write_requests() == 0
    adapter.set_issue_fields(issue, {"Priority": "Medium"}, grant=kept)
    assert world.server_issue_fields(issue) == {"Priority": "Medium"}


def test_the_field_payload_is_pure_and_refuses_what_the_org_does_not_define():
    rows = [
        {"id": 7, "name": "Priority", "data_type": "single_select", "options": [{"name": "Low"}]},
        {"id": 8, "name": "Notes", "data_type": "text"},
    ]
    assert field_payload({"Priority": "Low", "Notes": "x"}, rows) == {
        "issue_field_values": [{"field_id": 8, "value": "x"}, {"field_id": 7, "value": "Low"}]
    }
    with pytest.raises(PayloadRejected):
        field_payload({"Priority": "High"}, rows)


def test_a_user_owned_repository_has_no_org_fields_and_nothing_is_written():
    world, issue, adapter = setup(FIELDS)
    world.scripted.append(httpx.Response(404, json={"message": "Not Found"}))
    with pytest.raises(Exception) as caught:  # noqa: PT011 - the exact port error is asserted
        adapter.set_issue_fields(issue, {"Priority": "Low"}, grant=grant(issue, FIELDS))
    assert not isinstance(caught.value, WriteOutcomeUnknown)
    assert world.write_requests() == 0


# ---- the ambiguous outcome ----------------------------------------------------------------------


@pytest.mark.parametrize("applied", [True, False])
def test_an_ambiguous_write_is_unknown_never_retried_and_spends_the_grant(applied):
    world, issue, adapter = setup()
    for op, call, value in (
        (TYPE, lambda g: adapter.set_issue_type(issue, "Task", grant=g), "Task"),
        (FIELDS, lambda g: adapter.set_issue_fields(issue, {"Priority": "High"}, grant=g), "High"),
    ):
        world.fail_next_write_ambiguously(applied=applied)
        once = grant(issue, op, f"g-amb-{op.value}")
        with pytest.raises(WriteOutcomeUnknown):
            call(once)
        with pytest.raises(OperationNotEnabled, match="already used"):
            call(once)
        held = (
            world.server_issue_type(issue)
            if op is TYPE
            else world.server_issue_fields(issue).get("Priority", "")
        )
        assert (held == value) is applied
    assert world.write_requests() == 2 and world.sleeps == []


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(200, content=b"not json"),
        httpx.Response(200, json={"id": 1}),
        httpx.Response(503),
    ],
    ids=["not-json", "no-type", "server-error"],
)
def test_an_answer_without_the_type_is_unknown(reply):
    world, issue, adapter = setup(TYPE)
    world.scripted.append(reply)
    with pytest.raises(WriteOutcomeUnknown):
        adapter.set_issue_type(issue, "Bug", grant=grant(issue, TYPE))


def test_a_field_answer_without_the_value_is_unknown():
    world, issue, adapter = setup(FIELDS)
    fields = httpx.Response(200, json=world._field_rows())
    world.scripted.extend([fields, httpx.Response(200, json=[])])
    with pytest.raises(WriteOutcomeUnknown):
        adapter.set_issue_fields(issue, {"Priority": "Low"}, grant=grant(issue, FIELDS))


# ---- the read-only probe ------------------------------------------------------------------------


def _facts(scopes=frozenset({"repo", "read:org"}), **permissions) -> RepositoryFacts:
    return RepositoryFacts(readable=True, permissions=permissions, scopes=scopes)


@pytest.mark.parametrize("op", [TYPE, FIELDS])
def test_the_probe_refutes_a_role_below_write(op):
    assert probe_operation(op, _facts(triage=True))[0] is Tri.NO
    assert probe_operation(op, _facts(push=True))[0] is Tri.YES


def test_the_probe_refutes_fields_for_a_classic_token_without_read_org():
    assert probe_operation(FIELDS, _facts(frozenset({"repo"}), push=True))[0] is Tri.NO
    assert probe_operation(TYPE, _facts(frozenset({"repo"}), push=True))[0] is Tri.YES
    assert probe_operation(FIELDS, _facts(None, push=True))[0] is Tri.UNKNOWN
