"""One interpreter for bead worktrees and the gate (bh-tmrmr).

``.python-version`` is what ``uv`` reads when it creates a worktree's ``.venv``; without it uv
takes the first interpreter on PATH satisfying ``requires-python`` (>=3.11), which on a bead
worktree was 3.11 while the main gate ran 3.12 and hid 3.12-only failures (bh-fdrnq). These
checks are static or compare the running interpreter with the pin: no timing, no subprocess.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]


def _pin() -> str:
    return (ROOT / ".python-version").read_text().strip()


def _minor(version: str) -> tuple[int, int]:
    major, minor = version.split(".")[:2]
    return int(major), int(minor)


def test_python_version_pin_is_major_minor():
    assert re.fullmatch(r"\d+\.\d+", _pin()), "pin a minor (e.g. 3.12) so patch bumps need no edit"


def test_pin_satisfies_requires_python():
    spec = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["requires-python"]
    assert SpecifierSet(spec).contains(f"{_pin()}.0")


def test_pin_agrees_with_mise():
    mise = tomllib.loads((ROOT / ".mise.toml").read_text())["tools"]["python"]
    assert _minor(mise) == _minor(_pin())


def test_running_interpreter_matches_pin():
    assert sys.version_info[:2] == _minor(_pin()), (
        f"running Python {sys.version_info[0]}.{sys.version_info[1]} but .python-version pins "
        f"{_pin()}; run `uv sync` in this worktree so its .venv follows the pin"
    )
