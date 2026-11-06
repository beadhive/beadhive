"""The ``issue.type`` and ``issue.fields`` writers for GitHub (``bh-poqy0``).

* ``issue.type`` (``IssueTypeWriter.set_issue_type``): ``PATCH /repos/{owner}/{repo}/issues/{n}``
  with exactly ``{"type": <name>}``. The same endpoint edits the title, the body, the state and
  the labels, and a ``labels`` member REPLACES the issue's whole label set, so the payload is
  built here from the type alone. GitHub answers the issue; a type the organization does not
  define is its ``422`` (``PayloadRejected``: nothing written).
* ``issue.fields`` (``IssueFieldWriter.set_issue_fields``): ``POST
  /repos/{owner}/{repo}/issues/{n}/issue-field-values`` with ``{"issue_field_values":
  [{"field_id": <id>, "value": <option name>}]}``, which ADDS the named values and keeps every
  other field (``PUT`` would replace them all, so it is never used). The field ids are the
  organization's: ONE ``GET /orgs/{owner}/issue-fields`` resolves each configured name to its id
  and checks a select field's option, BEFORE the grant is spent, so an unknown field or option
  is ``PayloadRejected`` with the grant kept. GitHub answers the issue's field values.

The endpoints were checked against GitHub's REST reference (docs.github.com ``/rest/issues/
issue-field-values``, ``/rest/orgs/issue-fields`` and ``/rest/issues/issues#update-an-issue``,
2026-11-06); both are served on API version 2022-11-28.

Both are mixins of :class:`~beadhive_tracker_github.adapter.tracker.GitHubTrackerAdapter`,
composed in only when their operation is enabled (:mod:`.compose`), and refuse **before any
request** exactly as ``issue.close`` does: not enabled, then a grant that is not a ``WriteGrant``
for this operation and issue, then a key of another repository, then a spent grant. An anonymous
credential is ``AuthenticationFailed`` with the grant kept; the grant is spent immediately before
the one write and never refunded. An ambiguous outcome (a ``5xx``, a timeout, an answer that does
not show the value) is ``WriteOutcomeUnknown`` and is never retried: the consumer reconciles by
reading the issue back. **No live GitHub write happens in the test suite.**
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING, Any

from beadhive.modules.tracker.contracts import (
    AuthenticationFailed,
    IssueKey,
    Operation,
    PayloadRejected,
    WriteGrant,
    WriteOutcomeUnknown,
    WriteReceipt,
)

from . import mapping
from .grants import GrantLedger

if TYPE_CHECKING:
    from .transport import Session, Transport
    from .urls import GitHubLocation

__all__ = ["IssueFieldWriting", "IssueTyping", "field_payload", "type_payload"]


def type_payload(issue_type: str) -> dict[str, str]:
    """The ``issue.type`` request body: the type's name and nothing else (pure)."""
    if not isinstance(issue_type, str) or not issue_type.strip():
        raise ValueError("an issue type needs a name")
    return {"type": issue_type}


def field_payload(values: Mapping[str, str], definitions: Any) -> dict[str, list[dict[str, Any]]]:
    """The ``issue.fields`` request body from the organization's field ``definitions`` (the
    ``GET /orgs/{org}/issue-fields`` answer): each named field's id and its value. A field the
    organization does not define, or a select option it does not offer, is ``PayloadRejected``
    (pure: no request)."""
    if not isinstance(definitions, list):
        raise mapping.PayloadUnreadable("GitHub returned issue fields that are not a list")
    by_name = {
        row["name"]: row
        for row in definitions
        if isinstance(row, dict) and isinstance(row.get("name"), str)
    }
    entries: list[dict[str, Any]] = []
    for name, value in sorted(values.items()):
        row = by_name.get(name)
        if row is None or not isinstance(row.get("id"), int):
            raise PayloadRejected(f"the organization defines no issue field named {name!r}")
        if row.get("data_type") in ("single_select", "multi_select"):
            options = {o.get("name") for o in row.get("options") or () if isinstance(o, dict)}
            if value not in options:
                raise PayloadRejected(f"the issue field {name!r} offers no option {value!r}")
        entries.append({"field_id": row["id"], "value": value})
    return {"issue_field_values": entries}


class _ValueWriting:
    """The refusals and the one write both value writers share (host: ``GitHubTrackerAdapter``)."""

    _grants: GrantLedger
    _location: GitHubLocation
    _transport: Transport
    _require: Callable[[Operation], None]
    _issue_number: Callable[[IssueKey], int]

    def _checked(self, operation: Operation, issue: IssueKey, grant: WriteGrant):
        self._require(operation)
        ledger = self._grants
        checked = ledger.check(grant, operation, issue)
        number = self._issue_number(issue)
        return ledger, checked, number

    @staticmethod
    def _signed_in(session: Session, operation: Operation) -> None:
        if session.anonymous:
            raise AuthenticationFailed(
                f"{operation.value} needs a credential; none was resolved (anonymous)"
            )


class IssueTyping(_ValueWriting):
    """Mixin: ``set_issue_type`` over the host adapter's location, transport and grant ledger."""

    def set_issue_type(
        self, issue: IssueKey, issue_type: str, *, grant: WriteGrant
    ) -> WriteReceipt:
        operation = Operation.ISSUE_TYPE
        ledger, checked, number = self._checked(operation, issue, grant)
        payload = type_payload(issue_type)
        ledger.refuse_if_spent(checked)  # read-only: spending waits for the credential
        path = f"{self._location.repo_path}/issues/{number}"
        with self._transport.operation(operation, write=True) as session:
            self._signed_in(session, operation)
            ledger.spend(checked)  # only now: a missing credential never burns the approval
            reply = session.send("PATCH", path, payload)
        try:
            after = mapping.issue_type(reply.body)
        except mapping.PayloadUnreadable:
            after = None
        if after is None or after.casefold() != issue_type.casefold():
            raise WriteOutcomeUnknown(
                "GitHub answered the type change without that type; read the issue back"
            )
        return WriteReceipt(checked.grant_id, checked.operation, issue, type_after=after)


class IssueFieldWriting(_ValueWriting):
    """Mixin: ``set_issue_fields`` over the host adapter's location, transport and ledger."""

    def set_issue_fields(
        self, issue: IssueKey, values: Mapping[str, str], *, grant: WriteGrant
    ) -> WriteReceipt:
        operation = Operation.ISSUE_FIELDS
        ledger, checked, number = self._checked(operation, issue, grant)
        if not values or not all(isinstance(v, str) and v for v in values.values()):
            raise ValueError("an issue.fields write needs at least one field and its value")
        ledger.refuse_if_spent(checked)
        path = f"{self._location.repo_path}/issues/{number}/issue-field-values"
        with self._transport.operation(operation, write=True) as session:
            self._signed_in(session, operation)
            # the field ids are the organization's: one read, before the grant is spent
            fields = session.get(f"/orgs/{self._location.owner}/issue-fields")
            payload = field_payload(values, fields.body)
            ledger.spend(checked)
            reply = session.send("POST", path, payload)
        try:
            after = mapping.field_values(reply.body)
        except mapping.PayloadUnreadable:
            after = None
        if after is None or any(dict(after).get(k) != v for k, v in values.items()):
            raise WriteOutcomeUnknown(
                "GitHub answered the field change without those values; read the issue back"
            )
        return WriteReceipt(checked.grant_id, checked.operation, issue, fields_after=after)
