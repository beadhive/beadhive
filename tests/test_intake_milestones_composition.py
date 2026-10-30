"""The merge and release milestones through the composition root (I10), end to end.

``intake_services.work_merged`` / ``delivery_release`` compose the config switches, the OPERATIONAL
``ReportStore`` (the in-memory one, standing in for bd), the root lock and a ``ReleaseVerifier``
(``FakeReleaseVerifier``, or ``GitReleaseVerifier`` over a scripted git runner). What only the
composition adds is pinned here, on the public-store fixture of the import composition:

* off by default: with ``intake.external.enabled`` absent the merge trigger builds nothing and the
  release verb refuses ``intake_disabled`` before it asks the verifier anything;
* a milestone only DRAFTS: the world sees zero write requests across every case, no sender is
  bound, and the byte-fingerprinted public store is identical after each (``SCOPE_PUBLIC`` is never
  resolved);
* the config keys parse strictly (only a literal ``true`` switches a draft on);
* the release verb verifies the tag first (an unknown tag records nothing), and the command
  body renders the outcome; the Typer function is the catalogued `work delivery release`.

No live GitHub, no clock, no sleep: ``MemoryTracker``, an in-process lock and scripted git only.
"""

from __future__ import annotations

import json

import pytest
import typer
from tests.test_intake_import_composition import ACTOR, Rig

from beadhive import guard, intake_services, work, work_intake
from beadhive.adapters.intake import GitReleaseVerifier
from beadhive.modules.intake import (
    IssueBinding,
    IssueStatus,
    OwnIdentity,
    ReportDraft,
    SourceSnapshot,
)
from beadhive.modules.intake.domain import (
    OpKind,
    OpState,
    OutboxApprove,
    OutboxPolicy,
    Verdict,
    WorkDraft,
)
from beadhive.modules.tracker.contracts import READ_OPERATIONS, WRITE_OPERATIONS
from beadhive.testing.intake import FakeReleaseVerifier, SequentialIdSource, draft
from beadhive.testing.tracker import MemoryTracker, at

MERGE_SHA = "a" * 40
OTHER_SHA = "b" * 40
RELEASE_SHA = "c" * 40
DRAFTING = {
    "enabled": True,
    "outbox": {"enabled": True},
    "milestones": {"draft_on_merge": True, "draft_on_release": True},
}


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def ids_of(rig: Rig) -> SequentialIdSource:
    """One deterministic id source per rig, so two triggers never mint the same record id."""
    if not hasattr(rig, "ids"):
        rig.ids = SequentialIdSource()
    return rig.ids


def make_work(rig: Rig, title: str = "fix the crash") -> str:
    return rig.store.create_work(WorkDraft(title, "bug", ACTOR))


def bound_draft(issue, locator: str) -> ReportDraft:
    """A bound report on a REAL memory issue (so a send has somewhere to post)."""
    return ReportDraft(
        "a bug",
        "bug",
        "origin:github",
        ACTOR,
        binding=IssueBinding("", "memory", locator, issue.url, issue.identifier),
        snapshot=SourceSnapshot("a bug", "", IssueStatus.OPEN, "r1"),
    )


def cfg_with(rig: Rig, **external):
    return {**rig.cfg, "intake": {"external": external}}


def linked_report(rig: Rig, work_id: str, title: str = "login is broken") -> str:
    bead = rig.store.create(draft(title, bound=True))
    rig.store.track(bead, work_id, actor=ACTOR)
    rig.store.set_state(bead, "delivery", "pending", reason="link", actor=ACTOR)
    return bead


def merged(rig: Rig, work_id: str, cfg=None, commit: str = MERGE_SHA, **kwargs):
    return intake_services.work_merged(
        rig.cfg if cfg is None else cfg,
        rig.entry,
        rig.hive,
        [work_id],
        commit,
        "merger/test",
        **{"lock": rig.lock, "ids": ids_of(rig), **kwargs},
    )


def released(rig: Rig, tag: str, cfg, verifier, **kwargs):
    return intake_services.delivery_release(
        cfg,
        rig.entry,
        rig.hive,
        tag,
        "alice",
        **{"verifier": verifier, "lock": rig.lock, "ids": ids_of(rig), **kwargs},
    )


def verifier(*contained: str) -> FakeReleaseVerifier:
    return FakeReleaseVerifier(
        tags={"v1.0.0": RELEASE_SHA}, contained={RELEASE_SHA: frozenset(contained)}
    )


# -- the config keys ---------------------------------------------------------------------------


