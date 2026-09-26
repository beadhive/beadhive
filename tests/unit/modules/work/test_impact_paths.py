from __future__ import annotations

from beadhive.adapters.impact_paths import PathsImpactBackend, selector_patterns
from beadhive.bootstrap.impact import impact_resolver
from beadhive.modules.config.contracts import AttestConfig
from beadhive.modules.work.contracts.impact_resolution import FailClosedResolver
from beadhive.modules.work.domain.impact import AttestKey, ChangedPath, ImpactReason


class _Diff:
    def __init__(self, *paths: str) -> None:
        self.paths = paths

    def tree_of(self, _repo: str, rev: str) -> str:
        return f"{rev}-tree"

    def changed_paths(self, _repo: str, _base: str, _head: str):
        return tuple(ChangedPath(path) for path in self.paths)


def _key(name: str, *patterns: str) -> AttestKey:
    return AttestKey(name, f"just attest-{name}", selectors={"paths": "\n".join(patterns)})


def test_paths_backend_partitions_keys_and_keeps_cross_cutting_matches() -> None:
    keys = (
        _key("docs", "*.md"),
        _key("unit", "src/*", "docs/proof/*.json"),
        _key("architecture", "docs/proof/*"),
    )
    resolver = FailClosedResolver(PathsImpactBackend(), _Diff("docs/proof/result.json"))

    receipt = resolver.resolve("/repo", "base", "head", keys)

    assert receipt.invalidated_keys == ("architecture", "unit")
    assert receipt.unaffected_keys == ("docs",)
    assert receipt.evidence["unit"].reason == ImpactReason.AFFECTED


def test_unmatched_path_fails_closed_to_every_key() -> None:
    keys = (_key("docs", "*.md"), _key("unit", "src/*"))
    resolver = FailClosedResolver(PathsImpactBackend(), _Diff("new-kind/file.xyz"))

    receipt = resolver.resolve("/repo", "base", "head", keys)

    assert receipt.invalidated_keys == ("docs", "unit")
    assert receipt.unaffected_keys == ()
    assert receipt.unowned_paths == ("new-kind/file.xyz",)
    assert all(item.reason == ImpactReason.UNOWNED_PATH for item in receipt.evidence.values())


def test_selector_parser_ignores_comments_and_blank_lines() -> None:
    assert selector_patterns("\n# why\nsrc/*\n  tests/*  \n") == ("src/*", "tests/*")


def test_paths_backend_is_available_without_a_plugin() -> None:
    attest = AttestConfig.model_validate({"impact": {"backend": "paths"}})

    resolver = impact_resolver(attest, tree_diff=_Diff("src/app.py"))

    assert isinstance(resolver, FailClosedResolver)
    assert isinstance(resolver.backend, PathsImpactBackend)
