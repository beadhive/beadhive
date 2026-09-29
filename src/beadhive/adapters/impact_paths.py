"""Native path-rule impact backend.

Each attest key's ``paths`` selector is a newline-separated list of fnmatch patterns.  Matching
paths invalidate that key.  A path matching no key remains unowned so the shared fail-closed
policy expands the answer to every key.
"""

from __future__ import annotations

import fnmatch
import tomllib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from ..modules.work.domain.impact import BackendImpact, ImpactRequest

PATHS_BACKEND = "paths"
PATHS_BACKEND_VERSION = "1"
ROOT_COMPOSITION = "@root-composition"

# Registered root tests for every workspace distribution consumed by the root project.  The
# paths live here, beside the derived dependency resolver, so impact analysis can fail closed
# before declaring a package change resolved when its root test closure is unknown.
PACKAGE_TESTS: dict[str, tuple[str, ...]] = {
    "beadhive-bd-cli": (
        "tests/test_bd_cli.py",
        "tests/test_bd_cli_boundary.py",
        "tests/test_beads_routing.py",
        "tests/test_guard_primary.py",
        "tests/test_merge_slot.py",
    ),
    "beadhive-beads-client": (
        "tests/test_beads_routing.py",
        "tests/test_dispatch_state.py",
        "tests/test_plan.py",
        "tests/test_work_lifecycle_shell.py",
        "tests/test_work_queue.py",
        "tests/test_work_reads.py",
        "tests/test_work_review_shell.py",
    ),
    "beadhive-core": (
        "tests/test_beads_routing.py",
        "tests/test_claim_fence.py",
        "tests/test_dispatch_state.py",
        "tests/test_work_assignment_boundaries.py",
        "tests/test_work_lifecycle_shell.py",
        "tests/test_work_queue.py",
        "tests/test_work_reads.py",
        "tests/test_work_review_shell.py",
    ),
    "beadhive-pants": (
        "tests/test_selective_validation_impact_golden.py",
        "tests/unit/modules/work/test_impact_pants.py",
        "tests/unit/modules/work/test_impact_plugin_backends.py",
    ),
    "beadhive-plugins": (
        "tests/test_build_verify_surfaces.py",
        "tests/test_plugins.py",
    ),
    "beadhive-worktrees": (
        "tests/test_retire.py",
        "tests/test_worktree.py",
        "tests/test_worktree_inventory_boundaries.py",
        "tests/unit/integrations/test_herdr_binding_conformance.py",
    ),
}


class UnresolvedRootComposition(ValueError):
    """A root workspace dependency has no registered root-test closure."""


def _dependency_group_requirements(groups: Mapping[str, Any]) -> tuple[str, ...]:
    """Expand every PEP 735 dependency group, including nested group references."""
    requirements: list[str] = []

    def visit(name: str, ancestors: tuple[str, ...] = ()) -> None:
        if name in ancestors:
            cycle = " -> ".join((*ancestors, name))
            raise UnresolvedRootComposition(f"cyclic dependency-group include: {cycle}")
        entries = groups.get(name, ())
        if not isinstance(entries, list):
            raise UnresolvedRootComposition(f"dependency-group {name!r} must be a list")
        for entry in entries:
            if isinstance(entry, str):
                requirements.append(entry)
                continue
            if isinstance(entry, dict) and isinstance(entry.get("include-group"), str):
                visit(entry["include-group"], (*ancestors, name))
                continue
            raise UnresolvedRootComposition(
                f"dependency-group {name!r} has an unsupported entry: {entry!r}"
            )

    for group_name in groups:
        visit(group_name)
    return tuple(requirements)


