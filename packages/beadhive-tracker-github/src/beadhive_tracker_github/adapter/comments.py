"""The ``comment.create`` writer: ``CommentWriter.post_comment`` for GitHub (W8, ``bh-03s4w``).

``POST /repos/{owner}/{repo}/issues/{number}/comments`` with the body plus the ``bh:v1``
own-message marker (:mod:`.marker`). It is a mixin of
:class:`~beadhive_tracker_github.adapter.tracker.GitHubTrackerAdapter`, composed in only when
``comment.create`` is enabled (:mod:`.compose`), so a disabled write has no method at all.

**Enabling is necessary, never sufficient.** A call is refused with ``OperationNotEnabled``
**before any request** (and before the credential is resolved) when, in this order:

1. ``comment.create`` is not in the adapter's resolved ``enabled`` set;
2. ``grant`` is not a ``WriteGrant``, names another operation, or names another issue;
3. the issue key is not an issue of this repository (``NotVisible``, a pure URL check), or the
   marker cannot be encoded (``ValueError``);
4. the grant was already presented to this adapter (spent).

The credential is then resolved; one that does not resolve (anonymous, or a failing source) is
refused with ``AuthenticationFailed`` and the grant is **kept**, so a missing token never burns an
operator approval. Only after the credential resolves is the grant spent, immediately before the
request is sent, and it is never refunded whatever happens next (:mod:`.grants`).

**One request, never retried on an ambiguous outcome.** The write is sent once. GitHub refusing
it with a rate limit (proof that nothing was written) is retried within the ``RetryBudget`` on
the injected sleep; every other refusal raises its port error carrying the exact, scrubbed
one-line diagnostic (:mod:`.diagnostics`). A ``5xx``, a timeout or a dropped connection after the
send, an unconfirmed redirect, or a ``201`` whose body cannot be read raises
``WriteOutcomeUnknown`` and is never retried: the comment may exist exactly once, and the
consumer finds it again by ``OwnMarker.outbox_id`` (``list_comments``).

GitHub never accepts an anonymous comment, so that refusal is local and sends nothing.

**No live GitHub write happens in the test suite.** The first real write is an operator-approved
step recorded on bead ``bh-03s4w``; until it is exercised, every refusal diagnostic except ``401``
and ``404`` is the documented form (``tests/recorded/comment_create_refusals.json``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

from beadhive.modules.tracker.contracts import (
    AuthenticationFailed,
    IssueKey,
    Operation,
    OwnMarker,
    WriteGrant,
    WriteOutcomeUnknown,
    WriteReceipt,
)

from . import mapping
from .grants import GrantLedger
from .marker import encode_marker

if TYPE_CHECKING:
    from .transport import Transport
    from .urls import GitHubLocation

__all__ = ["CommentCreate", "comment_payload"]

_OPERATION = Operation.COMMENT_CREATE


def comment_payload(body: str, marker: OwnMarker) -> dict[str, str]:
    """The request body: ``body`` with the marker as its last line (pure)."""
    if not isinstance(body, str):
        raise TypeError("a comment body must be a string")
    if not isinstance(marker, OwnMarker):
        raise TypeError("a comment needs an OwnMarker")
    return {"body": encode_marker(body, marker)}


class CommentCreate:
    """Mixin: ``post_comment`` over the host adapter's location, transport and grant ledger."""

    # Provided by the host, ``GitHubTrackerAdapter``.
    _grants: GrantLedger
    _location: GitHubLocation
    _transport: Transport
    _require: Callable[[Operation], None]
    _issue_number: Callable[[IssueKey], int]

    def post_comment(
        self,
        issue: IssueKey,
        body: str,
        *,
        marker: OwnMarker,
        grant: WriteGrant,
    ) -> WriteReceipt:
        self._require(_OPERATION)
        ledger = self._grants
        checked = ledger.check(grant, _OPERATION, issue)
        number = self._issue_number(issue)
        payload = comment_payload(body, marker)
        ledger.refuse_if_spent(checked)  # read-only: spending waits for the credential
        path = f"{self._location.repo_path}/issues/{number}/comments"
        with self._transport.operation(_OPERATION, write=True) as session:
            if session.anonymous:
                raise AuthenticationFailed(
                    "comment.create needs a credential; none was resolved (anonymous)"
                )
            ledger.spend(checked)  # only now: a missing credential never burns the approval
            reply = session.send("POST", path, payload)
        try:
            posted = mapping.comment(reply.body)
        except mapping.PayloadUnreadable:
            raise WriteOutcomeUnknown(
                "GitHub accepted the comment but its answer could not be read; it may have applied"
            ) from None
        return WriteReceipt(checked.grant_id, checked.operation, issue, comment=posted)
