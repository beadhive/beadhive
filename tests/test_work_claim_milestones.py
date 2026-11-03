"""The started milestone seam (bh-dev42): ``work claim`` / ``start`` / ``claim --group`` call
``work_intake.on_work_claimed`` after a FRESH claim.

Real git in a tmp hive plus the faked ``bd`` of ``test_work``; the milestone itself is replaced
with a recorder at ``intake_services.work_started`` (the service is proven offline in
``tests/unit/modules/intake/test_intake_progress_milestones.py``, where a later claim of the same
work drafts nothing). What is pinned here is the seam:

* off by default: an ordinary hive never reaches the milestone and makes no extra bd call, so a
  claim is byte-for-byte what it was;
* with ``intake.external.enabled``, the outbox and ``milestones.draft_on_start`` the hook runs
  once per fresh claim; a re-attach of a claim the actor already holds does not run it;
* a hook that raises or refuses NEVER fails the claim: the bead is claimed, the worktree is
  provisioned, and the problem is a warning on stderr.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from beadhive import config_store, intake_services, work, work_lifecycle
from beadhive.config_consumer_ports import work_settings
from beadhive.modules.intake.domain import MilestoneOutcome
from test_work import CONFIG_YAML, _batch_wt, _mol_branch, _wt, fakebd, hive

__all__ = ["fakebd", "hive"]  # the shared fixtures, re-exported for pytest

ON = (
    "intake:\n  external:\n    enabled: true\n    outbox: {enabled: true}\n"
    "    milestones: {draft_on_start: true}\n"
)


def _enable(hive, section: str = ON) -> None:
    hive.cfg_path.write_text(CONFIG_YAML + section)
    config_store.clear_load_cache()


class Started:
    """A recorder in place of ``intake_services.work_started``."""

    def __init__(self, result=None, fail: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.result = result or intake_services.MilestoneResult(0, outcome=MilestoneOutcome(()))
        self.fail = fail

    def __call__(self, cfg, entry, directory, work_id, actor, **kwargs):
        self.calls.append({"work": work_id, "actor": actor, "directory": str(directory)})
        if self.fail is not None:
            raise self.fail
        return self.result


@pytest.fixture
def started(monkeypatch):
    recorder = Started()
    monkeypatch.setattr(intake_services, "work_started", recorder)
    return recorder


# -- off by default ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "section",
    [
        "",
        "intake:\n  external:\n    enabled: true\n",
        "intake:\n  external:\n    enabled: true\n    outbox: {enabled: true}\n",
    ],
    ids=["absent", "intake-only", "no-draft-on-start"],
)
def test_with_the_milestone_off_a_claim_never_reaches_it(hive, fakebd, started, section) -> None:
    _enable(hive, section)
    fakebd.seed("mr-1", title="t")

    work.claim(bead="mr-1", as_="dev/alice", hive="myrepo")

    assert started.calls == []
    assert fakebd.beads["mr-1"]["status"] == "in_progress"
    assert not [args for _actor, args in fakebd.calls if "tracks" in args]


# -- on: the seam ------------------------------------------------------------------------------


def test_a_fresh_claim_runs_the_hook_once_and_a_reattach_does_not(hive, fakebd, started) -> None:
    _enable(hive)
    fakebd.seed("mr-1", title="t")

    work.claim(bead="mr-1", as_="dev/alice", hive="myrepo")
    work.claim(bead="mr-1", as_="dev/alice", hive="myrepo")  # the same actor re-attaches

    assert started.calls == [{"work": "mr-1", "actor": "dev/alice", "directory": str(hive.main)}]


def test_starting_an_epic_runs_the_hook_for_the_epic(hive, fakebd, started) -> None:
    _enable(hive)
    fakebd.seed("mr-epic", title="e", issue_type="epic")
    fakebd.states["mr-epic"] = {"kickoff": "approved"}

    work.start(epic="mr-epic", as_="disp/lead", hive="myrepo")

    assert [(c["work"], c["actor"]) for c in started.calls] == [("mr-epic", "disp/lead")]


def test_a_group_claim_runs_the_hook_for_every_member(hive, fakebd, started) -> None:
    _enable(hive)
    _mol_branch(hive, "mr-1")
    fakebd.seed("mr-1.1", title="a", parent="mr-1", labels=["batch:g"])
    fakebd.seed("mr-1.2", title="b", parent="mr-1", labels=["batch:g"])

    work.claim(bead="", as_="dev/alice", group="mr-1.1,mr-1.2", hive="myrepo")

    assert _batch_wt(hive, "g").exists()
    assert [c["work"] for c in started.calls] == ["mr-1.1", "mr-1.2"]


# -- a hook failure never fails the claim ------------------------------------------------------


@pytest.mark.parametrize(
    "recorder",
    [
        Started(fail=RuntimeError("boom")),
        Started(result=intake_services.MilestoneResult(1, "not primary", "not_primary")),
    ],
    ids=["raises", "refused"],
)
def test_a_milestone_failure_never_fails_the_claim(
    hive, fakebd, monkeypatch, capsys, recorder
) -> None:
    _enable(hive)
    monkeypatch.setattr(intake_services, "work_started", recorder)
    fakebd.seed("mr-1", title="t")
    capsys.readouterr()

    work.claim(bead="mr-1", as_="dev/alice", hive="myrepo")  # must not raise

    out = capsys.readouterr()
    assert fakebd.beads["mr-1"]["status"] == "in_progress" and _wt(hive, "mr-1").exists()
    assert "✓ claimed mr-1" in out.out and "the claim stands" in out.err
    assert len(recorder.calls) == 1


def test_bh_work_next_runs_the_hook_after_provisioning_its_won_claim(
    hive, monkeypatch, started
) -> None:
    """``bh work next`` wins the claim itself (atomic route or the CLI loop) and then provisions
    it through ``work_lifecycle.provision_claim``: every call there is a fresh win."""
    _enable(hive)
    checkout = SimpleNamespace(target=hive.main / "wt", handle=None)

    @contextlib.contextmanager
    def commands(cfg, hive_name, main, entry):
        yield SimpleNamespace(provision_claim=lambda bead, actor: checkout)

    monkeypatch.setattr(work_lifecycle, "commands", commands)
    cfg = work_settings.load()

    target, _identity = work_lifecycle.provision_claim(cfg, "myrepo", hive.main, "mr-7", "dev/bob")

    assert target == str(checkout.target)
    assert [(c["work"], c["actor"]) for c in started.calls] == [("mr-7", "dev/bob")]
