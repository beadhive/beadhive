"""Contracts for direct pytest collection and workspace distribution coverage."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest
from scripts.native_package_tests import UnsafeNativePackagePath, package_test_roots

ROOT = Path(__file__).parents[1]


def _recipe_body(justfile: str, recipe: str) -> list[str]:
    lines = justfile.splitlines()
    start = next(
        i
        for i, line in enumerate(lines)
        if line.startswith(f"{recipe}:") or line.startswith(f"{recipe} ")
    )
    body: list[str] = []
    for line in lines[start + 1 :]:
        if not line.startswith("    "):
            break
        body.append(line.strip())
    return body


def _dependencies(justfile: str, recipe: str) -> set[str]:
    declaration = next(line for line in justfile.splitlines() if line.startswith(f"{recipe}:"))
    return set(declaration.split(":", 1)[1].split())


def _workspace_packages(root: Path) -> list[Path]:
    config = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    patterns = config["tool"]["uv"]["workspace"]["members"]
    return sorted(
        member
        for pattern in patterns
        for member in root.glob(pattern)
        if (member / "pyproject.toml").is_file()
    )


def test_workspace_glob_additions_are_discovered_and_require_tests(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n', encoding="utf-8"
    )
    added = tmp_path / "packages" / "added"
    (added / "tests").mkdir(parents=True)
    (added / "pyproject.toml").write_text("[project]\nname = 'added'\n", encoding="utf-8")
    assert _workspace_packages(tmp_path) == [added]
    assert list((added / "tests").glob("test_*.py")) == []


def test_native_package_test_paths_reject_shell_unsafe_names(tmp_path: Path) -> None:
    (tmp_path / "packages" / "unsafe name" / "tests").mkdir(parents=True)

    with pytest.raises(UnsafeNativePackagePath, match="unsafe for shell expansion"):
        package_test_roots(tmp_path)


def test_workspace_packages_are_all_tested_and_built_by_the_gate() -> None:
    members = _workspace_packages(ROOT)
    assert members, "workspace member glob resolved no packages"
    for member in members:
        tests = list((member / "tests").glob("test_*.py"))
        assert tests, f"workspace package has no pytest coverage: {member.relative_to(ROOT)}"
        metadata = tomllib.loads((member / "pyproject.toml").read_text(encoding="utf-8"))
        assert metadata["build-system"]["build-backend"] == "hatchling.build"

    manifest = json.loads(
        (ROOT / "scripts" / "pants_proven_tests.json").read_text(encoding="utf-8")
    )
    for test_path in manifest["tests"]:
        if test_path.startswith("tests/"):
            assert (ROOT / test_path).is_file(), f"graduated core test missing: {test_path}"

    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    package_gate = "\n".join(_recipe_body(justfile, "packages-check"))
    assert "--all-packages" not in package_gate
    assert "just release-smoke-check" not in package_gate
    assert "$(uv run --no-sync python scripts/native_package_tests.py --relative)" in package_gate
    for package in (
        "beadhive-package-template",
        "beadhive-core",
        "beadhive-plugins",
        "beadhive-worktrees",
    ):
        assert f"uv build --package {package}" in package_gate
    assert "uv build --package beadhive-bd-cli" not in package_gate
    assert "uv build --package beadhive-pants" not in package_gate

    # beadhive-bd-cli is carved out of the shared `packages-check` glob (excluded from the
    # `packages/*/tests` pytest collection above) into its own `bd-cli-check` leaf/key
    # (bh-vq34o); confirm the overall "every workspace package is tested by the native gate"
    # invariant still holds through that other recipe.
    bd_cli_gate = "\n".join(_recipe_body(justfile, "bd-cli-check"))
    assert "--all-packages" not in bd_cli_gate
    assert "--package beadhive-bd-cli" in bd_cli_gate
    assert (
        "python scripts/pytest_with_report.py -n auto packages/beadhive-bd-cli/tests" in bd_cli_gate
    )
    assert "uv build --package beadhive-bd-cli" in bd_cli_gate

    root_workspace_gate = "\n".join(_recipe_body(justfile, "root-workspace-check"))
    assert "--all-packages" not in root_workspace_gate
    assert "packages/beadhive-pants/tests" in root_workspace_gate
    assert "uv build --package beadhive-pants" in root_workspace_gate
    assert "just release-smoke-check" in root_workspace_gate

    beads_client_gate = "\n".join(_recipe_body(justfile, "beads-client-check"))
    assert "packages/beadhive-beads-client/tests" in beads_client_gate
    assert "uv build --package beadhive-beads-client" in beads_client_gate

    complete_gate = "\n".join(
        (
            package_gate,
            beads_client_gate,
            bd_cli_gate,
            root_workspace_gate,
        )
    )
    for package in (
        "beadhive-package-template",
        "beadhive-bd-cli",
        "beadhive-beads-client",
        "beadhive-core",
        "beadhive-pants",
        "beadhive-plugins",
        "beadhive-worktrees",
    ):
        assert complete_gate.count(f"uv build --package {package}") == 1, package

    discovered = {path.parent.name for path in package_test_roots(ROOT)}
    assert discovered == {
        "_template",
        "beadhive-core",
        "beadhive-plugins",
        "beadhive-worktrees",
    }


def test_native_test_recipes_collect_all_core_tests_without_pants_filtering() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    fast = "\n".join(_recipe_body(justfile, "test"))
    native = "\n".join(_recipe_body(justfile, "stateful-native"))
    composition = "\n".join(_recipe_body(justfile, "root-composition-native"))
    integration = "\n".join(_recipe_body(justfile, "test-integration-land"))
    for body in (fast, native, composition, integration):
        assert "pytest" in body
        assert "tests" in body
        assert "pants_ci.py" not in body
    assert "--ignore" not in fast
    assert "--ignore" not in integration
    assert "root_composition_tests.py --ignore-args" in native
    assert "root_composition_tests.py" in composition
    assert "root-composition-validate" in _dependencies(justfile, "stateful-native")
    assert "root-composition-validate" in _dependencies(justfile, "root-composition-native")
    assert "root-composition-validate" in _dependencies(justfile, "root-workspace-check")
    assert {"stateful-native", "root-composition-native"} <= _dependencies(justfile, "check-native")
    assert '-m "integration"' in integration


def test_proven_manifest_enforcement_stays_in_optional_pants_profile() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    native_workspace = "\n".join(_recipe_body(justfile, "root-workspace-check"))
    pants = "\n".join(_recipe_body(justfile, "stateful-pants"))
    package_tests = (ROOT / "packages" / "beadhive-pants" / "tests" / "test_package.py").read_text(
        encoding="utf-8"
    )

    assert '-m "not pants_profile" packages/beadhive-pants/tests' in native_workspace
    assert "uv run python scripts/pants_ci.py all" in pants
    assert (
        "@pytest.mark.pants_profile\n"
        "def test_proven_manifest_explicitly_declares_every_package_test()" in package_tests
    )


@pytest.mark.parametrize("recipe", ("root-composition-native", "root-workspace-check"))
def test_root_composition_render_failure_stops_recipe_before_pytest(
    tmp_path: Path, recipe: str
) -> None:
    just = shutil.which("just")
    assert just
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "uv.log"
    uv = bin_dir / "uv"
    uv.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$*\" >> {log}\nexit 23\n")
    uv.chmod(0o755)

    result = subprocess.run(
        [just, "--justfile", str(ROOT / "justfile"), recipe],
        cwd=ROOT,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"},
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert log.read_text().splitlines() == [
        "run python scripts/root_composition_tests.py --validate-only"
    ]
    assert "pytest" not in log.read_text()


def test_new_package_test_runs_without_pants_manifest_registration(tmp_path: Path) -> None:
    package = tmp_path / "packages" / "example"
    tests = package / "tests"
    tests.mkdir(parents=True)
    (package / "pyproject.toml").write_text(
        '[project]\nname = "example"\nversion = "1"\n', encoding="utf-8"
    )
    added = tests / "test_new_native_package_path.py"
    added.write_text(
        "from pathlib import Path\n\n"
        "def test_package_manifest_is_visible():\n"
        "    manifest = Path(__file__).parents[1] / 'pyproject.toml'\n"
        "    assert 'name = \"example\"' in manifest.read_text()\n",
        encoding="utf-8",
    )
    manifests = (
        tmp_path / "scripts" / "pants_proven_tests.json",
        tmp_path
        / "packages"
        / "beadhive-pants"
        / "src"
        / "beadhive_pants"
        / "data"
        / "proven_tests.json",
    )
    for manifest in manifests:
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('{"tests": {}}\n', encoding="utf-8")
    before = tuple(manifest.read_bytes() for manifest in manifests)

    roots = package_test_roots(tmp_path)
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(roots[0])],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )

    assert roots == (tests,)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed" in result.stdout
    assert tuple(manifest.read_bytes() for manifest in manifests) == before
