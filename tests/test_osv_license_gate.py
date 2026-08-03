"""scripts/osv-license-gate.sh — the license-ONLY mode wrapper around osv-scanner (bh-1kvq).

The bug this guards: `osv-scanner scan source` reports vulnerabilities AND licenses under one
exit code, so wrapping it in the generic enforce|warn gate (scripts/osv-gate.sh) made a CVE
finding block `license-check` — the only escape was `BH_LICENSE_MODE=warn`, which also disabled
license enforcement. osv-license-gate.sh re-derives a license-only status from the scan's JSON
report instead, via each package's `license_violations` field.

osv-scanner and jq are stubbed on PATH so these run anywhere, including CI without the scanner.
The stub writes a canned JSON report to whatever path follows `--output-file` in its argv (real
osv-scanner behavior when given `--format json --output-file <path>`), then exits with a given
raw code — mirroring what a real scan does: exit 1 for ANY finding, license or vulnerability.
"""

import os
import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "scripts" / "osv-license-gate.sh"

CLEAN_REPORT = '{"results": [{"packages": []}]}'

VULN_ONLY_REPORT = """
{"results": [{"packages": [
  {"package": {"name": "cryptography", "version": "49.0.0", "ecosystem": "PyPI"},
   "licenses": ["Apache-2.0"],
   "vulnerabilities": [{"id": "GHSA-g6cj-pr64-35w5"}]}
]}]}
""".strip()

LICENSE_VIOLATION_REPORT = """
{"results": [{"packages": [
  {"package": {"name": "copyleft-pkg", "version": "1.0.0", "ecosystem": "PyPI"},
   "licenses": ["GPL-3.0"],
   "license_violations": ["GPL-3.0"]}
]}]}
""".strip()

BOTH_REPORT = """
{"results": [{"packages": [
  {"package": {"name": "cryptography", "version": "49.0.0", "ecosystem": "PyPI"},
   "licenses": ["Apache-2.0"],
   "vulnerabilities": [{"id": "GHSA-g6cj-pr64-35w5"}]},
  {"package": {"name": "copyleft-pkg", "version": "1.0.0", "ecosystem": "PyPI"},
   "licenses": ["GPL-3.0"],
   "license_violations": ["GPL-3.0"]}
]}]}
""".strip()

MALFORMED_JSON_REPORT = "not valid json{"


def _stub_source(report: str | None, exit_code: int) -> str:
    """Bash source for a fake osv-scanner: writes `report` to the path following
    --output-file in its argv (or writes nothing if `report` is None), then exits `exit_code`.
    """
    write_block = ""
    if report is not None:
        write_block = f"cat > \"$out\" <<'REPORT_EOF'\n{report}\nREPORT_EOF\n"
    return f"""#!/usr/bin/env bash
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "--output-file" ]; then
    out="$arg"
  fi
  prev="$arg"
done
{write_block}exit {exit_code}
"""


def run_license_gate(tmp_path, mode, report, exit_code, label="probe"):
    """Invoke osv-license-gate.sh with a stub osv-scanner that writes `report` (a JSON string,
    or None to write nothing) and exits `exit_code`. jq stays the real one from the ambient PATH.
    """
    stub = tmp_path / "osv-scanner"
    stub.write_text(_stub_source(report, exit_code))
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    return subprocess.run(
        [str(GATE), mode, label, "scan", "source"],
        capture_output=True,
        text=True,
        env=env,
    )


