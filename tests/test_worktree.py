"""Worktree self-checks — the money paths: naming/templating, session sortability,
declarative init-rule evaluation, and the path-prefix 'managed' filter."""

from __future__ import annotations

import datetime
import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
import typer

from beadhive import config, ghpr, orca, plugins, validation_ledger, worktree, wt_status
from beadhive.run import run

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


def test_config_example_justfile_rule_is_probe_guarded():
    """The shipped default just-setup rule probes for the recipe: template YAML parses, the
    rule shell-splits cleanly, and a repo without a `setup` recipe gets a quiet info echo —
    never the warn path (`just setup` is not run blind)."""
    import shlex

    import yaml

    cfg = yaml.safe_load(config.template("config.example.yaml").read_text())
    rules = [r for r in cfg["worktrees"]["init"] if r.get("if_exists") == "justfile"]
    assert len(rules) == 1
    argv = shlex.split(rules[0]["run"])
    assert argv[:2] == ["sh", "-c"]
    assert "just --show setup" in argv[2]  # probe before running
    assert "just setup: not configured in this repo" in argv[2]  # quiet info fallback


def test_run_init_appends_per_hive_rules(tmp_path):
    cfg = {"worktrees": {"init": [{"run": "touch global.marker"}]}}
    entry = {"worktree_init": [{"run": "touch hive.marker"}]}
    worktree.run_init(cfg, entry, tmp_path)
    assert (tmp_path / "global.marker").exists()
    assert (tmp_path / "hive.marker").exists()


_VERIFY_RULES = {
    "worktrees": {
        "init": [
            {"run": "touch flagged.marker", "verify": True},
            {"run": "touch unflagged.marker"},
        ]
    }
}


def test_run_init_verify_only_filters_to_flagged_rules(tmp_path):
    """verify_only (the clean_checkout pass, bh-7k1p) runs ONLY rules flagged verify: true —
    unflagged seat provisioning never fires per validation."""
    worktree.run_init(_VERIFY_RULES, {}, tmp_path, verify_only=True)
    assert (tmp_path / "flagged.marker").exists()
    assert not (tmp_path / "unflagged.marker").exists()


def test_run_init_default_mode_runs_flagged_and_unflagged(tmp_path):
    """verify: true opts a rule IN to verify checkouts — it does not opt it OUT of the normal
    _do_add create pass, where both flagged and unflagged rules run."""
    worktree.run_init(_VERIFY_RULES, {}, tmp_path)
    assert (tmp_path / "flagged.marker").exists()
    assert (tmp_path / "unflagged.marker").exists()


# ---- declared toolchains are knowledge-only (bh-d0kb, revised) ---------------


def test_rules_ignore_declared_toolchain():
    """A declaration NEVER contributes init rules — knowledge-only (the revised bh-d0kb
    decision): nothing runs because of a `toolchain:` key, global or per-hive."""
    assert worktree._rules({"worktrees": {"toolchain": "uv"}}, {}) == []
    assert worktree._rules({"worktrees": {"toolchain": ["uv", "just"]}}, {}) == []
    assert worktree._rules({}, {"toolchain": "npm"}) == []
    assert worktree._rules({}, {}) == []  # unset stays today's empty default


def test_rules_come_from_explicit_config_only():
    """Global worktrees.init + the hive's worktree_init are the whole rule set, with or
    without a declaration alongside."""
    cfg = {"worktrees": {"toolchain": "uv", "init": [{"run": "echo explicit"}]}}
    assert worktree._rules(cfg, {}) == [{"run": "echo explicit"}]
    entry = {"worktree_init": [{"run": "echo hive"}], "toolchain": "npm"}
    assert worktree._rules({"worktrees": {"toolchain": "uv"}}, entry) == [{"run": "echo hive"}]
    assert worktree._rules(cfg, entry) == [{"run": "echo explicit"}, {"run": "echo hive"}]


def test_run_init_never_runs_toolchain_template_rules(tmp_path):
    """End-to-end through run_init: even a registry override carrying a live suggested
    rule executes nothing — suggestions are propose-only, never provisioning."""
    cfg = {
        "worktrees": {
            "toolchain": "mytc",
            "toolchains": {"mytc": {"suggested_init": [{"run": "touch tc.marker"}]}},
        }
    }
    worktree.run_init(cfg, {}, tmp_path)
    assert not (tmp_path / "tc.marker").exists()


# ---- integration_base climb -------------------------------------------------


