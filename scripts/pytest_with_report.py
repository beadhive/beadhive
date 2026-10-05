#!/usr/bin/env python3
"""Run pytest, adding one collision-free JUnit report only inside a bh validation run."""

from __future__ import annotations

import os
import sys
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from beadhive.test_report import ENV_VAR

COVERAGE_ENV_VAR = "BH_TEST_COVERAGE"
_TRUE = frozenset({"1", "true", "yes", "on"})


def pytest_argv(args: Sequence[str], env: Mapping[str, str] | None = None) -> list[str]:
    """Build pytest's argv without changing the unset-variable path.

    pytest runs as `<this interpreter> -m pytest`, not a bare `pytest` PATH lookup: a caller that
    reaches this script through the venv interpreter without activating the venv (so its `bin/`
    is not on PATH) would otherwise die in `execvp` with FileNotFoundError.
    """
    environ = os.environ if env is None else env
    command = [sys.executable, "-m", "pytest", *args]
    directory = environ.get(ENV_VAR)
    if not directory:
        return command
    try:
        descriptor, filename = tempfile.mkstemp(
            prefix=f"pytest-{os.getpid()}-", suffix=".xml", dir=Path(directory)
        )
    except OSError as exc:
        # Report production is detail. An unavailable drop zone must not change pytest's rc.
        print(
            f"warning: {ENV_VAR} is unavailable; running pytest without a report: {exc}",
            file=sys.stderr,
        )
        return command
    os.close(descriptor)
    command.insert(3, f"--junitxml={filename}")
    if environ.get(COVERAGE_ENV_VAR, "").lower() in _TRUE:
        coverage = Path(filename).with_name(Path(filename).name.replace("pytest-", "coverage-", 1))
        command[3:3] = [
            "--cov=src/beadhive",
            "--cov-report=",
            f"--cov-report=xml:{coverage}",
        ]
    return command


def main() -> None:
    command = pytest_argv(os.sys.argv[1:])
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
