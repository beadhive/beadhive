"""Integration self-checks for `ws work show` / `ws work refine` on real git.

Builds a noisy bead branch via the AGF harness and drives the real verbs. show/refine touch
no bd, but the rig builder embeds bd, so we keep the integration marker + skip-if-no-bd.
"""

from __future__ import annotations

import json

import pytest
import typer

from beadhive import config, work, worktree
from harness.beads import skip_if_no_bd
from harness.hive import make_hive
from harness.noisy import author_date, branches, commit, make_noisy_branch, provision

pytestmark = [pytest.mark.integration, skip_if_no_bd]

_BEAD = "mr-noisy"


def _locate(hive):
    cfg = config.load()
    entry, _main, target, branch = worktree.locate(cfg, hive.repo, _BEAD)
    return entry, target, branch


def _backup_of(hive, branch):
    found = [b for b in branches(hive.main) if b.startswith(f"{branch}.refine-")]
    assert found, "refine should leave a backup branch"
    return found[0]


# ---- show -------------------------------------------------------------------


def test_show_json_reports_count_and_flags(world, capsys):
    hive = make_hive(world)
    make_noisy_branch(hive)
    capsys.readouterr()
    work.show(bead=_BEAD, view=["log"], json_out=True, hive=hive.repo)
    payload = json.loads(capsys.readouterr().out)

    assert len(payload["commits"]) == 4
    for c in payload["commits"]:  # every row carries the full flag triple
        assert set(c["flags"]) == {"marker", "fixup", "run"}
    marker = next(c for c in payload["commits"] if c["flags"]["marker"])
    assert marker["subject"].startswith("fixup! ")  # the --fixup commit
    wip = next(c for c in payload["commits"] if c["subject"] == "wip checkpoint")
    assert wip["flags"]["fixup"]  # file-subset of the helper commit → fold suggestion


def test_show_sig_view_runs(world, capsys):
    hive = make_hive(world)
    make_noisy_branch(hive)
    capsys.readouterr()
    work.show(bead=_BEAD, view=["sig"], json_out=False, hive=hive.repo)
    out = capsys.readouterr().out
    assert "feat: core feature" in out
    assert "✔" in out  # harness commits are signed by the human key


# ---- refine --plan ----------------------------------------------------------


def test_refine_plan_byte_identical_and_retains_dates(world):
    hive = make_hive(world)
    n = make_noisy_branch(hive)
    entry, target, branch = _locate(hive)
    core_date = author_date(target, n.shas["core"])
    helper_date = author_date(target, n.shas["helper"])

    plan = {
        "groups": [
            {"keep": n.shas["core"], "fold": [n.shas["fix1"]]},
            {"keep": n.shas["helper"], "fold": [n.shas["wip"]]},
        ]
    }
    plan_file = world.tmp / "plan.json"
    plan_file.write_text(json.dumps(plan))
    work.refine(bead=_BEAD, plan=str(plan_file), autosquash=False, since="", dry_run=False,
                hive=hive.repo)

    backup = _backup_of(hive, branch)
    assert worktree.same_tree(entry, backup, branch)  # the refine safety gate

    rows = worktree.commit_rows(entry, n.base, branch)
    assert len(rows) == 2  # two digests, the four checkpoints squashed away
    # each digest keeps its keep's author date — the spread is NOT collapsed to "now"
    assert rows[0]["date"] == core_date
    assert rows[1]["date"] == helper_date
    assert len({r["date"] for r in rows}) == 2


def test_refine_dry_run_changes_nothing(world, capsys):
    hive = make_hive(world)
    n = make_noisy_branch(hive)
    entry, target, branch = _locate(hive)
    tip_before = worktree.head_sha(target)

    plan = {"groups": [{"keep": n.shas["core"], "fold": [n.shas["fix1"]]}]}
    plan_file = world.tmp / "plan.json"
    plan_file.write_text(json.dumps(plan))
    capsys.readouterr()
    work.refine(bead=_BEAD, plan=str(plan_file), autosquash=False, since="", dry_run=True,
                hive=hive.repo)
    out = capsys.readouterr().out

    assert "would produce" in out
    assert worktree.head_sha(target) == tip_before  # no rewrite
    assert not any(b.startswith(f"{branch}.refine-") for b in branches(hive.main))  # no backup


# ---- refine --autosquash ----------------------------------------------------


def test_refine_autosquash_folds_marker_and_keeps_date(world):
    hive = make_hive(world)
    n = make_noisy_branch(hive)
    entry, target, branch = _locate(hive)
    core_date = author_date(target, n.shas["core"])

    work.refine(bead=_BEAD, plan="", autosquash=True, since="", dry_run=False, hive=hive.repo)

    backup = _backup_of(hive, branch)
    assert worktree.same_tree(entry, backup, branch)
    rows = worktree.commit_rows(entry, n.base, branch)
    assert not any(r["subject"].startswith("fixup! ") for r in rows)  # marker folded in
    assert len(rows) == 3  # core(+fixup), helper, wip
    core = next(r for r in rows if r["subject"] == "feat: core feature")
    assert core["date"] == core_date  # fixup keeps the target's author date


# ---- refine conflict path (non-contiguous reorder) --------------------------


def test_refine_conflict_aborts_and_restores(world):
    hive = make_hive(world)
    entry, target, branch, base = provision(hive, _BEAD)
    # Stacked edits to the same file; folding #3 into #1 (with #2 between) reorders into a
    # context that no longer exists → conflict.
    c1 = commit(target, "cf.py", "a\n", "feat: c")
    commit(target, "cf.py", "a\nb\n", "feat: d")
    c3 = commit(target, "cf.py", "a\nb\nc\n", "wip: e")
    tip_before = worktree.head_sha(target)

    plan = {"groups": [{"keep": c1, "fold": [c3]}]}
    plan_file = world.tmp / "plan.json"
    plan_file.write_text(json.dumps(plan))

    with pytest.raises(typer.Exit):
        work.refine(bead=_BEAD, plan=str(plan_file), autosquash=False, since="", dry_run=False,
                    hive=hive.repo)

    assert worktree.head_sha(target) == tip_before  # restored from backup
    assert worktree.is_clean(target)  # the rebase was aborted, not left in progress
    assert worktree.current_branch(target) == branch