def _mol_hive(tmp_path, monkeypatch):
    """A real one-commit hive clone under GIT_WORKSPACE; returns its managed_repos entry."""
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
    entry, repo = _mol_hive(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/ag-epic", cwd=repo)  # epic kicked off → container present
    assert worktree.integration_base(entry, "ag-epic.3", "main") == "wt/bead/epic/ag-epic"


def test_integration_base_two_hop_workstream_present(tmp_path, monkeypatch):
    """2-hop: nearest-first — a grandchild lands on its epic when that container exists, even
    though a workstream container exists one tier above."""
    entry, repo = _mol_hive(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/ws", cwd=repo)  # workstream container (grandparent)
    _git("branch", "wt/bead/epic/ws.2", cwd=repo)  # epic container (parent) — nearest wins
    assert worktree.integration_base(entry, "ws.2.5", "main") == "wt/bead/epic/ws.2"


def test_integration_base_two_hop_climbs_to_workstream(tmp_path, monkeypatch):
    """2-hop climb: with only the workstream container present, an epic <ws>.<n> lands on the
    workstream — its own epic container isn't opened (it IS the container being resolved for)."""
    entry, repo = _mol_hive(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/ws", cwd=repo)  # workstream container only
    assert worktree.integration_base(entry, "ws.2", "main") == "wt/bead/epic/ws"


def test_integration_base_zero_hop_no_container(tmp_path, monkeypatch):
    """0-hop: no container branch anywhere in the chain → the hive integration branch (main)."""
    entry, _ = _mol_hive(tmp_path, monkeypatch)  # no container branches
    assert worktree.integration_base(entry, "ag-epic.3", "main") == "main"


def test_integration_base_no_dot_is_root(tmp_path, monkeypatch):
    """A dotless (top-level) id has no parent to climb to → integration (main)."""
    entry, repo = _mol_hive(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/ag-epic", cwd=repo)  # present, but the id itself is the root
    assert worktree.integration_base(entry, "ag-epic", "main") == "main"


def test_integration_base_skips_issue_type_ancestor(tmp_path, monkeypatch):
    """A sub-bead of an ISSUE (xn3o.5.1) finds no container at its parent (that ref lives under
    issue/, not a CONTAINER_TYPE), so the climb walks past it to the epic — fixing the latent
    single-hop bug that would have targeted integration directly."""
    entry, repo = _mol_hive(tmp_path, monkeypatch)
    _git("branch", "wt/bead/issue/xn3o.5", cwd=repo)  # parent is a leaf issue, not a container
    _git("branch", "wt/bead/epic/xn3o", cwd=repo)  # grandparent epic container
    assert worktree.integration_base(entry, "xn3o.5.1", "main") == "wt/bead/epic/xn3o"


# ---- parent-link resolution (bh-2m6v: re-parent/split) ----------------------


def _fake_bd_show(monkeypatch, parents: dict, status: dict | None = None):
    """Stub beadhive.bd.show so worktree's parent-link climb reads a synthetic parent map."""
    status = status or {}

    def _show(bead, cwd):  # noqa: ARG001 — cwd irrelevant to the stub
        if bead not in parents and bead not in status:
            return None
        return {"parent": parents.get(bead, ""), "status": status.get(bead, "in_progress")}

    monkeypatch.setattr("beadhive.bd.show", _show)


def test_integration_base_reparented_child_follows_parent_link(tmp_path, monkeypatch):
    """A child re-parented under a NEW epic but keeping its birth `<oldepic>.<n>` dotted id lands
    on its parent-link container — not the stale prefix (whose container is gone)."""
    entry, repo = _mol_hive(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/ji4p", cwd=repo)  # new parent container; vwhk container is gone
    _fake_bd_show(monkeypatch, {"vwhk.3": "ji4p"})
    assert worktree.integration_base(entry, "vwhk.3", "main") == "wt/bead/epic/ji4p"


def test_integration_base_prefers_parent_link_over_stale_prefix(tmp_path, monkeypatch):
    """When BOTH the stale-prefix container and the parent-link container exist, the parent-link
    (source of truth after a re-parent) wins."""
    entry, repo = _mol_hive(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/vwhk", cwd=repo)  # stale birth container still lingers
    _git("branch", "wt/bead/epic/ji4p", cwd=repo)  # real parent after re-parent
    _fake_bd_show(monkeypatch, {"vwhk.3": "ji4p"})
    assert worktree.integration_base(entry, "vwhk.3", "main") == "wt/bead/epic/ji4p"


def test_container_conflict_flags_live_disagreement(tmp_path, monkeypatch):
    """A re-parent that leaves BOTH containers live is a genuine ambiguity a merge must refuse."""
    entry, repo = _mol_hive(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/vwhk", cwd=repo)
    _git("branch", "wt/bead/epic/ji4p", cwd=repo)
    _fake_bd_show(monkeypatch, {"vwhk.3": "ji4p"})
    assert worktree.container_conflict(entry, "vwhk.3", "main") == (
        "wt/bead/epic/vwhk",
        "wt/bead/epic/ji4p",
    )


def test_container_conflict_none_when_prefix_gone(tmp_path, monkeypatch):
    """The unambiguous re-parent case (stale container gone) is NOT a conflict — trust the link."""
    entry, repo = _mol_hive(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/ji4p", cwd=repo)
    _fake_bd_show(monkeypatch, {"vwhk.3": "ji4p"})
    assert worktree.container_conflict(entry, "vwhk.3", "main") is None


def test_container_conflict_none_when_link_agrees(tmp_path, monkeypatch):
    """A never-reparented child (id prefix == parent link) is never a conflict."""
    entry, repo = _mol_hive(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/ag-epic", cwd=repo)
    _fake_bd_show(monkeypatch, {"ag-epic.3": "ag-epic"})
    assert worktree.container_conflict(entry, "ag-epic.3", "main") is None


def test_container_epic_closed_detects_landed_container(tmp_path, monkeypatch):
    """A container branch whose epic is closed is a landed container a merge must not resurrect."""
    entry, repo = _mol_hive(tmp_path, monkeypatch)
    _git("branch", "wt/bead/epic/vwhk", cwd=repo)
    _fake_bd_show(monkeypatch, {}, status={"vwhk": "closed"})
    assert worktree.container_epic_closed(entry, "wt/bead/epic/vwhk") is True
    assert worktree.container_epic_closed(entry, "main") is False


def test_ensure_integration_branch_nested_epic_forks_off_workstream(tmp_path, monkeypatch):
    """A nested epic <ws>.<epic> seat (ensure kind='epic', the retired ensure_integration_branch)
    opens its container off the workstream container (integration_base one tier up), not off main
    — so it sees the workstream's assembled work."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
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


def _ancestry_hive(tmp_path, monkeypatch):
    """Two-commit hive: base commit on main, then a feature branch with one extra commit."""
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
    entry, repo = _ancestry_hive(tmp_path, monkeypatch)
    # Merge the feature branch into main so it becomes an ancestor
    _git("merge", "--no-ff", "-m", "merge feature", "wt/bead/issue/my-bead", cwd=repo)
    assert worktree.is_merged(entry, "wt/bead/issue/my-bead", "main") is True


def test_is_merged_returns_false_when_branch_is_not_ancestor(tmp_path, monkeypatch):
    entry, repo = _ancestry_hive(tmp_path, monkeypatch)
    # Branch not merged — feature commit is not reachable from main
    assert worktree.is_merged(entry, "wt/bead/issue/my-bead", "main") is False


# ---- is_landed: GitHub squash-merge detection via gh (bh-v0wu) ---------------


def _squash_landed_hive(tmp_path, monkeypatch):
    """A branch with TWO commits squash-merged into main by hand: not an ancestor, and the
    single squash commit patch-id-matches neither original — exactly the GitHub squash-merge
    shape that defeats every local landed signal."""
    entry, repo = _ancestry_hive(tmp_path, monkeypatch)
    _git("checkout", "-q", "wt/bead/issue/my-bead", cwd=repo)
    (repo / "feat2.txt").write_text("more")
    _git("add", "feat2.txt", cwd=repo)
    _git("commit", "-qm", "feature commit 2", cwd=repo)
    _git("checkout", "-q", "main", cwd=repo)
    _git("merge", "--squash", "wt/bead/issue/my-bead", cwd=repo)
    _git("commit", "-qm", "feature (#7)", cwd=repo)
    # sanity: the squash defeats plain ancestry
    assert worktree.is_merged(entry, "wt/bead/issue/my-bead", "main") is False
    return entry, repo


def _fake_gh(monkeypatch, rows):
    """Patch the ghpr.run seam to serve `rows` for any `gh pr list`; returns the call log."""
    calls = []

    def fake_run(cmd, **_kw):
        calls.append(list(cmd))
        return SimpleNamespace(returncode=0, stdout=json.dumps(rows), stderr="")

    monkeypatch.setattr(ghpr, "run", fake_run)
    monkeypatch.setattr(ghpr, "available", lambda: True)
    return calls


def test_is_landed_detects_github_squash_merge_via_gh(tmp_path, monkeypatch):
    """No close_reason, no ancestry, no patch-id match — but GitHub reports a MERGED PR with
    this head, so the seat classifies LANDED (the bh-v0wu 'UNMERGED forever' fix)."""
    entry, _repo = _squash_landed_hive(tmp_path, monkeypatch)
    pr = {"number": 7, "url": "https://github.com/myorg/myrepo/pull/7", "state": "MERGED"}
    calls = _fake_gh(monkeypatch, [pr])

    assert worktree.is_landed(entry, "wt/bead/issue/my-bead", "main", close_reason="") is True
    (call,) = calls  # exactly one gh probe: pr list --state merged --head <branch>
    assert call[call.index("--state") + 1] == "merged"
    assert call[call.index("--head") + 1] == "wt/bead/issue/my-bead"


def test_is_landed_stays_unmerged_without_any_signal(tmp_path, monkeypatch):
    """Same squash shape but GitHub has no merged PR for the head → conservative UNMERGED."""
    entry, _repo = _squash_landed_hive(tmp_path, monkeypatch)
    _fake_gh(monkeypatch, [])

    assert worktree.is_landed(entry, "wt/bead/issue/my-bead", "main", close_reason="") is False


def test_is_landed_close_reason_short_circuits_gh(tmp_path, monkeypatch):
    """The authoritative merge-event check never reaches for gh (fast path preserved)."""
    entry, _repo = _squash_landed_hive(tmp_path, monkeypatch)

    def boom(*_a, **_k):
        raise AssertionError("gh probed despite an authoritative close_reason")

    monkeypatch.setattr(ghpr, "run", boom)
    assert worktree.is_landed(entry, "wt/bead/issue/my-bead", "main", close_reason="merged") is True


def test_is_landed_gh_probe_only_for_github_backed_hives(tmp_path, monkeypatch):
    """A non-GitHub hive never shells out to gh — the probe is guarded, best-effort."""
    entry, _repo = _squash_landed_hive(tmp_path, monkeypatch)
    entry = {**entry, "provider": "gitlab"}

    def boom(*_a, **_k):
        raise AssertionError("gh probed for a non-github hive")

    monkeypatch.setattr(ghpr, "run", boom)
    monkeypatch.setattr(ghpr, "available", lambda: True)
    assert worktree.is_landed(entry, "wt/bead/issue/my-bead", "main", close_reason="") is False


def test_bead_and_parent_primary_parses_id_from_real_ref(tmp_path, monkeypatch):
    """Primary path: the bead id is parsed from the real wt/bead/<type>/<id> ref (dots preserved,
    unlike the dashed dir leaf) supplied by managed()."""
    entry, repo = _ancestry_hive(tmp_path, monkeypatch)
    wts_root = tmp_path / "wts"
    monkeypatch.setenv("BH_WORKTREES", str(wts_root))
    wt_path = wts_root / "github" / "myorg" / "myrepo" / "bc-88vi-1"  # dashed dir leaf

    bead_id, parent = worktree.bead_and_parent(
        entry, str(wt_path), "main", branch="wt/bead/issue/bc-88vi.1"
    )
    assert bead_id == "bc-88vi.1"  # dot preserved from the ref, not the dashed leaf
    assert parent == "main"  # no container ancestor → integration


def test_bead_and_parent_resolves_bead_id_and_integration(tmp_path, monkeypatch):
    """Fallback path: a wt/bead/issue/<id> worktree path resolves to (bead_id, integration) when
    no container branch exists."""
    entry, repo = _ancestry_hive(tmp_path, monkeypatch)
    wts_root = tmp_path / "wts"
    monkeypatch.setenv("BH_WORKTREES", str(wts_root))

    # Create the shadow path for the worktree
    wt_path = wts_root / "github" / "myorg" / "myrepo" / "my-bead"
    wt_path.mkdir(parents=True)

    bead_id, parent = worktree.bead_and_parent(entry, str(wt_path), "main")
    assert bead_id == "my-bead"
    assert parent == "main"  # no container → falls back to integration


def test_bead_and_parent_resolves_container_branch_when_present(tmp_path, monkeypatch):
    """Parent resolves to the parent epic's container branch wt/bead/epic/<epic> when it exists."""
    entry, repo = _ancestry_hive(tmp_path, monkeypatch)
    wts_root = tmp_path / "wts"
    monkeypatch.setenv("BH_WORKTREES", str(wts_root))

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
    entry, repo = _ancestry_hive(tmp_path, monkeypatch)
    wts_root = tmp_path / "wts"
    monkeypatch.setenv("BH_WORKTREES", str(wts_root))

    # Session-style leaf with no corresponding wt/bead/<leaf> branch
    wt_path = wts_root / "github" / "myorg" / "myrepo" / "some-session"
    wt_path.mkdir(parents=True)

    bead_id, parent = worktree.bead_and_parent(entry, str(wt_path), "main")
    assert bead_id is None
    assert parent == "main"


# ---- ensure() start-point threading ----------------------------------------


def _ensure_hive(tmp_path, monkeypatch):
    """Full hive environment for ensure() tests: real git clone + managed worktrees root."""
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
    monkeypatch.setenv("BH_WORKTREES", str(wts_root))
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
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)

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
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    # No wt/bead/epic/ag-epic branch — molecule not yet kicked off

    _, target, _ = worktree.ensure(cfg, "mr", "ag-epic.3")

    assert (target / "f.txt").exists(), "worktree should contain integration-branch file"
    assert not (target / "mol.txt").exists(), "container-only file must not appear"


def test_ensure_epic_kind_opens_container_namespace(tmp_path, monkeypatch):
    """ensure(..., kind='epic') provisions the coordinator seat on wt/bead/epic/<id> — the same
    op as a developer seat, differing only in the <type> segment (design xn3o.6)."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)

    _, target, br = worktree.ensure(cfg, "mr", "ag-epic", kind="epic")

    assert br == "wt/bead/epic/ag-epic"
    assert worktree._branch_exists(repo, "wt/bead/epic/ag-epic") is True


def test_ensure_same_host_resume_reattaches_exact_worktree(tmp_path, monkeypatch):
    """Same-host resume is deterministic: a second ensure() re-derives wt/bead/issue/<id> and
    re-attaches the exact live worktree dir (idempotent), recovering in-progress work — the payoff
    of stable naming (design xn3o.5)."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)

    _, target1, br1 = worktree.ensure(cfg, "mr", "ag-epic.3")
    # simulate uncommitted in-progress work in the live worktree
    (target1 / "wip.txt").write_text("in progress")

    _, target2, br2 = worktree.ensure(cfg, "mr", "ag-epic.3")

    assert br2 == br1 == "wt/bead/issue/ag-epic.3"
    assert target2 == target1  # exact same worktree dir, deterministically re-derived
    assert (target2 / "wip.txt").read_text() == "in progress"  # uncommitted work preserved


def test_ensure_repoints_stale_child_after_container_refresh(tmp_path, monkeypatch):
    """bh-4wwi: a child provisioned before its container advances is re-pointed to the refreshed
    tip on the next idempotent ensure — it carries no unique work, so the move is lossless."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    _git("checkout", "-b", "wt/bead/epic/ag-epic", cwd=repo)
    (repo / "mol.txt").write_text("v1")
    _git("add", "mol.txt", cwd=repo)
    _git("commit", "-qm", "container v1", cwd=repo)
    _git("checkout", "main", cwd=repo)

    _, target, br = worktree.ensure(cfg, "mr", "ag-epic.3")  # forks off container@v1
    assert (target / "mol.txt").read_text() == "v1"

    # Refresh the container with a new container-only commit
    _git("checkout", "wt/bead/epic/ag-epic", cwd=repo)
    (repo / "mol.txt").write_text("v2")
    _git("add", "mol.txt", cwd=repo)
    _git("commit", "-qm", "container v2", cwd=repo)
    _git("checkout", "main", cwd=repo)

    _, target2, br2 = worktree.ensure(cfg, "mr", "ag-epic.3")

    assert target2 == target and br2 == br  # same worktree, re-pointed in place
    assert (target2 / "mol.txt").read_text() == "v2", "stale empty child should track refreshed tip"


def test_ensure_never_repoints_child_with_real_commits(tmp_path, monkeypatch):
    """bh-4wwi: a child carrying its own commits is NEVER re-pointed, even when its container has
    advanced — its work is preserved and the container commit is not forced in."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    _git("checkout", "-b", "wt/bead/epic/ag-epic", cwd=repo)
    (repo / "mol.txt").write_text("v1")
    _git("add", "mol.txt", cwd=repo)
    _git("commit", "-qm", "container v1", cwd=repo)
    _git("checkout", "main", cwd=repo)

    _, target, br = worktree.ensure(cfg, "mr", "ag-epic.3")
    (target / "child.txt").write_text("my work")  # the child does real work
    _git("add", "child.txt", cwd=target)
    _git("commit", "-qm", "feat: child work", cwd=target)

    _git("checkout", "wt/bead/epic/ag-epic", cwd=repo)  # container advances
    (repo / "mol.txt").write_text("v2")
    _git("add", "mol.txt", cwd=repo)
    _git("commit", "-qm", "container v2", cwd=repo)
    _git("checkout", "main", cwd=repo)

    _, target2, _ = worktree.ensure(cfg, "mr", "ag-epic.3")

    assert target2 == target
    assert (target2 / "child.txt").read_text() == "my work"  # child work preserved
    assert (target2 / "mol.txt").read_text() == "v1", "container v2 must NOT be forced in"


def test_refresh_container_writes_conventional_merge_subject(tmp_path, monkeypatch):
    """bh-cgxc: container refresh must merge with an explicit conventional subject. A bare
    `git merge --no-edit` writes git's default 'Merge branch …' subject, which a commitizen
    commit-msg hook rejects on hook-enforcing hives — the same failure bh-fr0a fixed for landing
    bubbles, here on the upstream-sync path."""
    from beadhive.work_logic import _CONVENTIONAL

    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)

    # Provision the container seat and give it a container-only commit main doesn't have.
    _, seat, br = worktree.ensure(cfg, "mr", "ag-epic", kind="epic")
    assert br == "wt/bead/epic/ag-epic"
    (seat / "mol.txt").write_text("container work")
    _git("add", "mol.txt", cwd=seat)
    _git("commit", "-qm", "feat(mol): container-only commit", cwd=seat)

    # Advance upstream (main) with a commit the container lacks → forces a real merge commit
    # (not a fast-forward), so the merge subject is actually written.
    (repo / "up.txt").write_text("upstream work")
    _git("add", "up.txt", cwd=repo)
    _git("commit", "-qm", "fix(up): upstream advance", cwd=repo)

    worktree.refresh_container(entry, "wt/bead/epic/ag-epic", "main")

    subject = _gitout("log", "-1", "--format=%s", cwd=seat)
    assert subject == "chore(merge): refresh wt/bead/epic/ag-epic from main"
    assert _CONVENTIONAL.match(subject), f"non-conventional merge subject: {subject!r}"


# ---- preview() / add(--preview --json) contract (bh-73rz.3) -----------------


def test_preview_would_create_for_a_brand_new_bead(tmp_path, monkeypatch):
    """No branch, no worktree dir yet → `would` is 'create', with a start_point (forked off the
    integration branch since no container exists), and zero side effects."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)

    result = worktree.preview(cfg, "mr", bead="ag-epic.3")

    assert result["op"] == "add"
    assert result["hive"] == "github/myorg/myrepo"
    assert result["bead"] == "ag-epic.3"
    assert result["branch"] == "wt/bead/issue/ag-epic.3"
    assert result["would"] == "create"
    assert result["start_point"] == "main"
    assert result["branch_exists"] is False
    assert result["path_exists"] is False
    assert isinstance(result["init"], list)
    # side-effect-free: no branch, no worktree dir
    assert worktree._branch_exists(repo, "wt/bead/issue/ag-epic.3") is False
    assert not Path(result["path"]).exists()


def test_preview_would_attach_when_branch_exists_but_no_live_dir(tmp_path, monkeypatch):
    """Branch already exists (e.g. pruned worktree) but the dir doesn't → `would` is 'attach'."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    _git("branch", "wt/bead/issue/ag-epic.3", cwd=repo)

    result = worktree.preview(cfg, "mr", bead="ag-epic.3")

    assert result["would"] == "attach"
    assert result["branch_exists"] is True
    assert result["path_exists"] is False
    assert result["start_point"] == ""  # only 'create' resolves a start point


def test_preview_would_reuse_when_worktree_dir_already_live(tmp_path, monkeypatch):
    """A live worktree dir at the target path → `would` is 'reuse'."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    worktree.ensure(cfg, "mr", "ag-epic.3")  # provision it for real first

    result = worktree.preview(cfg, "mr", bead="ag-epic.3")

    assert result["would"] == "reuse"
    assert result["branch_exists"] is True
    assert result["path_exists"] is True


def test_add_preview_json_matches_preview_and_changes_nothing(tmp_path, monkeypatch, capsys):
    """`add(..., dry_run=True, as_json=True)` prints exactly the `preview()` contract and
    provisions nothing."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "load", lambda: cfg)

    worktree.add(hive="mr", bead="ag-epic.3", dry_run=True, as_json=True)

    printed = json.loads(capsys.readouterr().out)
    assert printed == worktree.preview(cfg, "mr", bead="ag-epic.3")
    assert worktree._branch_exists(repo, "wt/bead/issue/ag-epic.3") is False
    assert not Path(printed["path"]).exists()


def test_add_json_reports_created_path_and_branch(tmp_path, monkeypatch, capsys):
    """Real (non-preview) `add(..., as_json=True)` emits the created path/branch for orchestrators
    to parse with the same shape as the preview phase."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    monkeypatch.setattr(config, "load", lambda: cfg)

    worktree.add(hive="mr", bead="ag-epic.3", as_json=True)

    out = capsys.readouterr().out
    result = json.loads(out[out.index("{") :])  # JSON block trails the human progress echo
    assert result["op"] == "add"
    assert result["hive"] == "github/myorg/myrepo"
    assert result["branch"] == "wt/bead/issue/ag-epic.3"
    assert result["created"] is True
    assert Path(result["path"]).exists()
    assert worktree._branch_exists(repo, "wt/bead/issue/ag-epic.3") is True


# ---- _resolve_entry from a worktree cwd (reverse-map the shadow root) --------


def test_resolve_entry_from_worktree_cwd_needs_no_hive(tmp_path, monkeypatch):
    """cwd inside a managed worktree (under the shadow root, NOT under $GIT_WORKSPACE) resolves
    the right hive with no --hive: workspace_identity returns None, so we reverse-map the path."""
    cfg, entry, _ = _ensure_hive(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "ag-epic.3")

    monkeypatch.chdir(target)  # an agent running ws from inside its worktree
    resolved = worktree._resolve_entry(cfg, "")

    assert (resolved["provider"], resolved["org"], resolved["repo"]) == (
        "github",
        "myorg",
        "myrepo",
    )
    assert resolved["prefix"] == "mr"  # the registered entry, not a synthesized stand-in


def test_resolve_entry_errors_outside_any_hive(tmp_path, monkeypatch):
    """cwd outside both $GIT_WORKSPACE and the shadow worktrees root still errors clearly."""
    cfg, _, _ = _ensure_hive(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)

    with pytest.raises(typer.Exit):
        worktree._resolve_entry(cfg, "")


# ---- cwd_identity (side-effect-free triplet + worktree leaf for telemetry) ---


def test_cwd_identity_from_worktree(tmp_path, monkeypatch):
    """Inside a managed worktree, cwd_identity reverse-maps the path to (triplet, leaf) — no
    typer.Exit, no echo (it must be safe to call while building the OTel Resource)."""
    cfg, _, _ = _ensure_hive(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "ag-epic.3")
    monkeypatch.chdir(target)

    triplet, leaf = worktree.cwd_identity(cfg)

    assert triplet == ("github", "myorg", "myrepo")
    assert leaf == "ag-epic-3"  # the sanitized worktree dir name (bead id, '.'→'-')


def test_cwd_identity_none_outside_any_hive(tmp_path, monkeypatch):
    """Outside both the shadow root and $GIT_WORKSPACE, cwd_identity returns (None, '') quietly
    (never raises) so enrichment simply omits the identity attributes."""
    cfg, _, _ = _ensure_hive(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert worktree.cwd_identity(cfg) == (None, "")


# ---- cwd_worktree_dir (side-effect-free worktree-root path for the overlay) --


def test_cwd_worktree_dir_from_nested_cwd(tmp_path, monkeypatch):
    """From anywhere inside (or below) a managed worktree, returns the worktree ROOT dir — the
    overlay's `.bh/otel.env` lives there, not in a nested subdir."""
    cfg, _, _ = _ensure_hive(tmp_path, monkeypatch)
    _, target, _ = worktree.ensure(cfg, "mr", "ag-epic.3")
    nested = target / "src" / "pkg"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)

    assert worktree.cwd_worktree_dir(cfg) == target.resolve()


def test_cwd_worktree_dir_none_outside_shadow_root(tmp_path, monkeypatch):
    cfg, _, _ = _ensure_hive(tmp_path, monkeypatch)
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert worktree.cwd_worktree_dir(cfg) is None


def test_cwd_worktree_dir_none_at_repo_level(tmp_path, monkeypatch):
    """The <root>/<provider>/<org>/<repo> level (no leaf) is not a worktree → None."""
    root = (tmp_path / "wts").resolve()
    monkeypatch.setenv("BH_WORKTREES", str(root))
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
    monkeypatch.setenv("BH_WORKTREES", str(wts_root))

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


def test_unregistered_repo_worktrees_are_surfaced_not_omitted(tmp_path, monkeypatch, capsys):
    """bh-ea1i: a repo with worktrees on disk but NO managed_repos registration must be surfaced
    (in `list` output + a status warning), never silently omitted — the sweep walks the wt root,
    not just the hive list."""
    ws_root = tmp_path / "ws"
    repo = ws_root / "github" / "ghost" / "unregrepo"
    repo.mkdir(parents=True)
    _git("init", "-q", "-b", "main", cwd=repo)
    _git("config", "user.email", "t@example.com", cwd=repo)
    _git("config", "user.name", "t", cwd=repo)
    (repo / "f.txt").write_text("hi")
    _git("add", "f.txt", cwd=repo)
    _git("commit", "-qm", "init", cwd=repo)

    wts_root = tmp_path / "wts"
    monkeypatch.setenv("GIT_WORKSPACE", str(ws_root))
    monkeypatch.setenv("BH_WORKTREES", str(wts_root))

    # A managed-shaped worktree on disk for a repo that is NOT in managed_repos.
    leaf = wts_root / "github" / "ghost" / "unregrepo" / "feat"
    leaf.parent.mkdir(parents=True)
    _git("worktree", "add", "-q", "-b", "feat", str(leaf), cwd=repo)

    cfg = {"managed_repos": []}  # nothing registered

    unreg = worktree.unregistered_worktrees(cfg)
    slugs = [slug for slug, *_ in unreg]
    assert "github/ghost/unregrepo" in slugs
    assert any(str(leaf) == path for _slug, _leaf, path, _br in unreg)
    assert any(br == "feat" for *_h, br in unreg)

    # list_cmd includes the orphan row + warns — never silently omitted.
    monkeypatch.setattr(worktree.config, "load", lambda: cfg)
    worktree.list_cmd()
    captured = capsys.readouterr()
    assert str(leaf) in captured.out
    assert "unregrepo" in captured.err  # the surfaced warning


# ---- empty-dir cleanup ------------------------------------------------------


def test_rmdir_empty_parents_climbs_to_root(tmp_path, monkeypatch):
    root = tmp_path / "wts"
    monkeypatch.setenv("BH_WORKTREES", str(root))
    leaf = root / "github" / "org" / "repo" / "feat"
    leaf.mkdir(parents=True)
    leaf.rmdir()  # simulate git having removed the worktree dir

    worktree._rmdir_empty_parents(leaf, {})

    assert root.exists()  # root itself is never removed
    assert not (root / "github").exists()  # empty triplet dirs climbed away


def test_rmdir_empty_parents_stops_at_nonempty(tmp_path, monkeypatch):
    root = tmp_path / "wts"
    monkeypatch.setenv("BH_WORKTREES", str(root))
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
    monkeypatch.setenv("BH_WORKTREES", str(root))
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


def _shared_base_hive(tmp_path, monkeypatch, initial):
    """An _ensure_hive with a shared `s.txt` (content=`initial`) committed on main, so worktrees
    forked off it diverge on the SAME file."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
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
    cfg, entry, repo = _shared_base_hive(tmp_path, monkeypatch, "L0\n")
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
    cfg, entry, repo = _shared_base_hive(tmp_path, monkeypatch, "L0\n")
    _, t1, b1 = worktree.ensure(cfg, "mr", "x-1")
    (t1 / "only.txt").write_text("solo\n")  # touches a different file → no conflict
    _git("add", "-A", cwd=t1)
    _git("commit", "-qm", "feat: solo", cwd=t1)

    rc, _out, how = worktree.try_merge_rebase(entry, b1, "main", t1)
    assert rc == 0 and how == "clean"


def test_try_merge_rebase_restores_branch_on_real_conflict(tmp_path, monkeypatch):
    """Two bead branches edit the SAME line divergently — unresolvable. try_merge_rebase fails
    (how='conflict'), main is untouched, and the bead branch is reset to its pre-rebase tip."""
    cfg, entry, repo = _shared_base_hive(tmp_path, monkeypatch, "base\n")
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
    cfg, entry, repo = _shared_base_hive(tmp_path, monkeypatch, "L0\n")
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
    cfg, entry, repo = _shared_base_hive(tmp_path, monkeypatch, "L0\n")
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
    cfg, entry, repo = _shared_base_hive(tmp_path, monkeypatch, "base\n")
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
    cfg, entry, repo = _shared_base_hive(tmp_path, monkeypatch, "base\n")
    _, t1, b1 = worktree.ensure(cfg, "mr", "ue-1")
    _, t2, b2 = worktree.ensure(cfg, "mr", "ue-2")
    _set_line(t1, "X\n")
    _set_line(t2, "Y\n")

    assert worktree.merge_no_ff(entry, b1, "main")[0] == 0
    rc, _out, how = worktree.try_merge_rebase(entry, b2, "main", t2, union_globs=())

    assert rc != 0 and how == "conflict"


# ---- provision_observaloop (worktree-create hook) ---------------------------
#
# The per-hive profile provisioning + .bh/otel.env overlay that _do_add runs AFTER run_init on a
# true worktree create. Observaloop is faked throughout. Covers: enabled (ensure+up+overlay),
# disabled-and-import-free (default path touches no observaloop module), failure-still-succeeds
# (any exception warns, never raises), and verify- skip (ephemeral clean-checkout worktrees).

_OBS_HIVE = {"provider": "github", "org": "myorg", "repo": "myrepo", "prefix": "mr"}
_OBS_ENABLED_CFG = {
    "otel": {"enabled": True},
    "observaloop": {"enabled": True},
    "managed_repos": [_OBS_HIVE],
}


def test_provision_observaloop_enabled_ensures_profile_and_writes_overlay(tmp_path, monkeypatch):
    """Enabled → ensure_profile + up (idempotent) then write <worktree>/.bh/otel.env at the
    resolved endpoint, so a ws invocation there exports to the hive profile."""
    from beadhive import observaloop

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
    worktree.provision_observaloop(_OBS_ENABLED_CFG, _OBS_HIVE, target)

    assert calls["ensure"] == ["mr"] and calls["up"] == ["mr"]  # profile ensured + up
    env_file = target / ".bh" / "otel.env"
    assert env_file.is_file()
    body = env_file.read_text()
    assert "OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318" in body
    assert "BH_OBSERVALOOP_PROFILE=mr" in body


def test_provision_observaloop_disabled_is_import_free_and_writes_nothing(tmp_path, monkeypatch):
    """Default/off path: no overlay written AND ws.observaloop is never imported (cheap path)."""
    sys.modules.pop("ws.observaloop", None)
    sys.modules.pop("ws.observaloop_env", None)

    target = tmp_path / "wt"
    target.mkdir()
    worktree.provision_observaloop({"otel": {"enabled": False}}, _OBS_HIVE, target)

    assert not (target / ".bh").exists()  # nothing provisioned
    assert "ws.observaloop" not in sys.modules  # default path imports no observaloop seam


def test_provision_observaloop_failure_warns_and_does_not_raise(tmp_path, monkeypatch):
    """Observaloop/docker failure (any exception) warns and returns — NEVER blocks creation."""
    from beadhive import observaloop

    def _boom(*a, **k):
        raise RuntimeError("docker down")

    monkeypatch.setattr(observaloop, "ensure_profile", _boom)

    target = tmp_path / "wt"
    target.mkdir()
    worktree.provision_observaloop(_OBS_ENABLED_CFG, _OBS_HIVE, target)  # must not raise

    assert not (target / ".bh").exists()  # overlay not written, but creation survives


def test_provision_observaloop_no_endpoint_skips_overlay(tmp_path, monkeypatch):
    """Unavailable / down → endpoint_for returns None → overlay is skipped (warn-and-continue)."""
    from beadhive import observaloop

    monkeypatch.setattr(observaloop, "ensure_profile", lambda name, cfg=None: None)
    monkeypatch.setattr(observaloop, "up", lambda name, cfg=None: None)
    monkeypatch.setattr(observaloop, "endpoint_for", lambda name, proto, cfg=None: None)

    target = tmp_path / "wt"
    target.mkdir()
    worktree.provision_observaloop(_OBS_ENABLED_CFG, _OBS_HIVE, target)  # must not raise

    assert not (target / ".bh").exists()


def test_provision_observaloop_skips_verify_leaf(tmp_path, monkeypatch):
    """A verify- leaf (ephemeral clean-checkout) is defensively skipped even when enabled."""
    from beadhive import observaloop

    called = []
    monkeypatch.setattr(observaloop, "ensure_profile", lambda name, cfg=None: called.append(name))

    target = tmp_path / f"{worktree.VERIFY_LEAF_PREFIX}ag-epic-3"
    target.mkdir()
    worktree.provision_observaloop(_OBS_ENABLED_CFG, _OBS_HIVE, target)

    assert called == []  # never provisioned
    assert not (target / ".bh").exists()


# ---- clean_checkout: telemetry-neutral validation env -----


def test_clean_checkout_validation_env_is_telemetry_neutral(tmp_path, monkeypatch):
    """The clean-checkout validation child runs with telemetry scrubbed: no OTEL_* /
    BH_OBSERVALOOP_PROFILE leak from the parent (so submit's result can't depend on the operator's
    otel config), OTEL_SDK_DISABLED forced on, and non-telemetry env (PATH) preserved — the bug
    surfaced in where submit's validation inherited the worktree overlay
    endpoint."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "ws.hive=mr")
    monkeypatch.setenv("BH_OBSERVALOOP_PROFILE", "dev")
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
    assert "BH_OBSERVALOOP_PROFILE" not in env
    assert env["OTEL_SDK_DISABLED"] == "true"
    assert env["PATH"] == "/sentinel/bin"  # non-telemetry env preserved


# ---- clean_checkout: per-invocation verify dirs + liveness sweep (bh-nikb) ---


class _Done:
    returncode = 0


def test_clean_checkout_unique_per_invocation_dirs_and_marker(tmp_path, monkeypatch):
    """Two clean_checkouts of the SAME branch use DISTINCT verify-<leaf>-<rand6> dirs — no shared
    deterministic path, so concurrent validations can't collide. The verify- prefix is preserved
    (ephemeral classification keeps working), a liveness marker is present during validation, and
    the finally-cleanup removes only this invocation's own dir."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)

    calls = []
    seen_markers = []

    def _fake_run(cmd, **kw):
        calls.append(list(cmd))
        if list(cmd)[:1] != ["git"]:  # the validation spawn: marker must already be in place
            marker = Path(kw["cwd"]) / worktree.VERIFY_MARKER
            seen_markers.append(json.loads(marker.read_text()) if marker.exists() else None)
        return _Done()

    monkeypatch.setattr(worktree, "run", _fake_run)

    assert worktree.clean_checkout(entry, "main", "just check") == 0
    assert worktree.clean_checkout(entry, "main", "just check") == 0

    adds = [c for c in calls if c[3:5] == ["worktree", "add"]]
    removes = [c for c in calls if c[3:5] == ["worktree", "remove"]]
    add_paths = [c[-2] for c in adds]
    assert len(add_paths) == 2
    assert len(set(add_paths)) == 2  # per-invocation isolation: never the same dir
    for p in add_paths:
        leaf = Path(p).name
        assert leaf.startswith(f"{worktree.VERIFY_LEAF_PREFIX}main-")  # prefix + human leaf kept
        assert len(leaf.rsplit("-", 1)[-1]) == 6  # -<rand6> isolation suffix
    # teardown removed exactly this invocation's own dirs — nothing else
    assert [c[-1] for c in removes] == add_paths

    # the liveness marker (HolderToken analog) was live during each validation run
    assert len(seen_markers) == 2
    for m in seen_markers:
        assert m is not None
        assert m["pid"] == os.getpid()
        assert m["branch"] == "main"
        assert m["command"] == "just check"
        assert set(m) >= {"host", "pid", "pid_start", "created_at", "branch", "command"}


def test_clean_checkout_spares_a_live_sibling(tmp_path, monkeypatch):
    """A LIVE sibling verify dir (another in-flight validation of the same branch) survives a
    concurrent clean_checkout untouched: no entry-time pre-clean, and the sweep spares a marker
    whose pid is alive with a matching start-time."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    sibling = worktree.wt_dir(entry, "verify-main-live01")
    sibling.mkdir(parents=True)
    worktree._write_verify_marker(sibling, "main", "just check")  # our own live pid

    calls = []

    def _fake_run(cmd, **kw):
        calls.append(list(cmd))
        return _Done()

    monkeypatch.setattr(worktree, "run", _fake_run)

    assert worktree.clean_checkout(entry, "main", "just check") == 0
    assert sibling.exists()  # never pre-cleaned, never swept
    removes = [c for c in calls if c[3:5] == ["worktree", "remove"]]
    assert all(c[-1] != str(sibling) for c in removes)


def test_sweep_verify_dirs_reaps_orphans_and_spares_live(tmp_path, monkeypatch):
    """The global sweep reaps demonstrably-dead verify dirs (dead pid, recycled pid via start-time
    mismatch, unmarked past grace, older than the hard TTL) and spares live/fresh/non-verify
    siblings."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    monkeypatch.setattr(worktree, "_pid_alive", lambda pid: pid == os.getpid())

    def _mk(leaf, pid=None, pid_start=None, age=0):
        d = worktree.wt_dir(entry, leaf)
        d.mkdir(parents=True)
        if pid is not None:
            worktree._write_verify_marker(d, "b1", "just check")
            marker = d / worktree.VERIFY_MARKER
            m = json.loads(marker.read_text())
            m["pid"] = pid
            if pid_start is not None:
                m["pid_start"] = pid_start
            marker.write_text(json.dumps(m))
        if age:
            old = time.time() - age
            os.utime(d, (old, old))
        return d

    live = _mk("verify-b1-live00", pid=os.getpid())
    dead = _mk("verify-b1-dead00", pid=424242)  # _pid_alive → False
    recycled = _mk("verify-b1-recy00", pid=os.getpid(), pid_start="not the real start")
    unmarked_fresh = _mk("verify-b1-fresh0")
    unmarked_old = _mk("verify-b1-old000", age=10 * 60)  # past the 5-min grace
    expired = _mk("verify-b1-ttl000", pid=os.getpid(), age=25 * 60 * 60)  # past the 24h TTL
    bystander = _mk("some-bead")  # not verify- — never examined

    reaped = worktree.sweep_verify_dirs(entry)
    assert reaped == 4
    assert live.exists() and unmarked_fresh.exists() and bystander.exists()
    assert not dead.exists()
    assert not recycled.exists()
    assert not unmarked_old.exists()
    assert not expired.exists()


# ---- clean_checkout: verify-flagged init rules + bare-checkout hint (bh-7k1p) ----


def test_clean_checkout_runs_verify_flagged_init_rules(tmp_path, monkeypatch):
    """clean_checkout applies verify-flagged init rules in the bare checkout BEFORE the
    validation command — so validate_cmd sees a 'provisioned enough to validate' tree — and
    skips unflagged rules (heavy seat provisioning must not run per validation). Real git,
    real subprocesses: the validation command itself asserts the markers from inside the
    verify dir."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    cfg = {**cfg, **_VERIFY_RULES}
    rc = worktree.clean_checkout(
        entry,
        "main",
        "sh -c 'test -f flagged.marker && test ! -f unflagged.marker'",
        cfg=cfg,
    )
    assert rc == 0


def test_clean_checkout_hint_on_validation_failure(tmp_path, monkeypatch, capsys):
    """A nonzero validate_cmd in the verify checkout appends the bare-checkout diagnostic hint
    to stderr — centrally, so every caller (submit / merge / batch / review) inherits it."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    rc = worktree.clean_checkout(entry, "main", "false", cfg=cfg)
    assert rc != 0
    err = capsys.readouterr().err
    assert "bare clean checkout" in err
    assert "verify: true" in err
    assert "docs/WORKTREES.md" in err


def test_clean_checkout_no_hint_on_success(tmp_path, monkeypatch, capsys):
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    assert worktree.clean_checkout(entry, "main", "true", cfg=cfg) == 0
    assert "bare clean checkout" not in capsys.readouterr().err


def test_clean_checkout_real_git_leaves_no_verify_dirs(tmp_path, monkeypatch):
    """End-to-end with real git: validation runs in a real detached checkout and the invocation's
    verify dir (plus its marker) is gone afterwards."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)

    rc = worktree.clean_checkout(entry, "main", "true")
    assert rc == 0
    parent = worktree.wt_dir(entry, "x").parent
    assert not list(parent.glob(f"{worktree.VERIFY_LEAF_PREFIX}*"))


# ---- clean_checkout: validation verdict ledger (bh-dfx0) ---------------------
#
# Verdicts are keyed by (commit sha, cmd hash) in <hive>/.git/bh-validation-ledger.json —
# repo-local untracked state. Only reuse=True callers (submit; review --no-fresh) consult it;
# only a fresh GREEN verdict short-circuits. The logging command makes "did the validation
# actually run" directly observable.


def _log_cmd(tmp_path, rc=0, name="runs.log"):
    """A validation command that appends a line to a log file — observable run count."""
    log = tmp_path / name
    return log, f"sh -c 'echo ran >> {log}; exit {rc}'"


def _run_count(log):
    return len(log.read_text().splitlines()) if log.exists() else 0


def test_clean_checkout_reuses_green_verdict(tmp_path, monkeypatch, capsys):
    """reuse=True with a fresh green verdict for the exact (sha, cmd) skips the checkout and the
    command entirely: rc 0, no second run, and the reuse line names the sha + recording time."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    log, cmd = _log_cmd(tmp_path)

    assert worktree.clean_checkout(entry, "main", cmd, cfg=cfg) == 0  # records the green verdict
    assert _run_count(log) == 1
    assert worktree.clean_checkout(entry, "main", cmd, cfg=cfg, reuse=True) == 0
    assert _run_count(log) == 1  # never re-ran — the whole checkout was skipped

    out = capsys.readouterr().out
    assert "validation verdict reused" in out
    assert worktree._branch_sha(entry, "main")[:7] in out
    # the ledger is repo-local untracked state inside the hive's .git dir
    assert (repo / ".git" / validation_ledger.LEDGER_FILENAME).is_file()


def test_clean_checkout_reuse_hit_counts_telemetry(tmp_path, monkeypatch):
    """A reuse hit increments the dedicated bh.work.validation.reused counter (tagged with the
    hive) — the series that keeps runs/duration interpretable once reuse is common."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    log, cmd = _log_cmd(tmp_path)
    calls = []
    monkeypatch.setattr(
        worktree.otel, "count_validation_reuse", lambda attrs=None: calls.append(attrs)
    )

    assert worktree.clean_checkout(entry, "main", cmd, cfg=cfg) == 0  # real run — no reuse count
    assert calls == []
    assert worktree.clean_checkout(entry, "main", cmd, cfg=cfg, reuse=True) == 0
    assert calls == [{"bh.hive": str(entry.get("prefix", ""))}]


def test_clean_checkout_records_validated_head_not_stale_sha(tmp_path, monkeypatch):
    """The recorded verdict keys on the verify checkout's OWN HEAD, not the pre-resolved branch
    sha — so a branch moving between lookup and checkout (TOCTOU) can never make a verdict vouch
    for content it didn't see."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    log, cmd = _log_cmd(tmp_path)
    stale = "d" * 40
    monkeypatch.setattr(worktree, "_branch_sha", lambda entry, branch: stale)

    assert worktree.clean_checkout(entry, "main", cmd, cfg=cfg) == 0
    entries = json.loads((repo / ".git" / validation_ledger.LEDGER_FILENAME).read_text())
    real_head = run(
        ["git", "-C", str(repo), "rev-parse", "main"], check=False, capture=True
    ).stdout.strip()
    assert [e["sha"] for e in entries] == [real_head]  # the validated tree, never the stale key


def test_clean_checkout_default_never_consults_ledger(tmp_path, monkeypatch):
    """The default (reuse=False) always runs fresh even when a green verdict exists — this is the
    landing-boundary contract: merge / postland / finish / batch callers all use the default, so
    the gate at landing never believes the ledger."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    log, cmd = _log_cmd(tmp_path)

    assert worktree.clean_checkout(entry, "main", cmd, cfg=cfg) == 0
    assert worktree.clean_checkout(entry, "main", cmd, cfg=cfg) == 0  # default → fresh run
    assert _run_count(log) == 2


def test_clean_checkout_red_verdict_not_reused(tmp_path, monkeypatch):
    """A recorded RED verdict is never reused: reuse=True revalidates (a failure must always be
    re-demonstrated, never served from cache)."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    log, cmd = _log_cmd(tmp_path, rc=1)

    assert worktree.clean_checkout(entry, "main", cmd, cfg=cfg) == 1  # records the red verdict
    assert worktree.clean_checkout(entry, "main", cmd, cfg=cfg, reuse=True) == 1
    assert _run_count(log) == 2  # revalidated — red is recorded but never trusted


def test_clean_checkout_stale_verdict_revalidates(tmp_path, monkeypatch):
    """A green verdict older than the TTL is stale — reuse=True revalidates."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    log, cmd = _log_cmd(tmp_path)
    assert worktree.clean_checkout(entry, "main", cmd, cfg=cfg) == 0

    ledger = repo / ".git" / validation_ledger.LEDGER_FILENAME
    entries = json.loads(ledger.read_text())
    for e in entries:
        e["at"] = time.time() - validation_ledger.LEDGER_TTL_SECONDS - 60
    ledger.write_text(json.dumps(entries))

    assert worktree.clean_checkout(entry, "main", cmd, cfg=cfg, reuse=True) == 0
    assert _run_count(log) == 2  # expired → fresh run


def test_clean_checkout_cmd_change_revalidates(tmp_path, monkeypatch):
    """The cmd hash is half the key: the same sha with a DIFFERENT validation command never
    reuses (minimal env-drift coverage — a changed command means a changed contract)."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    log_a, cmd_a = _log_cmd(tmp_path, name="a.log")
    log_b, cmd_b = _log_cmd(tmp_path, name="b.log")

    assert worktree.clean_checkout(entry, "main", cmd_a, cfg=cfg) == 0  # green for (sha, cmd_a)
    assert worktree.clean_checkout(entry, "main", cmd_b, cfg=cfg, reuse=True) == 0
    assert _run_count(log_b) == 1  # cmd_b has no verdict — it ran fresh


def test_validation_ledger_roundtrip_and_corruption(tmp_path, monkeypatch):
    """Ledger unit contract: exact-key green hit; miss on other sha / other cmd; a red verdict
    replaces a green one for the same key; a corrupt file reads as empty and heals on the next
    record (best-effort — the ledger can never fail a validation)."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)

    validation_ledger.record(entry, "abc123", "just check", 0)
    hit = validation_ledger.green_verdict(entry, "abc123", "just check")
    assert hit is not None and hit["rc"] == 0 and hit["host"]
    assert validation_ledger.green_verdict(entry, "abc123", "other cmd") is None
    assert validation_ledger.green_verdict(entry, "zzz999", "just check") is None

    validation_ledger.record(entry, "abc123", "just check", 1)  # red replaces the green entry
    assert validation_ledger.green_verdict(entry, "abc123", "just check") is None

    ledger = repo / ".git" / validation_ledger.LEDGER_FILENAME
    ledger.write_text("not json {")
    assert validation_ledger.green_verdict(entry, "abc123", "just check") is None  # no raise
    validation_ledger.record(entry, "def456", "just check", 0)  # heals the corrupt file
    assert validation_ledger.green_verdict(entry, "def456", "just check") is not None


# ---- worktree delegation seam: _consult_wt_create / _consult_wt_remove ------
#
# The generic seam _do_add/remove/prune wire into: the first ENABLED plugin defining the hook
# wins; None/False => not handled => native. A typer.Exit raised by a hook is the plugin's own
# hard-fail policy and PROPAGATES; any other exception warns (stderr) and falls through, mirroring
# retire.py's plugin-notify fence.


def _fake_plugin(name, *, enabled=True, wt_create=None, wt_remove=None):
    return plugins.Plugin(
        name=name,
        cli=typer.Typer(),
        enabled=lambda cfg, entry: enabled,
        wt_create=wt_create,
        wt_remove=wt_remove,
    )


def test_consult_wt_create_none_when_no_plugin_defines_hook(monkeypatch):
    monkeypatch.setattr(plugins, "registry", lambda: [_fake_plugin("noop")])
    result = worktree._consult_wt_create(
        {}, {}, main=Path("/main"), branch="b", target=Path("/t"), start_point=""
    )
    assert result is None


def test_consult_wt_create_hook_wins_over_native(monkeypatch):
    created = Path("/created")
    plugin = _fake_plugin("p", wt_create=lambda cfg, entry, **kw: created)
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])
    result = worktree._consult_wt_create(
        {}, {}, main=Path("/main"), branch="b", target=Path("/t"), start_point=""
    )
    assert result == created


def test_consult_wt_create_skips_disabled_plugin(monkeypatch):
    plugin = _fake_plugin("p", enabled=False, wt_create=lambda cfg, entry, **kw: Path("/x"))
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])
    result = worktree._consult_wt_create(
        {}, {}, main=Path("/main"), branch="b", target=Path("/t"), start_point=""
    )
    assert result is None


def test_consult_wt_create_first_enabled_plugin_with_hook_wins(monkeypatch):
    no_hook = _fake_plugin("no-hook")  # enabled but defines nothing → skipped
    first = _fake_plugin("first", wt_create=lambda cfg, entry, **kw: Path("/first"))
    second = _fake_plugin("second", wt_create=lambda cfg, entry, **kw: Path("/second"))
    monkeypatch.setattr(plugins, "registry", lambda: [no_hook, first, second])
    result = worktree._consult_wt_create(
        {}, {}, main=Path("/main"), branch="b", target=Path("/t"), start_point=""
    )
    assert result == Path("/first")


def test_consult_wt_create_typer_exit_propagates(monkeypatch):
    def boom(cfg, entry, **kw):
        raise typer.Exit(3)

    monkeypatch.setattr(plugins, "registry", lambda: [_fake_plugin("boom", wt_create=boom)])
    with pytest.raises(typer.Exit) as exc:
        worktree._consult_wt_create(
            {}, {}, main=Path("/main"), branch="b", target=Path("/t"), start_point=""
        )
    assert exc.value.exit_code == 3


def test_consult_wt_create_other_exception_warns_and_falls_through(monkeypatch, capsys):
    def boom(cfg, entry, **kw):
        raise RuntimeError("kaboom")

    ok = _fake_plugin("ok", wt_create=lambda cfg, entry, **kw: Path("/ok"))
    monkeypatch.setattr(plugins, "registry", lambda: [_fake_plugin("boom", wt_create=boom), ok])
    result = worktree._consult_wt_create(
        {}, {}, main=Path("/main"), branch="b", target=Path("/t"), start_point=""
    )
    assert result == Path("/ok")  # fell through to the next plugin
    assert "boom" in capsys.readouterr().err


def test_consult_wt_remove_false_when_no_plugin_defines_hook(monkeypatch):
    monkeypatch.setattr(plugins, "registry", lambda: [_fake_plugin("noop")])
    result = worktree._consult_wt_remove(
        {}, {}, main=Path("/main"), target=Path("/t"), force=True, keep_branch=True
    )
    assert result is False


def test_consult_wt_remove_hook_wins_when_true(monkeypatch):
    plugin = _fake_plugin("p", wt_remove=lambda cfg, entry, **kw: True)
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])
    result = worktree._consult_wt_remove(
        {}, {}, main=Path("/main"), target=Path("/t"), force=True, keep_branch=True
    )
    assert result is True


