"""Callback adapter for the existing Git inventory and adopted status classifier."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Generic, TypeVar

from ..domain import ManagedWorktree, WorktreeInventoryRequest, WorktreeStatusRequest

StatusRowT = TypeVar("StatusRowT")


class CallbackWorktreeInventory(Generic[StatusRowT]):
    """Keep dynamic facade lookups while translating raw identity tuples once."""

    def __init__(
        self,
        *,
        inventory_reader: Callable[[str], Iterable[tuple[str, str, str]]],
        status_reader: Callable[[str], Iterable[StatusRowT]],
    ) -> None:
        self._inventory_reader = inventory_reader
        self._status_reader = status_reader

    def inventory(self, request: WorktreeInventoryRequest) -> tuple[ManagedWorktree, ...]:
        return tuple(
            ManagedWorktree(hive, path, branch)
            for hive, path, branch in self._inventory_reader(request.hive)
        )

    def status(self, request: WorktreeStatusRequest) -> tuple[StatusRowT, ...]:
        return tuple(self._status_reader(request.hive))
