"""The production thread service and the ``bh work thread`` verbs (``bh-o9rep``, wire minor).

``intake_services.thread_call`` composes ``ThreadService`` over the OPERATIONAL store, the root
lock, the store-backed outbox view and, for ingestion, the tracker the hive's public-store binding
serves (requesting ``comments.read``), with this hive's own identity. What is pinned here, through
the registered CLI and a real ``TrackerAdapters`` over ``MemoryWorld``:

* off by default: ``intake.external.comments.enabled`` (and ``intake.external.enabled``) gate every
  verb with a message naming the key; ingestion also needs the plugin's ``read.comments``;
* added, edited and deleted comments ingest; ``thread show`` reports new evidence; ``thread
  considered --through <seq>`` clears what was read, is idempotent, and a later comment is new
  evidence again;
* a comment this install posted through the outbox is classified ``own`` and never counted as
  new evidence;
* ingestion only reads: the world sees no write request from any thread verb.

No live GitHub, no clock, no sleep: ``MemoryWorld`` timestamps are explicit, the lock in-process.
"""

from __future__ import annotations

import json

import pytest
from tests.test_intake_outbox_wiring import Wired, external
from tests.unit.bootstrap.test_tracker_composition import plugin, roots
from typer.testing import CliRunner

from beadhive import cli, intake_services
from beadhive.modules.intake import ReportDraft
from beadhive.testing.tracker import at

ENV = {"BH_SKIP_SETUP_CHECK": "1", "NO_COLOR": "1", "COLUMNS": "200"}
OPERATOR = "ops/alice"
TRIAGER = "dev/triager"
READS = {"tracker-github": plugin(comments=True, comment=True)}
COMMENTS_ON = external(comments={"enabled": True})


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Wired:
    rig = Wired(tmp_path, monkeypatch, READS)
    rig.configure(COMMENTS_ON)
    return rig


def bh(*argv: str):
    return CliRunner().invoke(cli.app, ["work", *argv], env=ENV)


def doc(result) -> dict:
    return json.loads(result.stdout)


def thread(rig: Wired, report: str) -> dict:
    shown = bh("thread", "show", report, "--hive", "widget", "--json")
    assert shown.exit_code == 0, shown.output
    return doc(shown)["thread"]


def ingest(report: str):
    return bh("thread", "ingest", report, "--hive", "widget", "--as", TRIAGER, "--json")


def roles(view: dict) -> dict[str, tuple[str, str]]:
    return {c["id"]: (c["role"], c["state"]) for c in view["comments"]}


# -- off by default -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["show", "w-1"],
        ["ingest", "w-1", "--as", TRIAGER],
        ["considered", "w-1", "--through", "1", "--as", TRIAGER],
    ],
)
@pytest.mark.parametrize(
    ("cfg", "key"),
    [
        ({}, "opt-in"),
        (external(), "intake.external.comments.enabled"),
        (external(comments={"enabled": False}), "intake.external.comments.enabled"),
    ],
)
def test_every_thread_verb_refuses_naming_the_switch_while_it_is_off(rig, argv, cfg, key) -> None:
    rig.configure(cfg)

    result = bh("thread", *argv, "--hive", "widget", "--json")

    assert result.exit_code == 1
    assert doc(result)["reason"] == "intake_disabled" and key in doc(result)["error"]
    assert rig.store.writes == 0 and rig.world.read_requests() == 0


def test_ingestion_refuses_naming_the_plugin_keys_while_the_tracker_is_off(rig) -> None:
    report, _issue = rig.report()
    rig.plugin_off()
    reads = rig.world.read_requests()

    result = ingest(report)

    assert result.exit_code == 1 and doc(result)["reason"] == "intake_disabled"
    assert "plugins.tracker-github.enabled" in doc(result)["error"]
    assert "read.comments.enabled" in doc(result)["error"]
    assert thread(rig, report) is None  # the stored read needs no tracker
    assert rig.world.read_requests() == reads


