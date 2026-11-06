"""``bh work labels`` and ``bh work triage`` through the production composition (bh-v3fe7).

The registered CLI runs every verb against a real ``TrackerAdapters`` over ``MemoryWorld`` (the
tracker-github plugin config with ``read.labels``, ``write.label_definitions`` and an owned
``bh:`` prefix) and the in-memory report store, with only the process seams patched. Pinned here:

* every verb has ``--help`` and one ``--json`` document; argument errors exit 2;
* off by default: each switch refuses naming its key, writing nothing and reading no tracker;
* labels: define (dry run, digest, operator-only ``--approve`` with one grant and an audit line,
  converged re-run), sync (drafts one ``labels`` record per issue, never sends), review and the
  operator-only batch approval;
* triage: review packets (with ``--out`` files), propose and ``--review-of`` verdicts (pass
  numbers and the batch skill's B / C), check, and apply (operator-only, idempotent, a reroute
  re-filed through ``--reroute-to``);
* the opt-in transition refresh on a disposition;
* the dry-run walkthrough on a fixture hive writes nothing;
* ``bh label docs``' projected label table.

No live GitHub, no clock, no sleep: ``MemoryWorld`` timestamps are explicit, the lock in-process.
"""

from __future__ import annotations

import json
import stat
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from tests.test_intake_outbox_wiring import Wired
from typer.testing import CliRunner

from beadhive import cli, git_linkage, intake_beads, intake_services, registry, work_intake
from beadhive.modules.tracker.contracts import LabelDefinition
from beadhive.testing.tracker import at

ENV = {"BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1", "COLUMNS": "200"}
OPERATOR = "ops/alice"
AGENT = "dev/triager"
PLUGINS = {
    "tracker-github": {
        "enabled": True,
        "read": {
            "issues": {"enabled": True},
            "comments": {"enabled": True},
            "labels": {"enabled": True},
        },
        "write": {
            "comment": {"enabled": True},
            "labels": {"enabled": True, "owned_prefixes": ["bh:"]},
            "label_definitions": {"enabled": True},
        },
    }
}
REJECT = {"disposition": "reject", "reporter_text": "This works as designed: see the docs."}


def on(**labels) -> dict:
    """Every switch the two verb groups read, on."""
    return {
        "intake": {
            "external": {
                "enabled": True,
                "outbox": {"enabled": True, "labels_enabled": True},
                "comments": {"enabled": True},
                "labels": {"enabled": True, **labels},
            }
        }
    }


class Rig(Wired):
    """The outbox rig with the labels plugin config and the root seams these verbs add."""

    def __init__(self, tmp_path, monkeypatch) -> None:
        super().__init__(tmp_path, monkeypatch, PLUGINS)
        self.configure(on())
        monkeypatch.setattr(
            work_intake,
            "config",
            SimpleNamespace(load=lambda: self.cfg, integration_branch=lambda cfg, entry: "main"),
        )
        monkeypatch.setattr(registry, "hive_dir_for", lambda cfg, hive: self.hive)
        monkeypatch.setattr(intake_beads, "entry_of_directory", lambda directory: self.entry)
        build = intake_services.report_store  # the rig's store, also for the directory reads

        def report_store(directory, scope, entry=None, cli_only=False):
            return build(directory, lambda: self.scope, entry=entry)

        monkeypatch.setattr(intake_services, "report_store", report_store)
        self.landed: dict[str, list[str]] = {}
        monkeypatch.setattr(
            git_linkage, "read_commits", lambda bead, main: self.landed.get(bead, [])
        )
        self.refiled: list[tuple] = []

        def file_report(hive, title, rtype, actor, cfg=None, description=""):
            self.refiled.append((hive, title, rtype, actor, description))
            return 0, "", "oth-1"

        from beadhive import report

        monkeypatch.setattr(report, "file_report", file_report)

    def outbox(self, report: str):
        return self.store.read_outbox(report)

    def labels_records(self, report: str) -> list:
        doc = self.outbox(report)
        if doc is None:
            return []
        return [r for r in doc.records.values() if r.event.value == "labels"]


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def bh(*argv: str, input: str | None = None):
    return CliRunner().invoke(cli.app, ["work", *argv], env=ENV, input=input)


def doc(result) -> dict:
    return json.loads(result.stdout)


def labels(*argv: str, actor: str = OPERATOR):
    return bh("labels", *argv, "--hive", "widget", "--as", actor, "--json")


def triage(*argv: str, actor: str = OPERATOR, input: str | None = None):
    return bh("triage", *argv, "--hive", "widget", "--as", actor, "--json", input=input)


