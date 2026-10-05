"""Contract for the bounded concurrent check runner used by architecture-structural-check."""

from __future__ import annotations

import importlib.util
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_checks_concurrently", ROOT / "scripts" / "run_checks_concurrently.py"
)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _py(code: str) -> str:
    return f"{sys.executable} -c {MODULE.shlex.quote(code)}"


def test_all_green_exits_zero_and_preserves_each_checks_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert MODULE.main(["--jobs", "2", _py("print('alpha-out')"), _py("print('beta-out')")]) == 0
    out = capsys.readouterr().out
    assert "alpha-out" in out and "beta-out" in out
    assert out.count("[ok,") == 2


def test_an_injected_failing_check_fails_the_run_with_its_exit_code_and_output(
    capsys: pytest.CaptureFixture[str],
) -> None:
    failing = _py(
        "import sys; print('boom-detail'); print('boom-err', file=sys.stderr); sys.exit(7)"
    )
    rc = MODULE.main(["--jobs", "3", _py("print('fine')"), failing, _py("print('also-fine')")])
    captured = capsys.readouterr()
    assert rc == 7
    assert "boom-detail" in captured.out and "boom-err" in captured.out
    assert "FAILED (exit 7)" in captured.out
    # The siblings still ran to completion, so one failure never hides another report.
    assert "fine" in captured.out and "also-fine" in captured.out
    assert "1 of 3 checks failed" in captured.err


def test_the_first_failing_command_in_argument_order_names_the_exit_status() -> None:
    first = _py("import time, sys; time.sleep(0.4); sys.exit(3)")
    second = _py("import sys; sys.exit(5)")
    assert MODULE.main(["--jobs", "2", first, second]) == 3


def test_checks_overlap_but_never_exceed_the_job_bound() -> None:
    sleeper = _py("import time; time.sleep(0.6)")
    started = time.monotonic()
    assert MODULE.main(["--jobs", "4", *([sleeper] * 4)]) == 0
    assert time.monotonic() - started < 2.0  # serial would be >= 2.4s

    log = _py("import time; time.sleep(0.3)")
    started = time.monotonic()
    assert MODULE.main(["--jobs", "1", *([log] * 3)]) == 0
    assert time.monotonic() - started >= 0.9  # bound of one is serial


def test_a_non_positive_job_bound_is_rejected() -> None:
    with pytest.raises(ValueError):
        MODULE.run_checks(["true"], 0)
