"""Native path-rule impact backend.

Each attest key's ``paths`` selector is a newline-separated list of fnmatch patterns.  Matching
paths invalidate that key.  A path matching no key remains unowned so the shared fail-closed
policy expands the answer to every key.
"""

from __future__ import annotations

import fnmatch
import tomllib
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

from ..modules.work.domain.impact import BackendImpact, ImpactRequest

PATHS_BACKEND = "paths"
PATHS_BACKEND_VERSION = "1"
ROOT_WORKSPACE_PACKAGES = "@root-workspace-packages"


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


def root_workspace_package_patterns(repo: str | Path) -> tuple[str, ...]:
    """Derive path patterns for workspace distributions consumed by the root project."""
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
    ]
    dependency_names = {
        canonicalize_name(Requirement(requirement).name) for requirement in requirements
    }
    members = (payload.get("tool") or {}).get("uv", {}).get("workspace", {}).get("members", ())
    patterns: list[str] = []
    for member_glob in members:
        for directory in sorted(root.glob(member_glob)):
            manifest = directory / "pyproject.toml"
            if not manifest.is_file():
                continue
            package = tomllib.loads(manifest.read_text(encoding="utf-8"))
            name = str((package.get("project") or {}).get("name") or "")
            if name and canonicalize_name(name) in dependency_names:
                patterns.append(f"{directory.relative_to(root).as_posix()}/*")
    return tuple(sorted(set(patterns)))


def expanded_selector_patterns(repo: str | Path, selector: str | None) -> tuple[str, ...]:
    """Expand derived selector tokens while leaving ordinary fnmatch rules unchanged."""
    expanded: list[str] = []
    for pattern in selector_patterns(selector):
        if pattern == ROOT_WORKSPACE_PACKAGES:
            expanded.extend(root_workspace_package_patterns(repo))
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
    "ROOT_WORKSPACE_PACKAGES",
    "PathsImpactBackend",
    "expanded_selector_patterns",
    "matches_path",
    "root_workspace_package_patterns",
    "selector_patterns",
]
