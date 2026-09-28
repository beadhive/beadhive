from __future__ import annotations

from pathlib import Path

from scripts.check_native_impact_map import check

from beadhive.adapters.impact_paths import ROOT_COMPOSITION, PathsImpactBackend
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
                    "selectors": {"paths": f"src/*\ntests/*\n{composition}"},
                },
                {
                    "name": "packages",
                    "cmd": "just attest-packages",
                    "selectors": {"paths": "packages/*"},
                },
            ],
        }
    )


def test_optional_extras_count_as_root_workspace_dependencies(tmp_path: Path) -> None:
    repo = _repo(tmp_path, root_depends_on_core=False)
    with (repo / "pyproject.toml").open("a") as manifest:
        manifest.write('[project.optional-dependencies]\ncore = ["beadhive-core"]\n')

    errors = check(repo, _attest(composition="src/*"), ("packages/core/core.py",))

    assert any(error.startswith("root-composition: root depends") for error in errors)


def test_coverage_rejects_unmatched_tracked_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    errors = check(repo, _attest(), ("packages/core/core.py", "unowned/file.xyz"))
    assert any("unowned/file.xyz" in error for error in errors)


def test_root_dependency_requires_derived_rule_on_root_composition(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    errors = check(repo, _attest(composition="src/*"), ("packages/core/core.py",))
    assert any(error.startswith("root-composition: root depends") for error in errors)


def test_non_dependency_package_needs_only_packages_rule(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert check(repo, _attest(), ("packages/client/client.py",)) == []


class _Diff:
    def __init__(self, path: str = "packages/client/client.py") -> None:
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
        AttestKey("packages", "just attest-packages", selectors={"paths": "packages/*"}),
    )
    receipt = FailClosedResolver(PathsImpactBackend(), _Diff("packages/core/core.py")).resolve(
        str(repo), "base", "head", keys
    )
    assert receipt.invalidated_keys == ("packages", "root-composition")
    assert receipt.unaffected_keys == ("demos", "integration", "stateful")


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
