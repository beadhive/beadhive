"""Contract evidence for the live planning composition and Beads adapter semantics."""

from __future__ import annotations

from collections import namedtuple
from pathlib import Path

from beadhive import plan, planning_services
from beadhive.modules.planning import FilingRequest, FilingResult, KickoffRequest, KickoffResult


def test_callback_filing_receives_validated_stable_dag() -> None:
    calls = []
    spec = {
        "epic": {"title": "Extract planning"},
        "issues": [
            {"handle": "implement", "deps": ["design"]},
            {"handle": "design", "deps": []},
        ],
    }

    def file_adapter(request, graph):
        calls.append((request, graph))
        return FilingResult("bh-plan", 2, 1)

    result = planning_services.planning_service(
        validate=lambda _request: [], file=file_adapter
    ).file(FilingRequest(spec, Path("/hive"), "planner", {}))

    assert result == FilingResult("bh-plan", 2, 1)
    assert calls[0][1].order == ("design", "implement")
    assert calls[0][1].roots == ("design",)


def test_kickoff_adapter_preserves_exact_gate_write_contract(monkeypatch) -> None:
    completed = namedtuple("Completed", "returncode stdout stderr")
    writes = []
    monkeypatch.setattr(
        plan.bd,
        "run",
        lambda args, cwd, actor="", **_kwargs: (
            writes.append((args, cwd, actor)) or completed(0, "", "")
        ),
    )

    plan._create_kickoff_gate("bh-epic.1", "bh-epic", Path("/hive"), "planner")

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
