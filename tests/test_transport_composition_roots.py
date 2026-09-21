"""Executable boundaries for the four installed transport composition roots."""

from __future__ import annotations

import ast
import importlib
import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

from beadhive import operation_catalog
from beadhive.kernel import operations
from beadhive.transport_inventory import document, registration_drift, validate_registration_drift

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "src" / "beadhive"

ROOTS = {
    "cli": ("bh", "beadhive.bootstrap.cli:main"),
    "mcp": ("bh-mcp", "beadhive.bootstrap.mcp:main"),
    "operator-api": ("bh-host-daemon", "beadhive.bootstrap.host:main"),
    "gateway": ("beadhive-frame-bridge", "beadhive.bootstrap.frame_bridge:main"),
}


def _module_path(module: str) -> Path:
    return SOURCE.parent / Path(*module.split(".")).with_suffix(".py")


def test_installed_transport_entrypoints_are_thin_bootstrap_modules() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text())
    assert project["project"]["scripts"] == {
        script: target for script, target in (spec for spec in ROOTS.values())
    }

    for _surface, (_script, target) in ROOTS.items():
        module, callable_name = target.split(":", 1)
        source = _module_path(module).read_text()
        tree = ast.parse(source)
        assert callable(getattr(importlib.import_module(module), callable_name))
        assert sum(isinstance(node, ast.ClassDef) for node in tree.body) == 0
        assert (
            sum(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) for node in tree.body)
            == 1
        )
        assert len(source.splitlines()) <= 40


def test_production_code_never_depends_back_on_bootstrap() -> None:
    offenders: list[str] = []
    for path in SOURCE.rglob("*.py"):
        if "bootstrap" in path.relative_to(SOURCE).parts:
            continue
        tree = ast.parse(path.read_text(), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            if any(name.startswith("beadhive.bootstrap") for name in names):
                offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


def test_kernel_operation_catalog_is_the_compatibility_identity() -> None:
    assert operation_catalog is operations
    assert operation_catalog.OperationSpec is operations.OperationSpec
    assert operation_catalog.operations is operations.operations

    for relative in (
        "adapters/cli/declarations.py",
        "adapters/cli/projection.py",
        "adapters/cli/tree.py",
        "mcp.py",
        "transport_inventory.py",
    ):
        tree = ast.parse((SOURCE / relative).read_text())
        imported_modules = {
            node.module
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module is not None
        }
        imported_modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert "operation_catalog" not in imported_modules


def test_checked_inventory_names_every_installed_composition_root() -> None:
    roots = document()["composition_roots"]
    assert {
        row["surface"]: (row["console_script"], f"{row['module']}:{row['callable']}")
        for row in roots
    } == {surface: spec for surface, spec in ROOTS.items()}
    assert all(row["responsibility"] == "composition-and-process-lifecycle-only" for row in roots)
    assert all(row["registration_drift_test"] for row in roots)
    for row in roots:
        assert "tests/test_transport_composition_roots.py" in row["test_closure"]
        if row["surface"] in {"gateway", "operator-api"}:
            assert "tests/test_transport_inventory.py" in row["test_closure"]


def test_checked_before_after_evidence_is_current_and_closes_every_root() -> None:
    result = subprocess.run(
        ["uv", "run", "python", "scripts/render_transport_composition_evidence.py", "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    sys.path.insert(0, str(ROOT / "scripts"))
    from scripts import render_transport_composition_evidence as composition_evidence

    evidence = json.loads(composition_evidence.render())
    assert evidence["integration_base"] == "6461a048c1b81ae2e3cf9071cb9384d8579f9ac2"
    assert {row["surface"] for row in evidence["roots"]} == set(ROOTS)
    for row in evidence["roots"]:
        drift = row["registration_drift"]
        assert drift["matches"] is True
        assert drift["delta"] == 0
        assert drift["additions"] == []
        assert drift["removals"] == []
        assert drift["declared"] == drift["observed"]
        assert drift["declared_count"] == drift["observed_count"] == len(drift["declared"])
        exclusions = drift["comparison"]["excluded_declarations"]
        if row["surface"] == "operator-api":
            assert exclusions == ["OPTIONS *"]
            assert drift["comparison"]["projection_declaration_count"] == 13
            assert "OpenAPI" in drift["comparison"]["exclusion_reason"]
        else:
            assert exclusions == []
            assert drift["comparison"]["projection_declaration_count"] == len(drift["declared"])
    assert all(not row["after"]["cycle_member"] for row in evidence["roots"])


def test_before_after_evidence_does_not_require_an_unreachable_git_object(tmp_path) -> None:
    fake_git = tmp_path / "git"
    fake_git.write_text("#!/bin/sh\nexit 99\n", encoding="utf-8")
    fake_git.chmod(0o755)
    result = subprocess.run(
        [sys.executable, "scripts/render_transport_composition_evidence.py", "--check"],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": f"{tmp_path}:{os.environ['PATH']}"},
    )

    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize("surface", ROOTS)
@pytest.mark.parametrize("mutation", ["addition", "removal"])
def test_registration_drift_validation_rejects_additions_and_removals(
    surface: str, mutation: str
) -> None:
    declared = frozenset({"one", "two"})
    observed = (
        frozenset({"one", "two", "undeclared"}) if mutation == "addition" else frozenset({"one"})
    )
    drift = registration_drift(surface, declared, observed)

    assert drift["delta"] == 1
    with pytest.raises(RuntimeError, match=rf"{surface} registration drift"):
        validate_registration_drift(drift)
