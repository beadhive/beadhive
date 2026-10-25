"""Stand-ins for the GitHub adapter: just enough surface to be bound and inspected offline.

``build_adapter`` shows the plugin-layer rule that a write method is ABSENT (not merely refused)
until its operation is enabled, so ``isinstance(port, CommentWriter)`` is False by default.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bh-gj1hb"))

from credentials_proto import ResolvedToken, TokenSource, scrub  # noqa: E402
from tracker_contracts import (  # noqa: E402
    Actor,
    ActorKey,
    CapabilityReport,
    CapabilityState,
    Operation,
    ProjectRef,
    TrackerDescriptor,
    Tri,
)


class ReadOnlyStub:
    """Satisfies ``TrackerAdapter`` (reader + identity) and nothing else."""

    def __init__(self, repo: str, token_source: TokenSource, enabled: frozenset[Operation]):
        self._repo = repo
        self._token_source = token_source
        self._enabled = enabled

    def describe(self) -> TrackerDescriptor:
        return TrackerDescriptor(ProjectRef("github", self._repo), 1, frozenset(Operation))

    def list_issues(self, query):  # pragma: no cover - not exercised
        raise NotImplementedError

    def list_comments(self, query):  # pragma: no cover
        raise NotImplementedError

    def list_ids(self, query):  # pragma: no cover
        raise NotImplementedError

    def whoami(self) -> Actor | None:
        token = self._token_source()
        if token is None:
            return None
        return Actor(ActorKey("583231", "human"), "octocat")

    def probe_capabilities(self) -> CapabilityReport:
        token = self._token_source()
        probe = Tri.UNKNOWN if token else Tri.NO
        reason = "token grants are not readable" if token else "no credential"
        states = {
            op: CapabilityState(op, True, op in self._enabled, probe, reason) for op in Operation
        }
        return CapabilityReport(self.whoami(), token.source if token else None, states)


class CommentOnlyStub(ReadOnlyStub):
    def post_comment(self, issue, body, *, marker, grant):  # pragma: no cover
        raise NotImplementedError


class LabelsOnlyStub(ReadOnlyStub):
    def change_labels(self, issue, *, add, remove, ownership, grant):  # pragma: no cover
        raise NotImplementedError


class WritingStub(CommentOnlyStub, LabelsOnlyStub):
    """Both writers; only built when both write operations are enabled."""


def enabled_operations(settings: Mapping) -> frozenset[Operation]:
    """The resolved ``enabled`` set the adapter is constructed with (plugin flag already folded)."""
    read, write = settings.get("read", {}), settings.get("write", {})
    ops = set()
    if settings.get("enabled") is True:
        if read.get("issues", {}).get("enabled") is True:
            ops.add(Operation.ISSUES_READ)
        if read.get("comments", {}).get("enabled") is True:
            ops.add(Operation.COMMENTS_READ)
        if write.get("comment", {}).get("enabled") is True:
            ops.add(Operation.COMMENT_CREATE)
        if write.get("labels", {}).get("enabled") is True:
            ops.add(Operation.LABELS_CHANGE)
    return frozenset(ops)


def build_adapter(settings: Mapping, token_source: TokenSource) -> ReadOnlyStub:
    """The package factory root's lazy loader would call. It returns an object with writer
    methods only for the write operations the operator enabled."""
    ops = enabled_operations(settings)
    cls = {
        (False, False): ReadOnlyStub,
        (True, False): CommentOnlyStub,
        (False, True): LabelsOnlyStub,
        (True, True): WritingStub,
    }[(Operation.COMMENT_CREATE in ops, Operation.LABELS_CHANGE in ops)]
    return cls(str(settings.get("repo") or ""), token_source, ops)


__all__ = ["ResolvedToken", "build_adapter", "enabled_operations", "scrub"]
