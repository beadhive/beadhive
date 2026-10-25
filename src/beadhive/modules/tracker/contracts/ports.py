"""The Protocols of the bead tracker adapter port. Standard library only.

Small capability interfaces: :class:`TrackerReader` and :class:`TrackerIdentity` are required
and together form :class:`TrackerAdapter`, the one Protocol the plugin wiring binds.
:class:`CommentWriter`, :class:`LabelWriter` and :class:`IssueResolver` are optional and are
discovered with ``isinstance`` on the bound object. A writer's presence is necessary, never
sufficient: the operation must also be enabled and each call must carry a fresh grant.
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
class IssueResolver(Protocol):
    """Optional URL resolution (amendment A1; the API version stays 1).

    ``resolve`` is pure (no request): it canonicalises a pasted URL into the adapter's
    :class:`IssueKey`, raising ``UrlNotRecognized``, ``OtherProject`` or ``ChangeRequestUrl``.
    ``get`` performs one read and raises ``NotVisible`` or ``ChangeRequestUrl``. Without it a
    consumer pages ``list_issues`` and matches ``IssueKey.url``, and the distinct refusals
    collapse into one "not found in project"."""

    def resolve(self, url: str) -> IssueKey: ...

    def get(self, key: IssueKey) -> TrackerIssue: ...


__all__ = [
    "CommentWriter",
    "IssueResolver",
    "LabelWriter",
    "TrackerAdapter",
    "TrackerIdentity",
    "TrackerReader",
]