def run_license_gate_without_scanner(tmp_path, mode="enforce", label="license gate"):
    """Invoke osv-license-gate.sh with NO osv-scanner anywhere on PATH."""
    empty = tmp_path / "emptybin"
    empty.mkdir()
    return subprocess.run(
        [str(GATE), mode, label, "scan", "source"],
        capture_output=True,
        text=True,
        env={"PATH": f"{empty}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


def run_license_gate_without_jq(tmp_path, mode="enforce", label="license gate"):
    """Invoke osv-license-gate.sh with a stub osv-scanner present but NO jq anywhere on PATH."""
    minimal = tmp_path / "minimalbin"
    minimal.mkdir()
    stub = minimal / "osv-scanner"
    stub.write_text(_stub_source(CLEAN_REPORT, 0))
    stub.chmod(0o755)
    return subprocess.run(
        [str(GATE), mode, label, "scan", "source"],
        capture_output=True,
        text=True,
        env={"PATH": f"{minimal}:/usr/bin:/bin", "HOME": str(tmp_path)},
    )


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_clean_scan_passes_in_both_modes(tmp_path, mode):
    assert run_license_gate(tmp_path, mode, CLEAN_REPORT, 0).returncode == 0


def test_vulnerability_only_finding_does_not_fail_under_enforce(tmp_path):
    """The bug this bead fixes: a CVE finding must not block license-check at the default
    BH_LICENSE_MODE=enforce. osv-scanner's raw exit is 1 (a finding is present), but that
    finding has no license_violations, so the license gate must pass."""
    result = run_license_gate(tmp_path, "enforce", VULN_ONLY_REPORT, 1)
    assert result.returncode == 0
    assert "not gated here" in result.stderr
    assert "cve-report" in result.stderr


def test_license_violation_fails_under_enforce(tmp_path):
    """The property that must not be silenced while fixing the CVE coupling: a genuine license
    violation still blocks the gate."""
    result = run_license_gate(tmp_path, "enforce", LICENSE_VIOLATION_REPORT, 1)
    assert result.returncode == 1
    assert "license violations" in result.stderr
    assert "copyleft-pkg" in result.stderr


def test_license_violation_is_downgraded_under_warn(tmp_path):
    result = run_license_gate(tmp_path, "warn", LICENSE_VIOLATION_REPORT, 1)
    assert result.returncode == 0
    assert "not failing the build" in result.stderr


def test_license_violation_alongside_a_cve_still_fails_under_enforce(tmp_path):
    """A license violation must keep blocking even when the same scan also reports a CVE —
    the two toggles stay independent in both directions."""
    result = run_license_gate(tmp_path, "enforce", BOTH_REPORT, 1)
    assert result.returncode == 1
    assert "copyleft-pkg" in result.stderr


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_exit_127_is_fatal_in_both_modes(tmp_path, mode):
    """127 means the scan never ran. Downgrading it under `warn` would report a clean pass over
    a tree nothing examined — the worst available failure for this gate."""
    result = run_license_gate(tmp_path, mode, None, 127)
    assert result.returncode == 127
    assert "FAILED TO RUN" in result.stderr


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_no_report_produced_is_fatal_in_both_modes(tmp_path, mode):
    """A non-127 exit with no report to inspect (e.g. the scanner crashed after starting) must
    not be read as zero findings — that would be a clean-looking pass over an unexamined tree."""
    result = run_license_gate(tmp_path, mode, None, 1)
    assert result.returncode == 127
    assert "produced no report" in result.stderr


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_unparseable_report_is_fatal_in_both_modes(tmp_path, mode):
    result = run_license_gate(tmp_path, mode, MALFORMED_JSON_REPORT, 1)
    assert result.returncode == 127
    assert "jq could not parse" in result.stderr


def test_invalid_mode_fails_loudly_rather_than_defaulting(tmp_path):
    result = run_license_gate(tmp_path, "enfroce", CLEAN_REPORT, 0)
    assert result.returncode == 2
    assert "invalid mode" in result.stderr
    assert "enfroce" in result.stderr


def test_invalid_mode_is_rejected_before_the_scanner_runs(tmp_path):
    result = run_license_gate(tmp_path, "", CLEAN_REPORT, 0)
    assert result.returncode == 2
    assert not (tmp_path / "report-marker").exists()


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_missing_scanner_is_not_reported_as_an_input_error(tmp_path, mode):
    result = run_license_gate_without_scanner(tmp_path, mode)
    assert result.returncode == 127
    assert "not installed" in result.stderr
    assert "brew" in result.stderr
    assert "rejected its input" not in result.stderr


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_missing_jq_is_fatal_in_both_modes(tmp_path, mode):
    result = run_license_gate_without_jq(tmp_path, mode)
    assert result.returncode == 127
    assert "jq is not installed" in result.stderr
