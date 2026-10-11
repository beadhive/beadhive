"""The wall-clock guard fails on a new un-allowlisted sleep (bh-zbn8a)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_test_wall_clock.py"
SPEC = importlib.util.spec_from_file_location("check_test_wall_clock", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

EMPTY = ""


def _tree(root: Path, source: str, allowlist: str = EMPTY) -> None:
    (root / "tests").mkdir(parents=True)
    (root / "tests" / "test_x.py").write_text(source, encoding="utf-8")
    (root / "tests" / "wall_clock_allowlist.toml").write_text(allowlist, encoding="utf-8")


ENTRY = (
    '[[allow]]\npath = "tests/test_x.py"\nanchor = "{anchor}"\nkind = "{kind}"\n'
    'reason = "{reason}"\n'
)


def test_repository_tests_are_clean() -> None:
    assert MODULE.check() == []


def test_a_new_unallowlisted_sleep_fails(tmp_path: Path) -> None:
    _tree(tmp_path, "import time\n\ndef test_a():\n    time.sleep(0.5)\n")
    errors = MODULE.check(tmp_path)
    assert len(errors) == 1
    assert "tests/test_x.py:4" in errors[0] and "sleep in test_a" in errors[0]


def test_asyncio_and_bare_sleep_fail_but_zero_yield_does_not(tmp_path: Path) -> None:
    source = (
        "import asyncio\nfrom time import sleep\n\n"
        "async def test_a():\n    await asyncio.sleep(0)\n    await asyncio.sleep(0.1)\n"
        "def test_b():\n    sleep(1)\n"
    )
    _tree(tmp_path, source)
    lines = sorted(f.line for f in MODULE.scan(tmp_path))
    assert lines == [6, 8]


def test_measured_duration_assert_fails_but_plain_timestamp_does_not(tmp_path: Path) -> None:
    source = (
        "import time\n\n"
        "def test_a():\n    start = time.monotonic()\n    elapsed = time.monotonic() - start\n"
        "    assert elapsed < 1\n"
        "def test_b():\n    assert time.time() > 0\n"
    )
    _tree(tmp_path, source)
    assert [(f.anchor, f.kind) for f in MODULE.scan(tmp_path)] == [("test_a", "elapsed-assert")]


def test_sleep_inside_a_child_program_string_is_not_parsed(tmp_path: Path) -> None:
    _tree(tmp_path, 'CODE = "import time; time.sleep(300)"\n')
    assert MODULE.check(tmp_path) == []


def test_allowlist_entry_with_reason_accepts_the_finding(tmp_path: Path) -> None:
    entry = ENTRY.format(anchor="test_a", kind="sleep", reason="readiness poll interval")
    _tree(tmp_path, "import time\n\ndef test_a():\n    time.sleep(0.5)\n", entry)
    assert MODULE.check(tmp_path) == []


def test_allowlist_entry_without_reason_is_rejected(tmp_path: Path) -> None:
    entry = ENTRY.format(anchor="test_a", kind="sleep", reason=" ")
    _tree(tmp_path, "import time\n\ndef test_a():\n    time.sleep(0.5)\n", entry)
    assert any("missing reason" in e for e in MODULE.check(tmp_path))


def test_stale_allowlist_entry_fails(tmp_path: Path) -> None:
    entry = ENTRY.format(anchor="test_gone", kind="sleep", reason="was needed")
    _tree(tmp_path, "def test_a():\n    pass\n", entry)
    assert any("stale entry" in e for e in MODULE.check(tmp_path))


def test_perf_marked_tests_are_exempt(tmp_path: Path) -> None:
    source = (
        "import time, pytest\n\n@pytest.mark.perf\ndef test_a():\n"
        "    s = time.monotonic()\n    assert time.monotonic() - s < 1\n"
    )
    _tree(tmp_path, source)
    assert MODULE.check(tmp_path) == []
