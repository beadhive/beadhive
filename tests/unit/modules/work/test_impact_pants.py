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
    AttestKey("always-run", "just attest-always-run"),
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
        target("manual:docs", sources=("manual/guide.md",), tags=("attest:docs",)),
        target(
            "tests:test_unit.py",
            sources=("tests/test_unit.py",),
            tags=("attest:unit",),
            target_type="python_test",
        ),
    ]


def test_docs_only_invalidates_docs_and_selectorless_always_run(repo):
    graph = complete_graph()
    receipt, calls = resolve(repo, [ChangedPath("manual/guide.md")], graph, [graph[0]])
    assert receipt.invalidated_keys == ("always-run", "docs")
    assert receipt.unaffected_keys == ("unit",)
    assert receipt.backend_version == "2.32.1"
    assert receipt.evidence["docs"].reason == ImpactReason.AFFECTED
    assert receipt.evidence["unit"].reason == ImpactReason.UNAFFECTED
    assert calls[1][0] == (
        "--changed-since=base",
        "--changed-dependents=transitive",
        "peek",
    )


def test_source_change_invalidates_transitive_dependent_key(repo):
    graph = complete_graph() + [target("src:lib", sources=("src/lib.py",))]
    receipt, _ = resolve(repo, [ChangedPath("src/lib.py")], graph, [graph[2], graph[1]])
    assert set(receipt.invalidated_keys) == {"unit", "always-run"}
    assert receipt.evidence["unit"].units == ("tests:test_unit.py",)
    assert receipt.unaffected_keys == ("docs",)


@pytest.mark.parametrize("path", ["unowned.txt", "BUILD", "uv.lock"])
def test_unowned_and_global_inputs_invalidate_every_key(repo, path):
    graph = complete_graph()
    receipt, _ = resolve(repo, [ChangedPath(path)], graph, [])
    assert receipt.invalidated_keys == ("always-run", "docs", "unit")


def test_deleted_file_invalidates_every_key(repo):
    receipt, _ = resolve(repo, [ChangedPath("manual/guide.md", "D")], complete_graph(), [])
    assert receipt.invalidated_keys == ("always-run", "docs", "unit")
    assert receipt.unowned_paths == ("manual/guide.md",)


def test_pants_failure_falls_back_to_native_full(repo):
    receipt, calls = resolve(repo, [ChangedPath("manual/guide.md")], [], [], failing=True)
    assert receipt.backend == "native-full"
    assert receipt.fallback_reason.startswith("pants: error: RuntimeError")
    assert receipt.invalidated_keys == ("always-run", "docs", "unit")
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
    assert receipt.invalidated_keys == ("always-run", "docs", "unit")
    assert receipt.evidence["unit"].reason == ImpactReason.UNPROVEN


def test_unaffected_unproven_test_does_not_poison_an_unrelated_change(repo):
    graph = complete_graph()
    graph[1]["sources"] = ["tests/test_not_proven.py"]
    receipt, _ = resolve(repo, [ChangedPath("manual/guide.md")], graph, [graph[0]])
    assert receipt.unaffected_keys == ("unit",)


def test_manifest_schema_failure_falls_back(repo):
    (repo / "scripts/pants_proven_tests.json").write_text('{"schema_version": 2}')
    receipt, _ = resolve(repo, [ChangedPath("manual/guide.md")], complete_graph(), [])
    assert receipt.is_fallback