def test_consult_wt_remove_false_falls_through_to_next_plugin(monkeypatch):
    first = _fake_plugin("first", wt_remove=lambda cfg, entry, **kw: False)
    second = _fake_plugin("second", wt_remove=lambda cfg, entry, **kw: True)
    monkeypatch.setattr(plugins, "registry", lambda: [first, second])
    result = worktree._consult_wt_remove(
        {}, {}, main=Path("/main"), target=Path("/t"), force=True, keep_branch=False
    )
    assert result is True


def test_consult_wt_remove_typer_exit_propagates(monkeypatch):
    def boom(cfg, entry, **kw):
        raise typer.Exit(4)

    monkeypatch.setattr(plugins, "registry", lambda: [_fake_plugin("boom", wt_remove=boom)])
    with pytest.raises(typer.Exit) as exc:
        worktree._consult_wt_remove(
            {}, {}, main=Path("/main"), target=Path("/t"), force=True, keep_branch=False
        )
    assert exc.value.exit_code == 4


def test_consult_wt_remove_other_exception_warns_and_falls_through(monkeypatch, capsys):
    def boom(cfg, entry, **kw):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(plugins, "registry", lambda: [_fake_plugin("boom", wt_remove=boom)])
    result = worktree._consult_wt_remove(
        {}, {}, main=Path("/main"), target=Path("/t"), force=True, keep_branch=False
    )
    assert result is False  # no other plugin picked it up → native fallback
    assert "boom" in capsys.readouterr().err