def propose(report: str, document: dict, *extra: str, actor: str = AGENT):
    return triage("propose", report, "-", *extra, actor=actor, input=json.dumps(document))


def confirm(report: str, pass_: str = "1"):
    result = propose(report, {"verdict": "confirmed"}, "--review-of", pass_, actor="ops/verifier")
    assert result.exit_code == 0, result.output
    return result


def snapshot(rig: Rig) -> tuple:
    return (rig.store.writes, rig.world.write_requests(), rig.world.label_definitions())


# -- help and argument shape ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "phrase"),
    [
        (("labels", "sync"), "--include-closed"),
        (("labels", "define"), "--approve"),
        (("labels", "review"), "batch digest"),
        (("labels", "approve"), "--batch"),
        (("triage", "review"), "--batch-size"),
        (("triage", "propose"), "--review-of"),
        (("triage", "check"), "--pending"),
        (("triage", "apply"), "--reroute-to"),
    ],
)
def test_every_verb_has_help(argv, phrase) -> None:
    result = bh(*argv, "--help")

    assert result.exit_code == 0, result.output
    assert phrase in result.output and "--json" in result.output


def test_the_groups_are_listed_with_their_help() -> None:
    result = bh("--help")

    assert result.exit_code == 0
    assert "labels" in result.output and "triage" in result.output


@pytest.mark.parametrize(
    "argv",
    [
        ("labels", "sync", "wid-1", "--all"),
        ("triage", "review"),
        ("triage", "review", "--pending", "--batch-size", "0"),
        ("triage", "apply"),
        ("triage", "apply", "--reviewed", "--reroute-to", "nohive"),
        ("triage", "propose", "wid-1", "-", "--review-of", "A"),
        ("triage", "propose", "wid-1", "-", "--review-of", "zero"),
    ],
)
def test_argument_errors_exit_2_before_anything_runs(rig, argv) -> None:
    before = snapshot(rig)

    result = bh(*argv, "--hive", "widget", input="{}")

    assert result.exit_code == 2, result.output
    assert snapshot(rig) == before


def test_a_proposal_that_is_not_json_exits_2(rig) -> None:
    report, _issue = rig.report()

    result = triage("propose", report, "-", input="not json")

    assert result.exit_code == 2 and "not a JSON document" in result.output


# -- off by default -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "cfg", "key"),
    [
        (("labels", "sync", "--dry-run"), {}, "intake.external.enabled"),
        (("labels", "define"), {}, "intake.external.enabled"),
        (
            ("labels", "sync", "--dry-run"),
            {"intake": {"external": {"enabled": True}}},
            "intake.external.labels.enabled",
        ),
        (
            ("labels", "define"),
            {"intake": {"external": {"enabled": True, "labels": {"enabled": "true"}}}},
            "intake.external.labels.enabled",
        ),
        (("labels", "review"), {"intake": {"external": {"enabled": True}}}, "outbox.enabled"),
        (
            ("labels", "approve", "--batch", "abc"),
            {"intake": {"external": {"enabled": True}}},
            "outbox.enabled",
        ),
        (("triage", "review", "--pending"), {}, "intake.external.enabled"),
        (
            ("triage", "review", "--pending"),
            {"intake": {"external": {"enabled": True}}},
            "intake.external.comments.enabled",
        ),
        (("triage", "check"), {}, "intake.external.enabled"),
        (("triage", "apply", "--reviewed", "--dry-run"), {}, "intake.external.enabled"),
    ],
)
def test_every_verb_refuses_naming_the_switch_while_it_is_off(rig, argv, cfg, key) -> None:
    rig.report()
    rig.configure(cfg)
    before, reads = snapshot(rig), rig.world.read_requests()

    actor = () if argv == ("labels", "review") else ("--as", OPERATOR)
    result = bh(*argv, "--hive", "widget", *actor, "--json")

    assert result.exit_code == 1, result.output
    assert doc(result)["ok"] is False and key in doc(result)["error"]
    assert snapshot(rig) == before and rig.world.read_requests() == reads


def test_propose_is_off_with_intake_off(rig) -> None:
    report, _issue = rig.report()
    rig.configure({})

    result = propose(report, REJECT)

    assert result.exit_code == 1 and doc(result)["reason"] == "intake_disabled"
    assert rig.store.read_proposal(report) is None


