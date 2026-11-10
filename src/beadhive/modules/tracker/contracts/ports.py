"""The Protocols of the bead tracker adapter port. Standard library only.

Small capability interfaces: :class:`TrackerReader` and :class:`TrackerIdentity` are required
and together form :class:`TrackerAdapter`, the one Protocol the plugin wiring binds.
:class:`CommentWriter`, :class:`LabelWriter`, :class:`LabelReader`, :class:`LabelDefiner`,
:class:`LabelPruner`, :class:`IssueCloser`, :class:`IssueReopener`, :class:`IssueTypeWriter`,
:class:`IssueFieldWriter`, :class:`IssueResolver` and :class:`MarkerCodec` are optional and are
discovered with ``isinstance`` on the bound object. A writer's presence is necessary, never
sufficient: the operation must also be enabled and each call must carry a fresh grant.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol, runtime_checkable

from .records import (
    Actor,
    CapabilityReport,
    CloseReason,
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
class LabelPruner(Protocol):
    """Optional (additive; the API version stays 1): delete repository label definitions the
    consumer no longer produces (``labels define --prune``). The same ``labels.define`` operation
    and the same repository scope as :class:`LabelDefiner`, so ``grant.target`` is the
    :class:`ProjectRef`; only owned names, refused for the whole batch before any request. A name
    the repository does not hold is already deleted (idempotent)."""

    def delete_labels(self, names: tuple[str, ...], *, grant: WriteGrant) -> WriteReceipt: ...


@runtime_checkable
class IssueCloser(Protocol):
    """Optional (additive; the API version stays 1): close one issue with a :class:`CloseReason`
    (``issue.close``). An issue-scoped write, so ``grant.target`` is the :class:`IssueKey`. It
    changes the issue's state and nothing else: never its labels, title or body. Closing an
    issue that is already closed is not an error (the receipt reports the state it holds).

    ``duplicate_of`` (``bh-iamru``) names the canonical issue a ``DUPLICATE`` close is a duplicate
    OF, so the tracker records the link and not just the reason. It must be another issue of the
    same project and goes only with ``CloseReason.DUPLICATE`` (``ValueError`` otherwise, before
    any request); a canonical the tracker cannot see is refused before the write, with the grant
    kept. ``None`` closes as a plain duplicate, naming no original."""

    def close_issue(
        self,
        issue: IssueKey,
        *,
        reason: CloseReason,
        grant: WriteGrant,
        duplicate_of: IssueKey | None = None,
    ) -> WriteReceipt: ...


@runtime_checkable
class IssueReopener(Protocol):
    """Optional (additive; the API version stays 1): reopen one closed issue (``issue.reopen``,
    ``bh-ih484``), the inverse of :class:`IssueCloser` for a close that triage later corrects. An
    issue-scoped write, so ``grant.target`` is the :class:`IssueKey`. It changes the issue's state
    and nothing else: never its labels, title or body. Reopening an issue that is already open is
    not an error (the receipt reports the state it holds)."""

    def reopen_issue(self, issue: IssueKey, *, grant: WriteGrant) -> WriteReceipt: ...


@runtime_checkable
class IssueTypeWriter(Protocol):
    """Optional (additive; the API version stays 1): set one issue's type (``issue.type``) to
    ``issue_type``, a type the tracker defines, by its name. An issue-scoped write, so
    ``grant.target`` is the :class:`IssueKey`. It changes the type and nothing else: never the
    labels, the state, the title or the body. A type the tracker does not define is refused
    (``PayloadRejected``, nothing written). The same operation's switch gates the read: an
    adapter reports ``TrackerIssue.issue_type`` only with ``issue.type`` enabled."""

    def set_issue_type(
        self, issue: IssueKey, issue_type: str, *, grant: WriteGrant
    ) -> WriteReceipt: ...


@runtime_checkable
class IssueFieldWriter(Protocol):
    """Optional (additive; the API version stays 1): set field values of one issue
    (``issue.fields``), ``values`` mapping a field's name to its value (a select field's option
    name). An issue-scoped write, so ``grant.target`` is the :class:`IssueKey`. Only the named
    fields change: never another field, the labels, the type or the state. A field or option the
    tracker does not define is refused before any write (``PayloadRejected``). The same
    operation's switch gates the read: ``TrackerIssue.fields`` only with ``issue.fields``
    enabled."""

    def set_issue_fields(
        self, issue: IssueKey, values: Mapping[str, str], *, grant: WriteGrant
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


def check_close(issue: IssueKey, reason: CloseReason, duplicate_of: IssueKey | None) -> None:
    """The shape every :class:`IssueCloser` refuses with ``ValueError`` before any request: the
    reason is a :class:`CloseReason`, and ``duplicate_of`` goes only with ``DUPLICATE`` and names
    another issue than the one closed. Whether it is an issue of the same project and visible is
    the adapter's to establish. Pure."""
    if not isinstance(reason, CloseReason):
        raise ValueError("a close needs a CloseReason")
    if duplicate_of is None:
        return
    if not isinstance(duplicate_of, IssueKey):
        raise ValueError("duplicate_of names the canonical issue by its IssueKey")
    if reason is not CloseReason.DUPLICATE:
        raise ValueError("duplicate_of goes only with CloseReason.DUPLICATE")
    if duplicate_of == issue:
        raise ValueError("an issue cannot be a duplicate of itself")


__all__ = [
    "CommentWriter",
    "IssueCloser",
    "IssueFieldWriter",
    "IssueReopener",
    "IssueResolver",
    "IssueTypeWriter",
    "LabelDefiner",
    "LabelPruner",
    "LabelReader",
    "LabelWriter",
    "MarkerCodec",
    "TrackerAdapter",
    "TrackerIdentity",
    "TrackerReader",
    "check_close",
    "join_marker_line",
]
