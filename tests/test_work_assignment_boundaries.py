"""Structural contracts for assignment and dispatch lifecycle extraction.

The assign / claim / resume / abandon policy is served by ``beadhive_core.lifecycle`` through
the one ``work_lifecycle`` composition seam (bh-sy36q.1); ``work_assignment`` keeps only the
shell-owned claim-record and batch-checkout capabilities.
"""

from __future__ import annotations

import ast
import inspect

import pytest

from beadhive import (
    work,
    work_assignment,
    work_dispatch,
    work_group,
    work_lifecycle,
    work_logic,
    work_next,
)
from beadhive_core import LifecycleCommands

ASSIGNMENT_OPERATIONS = (
    "_claim_fence",
    "_issue_claim",
    "_batch_worktree",
)

#: Facade verbs/helpers whose whole body is one delegation to the core composition seam.
LIFECYCLE_SEAM_OPERATIONS = {
    "assign": "work_lifecycle.assign",
    "claim": "work_lifecycle.claim",
    "_claim_single_bead": "work_lifecycle.claim_single_bead",
    "_batch_member_procedure_msg": "work_lifecycle.batch_member_procedure",
    "_try_claim": "work_lifecycle.try_claim",
    "_release_claim": "work_lifecycle.release_claim",
    "_provision_claim": "work_lifecycle.provision_claim",
    "resume": "work_lifecycle.resume",
    "abandon": "work_lifecycle.abandon",
}

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


@pytest.mark.parametrize(("operation", "seam"), sorted(LIFECYCLE_SEAM_OPERATIONS.items()))
def test_lifecycle_verbs_delegate_to_the_one_core_composition_seam(operation, seam):
    facade_source = inspect.getsource(getattr(work, operation))
    body = ast.parse(facade_source).body[0].body
    assert f"return {seam}(" in facade_source
    assert isinstance(body[-1], ast.Return)
    assert "bd.run(" not in facade_source and "bd.show(" not in facade_source


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

    claim_source = inspect.getsource(work_lifecycle.claim)
    assert "work.work_group.claim_group" in claim_source
    assert "work.work_group.claim_collapsed" in claim_source
    assert "api.schedule_mod.plan_schedule" in inspect.getsource(
        work_dispatch.impl_schedule_payload
    )


def test_claim_authorization_precedes_ownership_and_only_fresh_claims_dispatch():
    source = inspect.getsource(LifecycleCommands.claim)
    open_guard = source.index("issue = self._read_open(bead)")
    owner_guard = source.index("self._guard_not_other(issue, actor, bead)")
    seat_guard = source.index("self._guard_seat(issue, actor, bead")
    ownership = source.index("already_held = claim_won(issue, actor)")
    fresh = source.index("if not already_held:")
    conventions = source.index("self._dispatch_gate(issue, bead)")
    open_molecule = source.index("self._workspace.open_container(bead)")
    claim_write = source.index("self._leases.acquire(bead, actor=actor)")

    assert open_guard < owner_guard < seat_guard < ownership < fresh
    assert fresh < conventions < open_molecule < claim_write