def test_a_label_read_that_is_not_enabled_refuses_naming_the_plugin_key(rig) -> None:
    rig.report()
    plugins = json.loads(json.dumps(PLUGINS))
    plugins["tracker-github"]["read"]["labels"]["enabled"] = False
    plugins["tracker-github"]["write"]["label_definitions"]["enabled"] = False
    rig.plugins = plugins
    rig.adapters, _factory = __import__(
        "tests.unit.bootstrap.test_tracker_composition", fromlist=["roots"]
    ).roots(plugins, factory=rig.factory)
    rig.process = rig.adapters
    rig.configure(on())

    result = labels("define")

    assert result.exit_code == 1
    assert "read.labels.enabled" in doc(result)["error"]


# -- labels define ------------------------------------------------------------------------------


def test_define_shows_the_plan_and_applies_it_only_from_an_operator_seat(rig) -> None:
    rig.report()
    rig.world.seed_label_definition(LabelDefinition("human-bug", "ff0000", "theirs"))

    plan = labels("define")
    assert plan.exit_code == 0, plan.output
    body = doc(plan)["plan"]
    created = {row["name"] for row in body["create"]}
    assert {"bh:intake/untriaged", "bh:type/bug", "bh:priority/p2"} <= created
    assert all(name.startswith("bh:") for name in created)
    assert doc(plan)["applied"] is False and rig.world.write_requests() == 0

    agent = labels("define", "--approve", body["short_digest"], actor=AGENT)
    assert agent.exit_code == 1 and doc(agent)["reason"] == "operator_seat_required"
    assert rig.world.write_requests() == 0

    stale = labels("define", "--approve", "0" * 12)
    assert stale.exit_code == 1 and doc(stale)["reason"] == "stale_digest"

    applied = labels("define", "--approve", body["short_digest"])
    assert applied.exit_code == 0, applied.output
    assert doc(applied)["applied"] is True and len(doc(applied)["grant_ids"]) == 1
    names = {d.name for d in rig.world.label_definitions()}
    assert created <= names and "human-bug" in names  # theirs untouched
    audit = rig.hive / ".bh" / "intake" / intake_services.AUDIT_LOG_FILE
    lines = audit.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1 and json.loads(lines[0])["actor"] == OPERATOR
    assert stat.S_IMODE(audit.stat().st_mode) == 0o600

    again = labels("define")
    assert doc(again)["plan"]["changed"] is False and "converged" in doc(again)["detail"]


def test_define_prune_deletes_through_the_bound_pruner_and_keeps_carried_labels(rig) -> None:
    _report, issue = rig.report()
    rig.world.seed_label_definition(LabelDefinition("bh:intake/obsolete", "ededed", "gone"))
    rig.world.seed_label_definition(LabelDefinition("bh:intake/carried", "ededed", "kept"))
    rig.world.add_human_label(issue, "bh:intake/carried")

    plan = doc(labels("define", "--prune"))["plan"]
    assert plan["delete"] == ["bh:intake/obsolete"]
    assert [k["name"] for k in plan["kept"]] == ["bh:intake/carried"]

    applied = labels("define", "--prune", "--approve", plan["short_digest"])

    assert applied.exit_code == 0, applied.output
    assert len(doc(applied)["grant_ids"]) == 2  # one per tracker write: define, then delete
    names = {d.name for d in rig.world.label_definitions()}
    assert "bh:intake/obsolete" not in names and "bh:intake/carried" in names


def test_define_dry_run_and_approve_together_are_refused(rig) -> None:
    result = labels("define", "--dry-run", "--approve", "abc")

    assert result.exit_code == 1 and doc(result)["reason"] == "invalid_request"


def test_define_human_output_names_the_approve_command(rig) -> None:
    rig.report()

    result = bh("labels", "define", "--hive", "widget", "--as", OPERATOR)

    assert result.exit_code == 0, result.output
    assert "create bh:intake/untriaged" in result.output
    assert "bh work labels define --approve" in result.output


# -- labels sync, review and approve --------------------------------------------------------------


def _defined(rig: Rig) -> None:
    plan = doc(labels("define"))["plan"]
    applied = labels("define", "--approve", plan["short_digest"])
    assert applied.exit_code == 0, applied.output


