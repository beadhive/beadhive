"""Structural contracts for submission and review-gate lifecycle extraction."""

from __future__ import annotations

import ast
import inspect

import pytest

from beadhive import work, work_logic, work_submission

SUBMISSION_OPERATIONS = (
    "check",
    "_mark_self_check",
    "_record_check_verdict",
    "_checked_sha",
    "_guard_fork_remote",
    "submit",
    "_record_submit_commits",
    "_guard_submit_worktree",
    "_resolve_submit_actor",
    "_guard_claim_fence",
    "_guard_submit_ready",
    "_warn_submit_release_hint",
    "_validate_submit_checkout",
    "_open_submit_gate",
    "_person_of",
    "_guard_self_review",
    "approve",
    "_approve_security_gate",
    "_approve_release_hold_gate",
    "_guard_human_review_gate",
    "_resolve_review_gates",
    "_clear_stale_review_state",
    "bounce",
)


@pytest.mark.parametrize("operation", SUBMISSION_OPERATIONS)
def test_submission_operations_have_one_injected_implementation_behind_the_facade(operation):
    implementation = getattr(work_submission, f"impl_{operation}")
    facade = getattr(work, operation)
    facade_source = inspect.getsource(facade)
    facade_node = ast.parse(facade_source).body[0]

    assert implementation.__module__ == work_submission.__name__
    assert next(iter(inspect.signature(implementation).parameters)) == "api"
    assert f"work_submission.impl_{operation}" in facade_source
    assert "sys.modules[__name__]" in facade_source
    assert len(facade_node.body) == 2
    assert isinstance(facade_node.body[-1], ast.Return)


def test_submission_service_does_not_import_the_mutable_work_facade():
    tree = ast.parse(inspect.getsource(work_submission))
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


def test_existing_policy_and_ledger_boundaries_remain_executable_owners():
    assert work.work_logic is work_logic
    assert "api.validation_ledger.record" in inspect.getsource(
        work_submission.impl__record_check_verdict
    )
    assert "reuse=True" in inspect.getsource(work_submission.impl__validate_submit_checkout)
    assert "api.work_logic.ensure_review_gate" in inspect.getsource(
        work_submission.impl__open_submit_gate
    )
    assert "api.work_logic.review_gates" in inspect.getsource(work_submission.impl_approve)
    assert "api.work_logic.review_gates" in inspect.getsource(work_submission.impl_bounce)


def test_read_only_review_presentation_remains_outside_submission_boundary():
    assert work.review.__module__ == "beadhive.work_show"
    assert not hasattr(work_submission, "impl_review")
