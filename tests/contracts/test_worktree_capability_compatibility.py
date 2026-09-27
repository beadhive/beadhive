"""Executable compatibility boundary for the extracted worktrees capability."""

from __future__ import annotations

import inspect
from pathlib import Path

from beadhive import cli, mcp, worktree, worktree_cleanup, worktree_inventory
from beadhive.modules import worktrees


def test_legacy_branch_helpers_keep_patchable_facades_over_module_policy() -> None:
    assert worktree.apply_prefix.__module__ == "beadhive.worktree"
    assert worktree._leaf.__module__ == "beadhive.worktree"
    assert worktrees.apply_prefix.__module__.endswith("domain.models")
    assert "_policy_apply_prefix" in inspect.getsource(worktree.apply_prefix)
    assert "leaf_for_branch" in inspect.getsource(worktree._leaf)


def test_legacy_inventory_keeps_tuple_and_status_object_shapes(monkeypatch) -> None:
    row = object()
    monkeypatch.setattr(
        worktree_inventory,
        "impl_managed",
        lambda _cfg: [("bh", "/managed/bh-1", "wt/bead/issue/bh-1")],
    )
    monkeypatch.setattr(worktree_inventory, "impl_status_rows", lambda _hive: [row])

    assert worktree.managed({}) == [("bh", "/managed/bh-1", "wt/bead/issue/bh-1")]
    assert worktree.status_rows("bh") == [row]


def test_facade_composes_adapters_over_dynamic_patch_points() -> None:
    source = "\n".join(
        inspect.getsource(obj)
        for obj in (
            worktree._worktree_lifecycle_service,
            worktree._selected_worktree_manager,
            worktree._PluginCreateObserver,
        )
    )
    for seam in ("_run_git", "_notify_wt_create", "config.worktrees_manager"):
        assert seam in source
    assert "NativeGitWorktreeManager" in source
    assert "bind_application_port" in source
    # bh-055ot.1: one selected manager — no plugin delegation seam, no plugin-first fallback.
    for retired in ("_consult_wt_create", "_consult_wt_remove", "PluginWorktreeProvisioner"):
        assert retired not in source
        assert not hasattr(worktree, retired)


def test_cli_and_mcp_keep_rendering_and_payload_ownership() -> None:
    assert "worktree.add(" in inspect.getsource(cli.wt_add)
    assert "worktree.remove(" in inspect.getsource(cli.wt_rm)
    assert "worktree.status_cmd(" in inspect.getsource(cli.wt_status)
    assert "worktree.status_rows()" in inspect.getsource(mcp._register_read_resources)
    worktrees_src = Path(__file__).parents[2] / "packages/beadhive-worktrees/src/beadhive_worktrees"
    module_source = "\n".join(path.read_text() for path in sorted(worktrees_src.rglob("*.py")))
    for presentation_name in ("typer", "jsonout", "as_json", "exit_code", "render"):
        assert presentation_name not in module_source


def test_remove_and_prune_keep_compensation_outside_the_module() -> None:
    remove_source = inspect.getsource(worktree_cleanup.impl_remove)
    prune_source = inspect.getsource(worktree_cleanup.impl__prune_remove_one)
    assert "claim_authority.remove_record_path" in remove_source
    assert "_rmdir_empty_parents" in remove_source
    assert '"branch", "-D"' in prune_source
    assert "claim_authority" not in inspect.getsource(worktrees.WorktreeLifecycleService)