def test_sync_drafts_one_record_per_issue_and_the_batch_is_approved_by_an_operator(rig) -> None:
    report, issue = rig.report()
    rig.world.add_human_label(issue, "human-bug")
    _defined(rig)
    writes = rig.world.write_requests()

    dry = labels("sync", "--dry-run")
    assert dry.exit_code == 0, dry.output
    (item,) = doc(dry)["items"]
    assert "bh:intake/untriaged" in item["add"] and item["drafted"] == ""
    assert rig.labels_records(report) == []

    synced = labels("sync", report)
    assert synced.exit_code == 0, synced.output
    (item,) = doc(synced)["items"]
    assert item["drafted"].startswith("ob1_") and doc(synced)["drafted"] == [item["drafted"]]
    assert len(rig.labels_records(report)) == 1
    again = labels("sync")
    assert doc(again)["items"][0]["pending"] == item["drafted"]  # found, never stacked
    assert len(rig.labels_records(report)) == 1

    review = bh("labels", "review", "--hive", "widget", "--json")
    assert review.exit_code == 0, review.output
    batch = doc(review)["batch"]
    assert [i["outbox_id"] for i in batch["items"]] == [item["drafted"]]

    refused = labels("approve", "--batch", batch["short_digest"], actor=AGENT)
    assert refused.exit_code == 1 and doc(refused)["reason"] == "operator_seat_required"

    approved = labels("approve", "--batch", batch["short_digest"])
    assert approved.exit_code == 0, approved.output
    assert len(doc(approved)["grant_ids"]) == 1 and doc(approved)["failed"] == []
    assert rig.world.write_requests() == writes  # approving sends nothing
    assert rig.world.server_labels(issue) >= {"human-bug"}


def _with_values(rig: Rig) -> None:
    """Rebind the rig's tracker with ``write.issue_type`` and ``write.issue_fields`` on."""
    from tests.unit.bootstrap.test_tracker_composition import roots

    plugins = json.loads(json.dumps(PLUGINS))
    plugins["tracker-github"]["write"]["issue_type"] = {"enabled": True}
    plugins["tracker-github"]["write"]["issue_fields"] = {"enabled": True}
    rig.plugins = plugins
    rig.adapters, _factory = roots(plugins, factory=rig.factory)
    rig.process = rig.adapters
    rig.configure(on())


def test_sync_drafts_the_type_and_priority_with_the_labels_and_sends_each_kind(rig) -> None:
    _with_values(rig)
    report, issue = rig.report()
    rig.store.update_fields(report, issue_type="bug", priority="1", actor=OPERATOR)
    _defined(rig)

    synced = labels("sync", report)
    assert synced.exit_code == 0, synced.output
    (item,) = doc(synced)["items"]
    assert item["values"] == [
        {"name": "type", "live": "", "desired": "Bug"},
        {"name": "Priority", "live": "", "desired": "High"},
    ]
    assert doc(synced)["value_drift"] == {report: ["type (unset) → Bug", "Priority (unset) → High"]}
    human = bh("labels", "sync", "--dry-run", "--hive", "widget", "--as", OPERATOR)
    assert "type (unset) → Bug; Priority (unset) → High" in human.output

    batch = doc(bh("labels", "review", "--hive", "widget", "--json"))["batch"]
    assert [i["kind"] for i in batch["items"]] == ["labels.change", "issue.type", "issue.fields"]
    assert batch["items"][1]["values"] == [["type", "Bug"]]
    shown = bh("labels", "review", "--hive", "widget")
    assert "issue.type type=Bug" in shown.output and "issue.fields Priority=High" in shown.output

    approved = labels("approve", "--batch", batch["short_digest"])
    assert approved.exit_code == 0, approved.output
    assert len(doc(approved)["grant_ids"]) == 3
    oid = item["drafted"]
    common = ("--hive", "widget", "--as", OPERATOR, "--json")
    assert bh("outbox", "send", oid, "--kind", "labels", *common).exit_code == 0
    labelled = rig.world.server_labels(issue)
    for kind in ("type", "fields"):
        sent = bh("outbox", "send", oid, "--kind", kind, *common)
        assert sent.exit_code == 0 and doc(sent)["results"][0]["state"] == "sent", sent.output

    assert rig.world.server_issue_type(issue) == "Bug"
    assert rig.world.server_issue_fields(issue) == {"Priority": "High"}
    assert rig.world.server_labels(issue) == labelled  # labels unchanged by the value writes
    again = labels("sync", report)
    assert doc(again)["items"][0]["values"] == [] and doc(again)["drafted"] == []


def test_without_the_value_switches_sync_drafts_the_labels_only(rig) -> None:
    report, issue = rig.report()
    rig.store.update_fields(report, issue_type="bug", priority="1", actor=OPERATOR)
    _defined(rig)

    (item,) = doc(labels("sync", report))["items"]

    assert item["values"] == [] and item["value_notes"] == []
    batch = doc(bh("labels", "review", "--hive", "widget", "--json"))["batch"]
    assert [i["kind"] for i in batch["items"]] == ["labels.change"]
    refused = bh(
        "outbox",
        "send",
        item["drafted"],
        "--kind",
        "type",
        "--hive",
        "widget",
        "--as",
        OPERATOR,
        "--json",
    )
    assert refused.exit_code == 1 and doc(refused)["reason"] == "type_disabled"
    assert rig.world.server_issue_type(issue) == ""