# ---- delegation wiring: _do_add (new-branch create only; attach stays native) -----------------


def test_do_add_new_branch_delegates_to_plugin_hook(tmp_path, monkeypatch):
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    target = worktree.wt_dir(entry, "deleg-1")
    branch = "wt/bead/issue/deleg-1"

    native_add_calls = []
    real_run_git = worktree._run_git

    def spy(args, **kw):
        if "worktree" in args and "add" in args:
            native_add_calls.append(args)
        return real_run_git(args, **kw)

    monkeypatch.setattr(worktree, "_run_git", spy)

    def fake_wt_create(cfg, entry, *, main, branch, target, start_point):
        # Simulate an external tool creating the worktree directly — a real delegate (e.g. orca)
        # shells out on its own, bypassing bh's _run_git entirely.
        target.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "-C", str(main), "worktree", "add", "-b", branch, str(target)], check=True)
        return target

    plugin = _fake_plugin("fake", wt_create=fake_wt_create)
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])

    worktree._do_add(cfg, entry, repo, branch, target, new_branch=True)

    assert target.exists()
    assert worktree._branch_exists(repo, branch)
    assert native_add_calls == []  # the native git worktree add subprocess never ran


def test_do_add_new_branch_falls_through_to_native_when_hook_returns_none(tmp_path, monkeypatch):
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    target = worktree.wt_dir(entry, "native-1")
    branch = "wt/bead/issue/native-1"

    plugin = _fake_plugin("noop", wt_create=lambda cfg, entry, **kw: None)
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])

    worktree._do_add(cfg, entry, repo, branch, target, new_branch=True)

    assert target.exists()
    assert worktree._branch_exists(repo, branch)