def test_the_milestone_settings_are_off_by_default_and_strict() -> None:
    outbox, milestones = intake_services.milestone_settings({})
    assert outbox.enabled is False and not milestones.draft_on_merge
    assert not milestones.draft_on_release and not milestones.append_release_to_closed
    assert milestones.completion.value == "close-on-merge"

    outbox, milestones = intake_services.milestone_settings(
        {
            "intake": {
                "external": {
                    "completion_policy": "close-on-release",
                    "outbox": {"enabled": "yes"},  # only a literal true enables anything
                    "milestones": {"draft_on_merge": True, "draft_on_release": 1},
                }
            }
        }
    )
    assert outbox.enabled is False and milestones.draft_on_merge is True
    assert milestones.draft_on_release is False
    assert milestones.completion.value == "close-on-release"

    _, odd = intake_services.milestone_settings(
        {"intake": {"external": {"completion_policy": "close-never"}}}
    )
    assert odd.completion.value == "close-on-release"  # unrecognised: keep the report open


def _labels_gate(outbox: dict) -> bool:
    policy, _ = intake_services.milestone_settings({"intake": {"external": {"outbox": outbox}}})
    return policy.labels_enabled


def test_the_labels_gate_is_off_by_default_and_needs_its_own_literal_true_beside_the_outbox():
    assert intake_services.milestone_settings({})[0].labels_enabled is False
    assert _labels_gate({"enabled": True}) is False  # the comment sender's switch never opens it
    assert _labels_gate({"labels_enabled": True}) is False  # nor does it open without the outbox
    assert _labels_gate({"enabled": True, "labels_enabled": "yes"}) is False
    assert _labels_gate({"enabled": True, "labels_enabled": 1}) is False
    assert _labels_gate({"enabled": True, "labels_enabled": True}) is True


# -- off by default ----------------------------------------------------------------------------


def test_with_intake_off_the_merge_trigger_builds_nothing_and_the_release_verb_refuses(rig):
    target = make_work(rig)
    linked_report(rig, target)
    off = {k: v for k, v in rig.cfg.items() if k != "intake"}
    scripted = verifier(MERGE_SHA)
    built, writes = rig.builds, rig.store.writes

    merge = merged(rig, target, off)
    release = released(rig, "v1.0.0", off, scripted)

    assert (merge.code, merge.outcome.items) == (0, ())  # free: nothing read, nothing written
    assert (release.code, release.reason) == (1, "intake_disabled") and "opt-in" in release.error
    assert scripted.resolved == [] and scripted.asked == []  # the verifier was never asked
    assert rig.builds == built and rig.store.writes == writes and rig.lock.holds == []
    rig.assert_public_store_untouched()


def test_an_explicitly_false_switch_is_off_too(rig) -> None:
    target = make_work(rig)
    linked_report(rig, target)

    result = merged(rig, target, cfg_with(rig, enabled=False))

    assert result.outcome.items == () and rig.builds == 0


# -- the merge trigger -------------------------------------------------------------------------


def test_the_merge_trigger_records_delivery_and_completes_when_the_outbox_is_off(rig) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)
    duplicate = linked_report(rig, target, "same bug")
    rig.store.mark(duplicate, status="closed")

    result = merged(rig, target)

    assert result.code == 0 and [i.report for i in result.outcome.items] == [report, duplicate]
    assert "delivery:merged" in rig.store.get(report).labels
    assert "delivery:merged" in rig.store.get(duplicate).labels  # a closed duplicate hears of it
    assert rig.store.get(report).status == "closed"  # close-on-merge, no outbox: complete at merge
    assert rig.store.reports_with_outbox() != ()  # the merged fact is recorded for the release
    rig.assert_public_store_untouched()


def test_with_drafting_on_the_trigger_leaves_a_drafted_record_and_sends_nothing(rig) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)

    result = merged(rig, target, cfg_with(rig, **DRAFTING))

    (item,) = result.outcome.items
    assert item.delivery == "merged" and item.drafted.startswith("ob1_") and not item.closed
    (record,) = rig.store.read_outbox(report).records.values()
    assert record.event.value == "merged" and record.operations[0].state is OpState.DRAFTED
    assert rig.store.get(report).status == "open"  # waits for the record to be told or discarded
    assert rig.world.write_requests() == 0 and rig.world.read_requests() == 0
    rig.assert_public_store_untouched()


def test_a_merge_with_no_tracking_report_is_a_no_op(rig) -> None:
    target = make_work(rig)
    writes = rig.store.writes

    result = merged(rig, target, cfg_with(rig, **DRAFTING))

    assert result.code == 0 and result.outcome.items == () and rig.store.writes == writes