def test_ingestion_refuses_with_the_binding_message_when_comment_reads_are_not_enabled(
    tmp_path, monkeypatch
) -> None:
    rig = Wired(tmp_path, monkeypatch, {"tracker-github": plugin(comments=False)})
    rig.configure(COMMENTS_ON)
    report, _issue = rig.report()

    result = ingest(report)

    assert result.exit_code == 1
    assert "plugins.tracker-github.read.comments.enabled" in doc(result)["error"]


def test_a_report_that_is_not_bound_has_no_thread_to_ingest_or_consider(rig) -> None:
    native = rig.store.create(ReportDraft("from a human", "bug", "origin:report", "x"))

    ingested = ingest(native)
    considered = bh(
        "thread", "considered", native, "--through", "0", "--hive", "widget", "--as", TRIAGER
    )

    assert ingested.exit_code == 1 and doc(ingested)["reason"] == "not_bound"
    assert considered.exit_code == 1 and "not bound" in considered.output


def test_considered_needs_a_real_seq(rig) -> None:
    result = bh("thread", "considered", "w-1", "--through", "-1", "--hive", "widget", "--json")

    assert result.exit_code == 2 and doc(result)["reason"] == "bad_argument"


# -- added, edited and deleted comments, new evidence and the watermark ---------------------------


def test_added_edited_and_deleted_comments_ingest_and_considered_clears_new_evidence(rig) -> None:
    report, issue = rig.report()
    first = rig.world.add_comment(issue, "It crashes on login.", at=at(3), author="alice")
    second = rig.world.add_comment(issue, "Me too.", at=at(4), author="bob")

    ingested = ingest(report)

    assert ingested.exit_code == 0, ingested.output
    steps = doc(ingested)["outcomes"]
    assert [s["step"] for s in steps] == ["ingest", "reconcile"]
    assert {e["kind"] for e in steps[0]["events"]} >= {"added"}
    view = thread(rig, report)
    assert view["new_evidence"] == [first.id, second.id]
    assert roles(view) == {first.id: ("external", "live"), second.id: ("external", "live")}

    seq = view["seq"]
    considered = bh(
        "thread", "considered", report, "--through", str(seq), "--hive", "widget",
        "--as", TRIAGER, "--json",
    )  # fmt: skip
    assert considered.exit_code == 0 and doc(considered)["outcome"]["written"] is True
    assert thread(rig, report)["new_evidence"] == []
    again = bh(
        "thread", "considered", report, "--through", str(seq), "--hive", "widget",
        "--as", TRIAGER, "--json",
    )  # fmt: skip
    assert again.exit_code == 0 and doc(again)["outcome"]["written"] is False  # idempotent

    rig.world.edit_comment(first, "It crashes on login, every time.", at=at(5))
    rig.world.delete_comment(second)
    assert ingest(report).exit_code == 0
    view = thread(rig, report)
    assert view["new_evidence"] == [first.id]  # the edit is new evidence; the delete pending
    edited = next(c for c in view["comments"] if c["id"] == first.id)
    assert (edited["rev"], edited["body"]) == (2, "It crashes on login, every time.")
    assert roles(view)[second.id] == ("external", "live")  # one absence is not a deletion

    assert ingest(report).exit_code == 0  # the second agreeing complete snapshot
    view = thread(rig, report)
    assert roles(view)[second.id] == ("external", "tombstoned")
    gone = next(c for c in view["comments"] if c["id"] == second.id)
    assert gone["body"] is None  # purged by default (not erasure)
    assert rig.world.write_requests() == 0  # ingestion only ever reads


def test_a_comment_after_the_read_stays_new_evidence(rig) -> None:
    report, issue = rig.report()
    rig.world.add_comment(issue, "First.", at=at(3))
    ingest(report)
    read_at = thread(rig, report)["seq"]
    later = rig.world.add_comment(issue, "Second.", at=at(4))
    ingest(report)

    bh("thread", "considered", report, "--through", str(read_at), "--hive", "widget")

    assert thread(rig, report)["new_evidence"] == [later.id]


