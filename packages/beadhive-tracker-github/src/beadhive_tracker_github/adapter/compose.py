"""Structural narrowing: a write method exists only when its operation is enabled.

tracker-adapter-plugin-adr.md section 1 ("Factory"): the package returns an object whose writer
methods exist only for write operations whose flags are on. :func:`adapter_class` composes the
read-path :class:`~.tracker.GitHubTrackerAdapter` with the writer mixin of every ENABLED write
operation; :func:`new_adapter` builds one. Root's own ``narrow`` view does the same again on the
bound object, so a disabled write is absent at both layers, and a present writer still refuses a
call without a fresh grant.

Each writer bead adds one row to :data:`WRITERS`.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from beadhive.modules.tracker.contracts import Operation

from .comments import CommentCreate
from .labels import LabelWriting
from .tracker import GitHubTrackerAdapter

__all__ = ["WRITERS", "adapter_class", "new_adapter"]

#: ``(operation, mixin)`` for every write operation this package implements.
WRITERS: tuple[tuple[Operation, type], ...] = (
    (Operation.COMMENT_CREATE, CommentCreate),
    (Operation.LABELS_CHANGE, LabelWriting),
)

_CLASSES: dict[frozenset[Operation], type[GitHubTrackerAdapter]] = {}


def adapter_class(enabled: Iterable[Operation]) -> type[GitHubTrackerAdapter]:
    """The adapter class carrying exactly the writers of the enabled write operations."""
    wanted = frozenset(op for op, _ in WRITERS if op in frozenset(enabled))
    if wanted not in _CLASSES:
        mixins = tuple(mixin for op, mixin in WRITERS if op in wanted)
        if not mixins:
            _CLASSES[wanted] = GitHubTrackerAdapter
        else:
            name = "GitHubTrackerAdapter" + "".join(m.__name__ for m in mixins)
            _CLASSES[wanted] = type(name, (*mixins, GitHubTrackerAdapter), {})
    return _CLASSES[wanted]


def new_adapter(
    *, enabled: frozenset[Operation] = frozenset(), **kwargs: Any
) -> GitHubTrackerAdapter:
    """A ``GitHubTrackerAdapter`` with the writers of ``enabled`` composed in."""
    return adapter_class(enabled)(enabled=enabled, **kwargs)
