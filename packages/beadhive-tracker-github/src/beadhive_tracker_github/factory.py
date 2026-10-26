"""The one call root's lazy provider row makes (``bootstrap/tracker.py``).

Skeleton: no adapter exists yet, so :func:`build` refuses with a typed error. Root maps any
factory failure to a typed ``not-conforming`` refusal that names the exception class and never
its text, so nothing here can leak configuration. The read path replaces the body; the
signature is fixed by the plugin ADR (section 1, "Factory").
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from beadhive.modules.tracker.contracts import ProjectRef, TrackerAdapter
from beadhive_plugins.credentials import TokenSource

__all__ = ["TrackerAdapterNotImplemented", "build"]


class TrackerAdapterNotImplemented(NotImplementedError):
    """The GitHub adapter is a package skeleton: there is nothing to build yet."""


def build(
    settings: Mapping[str, object],
    token_source: TokenSource,
    *,
    project: ProjectRef,
    sleep: Callable[[float], None],
) -> TrackerAdapter:
    """Return the GitHub ``TrackerAdapter`` for *project* (not implemented in the skeleton)."""
    raise TrackerAdapterNotImplemented(
        "the beadhive-tracker-github read path is not implemented yet"
    )
