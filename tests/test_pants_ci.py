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

BENCH_SPEC = importlib.util.spec_from_file_location(
    "pants_ci_benchmark", ROOT / "scripts/pants_ci_benchmark.py"
)
assert BENCH_SPEC and BENCH_SPEC.loader
pants_ci_benchmark = importlib.util.module_from_spec(BENCH_SPEC)
sys.modules[BENCH_SPEC.name] = pants_ci_benchmark
BENCH_SPEC.loader.exec_module(pants_ci_benchmark)


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
                    {"target_type": "python_source", "sources": ["src/beadhive/x.py"]},
                    {"target_type": "python_test", "sources": ["tests/unit/test_proven.py"]},
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


def test_proven_only_impact_selects_pants_without_native() -> None:
    route = pants_ci.plan_changed(
        "base",
        ("tests/unit/test_proven.py",),
        changes=("src/beadhive/proven_dependency.py",),
        rows=(
            {"target_type": "python_source", "sources": ["src/beadhive/proven_dependency.py"]},
            {"target_type": "python_test", "sources": ["tests/unit/test_proven.py"]},
        ),
    )
    assert route.pants_tests == ("tests/unit/test_proven.py",)
    assert route.run_native is False
    assert route.run_all_pants is False
    assert route.reason == "affected-proven-tests"


def test_package_test_impact_runs_under_pants_without_a_manifest_entry() -> None:
    route = pants_ci.plan_changed(
        "base",
        ("tests/unit/test_proven.py",),
        changes=("packages/example/src/example/__init__.py",),
        rows=(
            {
                "target_type": "python_source",
                "sources": ["packages/example/src/example/__init__.py"],
            },
            {"target_type": "python_test", "sources": ["packages/example/tests/test_example.py"]},
        ),
    )
    assert route.pants_tests == ("packages/example/tests/test_example.py",)
    assert route.run_native is False
    assert route.run_all_pants is False
    assert route.reason == "affected-proven-tests"


def test_unproven_test_impact_routes_to_native_closure() -> None:
    route = pants_ci.plan_changed(
        "base",
        ("tests/unit/test_proven.py",),
        changes=("src/beadhive/shared.py",),
        rows=(
            {"target_type": "python_test", "sources": ["tests/unit/test_proven.py"]},
            {"target_type": "python_test", "sources": ["tests/test_native.py"]},
        ),
    )
    assert route.pants_tests == ("tests/unit/test_proven.py",)
    assert route.run_native is True
    assert route.run_all_pants is False
    assert route.reason == "affected-unproven-tests"


@pytest.mark.parametrize("changed", [("pants.toml",), ("tests/BUILD",), ("uv.lock",)])
def test_global_inputs_route_to_complete_pants_and_native(changed: tuple[str, ...]) -> None:
    route = pants_ci.plan_changed("base", ("tests/unit/test_proven.py",), changes=changed, rows=())
    assert route.pants_tests == ("tests/unit/test_proven.py",)
    assert route.run_native is True
    assert route.run_all_pants is True
    assert route.reason == "global-input"


def test_unowned_executable_change_fails_closed_to_both_partitions() -> None:
    route = pants_ci.plan_changed(
        "base",
        ("tests/unit/test_proven.py",),
        changes=("scripts/new_tool.py",),
        rows=(),
    )
    assert route.run_native is True
    assert route.run_all_pants is True
    assert route.reason == "unproven-or-unowned-impact"


def test_documentation_only_change_has_no_test_closure() -> None:
    documentation_path = "do" + "cs/guide.md"
    route = pants_ci.plan_changed(
        "base", ("tests/unit/test_proven.py",), changes=(documentation_path,), rows=()
    )
    assert route.pants_tests == ()
    assert route.run_native is False
    assert route.reason == "non-code-only"


def test_analysis_failure_executes_both_complete_partitions(monkeypatch, capsys) -> None:
    commands: list[list[str]] = []

    monkeypatch.setattr(
        pants_ci,
        "plan_changed",
        lambda base, selected: (_ for _ in ()).throw(pants_ci.PartitionError("boom")),
    )
    monkeypatch.setattr(
        pants_ci,
        "run_pants",
        lambda paths, *, action: commands.append(list(paths)) or 0,
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda command, **kwargs: (
            commands.append(list(command)) or subprocess.CompletedProcess(command, 0)
        ),
    )
    assert pants_ci.run_changed("base", ("tests/unit/test_proven.py",)) == 0
    assert commands == [["tests/unit/test_proven.py"], ["just", "stateful-native"]]
    assert "analysis-failed-closed" in capsys.readouterr().out


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


def test_checked_benchmark_evidence_has_honest_sample_counts_and_percentiles() -> None:
    pants_ci_benchmark.check_evidence(pants_ci_benchmark.DEFAULT_EVIDENCE)


def test_benchmark_statistics_do_not_call_singletons_percentiles() -> None:
    assert pants_ci_benchmark.status_for(1) == "pending"
    assert pants_ci_benchmark.status_for(5) == "provisional"
    assert pants_ci_benchmark.status_for(10) == "measured"
    assert pants_ci_benchmark.nearest_rank([29.664, 29.420, 29.604, 30.891, 30.612], 0.9) == 30.891
