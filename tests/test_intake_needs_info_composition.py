"""``bh work needs-info`` and ``bh work wake`` through the production composition (bh-61xkc).

``intake_services.needs_info`` runs on the outbox port (the supervised sender, with the thread
service while comments are on) and ``intake_services.wake`` on the thread port; both are gated by
``intake.external.needs_info.enabled`` (off by default), and ``wake --all`` additionally by
``intake.external.needs_info.sweep``. A link catches a report up on already merged work only with
``intake.external.milestones.catch_up_on_link``. What is pinned here, through the registered CLI
and a real ``TrackerAdapters`` over ``MemoryWorld``:

* off by default: every switch refuses naming its key, writing nothing and reading no tracker;
* the round trip: ask (drafted, never posted), the operator's send never wakes it, the reporter's
  reply does, back in ``intake:untriaged`` and still held;
* a born-native report is refused clearly;
* ``wake --all`` and its separate switch, argument errors;
* the link catch-up's ``milestones`` key appears only when it ran.

No live GitHub, no clock, no sleep: ``MemoryWorld`` timestamps are explicit, the lock in-process.
"""

from __future__ import annotations

import json

import pytest
from tests.test_intake_outbox_wiring import Wired, external
from tests.unit.bootstrap.test_tracker_composition import plugin
from typer.testing import CliRunner

from beadhive import cli, intake_services
from beadhive.modules.intake import ReportDraft
from beadhive.modules.intake.domain import WorkDraft
from beadhive.testing.tracker import at

ENV = {"BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1", "COLUMNS": "200"}
OPERATOR = "ops/alice"
TRIAGER = "dev/triager"
PLUGINS = {"tracker-github": plugin(comments=True, comment=True)}
QUESTION = "Which version are you on?"


def on(**needs_info) -> dict:
    return external(comments={"enabled": True}, needs_info={"enabled": True, **needs_info})


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Wired:
    rig = Wired(tmp_path, monkeypatch, PLUGINS)
    rig.configure(on())
    return rig


def bh(*argv: str):
    return CliRunner().invoke(cli.app, ["work", *argv], env=ENV)


def doc(result) -> dict:
    return json.loads(result.stdout)


def labels(rig: Wired, report: str) -> tuple[str, ...]:
    view = rig.store.get(report)
    assert view is not None
    return view.labels


def ask(report: str, text: str = QUESTION):
    return bh("needs-info", report, "--ask", text, "--hive", "widget", "--as", TRIAGER, "--json")


def wake(*argv: str):
    return bh("wake", *argv, "--hive", "widget", "--as", TRIAGER, "--json")


def ingest(report: str) -> None:
    result = bh("thread", "ingest", report, "--hive", "widget", "--as", TRIAGER)
    assert result.exit_code == 0, result.output


def considered(report: str) -> None:
    shown = doc(bh("thread", "show", report, "--hive", "widget", "--json"))["thread"]
    seq = str(shown["seq"]) if shown else "0"
    result = bh("thread", "considered", report, "--through", seq, "--hive", "widget")
    assert result.exit_code == 0, result.output


# -- off by default -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("cfg", "key"),
    [
        ({}, "intake.external.enabled"),
        (external(comments={"enabled": True}), "intake.external.needs_info.enabled"),
        (
            external(comments={"enabled": True}, needs_info={"enabled": "true"}),
            "intake.external.needs_info.enabled",
        ),
    ],
)
@pytest.mark.parametrize("verb", ["needs-info", "wake", "wake-all"])
def test_every_verb_refuses_naming_the_switch_while_it_is_off(rig, cfg, key, verb) -> None:
    report, _issue = rig.report()
    writes, reads = rig.store.writes, rig.world.read_requests()
    rig.configure(cfg)

    result = {
        "needs-info": lambda: ask(report),
        "wake": lambda: wake(report),
        "wake-all": lambda: wake("--all"),
    }[verb]()

    assert result.exit_code == 1, result.output
    assert doc(result)["reason"] == "intake_disabled" and key in doc(result)["error"]
    assert rig.store.writes == writes and rig.world.read_requests() == reads


