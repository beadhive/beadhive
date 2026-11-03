"""The progress milestones through the composition root and the work layer (bh-dev42).

``intake_services.work_started`` (the claim hook's root function) and ``delivery_notice`` (``bh
work delivery blocked|deprioritised``) compose the config switches, the OPERATIONAL
``ReportStore`` (the in-memory one, standing in for bd) and the root lock. What only the
composition adds is pinned here, on the public-store fixture of the import composition:

* off by default: ``draft_on_start`` is a pure config read that needs ``intake.external.enabled``,
  the outbox and ``milestones.draft_on_start``; each notice switch refuses naming its key before
  any store is built;
* a progress milestone only DRAFTS: zero tracker requests in every case, no sender is bound, and
  the byte-fingerprinted public store is identical after each;
* ``work_intake.on_work_claimed`` never raises and writes only to stderr;
* the two verbs are catalogued ``work delivery blocked`` / ``work delivery deprioritised`` leaves
  and render one human or ``--json`` result.

No live GitHub, no clock, no sleep: ``MemoryWorld``, an in-process lock, deterministic ids.
"""

from __future__ import annotations

import json

import pytest
import typer
from tests.test_intake_import_composition import ACTOR, Rig

from beadhive import intake_services, work, work_intake
from beadhive.modules.intake.domain import OpState, WorkDraft
from beadhive.testing.intake import SequentialIdSource, draft

WAITING = "Waiting on an upstream fix in the parser library."
DRAFTING = {
    "enabled": True,
    "outbox": {"enabled": True},
    "milestones": {
        "draft_on_start": True,
        "draft_on_blocked": True,
        "draft_on_deprioritised": True,
    },
}


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Rig:
    return Rig(tmp_path, monkeypatch)


def ids_of(rig: Rig) -> SequentialIdSource:
    if not hasattr(rig, "ids"):
        rig.ids = SequentialIdSource()
    return rig.ids


def make_work(rig: Rig, title: str = "fix the crash") -> str:
    return rig.store.create_work(WorkDraft(title, "bug", ACTOR))


def linked_report(rig: Rig, work_id: str, title: str = "login is broken") -> str:
    bead = rig.store.create(draft(title, bound=True))
    rig.store.track(bead, work_id, actor=ACTOR)
    rig.store.set_state(bead, "delivery", "pending", reason="link", actor=ACTOR)
    return bead


def cfg_with(rig: Rig, **external):
    return {**rig.cfg, "intake": {"external": external}}


def started(rig: Rig, work_id: str, cfg=None):
    return intake_services.work_started(
        cfg_with(rig, **DRAFTING) if cfg is None else cfg,
        rig.entry,
        rig.hive,
        work_id,
        "dev/alice",
        lock=rig.lock,
        ids=ids_of(rig),
    )


def notice(rig: Rig, work_id: str, verb: str = "blocked", reason: str = WAITING, cfg=None):
    return intake_services.delivery_notice(
        cfg_with(rig, **DRAFTING) if cfg is None else cfg,
        rig.entry,
        rig.hive,
        work_id,
        verb,
        reason,
        "alice",
        lock=rig.lock,
        ids=ids_of(rig),
    )


def records(rig: Rig, report: str):
    doc = rig.store.read_outbox(report)
    return sorted((doc.records if doc else {}).values(), key=lambda r: r.created_seq)


# -- the config keys ---------------------------------------------------------------------------


def test_the_progress_switches_are_off_by_default_and_strict() -> None:
    _outbox, milestones = intake_services.milestone_settings({})
    assert not (
        milestones.draft_on_start
        or milestones.draft_on_blocked
        or milestones.draft_on_deprioritised
    )
    _outbox, milestones = intake_services.milestone_settings(
        {
            "intake": {
                "external": {
                    "milestones": {
                        "draft_on_start": True,
                        "draft_on_blocked": "true",  # only a literal true switches anything on
                        "draft_on_deprioritised": 1,
                    }
                }
            }
        }
    )
    assert milestones.draft_on_start
    assert not milestones.draft_on_blocked and not milestones.draft_on_deprioritised


@pytest.mark.parametrize(
    ("external", "on"),
    [
        ({}, False),
        ({"enabled": True, "outbox": {"enabled": True}}, False),
        ({"enabled": True, "milestones": {"draft_on_start": True}}, False),
        ({"outbox": {"enabled": True}, "milestones": {"draft_on_start": True}}, False),
        (DRAFTING, True),
    ],
)
def test_draft_on_start_needs_intake_the_outbox_and_its_own_switch(external, on) -> None:
    assert intake_services.draft_on_start({"intake": {"external": external}}) is on


