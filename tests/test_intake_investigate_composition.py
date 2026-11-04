"""``bh work investigate`` through the composition root and the work layer (bh-u3a53).

The import rig with the production outbox wiring (``Wired``: the in-memory operational
``ReportStore`` standing in for bd, a ``MemoryWorld`` tracker, the root lock over ``tmp_path``).
What only the composition adds is pinned here:

* off by default: every form refuses ``intake_disabled`` before any store is built;
* ``--work``, ``--new-work``, ``--work --remove`` and the listing render one human or ``--json``
  result, and ``bh work thread show`` lists both kinds of link;
* the key property end to end: with EVERY milestone switch on, the root functions behind the claim
  hook, ``bh work delivery blocked|deprioritised``, the merge hook, ``bh work delivery release`` and
  the link catch-up never reach a report the work only investigates, while a report tracking the
  same bead hears each one; the work layer's claim and merge hooks print nothing for it;
* the public store is never resolved and no tracker request is made.

No live GitHub, no clock, no sleep.
"""

from __future__ import annotations

import json

import pytest
from tests.test_intake_outbox_wiring import ENV, Wired, external
from tests.unit.bootstrap.test_tracker_composition import plugin
from typer.testing import CliRunner

from beadhive import cli, intake_services, work, work_intake
from beadhive.modules.intake.domain import WorkDraft
from beadhive.testing.intake import FakeReleaseVerifier, SequentialIdSource

TRIAGER = "dev/triager"
PLUGINS = {"tracker-github": plugin(comments=True, comment=True)}
MERGE_SHA = "a" * 40
RELEASE_SHA = "d" * 40
TAG = "v1.2.0"
WAITING = "Waiting on an upstream fix in the parser library."
EVERYTHING = {
    "comments": {"enabled": True},
    "milestones": {
        "draft_on_merge": True,
        "draft_on_release": True,
        "append_release_to_closed": True,
        "draft_on_start": True,
        "draft_on_blocked": True,
        "draft_on_deprioritised": True,
        "catch_up_on_link": True,
    },
}


@pytest.fixture
def rig(tmp_path, monkeypatch) -> Wired:
    rig = Wired(tmp_path, monkeypatch, PLUGINS)
    rig.configure(external(**EVERYTHING))
    rig.ids = SequentialIdSource()
    return rig


def bh(*argv: str):
    return CliRunner().invoke(cli.app, ["work", *argv], env=ENV)


def investigate(*argv: str):
    return bh("investigate", *argv, "--hive", "widget", "--as", TRIAGER)


def doc(result) -> dict:
    return json.loads(result.stdout)


def repro(rig: Wired, title: str = "Reproduce: a bug") -> str:
    return rig.store.create_work(WorkDraft(title, "task", "dev/t"))


def untouched(rig: Wired, report: str) -> None:
    view = rig.store.get(report)
    assert view is not None and view.status == "open"
    assert rig.store.read_outbox(report) is None
    assert not [x for x in view.labels if x.startswith(("delivery:", "notified:"))]
    assert report not in rig.store.ready_ids()


def events(rig: Wired, report: str) -> list[str]:
    doc_ = rig.store.read_outbox(report)
    found = sorted((doc_.records if doc_ else {}).values(), key=lambda r: r.created_seq)
    return [record.event.value for record in found]


# -- off by default ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ("--work", "acme-0001"),
        ("--new-work",),
        ("--work", "acme-0001", "--remove"),
        (),
    ],
    ids=["work", "new-work", "remove", "list"],
)
def test_every_form_refuses_while_intake_is_off_before_any_store_is_built(rig, argv) -> None:
    rig.configure({})

    result = investigate("acme-0002", *argv, "--json")

    assert result.exit_code == 1
    assert doc(result)["reason"] == "intake_disabled" and rig.builds == 0


@pytest.mark.parametrize(
    ("argv", "message"),
    [
        (("--work", "acme-1", "--new-work"), "at most one of --work"),
        (("--remove",), "--remove needs --work"),
        (("--new-work", "--remove"), "--remove needs --work"),
        (("--work", "acme-1", "--type", "bug"), "apply to the bead --new-work creates"),
    ],
)
def test_contradictory_flags_are_a_usage_error(rig, argv, message) -> None:
    result = investigate("acme-0002", *argv)

    assert result.exit_code == 2 and message in result.output and rig.builds == 0


