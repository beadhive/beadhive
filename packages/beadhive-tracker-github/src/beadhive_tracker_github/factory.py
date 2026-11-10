"""The one call root's lazy provider row makes (``bootstrap/tracker.py``).

:func:`build` receives the already-validated, already-folded ``plugins.tracker-github`` fragment
(every operation flag equals the resolved enablement), the injected credential port, the
``ProjectRef("github", "owner/name")`` root derived from the hive identity, and the sleep seam.
It returns a :class:`~beadhive_tracker_github.adapter.GitHubTrackerAdapter`, which implements
``TrackerAdapter`` and the optional ``IssueResolver``, plus the writer of each ENABLED write
operation that has landed (``comment.create``: ``CommentWriter``; ``labels.change``:
``LabelWriter``; ``labels.define``: ``LabelDefiner`` and ``LabelPruner``; ``issue.close``:
``IssueCloser``; ``issue.reopen``: ``IssueReopener``; ``issue.type``: ``IssueTypeWriter``;
``issue.fields``: ``IssueFieldWriter``); a disabled write has no method on the returned object,
and an enabled one still needs a fresh operator-approved grant per call.

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
    command_scoped: bool = False,
) -> TrackerAdapter:
    """Return the GitHub ``TrackerAdapter`` for *project*. ``command_scoped``: it lives for one
    command, so it may reuse one HTTP client and the org field definitions (never a daemon's)."""
    if not isinstance(project, ProjectRef):
        raise TypeError("project must be a ProjectRef")
    if not callable(token_source) or not callable(sleep):
        raise TypeError("token_source and sleep must be callables")
    return build_adapter(
        settings, token_source, project=project, sleep=sleep, command_scoped=command_scoped
    )
