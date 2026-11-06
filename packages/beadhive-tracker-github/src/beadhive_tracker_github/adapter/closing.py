"""The ``issue.close`` writer: ``IssueCloser.close_issue`` for GitHub (``bh-pr5ll``).

``PATCH /repos/{owner}/{repo}/issues/{number}`` with exactly ``{"state": "closed",
"state_reason": <reason>}``, plus ``duplicate_issue_id`` for a duplicate that names its
canonical. It is a mixin of
:class:`~beadhive_tracker_github.adapter.tracker.GitHubTrackerAdapter`, composed in only when
``issue.close`` is enabled (:mod:`.compose`), so a disabled write has no method at all.

**Only the state is sent.** The same endpoint edits the title, the body, the assignees and the
labels, and a ``labels`` member REPLACES the issue's whole label set (a human's label would be
lost), so the payload is built here from the two state members alone and never from anything
the caller passes through. ``state_reason`` only takes effect together with ``state``
(github/docs#22914), so both always travel. :data:`STATE_REASONS` maps the port's
:class:`~beadhive.modules.tracker.contracts.CloseReason` to GitHub's vocabulary.

**Enabling is necessary, never sufficient.** A call is refused **before any request** (and before
the credential is resolved) when, in this order:

1. ``issue.close`` is not in the adapter's resolved ``enabled`` set (``OperationNotEnabled``);
2. ``grant`` is not a ``WriteGrant``, names another operation, or names another issue
   (``OperationNotEnabled``);
3. the issue key is not an issue of this repository (``NotVisible``, a pure URL check), or the
   reason is not a ``CloseReason`` (``ValueError``);
4. the grant was already presented to this adapter (spent).

The credential is then resolved; an anonymous one is refused with ``AuthenticationFailed`` and the
grant is **kept**. Only then is the grant spent, immediately before the one request, and never
refunded (:mod:`.grants`).

**One request, never retried on an ambiguous outcome.** A rate limit (nothing was written) is
retried within the budget on the injected sleep; a ``5xx``, a timeout, a dropped connection, an
unconfirmed redirect or an answer that is not a closed issue raises ``WriteOutcomeUnknown`` and is
never retried: the consumer reconciles by reading the issue's state back. Closing an issue that is
already closed is accepted by GitHub and answers the closed issue, so it is a success here too.

**A duplicate names its canonical** (``bh-iamru``). With ``duplicate_of`` the same PATCH also
carries ``duplicate_issue_id``: the canonical's numeric issue ``id`` (not its number), which
GitHub's "Update an issue" documents as "the ID of the issue that this issue duplicates, used when
marking this issue as a duplicate with ... state_reason: duplicate" (GraphQL's
``closeIssue(input: {duplicateIssueId})`` is the same link by node id). The ``id`` is read with ONE
``GET .../issues/{n}`` of the canonical inside the write's session, after the credential is
resolved and BEFORE the grant is spent, so a canonical that is missing, a pull request, moved to
another repository or unreadable is refused (``NotVisible``, a proof of no write) with the grant
kept. ``duplicate_of`` must be an issue of this repository and goes only with ``duplicate``
(``ValueError``, before any request).

**No live GitHub write happens in the test suite**; the wire form is asserted on the fake.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import TYPE_CHECKING

from beadhive.modules.tracker.contracts import (
    AuthenticationFailed,
    CloseReason,
    IssueKey,
    IssueState,
    NotVisible,
    Operation,
    PermissionDenied,
    RateLimited,
    TrackerError,
    WriteGrant,
    WriteOutcomeUnknown,
    WriteReceipt,
    check_close,
)

from . import mapping
from .grants import GrantLedger

if TYPE_CHECKING:
    from .transport import Session, Transport
    from .urls import GitHubLocation

__all__ = ["STATE_REASONS", "IssueClosing", "close_payload"]

_OPERATION = Operation.ISSUE_CLOSE

#: GitHub's ``state_reason`` for each port reason (REST API version 2022-11-28).
STATE_REASONS: Mapping[CloseReason, str] = {
    CloseReason.COMPLETED: "completed",
    CloseReason.NOT_PLANNED: "not_planned",
    CloseReason.DUPLICATE: "duplicate",
}


def close_payload(reason: CloseReason, duplicate_issue_id: int | None = None) -> dict[str, object]:
    """The request body: the state and its reason, and for a duplicate that names its canonical
    the canonical's numeric issue id; nothing else (pure)."""
    if not isinstance(reason, CloseReason):
        raise ValueError("a close needs a CloseReason")
    body: dict[str, object] = {"state": "closed", "state_reason": STATE_REASONS[reason]}
    if duplicate_issue_id is not None:
        if reason is not CloseReason.DUPLICATE:
            raise ValueError("duplicate_issue_id goes only with the duplicate reason")
        body["duplicate_issue_id"] = duplicate_issue_id
    return body


