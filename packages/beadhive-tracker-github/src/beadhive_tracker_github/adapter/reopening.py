"""The ``issue.reopen`` writer: ``IssueReopener.reopen_issue`` for GitHub (``bh-ih484``).

``PATCH /repos/{owner}/{repo}/issues/{number}`` with exactly ``{"state": "open"}``. It is the
inverse of :mod:`.closing` for a close that triage later corrects, and a mixin of
:class:`~beadhive_tracker_github.adapter.tracker.GitHubTrackerAdapter`, composed in only when
``issue.reopen`` is enabled (:mod:`.compose`; ``write.issue_reopen.enabled``, default off), so a
disabled write has no method at all.

**Only the state is sent.** The same endpoint edits the title, the body, the assignees and the
labels, and a ``labels`` member REPLACES the issue's whole label set (a human's label would be
lost), so the payload is built here from the one state member alone and never from anything the
caller passes through. Reopening clears GitHub's ``state_reason``; none is sent.

**Enabling is necessary, never sufficient.** A call is refused **before any request** (and before
the credential is resolved) when, in this order:

1. ``issue.reopen`` is not in the adapter's resolved ``enabled`` set (``OperationNotEnabled``);
2. ``grant`` is not a ``WriteGrant``, names another operation (the close's grant does not reopen),
   or names another issue (``OperationNotEnabled``);
3. the issue key is not an issue of this repository (``NotVisible``, a pure URL check);
4. the grant was already presented to this adapter (spent).

The credential is then resolved; an anonymous one is refused with ``AuthenticationFailed`` and the
grant is **kept**. Only then is the grant spent, immediately before the one request, and never
refunded (:mod:`.grants`).

**One request, never retried on an ambiguous outcome.** A rate limit (nothing was written) is
retried within the budget on the injected sleep; a ``5xx``, a timeout, a dropped connection, an
unconfirmed redirect or an answer that is not an open issue raises ``WriteOutcomeUnknown`` and is
never retried: the consumer reconciles by reading the issue's state back. Reopening an issue that
is already open is accepted by GitHub and answers the open issue, so it is a success here too.

**No live GitHub write happens in the test suite**; the wire form is asserted on the fake.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from beadhive.modules.tracker.contracts import (
    AuthenticationFailed,
    IssueKey,
    IssueState,
    Operation,
    WriteGrant,
    WriteOutcomeUnknown,
    WriteReceipt,
)

from . import mapping
from .grants import GrantLedger

if TYPE_CHECKING:
    from .transport import Transport
    from .urls import GitHubLocation

__all__ = ["IssueReopening", "reopen_payload"]

_OPERATION = Operation.ISSUE_REOPEN


def reopen_payload() -> dict[str, str]:
    """The request body: the state, and nothing else (pure)."""
    return {"state": "open"}


class IssueReopening:
    """Mixin: ``reopen_issue`` over the host adapter's location, transport and grant ledger."""

    # Provided by the host, ``GitHubTrackerAdapter``.
    _grants: GrantLedger
    _location: GitHubLocation
    _transport: Transport
    _require: Callable[[Operation], None]
    _issue_number: Callable[[IssueKey], int]

    def reopen_issue(self, issue: IssueKey, *, grant: WriteGrant) -> WriteReceipt:
        self._require(_OPERATION)
        ledger = self._grants
        checked = ledger.check(grant, _OPERATION, issue)
        number = self._issue_number(issue)
        ledger.refuse_if_spent(checked)  # read-only: spending waits for the credential
        path = f"{self._location.repo_path}/issues/{number}"
        with self._transport.operation(_OPERATION, write=True) as session:
            if session.anonymous:
                raise AuthenticationFailed(
                    "issue.reopen needs a credential; none was resolved (anonymous)"
                )
            ledger.spend(checked)  # only now: a missing credential never burns the approval
            reply = session.send("PATCH", path, reopen_payload())
        try:
            reopened = mapping.issue(reply.body, self._location.canonical_url)
        except mapping.PayloadUnreadable:
            raise WriteOutcomeUnknown(
                "GitHub accepted the reopen but its answer could not be read; it may have applied"
            ) from None
        if reopened.state is not IssueState.OPEN:
            raise WriteOutcomeUnknown(
                "GitHub answered the reopen with an issue that is not open; read it back"
            )
        return WriteReceipt(checked.grant_id, checked.operation, issue, state_after=reopened.state)
