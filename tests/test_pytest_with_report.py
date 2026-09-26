from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from scripts.pytest_with_report import pytest_argv

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


def test_every_direct_justfile_pytest_invocation_uses_the_report_launcher() -> None:
    direct = [
        line
        for line in (ROOT / "justfile").read_text().splitlines()
        if not line.lstrip().startswith("#")
        and ("uv run pytest" in line or "all-packages pytest" in line)
    ]
    assert direct == []
