from __future__ import annotations

from pathlib import Path

import pytest
from scripts.check_native_impact_map import check

from beadhive.adapters.impact_paths import (
    PACKAGE_TESTS,
    ROOT_COMPOSITION,
    PathsImpactBackend,
    root_composition_package_patterns,
)
from beadhive.bootstrap.impact import attest_keys
from beadhive.modules.config.contracts import AttestConfig
from beadhive.modules.work.contracts.impact_resolution import FailClosedResolver
from beadhive.modules.work.domain.impact import AttestKey, ChangedPath


def _repo(tmp_path: Path, *, root_depends_on_core: bool = True) -> Path:
    repo = tmp_path / "repo"
    core = repo / "packages/beadhive-core"
    client = repo / "packages/beadhive-pants"
    core.mkdir(parents=True)
    client.mkdir(parents=True)
    dependency = 'dependencies = ["beadhive-core"]\n' if root_depends_on_core else ""
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "root"\nversion = "1"\n'
        f"{dependency}"
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
    )
    (core / "pyproject.toml").write_text('[project]\nname = "beadhive-core"\nversion = "1"\n')
    (client / "pyproject.toml").write_text('[project]\nname = "beadhive-pants"\nversion = "1"\n')
    return repo


def _attest(*, composition: str = ROOT_COMPOSITION) -> AttestConfig:
    return AttestConfig.model_validate(
        {
            "impact": {"backend": "paths"},
            "keys": [
                {
                    "name": "stateful",
                    "cmd": "just attest-stateful",
                    "selectors": {"paths": "src/*\ntests/*"},
                },
                {
                    "name": "integration",
                    "cmd": "just attest-integration",
                    "selectors": {"paths": "src/*\ntests/*"},
                },
                {
                    "name": "demos",
                    "cmd": "just attest-demos",
                    "selectors": {"paths": "src/*\nscripts/*"},
                },
                {
                    "name": "root-composition",
                    "cmd": "just attest-root-composition",
                    "selectors": {
                        "paths": f"src/*\ntests/*\nscripts/*\nREADME.md\n"
                        f"docs/design/*.md\npackages/_template/*\n{composition}"
                    },
                },
                {
                    "name": "packages",
                    "cmd": "just attest-packages",
                    "selectors": {
                        "paths": "\n".join(
                            (
                                "packages/_template/*",
                                "packages/beadhive-beads-client/*",
                                "packages/beadhive-core/*",
                                "packages/beadhive-plugins/*",
                                "packages/beadhive-worktrees/*",
                                "pyproject.toml",
                                "uv.lock",
                                "justfile",
                                "scripts/*",
                            )
                        )
                    },
                },
                {
                    "name": "bd-cli",
                    "cmd": "just attest-bd-cli",
                    "selectors": {
                        "paths": "\n".join(
                            (
                                "packages/beadhive-bd-cli/*",
                                "packages/beadhive-core/*",
                                "packages/beadhive-beads-client/*",
                                "pyproject.toml",
                                "uv.lock",
                                "justfile",
                                "scripts/*",
                            )
                        )
                    },
                },
            ],
        }
    )


def test_optional_extras_count_as_root_workspace_dependencies(tmp_path: Path) -> None:
    repo = _repo(tmp_path, root_depends_on_core=False)
    with (repo / "pyproject.toml").open("a") as manifest:
        manifest.write('[project.optional-dependencies]\ncore = ["beadhive-core"]\n')

    errors = check(repo, _attest(composition="src/*"), ("packages/beadhive-core/core.py",))

    assert any(error.startswith("root-composition: root depends") for error in errors)


def test_optional_dependency_change_selects_root_composition(tmp_path: Path) -> None:
    repo = _repo(tmp_path, root_depends_on_core=False)
    with (repo / "pyproject.toml").open("a") as manifest:
        manifest.write('[project.optional-dependencies]\ncore = ["beadhive-core"]\n')
    keys = (
        AttestKey(
            "root-composition",
            "just attest-root-composition",
            selectors={"paths": ROOT_COMPOSITION},
        ),
        AttestKey(
            "packages", "just attest-packages", selectors={"paths": "packages/beadhive-core/*"}
        ),
    )

    receipt = FailClosedResolver(
        PathsImpactBackend(), _Diff("packages/beadhive-core/optional.py")
    ).resolve(str(repo), "base", "head", keys)

    assert receipt.invalidated_keys == ("packages", "root-composition")
    assert receipt.fallback_reason == ""


