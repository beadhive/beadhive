"""The Protocols of the bead tracker adapter port. Standard library only.

Small capability interfaces: :class:`TrackerReader` and :class:`TrackerIdentity` are required
and together form :class:`TrackerAdapter`, the one Protocol the plugin wiring binds.
:class:`CommentWriter`, :class:`LabelWriter`, :class:`LabelReader`, :class:`LabelDefiner`,
:class:`IssueResolver` and :class:`MarkerCodec` are
optional and are discovered with ``isinstance`` on the bound object. A writer's presence is
necessary, never sufficient: the operation must also be enabled and each call must carry a fresh
grant.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from .records import (
    Actor,
    CapabilityReport,
    CommentQuery,
    IdPage,
    IdQuery,
    IssueKey,
    IssueQuery,
    LabelDefinition,
    LabelOwnership,
    OwnMarker,
    Page,
    TrackerComment,
    TrackerDescriptor,
    TrackerIssue,
    WriteGrant,
    WriteReceipt,
)


@runtime_checkable
class TrackerReader(Protocol):
    def describe(self) -> TrackerDescriptor: ...

    def list_issues(self, query: IssueQuery) -> Page[TrackerIssue]: ...

    def list_comments(self, query: CommentQuery) -> Page[TrackerComment]: ...

    def list_ids(self, query: IdQuery) -> IdPage: ...


@runtime_checkable
class TrackerIdentity(Protocol):
    def whoami(self) -> Actor | None: ...

    def probe_capabilities(self) -> CapabilityReport: ...  # read-only requests only


@runtime_checkable
class TrackerAdapter(TrackerReader, TrackerIdentity, Protocol):
    """The bead tracker adapter port: the required part every adapter implements."""


@runtime_checkable
class CommentWriter(Protocol):
    def post_comment(
        self, issue: IssueKey, body: str, *, marker: OwnMarker, grant: WriteGrant
    ) -> WriteReceipt: ...


@runtime_checkable
class LabelWriter(Protocol):
    def change_labels(
        self,
        issue: IssueKey,
        *,
        add: frozenset[str],
        remove: frozenset[str],
        ownership: LabelOwnership,
        grant: WriteGrant,
    ) -> WriteReceipt: ...


@runtime_checkable
class LabelReader(Protocol):
    """Optional (additive; the API version stays 1): the label definitions of the repository.

    One read; ``labels.read`` is a read operation, so no grant is needed."""

    def list_label_definitions(self) -> tuple[LabelDefinition, ...]: ...


@runtime_checkable
class LabelDefiner(Protocol):
    """Optional (additive; the API version stays 1): create or update repository label
    definitions. A repository-scoped write, so ``grant.target`` is the :class:`ProjectRef`."""

    def define_labels(
        self, definitions: tuple[LabelDefinition, ...], *, grant: WriteGrant
    ) -> WriteReceipt: ...


@runtime_checkable
class IssueResolver(Protocol):
    """Optional URL resolution (amendment A1; the API version stays 1).

    ``resolve`` is pure (no request): it canonicalises a pasted URL into the adapter's
    :class:`IssueKey`, raising ``UrlNotRecognized``, ``OtherProject`` or ``ChangeRequestUrl``.
    ``get`` performs one read and raises ``NotVisible`` or ``ChangeRequestUrl``. Without it a
    consumer pages ``list_issues`` and matches ``IssueKey.url``, and the distinct refusals
    collapse into one "not found in project"."""

    def resolve(self, url: str) -> IssueKey: ...

    def get(self, key: IssueKey) -> TrackerIssue: ...


@runtime_checkable
class MarkerCodec(Protocol):
    """Optional: the adapter's own-message marker line, as it would append it (additive; the API
    version stays 1).

    ``marker_line`` is pure (no request, no clock): it returns the exact footer line the adapter's
    ``CommentWriter.post_comment`` appends for ``marker``, joined to the body by
    :func:`join_marker_line`, so a consumer can render, digest and show the bytes an approved
    write would post without importing the adapter's package. It
    raises ``ValueError`` for a marker field its grammar cannot carry. The encoding stays
    adapter-owned and versioned; the port carries only :class:`OwnMarker`."""

    def marker_line(self, marker: OwnMarker) -> str: ...


def join_marker_line(body: str, line: str) -> str:
    """The exact comment bytes a :class:`CommentWriter` posts for ``body`` and the footer
    ``line`` its :class:`MarkerCodec` returns: the body with trailing whitespace removed, one
    blank line, then the line; a body that is empty (or only whitespace) posts the line alone.

    This is the port's one joining rule (``bh-crf27``). An adapter implementing
    :class:`MarkerCodec` posts ``join_marker_line(body, marker_line(marker))`` from
    ``post_comment``, and a consumer renders, leak-checks and digests the same call, so the bytes
    an operator approves are the bytes posted. The line itself stays adapter-owned. Pure."""
    text = body.rstrip()
    return f"{text}\n\n{line}" if text else line


__all__ = [
    "CommentWriter",
    "IssueResolver",
    "LabelDefiner",
    "LabelReader",
    "LabelWriter",
    "MarkerCodec",
    "TrackerAdapter",
    "TrackerIdentity",
    "TrackerReader",
    "join_marker_line",
]
