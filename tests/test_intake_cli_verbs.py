"""The external-intake verbs, reached from the real CLI (wire 2.16.0, bh-73td2).

Every verb is invoked through ``cli.app`` (the assembled ``bh`` tree, so this proves the catalog
row, the generated Typer leaf and the command body are wired together) against offline fixtures:
the in-memory ``ReportStore`` and ``MemoryTracker`` world of the composition rig, the in-process
config, and a fake release verifier. No live GitHub, no clock, no sleep, no public-store write.

Pinned here, and nowhere else, because only the registered CLI adds it:

* every verb appears under ``bh work --help`` (``delivery`` and ``outbox`` as sub-groups);
* everything is OFF by default: with ``intake.external`` unset each verb refuses, or (the outbox
  listing, which reads nothing) reports an empty outbox, with a message naming the switch;
* ``import``, ``discover`` and the outbox verbs that need a tracker refuse naming the config to set
  while the tracker plugin is not switched on;
* one smoke case per verb on the enabled path.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from tests.test_intake_import_composition import ENABLED, Rig
from tests.test_phase_timings import assert_batch_timings
from typer.testing import CliRunner

from beadhive import cli, intake_services, registry, work_intake
from beadhive.modules.intake import OwnIdentity
from beadhive.modules.tracker.contracts import READ_OPERATIONS, WRITE_OPERATIONS, IssueState
from beadhive.testing.intake import FakeReleaseVerifier
from beadhive.testing.tracker import MemoryTracker, at

ENV = {"BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1", "COLUMNS": "200"}
RELEASE_SHA = "r" * 40
OPERATOR = "ops/alice"
ME = OwnIdentity("acme/widget", "inst-1")
OUTBOX_ON = {"intake": {"external": {"enabled": True, "outbox": {"enabled": True}}}}


def marker_line(marker) -> str:
    return f"<!-- bh:v1 hive={marker.hive} install={marker.install} outbox={marker.outbox_id} -->"


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Rig:
    rig = Rig(tmp_path, monkeypatch)
    monkeypatch.setattr(work_intake, "config", SimpleNamespace(load=lambda: rig.cfg))
    monkeypatch.setattr(registry, "resolve_hive", lambda cfg, hive: rig.entry)
    monkeypatch.setattr(registry, "current_hive", lambda cfg: rig.entry)
    monkeypatch.setattr(work_intake.identity, "resolve_actor", lambda explicit="": explicit or "x")
    monkeypatch.setattr(intake_services, "process_adapters", lambda cfg: rig.adapters)
    return rig


def bh(*argv: str):
    return CliRunner().invoke(cli.app, ["work", *argv], env=ENV)


def doc(result) -> dict:
    return json.loads(result.stdout)


def switch(rig: Rig, cfg: dict) -> None:
    rig.cfg = {"managed_repos": rig.cfg["managed_repos"], **cfg}


# -- reachable and listed -----------------------------------------------------------------------


def test_every_intake_verb_is_listed_and_has_help() -> None:
    top = bh("--help")
    assert top.exit_code == 0
    for verb in ("import", "discover", "link", "delivery", "outbox"):
        assert verb in top.stdout
    assert "release" in bh("delivery", "--help").stdout
    outbox = bh("outbox", "--help").stdout
    for verb in ("approve", "discard", "draft", "reconcile", "reopen", "retry", "send", "show"):
        assert verb in outbox
        assert bh("outbox", verb, "--help").exit_code == 0
    for argv in (
        ["import"],
        ["discover"],
        ["link"],
        ["accept"],
        ["delivery", "release"],
    ):
        assert bh(*argv, "--help").exit_code == 0
    accept = " ".join(bh("accept", "--help").stdout.split())
    assert "--work" in accept and "--new-work" in accept


# -- off by default -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["import", "https://example.test/acme/widget/issues/1", "--hive", "widget"],
        ["discover", "--hive", "widget"],
        ["link", "w-1", "--to", "w-2", "--hive", "widget"],
        ["link", "w-1", "--remove", "--hive", "widget"],
        ["accept", "w-1", "--work", "w-2", "--hive", "widget"],
        ["accept", "w-1", "--new-work", "--hive", "widget"],
        ["delivery", "release", "v1.0.0", "--hive", "widget"],
        ["outbox", "draft", "w-1", "--text", "hi", "--hive", "widget"],
        ["outbox", "show", "ob1_x", "--hive", "widget"],
        ["outbox", "approve", "ob1_x", "--op", "comment:abc", "--hive", "widget"],
        ["outbox", "send", "ob1_x", "--hive", "widget"],
        ["outbox", "reconcile", "--hive", "widget"],
        ["outbox", "discard", "ob1_x", "--reason", "no", "--hive", "widget"],
        ["outbox", "retry", "ob1_x", "--hive", "widget"],
    ],
)
def test_each_verb_refuses_with_a_clear_message_when_external_intake_is_unset(rig, argv) -> None:
    switch(rig, {})  # no `intake` section at all

    result = bh(*argv)

    assert result.exit_code != 0
    assert "intake" in result.output and "opt-in" in result.output
    assert rig.store.writes == 0 and rig.world.write_requests() == 0
    rig.assert_public_store_untouched()


def test_the_outbox_listing_reads_nothing_and_says_so_when_intake_is_unset(rig) -> None:
    switch(rig, {})

    result = bh("outbox", "show", "--hive", "widget", "--json")

    assert result.exit_code == 0 and doc(result)["records"] == []


def test_the_outbox_is_off_even_when_external_intake_is_on(rig) -> None:
    switch(rig, ENABLED)

    result = bh("outbox", "send", "ob1_x", "--hive", "widget", "--as", OPERATOR, "--json")

    assert result.exit_code == 1
    assert (doc(result)["ok"], doc(result)["reason"]) == (False, "outbox_disabled")
    assert "intake.external.outbox.enabled" in doc(result)["error"]


# -- the tracker is not switched on / the outbox tracker is not wired ---------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["import", "https://example.test/acme/widget/issues/1", "--hive", "widget", "--json"],
        ["discover", "--hive", "widget", "--json"],
    ],
)
def test_import_and_discover_refuse_naming_what_to_configure(rig, monkeypatch, argv) -> None:
    switch(rig, ENABLED)
    monkeypatch.setattr(intake_services, "process_adapters", lambda cfg: None)

    result = bh(*argv)

    assert result.exit_code == 1
    assert doc(result)["reason"] == "intake_disabled"
    assert "plugins.tracker-github.enabled" in doc(result)["error"]
    rig.assert_public_store_untouched()


@pytest.mark.parametrize(
    "argv",
    [
        ["draft", "w-1", "--text", "hi"],
        ["approve", "ob1_x", "--op", "comment:abc"],
        ["send", "ob1_x"],
        ["reconcile"],
        ["discard", "ob1_x", "--reason", "no"],
        ["retry", "ob1_x"],
    ],
)
def test_outbox_verbs_that_need_a_tracker_refuse_naming_the_plugin_switch(
    rig, monkeypatch, argv
) -> None:
    switch(rig, OUTBOX_ON)
    monkeypatch.setattr(intake_services, "process_adapters", lambda cfg: None)  # plugin off

    result = bh("outbox", *argv, "--hive", "widget", "--as", OPERATOR, "--json")

    assert result.exit_code == 1
    assert doc(result)["reason"] == "intake_disabled"
    assert "plugins.tracker-github.enabled" in doc(result)["error"]
    assert "marker codec" in doc(result)["error"]
    assert rig.world.write_requests() == 0


def test_the_outbox_listing_needs_no_tracker_wiring(rig) -> None:
    switch(rig, OUTBOX_ON)

    result = bh("outbox", "show", "--hive", "widget", "--json")

    assert result.exit_code == 0 and doc(result)["records"] == []


# -- one smoke case per verb on the enabled path ---------------------------------------------------


def test_import_discover_link_accept_and_release_run_end_to_end(rig, monkeypatch) -> None:
    switch(rig, ENABLED)
    first = rig.issue("First")
    rig.issue("Second")

    dry = bh("import", first.url, "--hive", "widget", "--dry-run", "--json")
    imported = bh("import", first.url, "--hive", "widget", "--json", "--as", "dev/importer")
    again = bh("import", first.url, "--hive", "widget", "--json")
    assert dry.exit_code == imported.exit_code == again.exit_code == 0
    assert (doc(dry)["kind"], doc(imported)["kind"], doc(again)["kind"]) == (
        "created",
        "created",
        "unchanged",
    )
    one = doc(imported)["report"]

    discovered = bh("discover", "--hive", "widget", "--json")
    assert discovered.exit_code == 0 and doc(discovered)["created"] == 1  # the second issue only
    (two,) = [
        view.id for view in rig.store.list_open_labeled("intake:untriaged") if view.id != one
    ]  # the discovered report

    made = bh("accept", one, "--new-work", "--hive", "widget", "--as", "dev/triage")
    assert made.exit_code == 0 and "created and tracks" in made.stdout
    work = rig.store.tracked_work(one)
    assert work and rig.store.trackers_of(work) == (one,)
    joined = bh("accept", two, "--work", work, "--hive", "widget", "--as", "dev/triage")
    assert joined.exit_code == 0 and rig.store.trackers_of(work) == tuple(sorted((one, two)))
    bh("link", two, "--remove", "--hive", "widget")

    linked = bh("link", two, "--to", work, "--hive", "widget", "--json")
    assert linked.exit_code == 0 and (doc(linked)["kind"], doc(linked)["work"]) == ("linked", work)
    assert rig.store.trackers_of(work) == tuple(sorted((one, two)))
    unlinked = bh("link", two, "--remove", "--hive", "widget", "--json")
    assert unlinked.exit_code == 0 and doc(unlinked)["kind"] == "unlinked"
    assert rig.store.trackers_of(work) == (one,)

    third = bh("link", one, "--to", work, "--hive", "widget")  # already linked: idempotent
    assert third.exit_code == 0 and "already tracks" in third.stdout

    both = bh("link", one, "--hive", "widget")  # neither --to nor --remove
    assert both.exit_code == 2 and "exactly one of --to" in both.output

    verifier = FakeReleaseVerifier(tags={"v1.0.0": RELEASE_SHA})
    monkeypatch.setattr("beadhive.adapters.intake.GitReleaseVerifier", lambda directory: verifier)
    release = bh("delivery", "release", "v1.0.0", "--hive", "widget", "--json")
    assert release.exit_code == 0
    assert (doc(release)["ok"], doc(release)["tag"], doc(release)["reports"]) == (
        True,
        "v1.0.0",
        [],
    )
    unknown = bh("delivery", "release", "v9", "--hive", "widget", "--json")
    assert unknown.exit_code == 1 and doc(unknown)["reason"] == "unknown_tag"

    assert rig.world.write_requests() == 0  # none of these verbs writes a tracker
    rig.assert_public_store_untouched()


def test_the_outbox_verbs_draft_review_approve_send_discard_and_reconcile(rig, monkeypatch) -> None:
    switch(rig, OUTBOX_ON)
    tracker = MemoryTracker(rig.world, enabled=READ_OPERATIONS | WRITE_OPERATIONS)
    rig.world.set_write_time(at(2))  # a deterministic tracker clock: nothing reads the real one
    monkeypatch.setattr(
        intake_services,
        "outbox_wiring",
        lambda cfg, entry, directory=None, **_: {
            "tracker": tracker,
            "identity": ME,
            "marker_line": marker_line,
        },
    )
    issue = rig.issue("A bug")
    report = doc(bh("import", issue.url, "--hive", "widget", "--json"))["report"]
    common = ("--hive", "widget", "--as", OPERATOR, "--json")

    drafted = bh("outbox", "draft", report, "--text", "Thanks, fixed soon.", *common)
    assert drafted.exit_code == 0
    (row,) = doc(drafted)["results"]
    oid = row["outbox_id"]
    assert oid.startswith("ob1_") and row["state"] == "drafted"

    listing = doc(bh("outbox", "show", "--hive", "widget", "--json"))
    assert [r["outbox_id"] for r in listing["records"]] == [oid]
    only_open = doc(bh("outbox", "show", "--open", "--bead", report, "--hive", "widget", "--json"))
    assert [r["outbox_id"] for r in only_open["records"]] == [oid]

    shown = bh("outbox", "show", oid, "--hive", "widget", "--json")
    assert shown.exit_code == 0
    (render,) = doc(shown)["renders"]
    assert render["kind"] == "comment.create" and "Thanks, fixed soon." in render["payload"]
    human = bh("outbox", "show", oid, "--hive", "widget")
    assert render["digest"] in human.stdout and "Thanks, fixed soon." in human.stdout
    assert rig.world.write_requests() == 0  # reviewing posts nothing

    refused = bh("outbox", "approve", oid, "--op", "comment:000000000000", *common)
    assert refused.exit_code == 1 and doc(refused)["ok"] is False
    assert bh("outbox", "approve", oid, "--op", "bogus:abc", *common).exit_code == 2
    assert bh("outbox", "approve", oid, "--op", "comment", *common).exit_code == 2
    assert (
        bh(
            "outbox",
            "approve",
            oid,
            "--op",
            "comment:a",
            "--op",
            "labels:b",
            "--publish",
            "x:0",
            *common,
        ).exit_code
        == 2
    )
    assert rig.world.write_requests() == 0

    approved = bh("outbox", "approve", oid, "--op", f"comment:{render['short_digest']}", *common)
    assert approved.exit_code == 0
    (grant,) = doc(approved)["results"]
    assert grant["state"] == "approved" and grant["grant_id"].startswith("gr1_")

    sent = bh("outbox", "send", oid, *common)
    assert sent.exit_code == 0 and doc(sent)["results"][0]["state"] == "sent"
    assert rig.world.write_requests() == 1 and rig.world.comment_count(issue) == 1
    again = bh("outbox", "send", oid, *common)  # a spent grant never posts twice
    assert again.exit_code == 1 and rig.world.write_requests() == 1

    second = doc(bh("outbox", "draft", report, "--text", "One more thing.", *common))
    other = second["results"][0]["outbox_id"]
    discarded = bh("outbox", "discard", other, "--reason", "not needed", *common)
    assert discarded.exit_code == 0 and doc(discarded)["results"][0]["state"] == "abandoned"
    retried = bh("outbox", "retry", other, *common)  # only a failed operation can be retried
    assert retried.exit_code == 1 and doc(retried)["ok"] is False

    reconciled = bh("outbox", "reconcile", *common)
    assert reconciled.exit_code == 0 and doc(reconciled)["ok"] is True
    assert bh("outbox", "reconcile", oid, "--hive", "widget", "--as", OPERATOR).exit_code == 0
    assert rig.world.write_requests() == 1  # exactly the one approved send


def _labels_rig(rig, monkeypatch, outbox: dict) -> tuple[str, str]:
    """A wired, write-capable tracker and an imported report; ``outbox`` is the config."""
    switch(rig, {"intake": {"external": {"enabled": True, "outbox": outbox}}})
    tracker = MemoryTracker(rig.world, enabled=READ_OPERATIONS | WRITE_OPERATIONS)
    monkeypatch.setattr(
        intake_services,
        "outbox_wiring",
        lambda cfg, entry, directory=None, **_: {
            "tracker": tracker,
            "identity": ME,
            "marker_line": marker_line,
        },
    )
    report = doc(bh("import", rig.issue("A bug").url, "--hive", "widget", "--json"))["report"]
    return report, "ob1_x"


@pytest.mark.parametrize(
    "argv",
    [["send", "ob1_x", "--kind", "labels"], ["approve", "ob1_x", "--op", "labels:abc"]],
)
@pytest.mark.parametrize("outbox", [{"enabled": True}, {"enabled": True, "labels_enabled": False}])
def test_the_labels_verbs_refuse_while_only_the_comment_sender_is_on(
    rig, monkeypatch, argv, outbox
) -> None:
    _labels_rig(rig, monkeypatch, outbox)
    reads = rig.world.read_requests()  # the import's own read

    result = bh("outbox", *argv, "--hive", "widget", "--as", OPERATOR, "--json")

    assert result.exit_code == 1
    assert (doc(result)["ok"], doc(result)["reason"]) == (False, "labels_disabled")
    assert rig.world.write_requests() == 0 and rig.world.read_requests() == reads


def test_the_labels_gate_opens_only_through_its_own_key(rig, monkeypatch) -> None:
    _labels_rig(rig, monkeypatch, {"enabled": True, "labels_enabled": True})

    result = bh(
        "outbox",
        "send",
        "ob1_x",
        "--kind",
        "labels",
        "--hive",
        "widget",
        "--as",
        OPERATOR,
        "--json",
    )

    assert result.exit_code == 1
    assert doc(result)["reason"] != "labels_disabled"  # past the gate: the record does not exist
    assert rig.world.write_requests() == 0


def test_an_agent_seat_cannot_approve_or_send(rig, monkeypatch) -> None:
    switch(rig, OUTBOX_ON)
    tracker = MemoryTracker(rig.world, enabled=READ_OPERATIONS | WRITE_OPERATIONS)
    monkeypatch.setattr(
        intake_services,
        "outbox_wiring",
        lambda cfg, entry, directory=None, **_: {
            "tracker": tracker,
            "identity": ME,
            "marker_line": marker_line,
        },
    )
    issue = rig.issue("A bug")
    report = doc(bh("import", issue.url, "--hive", "widget", "--json"))["report"]
    oid = doc(
        bh(
            "outbox",
            "draft",
            report,
            "--text",
            "Hello.",
            "--hive",
            "widget",
            "--as",
            OPERATOR,
            "--json",
        )
    )["results"][0]["outbox_id"]
    digest = doc(bh("outbox", "show", oid, "--hive", "widget", "--json"))["renders"][0]["digest"]

    approved = bh(
        "outbox",
        "approve",
        oid,
        "--op",
        f"comment:{digest}",
        "--hive",
        "widget",
        "--as",
        "dev/agent",
        "--json",
    )

    assert approved.exit_code == 1 and doc(approved)["ok"] is False
    assert rig.world.write_requests() == 0


# -- issue.close (bh-pr5ll): --op close, --kind close and backfill-close ---------------------------


def _close_rig(
    rig, monkeypatch, *, close_enabled: bool, reopen_enabled: bool = False
) -> MemoryTracker:
    """The outbox on with a wired tracker whose binding does (or does not) enable issue.close."""
    switch(rig, OUTBOX_ON)
    tracker = MemoryTracker(rig.world, enabled=READ_OPERATIONS | WRITE_OPERATIONS)
    rig.world.set_write_time(at(2))
    monkeypatch.setattr(
        work_intake,
        "config",
        SimpleNamespace(load=lambda: rig.cfg, integration_branch=lambda cfg, entry: "main"),
    )
    monkeypatch.setattr(
        intake_services,
        "outbox_wiring",
        lambda cfg, entry, directory=None, **_: {
            "tracker": tracker,
            "identity": ME,
            "marker_line": marker_line,
            "close_enabled": close_enabled,
            "reopen_enabled": reopen_enabled,
        },
    )
    return tracker


def test_the_backfill_verb_is_listed_with_help() -> None:
    assert "backfill-close" in bh("outbox", "--help").stdout
    usage = " ".join(bh("outbox", "backfill-close", "--help").stdout.split())
    assert "--dry-run" in usage and "write.issue_close.enabled" in usage


@pytest.mark.parametrize(
    "argv",
    [
        ["send", "ob1_x", "--kind", "close"],
        ["approve", "ob1_x", "--op", "close:abc"],
        ["backfill-close"],
        ["backfill-close", "--dry-run"],
    ],
)
def test_close_verbs_refuse_while_the_adapter_does_not_enable_issue_close(
    rig, monkeypatch, argv
) -> None:
    _close_rig(rig, monkeypatch, close_enabled=False)

    result = bh("outbox", *argv, "--hive", "widget", "--as", OPERATOR, "--json")

    assert result.exit_code == 1
    assert (doc(result)["ok"], doc(result)["reason"]) == (False, "close_disabled")
    assert "write.issue_close.enabled" in doc(result)["error"]
    assert rig.world.write_requests() == 0


def test_backfill_then_approve_and_send_the_close_after_the_reply(rig, monkeypatch) -> None:
    import dataclasses

    from beadhive.modules.intake.domain import ProposalDisposition, ProposalStatus
    from beadhive.modules.intake.domain.proposal import new_proposal

    _close_rig(rig, monkeypatch, close_enabled=True)
    issue = rig.issue("A bug")
    report = doc(bh("import", issue.url, "--hive", "widget", "--json"))["report"]
    common = ("--hive", "widget", "--as", OPERATOR, "--json")
    oid = doc(bh("outbox", "draft", report, "--text", "Works as designed.", *common))["results"][0][
        "outbox_id"
    ]
    (comment,) = doc(bh("outbox", "show", oid, "--hive", "widget", "--json"))["renders"]
    assert (
        bh("outbox", "approve", oid, "--op", f"comment:{comment['digest']}", *common).exit_code == 0
    )
    assert bh("outbox", "send", oid, *common).exit_code == 0
    applied = dataclasses.replace(
        new_proposal(ProposalDisposition.REJECT), status=ProposalStatus.APPLIED
    )
    rig.store.write_proposal(report, applied, expect_seq=0, actor=OPERATOR)
    rig.store.close(report, reason="works as designed", actor=OPERATOR)

    planned = bh("outbox", "backfill-close", "--dry-run", *common)
    assert planned.exit_code == 0 and doc(planned)["dry_run"] is True
    assert doc(planned)["items"] == [
        {
            "report": report,
            "outbox_id": oid,
            "reason": "not_planned",
            "drafted": False,
            "detail": "",
            "reply_text": "",
            "duplicate_of": "",
        }
    ]
    human = bh("outbox", "backfill-close", "--hive", "widget", "--as", OPERATOR)
    assert human.exit_code == 0 and f"drafted issue.close as not_planned on {oid}" in human.stdout

    shown = doc(bh("outbox", "show", oid, "--hive", "widget", "--json"))
    close = next(r for r in shown["renders"] if r["kind"] == "issue.close")
    assert close["close"] == "not_planned" and "state_reason: not_planned" in close["payload"]
    approved = bh("outbox", "approve", oid, "--op", f"close:{close['short_digest']}", *common)
    assert approved.exit_code == 0 and doc(approved)["results"][0]["kind"] == "issue.close"
    sent = bh("outbox", "send", oid, "--kind", "close", *common)

    assert sent.exit_code == 0 and doc(sent)["results"][0]["state"] == "sent"
    assert rig.world.issues[issue.identifier].state.value == "closed"
    assert rig.world.write_requests() == 2  # the reply, then the close


# -- the one-process batch send (bh-pc9rf): outbox send-approved -----------------------------------


def test_send_approved_is_listed_with_help() -> None:
    assert "send-approved" in bh("outbox", "--help").stdout
    usage = " ".join(bh("outbox", "send-approved", "--help").stdout.split())
    assert "--kind" in usage and "--bead" in usage and "own single-use grant" in usage


def test_send_approved_refuses_while_the_outbox_is_off(rig) -> None:
    switch(rig, ENABLED)

    result = bh("outbox", "send-approved", "--hive", "widget", "--as", OPERATOR, "--json")

    assert result.exit_code == 1 and doc(result)["reason"] == "outbox_disabled"
    assert rig.world.write_requests() == 0


def test_send_approved_sends_every_approved_reply_in_one_process(rig, monkeypatch) -> None:
    _close_rig(rig, monkeypatch, close_enabled=False)
    common = ("--hive", "widget", "--as", OPERATOR, "--json")
    sent_ids = []
    for title in ("A bug", "Another bug"):
        report = doc(bh("import", rig.issue(title).url, "--hive", "widget", "--json"))["report"]
        oid = doc(bh("outbox", "draft", report, "--text", "Thanks, fixed.", *common))["results"][0][
            "outbox_id"
        ]
        (comment,) = doc(bh("outbox", "show", oid, "--hive", "widget", "--json"))["renders"]
        approved = bh("outbox", "approve", oid, "--op", f"comment:{comment['digest']}", *common)
        assert approved.exit_code == 0
        sent_ids.append(oid)
    unapproved = doc(bh("outbox", "draft", report, "--text", "One more thing.", *common))

    result = bh("outbox", "send-approved", *common)

    payload = doc(result)
    assert result.exit_code == 0 and payload["ok"] is True and payload["sent"] == 2
    assert [r["outbox_id"] for r in payload["results"]] == sent_ids
    assert {r["state"] for r in payload["results"]} == {"sent"} and payload["failed"] == []
    assert rig.world.write_requests() == 2  # one request per approved operation, no more
    left = unapproved["results"][0]["outbox_id"]
    shown = doc(bh("outbox", "show", left, "--hive", "widget", "--json"))
    assert shown["record"]["operations"][0]["state"] == "drafted"  # never approved, never sent

    again = bh("outbox", "send-approved", "--hive", "widget", "--as", OPERATOR)
    assert again.exit_code == 0 and "nothing approved to send" in again.stdout


def _approved_reply(rig) -> str:
    common = ("--hive", "widget", "--as", OPERATOR, "--json")
    report = doc(bh("import", rig.issue("A bug").url, "--hive", "widget", "--json"))["report"]
    oid = doc(bh("outbox", "draft", report, "--text", "Thanks.", *common))["results"][0][
        "outbox_id"
    ]
    (comment,) = doc(bh("outbox", "show", oid, "--hive", "widget", "--json"))["renders"]
    approved = bh("outbox", "approve", oid, "--op", f"comment:{comment['digest']}", *common)
    assert approved.exit_code == 0
    return oid


def test_the_batch_and_single_send_json_carry_the_measured_timings(rig, monkeypatch) -> None:
    """bh-17g6b: a ``timings`` block, in the command output only (the stored record and receipt
    are unchanged), on exactly the sending verbs."""
    _close_rig(rig, monkeypatch, close_enabled=False)
    common = ("--hive", "widget", "--as", OPERATOR)
    first, second = _approved_reply(rig), _approved_reply(rig)

    single = bh("outbox", "send", first, *common, "--json")
    batch = bh("outbox", "send-approved", *common, "--json")

    for result in (single, batch):
        assert result.exit_code == 0, result.output
        timings = doc(result)["timings"]
        assert_batch_timings(timings)
        assert timings["phases"]["config_load"]["count"] == 1  # the verb's own configuration load
    assert [r["outbox_id"] for r in doc(batch)["results"]] == [second]
    assert "timings" not in doc(bh("outbox", "show", second, "--hive", "widget", "--json"))
    shown = bh("outbox", "show", first, "--hive", "widget", "--json")
    assert "timings" not in json.dumps(doc(shown)["record"])  # nothing stored


def test_the_timings_are_json_only_and_a_refusal_carries_them_too(rig) -> None:
    switch(rig, ENABLED)

    refused = bh("outbox", "send-approved", "--hive", "widget", "--as", OPERATOR, "--json")
    human = bh("outbox", "send-approved", "--hive", "widget", "--as", OPERATOR)

    assert refused.exit_code == 1 and doc(refused)["reason"] == "outbox_disabled"
    assert_batch_timings(doc(refused)["timings"])
    assert "timings" not in human.output and "_ms" not in human.output


def test_send_approved_reports_an_unknown_outcome_and_fails_the_batch(rig, monkeypatch) -> None:
    _close_rig(rig, monkeypatch, close_enabled=False)
    common = ("--hive", "widget", "--as", OPERATOR, "--json")
    report = doc(bh("import", rig.issue("A bug").url, "--hive", "widget", "--json"))["report"]
    oid = doc(bh("outbox", "draft", report, "--text", "Thanks.", *common))["results"][0][
        "outbox_id"
    ]
    (comment,) = doc(bh("outbox", "show", oid, "--hive", "widget", "--json"))["renders"]
    approved = bh("outbox", "approve", oid, "--op", f"comment:{comment['digest']}", *common)
    assert approved.exit_code == 0
    rig.world.fail_next_write_ambiguously(applied=False)

    result = bh("outbox", "send-approved", "--bead", report, "--hive", "widget", "--as", OPERATOR)

    assert result.exit_code == 1
    assert f"{oid} comment.create: unknown" in result.stdout
    assert "sent 0, unsettled 1, refused 0" in result.stdout


def test_send_approved_names_a_bad_kind(rig, monkeypatch) -> None:
    _close_rig(rig, monkeypatch, close_enabled=False)

    result = bh("outbox", "send-approved", "--kind", "nope", "--hive", "widget", "--json")

    assert result.exit_code == 2 and doc(result)["reason"] == "bad_argument"


# -- the operator's reopen (bh-ih484): outbox reopen ----------------------------------------------


def _closed_report(rig) -> tuple[str, object]:
    issue = rig.issue("A bug")
    report = doc(bh("import", issue.url, "--hive", "widget", "--json"))["report"]
    rig.world.update_issue(issue, at=at(2), state=IssueState.CLOSED)
    return report, issue


def test_the_reopen_verb_is_listed_with_help() -> None:
    assert "reopen" in bh("outbox", "--help").stdout
    usage = " ".join(bh("outbox", "reopen", "--help").stdout.split())
    for text in ("--text", "--reply-file", "--dry-run", "write.issue_reopen.enabled"):
        assert text in usage


def test_the_reopen_verb_refuses_while_the_adapter_does_not_enable_issue_reopen(
    rig, monkeypatch
) -> None:
    _close_rig(rig, monkeypatch, close_enabled=False)
    report, _ = _closed_report(rig)

    result = bh("outbox", "reopen", report, "--hive", "widget", "--as", OPERATOR, "--json")

    assert result.exit_code == 1
    assert (doc(result)["ok"], doc(result)["reason"]) == (False, "reopen_disabled")
    assert "write.issue_reopen.enabled" in doc(result)["error"]
    assert rig.world.write_requests() == 0


def test_the_reopen_verb_dry_runs_drafts_and_the_reopen_then_goes_before_its_reply(
    rig, monkeypatch, tmp_path
) -> None:
    _close_rig(rig, monkeypatch, close_enabled=False, reopen_enabled=True)
    report, issue = _closed_report(rig)
    common = ("--hive", "widget", "--as", OPERATOR)
    reply = tmp_path / "reply.txt"
    reply.write_text("We were wrong to close this, reopening.\n", encoding="utf-8")

    planned = bh("outbox", "reopen", report, "--reply-file", str(reply), "--dry-run", *common)
    assert planned.exit_code == 0 and "would draft issue.reopen" in planned.stdout
    assert "reopen | state: open" in planned.stdout
    assert "reply  | We were wrong to close this, reopening." in planned.stdout
    assert "dry run: nothing was written" in planned.stdout
    assert doc(bh("outbox", "show", "--hive", "widget", "--json"))["records"] == []  # no draft

    drafted = bh("outbox", "reopen", report, "--reply-file", str(reply), *common, "--json")
    payload = doc(drafted)
    assert drafted.exit_code == 0 and payload["ok"] and payload["dry_run"] is False
    assert payload["reopen"] == "state: open" and payload["target_url"] == issue.url
    assert payload["reply_text"].strip() == "We were wrong to close this, reopening."
    oid = payload["outbox_id"]
    assert rig.world.write_requests() == 0  # drafted: nothing approved, nothing sent

    again = bh("outbox", "reopen", report, "--text", "More.", *common, "--json")
    assert again.exit_code == 1 and doc(again)["reason"] == "reopen_pending"  # never two
    assert oid in doc(again)["error"]

    renders = {
        r["kind"]: r
        for r in doc(bh("outbox", "show", oid, "--hive", "widget", "--json"))["renders"]
    }
    for kind, key in (("issue.reopen", "reopen"), ("comment.create", "comment")):
        approved = bh(
            "outbox", "approve", oid, "--op", f"{key}:{renders[kind]['short_digest']}", *common
        )
        assert approved.exit_code == 0
    batch = doc(bh("outbox", "send-approved", *common, "--json"))
    assert batch["ok"] and [r["kind"] for r in batch["results"]] == [
        "issue.reopen",
        "comment.create",
    ]
    assert rig.world.issues[issue.identifier].state.value == "open"
    assert rig.world.write_requests() == 2  # the reopen, then its reply


def test_the_reopen_verb_refuses_an_open_issue_and_both_text_sources(
    rig, monkeypatch, tmp_path
) -> None:
    _close_rig(rig, monkeypatch, close_enabled=False, reopen_enabled=True)
    opened = doc(bh("import", rig.issue("Open one").url, "--hive", "widget", "--json"))["report"]
    common = ("--hive", "widget", "--as", OPERATOR, "--json")

    open_issue = bh("outbox", "reopen", opened, *common)
    assert open_issue.exit_code == 1 and doc(open_issue)["reason"] == "reopen_not_needed"

    reply = tmp_path / "reply.txt"
    reply.write_text("x", encoding="utf-8")
    both = bh("outbox", "reopen", opened, "--text", "a", "--reply-file", str(reply), *common)
    assert both.exit_code == 1 and doc(both)["reason"] == "invalid_request"
    missing = bh("outbox", "reopen", opened, "--reply-file", str(tmp_path / "nope"), *common)
    assert missing.exit_code == 1 and doc(missing)["reason"] == "invalid_request"
    blank_text = bh("outbox", "reopen", opened, "--text", "  \n\t ", *common)
    assert blank_text.exit_code == 1 and doc(blank_text)["reason"] == "invalid_request"
    blank = tmp_path / "blank.txt"
    blank.write_text(" \n\n", encoding="utf-8")
    blank_file = bh("outbox", "reopen", opened, "--reply-file", str(blank), *common)
    assert blank_file.exit_code == 1 and doc(blank_file)["reason"] == "invalid_request"
    assert rig.world.write_requests() == 0
