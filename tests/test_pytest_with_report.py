from __future__ import annotations

import os
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.pytest_with_report import COVERAGE_ENV_VAR, pytest_argv
from scripts.report_timings import rankings

from beadhive import test_report

ROOT = Path(__file__).resolve().parents[1]


def _report_path(argv: list[str]) -> Path:
    option = next(argument for argument in argv if argument.startswith("--junitxml="))
    return Path(option.removeprefix("--junitxml="))


def test_unset_environment_preserves_pytest_arguments_byte_for_byte() -> None:
    arguments = ["-q", "tests/example.py", "-m", "not integration"]

    assert pytest_argv(arguments, {}) == ["pytest", *arguments]


def test_report_filenames_are_exclusive_across_concurrent_invocations(tmp_path: Path) -> None:
    env = {test_report.ENV_VAR: str(tmp_path)}
    with ThreadPoolExecutor(max_workers=8) as pool:
        commands = list(pool.map(lambda _: pytest_argv(["-q"], env), range(32)))

    paths = [_report_path(command) for command in commands]
    assert len(set(paths)) == len(paths)
    assert all(path.is_file() for path in paths)


def test_unwritable_report_directory_warns_once_without_changing_pytest_argv(
    tmp_path: Path, capsys
) -> None:
    missing = tmp_path / "missing"

    assert pytest_argv(["-q"], {test_report.ENV_VAR: str(missing)}) == ["pytest", "-q"]
    assert capsys.readouterr().err.count("running pytest without a report") == 1


def test_unwritable_report_directory_preserves_pytest_exit_code(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            str(ROOT / ".venv/bin/python"),
            str(ROOT / "scripts/pytest_with_report.py"),
            "tests/test_pytest_with_report.py::does_not_exist",
        ],
        env={**os.environ, test_report.ENV_VAR: str(tmp_path / "missing")},
        capture_output=True,
        text=True,
        timeout=60,
    )

    assert result.returncode == 4
    assert result.stderr.count("running pytest without a report") == 1


def test_coverage_switch_writes_to_the_same_drop_zone(tmp_path: Path) -> None:
    command = pytest_argv(["-q"], {test_report.ENV_VAR: str(tmp_path), COVERAGE_ENV_VAR: "1"})

    junit = _report_path(command)
    coverage = next(
        Path(argument.removeprefix("--cov-report=xml:"))
        for argument in command
        if argument.startswith("--cov-report=xml:")
    )
    assert "--cov=src/beadhive" in command
    assert coverage.parent == tmp_path
    assert coverage.name != junit.name


def test_coverage_switch_produces_xml_in_the_drop_zone(tmp_path: Path) -> None:
    result = subprocess.run(
        [
            str(ROOT / ".venv/bin/python"),
            str(ROOT / "scripts/pytest_with_report.py"),
            "-q",
            "tests/test_pytest_with_report.py::test_unset_environment_preserves_pytest_arguments_byte_for_byte",
        ],
        env={
            **os.environ,
            test_report.ENV_VAR: str(tmp_path),
            COVERAGE_ENV_VAR: "1",
        },
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert result.returncode == 0, result.stderr
    assert len(list(tmp_path.glob("coverage-*.xml"))) == 1


def test_timing_reader_ranks_files_and_cases(tmp_path: Path) -> None:
    (tmp_path / "junit.xml").write_text(
        """<testsuite>
        <testcase classname="tests.test_fast" name="test_a" time="0.25"/>
        <testcase classname="tests.test_slow" name="test_b" time="2.5"/>
        <testcase classname="tests.test_slow" name="test_c" time="1.0"/>
        </testsuite>"""
    )

    result = rankings(tmp_path, limit=2)

    assert result["files"] == [
        {"file": "tests/test_slow.py", "seconds": 3.5},
        {"file": "tests/test_fast.py", "seconds": 0.25},
    ]
    assert [row["name"] for row in result["tests"]] == [
        "tests.test_slow::test_b",
        "tests.test_slow::test_c",
    ]


def test_every_direct_justfile_pytest_invocation_uses_the_report_launcher() -> None:
    direct = [
        line
        for line in (ROOT / "justfile").read_text().splitlines()
        if not line.lstrip().startswith("#")
        and ("uv run pytest" in line or "all-packages pytest" in line)
    ]
    assert direct == []
