from __future__ import annotations

from pathlib import Path

import pytest
from scripts import validate_final_refactor_parity as parity

ROOT = Path(__file__).resolve().parents[1]


def test_final_parity_rejects_an_unexpected_sixth_distribution_script(tmp_path: Path) -> None:
    installed = {
        **parity.EXPECTED_CONSOLE_SCRIPTS,
        "beadhive-gateway": "beadhive.remote_gateway_runtime:main",
        "beadhive-sixth": "beadhive.unexpected:main",
    }

    with pytest.raises(AssertionError, match="unexpected installed console scripts") as error:
        parity._validate_installed_entry_points(installed, tmp_path)

    assert "beadhive-sixth" in str(error.value)


def test_final_parity_rejects_live_harness_missing_semantic_coverage() -> None:
    result = {
        "status": "ok",
        "contractVersion": "gateway.v1",
        "coverage": list(parity.LIVE_COVERAGE[:-1]),
    }

    with pytest.raises(AssertionError, match="live proof coverage mismatch") as error:
        parity._validate_live_result(result)

    assert "process/socket/credential/temp-state cleanup" in str(error.value)