def test_sync_human_output_and_an_empty_review(rig) -> None:
    rig.report()

    review = bh("labels", "review", "--hive", "widget")
    sync = bh("labels", "sync", "--dry-run", "--hive", "widget", "--as", OPERATOR)

    assert review.exit_code == 0 and "no drafted label change" in review.output
    assert sync.exit_code == 0, sync.output
    assert "labels sync (dry run: nothing written)" in sync.output


def test_a_sync_that_cannot_draft_exits_1(rig) -> None:
    rig.report()  # nothing is defined, so every add is refused

    result = labels("sync")

    assert result.exit_code == 1 and doc(result)["ok"] is False
    (item,) = doc(result)["items"]
    assert item["undefined"] and "labels define" in item["error"]


# -- triage review --------------------------------------------------------------------------------


def test_review_builds_packets_with_the_full_thread_and_writes_out_files(rig, tmp_path) -> None:
    first, issue = rig.report("Login is broken on save")
    second, _ = rig.report("Login is broken on save!")
    rig.world.add_comment(issue, "Still broken on 1.2.", at=at(3), author="reporter")

    result = triage("review", "--pending", "--batch-size", "5")
    assert result.exit_code == 0, result.output
    body = doc(result)
    assert body["reports"] == [first, second]
    packets = {p["report"]: p for b in body["batches"] for p in b["packets"]}
    assert [c["body"] for c in packets[first]["thread"]["comments"]] == ["Still broken on 1.2."]
    assert packets[first]["cluster"] == packets[second]["cluster"]  # one family
    assert {e["report"] for e in body["index"]} == {first, second}

    out = tmp_path / "packets"
    written = triage("review", "--pending", "--out", str(out))
    assert written.exit_code == 0, written.output
    assert sorted(p.name for p in out.iterdir()) == ["batch-001.json", "index.json"]
    batch = json.loads((out / "batch-001.json").read_text())
    assert [p["report"] for p in batch["packets"]] == [first, second]
    assert len(batch["index"]) == 2
    index = json.loads((out / "index.json").read_text())
    assert index["batches"] == [{"number": 1, "reports": [first, second], "file": "batch-001.json"}]
    assert "packets" not in json.dumps(doc(written)["batches"])


def test_review_human_output(rig) -> None:
    rig.report()

    result = bh("triage", "review", "--pending", "--hive", "widget", "--as", AGENT)

    assert result.exit_code == 0, result.output
    assert "1 pending report(s) in 1 batch(es)" in result.output


# -- triage propose and --review-of --------------------------------------------------------------


def test_propose_then_review_of_records_pass_numbers_and_the_skill_letters(rig) -> None:
    report, _issue = rig.report()

    stored = propose(report, REJECT)
    assert stored.exit_code == 0, stored.output
    assert doc(stored)["kind"] == "created" and doc(stored)["status"] == "proposed"

    unsure = propose(report, {"verdict": "unsure"}, "--review-of", "B", actor="ops/b")
    assert unsure.exit_code == 0, unsure.output
    assert doc(unsure)["pass"] == 1 and doc(unsure)["status"] == "proposed"

    replay = propose(report, {"verdict": "unsure"}, "--review-of", "B", actor="ops/b")
    assert replay.exit_code == 1 and doc(replay)["reason"] == "proposal_moved"

    conflict = propose(report, {"verdict": "confirmed", "pass": 3}, "--review-of", "C")
    assert conflict.exit_code == 1 and "pass 3" in doc(conflict)["error"]

    confirmed = propose(report, {"verdict": "confirmed", "pass": "C"}, "--review-of", "2")
    assert confirmed.exit_code == 0, confirmed.output
    reviews = doc(confirmed)["proposal"]["reviews"]
    assert [(r["pass"], r["verdict"]) for r in reviews] == [(1, "unsure"), (2, "confirmed")]
    assert doc(confirmed)["status"] == "verified"


def test_the_batch_skills_spelling_parses(rig) -> None:
    """``propose <report> --json -`` and ``--review-of <pass> --json -`` (bh-skls-p0v): the
    ``--json`` output flag, then the ``-`` document source."""
    report, _issue = rig.report()

    stored = bh(
        "triage", "propose", report, "--json", "-", "--hive", "widget", input=json.dumps(REJECT)
    )
    verdict = bh(
        "triage",
        "propose",
        report,
        "--review-of",
        "B",
        "--json",
        "-",
        "--hive",
        "widget",
        "--as",
        "ops/b",
        input=json.dumps({"verdict": "confirmed"}),
    )

    assert stored.exit_code == 0, stored.output
    assert verdict.exit_code == 0, verdict.output
    assert doc(verdict)["status"] == "verified" and doc(verdict)["pass"] == 1