def test_do_add_attach_never_delegates_and_warns_when_plugin_enabled(tmp_path, monkeypatch, capsys):
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    branch = "wt/bead/issue/attach-1"
    _git("branch", branch, cwd=repo)  # existing branch to attach; dir doesn't exist yet
    target = worktree.wt_dir(entry, "attach-1")

    calls = []

    def hook(cfg, entry, **kw):
        calls.append(kw)
        return Path("/should-not-be-used")

    plugin = _fake_plugin("fake", wt_create=hook)
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])

    worktree._do_add(cfg, entry, repo, branch, target, new_branch=False)

    assert calls == []  # attach never calls the hook, even though a delegating plugin is enabled
    assert target.exists()
    assert "attach stays native" in capsys.readouterr().err


def test_do_add_typer_exit_from_hook_propagates(tmp_path, monkeypatch):
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    target = worktree.wt_dir(entry, "exit-1")
    branch = "wt/bead/issue/exit-1"

    def boom(cfg, entry, **kw):
        raise typer.Exit(7)

    plugin = _fake_plugin("boom", wt_create=boom)
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])

    with pytest.raises(typer.Exit) as exc:
        worktree._do_add(cfg, entry, repo, branch, target, new_branch=True)
    assert exc.value.exit_code == 7
    assert not target.exists()  # native create never ran either
    assert not worktree._branch_exists(repo, branch)


