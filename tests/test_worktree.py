"""Worktree self-checks — the money paths: naming/templating, session sortability,
declarative init-rule evaluation, and the path-prefix 'managed' filter."""

from __future__ import annotations

import datetime
import os
import sys

import pytest
import typer

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
    # Default kind is the leaf 'issue'; <type> lives in the branch, the dir leaf stays <id>.
    assert worktree._branch_and_leaf({}, bead="ag-infra-7") == (
        "wt/bead/issue/ag-infra-7",
        "ag-infra-7",
    )


def test_branch_and_leaf_bead_epic_kind():
    # An explicit epic kind opens the container namespace; leaf stays <id> (dir name unchanged).
    assert worktree._branch_and_leaf({}, bead="ag-epic", kind="epic") == (
        "wt/bead/epic/ag-epic",
        "ag-epic",
    )


def test_branch_and_leaf_branch_is_prefixed_not_overridden():
    assert worktree._branch_and_leaf({}, branch="spike-xyz") == ("wt/spike-xyz", "spike-xyz")
    assert worktree._branch_and_leaf({}, branch="feature/login") == ("wt/feature/login", "login")


def test_branch_and_leaf_branch_does_not_double_prefix():
    assert worktree._branch_and_leaf({}, branch="wt/foo") == ("wt/foo", "foo")


def test_branch_and_leaf_batch_mode():
    # a work-group rides the `wt/<name>` mode as batch/<group> → wt/batch/<group>, but its worktree
    # dir leaf carries a `batch-` prefix so it can't collide with a bead worktree of the same name
    # (notably the epic seat wt/bead/epic/<epic> in collapsed mode —)
    assert worktree._branch_and_leaf({}, branch="batch/samefile") == (
        "wt/batch/samefile",
        "batch-samefile",
    )


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


# ---- integration_base climb -------------------------------------------------


def _mol_rig(tmp_path, monkeypatch):
    """A real one-commit rig clone under GIT_WORKSPACE; returns its managed_repos entry."""
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("hi")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    return {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}, repo


