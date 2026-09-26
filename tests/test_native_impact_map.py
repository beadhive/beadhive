from __future__ import annotations

from pathlib import Path

from scripts.check_native_impact_map import check

from beadhive.adapters.impact_paths import ROOT_WORKSPACE_PACKAGES, PathsImpactBackend
from beadhive.modules.config.contracts import AttestConfig
from beadhive.modules.work.contracts.impact_resolution import FailClosedResolver
from beadhive.modules.work.domain.impact import AttestKey, ChangedPath


def _repo(tmp_path: Path, *, root_depends_on_core: bool = True) -> Path:
    repo = tmp_path / "repo"
    core = repo / "packages/core"
    client = repo / "packages/client"
    core.mkdir(parents=True)
    client.mkdir(parents=True)
    dependency = 'dependencies = ["beadhive-core"]\n' if root_depends_on_core else ""
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "root"\nversion = "1"\n'
        f"{dependency}"
        '[tool.uv.workspace]\nmembers = ["packages/*"]\n'
    )
    (core / "pyproject.toml").write_text('[project]\nname = "beadhive-core"\nversion = "1"\n')
    (client / "pyproject.toml").write_text('[project]\nname = "beadhive-client"\nversion = "1"\n')
    return repo


def test_optional_extras_count_as_root_workspace_dependencies(tmp_path: Path) -> None:
    repo = _repo(tmp_path, root_depends_on_core=False)
    with (repo / "pyproject.toml").open("a") as manifest:
        manifest.write('[project.optional-dependencies]\ncore = ["beadhive-core"]\n')

    errors = check(repo, _attest(stateful="src/*"), ("packages/core/core.py",))

    assert any(error.startswith("stateful: root depends") for error in errors)


def _attest(*, stateful: str = ROOT_WORKSPACE_PACKAGES) -> AttestConfig:
    return AttestConfig.model_validate(
        {
            "impact": {"backend": "paths"},
            "keys": [
                {
                    "name": "stateful",
                    "cmd": "just attest-stateful",
                    "selectors": {"paths": stateful},
                },
                {
                    "name": "integration",
                    "cmd": "just attest-integration",
                    "selectors": {"paths": ROOT_WORKSPACE_PACKAGES},
                },
                {
                    "name": "demos",
                    "cmd": "just attest-demos",
                    "selectors": {"paths": ROOT_WORKSPACE_PACKAGES},
                },
                {
                    "name": "packages",
                    "cmd": "just attest-packages",
                    "selectors": {"paths": "packages/*"},
                },
            ],
        }
    )


def test_coverage_rejects_unmatched_tracked_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    errors = check(repo, _attest(), ("packages/core/core.py", "unowned/file.xyz"))
    assert any("unowned/file.xyz" in error for error in errors)


def test_root_dependency_requires_derived_rule_on_root_consumers(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    errors = check(repo, _attest(stateful="src/*"), ("packages/core/core.py",))
    assert any(error.startswith("stateful: root depends") for error in errors)


def test_non_dependency_package_needs_only_packages_rule(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert check(repo, _attest(), ("packages/client/client.py",)) == []


class _Diff:
    def tree_of(self, _repo: str, rev: str) -> str:
        return rev

    def changed_paths(self, _repo: str, _base: str, _head: str):
        return (ChangedPath("packages/client/client.py"),)


def test_client_only_change_selects_packages_but_not_stateful(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    keys = (
        AttestKey(
            "stateful",
            "just attest-stateful",
            selectors={"paths": ROOT_WORKSPACE_PACKAGES},
        ),
        AttestKey("packages", "just attest-packages", selectors={"paths": "packages/*"}),
    )
    receipt = FailClosedResolver(PathsImpactBackend(), _Diff()).resolve(
        str(repo), "base", "head", keys
    )
    assert receipt.invalidated_keys == ("packages",)
    assert receipt.unaffected_keys == ("stateful",)
