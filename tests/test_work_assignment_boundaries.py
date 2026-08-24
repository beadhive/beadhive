"""Structural contracts for assignment and dispatch lifecycle extraction."""

from __future__ import annotations

import ast
import inspect

import pytest

from beadhive import (
    work,
    work_assignment,
    work_dispatch,
    work_group,
    work_logic,
    work_next,
)

ASSIGNMENT_OPERATIONS = (
    "assign",
    "_claim_fence",
    "_issue_claim",
    "claim",
    "_claim_single_bead",
    "_batch_member_procedure_msg",
    "_batch_worktree",
    "_try_claim",
    "_release_claim",
    "_provision_claim",
)

DISPATCH_OPERATIONS = (
    "_next_seat_actor",
    "_molecule_members",
    "_next_payload",
    "next_",
    "loop",
    "_merged_batch_groups",
    "schedule_payload",
    "_apply_start_gating",
    "schedule",
)


@pytest.mark.parametrize(
    ("module", "module_name", "operation"),
    [
        *((work_assignment, "work_assignment", name) for name in ASSIGNMENT_OPERATIONS),
        *((work_dispatch, "work_dispatch", name) for name in DISPATCH_OPERATIONS),
    ],
)
def test_lifecycle_operations_have_one_injected_implementation_behind_the_facade(
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


def test_lifecycle_services_do_not_import_the_mutable_work_facade():
    for module in (work_assignment, work_dispatch):
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


def test_existing_policy_modules_remain_the_executable_decision_owners():
    assert work.work_next is work_next
    assert work.work_group is work_group
    assert work.work_logic is work_logic

    assert "api.work_next.claim_won" in inspect.getsource(work_assignment.impl__try_claim)
    claim_source = inspect.getsource(work_assignment.impl_claim)
    assert "api.work_group.claim_group" in claim_source
    assert "api.work_group.claim_collapsed" in claim_source
    assert "api.schedule_mod.plan_schedule" in inspect.getsource(
        work_dispatch.impl_schedule_payload
    )
