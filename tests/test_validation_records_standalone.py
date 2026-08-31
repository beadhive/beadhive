"""Standalone collection and execution closure for validation records."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("mode", "expected"),
    [
        (("--collect-only",), "14 tests collected"),
        ((), "14 passed"),
    ],
)
def test_validation_records_standalone_without_import_preload(tmp_path, mode, expected):
    repo = Path(__file__).parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            *mode,
            "-q",
            "tests/test_validation_records.py",
            "--basetemp",
            str(tmp_path / ("collect" if mode else "run")),
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert expected in result.stdout