# -- the claim hook's root function ------------------------------------------------------------


def test_the_first_claim_drafts_a_started_record_and_sends_nothing(rig) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)

    result = started(rig, target)

    (item,) = result.outcome.items
    assert result.code == 0 and item.report == report and item.drafted.startswith("ob1_")
    (record,) = records(rig, report)
    assert record.event.value == "started" and record.operations[0].state is OpState.DRAFTED
    assert "delivery:pending" in rig.store.get(report).labels  # no dimension moved
    rig.assert_public_store_untouched()


def test_a_second_claim_of_the_same_work_drafts_nothing(rig) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)
    started(rig, target)
    writes = rig.store.writes

    again = started(rig, target)

    assert again.outcome.items[0].drafted == "" and rig.store.writes == writes
    assert len(records(rig, report)) == 1
    rig.assert_public_store_untouched()


def test_a_non_primary_host_is_reported_not_raised(rig, monkeypatch) -> None:
    target = make_work(rig)
    linked_report(rig, target)

    def refuse():
        raise intake_services.IntakeRefused(
            intake_services.RefusalReason.NOT_PRIMARY, "not the primary"
        )

    class Refusing:
        def hold(self, key):
            refuse()

    result = intake_services.work_started(
        cfg_with(rig, **DRAFTING), rig.entry, rig.hive, target, "dev/a", lock=Refusing()
    )

    assert result.code == 0 and "not the primary" in result.outcome.items[0].error
    rig.assert_public_store_untouched()


# -- the operator notices ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("external", "verb", "reason", "key"),
    [
        (None, "blocked", "intake_disabled", "intake.external.enabled"),
        ({"enabled": True}, "blocked", "outbox_disabled", "intake.external.outbox.enabled"),
        (
            {"enabled": True, "outbox": {"enabled": True}},
            "blocked",
            "intake_disabled",
            "intake.external.milestones.draft_on_blocked",
        ),
        (
            {
                "enabled": True,
                "outbox": {"enabled": True},
                "milestones": {"draft_on_blocked": True},
            },
            "deprioritised",
            "intake_disabled",
            "intake.external.milestones.draft_on_deprioritised",
        ),
    ],
)
def test_each_off_switch_refuses_naming_its_key_before_any_store_is_built(
    rig, external, verb, reason, key
) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)
    built, writes = rig.builds, rig.store.writes

    off = {k: v for k, v in rig.cfg.items() if k != "intake"}
    result = notice(rig, target, verb, cfg=off if external is None else cfg_with(rig, **external))

    assert (result.code, result.reason) == (1, reason) and key in result.error
    assert rig.builds == built and rig.store.writes == writes and records(rig, report) == []
    rig.assert_public_store_untouched()


def test_an_unknown_notice_verb_is_refused(rig) -> None:
    result = notice(rig, make_work(rig), "started")

    assert (result.code, result.reason) == (1, "invalid_request") and rig.builds == 0


@pytest.mark.parametrize("verb", ["blocked", "deprioritised"])
def test_a_notice_drafts_the_public_reason_and_sends_nothing(rig, verb) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)

    result = notice(rig, target, verb)

    (item,) = result.outcome.items
    assert result.code == 0 and item.drafted.startswith("ob1_") and not item.held
    (record,) = records(rig, report)
    assert record.event.value == verb and WAITING in record.message.text
    rig.assert_public_store_untouched()


def test_a_reason_naming_a_bead_id_leaves_the_record_held(rig) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)

    result = notice(rig, target, reason=f"Waiting for {target} to land first.")

    assert result.code == 0 and result.outcome.items[0].held
    (record,) = records(rig, report)
    assert record.operations[0].state is OpState.HELD
    rig.assert_public_store_untouched()


def test_an_empty_reason_or_unknown_work_is_refused_with_its_code(rig) -> None:
    target = make_work(rig)
    linked_report(rig, target)

    assert notice(rig, target, reason="  ").reason == "reason_required"
    assert notice(rig, "wd-missing").reason == "not_found"


# -- the command bodies ------------------------------------------------------------------------


def _render(result, verb, work_id, *, as_json=False, capsys):
    code = 0
    try:
        work_intake.render_delivery_notice(result, verb, work_id, as_json)
    except typer.Exit as exit_:
        code = exit_.exit_code
    out = capsys.readouterr()
    return code, out.out, out.err