def _wheel_package_roots(payload: Mapping[str, Any]) -> tuple[Path, ...]:
    wheel = (
        (payload.get("tool") or {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
    )
    packages: Iterable[Any] = wheel.get("packages", ())
    return tuple(Path(package) for package in packages if isinstance(package, str))


def selector_patterns(selector: str | None) -> tuple[str, ...]:
    """Parse one path selector, ignoring blank lines and comments."""
    return tuple(
        line
        for raw in (selector or "").splitlines()
        if (line := raw.strip()) and not line.startswith("#")
    )


def matches_path(path: str, pattern: str) -> bool:
    """Match repository-relative paths with the same fnmatch semantics used by policy config."""
    return fnmatch.fnmatchcase(path, pattern)


def root_composition_package_patterns(repo: str | Path) -> tuple[str, ...]:
    """Derive package paths whose public surfaces are composed by the root project."""
    root = Path(repo)
    payload = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    project = payload.get("project") or {}
    requirements = [
        *project.get("dependencies", ()),
        *(
            requirement
            for group in (project.get("optional-dependencies") or {}).values()
            for requirement in group
        ),
        *_dependency_group_requirements(payload.get("dependency-groups") or {}),
    ]
    dependency_names = {
        canonicalize_name(Requirement(requirement).name) for requirement in requirements
    }
    members = (payload.get("tool") or {}).get("uv", {}).get("workspace", {}).get("members", ())
    wheel_roots = _wheel_package_roots(payload)
    patterns: list[str] = []
    unresolved: list[str] = []
    for member_glob in members:
        for directory in sorted(root.glob(member_glob)):
            manifest = directory / "pyproject.toml"
            if not manifest.is_file():
                continue
            package = tomllib.loads(manifest.read_text(encoding="utf-8"))
            name = str((package.get("project") or {}).get("name") or "")
            canonical_name = canonicalize_name(name)
            member_root = directory.relative_to(root)
            vendored = any(package_root.is_relative_to(member_root) for package_root in wheel_roots)
            if name and (canonical_name in dependency_names or vendored):
                if canonical_name not in PACKAGE_TESTS:
                    unresolved.append(canonical_name)
                patterns.append(f"{directory.relative_to(root).as_posix()}/*")
    if unresolved:
        raise UnresolvedRootComposition(
            "root workspace dependencies lack PACKAGE_TESTS mappings: "
            + ", ".join(sorted(set(unresolved)))
        )
    return tuple(sorted(set(patterns)))


def expanded_selector_patterns(repo: str | Path, selector: str | None) -> tuple[str, ...]:
    """Expand derived selector tokens while leaving ordinary fnmatch rules unchanged."""
    expanded: list[str] = []
    for pattern in selector_patterns(selector):
        if pattern == ROOT_COMPOSITION:
            expanded.extend(root_composition_package_patterns(repo))
        else:
            expanded.append(pattern)
    return tuple(dict.fromkeys(expanded))


class PathsImpactBackend:
    """Resolve key impact from configured repository-relative path patterns."""

    name = PATHS_BACKEND
    version = PATHS_BACKEND_VERSION

    def analyze(self, request: ImpactRequest) -> BackendImpact:
        key_units: dict[str, tuple[str, ...]] = {}
        owners: dict[str, list[str]] = {change.path: [] for change in request.changed}

        for key in request.keys:
            patterns = expanded_selector_patterns(request.repo, key.selector(self.name))
            units = tuple(f"{key.name}:{pattern}" for pattern in patterns)
            key_units[key.name] = units
            for change in request.changed:
                owners[change.path].extend(
                    unit
                    for pattern, unit in zip(patterns, units, strict=True)
                    if matches_path(change.path, pattern)
                )

        affected = frozenset(unit for units in owners.values() for unit in units)
        return BackendImpact(
            backend_version=self.version,
            owners={path: tuple(sorted(set(units))) for path, units in owners.items()},
            affected_units=affected,
            key_units=key_units,
            proven_keys=frozenset(name for name, units in key_units.items() if units),
        )


__all__ = [
    "PATHS_BACKEND",
    "PATHS_BACKEND_VERSION",
    "PACKAGE_TESTS",
    "ROOT_COMPOSITION",
    "UnresolvedRootComposition",
    "PathsImpactBackend",
    "expanded_selector_patterns",
    "matches_path",
    "root_composition_package_patterns",
    "selector_patterns",
]
