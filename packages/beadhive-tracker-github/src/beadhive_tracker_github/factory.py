"""The one call root's lazy provider row makes (``bootstrap/tracker.py``).

:func:`build` receives the already-validated, already-folded ``plugins.tracker-github`` fragment
(every operation flag equals the resolved enablement), the injected credential port, the
``ProjectRef("github", "owner/name")`` root derived from the hive identity, and the sleep seam.
It returns the :class:`~beadhive_tracker_github.adapter.GitHubTrackerAdapter`, which implements
``TrackerAdapter``, the optional ``IssueResolver`` and the writers that have landed
(``LabelWriter``). A writer is exposed on the bound object only when its operation is enabled, and
each call still needs a single-use grant.

Building sends no request, resolves no credential and imports no githubkit. Root maps any failure
here to a typed ``not-conforming`` refusal naming the exception class and never its text, and
every message raised here is static anyway, so nothing can leak configuration. The signature is
fixed by the plugin ADR (section 1, "Factory").
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from beadhive.modules.tracker.contracts import ProjectRef, TrackerAdapter
from beadhive_plugins.credentials import TokenSource

from .adapter import build_adapter

__all__ = ["build"]


def build(
    settings: Mapping[str, object],
    token_source: TokenSource,
    *,
    project: ProjectRef,
    sleep: Callable[[float], None],
) -> TrackerAdapter:
    """Return the GitHub ``TrackerAdapter`` for *project*."""
    if not isinstance(project, ProjectRef):
        raise TypeError("project must be a ProjectRef")
    if not callable(token_source) or not callable(sleep):
        raise TypeError("token_source and sleep must be callables")
    return build_adapter(settings, token_source, project=project, sleep=sleep)
