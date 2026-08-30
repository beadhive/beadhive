"""Fail-closed contract for the opt-in installed Herdr six-seat probe."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PROBE = ROOT / "scripts" / "probe-herdr-managed-seats.sh"


def _fake_herdr(path: Path) -> Path:
    fake = path / "herdr"
    fake.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
echo "$*" >>"$BH_PROBE_LOG"
case "$*" in
  *"workspace create"*)
    printf '%s\n' '{"result":{"root_pane":{"pane_id":"pane-root"},'\
'"workspace":{"workspace_id":"workspace-proof"}}}' ;;
  *"pane split"*)
    printf '%s\n' '{"result":{"pane":{"pane_id":"pane-row"}}}' ;;
  *"agent start"*)
    [[ -z "${BH_PROBE_FAIL_TARGET:-}" || "$*" != *"$BH_PROBE_FAIL_TARGET"* ]] ;;
  *"agent read"*)
    target=$5
    printf '%s %s\n' "$target" "proof-redacted-${target#proof-}" ;;
  *"agent list"*) printf '%s\n' '{"agents":[]}' ;;
  *) : ;;
esac
"""
    )
    fake.chmod(0o755)
    return fake


def _run(tmp_path: Path, *, fail_target: str = "") -> subprocess.CompletedProcess[str]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _fake_herdr(bin_dir)
    checkout = tmp_path / "checkout"
    subprocess.run(["git", "init", str(checkout)], check=True, capture_output=True)
    log = tmp_path / "herdr.log"
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "BH_HERDR_PROOF_SESSION": "proof-session",
        "BH_HERDR_PROOF_CWD": str(checkout),
        "BH_PROBE_LOG": str(log),
        "BH_PROBE_FAIL_TARGET": fail_target,
    }
    result = subprocess.run([str(PROBE)], text=True, capture_output=True, env=env)
    result.herdr_log = log.read_text()  # type: ignore[attr-defined]
    return result


def test_probe_exits_zero_only_after_all_six_rows_pass(tmp_path):
    result = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout.count("ROW PASS") == 6
    assert "workspace close workspace-proof" in result.herdr_log


def test_probe_failed_row_is_nonzero_and_cleanup_still_runs(tmp_path):
    result = _run(tmp_path, fail_target="proof-claude-dispatcher")
    assert result.returncode != 0
    assert "ROW FAIL harness=claude seat=dispatcher stage=startup" in result.stderr
    assert result.stdout.count("ROW PASS") == 5
    assert "workspace close workspace-proof" in result.herdr_log