def test_asking_needs_the_outbox_and_comment_ingestion_naming_each_key(rig) -> None:
    report, _issue = rig.report()
    rig.configure({"intake": {"external": {"enabled": True, "needs_info": {"enabled": True}}}})
    no_outbox = ask(report)
    rig.configure(external(needs_info={"enabled": True}))
    no_comments = ask(report)

    assert doc(no_outbox)["reason"] == "outbox_disabled"
    assert "intake.external.outbox.enabled" in doc(no_outbox)["error"]
    assert doc(no_comments)["reason"] == "intake_disabled"
    assert "intake.external.comments.enabled" in doc(no_comments)["error"]
    assert "intake:untriaged" in labels(rig, report)


def test_the_settings_are_off_by_default_and_strict() -> None:
    assert intake_services.needs_info_settings({}) == (False, False)
    sweep_alone = {"intake": {"external": {"needs_info": {"sweep": True}}}}
    assert intake_services.needs_info_settings(sweep_alone) == (False, False)
    both = {"intake": {"external": {"needs_info": {"enabled": True, "sweep": True}}}}
    assert intake_services.needs_info_settings(both) == (True, True)
    assert intake_services.catch_up_on_link({}) is False
    loose = {"intake": {"external": {"milestones": {"catch_up_on_link": "yes"}}}}
    assert intake_services.catch_up_on_link(loose) is False


# -- the round trip -------------------------------------------------------------------------------


def test_needs_info_and_wake_round_trip_through_the_cli(rig) -> None:
    report, issue = rig.report()
    rig.world.add_comment(issue, "It crashes.", at=at(3), author="reporter")
    ingest(report)
    considered(report)

    asked = ask(report)

    assert asked.exit_code == 0, asked.output
    payload = doc(asked)
    assert payload["state"] == "needs-info" and payload["state_written"] is True
    oid = payload["outbox_id"]
    assert "intake:needs-info" in labels(rig, report)
    assert rig.world.write_requests() == 0  # drafted only

    common = ("--hive", "widget", "--as", OPERATOR, "--json")
    digest = doc(bh("outbox", "show", oid, "--hive", "widget", "--json"))["renders"][0]["digest"]
    assert bh("outbox", "approve", oid, "--op", f"comment:{digest}", *common).exit_code == 0
    rig.world.set_write_time(at(9))
    assert bh("outbox", "send", oid, *common).exit_code == 0
    ingest(report)

    idle = wake(report)
    assert idle.exit_code == 0 and doc(idle)["woken"] is False
    assert "intake:needs-info" in labels(rig, report)

    reply = rig.world.add_comment(issue, "v2.3.1", at=at(10), author="reporter")
    ingest(report)
    woken = wake(report)

    assert woken.exit_code == 0, woken.output
    assert doc(woken)["woken"] is True and doc(woken)["evidence"] == [reply.id]
    assert "intake:untriaged" in labels(rig, report)
    assert any(report in hold.blocks for hold in rig.store.holds())  # still held, never work
    assert report not in rig.store.ready_ids()
    shown = doc(bh("thread", "show", report, "--hive", "widget", "--json"))["thread"]
    assert shown["new_evidence"] == [reply.id]  # left unread for triage
    assert rig.world.write_requests() == 1  # exactly the operator-approved question


def test_human_output_says_nothing_is_sent_and_how_to_wake(rig) -> None:
    report, issue = rig.report()

    asked = bh("needs-info", report, "--ask", QUESTION, "--hive", "widget", "--as", TRIAGER)
    rig.world.add_comment(issue, "answer", at=at(3), author="reporter")
    ingest(report)
    woken = bh("wake", report, "--hive", "widget", "--as", TRIAGER)

    assert asked.exit_code == 0, asked.output
    assert "intake:needs-info" in asked.stdout and "nothing is sent" in asked.stdout
    assert "bh work outbox show" in asked.stdout
    assert woken.exit_code == 0 and f"✓ woke {report}" in woken.stdout