def test_a_non_primary_host_is_refused_before_anything_is_written(rig, monkeypatch) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)

    def refuse(hive, *, cfg, verb):
        raise typer.Exit(1)

    monkeypatch.setattr(guard, "guard_primary", refuse)
    writes = rig.store.writes

    result = merged(rig, target, lock=None)

    assert result.code == 0  # the per-report refusal is reported in the item, not raised
    assert (
        result.outcome.items[0].error and "not the hive's primary" in result.outcome.items[0].error
    )
    assert rig.store.writes == writes and "delivery:pending" in rig.store.get(report).labels


def test_the_default_lock_is_the_roots_primary_guarded_file_lock(rig, monkeypatch) -> None:
    target = make_work(rig)
    linked_report(rig, target)
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        guard, "guard_primary", lambda hive, *, cfg, verb: calls.append((hive, verb))
    )

    result = merged(rig, target, lock=None, hive="widgets")

    assert result.code == 0 and calls == [("widgets", "work merge")]
    assert len(list((rig.hive / ".bh" / "intake" / "locks").glob("*.lock"))) == 1


# -- the release verb --------------------------------------------------------------------------


def test_an_unknown_tag_is_refused_before_anything_is_recorded(rig) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)
    cfg = cfg_with(rig, **DRAFTING)
    merged(rig, target, cfg)
    writes = rig.store.writes
    scripted = verifier(MERGE_SHA)

    result = released(rig, "v9.9.9", cfg, scripted)

    assert (result.code, result.reason) == (1, "unknown_tag") and "v9.9.9" in result.error
    assert scripted.resolved == ["v9.9.9"] and scripted.asked == []
    assert rig.store.writes == writes and "delivery:merged" in rig.store.get(report).labels
    rig.assert_public_store_untouched()


def test_a_verified_release_sets_released_and_drafts_a_record_without_sending(rig) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)
    cfg = cfg_with(rig, **DRAFTING)
    merged(rig, target, cfg)

    result = released(rig, "v1.0.0", cfg, verifier(MERGE_SHA))

    assert (result.code, result.tag, result.commit) == (0, "v1.0.0", RELEASE_SHA)
    (item,) = result.outcome.items
    assert item.delivery == "released" and item.verdict is Verdict.YES
    assert item.drafted.startswith("ob1_")
    assert "delivery:released" in rig.store.get(report).labels
    events = sorted(r.event.value for r in rig.store.read_outbox(report).records.values())
    assert events == ["merged", "released"]
    assert rig.world.write_requests() == 0
    rig.assert_public_store_untouched()


def test_a_release_that_does_not_contain_the_merge_is_reported_not_verified(rig) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)
    cfg = cfg_with(rig, **DRAFTING)
    merged(rig, target, cfg)

    result = released(rig, "v1.0.0", cfg, verifier(OTHER_SHA))

    (item,) = result.outcome.items
    assert item.delivery == "" and item.verdict is Verdict.NO and "not verified" in item.note
    assert "delivery:merged" in rig.store.get(report).labels
    assert rig.store.read_outbox(report).facts.released is None


def test_close_on_release_keeps_the_report_open_until_the_release_is_verified(rig) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)
    cfg = cfg_with(rig, enabled=True, completion_policy="close-on-release")

    merged(rig, target, cfg)
    assert rig.store.get(report).status == "open"  # merged but unreleased: open and held
    assert report not in rig.store.ready_ids()
    assert "delivery:merged" in rig.store.get(report).labels

    result = released(rig, "v1.0.0", cfg, verifier(MERGE_SHA))

    assert result.outcome.items[0].closed and rig.store.get(report).status == "closed"


def test_the_git_verifier_is_the_default_for_the_release_verb(rig, monkeypatch) -> None:
    target = make_work(rig)
    linked_report(rig, target)
    cfg = cfg_with(rig, enabled=True)
    merged(rig, target, cfg)  # merge fact recorded for MERGE_SHA, then closed (no outbox)
    seen: list = []

    def runner(args):
        seen.append(tuple(args))
        if args[0] == "rev-parse":
            return 0, RELEASE_SHA + "\n"
        return 0, ""  # merge-base --is-ancestor: yes

    monkeypatch.setattr(
        "beadhive.adapters.intake.release_verifier._subprocess_git", lambda directory: runner
    )

    result = intake_services.delivery_release(
        cfg, rig.entry, rig.hive, "v1.0.0", "alice", lock=rig.lock, ids=ids_of(rig)
    )

    assert result.code == 0 and result.commit == RELEASE_SHA
    assert result.outcome.items[0].verdict is Verdict.YES
    # read-only against git: only rev-parse and merge-base were ever asked
    assert {a[0] for a in seen} == {"rev-parse", "merge-base"}


