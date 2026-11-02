"""The post-merge continuous-backfill seam: ``work.merge`` / ``finish`` call
``backfill_continuous.on_work_merged`` once a bead is closed on the integration branch.

Real git in a tmp hive plus the faked ``bd`` of ``test_work``; the incremental run itself is
replaced with a recorder (it is proven against a fake bd in ``tests/test_backfill_continuous.py``).
What is pinned here is the seam:

* off by default: an ordinary hive never reaches the hook, imports nothing and makes no extra
  call, so a merge is byte-for-byte what it was;
* with ``backfill.continuous.enabled`` the hook runs once the bead is closed on the integration
  branch, told the merged beads and the integration tip from before the land; a land into a
  container does not trigger it;
* a hook that raises, or a loader that fails inside it, NEVER fails the merge: the bead is closed,
  the merge exits normally, and the problem is a warning on stderr.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from beadhive import backfill_cli, backfill_continuous, config_store, host, work
from beadhive.backfill_bd import BdLoadError
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

ON = "backfill:\n  continuous:\n    enabled: true\n    passes: [timeline]\n"


def _enable(hive) -> None:
    hive.cfg_path.write_text(CONFIG_YAML + ON)
    config_store.clear_load_cache()


@pytest.fixture(autouse=True)
def _minted_host_identity():
    """Merge-slot holder tokens key on ``host.host_id()``: mint it as ``bh config init`` does."""
    host.mint_if_needed()


class Hook:
    """A recorder in place of ``backfill_continuous.on_work_merged``."""

    def __init__(self, fail: Exception | None = None) -> None:
        self.calls: list[dict] = []
        self.fail = fail

    def __call__(self, cfg, entry, main, base, beads, *, seams, home, since=None):
        self.calls.append(
            {
                "beads": list(beads),
                "base": base,
                "since": since,
                "main": str(main),
                "home": home,
                "seams": seams,
            }
        )
        if self.fail is not None:
            raise self.fail


@pytest.fixture
def hook(monkeypatch):
    recorder = Hook()
    monkeypatch.setattr(backfill_continuous, "on_work_merged", recorder)
    return recorder


def _tip(hive, ref="main") -> str:
    return _git("rev-parse", ref, cwd=hive.main).stdout.strip()


def test_with_it_off_a_merge_never_reaches_the_hook_or_imports_it(hive, fakebd, hook, monkeypatch):
    fakebd.seed("mr-10", title="t")
    _take_to_approved(hive, fakebd, "mr-10")
    monkeypatch.setattr(
        backfill_cli, "store_loader", lambda *a, **k: pytest.fail("the store was touched")
    )

    work.merge(bead="mr-10", hive="myrepo", rm=False, molecule=False)

    assert hook.calls == []
    assert fakebd.beads["mr-10"]["status"] == "closed"


def test_enabled_a_bead_merge_calls_the_hook_with_the_beads_and_the_prior_tip(hive, fakebd, hook):
    _enable(hive)
    fakebd.seed("mr-11", title="t")
    _take_to_approved(hive, fakebd, "mr-11")
    before = _tip(hive)

    work.merge(bead="mr-11", hive="myrepo", rm=False, molecule=False)

    assert fakebd.beads["mr-11"]["status"] == "closed"
    (call,) = hook.calls
    assert call["beads"] == ["mr-11"] and call["base"] == "main"
    assert call["since"] == before != _tip(hive)  # the tip before this merge's bubble
    assert call["main"] == str(hive.main)
    assert Path(call["home"]).is_absolute()
    assert call["seams"] is backfill_cli  # the composition root hands over the CLI seams


def test_a_land_into_a_container_is_not_on_main_yet_and_triggers_nothing(hive, fakebd, hook):
    _enable(hive)
    _mol_branch(hive, "mr-1")
    fakebd.seed("mr-1.1", title="t")
    work.claim(bead="mr-1.1", as_="", hive="myrepo")
    _commit(_wt_of(hive, "mr-1.1"), "feat: the change")
    work.submit(bead="mr-1.1", hive="myrepo")
    fakebd.approve("mr-1.1")

    work.merge(bead="mr-1.1", hive="myrepo", rm=False, molecule=False)

    assert fakebd.beads["mr-1.1"]["status"] == "closed" and hook.calls == []


def test_the_molecule_land_triggers_the_epic(hive, fakebd, hook):
    _enable(hive)
    _land_two_bead_molecule(hive, fakebd, "mr-1")
    fakebd.beads["mr-1"]["issue_type"] = "epic"
    assert hook.calls == []  # the children landed into the container, not main
    before = _tip(hive)

    work.finish(epic="mr-1", hive="myrepo")

    assert fakebd.beads["mr-1"]["status"] == "closed"
    (call,) = hook.calls
    assert call["beads"] == ["mr-1"] and call["since"] == before


def test_a_batch_merge_onto_the_integration_branch_triggers_every_closed_member(hive, fakebd, hook):
    # the container stands in for the canonical branch: the batch's base IS the integration branch
    hive.cfg_path.write_text(
        CONFIG_YAML.replace("work:\n", "work:\n  integration_branch: wt/bead/epic/mr-1\n") + ON
    )
    config_store.clear_load_cache()
    _submit_and_approve_batch(hive, fakebd)
    before = _tip(hive, "wt/bead/epic/mr-1")

    work.merge(bead="", group="mr-1.1,mr-1.2", hive="myrepo")

    (call,) = hook.calls
    assert call["beads"] == ["mr-1.1", "mr-1.2"] and call["since"] == before


def test_a_hook_that_raises_never_fails_the_merge(hive, fakebd, monkeypatch, capsys):
    _enable(hive)
    recorder = Hook(fail=RuntimeError("boom"))
    monkeypatch.setattr(backfill_continuous, "on_work_merged", recorder)
    fakebd.seed("mr-12", title="t")
    _take_to_approved(hive, fakebd, "mr-12")
    capsys.readouterr()

    work.merge(bead="mr-12", hive="myrepo", rm=False, molecule=False)  # must not raise

    out = capsys.readouterr()
    assert fakebd.beads["mr-12"]["status"] == "closed"
    assert "merged mr-12" in out.out and "and closed it" in out.out
    assert "continuous backfill did not run (the merge is unaffected): boom" in out.err
    assert len(recorder.calls) == 1


def test_a_failing_loader_is_a_warning_and_the_merge_still_succeeds(
    hive, fakebd, monkeypatch, capsys
):
    """The real hook runs; only the bd StoreLoader it asks for fails."""
    _enable(hive)

    def broken(cwd, *, group, pre_state_path=None):
        raise BdLoadError("bd export failed; nothing was written")

    monkeypatch.setattr(backfill_cli, "store_loader", broken)
    fakebd.seed("mr-13", title="t")
    _take_to_approved(hive, fakebd, "mr-13")
    capsys.readouterr()

    work.merge(bead="mr-13", hive="myrepo", rm=False, molecule=False)  # must not raise

    out = capsys.readouterr()
    assert fakebd.beads["mr-13"]["status"] == "closed"
    assert "and closed it" in out.out
    assert "backfill timeline not updated: bd export failed" in out.err


def test_re_running_an_already_landed_merge_is_unchanged(hive, fakebd, hook):
    _enable(hive)
    fakebd.seed("mr-14", title="t")
    _take_to_approved(hive, fakebd, "mr-14")
    work.merge(bead="mr-14", hive="myrepo", rm=False, molecule=False)
    fakebd.beads["mr-14"]["status"] = "in_progress"  # the bookkeeping half did not run

    work.merge(bead="mr-14", hive="myrepo", rm=False, molecule=False)  # the reconcile

    assert fakebd.beads["mr-14"]["status"] == "closed"
    assert len(hook.calls) == 1  # the reconcile of a landed branch is not a new land