def test_integration_base_one_hop_epic_present(tmp_path, monkeypatch):
    """1-hop: a child's nearest container is its parent epic's wt/bead/epic/<epic> branch."""
    entry, repo = _mol_rig(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/ag-epic", cwd=repo)  # epic kicked off → container present
    assert worktree.integration_base(entry, "ag-epic.3", "main") == "wt/bead/epic/ag-epic"


def test_integration_base_two_hop_workstream_present(tmp_path, monkeypatch):
    """2-hop: nearest-first — a grandchild lands on its epic when that container exists, even
    though a workstream container exists one tier above."""
    entry, repo = _mol_rig(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/ws", cwd=repo)  # workstream container (grandparent)
    _git("branch", "wt/bead/epic/ws.2", cwd=repo)  # epic container (parent) — nearest wins
    assert worktree.integration_base(entry, "ws.2.5", "main") == "wt/bead/epic/ws.2"


def test_integration_base_two_hop_climbs_to_workstream(tmp_path, monkeypatch):
    """2-hop climb: with only the workstream container present, an epic <ws>.<n> lands on the
    workstream — its own epic container isn't opened (it IS the container being resolved for)."""
    entry, repo = _mol_rig(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/ws", cwd=repo)  # workstream container only
    assert worktree.integration_base(entry, "ws.2", "main") == "wt/bead/epic/ws"


def test_integration_base_zero_hop_no_container(tmp_path, monkeypatch):
    """0-hop: no container branch anywhere in the chain → the rig integration branch (main)."""
    entry, _ = _mol_rig(tmp_path, monkeypatch)  # no container branches
    assert worktree.integration_base(entry, "ag-epic.3", "main") == "main"


def test_integration_base_no_dot_is_root(tmp_path, monkeypatch):
    """A dotless (top-level) id has no parent to climb to → integration (main)."""
    entry, repo = _mol_rig(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/ag-epic", cwd=repo)  # present, but the id itself is the root
    assert worktree.integration_base(entry, "ag-epic", "main") == "main"


def test_integration_base_skips_issue_type_ancestor(tmp_path, monkeypatch):
    """A sub-bead of an ISSUE (xn3o.5.1) finds no container at its parent (that ref lives under
    issue/, not a CONTAINER_TYPE), so the climb walks past it to the epic — fixing the latent
    single-hop bug that would have targeted integration directly."""
    entry, repo = _mol_rig(tmp_path, monkeypatch)
    _git("branch", "wt/bead/issue/xn3o.5", cwd=repo)  # parent is a leaf issue, not a container
    _git("branch", "wt/bead/epic/xn3o", cwd=repo)  # grandparent epic container
    assert worktree.integration_base(entry, "xn3o.5.1", "main") == "wt/bead/epic/xn3o"


def test_ensure_integration_branch_nested_epic_forks_off_workstream(tmp_path, monkeypatch):
    """A nested epic <ws>.<epic> seat (ensure kind='epic', the retired ensure_integration_branch)
    opens its container off the workstream container (integration_base one tier up), not off main
    — so it sees the workstream's assembled work."""
    cfg, entry, repo = _ensure_rig(tmp_path, monkeypatch)
    # workstream container carries a commit main does not have
    _git("checkout", "-q", "-b", "wt/bead/epic/ws", cwd=repo)
    (repo / "ws.txt").write_text("workstream work")
    _git("add", "ws.txt", cwd=repo)
    _git("commit", "-qm", "workstream-only commit", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)

    _, target, branch = worktree.ensure(cfg, "mr", bead="ws.3", kind="epic")
    assert branch == "wt/bead/epic/ws.3"
    # the nested container forked off the workstream, so it contains the workstream-only commit
    assert (target / "ws.txt").exists()
    assert worktree.is_merged(entry, "wt/bead/epic/ws", "wt/bead/epic/ws.3") is True


# ---- is_merged + bead_and_parent --------------------------------------------


def _ancestry_rig(tmp_path, monkeypatch):
    """Two-commit rig: base commit on main, then a feature branch with one extra commit."""
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "myorg" / "myrepo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "base.txt").write_text("base")
    _git("add", "base.txt", cwd=repo)
    _git("commit", "-qm", "base commit", cwd=repo)

    # feature branch with an extra commit not on main
    _git("checkout", "-q", "-b", "wt/bead/issue/my-bead", cwd=repo)
    (repo / "feat.txt").write_text("feature")
    _git("add", "feat.txt", cwd=repo)
    _git("commit", "-qm", "feature commit", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)

    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    entry = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
    return entry, repo


def test_is_merged_returns_true_when_branch_is_ancestor(tmp_path, monkeypatch):
    entry, repo = _ancestry_rig(tmp_path, monkeypatch)
    # Merge the feature branch into main so it becomes an ancestor
    _git("merge", "--no-ff", "-m", "merge feature", "wt/bead/issue/my-bead", cwd=repo)
    assert worktree.is_merged(entry, "wt/bead/issue/my-bead", "main") is True


def test_is_merged_returns_false_when_branch_is_not_ancestor(tmp_path, monkeypatch):
    entry, repo = _ancestry_rig(tmp_path, monkeypatch)
    # Branch not merged — feature commit is not reachable from main
    assert worktree.is_merged(entry, "wt/bead/issue/my-bead", "main") is False


def test_bead_and_parent_primary_parses_id_from_real_ref(tmp_path, monkeypatch):
    """Primary path: the bead id is parsed from the real wt/bead/<type>/<id> ref (dots preserved,
    unlike the dashed dir leaf) supplied by managed()."""
    entry, repo = _ancestry_rig(tmp_path, monkeypatch)
    wts_root = tmp_path / "wts"
    monkeypatch.setenv("WS_WORKTREES", str(wts_root))
    wt_path = wts_root / "github" / "myorg" / "myrepo" / "bc-88vi-1"  # dashed dir leaf

    bead_id, parent = worktree.bead_and_parent(
        entry, str(wt_path), "main", branch="wt/bead/issue/bc-88vi.1"
    )
    assert bead_id == "bc-88vi.1"  # dot preserved from the ref, not the dashed leaf
    assert parent == "main"  # no container ancestor → integration


def test_bead_and_parent_resolves_bead_id_and_integration(tmp_path, monkeypatch):
    """Fallback path: a wt/bead/issue/<id> worktree path resolves to (bead_id, integration) when
    no container branch exists."""
    entry, repo = _ancestry_rig(tmp_path, monkeypatch)
    wts_root = tmp_path / "wts"
    monkeypatch.setenv("WS_WORKTREES", str(wts_root))

    # Create the shadow path for the worktree
    wt_path = wts_root / "github" / "myorg" / "myrepo" / "my-bead"
    wt_path.mkdir(parents=True)

    bead_id, parent = worktree.bead_and_parent(entry, str(wt_path), "main")
    assert bead_id == "my-bead"
    assert parent == "main"  # no container → falls back to integration


def test_bead_and_parent_resolves_container_branch_when_present(tmp_path, monkeypatch):
    """Parent resolves to the parent epic's container branch wt/bead/epic/<epic> when it exists."""
    entry, repo = _ancestry_rig(tmp_path, monkeypatch)
    wts_root = tmp_path / "wts"
    monkeypatch.setenv("WS_WORKTREES", str(wts_root))

    # Simulate a leaf child of a started epic: leaf branch + parent container branch
    _git("branch", "wt/bead/issue/my-epic.3", cwd=repo)
    _git("branch", "wt/bead/epic/my-epic", cwd=repo)

    wt_path = wts_root / "github" / "myorg" / "myrepo" / "my-epic.3"
    wt_path.mkdir(parents=True)

    bead_id, parent = worktree.bead_and_parent(entry, str(wt_path), "main")
    assert bead_id == "my-epic.3"
    assert parent == "wt/bead/epic/my-epic"


def test_bead_and_parent_returns_none_for_non_bead_worktree(tmp_path, monkeypatch):
    """A batch/session worktree (no wt/bead/<type>/<leaf> branch) returns (None, integration)."""
    entry, repo = _ancestry_rig(tmp_path, monkeypatch)
    wts_root = tmp_path / "wts"
    monkeypatch.setenv("WS_WORKTREES", str(wts_root))

    # Session-style leaf with no corresponding wt/bead/<leaf> branch
    wt_path = wts_root / "github" / "myorg" / "myrepo" / "some-session"
    wt_path.mkdir(parents=True)

    bead_id, parent = worktree.bead_and_parent(entry, str(wt_path), "main")
    assert bead_id is None
    assert parent == "main"


# ---- ensure() start-point threading ----------------------------------------


def _ensure_rig(tmp_path, monkeypatch):
    """Full rig environment for ensure() tests: real git clone + managed worktrees root."""
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
    # Isolate HOME so ws's git ops (which scrub GIT_CONFIG_GLOBAL) use default git config — the
    # rebase-then-retry tests must be deterministic regardless of the developer's ~/.gitconfig.
    (tmp_path / "home").mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.delenv("GIT_CONFIG_GLOBAL", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    entry = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
    cfg = {"managed_repos": [entry]}
    return cfg, entry, repo


def test_ensure_new_bead_forks_off_container_branch_when_it_exists(tmp_path, monkeypatch):
    """A new bead worktree forks off its parent's container wt/bead/epic/<epic> when that branch
    exists — the container-only commit is visible in the new worktree."""
    cfg, entry, repo = _ensure_rig(tmp_path, monkeypatch)

    # Create wt/bead/epic/<epic> with an extra commit that main does not have
    _git("checkout", "-b", "wt/bead/epic/ag-epic", cwd=repo)
    (repo / "mol.txt").write_text("molecule work")
    _git("add", "mol.txt", cwd=repo)
    _git("commit", "-qm", "container-only commit", cwd=repo)
    _git("checkout", "main", cwd=repo)

    _, target, br = worktree.ensure(cfg, "mr", "ag-epic.3")

    assert br == "wt/bead/issue/ag-epic.3"  # a leaf child rides the issue namespace
    # The new worktree must contain the container-branch commit's file
    assert (target / "mol.txt").exists(), "worktree should contain container-only commit"
    assert (target / "f.txt").exists(), "worktree should also contain integration base"


def test_ensure_new_bead_forks_off_integration_when_no_container_branch(tmp_path, monkeypatch):
    """A new bead worktree forks off the integration branch when no container branch exists."""
    cfg, entry, repo = _ensure_rig(tmp_path, monkeypatch)
    # No wt/bead/epic/ag-epic branch — molecule not yet kicked off

    _, target, _ = worktree.ensure(cfg, "mr", "ag-epic.3")

    assert (target / "f.txt").exists(), "worktree should contain integration-branch file"
    assert not (target / "mol.txt").exists(), "container-only file must not appear"


def test_ensure_epic_kind_opens_container_namespace(tmp_path, monkeypatch):
    """ensure(..., kind='epic') provisions the coordinator seat on wt/bead/epic/<id> — the same
    op as a developer seat, differing only in the <type> segment (design xn3o.6)."""
    cfg, entry, repo = _ensure_rig(tmp_path, monkeypatch)

    _, target, br = worktree.ensure(cfg, "mr", "ag-epic", kind="epic")

    assert br == "wt/bead/epic/ag-epic"
    assert worktree._branch_exists(repo, "wt/bead/epic/ag-epic") is True


def test_ensure_same_host_resume_reattaches_exact_worktree(tmp_path, monkeypatch):
    """Same-host resume is deterministic: a second ensure() re-derives wt/bead/issue/<id> and
    re-attaches the exact live worktree dir (idempotent), recovering in-progress work — the payoff
    of stable naming (design xn3o.5)."""
    cfg, entry, repo = _ensure_rig(tmp_path, monkeypatch)

    _, target1, br1 = worktree.ensure(cfg, "mr", "ag-epic.3")
    # simulate uncommitted in-progress work in the live worktree
    (target1 / "wip.txt").write_text("in progress")

    _, target2, br2 = worktree.ensure(cfg, "mr", "ag-epic.3")

    assert br2 == br1 == "wt/bead/issue/ag-epic.3"
    assert target2 == target1  # exact same worktree dir, deterministically re-derived
    assert (target2 / "wip.txt").read_text() == "in progress"  # uncommitted work preserved


# ---- _resolve_entry from a worktree cwd (reverse-map the shadow root) --------


def test_resolve_entry_from_worktree_cwd_needs_no_rig(tmp_path, monkeypatch):
    """cwd inside a managed worktree (under the shadow root, NOT under $GIT_WORKSPACE) resolves
    the right rig with no --rig: workspace_identity returns None, so we reverse-map the path."""
    cfg, entry, _ = _ensure_rig(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "ag-epic.3")

    monkeypatch.chdir(target)  # an agent running ws from inside its worktree
    resolved = worktree._resolve_entry(cfg, "")

    assert (resolved["provider"], resolved["org"], resolved["repo"]) == (
        "github",
        "myorg",
        "myrepo",
    )
    assert resolved["prefix"] == "mr"  # the registered entry, not a synthesized stand-in


def test_resolve_entry_errors_outside_any_rig(tmp_path, monkeypatch):
    """cwd outside both $GIT_WORKSPACE and the shadow worktrees root still errors clearly."""
    cfg, _, _ = _ensure_rig(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)

    with pytest.raises(typer.Exit):
        worktree._resolve_entry(cfg, "")


# ---- cwd_identity (side-effect-free triplet + worktree leaf for telemetry) ---


def test_cwd_identity_from_worktree(tmp_path, monkeypatch):
    """Inside a managed worktree, cwd_identity reverse-maps the path to (triplet, leaf) — no
    typer.Exit, no echo (it must be safe to call while building the OTel Resource)."""
    cfg, _, _ = _ensure_rig(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "ag-epic.3")
    monkeypatch.chdir(target)

    triplet, leaf = worktree.cwd_identity(cfg)

    assert triplet == ("github", "myorg", "myrepo")
    assert leaf == "ag-epic-3"  # the sanitized worktree dir name (bead id, '.'→'-')


def test_cwd_identity_none_outside_any_rig(tmp_path, monkeypatch):
    """Outside both the shadow root and $GIT_WORKSPACE, cwd_identity returns (None, '') quietly
    (never raises) so enrichment simply omits the identity attributes."""
    cfg, _, _ = _ensure_rig(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert worktree.cwd_identity(cfg) == (None, "")


# ---- cwd_worktree_dir (side-effect-free worktree-root path for the overlay) --


def test_cwd_worktree_dir_from_nested_cwd(tmp_path, monkeypatch):
    """From anywhere inside (or below) a managed worktree, returns the worktree ROOT dir — the
    overlay's `.ws/otel.env` lives there, not in a nested subdir."""
    cfg, _, _ = _ensure_rig(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "ag-epic.3")
    nested = target / "src" / "pkg"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert worktree.cwd_worktree_dir(cfg) == target.resolve()


def test_cwd_worktree_dir_none_outside_shadow_root(tmp_path, monkeypatch):
    cfg, _, _ = _ensure_rig(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert worktree.cwd_worktree_dir(cfg) is None


def test_cwd_worktree_dir_none_at_repo_level(tmp_path, monkeypatch):
    """The <root>/<provider>/<org>/<repo> level (no leaf) is not a worktree → None."""
    root = (tmp_path / "wts").resolve()
    monkeypatch.setenv("WS_WORKTREES", str(root))
    repo_level = root / "github" / "myorg" / "myrepo"
    repo_level.mkdir(parents=True)
    monkeypatch.chdir(repo_level)

    assert worktree.cwd_worktree_dir() is None


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


# ---- try_merge_rebase: rebase-then-retry conflict recovery ------------------


def _gitout(*args, cwd) -> str:
    return run(
        ["git", *args], cwd=str(cwd), check=True, capture=True, env=_CLEAN_ENV
    ).stdout.strip()


def _set_line(wt, content, fname="s.txt"):
    (wt / fname).write_text(content)
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", f"feat: set {content.strip()}", cwd=wt)


def _shared_base_rig(tmp_path, monkeypatch, initial):
    """An _ensure_rig with a shared `s.txt` (content=`initial`) committed on main, so worktrees
    forked off it diverge on the SAME file."""
    cfg, entry, repo = _ensure_rig(tmp_path, monkeypatch)
    (repo / "s.txt").write_text(initial)
    _git("add", "s.txt", cwd=repo)
    _git("commit", "-qm", "chore: add s", cwd=repo)
    return cfg, entry, repo


def _append(wt, line, fname="s.txt"):
    p = wt / fname
    p.write_text(p.read_text() + line)
    _git("add", "-A", cwd=wt)
    _git("commit", "-qm", f"feat: append {line.strip()}", cwd=wt)


def test_try_merge_rebase_lands_coupled_change(tmp_path, monkeypatch):
    """Two coupled bead branches: A adds a boilerplate line; B adds the same line (a replay-
    skippable patch) plus its own. merge_no_ff lands A; try_merge_rebase lands B with both beads'
    work present and the duplicate de-duped — succeeding either by clean auto-resolve or by rebase-
    retry (modern git may resolve the coupled case at merge time, so we accept both)."""
    cfg, entry, repo = _shared_base_rig(tmp_path, monkeypatch, "L0\n")
    _, t1, b1 = worktree.ensure(cfg, "mr", "x-1")
    _, t2, b2 = worktree.ensure(cfg, "mr", "x-2")
    _append(t1, "shared\n")  # bead A adds boilerplate
    _append(t2, "shared\n")  # bead B adds the SAME line (replay-skippable patch)…
    _append(t2, "bonly\n")  # …plus its own unique change

    assert worktree.merge_no_ff(entry, b1, "main")[0] == 0  # first lands clean
    rc, _out, how = worktree.try_merge_rebase(entry, b2, "main", t2)

    assert rc == 0 and how in ("clean", "rebased")
    s = (repo / "s.txt").read_text()
    assert "bonly" in s  # bead B's unique work is on the base
    assert s.count("shared") == 1  # A's coupled line present exactly once (no dup, no loss)
    # history preserved: a real --no-ff merge bubble for the second bead
    parents = _gitout("rev-list", "--parents", "-n", "1", "main", cwd=repo).split()
    assert len(parents) == 3


def test_try_merge_rebase_clean_path_reports_clean(tmp_path, monkeypatch):
    """No conflict → behaves like a plain merge_no_ff and reports how='clean'."""
    cfg, entry, repo = _shared_base_rig(tmp_path, monkeypatch, "L0\n")
    _, t1, b1 = worktree.ensure(cfg, "mr", "x-1")
    (t1 / "only.txt").write_text("solo\n")  # touches a different file → no conflict
    _git("add", "-A", cwd=t1)
    _git("commit", "-qm", "feat: solo", cwd=t1)

    rc, _out, how = worktree.try_merge_rebase(entry, b1, "main", t1)
    assert rc == 0 and how == "clean"


def test_try_merge_rebase_restores_branch_on_real_conflict(tmp_path, monkeypatch):
    """Two bead branches edit the SAME line divergently — unresolvable. try_merge_rebase fails
    (how='conflict'), main is untouched, and the bead branch is reset to its pre-rebase tip."""
    cfg, entry, repo = _shared_base_rig(tmp_path, monkeypatch, "base\n")
    _, t1, b1 = worktree.ensure(cfg, "mr", "y-1")
    _, t2, b2 = worktree.ensure(cfg, "mr", "y-2")
    _set_line(t1, "X\n")
    _set_line(t2, "Y\n")

    assert worktree.merge_no_ff(entry, b1, "main")[0] == 0
    main_before = _gitout("rev-parse", "main", cwd=repo)
    branch_before = _gitout("rev-parse", b2, cwd=repo)

    rc, _out, how = worktree.try_merge_rebase(entry, b2, "main", t2)

    assert rc != 0 and how == "conflict"
    assert _gitout("rev-parse", "main", cwd=repo) == main_before  # main untouched
    assert _gitout("rev-parse", b2, cwd=repo) == branch_before  # branch restored
    assert _gitout("show", f"{b2}:s.txt", cwd=repo) == "Y"  # still carries its own change
    # the recovery path was entered: a pre-merge snapshot of the bead branch exists
    assert "premerge" in _gitout("branch", "--list", f"{b2}.premerge-*", cwd=repo)


# ---- try_merge_rebase: bounded union tier + mandatory re-validation ---------


def test_try_merge_rebase_union_lands_append_only_conflict(tmp_path, monkeypatch):
    """Two beads each append a DIFFERENT line at the EOF of a whitelisted file: A lands clean,
    B can't replay (the appends collide) so the union tier keeps BOTH lines, how='union', and a
    real --no-ff merge bubble preserves history."""
    cfg, entry, repo = _shared_base_rig(tmp_path, monkeypatch, "L0\n")
    _, t1, b1 = worktree.ensure(cfg, "mr", "u-1")
    _, t2, b2 = worktree.ensure(cfg, "mr", "u-2")
    _append(t1, "fromA\n")
    _append(t2, "fromB\n")

    assert worktree.merge_no_ff(entry, b1, "main")[0] == 0
    rc, _out, how = worktree.try_merge_rebase(entry, b2, "main", t2, union_globs=("*.txt",))

    assert rc == 0 and how == "union"
    s = (repo / "s.txt").read_text()
    assert "fromA" in s and "fromB" in s  # both appends kept (union driver)
    parents = _gitout("rev-list", "--parents", "-n", "1", "main", cwd=repo).split()
    assert len(parents) == 3  # merge bubble preserved


def test_try_merge_rebase_union_validation_failure_restores(tmp_path, monkeypatch):
    """A union merge whose result fails validate_cmd is NOT landed: the integration branch is
    hard-reset to its pre-union tip and the bead branch is restored — bounce with how='conflict'."""
    cfg, entry, repo = _shared_base_rig(tmp_path, monkeypatch, "L0\n")
    _, t1, b1 = worktree.ensure(cfg, "mr", "uv-1")
    _, t2, b2 = worktree.ensure(cfg, "mr", "uv-2")
    _append(t1, "fromA\n")
    _append(t2, "fromB\n")

    assert worktree.merge_no_ff(entry, b1, "main")[0] == 0
    main_before = _gitout("rev-parse", "main", cwd=repo)
    branch_before = _gitout("rev-parse", b2, cwd=repo)

    rc, _out, how = worktree.try_merge_rebase(
        entry, b2, "main", t2, union_globs=("*.txt",), validate_cmd="false"
    )

    assert rc != 0 and how == "conflict"
    assert _gitout("rev-parse", "main", cwd=repo) == main_before  # integration restored
    assert _gitout("rev-parse", b2, cwd=repo) == branch_before  # bead branch restored


def test_try_merge_rebase_union_skipped_for_nonwhitelisted_path(tmp_path, monkeypatch):
    """A conflict on a path OUTSIDE the whitelist skips the union tier entirely and bounces as
    today — integration and bead branch both untouched."""
    cfg, entry, repo = _shared_base_rig(tmp_path, monkeypatch, "base\n")
    _, t1, b1 = worktree.ensure(cfg, "mr", "un-1")
    _, t2, b2 = worktree.ensure(cfg, "mr", "un-2")
    _set_line(t1, "X\n")
    _set_line(t2, "Y\n")

    assert worktree.merge_no_ff(entry, b1, "main")[0] == 0
    main_before = _gitout("rev-parse", "main", cwd=repo)
    branch_before = _gitout("rev-parse", b2, cwd=repo)

    rc, _out, how = worktree.try_merge_rebase(entry, b2, "main", t2, union_globs=("docs/*",))

    assert rc != 0 and how == "conflict"
    assert _gitout("rev-parse", "main", cwd=repo) == main_before
    assert _gitout("rev-parse", b2, cwd=repo) == branch_before
    assert _gitout("show", f"{b2}:s.txt", cwd=repo) == "Y"  # bead keeps its own change


def test_try_merge_rebase_empty_union_globs_unchanged(tmp_path, monkeypatch):
    """Empty union_globs (the default) ⇒ a real conflict bounces exactly as before — no union."""
    cfg, entry, repo = _shared_base_rig(tmp_path, monkeypatch, "base\n")
    _, t1, b1 = worktree.ensure(cfg, "mr", "ue-1")
    _, t2, b2 = worktree.ensure(cfg, "mr", "ue-2")
    _set_line(t1, "X\n")
    _set_line(t2, "Y\n")

    assert worktree.merge_no_ff(entry, b1, "main")[0] == 0
    rc, _out, how = worktree.try_merge_rebase(entry, b2, "main", t2, union_globs=())

    assert rc != 0 and how == "conflict"


# ---- provision_observaloop (worktree-create hook) ---------------------------
#
# The per-rig profile provisioning + .ws/otel.env overlay that _do_add runs AFTER run_init on a
# true worktree create. Observaloop is faked throughout. Covers: enabled (ensure+up+overlay),
# disabled-and-import-free (default path touches no observaloop module), failure-still-succeeds
# (any exception warns, never raises), and verify- skip (ephemeral clean-checkout worktrees).

_OBS_RIG = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
_OBS_ENABLED_CFG = {
    "otel": {"enabled": True},
    "observaloop": {"enabled": True},
    "managed_repos": [_OBS_RIG],
}


def test_provision_observaloop_enabled_ensures_profile_and_writes_overlay(tmp_path, monkeypatch):
    """Enabled → ensure_profile + up (idempotent) then write <worktree>/.ws/otel.env at the
    resolved endpoint, so a ws invocation there exports to the rig profile."""
    from ws import observaloop

    calls = {"ensure": [], "up": []}
    monkeypatch.setattr(
        observaloop, "ensure_profile", lambda name, cfg=None: calls["ensure"].append(name)
    )
    monkeypatch.setattr(observaloop, "up", lambda name, cfg=None: calls["up"].append(name))
    monkeypatch.setattr(
        observaloop, "endpoint_for", lambda name, proto, cfg=None: "http://localhost:4318"
    )

    target = tmp_path / "wt"
    target.mkdir()
    worktree.provision_observaloop(_OBS_ENABLED_CFG, _OBS_RIG, target)

    assert calls["ensure"] == ["mr"] and calls["up"] == ["mr"]  # profile ensured + up
    env_file = target / ".ws" / "otel.env"
    assert env_file.is_file()
    body = env_file.read_text()
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318" in body
    assert "WS_OBSERVALOOP_PROFILE=mr" in body


def test_provision_observaloop_disabled_is_import_free_and_writes_nothing(tmp_path, monkeypatch):
    """Default/off path: no overlay written AND ws.observaloop is never imported (cheap path)."""
    sys.modules.pop("ws.observaloop", None)
    sys.modules.pop("ws.observaloop_env", None)

    target = tmp_path / "wt"
    target.mkdir()
    worktree.provision_observaloop({"otel": {"enabled": False}}, _OBS_RIG, target)

    assert not (target / ".ws").exists()  # nothing provisioned
    assert "ws.observaloop" not in sys.modules  # default path imports no observaloop seam


def test_provision_observaloop_failure_warns_and_does_not_raise(tmp_path, monkeypatch):
    """Observaloop/docker failure (any exception) warns and returns — NEVER blocks creation."""
    from ws import observaloop

    def _boom(*a, **k):
        raise RuntimeError("docker down")

    monkeypatch.setattr(observaloop, "ensure_profile", _boom)

    target = tmp_path / "wt"
    target.mkdir()
    worktree.provision_observaloop(_OBS_ENABLED_CFG, _OBS_RIG, target)  # must not raise

    assert not (target / ".ws").exists()  # overlay not written, but creation survives


def test_provision_observaloop_no_endpoint_skips_overlay(tmp_path, monkeypatch):
    """Unavailable / down → endpoint_for returns None → overlay is skipped (warn-and-continue)."""
    from ws import observaloop

    monkeypatch.setattr(observaloop, "ensure_profile", lambda name, cfg=None: None)
    monkeypatch.setattr(observaloop, "up", lambda name, cfg=None: None)
    monkeypatch.setattr(observaloop, "endpoint_for", lambda name, proto, cfg=None: None)

    target = tmp_path / "wt"
    target.mkdir()
    worktree.provision_observaloop(_OBS_ENABLED_CFG, _OBS_RIG, target)  # must not raise

    assert not (target / ".ws").exists()


def test_provision_observaloop_skips_verify_leaf(tmp_path, monkeypatch):
    """A verify- leaf (ephemeral clean-checkout) is defensively skipped even when enabled."""
    from ws import observaloop

    called = []
    monkeypatch.setattr(observaloop, "ensure_profile", lambda name, cfg=None: called.append(name))

    target = tmp_path / f"{worktree.VERIFY_LEAF_PREFIX}ag-epic-3"
    target.mkdir()
    worktree.provision_observaloop(_OBS_ENABLED_CFG, _OBS_RIG, target)

    assert called == []  # never provisioned
    assert not (target / ".ws").exists()


# ---- clean_checkout: telemetry-neutral validation env -----


def test_clean_checkout_validation_env_is_telemetry_neutral(tmp_path, monkeypatch):
    """The clean-checkout validation child runs with telemetry scrubbed: no OTEL_* /
    WS_OBSERVALOOP_PROFILE leak from the parent (so submit's result can't depend on the operator's
    otel config), OTEL_SDK_DISABLED forced on, and non-telemetry env (PATH) preserved — the bug
    surfaced in where submit's validation inherited the worktree overlay
    endpoint."""
    cfg, entry, repo = _ensure_rig(tmp_path, monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "ws.rig=mr")
    monkeypatch.setenv("WS_OBSERVALOOP_PROFILE", "dev")
    monkeypatch.setenv("PATH", "/sentinel/bin")

    calls = []

    class _Done:
        returncode = 0

    def _fake_run(cmd, **kw):
        calls.append((list(cmd), kw))
        return _Done()

    # Fake the subprocess seam so the git worktree add/remove no-op (rc 0) and we can inspect the
    # env handed to the validation spawn without running a real command.
    monkeypatch.setattr(worktree, "run", _fake_run)

    rc = worktree.clean_checkout(entry, "main", "just check")
    assert rc == 0

    # The validation spawn is the only non-git run() call (others are `git worktree add/remove`).
    val = [(cmd, kw) for cmd, kw in calls if cmd[:1] != ["git"]]
    assert len(val) == 1
    cmd, kw = val[0]
    assert cmd == ["just", "check"]
    env = kw["env"]
    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    assert not any(k.startswith("OTEL_") and k != "OTEL_SDK_DISABLED" for k in env)
    assert "WS_OBSERVALOOP_PROFILE" not in env
    assert env["OTEL_SDK_DISABLED"] == "true"
    assert env["PATH"] == "/sentinel/bin"  # non-telemetry env preserved