def test_asking_a_born_native_report_is_refused_clearly(rig) -> None:
    native = rig.store.create(ReportDraft("from a human", "bug", "origin:report", "x"))
    rig.store.set_state(native, "intake", "untriaged", reason="filed", actor="x")

    result = ask(native)

    assert result.exit_code == 1 and doc(result)["reason"] == "not_a_report"
    assert "not bound to an external issue" in doc(result)["error"]
    assert rig.store.read_outbox(native) is None


def test_asking_with_unread_evidence_is_refused_naming_the_fix(rig) -> None:
    report, issue = rig.report()
    rig.world.add_comment(issue, "trace attached", at=at(3), author="reporter")
    ingest(report)

    result = ask(report)

    assert doc(result)["reason"] == "unread_evidence"
    assert "bh work thread considered" in doc(result)["error"]


# -- the sweep and argument errors ----------------------------------------------------------------


def test_wake_all_needs_its_own_switch_and_wakes_the_answered_reports(rig) -> None:
    answered, issue = rig.report("one")
    silent, _ = rig.report("two")
    assert ask(answered).exit_code == 0 and ask(silent).exit_code == 0
    rig.world.add_comment(issue, "here", at=at(3), author="reporter")
    ingest(answered)

    refused = wake("--all")
    assert refused.exit_code == 1 and "intake.external.needs_info.sweep" in doc(refused)["error"]

    rig.configure(on(sweep=True))
    swept = wake("--all")

    assert swept.exit_code == 0, swept.output
    assert doc(swept)["woken"] == [answered]
    assert [item["report"] for item in doc(swept)["items"]] == [answered, silent]
    assert "intake:needs-info" in labels(rig, silent)


@pytest.mark.parametrize("argv", [(), ("w-1", "--all")])
def test_wake_needs_exactly_one_of_a_report_or_all(rig, argv) -> None:
    result = bh("wake", *argv, "--hive", "widget")

    assert result.exit_code == 2 and "exactly one of" in result.output


# -- the link catch-up ----------------------------------------------------------------------------


def _merged_work(rig: Wired) -> tuple[str, str]:
    """A work bead an imported canonical report tracks, merged (the trigger ran) and closed."""
    canonical, _ = rig.report("canonical")
    work = rig.store.create_work(WorkDraft("fix it", "bug", "dev/t"))
    assert bh("link", canonical, "--to", work, "--hive", "widget", "--as", TRIAGER).exit_code == 0
    result = intake_services.work_merged(
        rig.cfg, rig.entry, rig.hive, [work], "a" * 40, "merger/t", lock=rig.lock
    )
    assert result.code == 0, result.error
    rig.store.mark(work, status="closed")
    return canonical, work


def test_a_link_to_merged_work_catches_up_only_with_its_switch(rig) -> None:
    keep_open = {"completion_policy": "close-on-release"}
    rig.configure(external(**keep_open))
    _canonical, work = _merged_work(rig)
    plain, _ = rig.report("late one")

    off = bh("link", plain, "--to", work, "--hive", "widget", "--as", TRIAGER, "--json")

    assert off.exit_code == 0 and "milestones" not in doc(off)
    assert doc(off)["delivery"] == "pending"

    rig.configure(external(milestones={"catch_up_on_link": True}, **keep_open))
    late, _ = rig.report("late two")
    caught = bh("link", late, "--to", work, "--hive", "widget", "--as", TRIAGER, "--json")
    human_late, _ = rig.report("late three")
    human = bh("link", human_late, "--to", work, "--hive", "widget", "--as", TRIAGER)

    assert caught.exit_code == 0, caught.output
    (item,) = doc(caught)["milestones"]
    assert item["event"] == "merged" and item["delivery"] == "merged"
    assert "delivery:merged" in labels(rig, late)
    assert "caught up" in human.stdout and "delivery:merged" in human.stdout
    assert rig.world.write_requests() == 0
