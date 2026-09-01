"""Narrow, capability-owned views over a configuration value source.

The view is deliberately ignorant of the legacy facade.  Composition code supplies a
``ConfigValueSourcePort`` and an exact allowlist; consumers can therefore depend on only the
settings their capability owns while the outward adapter preserves historical patch points.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Protocol


class ConfigValueSourcePort(Protocol):
    """Resolve or replace one named value at the configured compatibility boundary."""

    def get_value(self, name: str) -> Any: ...

    def set_value(self, name: str, value: Any) -> None: ...

    def delete_value(self, name: str) -> None: ...


class CapabilitySettings:
    """Attribute-shaped, exact allowlisted configuration port for one capability."""

    __slots__ = ("_capability", "_names", "_source")

    def __init__(
        self,
        capability: str,
        names: Iterable[str],
        source: ConfigValueSourcePort,
    ) -> None:
        object.__setattr__(self, "_capability", capability)
        object.__setattr__(self, "_names", frozenset(names))
        object.__setattr__(self, "_source", source)

    @property
    def capability(self) -> str:
        return self._capability

    @property
    def allowed_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._names))

    def __getattr__(self, name: str) -> Any:
        if name not in self._names:
            raise AttributeError(f"{self._capability} configuration port does not expose {name!r}")
        return self._source.get_value(name)

    def __setattr__(self, name: str, value: Any) -> None:
        if name.startswith("_"):
            object.__setattr__(self, name, value)
            return
        if name not in self._names:
            raise AttributeError(f"{self._capability} configuration port does not expose {name!r}")
        self._source.set_value(name, value)

    def __delattr__(self, name: str) -> None:
        if name not in self._names:
            raise AttributeError(f"{self._capability} configuration port does not expose {name!r}")
        self._source.delete_value(name)


__all__ = ("CapabilitySettings", "ConfigValueSourcePort")
