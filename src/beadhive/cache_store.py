"""Pure path policy for per-hive minimal-clone caches.

The hydration hub and cache-reclaim adapter share these predicates without importing each
other.  Callers own configuration and registry lookup; this module only answers deterministic
questions about explicit paths and hive identity fields.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


def cache_path(cache_root: Path, entry: Mapping[str, object]) -> Path:
    """Return one hive's canonical cache path below an explicit cache root."""
    return cache_root / str(entry["provider"]) / str(entry["org"]) / str(entry["repo"])


def local_checkout_source(checkout: Path) -> Path | None:
    """Return ``checkout`` exactly when its local bead store supersedes a cache."""
    return checkout if (checkout / ".beads").is_dir() else None
