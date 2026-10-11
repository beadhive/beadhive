"""Longest-processing-time-first test ordering from a checked-in durations file.

Late-scheduled 300-400 s tests leave one xdist worker running while the rest idle.  Ordering the
collection by recorded duration (descending) lets ``--dist worksteal`` hand the long tests out
first and fill the tail with short ones.  The durations file is regenerated from validation JUnit
reports by ``scripts/refresh_test_durations.py``.

Opt out with ``BH_TEST_DURATION_ORDER=0``.  ``BH_TEST_DURATIONS_FILE`` points at another file.
Ordering is a pure sort: stable, deterministic, and it never adds, drops, or deselects an item.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path

ENABLE_ENV = "BH_TEST_DURATION_ORDER"
FILE_ENV = "BH_TEST_DURATIONS_FILE"
DEFAULT_FILE = Path(__file__).with_name("test_durations.json")
_OFF = frozenset({"0", "false", "no", "off"})


def enabled(env: Mapping[str, str] | None = None) -> bool:
    environ = os.environ if env is None else env
    return environ.get(ENABLE_ENV, "1").strip().lower() not in _OFF


def durations_path(env: Mapping[str, str] | None = None) -> Path:
    environ = os.environ if env is None else env
    override = environ.get(FILE_ENV)
    return Path(override) if override else DEFAULT_FILE


def junit_key(nodeid: str) -> str:
    """Map a pytest nodeid onto the ``classname::name`` form JUnit reports use."""
    path, _, rest = nodeid.partition("::")
    module = path.removesuffix(".py").replace("/", ".")
    *classes, name = rest.split("::") if rest else [""]
    return f"{'.'.join([module, *classes])}::{name}"


def load(path: Path) -> tuple[dict[str, float], float]:
    """Return ``(durations, fallback)``; a missing or malformed file yields no ordering signal."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        tests = {str(key): float(value) for key, value in data["tests"].items()}
        return tests, float(data.get("median", 0.0))
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}, 0.0


def order_items(items: Sequence, durations: Mapping[str, float], fallback: float) -> list:
    """Stable descending sort; unknown tests take ``fallback`` so they follow known-long ones."""
    if not durations:
        return list(items)
    return sorted(items, key=lambda item: -durations.get(junit_key(item.nodeid), fallback))


def apply(items: Sequence, env: Mapping[str, str] | None = None) -> list:
    if not enabled(env):
        return list(items)
    durations, fallback = load(durations_path(env))
    return order_items(items, durations, fallback)
