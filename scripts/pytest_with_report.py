#!/usr/bin/env python3
"""Run pytest, adding one collision-free JUnit report only inside a bh validation run."""

from __future__ import annotations

import os
import tempfile
from collections.abc import Mapping, Sequence
from pathlib import Path

from beadhive.test_report import ENV_VAR


def pytest_argv(args: Sequence[str], env: Mapping[str, str] | None = None) -> list[str]:
    """Build pytest's argv without changing the unset-variable path."""
    environ = os.environ if env is None else env
    command = ["pytest", *args]
    directory = environ.get(ENV_VAR)
    if not directory:
        return command
    try:
        descriptor, filename = tempfile.mkstemp(
            prefix=f"pytest-{os.getpid()}-", suffix=".xml", dir=Path(directory)
        )
    except OSError:
        # Report production is detail. An unavailable drop zone must not change pytest's rc.
        return command
    os.close(descriptor)
    command.insert(1, f"--junitxml={filename}")
    return command


def main() -> None:
    command = pytest_argv(os.sys.argv[1:])
    os.execvp(command[0], command)


if __name__ == "__main__":
    main()
