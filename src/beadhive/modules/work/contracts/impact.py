"""Ports for build-system-agnostic impact resolution (Attested Green ADR, Amendment 1).

Three narrow, runtime-checkable Protocols. Backends are bound at bootstrap and injected; domain
and application code never query a registry for one.

- :class:`ImpactResolver` is the public port every consumer calls.
- :class:`ImpactBackend` is what a build system (Pants, Turborepo, Bazel, ...) implements. It
  answers only the three questions and never builds a receipt itself: its raw answer always
  passes through the shared fail-closed rules first.
- :class:`TreeDiffPort` resolves revisions to trees and lists changed paths, so the set of
  changes a backend is asked about comes from git, not from the backend.
"""

from __future__ import annotations

from collections.abc import Sequence
from importlib import import_module
from typing import Protocol, runtime_checkable

from ..domain.impact import AttestKey, BackendImpact, ChangedPath, ImpactReceipt, ImpactRequest


@runtime_checkable
class ImpactResolver(Protocol):
    """Map ``(base_rev, head_rev)`` to the attest keys whose inputs changed, with a receipt."""

    def resolve(
        self, repo: str, base_rev: str, head_rev: str, keys: Sequence[AttestKey]
    ) -> ImpactReceipt: ...


@runtime_checkable
class ImpactBackend(Protocol):
    """One build system's raw answer: who owns each changed path, what transitively depends on
    those owners, and which keys select those dependents. ``version`` is what this backend
    expects to answer with; a raw answer reporting any other version is a fallback (rule 3)."""

    name: str
    version: str

    def analyze(self, request: ImpactRequest) -> BackendImpact: ...


@runtime_checkable
class TreeDiffPort(Protocol):
    """Git tree reads the resolver needs; implemented by an adapter, faked in tests."""

    def tree_of(self, repo: str, rev: str) -> str: ...

    def changed_paths(self, repo: str, base_tree: str, head_tree: str) -> tuple[ChangedPath, ...]:
        """Every path differing between the trees, deletions included, renames split."""
        ...


def resolve_impact(
    repo: str,
    base_rev: str,
    head_rev: str,
    keys: Sequence[AttestKey],
    backend: ImpactBackend,
) -> ImpactReceipt:
    """Resolve one plugin backend through core's fail-closed policy and Git adapter.

    Build-system packages need this narrow public composition point without importing core
    application or adapter internals. Imports stay lazy so the contracts module remains the
    dependency root for those implementations.
    """

    tree_diff_type = import_module("beadhive.adapters.impact_git").GitTreeDiff
    resolver_factory = import_module("beadhive.modules.work.application.impact").select_resolver
    resolver = resolver_factory(
        backend.name,
        tree_diff=tree_diff_type(),
        backends={backend.name: backend},
    )
    return resolver.resolve(repo, base_rev, head_rev, keys)


__all__ = [
    "AttestKey",
    "BackendImpact",
    "ChangedPath",
    "ImpactBackend",
    "ImpactReceipt",
    "ImpactRequest",
    "ImpactResolver",
    "TreeDiffPort",
    "resolve_impact",
]