# -- the verb --------------------------------------------------------------------------------


def test_work_new_work_listing_and_remove_round_trip(rig) -> None:
    report, _ = rig.report("Login page crashes")
    existing = repro(rig)

    linked = investigate(report, "--work", existing, "--json")
    created = investigate(report, "--new-work", "--priority", "1", "--json")
    again = investigate(report, "--work", existing, "--json")

    assert linked.exit_code == 0, linked.output
    assert doc(linked)["kind"] == "linked" and doc(linked)["created_work"] is False
    new = doc(created)["work"]
    assert doc(created)["created_work"] is True and doc(created)["kind"] == "linked"
    view = rig.store.get(new)
    assert view is not None and view.title == "Reproduce: Login page crashes"
    assert view.priority == "1" and view.binding is None
    assert doc(again)["kind"] == "already_linked"
    listing = doc(investigate(report, "--json"))
    assert listing["kind"] == "" and listing["relations"] == {
        "tracks": "",
        "progress": "unlinked",
        "investigated_by": sorted([existing, new]),
    }
    removed = investigate(report, "--work", existing, "--remove", "--json")
    assert doc(removed)["kind"] == "unlinked" and doc(removed)["work"] == existing
    assert doc(removed)["relations"]["investigated_by"] == [new]
    untouched(rig, report)
    assert rig.world.write_requests() == 0
    rig.assert_public_store_unread()


def test_human_output_says_the_bead_does_not_track_the_report(rig) -> None:
    report, _ = rig.report()
    existing = repro(rig)

    linked = investigate(report, "--work", existing)
    listing = investigate(report)
    gone = investigate(report, "--work", existing, "--remove")
    nothing = investigate(report, "--work", existing, "--remove")

    assert linked.exit_code == 0, linked.output
    assert f"{existing} now investigates {report}" in linked.stdout
    assert "drafts no delivery milestone and never closes the report" in linked.stdout
    assert f"investigated by: {existing}" in listing.stdout and "tracks: none" in listing.stdout
    assert f"{existing} no longer investigates {report}" in gone.stdout
    assert "nothing to remove" in nothing.stdout


def test_investigating_the_tracked_work_is_refused_naming_the_fix(rig) -> None:
    report, _ = rig.report()
    fix = repro(rig, "fix it")
    assert bh("link", report, "--to", fix, "--hive", "widget", "--as", TRIAGER).exit_code == 0

    refused = investigate(report, "--work", fix)

    assert refused.exit_code == 1
    assert "(tracked_work)" in refused.output and "--new-work" in refused.output


def test_thread_show_lists_both_kinds_of_link(rig) -> None:
    report, _ = rig.report()
    fix, existing = repro(rig, "fix it"), repro(rig)
    assert bh("link", report, "--to", fix, "--hive", "widget", "--as", TRIAGER).exit_code == 0
    assert investigate(report, "--work", existing).exit_code == 0

    shown = bh("thread", "show", report, "--hive", "widget", "--json")
    human = bh("thread", "show", report, "--hive", "widget")

    assert shown.exit_code == 0, shown.output
    assert doc(shown)["links"] == {
        "tracks": fix,
        "progress": "open",
        "investigated_by": [existing],
    }
    assert f"tracks: {fix} (open)" in human.stdout
    assert f"investigated by: {existing}" in human.stdout
    unknown = doc(bh("thread", "show", "acme-9999", "--hive", "widget", "--json"))
    assert unknown["thread"] is None and unknown["links"] is None  # still "no thread"


def test_the_command_is_the_catalogued_work_leaf() -> None:
    from beadhive.kernel import operations

    assert list(work.CLI_HANDLERS).count("work.investigate") == 1
    (spec,) = [s for s in operations.operations() if s.name == "work.investigate"]
    assert spec.surfaces["cli"]["path"] == "work investigate"
    assert "mcp" not in spec.surfaces  # CLI only, like the other intake verbs


