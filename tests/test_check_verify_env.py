"""`bh work check` establishes its environment FROM THE TREE before validating (bh-ku9n9.14).

`check` seeds the verdict ledger `submit` reuses from (bh-i0p1.4), but a seat worktree's
environment was provisioned whenever that seat was created — nothing re-derived it from the tree
at check time. Two runs over the identical tree could therefore validate different environments
and land in the ledger under the SAME key with the same rc, indistinguishable. `clean_checkout`
never had this problem because it runs the hive's `verify: true` init rules in its verify dir
first; `check` now does the same, in the seat worktree.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from beadhive import config, work, worktree


def _ok(*_a, **_k):
    return subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")


@pytest.fixture
def checkable(tmp_path, monkeypatch):
    """`work.check` reduced to its two subprocess-spawning seams — init rules and the validation
    command — both recording into one shared list so their ORDER is observable."""
    target = tmp_path / "wt"
    target.mkdir()
    entry = {"prefix": "mr"}
    calls: list = []

    monkeypatch.setattr(worktree, "locate", lambda *_a, **_k: (entry, tmp_path, target, "b"))
    monkeypatch.setattr(work, "_batch_worktree", lambda *_a, **_k: ("", None))
    monkeypatch.setattr(worktree, "in_bead_worktree", lambda _p: True)
    monkeypatch.setattr(config, "validate_cmd", lambda *_a, **_k: "validate-me")
    monkeypatch.setattr(work, "_record_check_verdict", lambda *_a, **_k: None)

    def fake_run(cmd, **_kw):
        calls.append(list(cmd))
        return _ok()

    monkeypatch.setattr(work, "run", fake_run)
    monkeypatch.setattr(worktree, "run", fake_run)  # run_init's own spawn seam
    return {"target": target, "calls": calls, "monkeypatch": monkeypatch}


def _set_rules(mp, rules):
    mp.setattr(config, "load", lambda: {"worktrees": {"init": rules}})


def test_check_runs_verify_flagged_rules_before_validating(checkable):
    """The flagged rules run, in the operator's declared order, in the SEAT worktree, and all of
    them before the validation command — so the verdict is a property of the tree."""
    _set_rules(
        checkable["monkeypatch"],
        [
            {"run": "trust-the-tree", "verify": True},
            {"run": "sync-the-deps", "verify": True},
            {"run": "heavy-seat-setup"},  # unflagged: seat provisioning, never per-validation
        ],
    )
    work.check(bead="mr-1", hive="myrepo")
    assert checkable["calls"] == [["trust-the-tree"], ["sync-the-deps"], ["validate-me"]]


def test_check_with_no_verify_flagged_rules_is_unchanged(checkable):
    """Degradation (bh-ku9n9.14 criterion 4): a hive that flags nothing spawns nothing extra —
    exactly today's behaviour, one validation command and no environment establishment."""
    _set_rules(checkable["monkeypatch"], [{"run": "heavy-seat-setup"}])
    work.check(bead="mr-1", hive="myrepo")
    assert checkable["calls"] == [["validate-me"]]


def test_check_verify_rules_honour_if_exists_in_the_worktree(checkable):
    """The rules stay opaque `{run, if_exists?, verify?}` entries — bh matches the glob against
    the tree and interprets nothing about what any of them mean."""
    (Path(checkable["target"]) / "present.toml").write_text("x\n")
    _set_rules(
        checkable["monkeypatch"],
        [
            {"run": "for-present", "if_exists": "present.toml", "verify": True},
            {"run": "for-absent", "if_exists": "absent.toml", "verify": True},
        ],
    )
    work.check(bead="mr-1", hive="myrepo")
    assert checkable["calls"] == [["for-present"], ["validate-me"]]