#: The canonical read's own refusals, already a proof that nothing was written.
_PROOFS = (AuthenticationFailed, PermissionDenied, NotVisible, RateLimited)


class IssueClosing:
    """Mixin: ``close_issue`` over the host adapter's location, transport and grant ledger."""

    # Provided by the host, ``GitHubTrackerAdapter``.
    _grants: GrantLedger
    _location: GitHubLocation
    _transport: Transport
    _require: Callable[[Operation], None]
    _issue_number: Callable[[IssueKey], int]

    def close_issue(
        self,
        issue: IssueKey,
        *,
        reason: CloseReason,
        grant: WriteGrant,
        duplicate_of: IssueKey | None = None,
    ) -> WriteReceipt:
        self._require(_OPERATION)
        ledger = self._grants
        checked = ledger.check(grant, _OPERATION, issue)
        number = self._issue_number(issue)
        check_close(issue, reason, duplicate_of)
        canonical = self._issue_number(duplicate_of) if duplicate_of is not None else None
        ledger.refuse_if_spent(checked)  # read-only: spending waits for the credential
        path = f"{self._location.repo_path}/issues/{number}"
        with self._transport.operation(_OPERATION, write=True) as session:
            if session.anonymous:
                raise AuthenticationFailed(
                    "issue.close needs a credential; none was resolved (anonymous)"
                )
            duplicate_id = None
            if canonical is not None:  # one read, before the grant is spent
                duplicate_id = self._canonical_id(session, canonical)
            payload = close_payload(reason, duplicate_id)
            ledger.spend(checked)  # only now: a missing credential never burns the approval
            reply = session.send("PATCH", path, payload)
        try:
            closed = mapping.issue(reply.body, self._location.canonical_url)
        except mapping.PayloadUnreadable:
            raise WriteOutcomeUnknown(
                "GitHub accepted the close but its answer could not be read; it may have applied"
            ) from None
        if closed.state is not IssueState.CLOSED:
            raise WriteOutcomeUnknown(
                "GitHub answered the close with an issue that is not closed; read it back"
            )
        return WriteReceipt(
            checked.grant_id,
            checked.operation,
            issue,
            state_after=closed.state,
            closed_reason=closed.closed_reason,
        )

    def _canonical_id(self, session: Session, number: int) -> int:
        """The numeric ``id`` of canonical issue ``number`` of this repository (one GET). Every
        failure is a refusal before the write: nothing has been sent yet."""
        label = f"the canonical #{number}"
        try:
            row = session.get(f"{self._location.repo_path}/issues/{number}").body
            if mapping.is_change_request(row):
                raise NotVisible(f"{label} is a pull request, not an issue")
            parsed = self._location.parse_html_url(mapping.issue_key(row).url)
        except _PROOFS:
            raise
        except TrackerError as exc:
            raise NotVisible(f"{label} could not be read ({type(exc).__name__})") from None
        if parsed is None or not self._location.same_repository(parsed[0], parsed[1]):
            raise NotVisible(f"{label} was moved to another repository")
        ident = row.get("id")
        if not isinstance(ident, int) or isinstance(ident, bool):
            raise NotVisible(f"{label} has no numeric issue id")
        return ident