# -- the key property through the root -------------------------------------------------------


def _scene(rig: Wired) -> tuple[str, str, str]:
    """A reproduction bead one report investigates and another tracks (the control)."""
    work_id = repro(rig)
    investigated, _ = rig.report("investigated")
    tracking, _ = rig.report("tracking")
    assert investigate(investigated, "--work", work_id).exit_code == 0
    linked = bh("link", tracking, "--to", work_id, "--hive", "widget", "--as", TRIAGER)
    assert linked.exit_code == 0, linked.output
    return work_id, investigated, tracking


def _claim(rig, work_id):
    return intake_services.work_started(
        rig.cfg, rig.entry, rig.hive, work_id, "dev/alice", ids=rig.ids
    )


def _notice(verb):
    def run(rig, work_id):
        return intake_services.delivery_notice(
            rig.cfg, rig.entry, rig.hive, work_id, verb, WAITING, "alice", ids=rig.ids
        )

    return run


def _merged(rig, work_id):
    rig.store.mark(work_id, status="closed")
    return intake_services.work_merged(
        rig.cfg, rig.entry, rig.hive, [work_id], MERGE_SHA, "merger/t", ids=rig.ids
    )


def _released(rig, work_id):
    assert _merged(rig, work_id).code == 0
    verifier = FakeReleaseVerifier(
        tags={TAG: RELEASE_SHA}, contained={RELEASE_SHA: frozenset({MERGE_SHA})}
    )
    return intake_services.delivery_release(
        rig.cfg, rig.entry, rig.hive, TAG, "alice", verifier=verifier, ids=rig.ids
    )


@pytest.mark.parametrize(
    ("trigger", "event"),
    [
        pytest.param(_claim, "started", id="claim-hook"),
        pytest.param(_notice("blocked"), "blocked", id="delivery-blocked"),
        pytest.param(_notice("deprioritised"), "deprioritised", id="delivery-deprioritised"),
        pytest.param(_merged, "merged", id="merge-hook"),
        pytest.param(_released, "released", id="delivery-release"),
    ],
)
def test_no_root_trigger_reaches_an_investigated_report(rig, trigger, event) -> None:
    work_id, investigated, tracking = _scene(rig)

    result = trigger(rig, work_id)

    assert result.code == 0, result.error
    assert investigated not in result.outcome.reports
    untouched(rig, investigated)
    assert tracking in result.outcome.reports and event in events(rig, tracking)
    assert rig.world.write_requests() == 0
    rig.assert_public_store_unread()


def test_investigating_merged_work_catches_nothing_up_while_a_link_does(rig) -> None:
    work_id, _investigated, tracking = _scene(rig)
    assert _merged(rig, work_id).code == 0
    late, _ = rig.report("late")
    linked_late, _ = rig.report("linked late")

    result = investigate(late, "--work", work_id, "--json")
    caught = bh("link", linked_late, "--to", work_id, "--hive", "widget", "--as", TRIAGER, "--json")

    assert result.exit_code == 0 and "milestones" not in doc(result)
    untouched(rig, late)
    assert doc(caught)["milestones"] and "merged" in events(rig, linked_late)  # the control
    assert "merged" in events(rig, tracking)


def test_the_work_layer_hooks_say_nothing_about_an_investigated_report(
    rig, capsys, monkeypatch
) -> None:
    work_id, investigated, tracking = _scene(rig)
    monkeypatch.setattr("beadhive.worktree._ref_sha", lambda main, ref: MERGE_SHA)
    monkeypatch.setattr("beadhive.identity.resolve_actor", lambda as_: "merger/test")
    capsys.readouterr()

    work_intake.on_work_claimed(rig.cfg, rig.entry, rig.hive, [work_id], "dev/alice")
    rig.store.mark(work_id, status="closed")
    work_intake.on_work_merged(rig.cfg, rig.entry, rig.hive, "main", [work_id], "widget")

    out = capsys.readouterr()
    assert investigated not in out.out + out.err
    assert tracking in out.err and tracking in out.out  # the control heard both hooks
    untouched(rig, investigated)
    assert events(rig, tracking) == ["started", "merged"]
