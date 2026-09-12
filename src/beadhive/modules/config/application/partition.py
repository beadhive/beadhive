"""Canonical fleet/host ownership policy for configuration leaves."""

from __future__ import annotations

from collections.abc import Iterable

from ..contracts import iter_schema_fields

FLEET = "fleet"
HOST = "host"

HOST_PREFIXES: frozenset[str] = frozenset(
    {
        "worktrees.path",
        "otel",
        "work.identity",
        "work.validation_slots",
        "work.dispatch.max_beads_per_session",
        "work.dispatch.auto_budget",
        "hq.remote",
        "dolt",
        "git_workspace",
        "log",
        "observaloop",
        "harness",
        "archive",
        "backup",
        "alerts",
        "metadata",
        "orca",
        "repowise",
        "hitch",
        "herdr",
        "host.daemon",
    }
)

FLEET_PREFIXES: frozenset[str] = frozenset(
    {
        "schema_version",
        "delimiter",
        "providers",
        "orgs",
        "exclude",
        "dimensions",
        "passthrough",
        "managed_repos",
        "worktrees",
        "work",
        "release",
        "claude",
        "host",
    }
)

FLEET_HOST_OVERRIDE_ALLOWLIST: frozenset[str] = frozenset({"worktrees.ephemeral"})

assert HOST_PREFIXES.isdisjoint(FLEET_PREFIXES), (
    "a prefix cannot be both fleet and host — fix HOST_PREFIXES/FLEET_PREFIXES"
)


def _prefix_match_len(path: str, prefixes: Iterable[str]) -> int:
    lengths = [
        len(prefix) for prefix in prefixes if path == prefix or path.startswith(prefix + ".")
    ]
    return max(lengths, default=-1)


def partition_of(
    path: str,
    *,
    host_prefixes: Iterable[str] = HOST_PREFIXES,
    fleet_prefixes: Iterable[str] = FLEET_PREFIXES,
) -> str | None:
    """Return the owner of *path*, preferring the longest matching prefix."""

    host_len = _prefix_match_len(path, host_prefixes)
    fleet_len = _prefix_match_len(path, fleet_prefixes)
    if host_len < 0 and fleet_len < 0:
        return None
    return HOST if host_len >= fleet_len else FLEET


def is_host_overridable(
    path: str, *, allowlist: Iterable[str] = FLEET_HOST_OVERRIDE_ALLOWLIST
) -> bool:
    """Return whether a fleet-owned path has an explicit host escape hatch."""

    return _prefix_match_len(path, allowlist) >= 0


def host_override_value_allowed(path: str, value: object) -> bool:
    """Return whether a host may set *value* for an allowlisted fleet key.

    ``worktrees.ephemeral`` is a one-way escape hatch: a host may retain
    persistent worktrees when the fleet default is ephemeral, but may not make
    itself more ephemeral than a fleet that requires persistence.
    """

    if not is_host_overridable(path):
        return False
    return path != "worktrees.ephemeral" or value is False


def schema_leaf_paths() -> list[str]:
    """Return the canonical model's settable leaf paths."""

    paths = [field.path for field in iter_schema_fields() if "[]" not in field.path]
    branches = {path for path in paths if any(other.startswith(path + ".") for other in paths)}
    return [path for path in paths if path not in branches]


FLEET_KEYS: frozenset[str] = frozenset(
    path for path in schema_leaf_paths() if partition_of(path) == FLEET
)
HOST_KEYS: frozenset[str] = frozenset(
    path for path in schema_leaf_paths() if partition_of(path) == HOST
)

__all__ = (
    "FLEET",
    "FLEET_HOST_OVERRIDE_ALLOWLIST",
    "FLEET_KEYS",
    "FLEET_PREFIXES",
    "HOST",
    "HOST_KEYS",
    "HOST_PREFIXES",
    "host_override_value_allowed",
    "is_host_overridable",
    "partition_of",
    "schema_leaf_paths",
)
