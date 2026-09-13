"""Executable policy tests for conservative Pants selective routes."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("pants_routes", ROOT / "scripts/pants_routes.py")
assert SPEC and SPEC.loader
routes = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = routes
SPEC.loader.exec_module(routes)


def test_every_unqualified_boundary_falls_back() -> None:
    cases = {
        "tests/conftest.py": "test-infrastructure",
        "uv.lock": "dependency-lock",
        "pants.toml": "build-config",
        "src/beadhive/config.py": "compatibility",
        "src/beadhive/modules/config/contracts.py": "shared-contract",
        "src/beadhive/modules/config/application/plugin_fragments.py": "plugin-dynamic",
        "docs/schemas/example.json": "generated",
        "tests/test_cli.py": "installed-console-script",
        "tests/test_work.py": "ambiguous-integration",
        "some/new_file.py": "unknown-or-unqualified",
    }
    for path, expected in cases.items():
        assert routes.classify([path]) == ("native", expected)


def test_known_unrelated_docs_avoid_and_qualified_edit_selects() -> None:
    assert routes.classify(["docs/README.md"]) == ("avoid", None)
    assert routes.classify([routes.QUALIFIED_SOURCE]) == ("pants", None)
    assert routes.classify([routes.QUALIFIED_TEST]) == ("pants", None)


def test_selector_error_cannot_skip_native_fallback(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []

    def fake_run(command, *, capture=False):
        calls.append(list(command))
        if command[:2] == ["git", "diff"]:
            raise RuntimeError("broken selector")
        return subprocess.CompletedProcess(command, 17)

    monkeypatch.setattr(routes, "_run", fake_run)
    assert routes.route("changed", "HEAD", pants="pants", native_command=["just", "check"]) == 17
    receipt = json.loads(capsys.readouterr().out)
    assert calls[-1] == ["just", "check"]
    assert receipt["fallback_reason"].startswith("selector-error:")
    assert receipt["exit_code"] == 17


def test_pants_execution_failure_is_red_and_observable(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        routes,
        "_run",
        lambda command, *, capture=False: subprocess.CompletedProcess(
            command, 23, "engine failed\n"
        ),
    )
    assert (
        routes.route("leaf", routes.QUALIFIED_TEST, pants="pants", native_command=["just", "check"])
        == 23
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["fallback_reason"] == "pants-execution-failed"
    assert receipt["executed_count"] == receipt["cache_served_count"] == 0


def test_cached_success_and_avoidance_counts(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        routes,
        "_run",
        lambda command, *, capture=False: subprocess.CompletedProcess(
            command, 0, "58 passed (cached locally)\n"
        ),
    )
    assert (
        routes.route("leaf", routes.QUALIFIED_TEST, pants="pants", native_command=["just", "check"])
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["cache_served_count"] == 1
    assert receipt["executed_count"] == 0


def test_dependent_route_records_the_reviewed_transitive_edge(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        routes,
        "_run",
        lambda command, *, capture=False: subprocess.CompletedProcess(command, 0, "58 passed\n"),
    )
    assert (
        routes.route(
            "dependent",
            routes.QUALIFIED_SOURCE,
            pants="pants",
            native_command=["just", "check"],
        )
        == 0
    )
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["transitive_dependents"] == [routes.QUALIFIED_TEST]
    assert receipt["invalidation_cause"] == routes.QUALIFIED_SOURCE


def test_justfile_exposes_all_routes_without_replacing_native() -> None:
    text = (ROOT / "justfile").read_text(encoding="utf-8")
    for recipe in (
        "pants-test-leaf",
        "pants-test-changed",
        "pants-test-dependents",
        "test",
        "check",
        "attest",
    ):
        assert f"{recipe}" in text


def test_route_launcher_discovers_scie_pants_and_missing_is_actionable(tmp_path) -> None:
    scie = tmp_path / "scie-pants"
    scie.write_text("#!/bin/sh\n", encoding="utf-8")
    scie.chmod(0o755)
    assert routes.launcher({"PATH": str(tmp_path)}) == str(scie)
    try:
        routes.launcher({"PATH": ""})
    except RuntimeError as exc:
        assert "mise install scie-pants" in str(exc)
    else:
        raise AssertionError("missing launcher was accepted")
