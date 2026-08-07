"""scripts/osv-license-gate.sh — the license-ONLY mode wrapper around osv-scanner (bh-1kvq).

The bug this guards: `osv-scanner scan source` reports vulnerabilities AND licenses under one
exit code, so wrapping it in the generic enforce|warn gate (scripts/osv-gate.sh) made a CVE
finding block `license-check` — the only escape was `BH_LICENSE_MODE=warn`, which also disabled
license enforcement. osv-license-gate.sh re-derives a license-only status from the scan's JSON
report instead, via each package's `license_violations` field.

osv-scanner and jq are stubbed on PATH so these run anywhere, including CI without the scanner.
The stub writes a canned JSON report to whatever path follows `--output` in its argv (real
osv-scanner behavior when given `--format json --output <path>`), then exits with a given
raw code — mirroring what a real scan does: exit 1 for ANY finding, license or vulnerability.
It also answers `--help` with a flag listing, because the gate asks the installed binary
whether it still accepts `--output` before depending on it (bh-e27ep) — the flag rename that
took every `just check` in this repo to exit 127.
"""

import json
import os
import subprocess
from pathlib import Path

import pytest

GATE = Path(__file__).resolve().parent.parent / "scripts" / "osv-license-gate.sh"

_SUMMARY = [{"name": "MIT", "count": 41}]

_CRYPTOGRAPHY_VULN = {
    "package": {"name": "cryptography", "version": "49.0.0", "ecosystem": "PyPI"},
    "licenses": ["Apache-2.0"],
    "vulnerabilities": [{"id": "GHSA-g6cj-pr64-35w5"}],
}
_COPYLEFT_VIOLATION = {
    "package": {"name": "copyleft-pkg", "version": "1.0.0", "ecosystem": "PyPI"},
    "licenses": ["GPL-3.0"],
    "license_violations": ["GPL-3.0"],
}


def _report(packages: list[dict], *, summary: list[dict] | str | None = None) -> str:
    """A report in the shape osv-scanner ACTUALLY emits (measured on 2.4.0).

    `summary` defaults to a populated `license_summary`, because every real report carries one
    when `--licenses` is honoured — 12 entries on a clean scan of this repo, and present on a
    violating scan too. It is the gate's proof that license analysis ran at all (bh-ymvn), so a
    fixture omitting it models a scanner that did no license work, not a clean tree. Pass
    `summary=[]` for the present-but-empty case and `summary="omit"` to leave the key out.
    """
    report: dict = {"results": [{"packages": packages}]}
    if summary != "omit":
        report["license_summary"] = _SUMMARY if summary is None else summary
    return json.dumps(report)


CLEAN_REPORT = _report([])
VULN_ONLY_REPORT = _report([_CRYPTOGRAPHY_VULN])
LICENSE_VIOLATION_REPORT = _report([_COPYLEFT_VIOLATION])
BOTH_REPORT = _report([_CRYPTOGRAPHY_VULN, _COPYLEFT_VIOLATION])

# THE fail-open this bead closes: osv-scanner restructured its license reporting, so the gate's
# `license_violations` query matches nothing and a scan holding a REAL violation reads as clean.
# Valid JSON, parses fine, entirely plausible — and silently unanswerable. Modelled with the
# summary gone too, which is the realistic shape: a rename of the per-package license field goes
# with a rename of the per-license tally, since they are one feature.
SCHEMA_MOVED_REPORT = _report(
    [
        {
            "package": {"name": "copyleft-pkg", "version": "1.0.0", "ecosystem": "PyPI"},
            "licenses": ["GPL-3.0"],
            "licenseViolations": ["GPL-3.0"],
        }
    ],
    summary="omit",
)

# License analysis never ran (no `--licenses` reached the scanner) — a vulnerability-only report.
# Zero violations is a true statement about this report and a useless one about the tree.
NO_LICENSE_ANALYSIS_REPORT = _report([_CRYPTOGRAPHY_VULN], summary="omit")

EMPTY_SUMMARY_REPORT = _report([], summary=[])

MALFORMED_JSON_REPORT = "not valid json{"


# The flag the gate writes its report with, as osv-scanner 2.3.3+ spells it. `--output-file` was
# the old spelling; a stub that still answers to it would keep passing after the real binary
# stopped, which is exactly how bh-e27ep reached main green.
OUTPUT_FLAG = "--output"

# What the stub prints for `<subcmd> --help` — the shape osv-scanner's own help has, since the
# gate greps it for the flag. `output_flag=None` models a scanner that dropped the flag entirely.
_HELP_TEMPLATE = """   --format string, -f string   sets the output format
   {output_line}
   --verbosity string           specify the level of information
"""

# The subcommand paths the stub answers `--help` for. Anything else is an unknown help topic —
# which is what a POSITIONAL SCAN TARGET looks like to osv-scanner's help dispatch, and the whole
# reason the gate cannot just treat leading non-flag args as a subcommand path (bh-e27ep).
_HELP_TOPICS = ("scan", "scan source")

