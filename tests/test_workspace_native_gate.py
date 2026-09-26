"""Contracts for direct pytest collection and workspace distribution coverage."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

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
    assert "./scripts/hermetic.sh uv sync --locked --offline --all-packages" in package_gate
    assert (
        "--all-packages python scripts/pytest_with_report.py -n auto packages/*/tests"
        in package_gate
    )
    assert "./scripts/hermetic.sh uv build --all-packages --no-build-isolation" in package_gate


def test_native_test_recipes_collect_all_core_tests_without_pants_filtering() -> None:
    justfile = (ROOT / "justfile").read_text(encoding="utf-8")
    fast = "\n".join(_recipe_body(justfile, "test"))
    native = "\n".join(_recipe_body(justfile, "stateful-native"))
    integration = "\n".join(_recipe_body(justfile, "test-integration-land"))
    for body in (fast, native, integration):
        assert "pytest" in body
        assert "tests" in body
        assert "pants_ci.py" not in body
        assert "--ignore" not in body
    assert '-m "integration"' in integration