def test_render_human_names_each_report_the_held_records_and_that_nothing_is_sent(
    rig, capsys
) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)

    code, out, err = _render(notice(rig, target), "blocked", target, capsys=capsys)
    assert code == 0 and f"{report}: drafted record ob1_" in out and "nothing is sent" in out

    other = make_work(rig, "another")
    linked_report(rig, other)
    held = notice(rig, other, reason=f"Waiting for {target}.")
    code, out, err = _render(held, "blocked", other, capsys=capsys)
    assert code == 0 and "held by the leak check" in out and "bh work outbox show" in err

    code, out, _ = _render(notice(rig, make_work(rig, "lonely")), "blocked", "x", capsys=capsys)
    assert code == 0 and "no report tracks x" in out

    off = notice(rig, target, cfg={k: v for k, v in rig.cfg.items() if k != "intake"})
    code, out, err = _render(off, "blocked", target, capsys=capsys)
    assert code == 1 and out == "" and err.startswith("✗ blocked notice not drafted (intake_")


def test_render_json_is_one_stable_document(rig, capsys) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)

    _, out, _ = _render(
        notice(rig, target, "deprioritised"), "deprioritised", target, as_json=True, capsys=capsys
    )
    doc = json.loads(out)
    code, out, _ = _render(
        notice(rig, target, reason=""), "blocked", target, as_json=True, capsys=capsys
    )
    refused = json.loads(out)

    assert (doc["ok"], doc["verb"], doc["work"]) == (True, "deprioritised", target)
    (row,) = doc["reports"]
    assert row["report"] == report and row["drafted"].startswith("ob1_") and row["held"] is False
    assert code == 1 and (refused["ok"], refused["reason"]) == (False, "reason_required")


def test_the_commands_are_the_catalogued_three_token_verbs() -> None:
    from beadhive.kernel import operations

    for verb in ("blocked", "deprioritised"):
        name = f"work.delivery.{verb}"
        assert list(work.CLI_HANDLERS).count(name) == 1
        (spec,) = [s for s in operations.operations() if s.name == name]
        assert spec.surfaces["cli"]["path"] == f"work delivery {verb}"
    assert callable(work.delivery_blocked_cmd) and callable(work.delivery_deprioritised_cmd)


# -- the claim hook in the work layer ----------------------------------------------------------


def test_on_work_claimed_is_a_pure_config_read_when_off(rig, monkeypatch) -> None:
    def refuse(*args, **kwargs):
        raise AssertionError("the claim hook did work with draft_on_start off")

    monkeypatch.setattr(intake_services, "work_started", refuse)

    for external in ({}, {"enabled": True, "outbox": {"enabled": True}}):
        work_intake.on_work_claimed(cfg_with(rig, **external), rig.entry, rig.hive, ["w"], "dev/a")
    work_intake.on_work_claimed({"intake": "garbage"}, rig.entry, rig.hive, ["w"], "dev/a")

    assert rig.builds == 0


def test_on_work_claimed_prints_what_it_drafted_on_stderr_only(rig, capsys, monkeypatch) -> None:
    target = make_work(rig)
    report = linked_report(rig, target)
    real = intake_services.work_started
    monkeypatch.setattr(
        intake_services,
        "work_started",
        lambda c, e, d, w, actor, **k: real(c, e, d, w, actor, lock=rig.lock, ids=ids_of(rig)),
    )

    work_intake.on_work_claimed(cfg_with(rig, **DRAFTING), rig.entry, rig.hive, [target], "dev/a")

    out = capsys.readouterr()
    assert out.out == "" and f"{report}: drafted record ob1_" in out.err
    assert "nothing is sent" in out.err and len(records(rig, report)) == 1


@pytest.mark.parametrize(
    "behaviour",
    [
        RuntimeError("boom"),
        intake_services.MilestoneResult(1, "not primary", "not_primary"),
    ],
    ids=["raises", "refused"],
)
def test_on_work_claimed_never_raises(rig, capsys, monkeypatch, behaviour) -> None:
    def hook(*args, **kwargs):
        if isinstance(behaviour, Exception):
            raise behaviour
        return behaviour

    monkeypatch.setattr(intake_services, "work_started", hook)

    work_intake.on_work_claimed(cfg_with(rig, **DRAFTING), rig.entry, rig.hive, ["w"], "dev/a")

    err = capsys.readouterr().err
    assert "the claim stands" in err