# The TOP-LEVEL listing, which carries no `--output` — measured on osv-scanner 2.3.3, where the
# flag is per-command. A stub that advertised it at top level would hide the reason the gate has
# to name a subcommand at all.
_TOP_LEVEL_HELP = """   --serve                      output as a local HTML server
   --verbosity string           specify the level of information
"""


def _stub_source(report: str | None, exit_code: int, output_flag: str | None = OUTPUT_FLAG) -> str:
    """Bash source for a fake osv-scanner, modelling osv-scanner 2.3.3's ACTUAL help dispatch.

    `--help` after a recognised subcommand path prints a flag listing advertising `output_flag`
    (omitted entirely when None) and then exits 127 — measured on the real binary, and why the
    gate reads the listing but ignores the help exit code. `--help` after an unrecognised word
    (a scan target such as `.`) prints `No help topic for '<word>'` with NO flag listing and
    exits 0 — the shape that made the first preflight assert the opposite of the truth.

    Otherwise the stub writes `report` to the path following `--output` in its argv (or writes
    nothing if `report` is None), then exits `exit_code`.
    """
    write_block = ""
    if report is not None:
        write_block = f"cat > \"$out\" <<'REPORT_EOF'\n{report}\nREPORT_EOF\n"
    output_line = ""
    if output_flag:
        output_line = f"{output_flag} string    saves the result to the given file path"
    help_text = _HELP_TEMPLATE.format(output_line=output_line)
    topics = "|" + "|".join(_HELP_TOPICS) + "|"
    return f"""#!/usr/bin/env bash
if [ "$1" = "--version" ]; then
  echo "osv-scanner version: 0.0.0-stub"
  exit 0
fi
wants_help=0
for arg in "$@"; do
  if [ "$arg" = "--help" ]; then
    wants_help=1
  fi
done
if [ "$wants_help" = 1 ]; then
  path=""
  for arg in "$@"; do
    case "$arg" in -*) break ;; esac
    cand="${{path:+$path }}$arg"
    case "{topics}" in
      *"|$cand|"*) path="$cand" ;;
      *) echo "No help topic for '$arg'"; exit 0 ;;
    esac
  done
  if [ -z "$path" ]; then
    cat <<'TOP_EOF'
{_TOP_LEVEL_HELP}TOP_EOF
    exit 127
  fi
  cat <<'HELP_EOF'
{help_text}HELP_EOF
  exit 127
fi
out=""
prev=""
for arg in "$@"; do
  if [ "$prev" = "{OUTPUT_FLAG}" ]; then
    out="$arg"
  fi
  prev="$arg"
done
{write_block}exit {exit_code}
"""


