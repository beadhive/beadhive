from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "check_package_imports.py"
SPEC = importlib.util.spec_from_file_location("check_package_imports", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _package_dir(root: Path, package: str, import_name: str) -> Path:
    return root / "packages" / package / "src" / import_name


def _make_package(
    root: Path,
    package: str,
    import_name: str,
    *,
    is_plugin: bool = False,
    init: str = "__all__ = []\n",
    modules: dict[str, str] | None = None,
) -> None:
    src = _package_dir(root, package, import_name)
    _write(src / "__init__.py", init)
    if is_plugin:
        _write(src / "plugin.json", "{}\n")
    for name, content in (modules or {}).items():
        _write(src / f"{name}.py", content)


def _core(root: Path, content: str, *, filename: str = "bootstrap.py") -> None:
    _write(root / "src" / "beadhive" / filename, content)


# --- plugin package <-> root -------------------------------------------------------------


def test_plugin_package_may_import_allowlisted_root_contracts(tmp_path: Path) -> None:
    _make_package(
        tmp_path,
        "example",
        "beadhive_example",
        is_plugin=True,
        init="from beadhive.modules.work.contracts.impact import ImpactBackend\n",
    )

    assert MODULE.check(tmp_path) == ()


def test_plugin_package_private_root_imports_name_every_forbidden_edge(tmp_path: Path) -> None:
    _make_package(
        tmp_path,
        "example",
        "beadhive_example",
        is_plugin=True,
        init=(
            "import beadhive.selective_validation\n"
            "from beadhive.modules.work.application import impact\n"
            "from beadhive import bootstrap\n"
        ),
    )

    violations = MODULE.check(tmp_path)

    assert [violation.edge for violation in violations] == [
        "beadhive.selective_validation",
        "beadhive.modules.work.application",
        "beadhive",
    ]
    assert all(
        "plugin packages may use only kernel/module contracts or beadhive.testing" in v.reason
        for v in violations
    )


def test_plugin_package_importing_nothing_from_beadhive_is_accepted(tmp_path: Path) -> None:
    _make_package(
        tmp_path,
        "example",
        "beadhive_example",
        is_plugin=True,
        init="import json\n\n__all__: list[str] = []\n",
    )

    assert MODULE.check(tmp_path) == ()


def test_core_may_not_statically_import_a_plugin_package(tmp_path: Path) -> None:
    _make_package(tmp_path, "example", "beadhive_example", is_plugin=True)
    _core(tmp_path, "from beadhive_example import something\n")

    (violation,) = MODULE.check(tmp_path)

    assert violation.edge == "beadhive_example"
    assert "resolve a plugin package's implementation lazily" in violation.reason


# --- library package <-> root/beadhive --------------------------------------------------


def test_library_package_importing_beadhive_is_rejected_under_any_path(tmp_path: Path) -> None:
    _make_package(
        tmp_path,
        "example",
        "beadhive_example",
        is_plugin=False,
        init=(
            "from beadhive.testing import fixtures\n"
            "from beadhive.modules.work.contracts.impact import ImpactBackend\n"
            "import beadhive\n"
        ),
    )

    violations = MODULE.check(tmp_path)

    assert [violation.edge for violation in violations] == [
        "beadhive.testing",
        "beadhive.modules.work.contracts.impact",
        "beadhive",
    ]
    assert all(
        "library packages may not import beadhive under any path" in v.reason for v in violations
    )


def test_core_may_import_a_librarys_public_surface(tmp_path: Path) -> None:
    _make_package(
        tmp_path,
        "example",
        "beadhive_example",
        is_plugin=False,
        init="__all__ = ['Widget']\n\nclass Widget:\n    pass\n",
    )
    _core(tmp_path, "from beadhive_example import Widget\n")

    assert MODULE.check(tmp_path) == ()


def test_core_importing_a_librarys_private_name_is_rejected(tmp_path: Path) -> None:
    _make_package(
        tmp_path,
        "example",
        "beadhive_example",
        is_plugin=False,
        init="__all__ = ['Widget']\n\nclass Widget:\n    pass\n\nclass _Internal:\n    pass\n",
    )
    _core(tmp_path, "from beadhive_example import _Internal\n")

    (violation,) = MODULE.check(tmp_path)

    assert violation.edge == "beadhive_example"
    assert "public __all__ surface" in violation.reason


def test_core_importing_a_librarys_private_submodule_is_rejected(tmp_path: Path) -> None:
    _make_package(
        tmp_path,
        "example",
        "beadhive_example",
        is_plugin=False,
        modules={"internals": "def helper():\n    return 1\n"},
    )
    _core(tmp_path, "from beadhive_example.internals import helper\n")

    (violation,) = MODULE.check(tmp_path)

    assert violation.edge == "beadhive_example.internals"
    assert "public __all__ surface" in violation.reason


def test_core_importing_a_librarys_public_submodule_is_accepted(tmp_path: Path) -> None:
    _make_package(
        tmp_path,
        "example",
        "beadhive_example",
        is_plugin=False,
        modules={"internals": "__all__ = ['helper']\n\n\ndef helper():\n    return 1\n"},
    )
    _core(tmp_path, "from beadhive_example import internals\n")

    assert MODULE.check(tmp_path) == ()


# --- package <-> package -----------------------------------------------------------------


def test_plugin_package_may_import_a_librarys_public_surface(tmp_path: Path) -> None:
    _make_package(
        tmp_path,
        "a-library",
        "a_library",
        is_plugin=False,
        init="__all__ = ['Widget']\n\nclass Widget:\n    pass\n",
    )
    _make_package(
        tmp_path,
        "a-plugin",
        "a_plugin",
        is_plugin=True,
        init="from a_library import Widget\n",
    )

    assert MODULE.check(tmp_path) == ()


def test_plugin_package_may_not_import_another_plugin_package(tmp_path: Path) -> None:
    _make_package(
        tmp_path,
        "other-plugin",
        "other_plugin",
        is_plugin=True,
        init="__all__ = ['Widget']\n\nclass Widget:\n    pass\n",
    )
    _make_package(
        tmp_path,
        "a-plugin",
        "a_plugin",
        is_plugin=True,
        init="from other_plugin import Widget\n",
    )

    (violation,) = MODULE.check(tmp_path)

    assert violation.edge == "other_plugin"
    assert "plugin packages are leaves" in violation.reason


def test_library_package_importing_another_librarys_private_module_is_rejected(
    tmp_path: Path,
) -> None:
    _make_package(
        tmp_path,
        "a-library",
        "a_library",
        is_plugin=False,
        modules={"internals": "def helper():\n    return 1\n"},
    )
    _make_package(
        tmp_path,
        "b-library",
        "b_library",
        is_plugin=False,
        init="from a_library.internals import helper\n",
    )

    (violation,) = MODULE.check(tmp_path)

    assert violation.edge == "a_library.internals"
    assert "public __all__ surface" in violation.reason


def test_same_distribution_imports_are_not_restricted(tmp_path: Path) -> None:
    # A single distribution can ship more than one top-level import name (as
    # beadhive-beads-client ships both beads_v1_3 and beadhive_beads_client); code within one
    # import name may freely use the other's internals, and a package's own tests may reach
    # into its own private submodules without an __all__.
    _make_package(
        tmp_path,
        "beads-client",
        "generated_client",
        is_plugin=False,
        modules={"internals": "def helper():\n    return 1\n"},
    )
    _make_package(
        tmp_path,
        "beads-client",
        "friendly_client",
        is_plugin=False,
        init="from generated_client.internals import helper\n",
    )

    assert MODULE.check(tmp_path) == ()


# --- the real repository -------------------------------------------------------------------


def test_real_repository_packages_classify_and_pass() -> None:
    packages = {pkg.name: pkg for pkg in MODULE._discover_packages(MODULE.ROOT)}

    assert packages["beadhive-pants"].is_plugin is True
    assert packages["beadhive-beads-client"].is_plugin is False
    assert packages["beadhive-core"].is_plugin is False
    assert packages["_template"].is_plugin is False

    assert MODULE.check(MODULE.ROOT) == ()
