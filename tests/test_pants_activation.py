"""Activation and exact-tree Pants attestation contracts."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("pants_attest", ROOT / "scripts/pants_attest.py")
assert SPEC and SPEC.loader
attest = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = attest
SPEC.loader.exec_module(attest)


def test_attest_configuration_is_pinned_coherent_local_and_uv() -> None:
    assert attest.configuration_errors() == []


def test_launcher_override_fails_actionably_when_missing() -> None:
    try:
        attest.launcher({"PANTS_BIN": "/definitely/missing/pants", "PATH": ""})
    except RuntimeError as exc:
        assert "PANTS_BIN is not executable" in str(exc)
    else:
        raise AssertionError("missing launcher was accepted")


def test_launcher_prefers_explicit_then_pants_then_scie_pants(tmp_path) -> None:
    pants = tmp_path / "pants"
    scie = tmp_path / "scie-pants"
    for path in (pants, scie):
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
    assert attest.launcher({"PANTS_BIN": str(scie), "PATH": str(tmp_path)}) == str(scie)
    assert attest.launcher({"PATH": str(tmp_path)}) == str(pants)
    pants.unlink()
    assert attest.launcher({"PATH": str(tmp_path)}) == str(scie)


def test_launcher_resolves_mise_tool_when_shims_are_not_on_path(tmp_path) -> None:
    resolved = tmp_path / "installed" / "scie-pants"
    resolved.parent.mkdir()
    resolved.write_text("#!/bin/sh\n", encoding="utf-8")
    resolved.chmod(0o755)
    mise = tmp_path / "mise"
    mise.write_text(f"#!/bin/sh\nprintf '%s\\n' {resolved!s}\n", encoding="utf-8")
    mise.chmod(0o755)

    assert attest.launcher({"PATH": str(tmp_path)}) == str(resolved)


def test_mise_pins_official_scie_pants_launcher() -> None:
    assert 'scie-pants = "0.13.2"' in (ROOT / ".mise.toml").read_text(encoding="utf-8")


def test_attest_runs_version_shadow_package_then_qualified_tests(monkeypatch, capsys) -> None:
    monkeypatch.setattr(attest, "launcher", lambda: "/pants")
    calls: list[list[str]] = []

    def fake_run(command):
        calls.append(command)
        return 0

    monkeypatch.setattr(attest, "run", fake_run)
    assert attest.main() == 0
    assert calls[0][-1] == "version"
    assert calls[1][-1] == "scripts/pants_shadow_evidence.py"
    assert calls[2][-2:] == ["package", "src/beadhive:bh"]
    assert calls[3][-2:] == ["test", attest.QUALIFIED]
    assert "native check-all follows" in capsys.readouterr().out


def test_disable_switch_invokes_native_and_preserves_failure(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location(
        "pants_routes_activation", ROOT / "scripts/pants_routes.py"
    )
    assert spec and spec.loader
    routes = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = routes
    spec.loader.exec_module(routes)
    monkeypatch.setenv("BH_PANTS_ROUTING", "0")
    monkeypatch.setattr(
        routes, "_run", lambda command, *, capture=False: subprocess.CompletedProcess(command, 19)
    )
    assert (
        routes.route("leaf", routes.QUALIFIED_TEST, pants="pants", native_command=["just", "check"])
        == 19
    )


def test_check_all_requires_pants_attest_and_native_phases() -> None:
    just = (ROOT / "justfile").read_text(encoding="utf-8")
    declaration = next(line for line in just.splitlines() if line.startswith("check-all:"))
    assert "pants-attest" in declaration
    assert "test-integration-land" in declaration
    assert "demo-local-loop" in declaration
    assert os.environ.get("BH_PANTS_ROUTING", "1") == "1"
