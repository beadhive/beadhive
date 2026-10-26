"""The ``labels.change`` writer (W9): add and remove OWNED labels on one issue, under a grant.

This is the optional ``LabelWriter`` of the port (port ADR section 3), kept in its own module and
mixed into :class:`~.tracker.GitHubTrackerAdapter`. Every refusal below happens **before any
request is sent** (and before the credential is resolved), and the tests assert it on the
transport:

1. ``labels.change`` is not in the resolved ``enabled`` set: ``OperationNotEnabled``;
2. the grant is not a ``WriteGrant``, names another operation or another issue, or was already
   used: ``OperationNotEnabled``;
3. a label to add or remove lies outside the call's ``LabelOwnership`` (an empty ownership owns
   nothing) or, when the adapter was built from configuration, outside the configured
   ``write.labels.owned_prefixes``: ``LabelNotOwned``. Human labels are never touched;
4. the issue key names another repository: ``NotVisible``;
5. no credential resolved (an anonymous write cannot succeed): ``AuthenticationFailed``.

Only then is the grant **spent** (an in-memory ledger, atomic under a lock; durable single use is
the consumer's outbox) and the requests sent. A grant is spent by the first attempt and never
refunded, whatever the answer: a write that might have applied must not be repeated under the
same approval, and a fresh approval is the operator's gate to issue.

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

import threading
from collections.abc import Iterable
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

from .transport import Session, Transport
from .urls import GitHubLocation

__all__ = ["GrantLedger", "LabelWriting"]


class GrantLedger:
    """The grants this adapter instance has spent. Atomic; never persisted, never printed."""

    def __init__(self) -> None:
        self._spent: set[str] = set()
        self._lock = threading.Lock()

    def refuse_if_spent(self, grant_id: str) -> None:
        with self._lock:
            spent = grant_id in self._spent
        if spent:
            raise OperationNotEnabled("the grant was already used; a write needs a fresh grant")

    def spend(self, grant_id: str) -> None:
        with self._lock:
            if grant_id in self._spent:
                raise OperationNotEnabled("the grant was already used; a write needs a fresh grant")
            self._spent.add(grant_id)


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
    """``change_labels`` for the adapter; mixed into :class:`~.tracker.GitHubTrackerAdapter`.

    The host class provides the members declared below (its constructor state and gates)."""

    _enabled: frozenset[Operation]
    _location: GitHubLocation
    _transport: Transport
    #: When built from configuration: the ``write.labels.owned_prefixes`` every label must ALSO
    #: satisfy (the call's own ``LabelOwnership`` can only narrow, never widen, the operator's
    #: choice). ``None``: only the call's ownership applies.
    _label_prefixes: tuple[str, ...] | None

    if TYPE_CHECKING:

        def _require(self, operation: Operation) -> None: ...

        def _issue_number(self, key: IssueKey) -> int: ...

    def _grants(self, operation: Operation) -> GrantLedger:
        ledgers: dict[Operation, GrantLedger] = self.__dict__.setdefault("_grant_ledgers", {})
        return ledgers.setdefault(operation, GrantLedger())

    def _authorise_labels(
        self,
        issue: IssueKey,
        add: frozenset[str],
        remove: frozenset[str],
        ownership: LabelOwnership,
        grant: WriteGrant,
    ) -> int:
        """Every local refusal, in order; returns the issue number (sends and spends nothing)."""
        self._require(Operation.LABELS_CHANGE)
        if not isinstance(grant, WriteGrant):
            raise OperationNotEnabled("labels.change needs an operator-approved WriteGrant")
        if grant.operation is not Operation.LABELS_CHANGE:
            raise OperationNotEnabled("the grant does not cover labels.change")
        if grant.target != issue:
            raise OperationNotEnabled("the grant does not cover this issue")
        self._grants(Operation.LABELS_CHANGE).refuse_if_spent(grant.grant_id)
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
        return self._issue_number(issue)

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
        number = self._authorise_labels(issue, add, remove, ownership, grant)
        path = f"{self._location.repo_path}/issues/{number}"
        with self._transport.operation(Operation.LABELS_CHANGE) as session:
            if session.anonymous:
                raise AuthenticationFailed("labels.change needs a credential and none resolved")
            self._grants(Operation.LABELS_CHANGE).spend(grant.grant_id)
            after = self._send(session, path, add, remove)
        return WriteReceipt(grant.grant_id, grant.operation, issue, labels_after=after)

    @staticmethod
    def _send(
        session: Session, path: str, add: frozenset[str], remove: frozenset[str]
    ) -> frozenset[str]:
        after: frozenset[str] | None = None
        if add:
            reply = session.write("POST", f"{path}/labels", {"labels": sorted(add)})
            after = _label_names(reply.body)
        for name in sorted(remove):
            try:
                reply = session.write("DELETE", f"{path}/labels/{quote(name, safe='')}")
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
