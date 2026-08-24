"""Structural contracts for merge, landing, and refine orchestration extraction."""

from __future__ import annotations

import ast
import inspect

import pytest

from beadhive import work, work_group, work_logic, work_merge, work_refine, worktree

MERGE_OPERATIONS = (
    "_delete_branch",
    "_teardown_coordinator_seat",
    "_rollback_or_keep",
    "_pr_ref",
    "_close_swarm_bead",
    "_pr_merge_gates",
    "_ensure_pr_gate",
    "_open_landing_pr",
    "_guard_molecule_children",
    "_guard_molecule_land_base",
    "_open_molecule_pr",
    "_validate_molecule_checkout",
    "_postland_revalidate_molecule",
    "_close_molecule_origin_reports",
    "_reconcile_landed_molecule",
    "_merge_molecule",
    "finish",
    "land",
    "_prune_landed_hive",
    "_guard_land_pr_pending",
    "_resolve_merged_land_pr",
    "_resolve_land_pr_merge_gates",
    "_close_land_origin_reports",
    "merge",
    "_guard_bead_merge_gates",
    "_guard_bead_land_base",
    "already_landed",
    "_guard_bead_clean_history",
    "_reconcile_landed_bead",
    "_guard_signed_history",
    "_merge_bead_no_ff",
    "_postland_revalidate_bead",
    "_record_merge_commit",
    "_merge_bead",
)

REFINE_OPERATIONS = (
    "_load_plan",
    "_restore",
    "refine_branch",
    "_guard_refine_mode",
    "_resolve_refine_base",
    "_build_refine_plan",
    "_apply_refine_rebase",
    "refine",
)


@pytest.mark.parametrize(
    ("module", "module_name", "operation"),
    [
        *((work_merge, "work_merge", name) for name in MERGE_OPERATIONS),
        *((work_refine, "work_refine", name) for name in REFINE_OPERATIONS),
    ],
)
def test_merge_and_refine_have_one_injected_implementation_behind_the_facade(
    module, module_name, operation
):
    implementation = getattr(module, f"impl_{operation}")
    facade = getattr(work, operation)
    facade_source = inspect.getsource(facade)
    facade_node = ast.parse(facade_source).body[0]

    assert implementation.__module__ == module.__name__
    assert next(iter(inspect.signature(implementation).parameters)) == "api"
    assert f"{module_name}.impl_{operation}" in facade_source
    assert "sys.modules[__name__]" in facade_source
    assert len(facade_node.body) == 2
    assert isinstance(facade_node.body[-1], ast.Return)


def test_extracted_services_do_not_import_the_mutable_work_facade():
    for module in (work_merge, work_refine):
        tree = ast.parse(inspect.getsource(module))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        from_imports = {node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)}

        assert "beadhive.work" not in imports
        assert "work" not in from_imports
        assert "beadhive.work" not in from_imports


def test_existing_integration_primitives_remain_executable_owners():
    assert work.work_group is work_group
    assert work.work_logic is work_logic
    assert work.worktree is worktree

    merge_source = inspect.getsource(work_merge.impl__merge_bead)
    molecule_source = inspect.getsource(work_merge.impl__merge_molecule)
    assert "api.work_group.merge_slot" in merge_source
    assert "api.work_group.merge_slot" in molecule_source
    assert "api.worktree.try_merge_rebase" in inspect.getsource(work_merge.impl__merge_bead_no_ff)
    assert "api.worktree.merge_no_ff" in molecule_source
    assert "api.work_group.merge_group" in inspect.getsource(work_merge.impl_merge)


def test_rollback_and_refine_recovery_contracts_live_in_executable_owners():
    rollback = inspect.getsource(work_merge.impl__rollback_or_keep)
    apply_refine = inspect.getsource(work_refine.impl__apply_refine_rebase)
    restore = inspect.getsource(work_refine.impl__restore)

    assert "api.worktree.safe_to_rewrite" in rollback
    assert "api.worktree.reset_hard" in rollback
    assert apply_refine.index("api.worktree.backup_branch") < apply_refine.index(
        "api.worktree.rebase_autosquash"
    )
    assert "api.worktree.same_tree" in apply_refine
    assert "api._restore(target, backup)" in apply_refine
    assert "api.worktree.rebase_abort" in restore
    assert "api.worktree.reset_hard" in restore


def test_facade_documents_final_dependency_map_and_preserves_adjacent_boundaries():
    facade_module = inspect.getsource(work)
    for service in (
        "work_reads",
        "work_show",
        "work_intake",
        "work_assignment",
        "work_dispatch",
        "work_submission",
        "work_merge",
        "work_refine",
    ):
        assert service in work.__doc__
        assert service in facade_module

    assert not hasattr(work_merge, "impl_start")
    assert not hasattr(work_merge, "impl_resume")
    assert not hasattr(work_merge, "impl_abandon")
