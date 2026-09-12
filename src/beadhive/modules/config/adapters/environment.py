"""Allowlisted environment projection for the pure resolver."""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from ..contracts import BeadhiveConfig


def _coerce(raw: str) -> Any:
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return raw


def _set_nested(target: dict[str, Any], parts: list[str], value: Any) -> None:
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


@dataclass(frozen=True, slots=True)
class MappingEnvironmentSource:
    """Project explicit ``BH_*`` variables without consulting process state."""

    environ: Mapping[str, str]

    def load_overlay(self) -> Mapping[str, Any]:
        overlay: dict[str, Any] = {}
        top_level = set(BeadhiveConfig.model_fields)
        for name, raw in sorted(self.environ.items()):
            if not name.startswith("BH_") or not raw or name == "BH_WORKTREES":
                continue
            parts = [part.lower() for part in name.removeprefix("BH_").split("__")]
            if not parts or parts[0] not in top_level or parts[0] == "schema_version":
                continue
            _set_nested(overlay, parts, _coerce(raw))
        return overlay


class ProcessEnvironmentSource(MappingEnvironmentSource):
    """Production adapter that snapshots the ambient environment at construction."""

    def __init__(self) -> None:
        super().__init__(dict(os.environ))


__all__ = ("MappingEnvironmentSource", "ProcessEnvironmentSource")
