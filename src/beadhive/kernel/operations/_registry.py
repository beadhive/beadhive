"""Reload-stable binding to the kernel-owned operation-name catalog."""

from __future__ import annotations

from types import MappingProxyType
from typing import Final

_EMPTY_REGISTRY: Final = MappingProxyType({})

# ``importlib.reload`` executes in the existing module namespace.  Preserve the sealed catalog
# if this private support module itself is reloaded; a fresh process still starts empty and the
# package initializer binds it before importing the executor.
if "_CANONICAL_OPERATION_NAMES" not in globals():
    _CANONICAL_OPERATION_NAMES: MappingProxyType[str, str] = _EMPTY_REGISTRY


def bind_registered_operation_names(names: frozenset[str]) -> None:
    """Seal the trusted catalog once, allowing only an identical package reload."""
    global _CANONICAL_OPERATION_NAMES

    if any(type(name) is not str for name in names):
        raise TypeError("canonical operation names must be built-in strings")
    canonical = MappingProxyType({name: name for name in names})
    if _CANONICAL_OPERATION_NAMES:
        if _CANONICAL_OPERATION_NAMES != canonical:
            raise RuntimeError("canonical operation-name registry cannot change at runtime")
        return
    _CANONICAL_OPERATION_NAMES = canonical


def canonical_operation_name(value: object) -> str | None:
    """Return the trusted catalog string without consulting attacker-defined string methods."""
    if type(value) is not str:
        return None
    return _CANONICAL_OPERATION_NAMES.get(value)
