from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from beadhive.adapters.impact_pants import PantsImpactBackend
from beadhive.modules.work.application.impact import FailClosedResolver
from beadhive.modules.work.domain.impact import AttestKey, ChangedPath, ImpactReason


class Diff:
    def __init__(self, changed: tuple[ChangedPath, ...]):
        self.changed = changed

    def tree_of(self, repo: str, rev: str) -> str:
        return {"base": "base-tree", "head": "head-tree"}[rev]

    def changed_paths(self, repo: str, base_tree: str, head_tree: str):
        return self.changed


KEYS = (
    AttestKey("docs", "just attest-docs", selectors={"pants": "attest:docs"}),
    AttestKey("unit", "just attest-unit", selectors={"pants": "attest:unit"}),
    AttestKey("stateful", "just attest-stateful", selectors={"pants": "attest:stateful"}),
    AttestKey("demos", "just attest-demos", selectors={"pants": "attest:demos"}),
)


def target(address, *, sources=(), tags=(), target_type="files"):
    return {
        "address": address,
        "sources": list(sources),
        "tags": list(tags),
        "target_type": target_type,
    }


@pytest.fixture
def repo(tmp_path, monkeypatch):
    (tmp_path / "pants.toml").write_text('[GLOBAL]\npants_version = "2.32.1"\n')
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    (scripts / "pants_proven_tests.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "inventory": "fixture",
                "planning_baseline": 1,
                "tests": {
                    "tests/test_unit.py": {
                        "status": "proven",
                        "dependencies": ["docs:docs"],
                    }
                },
            }
        )
    )

    def run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, "head-tree\n", "")

    monkeypatch.setattr(subprocess, "run", run)
    return tmp_path


def resolve(repo: Path, changed, graph, affected, *, failing=False):
    calls = []

    def query(path, args, timeout):
        calls.append((tuple(args), timeout))
        if failing:
            raise RuntimeError("engine unavailable")
        return graph if tuple(args) == ("peek", "::") else affected

    backend = PantsImpactBackend(repo, query=query)
    receipt = FailClosedResolver(backend, Diff(tuple(changed))).resolve(
        str(repo), "base", "head", KEYS
    )
    return receipt, calls


def complete_graph():
    return [
        target(
            "manual:docs",
            sources=("manual/guide.md",),
            tags=("category:docs", "attest:docs"),
        ),
        target(
            "tests:test_unit.py",
            sources=("tests/test_unit.py",),
            tags=("category:test-only", "attest:unit"),
            target_type="python_test",
        ),
        target(
            "tests:stateful-fixtures",
            sources=("tests/stateful_fixtures.py",),
            tags=("category:test-only", "attest:stateful"),
        ),
        target(
            "config:runtime",
            sources=("config/runtime.yaml",),
            tags=("category:config", "attest:demos"),
        ),
        target("scripts:demos", tags=("category:code", "attest:demos")),
    ]


def test_docs_only_invalidates_docs_only(repo):
    graph = complete_graph()
    receipt, calls = resolve(repo, [ChangedPath("manual/guide.md")], graph, [graph[0]])
    assert receipt.invalidated_keys == ("docs",)
    assert receipt.unaffected_keys == ("demos", "stateful", "unit")
    assert receipt.backend_version == "2.32.1"
    assert receipt.evidence["docs"].reason == ImpactReason.AFFECTED
    assert receipt.evidence["unit"].reason == ImpactReason.UNAFFECTED
    assert calls[1][0] == (
        "--changed-since=base",
        "--changed-dependents=transitive",
        "peek",
    )


def test_source_change_invalidates_transitive_dependent_key(repo):
    graph = complete_graph() + [target("src:lib", sources=("src/lib.py",), tags=("category:code",))]
    receipt, _ = resolve(
        repo,
        [ChangedPath("src/lib.py")],
        graph,
        [graph[5], graph[1], graph[4]],
    )
    assert receipt.invalidated_keys == ("demos", "unit")
    assert receipt.evidence["unit"].units == ("tests:test_unit.py",)
    assert receipt.unaffected_keys == ("docs", "stateful")


@pytest.mark.parametrize("path", ["unowned.txt", "BUILD"])
def test_unowned_paths_invalidate_every_key(repo, path):
    graph = complete_graph()
    receipt, _ = resolve(repo, [ChangedPath(path)], graph, [])
    assert receipt.invalidated_keys == ("demos", "docs", "stateful", "unit")


def test_build_system_category_invalidates_every_key(repo):
    graph = complete_graph() + [
        target(
            "root:build-system",
            sources=("tooling.cfg",),
            tags=("category:build-system",),
        )
    ]
    receipt, _ = resolve(repo, [ChangedPath("tooling.cfg")], graph, [graph[-1]])

    assert receipt.invalidated_keys == ("demos", "docs", "stateful", "unit")
    assert receipt.global_inputs_hit == ("tooling.cfg",)


