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


def impact_receipt(
    path: str = "docs/README.md",
    *,
    invalidated: tuple[str, ...] = (),
    unaffected: tuple[str, ...] = (),
    fallback: str = "",
):
    return routes.ImpactReceipt(
        backend="pants",
        backend_version="2.24.0",
        base_tree="base",
        head_tree="head",
        changed_paths=(path,),
        unowned_paths=(),
        global_inputs_hit=(),
        invalidated_keys=invalidated,
        unaffected_keys=unaffected,
        evidence={},
        fallback_reason=fallback,
    )


def test_classify_uses_impact_receipt_as_its_only_authority() -> None:
    assert routes.classify(impact_receipt(invalidated=("unit",))) == ("pants", None)
    assert routes.classify(impact_receipt(unaffected=("unit",))) == ("avoid", None)
    assert routes.classify(impact_receipt(fallback="pants: query failed")) == (
        "native",
        "pants: query failed",
    )


def test_doc_read_by_test_routes_to_that_test(monkeypatch, capsys) -> None:
    class Resolver:
        def resolve(self, repo, base_rev, head_rev, keys):
            assert base_rev == "BASE"
            assert keys == (routes.UNIT_KEY,)
            return impact_receipt("docs/operator-guide.md", invalidated=("unit",))

    calls: list[list[str]] = []

    def fake_run(command, *, capture=False):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 0, "1 passed\n")

    monkeypatch.setattr(routes, "_run", fake_run)
    assert (
        routes.route(
            "changed",
            "BASE",
            pants="pants",
            native_command=["just", "check"],
            resolver=Resolver(),
        )
        == 0
    )
    assert calls[-1][-1] == routes.QUALIFIED_TEST
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["changed"] == ["docs/operator-guide.md"]


def test_selector_error_cannot_skip_native_fallback(monkeypatch, capsys) -> None:
    calls: list[list[str]] = []

    class BrokenResolver:
        def resolve(self, repo, base_rev, head_rev, keys):
            raise RuntimeError("broken selector")

    def fake_run(command, *, capture=False):
        calls.append(list(command))
        return subprocess.CompletedProcess(command, 17)

    monkeypatch.setattr(routes, "_run", fake_run)
    assert (
        routes.route(
            "changed",
            "HEAD",
            pants="pants",
            native_command=["just", "check"],
            resolver=BrokenResolver(),
        )
        == 17
    )
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


def test_route_launcher_resolves_mise_tool_when_shims_are_not_on_path(tmp_path) -> None:
    resolved = tmp_path / "installed" / "scie-pants"
    resolved.parent.mkdir()
    resolved.write_text("#!/bin/sh\n", encoding="utf-8")
    resolved.chmod(0o755)
    mise = tmp_path / "mise"
    mise.write_text(
        f'#!/bin/sh\n[ "$1" = which ] && [ "$2" = scie-pants ] || exit 41\n'
        f"printf '%s\\n' {resolved!s}\n",
        encoding="utf-8",
    )
    mise.chmod(0o755)

    assert routes.launcher({"PATH": str(tmp_path)}) == str(resolved)
