"""Execution roots: the bounded places a key run may execute (``bh-mf1o4``).

An :class:`ExecutionRoot` is one directory the broker may schedule key runs on, with a kind
(``disk`` or ``tmpfs``) and a space bound. Under the local engine with roots configured, every
top-level key run is placed on **exactly one** root: its clean checkout and its hermetic fence
``TMPDIR`` scratch both live there, so a run never straddles two filesystems.

Disk space is one more reservation dimension. Inside the engine each root's space is a counted
pool named :meth:`ExecutionRoot.pool` (``root:<path>``), so admission, release, EASY backfill and
nested carving treat it exactly like ``dolt-servers``. A ``tmpfs`` root's space is RAM, so a
run's disk reservation on a tmpfs root is also added to its memory reservation.

Nothing here touches the filesystem: free space is read through the :data:`DiskSpace` port.
"""

from __future__ import annotations

import enum
import posixpath
from collections.abc import Callable, Sequence
from dataclasses import dataclass

__all__ = [
    "ROOT_POOL_PREFIX",
    "DiskSpace",
    "ExecutionRoot",
    "RootKind",
    "validate_roots",
]

#: Counted-pool prefix reserved for execution-root disk space. A configured pool may not use it.
ROOT_POOL_PREFIX = "root:"

#: Free bytes on the filesystem holding a root path. Production passes
#: :func:`~beadhive_validation_broker.statvfs_free`; tests pass
#: :class:`~beadhive_validation_broker.FakeDiskSpace`. A probe that raises makes that root count
#: as full (nothing admitted to it), never the whole engine down.
DiskSpace = Callable[[str], int]


class RootKind(enum.StrEnum):
    DISK = "disk"
    TMPFS = "tmpfs"  # RAM-backed: its usage also counts against memory


@dataclass(frozen=True)
class ExecutionRoot:
    """One bounded execution root: an absolute directory, its kind and its space bound."""

    path: str
    kind: RootKind
    max_bytes: int

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            raise ValueError(f"an execution root path must be absolute: {self.path!r}")
        if self.max_bytes <= 0:
            raise ValueError(f"execution root {self.path} needs max_bytes > 0")
        object.__setattr__(self, "path", posixpath.normpath(self.path))
        object.__setattr__(self, "kind", RootKind(self.kind))

    @property
    def pool(self) -> str:
        """The counted pool that carries this root's disk space inside the engine."""
        return ROOT_POOL_PREFIX + self.path

    @property
    def memory_backed(self) -> bool:
        return self.kind is RootKind.TMPFS


def _within(child: str, parent: str) -> bool:
    return child == parent or child.startswith(parent if parent == "/" else parent + "/")


def validate_roots(roots: Sequence[ExecutionRoot]) -> tuple[ExecutionRoot, ...]:
    """Roots in preference order; refuses duplicates and one root nested inside another.

    A nested pair would let one run's directory count against two bounds at once, and "exactly
    one root per run" would stop meaning one location.
    """
    ordered = tuple(roots)
    for i, first in enumerate(ordered):
        for second in ordered[i + 1 :]:
            if _within(first.path, second.path) or _within(second.path, first.path):
                raise ValueError(
                    f"execution roots must be distinct and not nested: {first.path} "
                    f"and {second.path}"
                )
    return ordered
