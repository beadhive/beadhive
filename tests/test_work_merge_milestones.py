"""The merge milestone seam (I10): ``work.merge`` / ``work.finish`` call ``on_work_merged``.

Real git in a tmp hive plus the faked ``bd`` of ``test_work``; the milestone itself is replaced
with a recorder at ``intake_services.work_merged`` (the service is proven offline in
``tests/unit/modules/intake/test_intake_milestones.py``). What is pinned here is the seam:

* off by default: an ordinary hive never reaches the milestone, reads no tip and makes no extra
  call, so a merge is byte-for-byte what it was;
* with ``intake.external.enabled`` the hook runs once the bead is closed on the integration
  branch, with the merge commit that landed it; a land into a container does not trigger it (the
  molecule's own land does, for the epic and its children);
* a hook that raises, refuses or reports a failed report NEVER fails the merge: the bead is closed,
  the merge exits normally, and the problem is a warning on stderr.
"""

from __future__ import annotations

import pytest

from beadhive import config_store, host, intake_services, work, work_intake, worktree
from beadhive.config_consumer_ports import work_settings as work_config
from beadhive.modules.intake.domain import MilestoneItem, MilestoneOutcome
from test_work import (
    CONFIG_YAML,
    _commit,
    _git,
    _land_two_bead_molecule,
    _mol_branch,
    _submit_and_approve_batch,
    _take_to_approved,
    _wt_of,
    fakebd,
    hive,
)

__all__ = ["fakebd", "hive"]  # the shared fixtures, re-exported for pytest

ON = "intake:\n  external:\n    enabled: true\n"


def _enable_intake(hive) -> None:
    hive.cfg_path.write_text(CONFIG_YAML + ON)
    config_store.clear_load_cache()


@pytest.fixture(autouse=True)
def _minted_host_identity():
    """Merge-slot holder tokens key on ``host.host_id()``: mint it as ``bh config init`` does."""
    host.mint_if_needed()