def test_thread_show_human_output_names_the_seq_new_evidence_and_bodies(rig) -> None:
    report, issue = rig.report()
    added = rig.world.add_comment(issue, "Steps to reproduce:\n1. log in", at=at(3))
    ingest(report)

    human = bh("thread", "show", report, "--hive", "widget")
    ingested = bh("thread", "ingest", report, "--hive", "widget", "--as", TRIAGER)

    assert human.exit_code == 0
    assert f"{report} thread seq" in human.stdout and f"new evidence: {added.id}" in human.stdout
    assert "  | Steps to reproduce:" in human.stdout and "  | 1. log in" in human.stdout
    assert ingested.exit_code == 0 and "✓ ingested" in ingested.stdout
    assert "no thread recorded" in bh("thread", "show", "w-404", "--hive", "widget").stdout


# -- own outbound comments ------------------------------------------------------------------------


def test_a_reply_sent_through_the_outbox_is_own_and_never_new_evidence(rig) -> None:
    report, issue = rig.report()
    human = rig.world.add_comment(issue, "Any update?", at=at(3))
    ingest(report)
    common = ("--hive", "widget", "--as", OPERATOR, "--json")
    oid = doc(bh("outbox", "draft", report, "--text", "Fixed in main.", *common))["results"][0][
        "outbox_id"
    ]
    digest = doc(bh("outbox", "show", oid, "--hive", "widget", "--json"))["renders"][0]["digest"]
    assert bh("outbox", "approve", oid, "--op", f"comment:{digest}", *common).exit_code == 0
    rig.world.set_write_time(at(9))  # the tracker's clock for the post, after the last read
    assert bh("outbox", "send", oid, *common).exit_code == 0
    (posted,) = [c for c in rig.world.comments.values() if c.marker is not None]

    assert ingest(report).exit_code == 0

    view = thread(rig, report)
    own = next(c for c in view["comments"] if c["id"] == posted.key.id)
    assert (own["role"], own["outbox_id"]) == ("own", oid)
    assert posted.key.id not in view["new_evidence"]
    assert view["new_evidence"] == [human.id]
    assert rig.world.write_requests() == 1  # exactly the operator-approved send


def test_the_outbox_service_carries_the_thread_operations_when_comments_are_on(rig) -> None:
    report, issue = rig.report()
    rig.world.add_comment(issue, "Hello.", at=at(3))
    ingest(report)

    call = intake_services.outbox_call(
        rig.cfg, rig.entry, rig.hive, lambda port: port.thread_of(report), lock=rig.lock
    )

    assert call.code == 0 and call.value is not None and call.value.report == report


def test_the_outbox_service_has_no_thread_operations_while_comments_are_off(rig) -> None:
    rig.configure(external())

    call = intake_services.outbox_call(
        rig.cfg, rig.entry, rig.hive, lambda port: port.thread_of("w-1"), lock=rig.lock
    )

    assert call.code == 0 and call.value is None


def test_the_comments_settings_are_off_by_default_and_strict() -> None:
    assert intake_services.comments_settings({}) == (False, intake_services.ThreadPolicy())
    enabled, policy = intake_services.comments_settings(
        {
            "intake": {
                "external": {
                    "comments": {
                        "enabled": "true",  # only a literal true enables
                        "deletion_confirmations": 3,
                        "purge_deleted_bodies": False,
                        "max_body_chars": 10,
                    }
                }
            }
        }
    )
    assert enabled is False
    assert (policy.deletion_confirmations, policy.purge_deleted_bodies) == (3, False)
    assert policy.max_body_chars == 10


def test_a_reads_only_binding_is_requested_for_ingestion(rig) -> None:
    requested: list[frozenset] = []
    adapters, _ = roots(READS, factory=rig.factory)
    real = adapters.for_store

    def spy(entry, scope="public", **kwargs):
        requested.append(kwargs.get("requested", frozenset()))
        return real(entry, scope, **kwargs)

    adapters.for_store = spy
    rig.process = adapters

    wiring = intake_services.thread_wiring(rig.cfg, rig.entry, rig.hive)

    assert wiring["tracker"] is not None and wiring["marker_line"] is not None
    (ops,) = requested
    assert {op.value for op in ops} == {"comments.read"}