def test_do_add_other_exception_from_hook_falls_through_to_native(tmp_path, monkeypatch, capsys):
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    target = worktree.wt_dir(entry, "fallback-1")
    branch = "wt/bead/issue/fallback-1"

    def boom(cfg, entry, **kw):
        raise RuntimeError("plugin exploded")

    plugin = _fake_plugin("boom", wt_create=boom)
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])

    worktree._do_add(cfg, entry, repo, branch, target, new_branch=True)

    assert target.exists()  # fell through to native create
    assert worktree._branch_exists(repo, branch)
    assert "plugin exploded" in capsys.readouterr().err


# ---- end-to-end: ensure on the REAL orca plugin -----------------------


def test_ensure_delegated_to_real_orca_plugin_still_runs_run_init(tmp_path, monkeypatch):
    """`ensure()` -> `_do_add` -> `_consult_wt_create` -> the real `orca.create_worktree` (not a
    fake plugin stand-in): only the `orca worktree create --json` subprocess is faked (there's no
    live orca runtime in CI); it shells out to a REAL `git worktree add` under the hood, so the
    git-level fixup (rename the sanitized leaf branch to bh's `wt/...` branch) runs for real too.
    Proves run_init still fires on the delegated path — the whole point of `_do_add` running it
    unconditionally after either branch of the create."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    cfg["git_workspace"] = {"enabled": True}
    cfg["orca"] = {"enabled": True, "worktrees": {"enabled": True}}
    cfg["worktrees"] = {"init": [{"run": "touch delegated.marker"}]}
    monkeypatch.setattr(plugins, "registry", lambda: [orca.PLUGIN])

    bead = "deleg-1"
    branch = f"wt/bead/issue/{bead}"
    leaf = bead
    target = worktree.wt_dir(entry, leaf)

    real_run = orca.run.run

    def fake_run(cmd, **kw):
        if cmd[0] == "orca":
            assert cmd[1:3] == ["worktree", "create"], f"unexpected orca call: {cmd}"
            real_run(
                ["git", "-C", str(repo), "worktree", "add", "-b", leaf, str(target)], check=True
            )
            payload = json.dumps({"ok": True, "result": {"worktree": {"path": str(target)}}})
            return SimpleNamespace(returncode=0, stdout=payload)
        return real_run(cmd, **kw)  # real git — actually exercises the branch-rename fixup

    monkeypatch.setattr(orca.run, "run", fake_run)

    result_entry, result_target, result_branch = worktree.ensure(cfg, "mr", bead=bead)

    assert result_target == target
    assert result_branch == branch
    assert target.exists()
    assert worktree._branch_exists(repo, branch)  # the fixup renamed leaf -> the bh branch
    assert not worktree._branch_exists(repo, leaf)  # sanitized leaf branch no longer exists
    assert (target / "delegated.marker").exists()  # run_init ran on the delegated path


# ---- delegation wiring: remove() (keep_branch=True — the branch is durable) -------------------


def _add_real_worktree(repo, entry, leaf, branch):
    target = worktree.wt_dir(entry, leaf)
    target.parent.mkdir(parents=True, exist_ok=True)
    _git("worktree", "add", "-b", branch, str(target), cwd=repo)
    return target


def test_remove_delegates_with_keep_branch_true(tmp_path, monkeypatch):
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    branch = "wt/bead/issue/rm-1"
    target = _add_real_worktree(repo, entry, "rm-1", branch)
    monkeypatch.setattr(config, "load", lambda: cfg)

    calls = []

    def hook(cfg, entry, **kw):
        calls.append(kw)
        run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(kw["target"])],
            check=True,
        )
        return True

    plugin = _fake_plugin("fake", wt_remove=hook)
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])

    worktree.remove("mr", "rm-1")

    assert not target.exists()
    assert calls[0]["keep_branch"] is True
    assert worktree._branch_exists(repo, branch)  # keep_branch semantics honored by the hook


def test_remove_json_reports_op_hive_path_removed(tmp_path, monkeypatch, capsys):
    """`remove(..., as_json=True)` (bh-73rz.4): the machine-readable completion an external
    orchestrator's preview→create→…→remove flow parses, mirroring `add --json`'s shape."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    branch = "wt/bead/issue/rm-json"
    target = _add_real_worktree(repo, entry, "rm-json", branch)
    monkeypatch.setattr(config, "load", lambda: cfg)

    worktree.remove("mr", "rm-json", as_json=True)

    printed = json.loads(capsys.readouterr().out)
    assert printed == {
        "op": "rm",
        "hive": "github/myorg/myrepo",
        "path": str(target),
        "removed": True,
    }
    assert not target.exists()


