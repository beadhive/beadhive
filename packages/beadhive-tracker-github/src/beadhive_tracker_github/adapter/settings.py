"""The ``plugins.tracker-github`` fragment, as the factory receives it, to adapter arguments.

Root hands the factory the already-validated fragment with every operation flag folded to the
resolved ``enabled`` set (``bootstrap/tracker.py``), so the flags here ARE the effective
enablement. The adapter still reads no configuration itself: this module turns the mapping into
explicit constructor arguments once, at build time.

It re-checks every value's type anyway (the mapping is plain data and the package must not trust
its caller's validation) and refuses with a STATIC message: a factory failure is reported by
root as its exception class only, and nothing here ever repeats a configured value.

The key names are the category's convention, ratified in tracker-adapter-plugin-adr.md
section 3. ``credential.sources`` is root's business (it builds the token source), so it is
not read here.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from beadhive.modules.tracker.contracts import Operation, RetryBudget

__all__ = ["OPERATION_FLAG_KEYS", "AdapterSettings", "parse_settings"]

#: Where each operation is switched on (mirrors root's ``OPERATION_FLAG_KEYS``).
OPERATION_FLAG_KEYS: Mapping[Operation, str] = {
    Operation.ISSUES_READ: "read.issues.enabled",
    Operation.COMMENTS_READ: "read.comments.enabled",
    Operation.COMMENT_CREATE: "write.comment.enabled",
    Operation.LABELS_CHANGE: "write.labels.enabled",
}

_MISSING = object()


@dataclass(frozen=True)
class AdapterSettings:
    enabled: frozenset[Operation]
    base_url: str | None
    include_closed: bool
    page_size: int | None
    http_cache: bool
    budget: RetryBudget
    #: ``write.labels.owned_prefixes`` (empty when unset): the labels a write may ever touch.
    label_prefixes: tuple[str, ...] = ()


def _lookup(settings: Mapping[str, Any], dotted: str) -> Any:
    node: Any = settings
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return _MISSING
        node = node[part]
    return node


def _value(
    settings: Mapping[str, Any], key: str, default: Any, accept: Callable[[Any], bool]
) -> Any:
    value = _lookup(settings, key)
    if value is _MISSING or value is None:
        return default
    if not accept(value):
        raise ValueError(f"plugins.tracker-github.{key} has the wrong type or range")
    return value


def _is_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _is_seconds(value: Any) -> bool:
    return (_is_int(value) or isinstance(value, float)) and 0 < value < float("inf")


def parse_settings(settings: Mapping[str, Any]) -> AdapterSettings:
    if not isinstance(settings, Mapping):
        raise ValueError("plugins.tracker-github settings must be a mapping")
    enabled = {
        op for op, key in OPERATION_FLAG_KEYS.items() if _value(settings, key, False, _is_bool)
    }
    prefixes = _lookup(settings, "write.labels.owned_prefixes")
    if not (isinstance(prefixes, list | tuple) and prefixes):
        enabled.discard(Operation.LABELS_CHANGE)  # no owned label: no label write is possible
        prefixes = ()
    elif not all(isinstance(p, str) and p and p == p.strip() for p in prefixes):
        raise ValueError("plugins.tracker-github.write.labels.owned_prefixes has the wrong type")
    single = float(_value(settings, "retry.max_single_wait_seconds", 60, _is_seconds))
    total = float(_value(settings, "retry.max_total_wait_seconds", 120, _is_seconds))
    if single > total:
        raise ValueError(
            "plugins.tracker-github.retry.max_single_wait_seconds exceeds max_total_wait_seconds"
        )
    budget = RetryBudget(
        max_retries=_value(settings, "retry.max_retries", 3, lambda v: _is_int(v) and v >= 0),
        max_single_wait=timedelta(seconds=single),
        max_total_wait=timedelta(seconds=total),
    )
    return AdapterSettings(
        enabled=frozenset(enabled),
        base_url=_value(settings, "base_url", None, lambda v: isinstance(v, str) and bool(v)),
        include_closed=_value(settings, "read.include_closed", True, _is_bool),
        page_size=_value(settings, "read.page_size", None, lambda v: _is_int(v) and 1 <= v <= 100),
        http_cache=_value(settings, "http_cache.enabled", False, _is_bool),
        budget=budget,
        label_prefixes=tuple(prefixes),
    )