def test_propose_reads_a_file_and_refuses_a_leaking_reporter_text(rig, tmp_path) -> None:
    report, _issue = rig.report()
    path = tmp_path / "proposal.json"
    path.write_text(json.dumps({"disposition": "reject", "reporter_text": f"see {report}"}))

    result = bh("triage", "propose", report, str(path), "--hive", "widget", "--json")

    assert result.exit_code == 1 and doc(result)["reason"] == "unresolved_finding"
    assert rig.store.read_proposal(report) is None


def test_propose_human_output(rig) -> None:
    report, _issue = rig.report()

    result = bh("triage", "propose", report, "-", "--hive", "widget", input=json.dumps(REJECT))

    assert result.exit_code == 0, result.output
    assert f"✓ {report}: proposal created (reject, proposed, seq 1)" in result.output


# -- triage check and apply -----------------------------------------------------------------------


def test_check_reports_findings_and_exits_1(rig) -> None:
    good, _ = rig.report("First")
    bad, _ = rig.report("Second")
    propose(good, REJECT)
    propose(bad, {"disposition": "tracked", "targets": ["wid-missing"]})

    result = triage("check")

    assert result.exit_code == 1
    rows = {row["report"]: row for row in doc(result)["checks"]}
    assert rows[good]["passed"] is True and rows[good]["plan"] == "blocked"  # not reviewed yet
    assert rows[bad]["passed"] is False
    assert [f["kind"] for f in rows[bad]["findings"]] == ["owner_missing"]
    assert doc(result)["failed"] == [bad]


def test_check_resolves_every_issue_a_reply_mentions_with_one_listing(rig) -> None:
    """bh-iamru: a #N that is no issue of the bound project fails the check; a #N that is another
    bound report is reported as a relates-to; the run lists the issues once."""
    original, original_issue = rig.report("First")
    missing, _ = rig.report("Second")
    related, _ = rig.report("Third")
    named = original_issue.identifier
    propose(original, REJECT)
    propose(missing, {"disposition": "reject", "reporter_text": f"See {named} and #999."})
    propose(related, {"disposition": "reject", "reporter_text": f"Answered in {named}."})
    reads = rig.world.read_requests()

    result = triage("check")

    assert result.exit_code == 1
    rows = {row["report"]: row for row in doc(result)["checks"]}
    assert [f["kind"] for f in rows[missing]["findings"]] == ["mention_missing"]
    assert "#999" in rows[missing]["findings"][0]["detail"]
    assert rows[related]["passed"] is True and rows[related]["relates"] == [original]
    assert rows[original]["relates"] == []
    assert rig.world.read_requests() - reads == 1  # one listing (one page) for the whole run
    assert rig.world.write_requests() == 0


def test_check_pending_keeps_only_reports_still_awaiting_triage(rig) -> None:
    kept, _ = rig.report("First")
    fielded, _ = rig.report("Second")
    propose(kept, REJECT)
    propose(fielded, REJECT)
    accepted = bh("accept", fielded, "--hive", "widget", "--as", OPERATOR)
    assert accepted.exit_code == 0, accepted.output

    every = doc(triage("check"))
    pending = doc(triage("check", "--pending"))

    assert {row["report"] for row in every["checks"]} == {kept, fielded}
    assert [row["report"] for row in pending["checks"]] == [kept]


def test_apply_is_operator_only_drafts_never_sends_and_is_idempotent(rig) -> None:
    report, issue = rig.report()
    propose(report, REJECT)
    confirm(report)
    comments = rig.world.comment_count(issue)

    planned = triage("apply", "--reviewed", "--dry-run", actor=AGENT)
    assert planned.exit_code == 0, planned.output
    (item,) = doc(planned)["items"]
    assert item["status"] == "planned" and item["steps"] == ["draft reply", "reject"]

    refused = triage("apply", "--reviewed", actor=AGENT)
    assert refused.exit_code == 1 and doc(refused)["reason"] == "operator_seat_required"
    assert rig.store.read_proposal(report).status.value == "verified"

    applied = triage("apply", "--reviewed")
    assert applied.exit_code == 0, applied.output
    (item,) = doc(applied)["items"]
    assert item["status"] == "applied" and item["drafted"][0].startswith("ob1_")
    assert rig.store.get(report).status == "closed"
    assert rig.world.comment_count(issue) == comments  # drafted, never sent

    again = triage("apply", "--reviewed")
    assert doc(again)["items"] == [] and doc(again)["already_applied"] == [report]


