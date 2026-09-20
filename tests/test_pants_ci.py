"""Executable contracts for the graduated Pants/native CI partition."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("pants_ci", ROOT / "scripts/pants_ci.py")
assert SPEC and SPEC.loader
pants_ci = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = pants_ci
SPEC.loader.exec_module(pants_ci)


def fixture_repo(tmp_path: Path, *, build_entry: bool = True) -> Path:
    (tmp_path / "scripts").mkdir()
    (tmp_path / "tests/unit").mkdir(parents=True)
    (tmp_path / "tests/unit/test_proven.py").write_text("def test_ok(): pass\n")
    (tmp_path / "tests/test_native.py").write_text("def test_ok(): pass\n")
    (tmp_path / "scripts/pants_proven_tests.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "tests": {
                    "tests/unit/test_proven.py": {
                        "status": "proven",
                        "partition": "pants",
                        "dependencies": ["tests:root-conftest"],
                    }
                },
            }
        )
    )
    source = '"unit/test_proven.py"' if build_entry else '"unit/test_other.py"'
    (tmp_path / "tests/BUILD").write_text(f'{source}\ntags=["pants:proven"]\n')
    return tmp_path


def test_partition_is_disjoint_exhaustive_and_defaults_new_tests_to_native(tmp_path: Path) -> None:
    assert pants_ci.verify_partition(fixture_repo(tmp_path)) == ("tests/unit/test_proven.py",)


def test_manifest_path_without_explicit_build_ownership_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(pants_ci.PartitionError, match="explicit tests/BUILD override"):
        pants_ci.verify_partition(fixture_repo(tmp_path, build_entry=False))


def test_affected_uses_transitive_query_and_only_returns_graduated_sources(monkeypatch) -> None:
    seen: list[str] = []

    def run(command, **kwargs):
        seen.extend(command)
        return subprocess.CompletedProcess(
            command,
            0,
            json.dumps(
                [
                    {"sources": ["src/beadhive/x.py"]},
                    {"sources": ["tests/unit/test_proven.py"]},
                ]
            ),
            "",
        )

    monkeypatch.setattr(pants_ci, "launcher", lambda: "pants")
    monkeypatch.setattr(subprocess, "run", run)
    assert pants_ci.affected("integration-base", ("tests/unit/test_proven.py",)) == (
        "tests/unit/test_proven.py",
    )
    assert "--changed-since=integration-base" in seen
    assert "--changed-dependents=transitive" in seen


def test_native_runner_ignores_exactly_the_graduated_partition(monkeypatch) -> None:
    commands: list[list[str]] = []

    def run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(subprocess, "run", run)
    assert pants_ci.run_native(["-m", "not integration"], ["tests/unit/test_proven.py"]) == 0
    assert "--ignore=tests/unit/test_proven.py" in commands[0]


def test_pants_receipt_reports_cache_reuse(monkeypatch, capsys) -> None:
    monkeypatch.setattr(pants_ci, "launcher", lambda: "pants")
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(
            command, 0, "target succeeded (cached locally)\n", ""
        ),
    )
    assert pants_ci.run_pants(["tests/unit/test_proven.py"], action="complete-closure") == 0
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["cache_served"] == 1
    assert receipt["selected"] == 1
