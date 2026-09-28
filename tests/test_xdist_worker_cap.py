"""Resource-safety contract for the repository's parallel pytest recipes."""

import os
import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).parents[1]
JUSTFILE = ROOT / "justfile"
AUTO_WORKER_ENV = "PYTEST_XDIST_AUTO_NUM_WORKERS"
MEASURED_WORKER_BOUNDS = frozenset({"{{integration_workers}}", "{{stateful_workers}}"})
WORKER_ARG = re.compile(r"(?:^|\s)(?:-n\s*|--numprocesses(?:=|\s+))(?P<workers>[^\s\\]+)")


def _logical_recipe_lines(text: str) -> list[str]:
    logical: list[str] = []
    continued = ""
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        continued = f"{continued} {line}".strip()
        if continued.endswith("\\"):
            continued = continued[:-1].rstrip()
            continue
        logical.append(continued)
        continued = ""
    if continued:
        logical.append(continued)
    return logical


def _parallel_pytest_lines(text: str | None = None) -> list[tuple[str, str]]:
    parallel: list[tuple[str, str]] = []
    for line in _logical_recipe_lines(JUSTFILE.read_text() if text is None else text):
        if not ("pytest" in line or "pants_ci.py native" in line):
            continue
        for match in WORKER_ARG.finditer(line):
            if match.group("workers") != "0":
                parallel.append((line, match.group("workers")))
    return parallel


def _unbounded_parallel_pytest_lines(text: str | None = None) -> list[str]:
    return [
        line
        for line, workers in _parallel_pytest_lines(text)
        if workers != "auto" and workers not in MEASURED_WORKER_BOUNDS
    ]


def test_every_parallel_pytest_recipe_uses_the_shared_xdist_ceiling() -> None:
    """Every discovered parallel recipe has the shared ceiling or a measured fixed bound."""
    text = JUSTFILE.read_text()
    assert f"export {AUTO_WORKER_ENV} := shell(" in text
    assert f"${{{AUTO_WORKER_ENV}:-16}}" in text
    parallel = _parallel_pytest_lines(text)
    assert parallel, "the inventory must exercise at least one parallel pytest recipe"
    assert any(workers == "auto" for _, workers in parallel)
    assert not _unbounded_parallel_pytest_lines(text)


def test_parallel_recipe_inventory_rejects_a_new_unbounded_worker_count() -> None:
    candidate = "uv run python scripts/pytest_with_report.py -n 32 tests/new_partition"

    assert _unbounded_parallel_pytest_lines(candidate) == [candidate]


def test_parallel_recipe_inventory_joins_multiline_commands() -> None:
    candidate = """test-new:
    uv run python scripts/pytest_with_report.py \\
        -n 32 tests/new_partition
"""

    assert _unbounded_parallel_pytest_lines(candidate) == [
        "uv run python scripts/pytest_with_report.py -n 32 tests/new_partition"
    ]


def test_parallel_recipe_inventory_recognizes_long_numprocesses_option() -> None:
    unbounded = "uv run pytest --numprocesses=32 tests/new_partition"
    bounded = "uv run pytest --numprocesses auto tests/existing_partition"

    assert _unbounded_parallel_pytest_lines(unbounded) == [unbounded]
    assert _unbounded_parallel_pytest_lines(bounded) == []


def test_parallel_recipe_inventory_checks_every_worker_arg_in_a_command() -> None:
    candidate = "uv run pytest -n auto tests/a && uv run pytest -n 32 tests/b"

    assert _unbounded_parallel_pytest_lines(candidate) == [candidate]


def test_live_integration_recipe_uses_its_measured_fixed_worker_bound() -> None:
    text = JUSTFILE.read_text()

    assert 'integration_workers := "16"' in text
    assert (
        'python scripts/pytest_with_report.py -n {{integration_workers}} tests -m "integration"'
        in text
    )


def test_stateful_recipe_uses_its_measured_fixed_worker_bound() -> None:
    text = JUSTFILE.read_text()

    assert 'stateful_workers := "16"' in text
    assert "python scripts/pytest_with_report.py -n {{stateful_workers}} tests" in text
    assert '-m "not integration and not pants_profile"' in text
    assert "root_composition_tests.py --ignore-args" in text
    assert "python scripts/pytest_with_report.py -n {{stateful_workers}} \\" in text


def test_just_exports_default_and_override_xdist_ceiling() -> None:
    command = ["just", "--justfile", str(JUSTFILE), "--evaluate", AUTO_WORKER_ENV]
    default_env = os.environ.copy()
    default_env.pop(AUTO_WORKER_ENV, None)

    default = subprocess.run(
        command, cwd=ROOT, check=True, text=True, capture_output=True, env=default_env
    )
    override = subprocess.run(
        command,
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
        env={**os.environ, AUTO_WORKER_ENV: "3"},
    )

    assert default.stdout.strip() == "16"
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


def test_marker_coverage_and_serial_debugging_contracts_remain() -> None:
    text = JUSTFILE.read_text()

    assert '{{ if set == "" { "" } else { "-m " + quote(set) } }}' in text
    assert '--deselect "tests/test_host_fence_int.py::' not in text
    assert "-m 'not integration' --cov=src/beadhive --cov-report=term-missing" in text
    assert "`uv run pytest -n0 ...` forces serial" in text