def test_a_reroute_is_refiled_only_where_reroute_to_sends_it(rig) -> None:
    report, issue = rig.report("Belongs elsewhere")
    propose(report, {"disposition": "reroute"})
    confirm(report)

    blocked = triage("apply", "--reviewed", "--dry-run")
    (item,) = doc(blocked)["items"]
    assert item["status"] == "blocked" and "re-filer" in item["detail"]

    planned = triage("apply", "--reviewed", "--dry-run", "--reroute-to", f"{report}=skills")
    (item,) = doc(planned)["items"]
    assert item["status"] == "planned" and item["reroute_to"] == "skills"
    assert rig.refiled == []

    other = triage("apply", "--reviewed", "--reroute-to", "wid-other=skills")
    (item,) = doc(other)["items"]
    assert item["status"] == "failed" and "--reroute-to" in item["detail"]
    assert rig.store.get(report).status != "closed" and rig.refiled == []

    applied = triage("apply", "--reviewed", "--reroute-to", f"{report}=skills")
    assert applied.exit_code == 0, applied.output
    assert rig.refiled == [
        ("skills", "Belongs elsewhere", "bug", OPERATOR, f"Rerouted from {issue.url}")
    ]
    assert rig.store.get(report).status == "closed"


def test_apply_human_output_lists_each_item(rig) -> None:
    report, _issue = rig.report()
    propose(report, REJECT)

    result = bh("triage", "apply", "--reviewed", "--dry-run", "--hive", "widget", "--as", AGENT)

    assert result.exit_code == 0, result.output
    assert "triage apply (dry run: nothing written): 0 applied, 0 planned, 1 blocked" in (
        result.output
    )
    assert f"blocked  {report}: reject; not reviewed" in result.output


# -- the opt-in transition refresh ----------------------------------------------------------------


def test_a_disposition_refreshes_the_labels_record_only_with_on_transition(rig) -> None:
    off, _ = rig.report("First")
    accepted = bh("accept", off, "--hive", "widget", "--as", OPERATOR)
    assert accepted.exit_code == 0, accepted.output
    assert rig.labels_records(off) == []

    rig.configure(on(on_transition=True))
    report, _ = rig.report("Second")
    accepted = bh("accept", report, "--hive", "widget", "--as", OPERATOR)

    assert accepted.exit_code == 0, accepted.output
    (record,) = rig.labels_records(report)
    op = record.op(record.operations[0].kind)
    assert "bh:intake/accepted" in op.labels.add
    assert rig.world.write_requests() == 0


def test_a_link_refreshes_the_labels_record_through_the_link_composer(rig) -> None:
    from beadhive.modules.intake.domain import WorkDraft

    rig.configure(on(on_transition=True))
    report, _ = rig.report()
    work = rig.store.create_work(WorkDraft("fix it", "feature", "dev/t"))

    linked = bh("link", report, "--to", work, "--hive", "widget", "--as", OPERATOR, "--json")

    assert linked.exit_code == 0, linked.output
    (record,) = rig.labels_records(report)
    op = record.op(record.operations[0].kind)
    assert {"bh:delivery/pending", "bh:type/feature"} <= op.labels.add
    assert rig.world.write_requests() == 0


# -- the dry-run walkthrough ----------------------------------------------------------------------


def test_the_dry_run_walkthrough_on_a_fixture_hive_writes_nothing(rig) -> None:
    """The walkthrough documented in docs/design/external-intake-module-adr.md: define, sync with
    the closed reports, the review packets, the checks and the apply plan, all dry."""
    first, issue = rig.report("Crash on save")
    closed, _ = rig.report("Old crash")
    rig.world.add_comment(issue, "Happens on 1.2 too.", at=at(3), author="reporter")
    for report in (first, closed):
        ingested = bh("thread", "ingest", report, "--hive", "widget", "--as", AGENT)
        assert ingested.exit_code == 0, ingested.output
    seq = doc(bh("thread", "show", first, "--hive", "widget", "--json"))["thread"]["seq"]
    propose(first, {**REJECT, "thread_seq": seq})
    confirm(first)
    reject = bh("reject", closed, "--reason", "fixed long ago", "--hive", "widget")
    assert reject.exit_code == 0, reject.output
    before, outbox = snapshot(rig), rig.store.outbox_writes[:]

    steps = [
        labels("define", "--dry-run"),
        labels("sync", "--dry-run", "--include-closed"),
        triage("review", "--pending", actor=AGENT),
        triage("check", actor=AGENT),
        triage("apply", "--reviewed", "--dry-run", actor=AGENT),
    ]

    assert [s.exit_code for s in steps] == [0, 0, 0, 0, 0], [s.output for s in steps]
    define, sync, review, check, apply = (doc(s) for s in steps)
    assert define["plan"]["create"] and define["applied"] is False
    assert {i["report"] for i in sync["items"]} == {first, closed} and sync["dry_run"] is True
    assert review["reports"] == [first]
    assert [row["report"] for row in check["checks"]] == [first] and check["failed"] == []
    assert [(i["report"], i["status"]) for i in apply["items"]] == [(first, "planned")]
    assert snapshot(rig) == before and rig.store.outbox_writes == outbox
    assert not (rig.hive / ".bh" / "intake" / intake_services.AUDIT_LOG_FILE).exists()


