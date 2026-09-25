from __future__ import annotations

import json
import subprocess
from importlib import resources
from pathlib import Path

from typer.testing import CliRunner

from beadhive.kernel.plugins.contracts import BuildVerifier
from beadhive.modules.work.contracts.impact import (
    AttestKey,
    ChangedPath,
    ImpactBackend,
    ImpactRequest,
)
from beadhive_pants import PantsBuildVerifier as ExportedPantsBuildVerifier
from beadhive_pants import cli
from beadhive_pants import impact as impact_pants
from beadhive_pants.impact import PantsImpactBackend
from beadhive_pants.verify import PantsBuildVerifier


def test_packaged_plugin_manifest_declares_build_impact() -> None:
    manifest = json.loads(
        (resources.files("beadhive_pants") / "plugin.json").read_text(encoding="utf-8")
    )

    assert manifest["plugin_id"] == "pants"
    assert manifest["capabilities"]["provides"] == [
        {"api_version": 1, "id": "build.impact"},
        {"api_version": 1, "id": "build.verify"},
    ]
    assert {row["command"] for row in manifest["presentation"]["cli"]} == {
        "plugin pants attest-check",
        "plugin pants cache",
        "plugin pants native",
        "plugin pants test affected",
        "plugin pants test all",
    }


def test_backend_implements_the_public_impact_contract(tmp_path) -> None:
    (tmp_path / "pants.toml").write_text('[GLOBAL]\npants_version = "2.32.1"\n')

    backend = PantsImpactBackend(tmp_path)

    assert isinstance(backend, ImpactBackend)
    assert backend.name == "pants"
    assert backend.version == "2.32.1"


def test_verifier_implements_the_public_build_verify_contract() -> None:
    assert isinstance(PantsBuildVerifier(), BuildVerifier)
    assert ExportedPantsBuildVerifier is PantsBuildVerifier


def test_proven_manifest_explicitly_lists_every_package_test() -> None:
    manifest = json.loads(
        (resources.files("beadhive_pants.data") / "proven_tests.json").read_text(encoding="utf-8")
    )
    proven_package_tests = {
        path
        for path, evidence in manifest["tests"].items()
        if path.startswith("packages/") and evidence["status"] == "proven"
    }
    workspace_package_tests = {
        path.as_posix()
        for package in Path("packages").iterdir()
        if package.is_dir()
        for path in package.glob("tests/**/test_*.py")
    }

    assert proven_package_tests >= workspace_package_tests


def test_backend_reports_packages_proven_for_the_current_inventory(tmp_path, monkeypatch) -> None:
    manifest = json.loads(
        (resources.files("beadhive_pants.data") / "proven_tests.json").read_text(encoding="utf-8")
    )
    package_tests = sorted(path for path in manifest["tests"] if path.startswith("packages/"))
    graph = [
        {
            "address": "packages/example/src:lib",
            "sources": ["packages/example/src/example.py"],
            "tags": ["category:code", "attest:packages"],
            "target_type": "python_source",
        },
        *(
            {
                "address": f"packages:test-{index}",
                "sources": [source],
                "tags": ["category:test-only", "attest:packages"],
                "target_type": "python_test",
            }
            for index, source in enumerate(package_tests)
        ),
    ]
    (tmp_path / "pants.toml").write_text('[GLOBAL]\npants_version = "2.32.1"\n')
    monkeypatch.setattr(
        impact_pants.subprocess,
        "run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "head-tree\n", ""),
    )

    backend = PantsImpactBackend(
        tmp_path,
        manifest=resources.files("beadhive_pants.data") / "proven_tests.json",
        query=lambda _repo, args, _timeout: graph if tuple(args) == ("peek", "::") else graph[:1],
    )
    result = backend.analyze(
        ImpactRequest(
            repo=str(tmp_path),
            base_tree="base-tree",
            head_tree="head-tree",
            changed=(ChangedPath("packages/example/src/example.py"),),
            keys=(
                AttestKey(
                    "packages",
                    "just attest-packages",
                    selectors={"pants": "attest:packages"},
                ),
            ),
            timeout_seconds=5,
            base_rev="base",
            head_rev="head",
        )
    )

    assert package_tests
    assert result.proven_keys == frozenset({"packages"})


def test_cli_projects_the_runner_partition_without_reimplementing_it(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(cli.runner, "main", lambda args: calls.append(list(args)) or 0)
    runner = CliRunner()

    assert runner.invoke(cli.app, ["test", "affected", "BASE"]).exit_code == 0
    assert runner.invoke(cli.app, ["test", "all"]).exit_code == 0
    assert runner.invoke(cli.app, ["native", "-m", "unit"]).exit_code == 0
    assert calls == [["affected", "BASE"], ["all"], ["native", "--", "-m", "unit"]]


def test_cli_projects_cache_and_attest_exit_status(monkeypatch) -> None:
    cache_calls: list[list[str]] = []
    monkeypatch.setattr(cli.cache, "main", lambda args: cache_calls.append(list(args)) or 0)
    monkeypatch.setattr(cli.attest, "main", lambda: 17)
    runner = CliRunner()

    assert runner.invoke(cli.app, ["cache"]).exit_code == 0
    assert runner.invoke(cli.app, ["cache", "check"]).exit_code == 0
    assert runner.invoke(cli.app, ["attest-check"]).exit_code == 17
    assert cache_calls == [["status"], ["check"]]
