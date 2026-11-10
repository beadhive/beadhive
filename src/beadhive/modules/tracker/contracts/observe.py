"""An optional, cheap observer of an adapter's blocking I/O (``bh-17g6b``).

A host that wants to know where a command spent its time installs an :class:`Observer` for the
current context with :func:`observing`; an adapter marks each blocking call it makes with
:func:`observe_begin` and :func:`observe_end` (or wraps a call in :func:`observe_call`). With no
observer installed (the default) a mark costs one context-variable read and nothing else, so an
adapter pays nothing for a host that does not measure. The observer is held in a
:class:`~contextvars.ContextVar` and reset when :func:`observing` exits, so nothing leaks between
commands or between the iterations of a long-lived process.

Standard library only; the names are a free-form string chosen by the adapter (for example
``github_request``), never a tracker-specific value the host must understand.
"""

from __future__ import annotations

import contextlib
import contextvars
from collections.abc import Callable, Iterator
from typing import Any, Protocol, TypeVar

__all__ = ["Observer", "observe_begin", "observe_call", "observe_end", "observing"]

_T = TypeVar("_T")


class Observer(Protocol):
    """What a host installs: ``begin`` marks the start of one named call and returns a handle,
    ``end`` closes that handle (also when the call raised)."""

    def begin(self, name: str) -> Any: ...

    def end(self, handle: Any) -> None: ...


_OBSERVER: contextvars.ContextVar[Observer | None] = contextvars.ContextVar(
    "tracker_observer", default=None
)


@contextlib.contextmanager
def observing(observer: Observer) -> Iterator[None]:
    """Install ``observer`` for the current context, restoring the previous one on exit."""
    token = _OBSERVER.set(observer)
    try:
        yield
    finally:
        _OBSERVER.reset(token)


def observe_begin(name: str) -> tuple[Observer, Any] | None:
    """Mark the start of the call ``name``; ``None`` (and nothing to close) when not observed."""
    observer = _OBSERVER.get()
    if observer is None:
        return None
    return observer, observer.begin(name)


def observe_end(mark: tuple[Observer, Any] | None) -> None:
    """Close what :func:`observe_begin` returned (a no-op for ``None``)."""
    if mark is not None:
        mark[0].end(mark[1])


def observe_call(name: str, call: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """``call(*args, **kwargs)``, observed as ``name`` when an observer is installed."""
    mark = observe_begin(name)
    try:
        return call(*args, **kwargs)
    finally:
        observe_end(mark)
