"""The ``labels.change`` writer (W9): add and remove OWNED labels on one issue, under a grant.

This is the optional ``LabelWriter`` of the port (port ADR section 3), kept in its own module: a
mixin of :class:`~.tracker.GitHubTrackerAdapter`, composed in only when ``labels.change`` is
enabled (:mod:`.compose`), so a disabled write has no method at all. Every refusal below happens
**before any request is sent** (and, but for the last, before the credential is resolved), and
the tests assert it on the transport:

1. ``labels.change`` is not in the resolved ``enabled`` set: ``OperationNotEnabled``;
2. the grant is not a ``WriteGrant``, names another operation or another issue, or was already
   used: ``OperationNotEnabled``;
3. a label to add or remove lies outside the call's ``LabelOwnership`` (an empty ownership owns
   nothing) or, when the adapter was built from configuration, outside the configured
   ``write.labels.owned_prefixes``: ``LabelNotOwned``. Human labels are never touched;
4. the issue key names another repository: ``NotVisible``;
5. no credential resolved (an anonymous write cannot succeed): ``AuthenticationFailed``.

Only then is the grant **spent** (the adapter's one :class:`~.grants.GrantLedger`, shared with
every writer; durable single use is the consumer's outbox) and the requests sent, through the one
write seam :meth:`~.transport.Session.send` (a client that never follows a redirect). A grant is
spent by the first attempt and never refunded, whatever the answer: a write that might have applied
must not be repeated under the same approval, and a fresh approval is the operator's gate to issue.

Wire (GitHub REST, API version 2022-11-28): ``POST /issues/{n}/labels`` with
``{"labels": [...]}`` adds (idempotent; it creates a repository label that does not exist yet),
and ``DELETE /issues/{n}/labels/{name}`` removes one. Both answer the issue's complete label
list, which is the receipt's ``labels_after``: the write is never a read-modify-write of the whole
list, so a label a human added concurrently is never overwritten. A ``404`` on a delete means
either "the label is not on the issue" (the desired end state already holds) or "the issue is
invisible"; a follow-up read of the issue tells them apart without trusting message wording.

Outcomes: a rate limit (nothing was done) is retried within the budget on the injected sleep; a
``5xx``, a timeout, a dropped connection or an unreadable success body raises
``WriteOutcomeUnknown`` at once and is NEVER retried, and no further request of the same call is
sent; every other refusal is its scrubbed port error (:mod:`.diagnostics`). Adds go first, then
removes, in sorted order; a failure part-way leaves the earlier requests applied (the error says
how it ended; the consumer reconciles by reading the issue's labels).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

from beadhive.modules.tracker.contracts import (
    AuthenticationFailed,
    IssueKey,
    LabelNotOwned,
    LabelOwnership,
    NotVisible,
    Operation,
    OperationNotEnabled,
    WriteGrant,
    WriteOutcomeUnknown,
    WriteReceipt,
)

if TYPE_CHECKING:
    from .grants import GrantLedger
    from .transport import Session, Transport
    from .urls import GitHubLocation

__all__ = ["LabelWriting"]

_OPERATION = Operation.LABELS_CHANGE


def _label_names(body: Any) -> frozenset[str]:
    """The label names in a GitHub label list, or ``WriteOutcomeUnknown`` when it is not one."""
    if isinstance(body, list) and all(
        isinstance(row, dict) and isinstance(row.get("name"), str) for row in body
    ):
        return frozenset(row["name"] for row in body)
    raise WriteOutcomeUnknown(
        "GitHub answered a label write with something other than a label list; it may have applied"
    )


def _as_labels(value: Iterable[str], what: str) -> frozenset[str]:
    labels = frozenset(value)
    if not all(isinstance(label, str) and label.strip() for label in labels):
        raise ValueError(f"{what} labels must be non-empty strings")
    return labels


class LabelWriting:
    """Mixin: ``change_labels`` over the host adapter's location, transport and grant ledger."""

    # Provided by the host, ``GitHubTrackerAdapter``.
    _grants: GrantLedger
    _location: GitHubLocation
    _transport: Transport
    #: When built from configuration: the ``write.labels.owned_prefixes`` every label must ALSO
    #: satisfy (the call's own ``LabelOwnership`` can only narrow, never widen, the operator's
    #: choice). ``None``: only the call's ownership applies.
    _label_prefixes: tuple[str, ...] | None
    _require: Callable[[Operation], None]
    _issue_number: Callable[[IssueKey], int]

    def _authorise_labels(
        self,
        issue: IssueKey,
        add: frozenset[str],
        remove: frozenset[str],
        ownership: LabelOwnership,
        grant: WriteGrant,
    ) -> WriteGrant:
        """Every local refusal, in order (sends and spends nothing); returns the checked grant."""
        self._require(_OPERATION)
        checked = self._grants.check(grant, _OPERATION, issue)
        self._grants.refuse_if_spent(checked)
        if not isinstance(ownership, LabelOwnership):
            raise OperationNotEnabled("labels.change needs the LabelOwnership it is allowed")
        configured = None if self._label_prefixes is None else LabelOwnership(self._label_prefixes)
        foreign = sorted(
            label
            for label in add | remove
            if not ownership.owns(label) or (configured is not None and not configured.owns(label))
        )
        if foreign:
            raise LabelNotOwned(f"labels outside the owned set: {', '.join(foreign)}")
        return checked

    def change_labels(
        self,
        issue: IssueKey,
        *,
        add: frozenset[str],
        remove: frozenset[str],
        ownership: LabelOwnership,
        grant: WriteGrant,
    ) -> WriteReceipt:
        """Add ``add`` and remove ``remove`` (owned labels only) on ``issue``, once, under
        ``grant``. ``labels_after`` is the issue's complete label list after the last request."""
        add, remove = _as_labels(add, "add"), _as_labels(remove, "remove")
        if add & remove:
            raise ValueError("a label cannot be both added and removed")
        if not add and not remove:
            raise ValueError("nothing to change: add or remove at least one label")
        checked = self._authorise_labels(issue, add, remove, ownership, grant)
        path = f"{self._location.repo_path}/issues/{self._issue_number(issue)}"
        with self._transport.operation(_OPERATION, write=True) as session:
            if session.anonymous:
                raise AuthenticationFailed("labels.change needs a credential and none resolved")
            self._grants.spend(checked)
            after = self._send_changes(session, path, add, remove)
        return WriteReceipt(checked.grant_id, checked.operation, issue, labels_after=after)

    @staticmethod
    def _send_changes(
        session: Session, path: str, add: frozenset[str], remove: frozenset[str]
    ) -> frozenset[str]:
        after: frozenset[str] | None = None
        if add:
            reply = session.send("POST", f"{path}/labels", {"labels": sorted(add)})
            after = _label_names(reply.body)
        for name in sorted(remove):
            try:
                reply = session.send("DELETE", f"{path}/labels/{quote(name, safe='')}")
            except NotVisible:
                # Not on the issue (already the desired state) or the issue is invisible: a read
                # of the issue says which (it raises NotVisible itself in the second case).
                row = session.get(path).body
                labels = row.get("labels") if isinstance(row, dict) else None
                after = _label_names(labels)
                continue
            after = _label_names(reply.body)
        assert after is not None  # add or remove was non-empty
        return after
