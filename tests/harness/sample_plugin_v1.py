"""Non-shipped sample implementation for the PluginManifest v1 authoring guide."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from beadhive.kernel.lifecycle import HostLifecycleContext


@runtime_checkable
class GreetingPort(Protocol):
    def greet(self, name: str) -> str: ...


@dataclass(frozen=True)
class GreetingProvider:
    prefix: str = "hello"

    def greet(self, name: str) -> str:
        return f"{self.prefix} {name}"


async def readiness_observer(context: HostLifecycleContext) -> None:
    """A successful best-effort observer with no ambient service lookup."""

    assert context.host_id


async def crashing_observer(_context: HostLifecycleContext) -> None:
    raise RuntimeError("sample observer failed")


async def slow_observer(_context: HostLifecycleContext) -> None:
    await asyncio.sleep(1)
