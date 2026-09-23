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


def _fixture(tmp_path: Path, *, package_source: str = "", core_source: str = "") -> Path:
    package = tmp_path / "packages" / "example" / "src" / "beadhive_example"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(package_source, encoding="utf-8")
    core = tmp_path / "src" / "beadhive"
    core.mkdir(parents=True)
    (core / "bootstrap.py").write_text(core_source, encoding="utf-8")
    return tmp_path


def test_package_may_import_module_contracts(tmp_path: Path) -> None:
    root = _fixture(
        tmp_path,
        package_source="from beadhive.modules.work.contracts.impact import ImpactBackend\n",
    )

    assert MODULE.check(root) == ()


def test_package_private_imports_name_every_forbidden_edge(tmp_path: Path) -> None:
    root = _fixture(
        tmp_path,
        package_source=(
            "import beadhive.selective_validation\n"
            "from beadhive.modules.work.application import impact\n"
            "from beadhive import bootstrap\n"
        ),
    )

    violations = MODULE.check(root)

    assert [violation.edge for violation in violations] == [
        "beadhive.selective_validation",
        "beadhive.modules.work.application",
        "beadhive",
    ]
    assert all("packages may use only" in violation.reason for violation in violations)


def test_core_may_not_statically_import_a_package_module(tmp_path: Path) -> None:
    root = _fixture(tmp_path, core_source="from beadhive_example import plugin\n")

    (violation,) = MODULE.check(root)

    assert violation.edge == "beadhive_example"
    assert "resolve package implementations lazily" in violation.reason
