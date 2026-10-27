"""Independence evidence for the intake module and its testing kit.

Three checks, none of which needs the CLI, the work layer, an adapter or a bd binary:

* the import and side-effect sentinel (the ``modules/hives`` precedent): the module and
  :mod:`beadhive.testing.intake` load, and the whole contract suite and a service pass run, with
  the CLI, work, plan, bd, config and plugin layers, every HTTP client and every tracker SDK
  refused at import time, and with no file read, process, thread, socket or real sleep allowed;
* gap G7, a module-local AST test: no legacy file except ``intake_services.py`` imports
  ``beadhive.modules.intake.domain`` or ``.application`` (the checker does not scan legacy
  importers), proved by a negative control on a scratch tree and then run on the real tree;
* the enumerable dependency surface: every import in the module is the standard library, the
  module itself, or the tracker port's contracts.
"""

from __future__ import annotations

import ast
import importlib
import importlib.abc
import importlib.metadata
import os
import socket
import subprocess
import sys
import threading
import time
import warnings
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]
_SOURCE_ROOT = _REPO_ROOT / "src"
_MODULE_ROOT = _SOURCE_ROOT / "beadhive" / "modules" / "intake"

_FORBIDDEN_IMPORTS = (
    "beadhive.cli",
    "beadhive.mcp",
    "beadhive.bd",
    "beadhive.config",
    "beadhive.plan",
    "beadhive.plugins",
    "beadhive.run",
    "beadhive.work",
    "beadhive.modules.work",
    "beadhive.modules.planning",
    "beadhive.adapters",
    "beadhive.kernel",
    "beadhive.bootstrap",
    "beadhive.intake_services",
    "beadhive.report",
    "beadhive.triage",
    "beadhive_bd_cli",
    "beadhive_core",
    "fastmcp",
    "typer",
    "httpx",
    "githubkit",
    "gidgethub",
    "github",
)
_UNDER_TEST = ("beadhive.modules.intake", "beadhive.testing.intake")