# -- the sender completes the report it just told ------------------------------------------------


def test_the_composed_sender_closes_the_report_once_its_milestone_comment_is_sent(
    rig, monkeypatch
) -> None:
    target = make_work(rig)
    issue = rig.world.add_issue("a bug", at=at(1))
    rig.world.set_write_time(at(2))
    report = rig.store.create(bound_draft(issue, rig.world.locator))
    rig.store.track(report, target, actor=ACTOR)
    rig.store.set_state(report, "delivery", "pending", reason="link", actor=ACTOR)
    cfg = cfg_with(rig, enabled=True, milestones={"draft_on_merge": True}, outbox={"enabled": True})
    merged(rig, target, cfg)
    assert rig.store.get(report).status == "open"
    (record,) = rig.store.read_outbox(report).records.values()
    tracker = MemoryTracker(rig.world, enabled=frozenset(READ_OPERATIONS | WRITE_OPERATIONS))
    service = intake_services.outbox_service_for(
        cfg,
        rig.entry,
        rig.hive,
        tracker=tracker,
        identity=OwnIdentity("acme/widgets", "inst-1"),
        marker_line=lambda m: f"<!-- bh:v1 outbox={m.outbox_id} -->",
        policy=OutboxPolicy(enabled=True),
        lock=rig.lock,
        ids=SequentialIdSource(start=100),
    )

    shown = service.render(record.outbox_id).of(OpKind.COMMENT)
    service.approve(OutboxApprove(record.outbox_id, shown.short_digest, "alice"))
    sent = service.send(record.outbox_id, actor="alice")

    assert sent.state is OpState.SENT and sent.notified == "merged"
    assert "notified:merged" in rig.store.get(report).labels
    assert rig.store.get(report).status == "closed" and "closed" in sent.detail
    assert rig.world.write_requests() == 1  # the operator's one approved send


# -- the command body -------------------------------------------------------------


def _render(result, tag, *, as_json=False, capsys):
    code = 0
    try:
        work_intake.render_delivery_release(result, tag, as_json)
    except typer.Exit as exit_:
        code = exit_.exit_code
    out = capsys.readouterr()
    return code, out.out, out.err


def test_render_human_names_the_tag_the_reports_and_that_nothing_is_sent(rig, capsys) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)
    cfg = cfg_with(rig, **DRAFTING)
    merged(rig, target, cfg)

    code, out, _ = _render(
        released(rig, "v1.0.0", cfg, verifier(MERGE_SHA)), "v1.0.0", capsys=capsys
    )

    assert code == 0 and f"verified v1.0.0 → {RELEASE_SHA[:12]}" in out
    assert f"{report}: delivery:released" in out and "nothing is sent" in out


def test_render_refusals_and_not_verified(rig, capsys) -> None:
    target = make_work(rig)
    linked_report(rig, target)
    cfg = cfg_with(rig, **DRAFTING)
    merged(rig, target, cfg)

    code, out, err = _render(released(rig, "nope", cfg, verifier()), "nope", capsys=capsys)
    assert code == 1 and out == "" and err.startswith("✗ release not recorded (unknown_tag): ")

    code, out, _ = _render(released(rig, "v1.0.0", cfg, verifier()), "v1.0.0", capsys=capsys)
    assert code == 0 and "not verified" in out  # nothing failed: it is simply not in this release

    off = {k: v for k, v in rig.cfg.items() if k != "intake"}
    code, _, err = _render(released(rig, "v1.0.0", off, verifier()), "v1.0.0", capsys=capsys)
    assert code == 1 and "intake_disabled" in err

    empty = _render(
        released(rig, "v1.0.0", cfg_with(rig, **DRAFTING), verifier()), "v1.0.0", capsys=capsys
    )
    assert empty[0] == 0


def test_render_json_is_one_stable_document(rig, capsys) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)
    cfg = cfg_with(rig, **DRAFTING)
    merged(rig, target, cfg)

    _, out, _ = _render(
        released(rig, "v1.0.0", cfg, verifier(MERGE_SHA)), "v1.0.0", as_json=True, capsys=capsys
    )
    doc = json.loads(out)
    code, out, _ = _render(
        released(rig, "nope", cfg, verifier()), "nope", as_json=True, capsys=capsys
    )
    refused = json.loads(out)

    assert (doc["ok"], doc["tag"], doc["commit"]) == (True, "v1.0.0", RELEASE_SHA)
    (row,) = doc["reports"]
    assert (row["report"], row["delivery"], row["verdict"]) == (report, "released", "yes")
    assert row["drafted"].startswith("ob1_") and row["error"] == ""
    assert code == 1 and (refused["ok"], refused["reason"]) == (False, "unknown_tag")


