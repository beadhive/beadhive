"""`hive init --claude` sandbox-grant self-checks — the money paths: the grant lands in both
sandbox + tool arrays, is self-healing on a worktrees-root move, idempotent, and never
clobbers unrelated settings. Pure dicts + tmp_path; no real Claude sandbox needed."""

from __future__ import annotations

import json
import os
from pathlib import Path

from beadhive import hive, worktree

SUFFIX = "github/o/r"


def test_merge_grants_both_arrays():
    out = hive._merge_sandbox_grant({}, "~/wts/github/o/r", SUFFIX)
    assert out["sandbox"]["filesystem"]["allowWrite"] == ["~/wts/github/o/r"]
    assert out["permissions"]["additionalDirectories"] == ["~/wts/github/o/r"]


def test_merge_is_self_healing_on_relocation():
    existing = {
        "sandbox": {"filesystem": {"allowWrite": ["~/old/github/o/r"]}},
        "permissions": {"additionalDirectories": ["~/old/github/o/r"]},
    }
    out = hive._merge_sandbox_grant(existing, "~/new/github/o/r", SUFFIX)
    # the stale root entry is gone, replaced by the current one — not accumulated
    assert out["sandbox"]["filesystem"]["allowWrite"] == ["~/new/github/o/r"]
    assert out["permissions"]["additionalDirectories"] == ["~/new/github/o/r"]


def test_merge_is_idempotent():
    once = hive._merge_sandbox_grant({}, "~/wts/github/o/r", SUFFIX)
    twice = hive._merge_sandbox_grant(once, "~/wts/github/o/r", SUFFIX)
    assert twice["sandbox"]["filesystem"]["allowWrite"] == ["~/wts/github/o/r"]


def test_merge_preserves_unrelated_and_other_hives():
    existing = {
        "permissions": {
            "allow": ["WebFetch(domain:github.com)"],
            "additionalDirectories": ["~/old/github/o/r", "~/wts/github/other/a/b"],
        },
        "sandbox": {"filesystem": {"allowWrite": ["~/wts/github/other/a/b"]}},
    }
    out = hive._merge_sandbox_grant(existing, "~/wts/github/o/r", SUFFIX)
    assert out["permissions"]["allow"] == ["WebFetch(domain:github.com)"]  # untouched
    # other hive's grant survives; this hive's stale one is replaced
    assert "~/wts/github/other/a/b" in out["permissions"]["additionalDirectories"]
    assert "~/old/github/o/r" not in out["permissions"]["additionalDirectories"]
    assert "~/wts/github/o/r" in out["permissions"]["additionalDirectories"]


def test_merge_does_not_mutate_input():
    existing = {"sandbox": {"filesystem": {"allowWrite": ["~/old/github/o/r"]}}}
    hive._merge_sandbox_grant(existing, "~/new/github/o/r", SUFFIX)
    assert existing["sandbox"]["filesystem"]["allowWrite"] == ["~/old/github/o/r"]


def test_subtree_is_home_relative_and_matches_wt_dir(monkeypatch):
    root = Path.home() / ".ws-test-wts"
    monkeypatch.setenv("WS_WORKTREES", str(root))
    sub = hive._sandbox_subtree({}, "github", "o", "r")
    assert sub.startswith("~/")
    entry = {"provider": "github", "org": "o", "repo": "r"}
    assert os.path.expanduser(sub) == str(worktree.wt_dir(entry, "leaf").parent)


def test_grant_is_current_detects_stale(tmp_path, monkeypatch):
    monkeypatch.setenv("WS_WORKTREES", str(Path.home() / ".ws-new"))
    clone = tmp_path / "clone"
    (clone / ".claude").mkdir(parents=True)
    (clone / ".claude" / "settings.local.json").write_text(
        json.dumps({"sandbox": {"filesystem": {"allowWrite": ["~/.ws-old/github/o/r"]}}})
    )
    assert hive.grant_is_current({}, clone, "github", "o", "r") is False  # root moved → stale
    # no grant present at all → None (hive opted out, not a drift warning)
    assert hive.grant_is_current({}, tmp_path / "bare", "github", "o", "r") is None


def test_install_grant_is_noop_when_ephemeral(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    hive._install_sandbox_grant({"worktrees": {"ephemeral": True}}, "github", "o", "r")
    assert not (tmp_path / ".claude" / "settings.local.json").exists()


def test_install_grant_writes_when_persistent(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WS_WORKTREES", str(tmp_path / "wts"))  # avoid disk config load
    hive._install_sandbox_grant({"worktrees": {"ephemeral": False}}, "github", "o", "r")
    data = json.loads((tmp_path / ".claude" / "settings.local.json").read_text())
    assert any(e.endswith("/github/o/r") for e in data["sandbox"]["filesystem"]["allowWrite"])