def test_intake_module_and_kit_run_without_higher_layers_or_effects(monkeypatch) -> None:
    for name in tuple(sys.modules):
        if name.startswith((*_UNDER_TEST, *_FORBIDDEN_IMPORTS)):
            monkeypatch.delitem(sys.modules, name, raising=False)

    class RefuseHigherLayers(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname.startswith(_FORBIDDEN_IMPORTS):
                raise AssertionError(f"intake module imported a higher layer: {fullname}")
            return None

    def forbidden(action):
        def fail(*_args, **_kwargs):
            raise AssertionError(f"intake module attempted {action}")

        return fail

    finder = RefuseHigherLayers()
    sys.meta_path.insert(0, finder)
    monkeypatch.setattr(Path, "read_text", forbidden("filesystem read"))
    monkeypatch.setattr(importlib.metadata, "entry_points", forbidden("plugin discovery"))
    monkeypatch.setattr(socket, "create_connection", forbidden("network connection"))
    monkeypatch.setattr(socket.socket, "connect", forbidden("network connection"))
    monkeypatch.setattr(subprocess, "Popen", forbidden("process spawn"))
    monkeypatch.setattr(subprocess, "run", forbidden("process spawn"))
    monkeypatch.setattr(os, "fork", forbidden("process fork"))
    monkeypatch.setattr(threading.Thread, "start", forbidden("runtime thread start"))
    monkeypatch.setattr(time, "sleep", forbidden("a real sleep"))
    try:
        intake = importlib.import_module("beadhive.modules.intake")
        kit = importlib.import_module("beadhive.testing.intake")
        for case in kit.REPORT_STORE_CASES:
            kit.run_report_store_case(kit.memory_store, case, subject="memory")
        store = kit.memory_store()

        class Labels:
            def violations(self, labels):
                return ()

        service = intake.IntakeService(store, Labels())
        report = service.file_report(
            intake.ReportDraft("a report", "bug", intake.ORIGIN_REPORT, "dev/test")
        )
        assert [item.id for item in service.queue(intake.QueueQuery()).items] == [report]
        assert isinstance(intake.bind_intake(False, lambda: service), intake.NullIntake)
        loaded = {name for name in sys.modules if name.startswith(_FORBIDDEN_IMPORTS)}
        assert not loaded, sorted(loaded)
    finally:
        sys.meta_path.remove(finder)


# -- gap G7: legacy importers of intake internals ----------------------------------------------

_INTERNAL_LAYERS = ("domain", "application")
_COMPOSITION_ROOT = "beadhive/intake_services.py"
_NON_LEGACY_TOPS = {"modules", "kernel", "adapters", "integrations", "bootstrap"}


def _package_of(relative: Path) -> list[str]:
    parts = list(relative.with_suffix("").parts)
    return parts if parts[-1] == "__init__" else parts[:-1]


def _imported_names(path: Path, relative: Path) -> set[str]:
    """Every dotted name a file imports or dynamically imports by literal string."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")  # unrelated sources may carry invalid escape sequences
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    package = _package_of(relative)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = package[: len(package) - node.level + 1] if node.level else []
            module = ".".join([*base, *(node.module.split(".") if node.module else [])])
            names.add(module)
            names.update(f"{module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Call):
            function = node.func
            called = (
                function.attr
                if isinstance(function, ast.Attribute)
                else getattr(function, "id", "")
            )
            if called in {"import_module", "__import__"} and node.args:
                argument = node.args[0]
                if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
                    names.add(argument.value)
    return names


def legacy_importers_of_intake_internals(source_root: Path) -> list[str]:
    """``path: imported name`` for each legacy file (other than the composition root) that
    imports ``beadhive.modules.intake.domain`` / ``.application`` or anything beneath them."""
    offenders: list[str] = []
    for path in sorted(source_root.rglob("*.py")):
        relative = path.relative_to(source_root)
        if relative.parts[0] != "beadhive" or "__pycache__" in relative.parts:
            continue
        if len(relative.parts) > 1 and relative.parts[1] in _NON_LEGACY_TOPS:
            continue
        if relative.as_posix() == _COMPOSITION_ROOT:
            continue
        for name in sorted(_imported_names(path, relative)):
            for layer in _INTERNAL_LAYERS:
                internal = f"beadhive.modules.intake.{layer}"
                if name == internal or name.startswith(f"{internal}."):
                    offenders.append(f"{relative.as_posix()}: {name}")
    return offenders


def _scratch(tmp_path: Path, files: dict[str, str]) -> Path:
    for relative, text in files.items():
        path = tmp_path / "src" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return tmp_path / "src"


@pytest.mark.parametrize(
    ("line", "flagged"),
    [
        ("from beadhive.modules.intake.domain import vocabulary", "beadhive.modules.intake.domain"),
        (
            "import beadhive.modules.intake.application.service",
            "beadhive.modules.intake.application",
        ),
        ("from beadhive.modules.intake import domain", "beadhive.modules.intake.domain"),
        (
            "from beadhive.modules.intake import application as app",
            "beadhive.modules.intake.application",
        ),
        ("from .modules.intake.domain import vocabulary", "beadhive.modules.intake.domain"),
        (
            "from .modules import intake\nfrom .modules.intake import domain",
            "beadhive.modules.intake.domain",
        ),
        (
            "import importlib\nimportlib.import_module('beadhive.modules.intake.application')",
            "beadhive.modules.intake.application",
        ),
    ],
)
def test_g7_negative_control_a_legacy_importer_of_internals_is_found(
    tmp_path: Path, line: str, flagged: str
) -> None:
    source_root = _scratch(
        tmp_path,
        {
            "beadhive/__init__.py": "",
            "beadhive/modules/__init__.py": "",
            "beadhive/legacy_user.py": line + "\n",
        },
    )

    offenders = legacy_importers_of_intake_internals(source_root)

    assert offenders and all(o.startswith("beadhive/legacy_user.py: ") for o in offenders)
    assert any(o.endswith(flagged) or flagged in o for o in offenders), offenders


def test_g7_allows_the_composition_root_the_public_api_and_contracts(tmp_path: Path) -> None:
    source_root = _scratch(
        tmp_path,
        {
            "beadhive/__init__.py": "",
            "beadhive/intake_services.py": (
                "from beadhive.modules.intake.application import IntakeService\n"
                "from beadhive.modules.intake.domain import vocabulary\n"
            ),
            "beadhive/state.py": "from .modules.intake import INTAKE_DIMENSIONS, channel_of\n",
            "beadhive/hooks.py": "from beadhive.modules.intake.contracts import IntakePort\n",
            "beadhive/testing/intake/kit.py": (
                "from beadhive.modules.intake.contracts import ReportStore\n"
            ),
        },
    )

    assert legacy_importers_of_intake_internals(source_root) == []


def test_g7_a_module_layer_is_not_a_legacy_file(tmp_path: Path) -> None:
    """Non-legacy layers are the checker's (direction rules); this test only owns legacy files."""
    source_root = _scratch(
        tmp_path,
        {
            "beadhive/__init__.py": "",
            "beadhive/modules/intake/application/x.py": "from ..domain import models\n",
            "beadhive/adapters/intake/store.py": (
                "from beadhive.modules.intake.domain import models\n"
            ),
        },
    )

    assert legacy_importers_of_intake_internals(source_root) == []


def test_g7_real_tree_has_no_legacy_importer_of_intake_internals() -> None:
    assert (_SOURCE_ROOT / "beadhive" / "state.py").is_file()  # the facade is scanned and clean
    assert legacy_importers_of_intake_internals(_SOURCE_ROOT) == []


# -- the enumerable dependency surface ---------------------------------------------------------


def test_module_imports_only_the_stdlib_itself_and_the_tracker_contracts() -> None:
    allowed_prefixes = ("beadhive.modules.intake", "beadhive.modules.tracker.contracts")
    for path in sorted(_MODULE_ROOT.rglob("*.py")):
        relative = path.relative_to(_SOURCE_ROOT)
        for name in _imported_names(path, relative):
            top = name.split(".")[0]
            if top in sys.stdlib_module_names or top == "__future__":
                continue
            assert name.startswith(allowed_prefixes), f"{relative}: unexpected import {name}"


def test_domain_depends_on_the_standard_library_and_itself_only() -> None:
    for path in sorted((_MODULE_ROOT / "domain").rglob("*.py")):
        relative = path.relative_to(_SOURCE_ROOT)
        for name in _imported_names(path, relative):
            top = name.split(".")[0]
            assert top in sys.stdlib_module_names or name.startswith(
                "beadhive.modules.intake.domain"
            ), f"{relative}: domain imports {name}"


def test_intake_never_imports_the_trackers_application_layer() -> None:
    """Gap G5's contract, as a source-level fact next to the checker's negative control."""
    for path in sorted(_MODULE_ROOT.rglob("*.py")):
        relative = path.relative_to(_SOURCE_ROOT)
        for name in _imported_names(path, relative):
            assert not name.startswith("beadhive.modules.tracker.application"), relative