def test_the_command_is_the_catalogued_three_token_verb() -> None:
    assert callable(work.delivery_release_cmd) and callable(work_intake.delivery_release)
    assert list(work.CLI_HANDLERS).count("work.delivery.release") == 1
    from beadhive.kernel import operations

    (spec,) = [s for s in operations.operations() if "delivery" in s.name]
    assert spec.name == "work.delivery.release"
    assert spec.surfaces["cli"]["path"] == "work delivery release"


def test_on_work_merged_prints_what_it_did_and_never_raises(rig, capsys, monkeypatch) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)
    cfg = cfg_with(rig, **DRAFTING)
    real = intake_services.work_merged
    monkeypatch.setattr(
        intake_services,
        "work_merged",
        lambda c, e, d, works, commit, actor, **k: real(
            c, e, d, works, commit, actor, lock=rig.lock, ids=ids_of(rig)
        ),
    )
    monkeypatch.setattr("beadhive.worktree._ref_sha", lambda main, ref: MERGE_SHA)
    monkeypatch.setattr("beadhive.identity.resolve_actor", lambda as_: "merger/test")

    work_intake.on_work_merged(cfg, rig.entry, rig.hive, "main", [target], "widgets")

    out = capsys.readouterr()
    assert f"{report}: delivery:merged" in out.out and "nothing is sent" in out.out
    assert out.err == ""


# -- the git verifier --------------------------------------------------------------------------


class Git:
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def __call__(self, args):
        self.asked.append(tuple(args))
        return self.answers.get(args[0], (128, ""))


def test_the_git_verifier_resolves_tags_and_answers_ancestry_read_only() -> None:
    git = Git({"rev-parse": (0, RELEASE_SHA + "\n"), "merge-base": (0, "")})
    v = GitReleaseVerifier("/hive", run=git)

    assert v.resolve("v1.0.0") == RELEASE_SHA
    assert v.contains(RELEASE_SHA, MERGE_SHA) is Verdict.YES
    assert git.asked == [
        ("rev-parse", "--verify", "--quiet", "refs/tags/v1.0.0^{commit}"),
        ("merge-base", "--is-ancestor", MERGE_SHA, RELEASE_SHA),
    ]
    assert {a[0] for a in git.asked} <= {"rev-parse", "merge-base"}  # never a write verb


@pytest.mark.parametrize(
    ("answer", "verdict"),
    [((0, ""), Verdict.YES), ((1, ""), Verdict.NO), ((128, ""), Verdict.UNKNOWN)],
)
def test_only_an_exit_status_of_zero_or_one_is_a_verdict(answer, verdict) -> None:
    assert (
        GitReleaseVerifier("/hive", run=Git({"merge-base": answer})).contains(
            RELEASE_SHA, MERGE_SHA
        )
        is verdict
    )


@pytest.mark.parametrize(
    "tag", ["", "-d", "--help", "a b", "a..b", "v1.", "v1/", "x.lock", "$(id)", "v1;rm", "\nx"]
)
def test_a_tag_that_could_be_read_as_an_option_or_a_range_never_reaches_git(tag) -> None:
    git = Git({"rev-parse": (0, RELEASE_SHA)})

    assert GitReleaseVerifier("/hive", run=git).resolve(tag) is None
    assert git.asked == []


def test_a_missing_tag_or_a_non_commit_answer_resolves_to_none() -> None:
    assert GitReleaseVerifier("/hive", run=Git({"rev-parse": (1, "")})).resolve("v1") is None
    assert (
        GitReleaseVerifier("/hive", run=Git({"rev-parse": (0, "not-a-sha")})).resolve("v1") is None
    )


@pytest.mark.parametrize(("release", "merge"), [("HEAD", MERGE_SHA), (RELEASE_SHA, "-x"), ("", "")])
def test_a_commit_that_is_not_plain_hex_is_unknown_without_asking_git(release, merge) -> None:
    git = Git({"merge-base": (0, "")})

    assert GitReleaseVerifier("/hive", run=git).contains(release, merge) is Verdict.UNKNOWN
    assert git.asked == []
