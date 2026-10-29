"""The external-intake verbs, reached from the real CLI (wire 2.16.0, bh-73td2).

Every verb is invoked through ``cli.app`` (the assembled ``bh`` tree, so this proves the catalog
row, the generated Typer leaf and the command body are wired together) against offline fixtures:
the in-memory ``ReportStore`` and ``MemoryTracker`` world of the composition rig, the in-process
config, and a fake release verifier. No live GitHub, no clock, no sleep, no public-store write.

Pinned here, and nowhere else, because only the registered CLI adds it:

* every verb appears under ``bh work --help`` (``delivery`` and ``outbox`` as sub-groups);
* everything is OFF by default: with ``intake.external`` unset each verb refuses, or (the outbox
  listing, which reads nothing) reports an empty outbox, with a message naming the switch;
* ``import`` and ``discover`` refuse naming the config to set while the tracker is not switched on,
  and the outbox verbs that need a tracker refuse saying the outbox is not wired;
* one smoke case per verb on the enabled path.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from tests.test_intake_import_composition import ENABLED, Rig
from typer.testing import CliRunner

from beadhive import cli, intake_services, registry, work_intake
from beadhive.modules.intake import OwnIdentity
from beadhive.modules.tracker.contracts import READ_OPERATIONS, WRITE_OPERATIONS
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
    for verb in ("approve", "discard", "draft", "reconcile", "retry", "send", "show"):
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
        ["show", "ob1_x"],
        ["approve", "ob1_x", "--op", "comment:abc"],
        ["send", "ob1_x"],
        ["reconcile"],
        ["discard", "ob1_x", "--reason", "no"],
        ["retry", "ob1_x"],
    ],
)
def test_outbox_verbs_that_need_a_tracker_refuse_saying_the_outbox_is_unwired(rig, argv) -> None:
    switch(rig, OUTBOX_ON)

    seat = [] if argv[0] == "show" else ["--as", OPERATOR]  # a read has no acting seat
    result = bh("outbox", *argv, "--hive", "widget", *seat, "--json")

    assert result.exit_code == 1
    assert doc(result)["reason"] == "intake_disabled" and "marker codec" in doc(result)["error"]
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
        lambda cfg, entry: {"tracker": tracker, "identity": ME, "marker_line": marker_line},
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


def test_an_agent_seat_cannot_approve_or_send(rig, monkeypatch) -> None:
    switch(rig, OUTBOX_ON)
    tracker = MemoryTracker(rig.world, enabled=READ_OPERATIONS | WRITE_OPERATIONS)
    monkeypatch.setattr(
        intake_services,
        "outbox_wiring",
        lambda cfg, entry: {"tracker": tracker, "identity": ME, "marker_line": marker_line},
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
