"""The `work.runtime` name registry (gas-city-runtime-seam-adr.md Decision 1).

Lives in the config module's domain so the config schema can validate `work.runtime` against it
at load without importing the runtime seam (which reads config). `beadhive.runtime` is the
public face: it re-exports `register_runtime` / `available_runtimes` and binds the built-in
factories. Mirrors `claim_authority`'s authority registry: lookup is by name, nothing is
auto-discovered, a plugin registers itself.

A built-in name is seeded with no factory (`None`) so it validates before `beadhive.runtime` has
been imported; importing that module binds the real factory.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

RuntimeFactory = Callable[[Any, Any], Any]

BUILTIN_RUNTIMES = ("claude", "local", "temporal")

RUNTIMES: dict[str, RuntimeFactory | None] = dict.fromkeys(BUILTIN_RUNTIMES)


def register_runtime(name: str, factory: RuntimeFactory, *, replace: bool = False) -> None:
    """Register `factory` under `name`. A name that already has a factory is refused unless
    ``replace=True``, so two plugins cannot silently shadow each other or a built-in. A built-in
    name that is still unbound (`beadhive.runtime` not imported yet) may be bound once."""
    if not name or not isinstance(name, str):
        raise ValueError("a runtime name must be a non-empty string")
    if RUNTIMES.get(name) is not None and not replace:
        raise ValueError(f"runtime {name!r} is already registered (pass replace=True to override)")
    RUNTIMES[name] = factory


def available_runtimes() -> list[str]:
    """Every registered name, sorted — surfaced in the unknown-runtime error."""
    return sorted(RUNTIMES)


def unknown_runtime(name: str) -> ValueError:
    names = ", ".join(available_runtimes())
    return ValueError(f"unknown work runtime {name!r}; available: {names}")


def validate_runtime_name(name: str) -> str:
    """`name` itself when registered, else `ValueError` listing the available names."""
    if name not in RUNTIMES:
        raise unknown_runtime(name)
    return name
