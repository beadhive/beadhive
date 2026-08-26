"""Resource-safety contract for the repository's parallel pytest recipes."""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
JUSTFILE = ROOT / "justfile"
AUTO_WORKER_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"


def _parallel_pytest_lines() -> list[str]:
    return [
        line.strip()
        for line in JUSTFILE.read_text().splitlines()
        if "pytest" in line and "-n auto" in line and not line.lstrip().startswith("#")
    ]


def test_every_parallel_pytest_recipe_uses_the_shared_xdist_ceiling() -> None:
    """A new `-n auto` recipe cannot silently bypass the exported shared setting."""
    text = JUSTFILE.read_text()
    assert f"export {AUTO_WORKER_ENV} := shell(" in text
    assert f"${{{AUTO_WORKER_ENV}:-6}}" in text
    assert len(_parallel_pytest_lines()) == 3
    assert all("-n auto" in line for line in _parallel_pytest_lines())


def test_just_exports_default_and_override_xdist_ceiling() -> None:
    command = ["just", "--justfile", str(JUSTFILE), "--evaluate", AUTO_WORKER_ENV]

    default = subprocess.run(command, cwd=ROOT, check=True, text=True, capture_output=True)
    override = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, AUTO_WORKER_ENV: "3"},
    )

    assert default.stdout.strip() == "6"
    assert override.stdout.strip() == "3"


def test_just_rejects_an_invalid_xdist_ceiling() -> None:
    result = subprocess.run(
        ["just", "--justfile", str(JUSTFILE), "--evaluate", AUTO_WORKER_ENV],
        cwd=ROOT,
        check=False,
        text=True,
        capture_output=True,
        env={**os.environ, AUTO_WORKER_ENV: "not-a-number"},
    )

    assert result.returncode != 0
    assert f"{AUTO_WORKER_ENV} must be a positive integer" in result.stderr


def test_marker_quarantine_coverage_and_serial_debugging_contracts_remain() -> None:
    text = JUSTFILE.read_text()

    assert '{{ if set == "" { "" } else { "-m " + quote(set) } }}' in text
    assert text.count('--deselect "tests/test_host_fence_int.py::') == 2
    assert "-m 'not integration' --cov=src/beadhive --cov-report=term-missing" in text
    assert "`uv run pytest -n0 ...` forces serial" in text
