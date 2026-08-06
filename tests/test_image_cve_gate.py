"""scripts/image-cve-gate.sh — the enforce|warn wrapper around grype over the image SBOM.

Sibling of test_osv_gate.py, guarding the same class of failure with one addition that matters.

osv-gate.sh treats exit 127 as always-fatal because a scan that COULD NOT RUN must never look
clean (bh-vf8h.3). The nix case found something worse (bh-btry): osv-scanner, pointed at the image
SBOM, reported

    found 19 packages / Filtered 19 local/unscannable package/s / No issues found

and exited **0**. There is no exit code for "I examined nothing", so a wrapper that only inspects
exit codes cannot catch it. This gate therefore asserts the COMPONENT COUNT itself, and these tests
exist to prove that assertion fires — a guard never seen red is not a guard.

A REAL `docker` is never invoked. Three of these cases are rejected before the scanner would run,
and the fourth stubs `docker` on PATH — the same trick test_osv_gate.py uses for `osv-scanner`.
Written the obvious way first, the fourth test reached the real container and took 75 seconds,
which would have made a unit test depend on a running Docker daemon.
"""

import json
import os
import subprocess
from pathlib import Path

GATE = Path(__file__).resolve().parent.parent / "scripts" / "image-cve-gate.sh"


def run_gate(mode, sbom, stub_docker_dir=None):
    """Invoke the gate. With `stub_docker_dir`, a no-op `docker` shadows the real one."""
    env = dict(os.environ)
    if stub_docker_dir is not None:
        stub = stub_docker_dir / "docker"
        stub.write_text('#!/usr/bin/env bash\necho "stub docker ran"\nexit 0\n')
        stub.chmod(0o755)
        env["PATH"] = f"{stub_docker_dir}:{env['PATH']}"
    return subprocess.run([str(GATE), mode, str(sbom)], capture_output=True, text=True, env=env)


def write_sbom(tmp_path, components):
    sbom = tmp_path / "sbom.cdx.json"
    sbom.write_text(json.dumps({"bomFormat": "CycloneDX", "components": components}))
    return sbom


def test_an_invalid_mode_is_refused_rather_than_defaulted():
    """A typo'd BH_IMAGE_CVE_MODE must not quietly become `warn`, which would disable the gate
    while leaving it looking enabled."""
    result = run_gate("bogus", "irrelevant.json")

    assert result.returncode == 2
    assert "invalid mode" in result.stderr


def test_a_missing_sbom_is_fatal_even_in_warn_mode(tmp_path):
    """`warn` downgrades FINDINGS, never "the scan did not happen". Swallowing this would print a
    clean-looking pass over a file that does not exist."""
    result = run_gate("warn", tmp_path / "absent.cdx.json")

    assert result.returncode == 127, "warn must not swallow a scan that could not run"
    assert "image-sbom" in result.stderr


def test_an_empty_sbom_is_fatal_rather_than_clean(tmp_path):
    """THE bh-btry FAILURE, as a test. An SBOM describing zero components would let any scanner
    exit 0 having examined nothing — the state osv-scanner actually reached against this file.
    Exit codes cannot express it, so the count is asserted directly."""
    result = run_gate("warn", write_sbom(tmp_path, []))

    assert result.returncode == 127
    assert "ZERO components" in result.stderr


def test_a_populated_sbom_reports_how_many_it_will_examine(tmp_path):
    """The positive half: the run must SAY what it examined. An output that names a number cannot
    silently degrade into one that examined none — which is how the osv-scanner result read as
    clean for as long as it did."""
    sbom = write_sbom(tmp_path, [{"name": "glibc"}, {"name": "jq"}])
    result = run_gate("warn", sbom, stub_docker_dir=tmp_path)

    assert "scanning 2 components" in result.stdout
    assert "completed over 2 components" in result.stdout
    assert result.returncode == 0