def run_license_gate(
    tmp_path,
    mode,
    report,
    exit_code,
    label="probe",
    output_flag=OUTPUT_FLAG,
    args=("scan", "source"),
):
    """Invoke osv-license-gate.sh with a stub osv-scanner that writes `report` (a JSON string,
    or None to write nothing) and exits `exit_code`. jq stays the real one from the ambient PATH.
    `output_flag=None` stubs a scanner whose help no longer advertises `--output`. `args` is the
    `<osv-scanner args...>` vector the gate's usage line advertises, so a test can pass the
    canonical `scan source <path>` form.
    """
    stub = tmp_path / "osv-scanner"
    stub.write_text(_stub_source(report, exit_code, output_flag))
    stub.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"}
    return subprocess.run(
        [str(GATE), mode, label, *args],
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


# ---- bh-e27ep: the gate must not assume the flag it writes its report with still exists ----


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_a_scanner_without_the_output_flag_is_fatal_and_names_the_flag(tmp_path, mode):
    """The bug this closes. osv-scanner renamed `--output-file` to `--output`; the gate kept
    passing the old spelling, the scanner exited "flag provided but not defined", and the gate
    reported exit 127 while blaming the ALLOWLIST — so every `just check` in this hive died at
    a message that pointed at the wrong file. The gate now asks the installed binary what it
    accepts, and when the answer is "not this flag" it says so, in both modes."""
    result = run_license_gate(tmp_path, mode, CLEAN_REPORT, 0, output_flag=None)
    assert result.returncode == 127
    assert "--output" in result.stderr
    assert "Toolchain drift" in result.stderr


def test_a_scanner_advertising_only_the_old_flag_does_not_satisfy_the_assertion(tmp_path):
    """`--output-file` must not be read as `--output` — the substring trap that would make the
    assertion pass against the very scanner generation it exists to reject."""
    result = run_license_gate(tmp_path, "enforce", CLEAN_REPORT, 0, output_flag="--output-file")
    assert result.returncode == 127
    assert "does not accept '--output'" in result.stderr


# ---- bh-e27ep (round 2): the preflight must not misdiagnose a POSITIONAL scan target ----


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_a_positional_scan_target_does_not_trip_the_output_preflight(tmp_path, mode):
    """The defect this closes. The preflight's first cut took every leading non-flag arg as the
    subcommand path to probe with `--help`. `scan source <path>` is osv-scanner's canonical form
    and this script's usage line advertises `<osv-scanner args...>`, so the scan TARGET went into
    that path — and `osv-scanner scan source . --help` answers `No help topic for '.'` with no
    flag listing at all. An empty listing has no `--output` in it, so the preflight declared a
    perfectly good scanner broken and exited 127 in both modes, with a `Check:` hint that
    reproduced its own false claim. Reproduced against real osv-scanner 2.3.3 before the fix."""
    result = run_license_gate(tmp_path, mode, CLEAN_REPORT, 0, args=("scan", "source", "."))
    assert result.returncode == 0
    assert "does not accept" not in result.stderr


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_the_output_assertion_still_fires_through_a_positional_target(tmp_path, mode):
    """The fix must not be "stop asserting". With a scan target present, a scanner that really
    has dropped `--output` is still caught and still named, in both modes."""
    result = run_license_gate(
        tmp_path, mode, CLEAN_REPORT, 0, output_flag=None, args=("scan", "source", ".")
    )
    assert result.returncode == 127
    assert "does not accept '--output'" in result.stderr
    # The remediation hint must name a real help topic, not the unknown one that caused the bug.
    assert "osv-scanner scan source --help" in result.stderr


def test_a_positional_target_does_not_mask_a_real_violation(tmp_path):
    """The preflight sits in front of the finding, so it must stay transparent to it: the same
    canonical `scan source <path>` invocation still enforces a genuine license violation."""
    result = run_license_gate(
        tmp_path, "enforce", LICENSE_VIOLATION_REPORT, 1, args=("scan", "source", ".")
    )
    assert result.returncode == 1
    assert "copyleft-pkg" in result.stderr


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_a_positional_target_does_not_mask_a_scan_that_failed_to_run(tmp_path, mode):
    """Fail-closed through the new discovery path too: exit 127 from the scan itself is still
    fatal in both modes when the invocation carries a positional target."""
    result = run_license_gate(tmp_path, mode, None, 127, args=("scan", "source", "."))
    assert result.returncode == 127
    assert "FAILED TO RUN" in result.stderr


def test_an_invocation_with_no_subcommand_is_not_failed_by_the_preflight(tmp_path):
    """With no recognised leading word there is no per-command help listing to read — and
    `--output` is a per-command flag the TOP-LEVEL listing never carries. Asserting from that
    listing would be the same false negative in a new costume, so the preflight stands down and
    leaves the verdict to the scan's own exit code."""
    result = run_license_gate(tmp_path, "enforce", CLEAN_REPORT, 0, args=("--licenses=MIT",))
    assert result.returncode == 0
    assert "does not accept" not in result.stderr


def test_the_help_probe_exit_code_is_ignored(tmp_path):
    """osv-scanner 2.3.3 prints the full flag listing and THEN exits 127 from `--help` (the stub
    models this exactly). Gating the preflight on that status would reject every scanner,
    including the working one — only the listing is evidence."""
    result = run_license_gate(tmp_path, "enforce", CLEAN_REPORT, 0)
    assert result.returncode == 0


# ---- bh-ymvn: the gate must not answer from a report it can no longer read ----


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_a_renamed_license_field_is_fatal_rather_than_clean(tmp_path, mode):
    """THE fail-open (bh-ymvn). Every query in the gate uses `// []`, so a renamed field yields
    an empty array — identical to a genuinely clean scan. This report carries a REAL violation
    under `licenseViolations`; before the fix the gate exited 0 and said so confidently."""
    result = run_license_gate(tmp_path, mode, SCHEMA_MOVED_REPORT, 1)

    assert result.returncode == 127, "a schema change must be fatal, never a clean pass"
    assert "license_summary" in result.stderr
    assert "schema changed" in result.stderr


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_a_scan_that_did_no_license_analysis_is_fatal(tmp_path, mode):
    """`--licenses` never reached the scanner, so there are no license findings to FIND. Zero
    violations is a true statement about this report and a useless one about the tree."""
    result = run_license_gate(tmp_path, mode, NO_LICENSE_ANALYSIS_REPORT, 1)

    assert result.returncode == 127
    assert "no license analysis" in result.stderr or "license_summary" in result.stderr


@pytest.mark.parametrize("mode", ["enforce", "warn"])
def test_an_empty_license_summary_is_fatal(tmp_path, mode):
    """Present-but-empty is the same unanswerable state as absent — a scan that tallied no
    licenses at all inspected nothing, whatever its exit code claims."""
    result = run_license_gate(tmp_path, mode, EMPTY_SUMMARY_REPORT, 0)

    assert result.returncode == 127
    assert "license_summary" in result.stderr


def test_a_real_violation_still_fails_after_the_schema_guard(tmp_path):
    """The guard must not swallow the finding it sits in front of: a well-formed report with a
    real violation still exits 1 under enforce, and still names the package."""
    result = run_license_gate(tmp_path, "enforce", LICENSE_VIOLATION_REPORT, 1)

    assert result.returncode == 1
    assert "copyleft-pkg" in result.stderr


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