def test_remove_falls_through_to_native_when_hook_returns_false(tmp_path, monkeypatch):
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    branch = "wt/bead/issue/rm-2"
    target = _add_real_worktree(repo, entry, "rm-2", branch)
    monkeypatch.setattr(config, "load", lambda: cfg)

    plugin = _fake_plugin("fake", wt_remove=lambda cfg, entry, **kw: False)
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])

    worktree.remove("mr", "rm-2")

    assert not target.exists()  # native remove ran


def test_remove_never_runs_native_after_successful_delegated_removal(tmp_path, monkeypatch):
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    branch = "wt/bead/issue/rm-3"
    target = _add_real_worktree(repo, entry, "rm-3", branch)
    monkeypatch.setattr(config, "load", lambda: cfg)

    native_remove_calls = []
    real_run_git = worktree._run_git

    def spy(args, **kw):
        if "worktree" in args and "remove" in args:
            native_remove_calls.append(args)
        return real_run_git(args, **kw)

    monkeypatch.setattr(worktree, "_run_git", spy)

    def hook(cfg, entry, **kw):
        # Simulate an external tool removing the worktree directly (bypassing bh's _run_git).
        run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(kw["target"])],
            check=True,
        )
        return True

    plugin = _fake_plugin("fake", wt_remove=hook)
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])

    worktree.remove("mr", "rm-3")

    assert not target.exists()
    assert native_remove_calls == []  # the native git worktree remove subprocess never ran


# ---- delegation wiring + native/delegated parity: prune() (keep_branch=False) -----------------


