"""Versioned raw L1-L4 producer fixtures never imply a downstream codec result."""

from __future__ import annotations

import json
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures" / "live_ingress" / "v1"


def test_l1_l4_raw_fixtures_keep_exact_correlation_and_split_identities() -> None:
    cells = [json.loads((FIXTURES / f"L{number}.json").read_text()) for number in range(1, 5)]

    assert [(cell["cell"], cell["surface"], cell["provider"]) for cell in cells] == [
        ("L1", "work_loop", "claude-code"),
        ("L2", "work_loop", "codex"),
        ("L3", "direct_role", "claude-code"),
        ("L4", "direct_role", "codex"),
    ]
    for cell in cells:
        descriptor = cell["source_descriptor"]
        journal = cell["run_journal_jsonl"][0]
        summary = cell["host_summary_json"]
        final = cell["final_seat_run"]
        assert descriptor["contract_version"] == "beadhive.named-hive-sources/v1"
        assert descriptor["identity"]["registered_identity"] == "github/beadhive/beadhive"
        assert journal["hive"] == summary["hive"] == "github/beadhive/beadhive"
        assert journal["bead"] == summary["bead"] == final["outcome"]["bead_id"]
        assert final["session_id"] == journal["provider_continuation"]
        assert len({journal["run_id"], summary["session_id"], final["session_id"]}) == 3


def test_fixture_report_limits_its_compatibility_claim() -> None:
    report = json.loads((FIXTURES / "report.json").read_text())

    assert report["compatibility_claim"] == "producer schemas and raw fixture compatibility only"
    assert report["beadhive_ui_codec_consumption"] == "not claimed"
    assert report["agent_run_summary_join"] == "never outer run id or provider continuation"
    assert report["genuine_provider_smoke"]["status"] == "externally_blocked"
