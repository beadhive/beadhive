"""The intake public API, its bounded surface and the ReportStore contract on the memory store."""

from __future__ import annotations

import dataclasses

import pytest

from beadhive.modules import intake
from beadhive.modules.intake import contracts, domain
from beadhive.testing.intake import (
    REPORT_STORE_CASES,
    ReportStoreCase,
    memory_store,
    run_report_store_case,
)


@pytest.mark.parametrize("case", REPORT_STORE_CASES, ids=lambda case: case.case_id)
def test_memory_report_store_passes_every_contract_case(case: ReportStoreCase) -> None:
    run_report_store_case(memory_store, case, subject="memory")


def test_public_api_is_bounded_and_resolves() -> None:
    names = intake.__all__
    assert len(names) == len(set(names))
    for name in names:
        assert hasattr(intake, name), name
    assert {"IntakePort", "IntakeService", "NullIntake", "ReportStore"} <= set(names)
    assert {"CheckpointStore", "LabelPolicy", "IntakeRefused", "RefusalReason"} <= set(names)
    for private in ("paging", "service", "ports", "models", "rules"):
        assert private not in names


def test_contracts_reexport_the_domain_records_by_identity() -> None:
    for name in contracts.__all__:
        if hasattr(domain, name):
            assert getattr(contracts, name) is getattr(domain, name), name


def test_domain_records_are_frozen_values() -> None:
    for name in domain.__all__:
        obj = getattr(domain, name)
        if dataclasses.is_dataclass(obj):
            assert obj.__dataclass_params__.frozen, f"{name} must be a frozen dataclass"


def test_port_names_match_the_adr() -> None:
    assert [m for m in vars(contracts.IntakePort) if not m.startswith("_")] == [
        "file_report",
        "import_issue",
        "link",
        "unlink",
        "queue",
        "dispose",
        "binding_of",
        "reports_for_work",
        "work_progress",
        "discover",
        "ingest_thread",
        "reconcile_thread",
        "reconcile_presence",
        "thread_of",
        "mark_considered",
        "sweep_holds",
        "draft_reply",
        "outbox",
        "render",
        "approve",
        "send",
        # the one-process batch send (bh-pc9rf), additive
        "send_approved",
        "reconcile",
        "discard",
        "retry",
        "backfill_closes",
        "reopen",
        # the read-only end-state scan (bh-cd03d, bh-c0x0i), additive
        "end_states",
        "on_work_merged",
        "on_release",
        # waiting for the reporter (bh-61xkc), additive
        "needs_info",
        "wake",
        "wake_all",
        # the progress milestones (bh-dev42), additive
        "on_work_started",
        "delivery_notice",
        # reproduction work, the investigates relation (bh-u3a53), additive
        "investigate",
        "uninvestigate",
        "relations",
        # review packets and triage-quality metrics (bh-hrx2r), additive
        "review_packets",
        "review_metrics",
        # the label projection and its batch approval (bh-uim9i), additive
        "sync_labels",
        "define_labels",
        "review_label_batch",
        "approve_label_batch",
        # propose, review and apply-reviewed (bh-9aqmv), additive
        "propose",
        "review_proposal",
        "apply_reviewed",
        # the delivery a rejected report must not hold (bh-5kpwc), additive
        "sweep_delivery",
    ]
    assert {m for m in vars(contracts.ReportStore) if not m.startswith("_")} >= {
        "scope",
        "get",
        "find_by_external_ref",
        "create",
        "set_state",
        "clear_state",
        "record_snapshot",
        "update_fields",
        "close",
        "assign",
        "list_open_labeled",
        "duplicate_pairs",
        "tracked_work",
        "track",
        "untrack",
        "trackers_of",
        "investigate",
        "uninvestigate",
        "investigations_of",
        "read_thread",
        "write_thread",
        "holds",
        "bound_reports",
        "open_hold",
        "resolve_hold",
        "read_outbox",
        "write_outbox",
        "reports_with_outbox",
    }
    assert {m for m in vars(contracts.CheckpointStore) if not m.startswith("_")} == {"get", "put"}
    assert {m for m in vars(contracts.LabelPolicy) if not m.startswith("_")} == {"violations"}
    assert {m for m in vars(contracts.IntakeLock) if not m.startswith("_")} == {"hold"}
    assert {m for m in vars(contracts.ReleaseVerifier) if not m.startswith("_")} == {
        "resolve",
        "contains",
    }
    assert {m for m in vars(contracts.OutboxView) if not m.startswith("_")} == {
        "receipt_ids",
        "pending",
        "issued",
        "known_actors",
    }


def test_refusal_carries_a_reason_and_a_one_line_detail() -> None:
    refusal = intake.IntakeRefused(intake.RefusalReason.NOT_A_URL, "paste the issue URL")

    assert refusal.reason is intake.RefusalReason.NOT_A_URL
    assert str(refusal) == "not_a_url: paste the issue URL"
    assert str(intake.IntakeRefused(intake.RefusalReason.INTAKE_DISABLED)) == "intake_disabled"