def test_untagged_owner_fails_closed_to_every_key(repo):
    graph = complete_graph() + [target("misc:owned", sources=("misc/owned.txt",))]
    receipt, _ = resolve(repo, [ChangedPath("misc/owned.txt")], graph, [graph[-1]])

    assert receipt.is_fallback
    assert "owner lacks exactly one known category tag" in receipt.fallback_reason
    assert receipt.invalidated_keys == ("demos", "docs", "stateful", "unit")


def test_multi_category_change_uses_union(repo):
    graph = complete_graph()
    receipt, _ = resolve(
        repo,
        [ChangedPath("manual/guide.md"), ChangedPath("config/runtime.yaml")],
        graph,
        [graph[0], graph[3]],
    )

    assert receipt.invalidated_keys == ("demos", "docs")
    assert receipt.unaffected_keys == ("stateful", "unit")


def test_readme_only_does_not_invalidate_stateful(repo):
    graph = complete_graph() + [
        target(
            "root:prose",
            sources=("README.md",),
            tags=("category:docs", "attest:docs"),
        )
    ]
    receipt, _ = resolve(repo, [ChangedPath("README.md")], graph, [graph[-1]])

    assert receipt.invalidated_keys == ("docs",)
    assert "stateful" in receipt.unaffected_keys


def test_deleted_file_invalidates_every_key(repo):
    receipt, _ = resolve(repo, [ChangedPath("manual/guide.md", "D")], complete_graph(), [])
    assert receipt.invalidated_keys == ("demos", "docs", "stateful", "unit")
    assert receipt.unowned_paths == ("manual/guide.md",)


def test_pants_failure_falls_back_to_native_full(repo):
    receipt, calls = resolve(repo, [ChangedPath("manual/guide.md")], [], [], failing=True)
    assert receipt.backend == "native-full"
    assert receipt.fallback_reason.startswith("pants: error: RuntimeError")
    assert receipt.invalidated_keys == ("demos", "docs", "stateful", "unit")
    assert len(calls) == 2


def test_transient_peek_failure_retries_before_fallback(repo):
    graph = complete_graph()
    calls: list[tuple[str, ...]] = []

    def query(path, args, timeout):
        call = tuple(args)
        calls.append(call)
        if len(calls) == 1:
            raise RuntimeError("Pants peek failed (1): Filesystem changed during run")
        return graph if call == ("peek", "::") else [graph[0]]

    backend = PantsImpactBackend(repo, query=query, sleeper=lambda _: None)
    receipt = FailClosedResolver(backend, Diff((ChangedPath("manual/guide.md"),))).resolve(
        str(repo), "base", "head", KEYS
    )

    assert not receipt.is_fallback
    assert calls[:2] == [("peek", "::"), ("peek", "::")]
    assert calls[2] == ("--changed-since=base", "--changed-dependents=transitive", "peek")


def test_unproven_selected_test_invalidates_its_key(repo):
    graph = complete_graph()
    graph[1]["sources"] = ["tests/test_not_proven.py"]
    receipt, _ = resolve(repo, [ChangedPath("manual/guide.md")], graph, graph)
    assert receipt.invalidated_keys == ("demos", "docs", "stateful", "unit")
    assert receipt.evidence["unit"].reason == ImpactReason.UNPROVEN


def test_unaffected_unproven_test_does_not_poison_an_unrelated_change(repo):
    graph = complete_graph()
    graph[1]["sources"] = ["tests/test_not_proven.py"]
    receipt, _ = resolve(repo, [ChangedPath("manual/guide.md")], graph, [graph[0]])
    assert receipt.unaffected_keys == ("demos", "stateful", "unit")


def test_package_change_invalidates_only_the_packages_key_without_a_manifest_entry(repo):
    packages_key = AttestKey(
        "packages", "just attest-packages", selectors={"pants": "attest:packages"}
    )
    graph = complete_graph() + [
        target(
            "packages/example/src:lib",
            sources=("packages/example/src/example/__init__.py",),
            tags=("category:code", "attest:packages"),
            target_type="python_source",
        ),
        target(
            "packages/example/tests:tests",
            sources=("packages/example/tests/test_example.py",),
            tags=("category:test-only", "attest:packages"),
            target_type="python_test",
        ),
    ]

    def query(path, args, timeout):
        return graph if tuple(args) == ("peek", "::") else graph[5:]

    backend = PantsImpactBackend(repo, query=query)
    receipt = FailClosedResolver(
        backend, Diff((ChangedPath("packages/example/src/example/__init__.py"),))
    ).resolve(str(repo), "base", "head", (*KEYS, packages_key))
    assert receipt.invalidated_keys == ("packages",)
    assert receipt.unaffected_keys == ("demos", "docs", "stateful", "unit")
    assert receipt.evidence["packages"].reason == ImpactReason.AFFECTED


def test_manifest_schema_failure_falls_back(repo):
    (repo / "scripts/pants_proven_tests.json").write_text('{"schema_version": 2}')
    receipt, _ = resolve(repo, [ChangedPath("manual/guide.md")], complete_graph(), [])
    assert receipt.is_fallback