def _prune_hive(tmp_path, monkeypatch):
    """Real hive + one real worktree pre-classified SAFE (bypasses bd via a faked classifier —
    prune's own classification logic is covered elsewhere; this seam only cares what happens
    once a row is SAFE)."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    branch = "wt/bead/issue/safe-1"
    target = _add_real_worktree(repo, entry, "safe-1", branch)
    monkeypatch.setattr(config, "load", lambda: cfg)

    st = wt_status.WtStatus(
        hive="mr",
        leaf="safe-1",
        branch=branch,
        path=str(target),
        bead_id="safe-1",
        classification=wt_status.WtClassification.SAFE,
        merged=True,
        dirty=False,
        safe=True,
    )
    monkeypatch.setattr(worktree, "managed", lambda cfg: [("mr", str(target), branch)])
    monkeypatch.setattr(worktree, "_classify_entry", lambda entry, rows, cfg: [st])
    return entry, repo, target, branch


def test_prune_delegates_with_keep_branch_false(tmp_path, monkeypatch):
    entry, repo, target, branch = _prune_hive(tmp_path, monkeypatch)

    calls = []

    def hook(cfg, entry, **kw):
        calls.append(kw)
        run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(kw["target"])],
            check=True,
        )
        return True

    plugin = _fake_plugin("fake", wt_remove=hook)
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])

    worktree.prune(hive="mr")

    assert not target.exists()
    assert calls[0]["keep_branch"] is False
    assert calls[0]["force"] is True


def test_prune_wires_through_real_orca_plugin_keep_branch_false(tmp_path, monkeypatch):
    """End-to-end wiring (the real orca.PLUGIN, not a fake): prune()'s SAFE removal flows
    through the generic seam into orca.remove_worktree(), which skips the keep_branch=True
    detach and drives 'orca worktree rm' (its subprocess is faked; the fake performs the same
    real-git-removal side effect orca's own rm would)."""
    import json
    from types import SimpleNamespace

    from beadhive import orca

    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    cfg["git_workspace"] = {"enabled": True}
    cfg["orca"] = {"enabled": True, "worktrees": {"enabled": True, "fallback": False}}
    branch = "wt/bead/issue/orca-safe-1"
    target = _add_real_worktree(repo, entry, "orca-safe-1", branch)
    monkeypatch.setattr(config, "load", lambda: cfg)

    st = wt_status.WtStatus(
        hive="mr",
        leaf="orca-safe-1",
        branch=branch,
        path=str(target),
        bead_id="orca-safe-1",
        classification=wt_status.WtClassification.SAFE,
        merged=True,
        dirty=False,
        safe=True,
    )
    monkeypatch.setattr(worktree, "managed", lambda cfg: [("mr", str(target), branch)])
    monkeypatch.setattr(worktree, "_classify_entry", lambda entry, rows, cfg: [st])
    monkeypatch.setattr(plugins, "registry", lambda: [orca.PLUGIN])

    calls: list[list[str]] = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        if cmd[:3] == ["orca", "worktree", "rm"]:
            run(["git", "-C", str(repo), "worktree", "remove", "--force", str(target)], check=True)
            return SimpleNamespace(
                returncode=0, stdout=json.dumps({"ok": True, "result": {"removed": True}})
            )
        raise AssertionError(f"unexpected orca subprocess call: {cmd}")

    monkeypatch.setattr(orca.run, "run", fake_run)

    worktree.prune(hive="mr")

    assert not target.exists()
    assert calls  # orca was actually consulted
    assert calls[0][:3] == ["orca", "worktree", "rm"]  # keep_branch=False: no detach call first


def test_prune_native_deletes_merged_branch_after_removal(tmp_path, monkeypatch):
    """Design delta: native prune ALSO deletes the merged branch of a SAFE tree (git branch -D)
    once the worktree is gone — the one deliberate native-behavior change (native/delegated
    parity; a delegated remove owns its own branch cleanup)."""
    entry, repo, target, branch = _prune_hive(tmp_path, monkeypatch)
    monkeypatch.setattr(plugins, "registry", lambda: [])  # no plugin → native path

    worktree.prune(hive="mr")

    assert not target.exists()
    assert worktree._branch_exists(repo, branch) is False


def test_prune_delegated_removal_skips_native_branch_delete(tmp_path, monkeypatch):
    """A delegated removal owns its own branch cleanup — native prune must NOT also run
    `git branch -D` (never native removal — including branch cleanup — after a successful
    delegated removal)."""
    entry, repo, target, branch = _prune_hive(tmp_path, monkeypatch)

    def hook(cfg, entry, **kw):
        # Deliberately does NOT delete the branch, to prove the seam doesn't do it either.
        run(
            ["git", "-C", str(repo), "worktree", "remove", "--force", str(kw["target"])],
            check=True,
        )
        return True

    plugin = _fake_plugin("fake", wt_remove=hook)
    monkeypatch.setattr(plugins, "registry", lambda: [plugin])

    worktree.prune(hive="mr")

    assert not target.exists()
    assert worktree._branch_exists(repo, branch) is True  # native branch -D never ran


# ---- index.lock retry seam (bh-i6o7) ----------------------------------------


def _locked_then_ok():
    """A fake run_fn: first call fails with an index.lock error, the next succeeds."""
    calls: list = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        if len(calls) == 1:
            return SimpleNamespace(
                returncode=128, stderr="fatal: Unable to create '.git/index.lock': File exists.\n"
            )
        return SimpleNamespace(returncode=0, stderr="")

    return fake_run, calls


def test_retry_on_index_lock_retries_then_succeeds():
    from beadhive.run import retry_on_index_lock

    fake_run, calls = _locked_then_ok()
    res = retry_on_index_lock(fake_run, ["git", "reset", "--hard", "HEAD"], sleep=0)
    assert res.returncode == 0
    assert len(calls) == 2  # first attempt lost the index.lock race, the retry won


def test_retry_on_index_lock_gives_up_after_retries():
    from beadhive.run import retry_on_index_lock

    calls: list = []

    def always_locked(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=128, stderr="cannot lock ref: .git/index.lock exists")

    res = retry_on_index_lock(always_locked, ["git", "branch", "-d", "x"], retries=3, sleep=0)
    assert res.returncode == 128
    assert len(calls) == 3  # exhausts the retry budget, then returns the last failure


def test_retry_on_index_lock_does_not_retry_other_errors():
    from beadhive.run import retry_on_index_lock

    calls: list = []

    def other_error(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=1, stderr="fatal: not a git repository")

    res = retry_on_index_lock(other_error, ["git", "status"], retries=5, sleep=0)
    assert res.returncode == 1
    assert len(calls) == 1  # a non-lock failure is returned immediately, never retried


def test_run_git_wires_the_index_lock_retry(monkeypatch):
    # The worktree seam every mutation funnels through retries a locked op transparently.
    fake_run, calls = _locked_then_ok()
    monkeypatch.setattr(worktree, "run", fake_run)
    monkeypatch.setattr("time.sleep", lambda *a, **k: None)

    res = worktree._run_git(["git", "-C", "/x", "reset", "--hard", "HEAD"])

    assert res.returncode == 0
    assert len(calls) == 2


# ---- push_branch pull-only rail (bh-uxam.1) ---------------------------------
#
# External hives fork-and-PR: `origin` (our fork) is the only remote we ever own write access
# to. `upstream` (the repo we forked from) is a read rail — any push path that resolves to it
# must refuse outright, never shell out to `git push`.


def test_push_branch_refuses_upstream_remote_without_shelling_out(monkeypatch):
    entry = {"provider": "github", "org": "acme", "repo": "widget"}

    def _boom(*a, **k):  # noqa: ARG001
        raise AssertionError("must refuse before ever invoking git")

    monkeypatch.setattr(worktree, "_run_git", _boom)

    rc = worktree.push_branch(entry, "wt/bead/issue/x-1", remote="upstream")

    assert rc != 0


def test_push_branch_still_pushes_to_origin(monkeypatch):
    entry = {"provider": "github", "org": "acme", "repo": "widget"}
    calls = []

    def _fake_run_git(args, **kw):  # noqa: ARG001
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worktree, "_run_git", _fake_run_git)
    monkeypatch.setattr(worktree.registry, "hive_dir", lambda e: Path("/x"))

    rc = worktree.push_branch(entry, "wt/bead/issue/x-1", remote="origin")

    assert rc == 0
    assert calls and calls[0][:2] == ["git", "-C"]
    assert "origin" in calls[0]


# ---- pr_base_ref: branch-base selection for external hives (bh-uxam.2) ------
#
# A contribution's diff has to be exactly what upstream would see — so a NEW bead branch on a
# `kind=external` hive forks off a freshly-fetched `upstream/<default>`, never local main (which
# may be stale, or diverged from whatever the fork happens to hold).


def test_pr_base_ref_non_external_hive_returns_local_branch_name_unfetched(monkeypatch):
    def _boom(*a, **k):  # noqa: ARG001
        raise AssertionError("a non-external hive must never fetch upstream")

    monkeypatch.setattr(worktree, "_run_git", _boom)

    assert worktree.pr_base_ref({}, {"kind": "personal"}) == "main"


def test_pr_base_ref_external_hive_fetches_and_prefixes_upstream(monkeypatch):
    calls = []

    def _fake_run_git(args, **kw):  # noqa: ARG001
        calls.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(worktree, "_run_git", _fake_run_git)
    monkeypatch.setattr(worktree.registry, "hive_dir", lambda e: Path("/x"))

    ref = worktree.pr_base_ref({}, {"kind": "external"})

    assert ref == "upstream/main"
    assert calls == [["git", "-C", "/x", "fetch", "upstream", "main"]]


def test_pr_base_ref_falls_back_to_local_on_fetch_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        worktree, "_run_git", lambda *a, **k: SimpleNamespace(returncode=1)  # noqa: ARG005
    )
    monkeypatch.setattr(worktree.registry, "hive_dir", lambda e: Path("/x"))

    ref = worktree.pr_base_ref({}, {"kind": "external"})

    assert ref == "main"  # degraded but working, not a hard failure
    assert "fetch upstream failed" in capsys.readouterr().err


def test_pr_base_ref_honors_configured_pr_base_branch_name(monkeypatch):
    calls = []
    monkeypatch.setattr(
        worktree,
        "_run_git",
        lambda args, **kw: calls.append(args) or SimpleNamespace(returncode=0),  # noqa: ARG005
    )
    monkeypatch.setattr(worktree.registry, "hive_dir", lambda e: Path("/x"))
    cfg = {"work": {"integration_branch": "develop"}}

    ref = worktree.pr_base_ref(cfg, {"kind": "external"})

    assert ref == "upstream/develop"
    assert calls == [["git", "-C", "/x", "fetch", "upstream", "develop"]]


def _external_ensure_hive(tmp_path, monkeypatch):
    """A `kind=external` hive: local main plus a distinct `upstream` remote one commit ahead —
    the fork-and-PR shape (origin is the fork we push to, upstream the repo we forked from)."""
    cfg, entry, repo = _ensure_hive(tmp_path, monkeypatch)
    entry["kind"] = "external"

    upstream_src = tmp_path / "upstream-src"
    _git("clone", "-q", str(repo), str(upstream_src), cwd=tmp_path)
    _git("config", "user.email", "t@example.com", cwd=upstream_src)
    _git("config", "user.name", "t", cwd=upstream_src)
    (upstream_src / "upstream-only.txt").write_text("fresh upstream work")
    _git("add", "upstream-only.txt", cwd=upstream_src)
    _git("commit", "-qm", "upstream-only commit", cwd=upstream_src)
    _git("remote", "add", "upstream", str(upstream_src), cwd=repo)

    # Local main is deliberately left BEHIND upstream — the base must come from upstream, not
    # whatever main happens to hold locally.
    return cfg, entry, repo


def test_ensure_external_hive_new_bead_forks_off_fetched_upstream_not_local_main(
    tmp_path, monkeypatch
):
    cfg, entry, repo = _external_ensure_hive(tmp_path, monkeypatch)

    _, target, br = worktree.ensure(cfg, "mr", "ag-epic.3")

    assert br == "wt/bead/issue/ag-epic.3"
    assert (target / "upstream-only.txt").exists(), "worktree must be based on fetched upstream"
    # the fetch actually ran and left a local remote-tracking ref behind
    ref = worktree._run_git(
        ["git", "-C", str(repo), "rev-parse", "--verify", "upstream/main"], check=False
    )
    assert ref.returncode == 0