# -- the root's translations ---------------------------------------------------------------------


def test_the_labels_section_translates_into_the_modules_settings() -> None:
    cfg = {
        "intake": {
            "external": {
                "labels": {
                    "enabled": True,
                    "on_transition": True,
                    "prefix": "bh:",
                    "project": ["intake", "type"],
                    "aliases": {"intake": "triage"},
                    "colors": {"type": "ABCDEF"},
                    "overrides": {"type/bug": {"name": "bh:bug"}},
                }
            }
        }
    }

    setting = intake_services.labels_config(cfg)

    assert setting.enabled and setting.on_transition and setting.error == ""
    assert setting.settings.project == ("intake", "type")
    assert setting.settings.group("intake") == "triage"
    assert setting.settings.colors["type"] == "abcdef"
    assert setting.settings.overrides["type/bug"].name == "bh:bug"
    assert intake_services.labels_config({}).enabled is False
    loose = {"intake": {"external": {"labels": {"enabled": True, "on_transition": "yes"}}}}
    assert intake_services.labels_config(loose).on_transition is False
    bad = {"intake": {"external": {"labels": {"enabled": True, "colors": {"type": "red"}}}}}
    assert intake_services.labels_config(bad).settings is None


def test_the_registry_dimensions_and_the_projected_label_table() -> None:
    cfg = {
        "dimensions": {
            "component": {"description": "Area of the code"},
            "intake": {"values": ["spam"], "description": "Triage state"},
        },
        **on(),
    }

    dims = intake_services.registry_dimensions(cfg)
    rows = intake_services.projected_label_rows(cfg)
    lines = intake_services.projected_label_lines(cfg)

    assert dims["component"].values is None and dims["intake"].values == frozenset({"spam"})
    names = {row["name"] for row in rows}
    assert {"bh:intake/spam", "bh:intake/untriaged", "bh:component/<value>"} <= names
    assert lines[0] == "## Projected GitHub labels"
    assert "| `bh:intake/spam` | `#fbca04` | Triage state: spam |" in lines
    assert intake_services.projected_label_lines({}) == []


def test_label_docs_renders_the_projected_table(tmp_path, monkeypatch) -> None:
    out = tmp_path / "labels.md"
    cfg = {"dimensions": {}, "managed_repos": [], **on()}
    monkeypatch.setattr(registry.config, "load", lambda: cfg)
    monkeypatch.setattr(registry.config, "docs_path", lambda: out)
    monkeypatch.setattr(registry, "effective_providers", lambda cfg: ["github"])

    result = CliRunner().invoke(cli.app, ["label", "docs"], env=ENV)

    assert result.exit_code == 0, result.output
    text = out.read_text()
    assert "## Projected GitHub labels" in text and "`bh:type/bug`" in text


def test_the_audit_log_and_landed_commits_adapters(tmp_path) -> None:
    path = tmp_path / ".bh" / "intake" / "audit.log"
    log = intake_services.FileAuditLog(path, clock=lambda: datetime(2026, 11, 5, tzinfo=UTC))

    log.note("labels define abc applied", actor=OPERATOR)
    log.note("again", actor=OPERATOR)

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows[0] == {
        "actor": OPERATOR,
        "at": "2026-11-05T00:00:00+00:00",
        "note": "labels define abc applied",
    }
    assert len(rows) == 2
    landed = intake_services.LinkageLandedCommits(tmp_path, read=lambda bead: ["a" * 40])
    assert landed.landed_commits("wid-1") == ("a" * 40,)


@pytest.mark.parametrize(("text", "expected"), [("1", 1), ("2", 2), ("12", 12), ("B", 1), ("c", 2)])
def test_review_of_takes_a_pass_number_or_the_skills_letters(text, expected) -> None:
    assert intake_services.review_pass(text) == expected


@pytest.mark.parametrize("text", ["A", "0", "-1", "D", ""])
def test_review_of_refuses_anything_else(text) -> None:
    with pytest.raises(ValueError):
        intake_services.review_pass(text)
