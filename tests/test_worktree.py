"""Worktree self-checks — the money paths: naming/templating, session sortability,
declarative init-rule evaluation, and the path-prefix 'managed' filter."""

from __future__ import annotations

import datetime
import os

from ws import worktree
from ws.run import run

UTC = datetime.UTC

# git pre-commit hooks export GIT_DIR / GIT_INDEX_FILE / GIT_WORK_TREE, which would override
# `-C` and point these subprocess git calls at the outer repo — scrub them so the suite is
# hermetic whether run bare or inside a hook.
_CLEAN_ENV = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}


def _git(*args, cwd):
    run(["git", *args], cwd=str(cwd), check=True, capture=True, env=_CLEAN_ENV)


# ---- naming / templating ----------------------------------------------------


def test_branch_and_leaf_bead():
    assert worktree._branch_and_leaf({}, bead="ag-infra-7") == ("wt/bead/ag-infra-7", "ag-infra-7")


def test_branch_and_leaf_branch_is_prefixed_not_overridden():
    assert worktree._branch_and_leaf({}, branch="spike-xyz") == ("wt/spike-xyz", "spike-xyz")
    assert worktree._branch_and_leaf({}, branch="feature/login") == ("wt/feature/login", "login")


def test_branch_and_leaf_branch_does_not_double_prefix():
    assert worktree._branch_and_leaf({}, branch="wt/foo") == ("wt/foo", "foo")


def test_branch_and_leaf_session_fallback():
    now = datetime.datetime(2026, 6, 27, 14, 30, 22, tzinfo=UTC)
    br, leaf = worktree._branch_and_leaf({}, now=now, rand="abcd")
    assert br == "wt/session/20260627T143022Z-abcd"
    assert leaf == "20260627t143022z-abcd"  # last segment; sanitize lowercases the leaf


def test_session_ids_sort_chronologically():
    earlier = worktree._session_id(datetime.datetime(2026, 6, 27, 14, 30, 22, tzinfo=UTC), "ffff")
    later = worktree._session_id(datetime.datetime(2026, 6, 27, 14, 30, 23, tzinfo=UTC), "0000")
    assert earlier < later  # ts is fixed-width & leads, so lexical == chronological
    assert sorted([later, earlier]) == [earlier, later]


def test_bead_branch_template_override():
    cfg = {"worktrees": {"bead_branch": "wip/{id}"}}  # template is the suffix; wt/ still added
    assert worktree._branch_and_leaf(cfg, bead="x-1") == ("wt/wip/x-1", "x-1")


# ---- init rules -------------------------------------------------------------


def test_run_init_respects_if_exists_and_tolerates_failure(tmp_path):
    (tmp_path / "trigger.txt").write_text("")
    cfg = {
        "worktrees": {
            "init": [
                {"if_exists": "trigger.txt", "run": "touch matched.marker"},
                {"if_exists": "absent.txt", "run": "touch unmatched.marker"},
                {"run": "false"},  # always-run failure must warn, not raise
            ]
        }
    }
    worktree.run_init(cfg, {}, tmp_path)  # no exception
    assert (tmp_path / "matched.marker").exists()
    assert not (tmp_path / "unmatched.marker").exists()


def test_run_init_appends_per_rig_rules(tmp_path):
    cfg = {"worktrees": {"init": [{"run": "touch global.marker"}]}}
    entry = {"worktree_init": [{"run": "touch rig.marker"}]}
    worktree.run_init(cfg, entry, tmp_path)
    assert (tmp_path / "global.marker").exists()
    assert (tmp_path / "rig.marker").exists()


# ---- managed() path-prefix filter -------------------------------------------


def test_managed_filters_to_shadow_root(tmp_path, monkeypatch):
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("hi")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)

    wts_root = tmp_path / "wts"
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("WS_WORKTREES", str(wts_root))

    inside = wts_root / "github" / "myorg" / "myrepo" / "feat"
    inside.parent.mkdir(parents=True)
    _git("worktree", "add", "-q", "-b", "feat", str(inside), cwd=repo)
    outside = tmp_path / "hand-made"
    _git("worktree", "add", "-q", "-b", "manual", str(outside), cwd=repo)

    entry = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
    cfg = {"managed_repos": [entry]}
    rows = worktree.managed(cfg)
    paths = [p for _, p, _ in rows]

    assert any(str(inside) == p or p.endswith("/feat") for p in paths)
    assert all("hand-made" not in p for p in paths)
    assert ("mr", str(inside), "feat") in [(pre, p, br) for pre, p, br in rows] or any(
        br == "feat" for _, _, br in rows
    )


# ---- empty-dir cleanup ------------------------------------------------------


def test_rmdir_empty_parents_climbs_to_root(tmp_path, monkeypatch):
    root = tmp_path / "wts"
    monkeypatch.setenv("WS_WORKTREES", str(root))
    leaf = root / "github" / "org" / "repo" / "feat"
    leaf.mkdir(parents=True)
    leaf.rmdir()  # simulate git having removed the worktree dir

    worktree._rmdir_empty_parents(leaf, {})

    assert root.exists()  # root itself is never removed
    assert not (root / "github").exists()  # empty triplet dirs climbed away


def test_rmdir_empty_parents_stops_at_nonempty(tmp_path, monkeypatch):
    root = tmp_path / "wts"
    monkeypatch.setenv("WS_WORKTREES", str(root))
    leaf = root / "github" / "org" / "repo" / "feat"
    leaf.mkdir(parents=True)
    sibling = root / "github" / "org" / "other-repo" / "live"
    sibling.mkdir(parents=True)  # another live worktree under the same org
    leaf.rmdir()

    worktree._rmdir_empty_parents(leaf, {})

    assert not (root / "github" / "org" / "repo").exists()  # empty repo dir removed
    assert (root / "github" / "org").exists()  # non-empty org stops the climb
    assert sibling.exists()


def test_rmdir_empty_parents_disabled(tmp_path, monkeypatch):
    root = tmp_path / "wts"
    monkeypatch.setenv("WS_WORKTREES", str(root))
    leaf = root / "github" / "org" / "repo" / "feat"
    leaf.mkdir(parents=True)
    leaf.rmdir()

    worktree._rmdir_empty_parents(leaf, {"worktrees": {"rmdir_empty": False}})

    assert (root / "github" / "org" / "repo").exists()  # left intact when disabled