def test_coverage_rejects_unmatched_tracked_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    errors = check(repo, _attest(), ("packages/beadhive-core/core.py", "unowned/file.xyz"))
    assert any("unowned/file.xyz" in error for error in errors)


def test_root_dependency_requires_derived_rule_on_root_composition(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    errors = check(repo, _attest(composition="src/*"), ("packages/beadhive-core/core.py",))
    assert any(error.startswith("root-composition: root depends") for error in errors)


def test_non_dependency_package_needs_only_packages_rule(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    package = repo / "packages" / "beadhive-plugins"
    package.mkdir()
    (package / "pyproject.toml").write_text('[project]\nname = "beadhive-plugins"\nversion = "1"\n')

    assert check(repo, _attest(), ("packages/beadhive-plugins/plugin.py",)) == []


def test_new_package_path_is_uncovered_until_cataloged(tmp_path: Path) -> None:
    repo = _repo(tmp_path)

    errors = check(repo, _attest(), ("packages/new-package/module.py",))
    receipt = FailClosedResolver(
        PathsImpactBackend(), _Diff("packages/new-package/module.py")
    ).resolve(str(repo), "base", "head", attest_keys(_attest()))

    assert errors == ["tracked paths have no native impact owner: packages/new-package/module.py"]
    assert receipt.invalidated_keys == (
        "bd-cli",
        "demos",
        "integration",
        "packages",
        "root-composition",
        "stateful",
    )
    assert receipt.unaffected_keys == ()
    assert receipt.unowned_paths == ("packages/new-package/module.py",)
    assert {evidence.reason for evidence in receipt.evidence.values()} == {"unowned-path"}


class _Diff:
    def __init__(self, path: str = "packages/beadhive-pants/plugin.py") -> None:
        self.path = path

    def tree_of(self, _repo: str, rev: str) -> str:
        return rev

    def changed_paths(self, _repo: str, _base: str, _head: str):
        return (ChangedPath(self.path),)


def test_root_dependency_change_selects_packages_and_root_composition_only(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    keys = (
        AttestKey("stateful", "just attest-stateful", selectors={"paths": "src/*"}),
        AttestKey("integration", "just attest-integration", selectors={"paths": "src/*"}),
        AttestKey("demos", "just attest-demos", selectors={"paths": "src/*"}),
        AttestKey(
            "root-composition",
            "just attest-root-composition",
            selectors={"paths": ROOT_COMPOSITION},
        ),
        AttestKey(
            "packages", "just attest-packages", selectors={"paths": "packages/beadhive-core/*"}
        ),
    )
    receipt = FailClosedResolver(
        PathsImpactBackend(), _Diff("packages/beadhive-core/core.py")
    ).resolve(str(repo), "base", "head", keys)
    assert receipt.invalidated_keys == ("packages", "root-composition")
    assert receipt.unaffected_keys == ("demos", "integration", "stateful")


def test_bd_cli_change_selects_dedicated_key_and_root_composition(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    package = repo / "packages" / "beadhive-bd-cli"
    package.mkdir()
    (package / "pyproject.toml").write_text('[project]\nname = "beadhive-bd-cli"\nversion = "1"\n')
    with (repo / "pyproject.toml").open("a") as manifest:
        manifest.write('[dependency-groups]\ncli = ["beadhive-bd-cli"]\n')

    receipt = FailClosedResolver(
        PathsImpactBackend(), _Diff("packages/beadhive-bd-cli/src/route.py")
    ).resolve(str(repo), "base", "head", attest_keys(_attest()))

    assert receipt.invalidated_keys == ("bd-cli", "root-composition")
    assert "packages" in receipt.unaffected_keys
    assert receipt.fallback_reason == ""


@pytest.mark.parametrize(
    ("path", "expected"),
    (
        (
            "packages/beadhive-core/src/change.py",
            ("bd-cli", "packages", "root-composition"),
        ),
        (
            "packages/beadhive-beads-client/src/change.py",
            ("bd-cli", "packages", "root-composition"),
        ),
        (
            "packages/beadhive-worktrees/src/change.py",
            ("packages", "root-composition"),
        ),
        (
            "packages/beadhive-bd-cli/src/change.py",
            ("bd-cli", "root-composition"),
        ),
    ),
)
def test_realistic_package_dependency_selection(path: str, expected: tuple[str, ...]) -> None:
    root = Path(__file__).parents[1]

    receipt = FailClosedResolver(PathsImpactBackend(), _Diff(path)).resolve(
        str(root), "base", "head", attest_keys(_attest())
    )

    assert receipt.invalidated_keys == expected
    assert receipt.fallback_reason == ""


@pytest.mark.parametrize("path", ("pyproject.toml", "uv.lock"))
def test_root_dependency_metadata_invalidates_bd_cli(path: str) -> None:
    root = Path(__file__).parents[1]

    receipt = FailClosedResolver(PathsImpactBackend(), _Diff(path)).resolve(
        str(root), "base", "head", attest_keys(_attest())
    )

    assert "bd-cli" in receipt.invalidated_keys
    assert receipt.fallback_reason == ""


@pytest.mark.parametrize(
    "path",
    (
        "pyproject.toml",
        "uv.lock",
        "justfile",
        "scripts/hermetic.sh",
        "scripts/test-watchdog.py",
        "scripts/pytest_with_report.py",
        "scripts/native_package_tests.py",
    ),
)
def test_shared_package_gate_inputs_invalidate_both_package_keys(path: str) -> None:
    root = Path(__file__).parents[1]

    receipt = FailClosedResolver(PathsImpactBackend(), _Diff(path)).resolve(
        str(root), "base", "head", attest_keys(_attest())
    )

    assert {"packages", "bd-cli"} <= set(receipt.invalidated_keys)
    assert receipt.fallback_reason == ""


def test_root_source_invalidates_root_composition_not_packages() -> None:
    root = Path(__file__).parents[1]

    receipt = FailClosedResolver(
        PathsImpactBackend(), _Diff("src/beadhive/bootstrap/cli.py")
    ).resolve(str(root), "base", "head", attest_keys(_attest()))

    assert "root-composition" in receipt.invalidated_keys
    assert "packages" in receipt.unaffected_keys
    assert "bd-cli" in receipt.unaffected_keys
    assert receipt.fallback_reason == ""


def test_release_readme_invalidates_root_composition_only() -> None:
    root = Path(__file__).parents[1]

    receipt = FailClosedResolver(PathsImpactBackend(), _Diff("README.md")).resolve(
        str(root), "base", "head", attest_keys(_attest())
    )

    assert receipt.invalidated_keys == ("root-composition",)
    assert receipt.fallback_reason == ""


def test_worktree_compatibility_ledger_invalidates_root_composition() -> None:
    root = Path(__file__).parents[1]

    receipt = FailClosedResolver(
        PathsImpactBackend(), _Diff("docs/design/worktree-compatibility-removal-ledger.md")
    ).resolve(str(root), "base", "head", attest_keys(_attest()))

    assert receipt.invalidated_keys == ("root-composition",)
    assert receipt.fallback_reason == ""


def test_dependency_group_counts_as_root_workspace_dependency(tmp_path: Path) -> None:
    repo = _repo(tmp_path, root_depends_on_core=False)
    with (repo / "pyproject.toml").open("a") as manifest:
        manifest.write('[dependency-groups]\ncore = ["beadhive-core"]\n')

    assert root_composition_package_patterns(repo) == ("packages/beadhive-core/*",)


def test_wheel_vendored_root_counts_as_root_workspace_dependency(tmp_path: Path) -> None:
    repo = _repo(tmp_path, root_depends_on_core=False)
    with (repo / "pyproject.toml").open("a") as manifest:
        manifest.write(
            '[tool.hatch.build.targets.wheel]\npackages = ["src/root", '
            '"packages/beadhive-core/src/beadhive_core"]\n'
        )

    assert root_composition_package_patterns(repo) == ("packages/beadhive-core/*",)


def test_real_root_composition_maps_all_consumed_workspace_packages() -> None:
    root = Path(__file__).parents[1]
    expected = {f"packages/{package}/*" for package in PACKAGE_TESTS}

    assert set(root_composition_package_patterns(root)) == expected
    assert expected == {
        "packages/beadhive-bd-cli/*",
        "packages/beadhive-beads-client/*",
        "packages/beadhive-core/*",
        "packages/beadhive-pants/*",
        "packages/beadhive-plugins/*",
        "packages/beadhive-worktrees/*",
    }
    key = AttestKey(
        "root-composition",
        "just attest-root-composition",
        selectors={"paths": ROOT_COMPOSITION},
    )
    for package in PACKAGE_TESTS:
        receipt = FailClosedResolver(
            PathsImpactBackend(), _Diff(f"packages/{package}/src/change.py")
        ).resolve(str(root), "base", "head", (key,))
        assert receipt.invalidated_keys == ("root-composition",), package
        assert receipt.fallback_reason == "", package


def test_root_change_still_selects_every_root_key(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    keys = tuple(
        AttestKey(name, f"just attest-{name}", selectors={"paths": "src/*"})
        for name in (
            "unit",
            "stateful",
            "integration",
            "architecture-contracts",
            "demos",
            "root-composition",
        )
    )
    receipt = FailClosedResolver(PathsImpactBackend(), _Diff("src/beadhive/work.py")).resolve(
        str(repo), "base", "head", keys
    )
    assert receipt.invalidated_keys == (
        "architecture-contracts",
        "demos",
        "integration",
        "root-composition",
        "stateful",
        "unit",
    )
    assert receipt.unaffected_keys == ()


def test_unresolvable_root_composition_falls_back_to_full_selection(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / "pyproject.toml").write_text("not valid toml = [")
    keys = (
        AttestKey("stateful", "just attest-stateful", selectors={"paths": "src/*"}),
        AttestKey(
            "root-composition",
            "just attest-root-composition",
            selectors={"paths": ROOT_COMPOSITION},
        ),
        AttestKey("packages", "just attest-packages", selectors={"paths": "packages/*"}),
    )
    receipt = FailClosedResolver(PathsImpactBackend(), _Diff()).resolve(
        str(repo), "base", "head", keys
    )
    assert receipt.invalidated_keys == ("packages", "root-composition", "stateful")
    assert receipt.unaffected_keys == ()
    assert "TOMLDecodeError" in receipt.fallback_reason


def test_missing_package_test_mapping_falls_back_to_full_selection(tmp_path: Path) -> None:
    repo = _repo(tmp_path, root_depends_on_core=False)
    missing = repo / "packages" / "missing"
    missing.mkdir()
    (missing / "pyproject.toml").write_text(
        '[project]\nname = "beadhive-unregistered-root-dependency"\nversion = "1"\n'
    )
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "root"\nversion = "1"\n'
        'dependencies = ["beadhive-unregistered-root-dependency"]\n'
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
    )
    keys = (
        AttestKey("stateful", "just attest-stateful", selectors={"paths": "src/*"}),
        AttestKey(
            "root-composition",
            "just attest-root-composition",
            selectors={"paths": ROOT_COMPOSITION},
        ),
        AttestKey("packages", "just attest-packages", selectors={"paths": "packages/*"}),
    )

    receipt = FailClosedResolver(PathsImpactBackend(), _Diff("packages/missing/new.py")).resolve(
        str(repo), "base", "head", keys
    )

    assert receipt.invalidated_keys == ("packages", "root-composition", "stateful")
    assert receipt.unaffected_keys == ()
    assert "lack PACKAGE_TESTS mappings" in receipt.fallback_reason

    errors = check(repo, _attest(), ("packages/missing/new.py",))
    assert errors == [
        "root-composition impact is unresolved: root workspace dependencies lack "
        "PACKAGE_TESTS mappings: beadhive-unregistered-root-dependency"
    ]
