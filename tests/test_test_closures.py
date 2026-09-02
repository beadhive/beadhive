"""Registry, impact-selection, and drift tests for advisory module closures."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "test_closures_script", ROOT / "scripts" / "test_closures.py"
)
assert SPEC is not None and SPEC.loader is not None
test_closures = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = test_closures
SPEC.loader.exec_module(test_closures)


def _replace_closure(registry, closure_id, **changes):
    return replace(
        registry,
        closures=tuple(
            replace(closure, **changes) if closure.id == closure_id else closure
            for closure in registry.closures
        ),
    )


def test_checked_registry_is_complete_and_keeps_full_gates_authoritative():
    registry = test_closures.load_registry()

    assert test_closures.validate_registry(registry) == ()
    assert registry.full_gate == "just check"
    assert registry.release_gate == "just check-all"
    assert len(registry.closures) == 23
    assert sum(closure.status == "present" for closure in registry.closures) == 21
    assert sum(closure.status == "absent" for closure in registry.closures) == 2


def test_capability_module_closures_survive_workstream_composition():
    closures = test_closures.load_registry().by_id()

    assert closures["module.agents"].status == "present"
    assert closures["module.agents"].owner_path == "src/beadhive/modules/agents"
    assert closures["module.config"].status == "present"
    assert closures["module.config"].owner_path == "src/beadhive/modules/config"
    assert closures["module.hives"].status == "present"
    assert closures["module.hives"].owner_path == "src/beadhive/modules/hives"
    assert closures["module.worktrees"].status == "present"
    assert closures["module.worktrees"].owner_path == "src/beadhive/modules/worktrees"
    assert closures["module.work"].status == "present"
    assert closures["module.work"].owner_path == "src/beadhive/modules/work"
    assert {"contract.agent-launch", "config.pure", "config.store", "config.fragments"} <= set(
        closures
    )


def test_impact_selection_unions_direct_shared_contract_and_reverse_dependency_tests():
    closure = test_closures.load_registry().by_id()["kernel"]

    assert "tests/test_operation_catalog.py" in closure.selectors
    assert "tests/unit/testing/test_conformance_testkit.py" in closure.selectors
    assert "tests/test_cli_projection.py" in closure.selectors
    assert len(closure.selectors) == len(set(closure.selectors))


def test_herdr_closure_owns_typed_integration_and_compatibility_surfaces():
    closure = test_closures.load_registry().by_id()["plugin.herdr"]

    assert closure.owner_path == "src/beadhive/herdr_plugin.py"
    assert "src/beadhive/integrations/herdr/**/*.py" in closure.source_paths
    assert "tests/unit/integrations/test_herdr_agent_lifecycle_adapter.py" in closure.tests
    assert "tests/unit/integrations/test_herdr_independence.py" in closure.tests
    assert "tests/test_herdr_plugin.py" in closure.tests
    assert "tests/test_herdr_presentation.py" in closure.tests


def test_marker_selected_closure_runs_supplemental_impact_tests_separately():
    closure = test_closures.load_registry().by_id()["integration"]

    commands = test_closures._pytest_commands(closure, collect_only=False)

    assert len(commands) == 2
    assert commands[0][-1] == "tests"
    assert commands[0][4:8] == ("-n", "auto", "-m", "integration")
    assert commands[0].count("--deselect") == 2
    assert commands[1][-1] == "tests/unit/testing/test_conformance_testkit.py"


def test_new_registered_plugin_without_closure_fails_drift_check(tmp_path):
    plugin = tmp_path / "src" / "beadhive" / "new_plugin.py"
    plugin.parent.mkdir(parents=True)
    plugin.write_text("PLUGIN = object()\n")

    errors = test_closures.validate_registry(test_closures.load_registry(), tmp_path)

    assert "registered plugin 'src/beadhive/new_plugin.py' has no declared test closure" in errors


def test_module_directory_cannot_remain_declared_absent(tmp_path):
    (tmp_path / "src" / "beadhive" / "modules" / "planning").mkdir(parents=True)

    errors = test_closures.validate_registry(test_closures.load_registry(), tmp_path)

    assert "registered module 'planning' is incorrectly declared absent" in errors
    assert (
        "module closure 'planning' is absent but 'src/beadhive/modules/planning' exists" in errors
    )


def test_new_registered_module_without_closure_fails_drift_check(tmp_path):
    (tmp_path / "src" / "beadhive" / "modules" / "billing").mkdir(parents=True)

    errors = test_closures.validate_registry(test_closures.load_registry(), tmp_path)

    assert "registered module 'billing' has no declared test closure" in errors


@pytest.mark.parametrize(
    "selector",
    [
        "--version",
        "README.md",
        "../tests/test_plugins.py",
        "/tmp/test_plugins.py",
        "tests/../README.md",
        "tests\\test_plugins.py",
        "tests/closures.toml",
        "tests/test_plugins.py::",
    ],
)
def test_unsafe_or_non_test_pytest_selector_fails_closed(selector):
    registry = _replace_closure(test_closures.load_registry(), "plugin.herdr", tests=(selector,))

    errors = test_closures.validate_registry(registry)

    assert any("invalid pytest selector" in error for error in errors)
    with pytest.raises(SystemExit, match="closure-registry-check: FAILED"):
        test_closures._require_valid(registry)


@pytest.mark.parametrize("pytest_args", [("--version",), ("-m", "--version")])
def test_option_like_pytest_argument_fails_closed(pytest_args):
    registry = _replace_closure(
        test_closures.load_registry(), "plugin.herdr", pytest_args=pytest_args
    )

    errors = test_closures.validate_registry(registry)

    assert any("pytest argument" in error for error in errors)


@pytest.mark.parametrize(
    "source_path",
    ["../../outside.py", "/tmp/outside.py", "README.md", "tests\\test_plugins.py"],
)
def test_unsafe_source_path_fails_closed(source_path):
    registry = _replace_closure(
        test_closures.load_registry(), "plugin.herdr", source_paths=(source_path,)
    )

    errors = test_closures.validate_registry(registry)

    assert any("invalid source path" in error for error in errors)


@pytest.mark.parametrize(
    ("relationship_field", "selector_field", "message"),
    [
        (
            "shared_contracts",
            "shared_contract_tests",
            "declares shared contracts but no shared-contract tests",
        ),
        (
            "reverse_dependencies",
            "reverse_dependency_tests",
            "declares reverse dependencies but no reverse-dependent tests",
        ),
    ],
)
def test_relationship_cannot_validate_without_its_tests(
    relationship_field, selector_field, message
):
    closure = test_closures.load_registry().by_id()["plugin.herdr"]
    assert getattr(closure, relationship_field)
    registry = _replace_closure(
        test_closures.load_registry(), "plugin.herdr", **{selector_field: ()}
    )

    errors = test_closures.validate_registry(registry)

    assert f"closure 'plugin.herdr' {message}" in errors


def test_present_closure_cannot_validate_or_run_without_direct_tests():
    registry = _replace_closure(test_closures.load_registry(), "plugin.herdr", tests=())

    errors = test_closures.validate_registry(registry)

    assert "closure 'plugin.herdr' is present but has no direct tests" in errors
    with pytest.raises(SystemExit, match="closure-registry-check: FAILED"):
        test_closures._require_valid(registry)


def test_check_rejects_an_ignored_closure_argument():
    with pytest.raises(SystemExit) as exc_info:
        test_closures.main(["check", "ignored-extra-argument"])

    assert exc_info.value.code == 2


def test_runner_reports_pytest_zero_collection(monkeypatch, capsys):
    closure = test_closures.load_registry().by_id()["plugin.herdr"]
    monkeypatch.setattr(
        test_closures.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 5),
    )

    assert test_closures._pytest(closure, collect_only=False) == 5
    assert "pytest collected/executed zero tests" in capsys.readouterr().err


def test_expected_future_module_requires_an_explicit_registry_row():
    registry = test_closures.load_registry()
    mutated = replace(registry, expected_modules=(*registry.expected_modules, "future"))

    errors = test_closures.validate_registry(mutated)

    assert "expected modules without explicit closure rows: ['future']" in errors


def test_just_check_keeps_the_existing_full_test_selection():
    check_line = next(
        line for line in (ROOT / "justfile").read_text().splitlines() if line.startswith("check:")
    )

    assert (
        check_line == "check: lint lint-md license-check architecture-check wire-schema-compat test"
    )
