"""scripts/osv-gate.sh — the enforce|warn mode wrapper around osv-scanner.

The failure this guards is a gate that looks enabled but isn't: a swallowed exit 127, or a
typo'd mode quietly degrading to `warn`. Both present as a green build over an unexamined tree.

osv-scanner is stubbed on PATH so these run anywhere, including CI without the scanner.
"""

import os
import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "scripts" / "osv-gate.sh"


def run_gate(tmp_path, mode, scanner_exit, label="probe"):
    """Invoke osv-gate.sh with a stub osv-scanner that exits `scanner_exit`."""
    stub = tmp_path / "osv-scanner"
    stub.write_text(f'#!/usr/bin/env bash\necho "stub scanner ran"\nexit {scanner_exit}\n')
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    return subprocess.run(
        [str(GATE), mode, label, "scan", "source"],
        capture_output=True,
        text=True,
        env=env,
    )


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_clean_scan_passes_in_both_modes(tmp_path, mode):
    assert run_gate(tmp_path, mode, 0).returncode == 0


def test_findings_fail_under_enforce(tmp_path):
    assert run_gate(tmp_path, "enforce", 1).returncode == 1


def test_findings_are_downgraded_under_warn(tmp_path):
    result = run_gate(tmp_path, "warn", 1)
    assert result.returncode == 0
    assert "not failing the build" in result.stderr


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_exit_127_is_fatal_in_both_modes(tmp_path, mode):
    """127 means the scan never ran. Downgrading it under `warn` would report a clean pass
    over a tree nothing examined — the worst available failure for this gate."""
    result = run_gate(tmp_path, mode, 127)
    assert result.returncode == 127
    assert "FAILED TO RUN" in result.stderr


def test_invalid_mode_fails_loudly_rather_than_defaulting(tmp_path):
    result = run_gate(tmp_path, "enfroce", 0)
    assert result.returncode == 2
    assert "invalid mode" in result.stderr
    assert "enfroce" in result.stderr


def test_invalid_mode_is_rejected_before_the_scanner_runs(tmp_path):
    """A bad mode must not fall through to a scan whose result would then be misapplied."""
    result = run_gate(tmp_path, "", 0)
    assert result.returncode == 2
    assert "stub scanner ran" not in result.stdout