class Milestones:
    """A recorder in place of ``intake_services.work_merged``."""

    def __init__(self, result=None, fail: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result or intake_services.MilestoneResult(0, outcome=MilestoneOutcome(()))
        self.fail = fail

    def __call__(self, cfg, entry, directory, works, commit, actor, **kwargs):
        self.calls.append(
            {"works": list(works), "commit": commit, "directory": str(directory), "actor": actor}
        )
        if self.fail is not None:
            raise self.fail
        return self.result


@pytest.fixture
def milestones(monkeypatch):
    recorder = Milestones()
    monkeypatch.setattr(intake_services, "work_merged", recorder)
    return recorder


def _tip(hive, ref="main") -> str:
    return _git("rev-parse", ref, cwd=hive.main).stdout.strip()


# -- off by default ----------------------------------------------------------------------------


def test_with_intake_off_a_merge_never_reaches_the_milestone_or_looks_up_trackers(
    hive, fakebd, milestones
):
    fakebd.seed("mr-10", title="t")
    _take_to_approved(hive, fakebd, "mr-10")
    before = len(fakebd.calls)

    work.merge(bead="mr-10", hive="myrepo", rm=False, molecule=False)

    assert milestones.calls == []
    assert fakebd.beads["mr-10"]["status"] == "closed"
    tracking = [args for _actor, args in fakebd.calls[before:] if "tracks" in args]
    assert tracking == []  # no dependency lookup was made on the milestone's behalf


# -- on: the seam ------------------------------------------------------------------------------


def test_a_merge_onto_the_integration_branch_triggers_the_milestone_with_the_merge_commit(
    hive, fakebd, milestones
):
    _enable_intake(hive)
    fakebd.seed("mr-11", title="t")
    _take_to_approved(hive, fakebd, "mr-11")

    work.merge(bead="mr-11", hive="myrepo", rm=False, molecule=False)

    assert fakebd.beads["mr-11"]["status"] == "closed"
    (call,) = milestones.calls
    assert call["works"] == ["mr-11"] and call["commit"] == _tip(hive)
    assert call["directory"] == str(hive.main)
    merge_subject = _git("log", "-1", "--format=%s", cwd=hive.main).stdout.strip()
    assert merge_subject == "chore(merge): bead mr-11"  # the commit named is the merge bubble


def test_a_land_into_a_container_is_not_on_main_yet_and_triggers_nothing(hive, fakebd, milestones):
    _enable_intake(hive)
    _mol_branch(hive, "mr-1")
    fakebd.seed("mr-1.1", title="t")
    work.claim(bead="mr-1.1", as_="", hive="myrepo")
    _commit(_wt_of(hive, "mr-1.1"), "feat: the change")
    work.submit(bead="mr-1.1", hive="myrepo")
    fakebd.approve("mr-1.1")

    work.merge(bead="mr-1.1", hive="myrepo", rm=False, molecule=False)

    assert fakebd.beads["mr-1.1"]["status"] == "closed" and milestones.calls == []


def test_the_molecule_land_triggers_the_epic_and_its_children_with_one_commit(
    hive, fakebd, milestones
):
    _enable_intake(hive)
    _land_two_bead_molecule(hive, fakebd, "mr-1")
    fakebd.beads["mr-1"]["issue_type"] = "epic"
    assert milestones.calls == []  # the children landed into the container, not main

    work.finish(epic="mr-1", hive="myrepo")

    assert fakebd.beads["mr-1"]["status"] == "closed"
    (call,) = milestones.calls
    assert call["works"][0] == "mr-1" and {"mr-1.1", "mr-1.2"} <= set(call["works"])
    assert call["commit"] == _tip(hive)


def test_merge_molecule_is_the_same_trigger_as_finish(hive, fakebd, milestones):
    _enable_intake(hive)
    _land_two_bead_molecule(hive, fakebd, "mr-1")

    work.merge(bead="mr-1", hive="myrepo", molecule=True)

    (call,) = milestones.calls
    assert call["works"][0] == "mr-1" and call["commit"] == _tip(hive)


def test_a_batch_merge_into_a_container_triggers_nothing(hive, fakebd, milestones):
    _enable_intake(hive)
    _submit_and_approve_batch(hive, fakebd)

    work.merge(bead="", group="mr-1.1,mr-1.2", hive="myrepo")

    assert fakebd.beads["mr-1.1"]["status"] == "closed" and milestones.calls == []


def test_a_batch_merge_onto_the_integration_branch_triggers_every_closed_member(
    hive, fakebd, milestones
):
    # the container stands in for the canonical branch: the batch's base IS the integration branch
    hive.cfg_path.write_text(
        CONFIG_YAML.replace("work:\n", "work:\n  integration_branch: wt/bead/epic/mr-1\n") + ON
    )
    config_store.clear_load_cache()
    _submit_and_approve_batch(hive, fakebd)

    work.merge(bead="", group="mr-1.1,mr-1.2", hive="myrepo")

    (call,) = milestones.calls
    assert call["works"] == ["mr-1.1", "mr-1.2"]
    assert call["commit"] == _tip(hive, "wt/bead/epic/mr-1")


def test_re_running_an_already_landed_merge_triggers_again_idempotently(hive, fakebd, milestones):
    _enable_intake(hive)
    fakebd.seed("mr-12", title="t")
    _take_to_approved(hive, fakebd, "mr-12")
    work.merge(bead="mr-12", hive="myrepo", rm=False, molecule=False)
    fakebd.beads["mr-12"]["status"] = "in_progress"  # the bookkeeping half did not run

    work.merge(bead="mr-12", hive="myrepo", rm=False, molecule=False)  # the reconcile

    assert fakebd.beads["mr-12"]["status"] == "closed"
    assert [c["works"] for c in milestones.calls] == [["mr-12"], ["mr-12"]]
    assert milestones.calls[0]["commit"] == milestones.calls[1]["commit"]


# -- a hook failure never fails the merge ------------------------------------------------------


@pytest.mark.parametrize(
    "result",
    [
        Milestones(fail=RuntimeError("boom")),
        Milestones(fail=ValueError("a store said no")),
        Milestones(result=intake_services.MilestoneResult(1, "not primary", "not_primary")),
        Milestones(
            result=intake_services.MilestoneResult(
                0,
                outcome=MilestoneOutcome(
                    (MilestoneItem("mr-99", "merged", delivery="merged", error="not closed: x"),)
                ),
            )
        ),
    ],
    ids=["raises", "raises-other", "refused", "report-failed"],
)
def test_a_milestone_failure_never_fails_the_merge(hive, fakebd, monkeypatch, capsys, result):
    _enable_intake(hive)
    monkeypatch.setattr(intake_services, "work_merged", result)
    fakebd.seed("mr-13", title="t")
    _take_to_approved(hive, fakebd, "mr-13")
    capsys.readouterr()

    work.merge(bead="mr-13", hive="myrepo", rm=False, molecule=False)  # must not raise

    out = capsys.readouterr()
    assert fakebd.beads["mr-13"]["status"] == "closed"
    assert "merged mr-13" in out.out and "and closed it" in out.out
    assert len(result.calls) == 1
    if result.fail is not None or result.result.code:
        assert "the merge is unaffected" in out.err
    else:
        assert "mr-99" in out.err and "FAILED" in out.err


def test_a_milestone_that_cannot_read_the_tip_is_skipped_with_a_warning(
    hive, milestones, monkeypatch, capsys
):
    _enable_intake(hive)
    monkeypatch.setattr(worktree, "_ref_sha", lambda *args, **kwargs: "")
    cfg = work_config.load()
    entry = cfg["managed_repos"][0]

    work_intake.on_work_merged(cfg, entry, hive.main, "main", ["mr-14"], "myrepo")

    assert milestones.calls == []
    assert "could not read the tip of main" in capsys.readouterr().err


def test_with_intake_off_the_hook_returns_before_any_git_or_bd_work(hive, milestones, monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("the milestone hook did work with intake off")

    monkeypatch.setattr(worktree, "_ref_sha", refuse)
    cfg = work_config.load()

    work_intake.on_work_merged(cfg, cfg["managed_repos"][0], hive.main, "main", ["mr-1"], "myrepo")

    assert milestones.calls == []


def test_a_molecule_whose_children_cannot_be_listed_still_triggers_the_epic(
    hive, milestones, capsys
):
    _enable_intake(hive)
    cfg = work_config.load()

    def broken():
        raise RuntimeError("bd children failed")

    work_intake.on_work_merged(
        cfg, cfg["managed_repos"][0], hive.main, "main", ["mr-1"], "myrepo", broken
    )

    (call,) = milestones.calls
    assert call["works"] == ["mr-1"]
    assert "could not list the molecule's children" in capsys.readouterr().err
