"""Contract evidence for the live planning composition and Beads adapter semantics."""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path

from beadhive import plan_filing, planning_services
from beadhive.modules.planning import KickoffRequest, KickoffResult


def test_kickoff_adapter_preserves_exact_gate_write_contract(monkeypatch) -> None:
    """``plan_filing.CliPlanningGates.create_kickoff_gate`` — moved here from
    ``plan._create_kickoff_gate`` (bh-sy36q.2) so `bh plan file` and `bh plan repair` share ONE
    implementation of the kickoff-gate contract."""
    completed = namedtuple("Completed", "returncode stdout stderr")
    writes = []
    monkeypatch.setattr(
        plan_filing.bd,
        "run",
        lambda args, cwd, actor="", **_kwargs: (
            writes.append((args, cwd, actor)) or completed(0, "", "")
        ),
    )

    plan_filing.CliPlanningGates(Path("/hive")).create_kickoff_gate(
        "bh-epic.1", "bh-epic", actor="planner"
    )

    assert writes == [
        (
            [
                "gate",
                "create",
                "--type=human",
                "--blocks",
                "bh-epic.1",
                "--reason",
                "kickoff bh-epic",
            ],
            Path("/hive"),
            "planner",
        )
    ]


def test_kickoff_callback_keeps_typed_epic_identity() -> None:
    service = planning_services.planning_service(
        approve=lambda request: KickoffResult(request.epic_id, 1)
    )
    assert service.approve(KickoffRequest("bh-epic", Path("/hive"), "planner", {})) == (
        KickoffResult("bh-epic", 1)
    )
