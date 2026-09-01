"""Compatibility facade for canonical fleet/host configuration policy."""

from __future__ import annotations

from .modules.config.application import partition as _policy

FLEET = _policy.FLEET
HOST = _policy.HOST
HOST_PREFIXES = _policy.HOST_PREFIXES
FLEET_PREFIXES = _policy.FLEET_PREFIXES
FLEET_HOST_OVERRIDE_ALLOWLIST = _policy.FLEET_HOST_OVERRIDE_ALLOWLIST


def _prefix_match_len(path: str, prefixes) -> int:
    return _policy._prefix_match_len(path, prefixes)


def partition_of(path: str) -> str | None:
    return _policy.partition_of(
        path,
        host_prefixes=HOST_PREFIXES,
        fleet_prefixes=FLEET_PREFIXES,
    )


def is_host_overridable(path: str) -> bool:
    return _policy.is_host_overridable(path, allowlist=FLEET_HOST_OVERRIDE_ALLOWLIST)


def schema_leaf_paths() -> list[str]:
    return _policy.schema_leaf_paths()


FLEET_KEYS = frozenset(path for path in schema_leaf_paths() if partition_of(path) == FLEET)
HOST_KEYS = frozenset(path for path in schema_leaf_paths() if partition_of(path) == HOST)

__all__ = (
    "FLEET",
    "FLEET_HOST_OVERRIDE_ALLOWLIST",
    "FLEET_KEYS",
    "FLEET_PREFIXES",
    "HOST",
    "HOST_KEYS",
    "HOST_PREFIXES",
    "is_host_overridable",
    "partition_of",
    "schema_leaf_paths",
)
