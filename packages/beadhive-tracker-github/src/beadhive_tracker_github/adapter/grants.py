"""The in-process half of single-use write grants (tracker-adapter-port-adr.md section 3).

Every write call carries its own :class:`~beadhive.modules.tracker.contracts.WriteGrant`, minted
by the consumer's operator-approved gate for ONE operation on ONE issue. The adapter refuses,
**before any request**, a call whose grant is missing, names another operation or issue, or was
already presented. Durable single use across processes is the consumer's outbox; this ledger is
the plugin ADR's in-process layer ("one adapter per (hive, store scope) per process", so the
spent set lives as long as the cached adapter).

A grant is **spent by the first call that passes its writer's local checks and resolves a
credential**, whatever happens next: a refusal, an unknown outcome or a success; it is never
refunded. So a grant can never authorise a second attempt, and a consumer that needs one mints a
fresh grant through a fresh approval. Every writer resolves the credential first and spends only
then, immediately before it sends, so a missing or failing credential never burns an approval.

Messages are static and never repeat a grant id (a grant is an approval artefact, not a secret,
but nothing here needs to show it).
"""

from __future__ import annotations

import threading

from beadhive.modules.tracker.contracts import (
    IssueKey,
    Operation,
    OperationNotEnabled,
    WriteGrant,
)

__all__ = ["GrantLedger"]


class GrantLedger:
    """The grants this adapter instance has seen; spending is atomic.

    ONE ledger serves every writer (``comment.create``, ``labels.change``): a grant names its
    operation, so the spent set is keyed by ``(operation, grant_id)`` and each writer's
    single-use rule is exactly per operation. Each writer decides WHEN it spends (after all of its
    own local refusals); a spent grant is never refunded."""

    def __init__(self) -> None:
        self._spent: set[tuple[Operation, str]] = set()
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"GrantLedger(spent={len(self._spent)})"

    @staticmethod
    def check(grant: object, operation: Operation, target: IssueKey) -> WriteGrant:
        """Refuse a grant that is not a ``WriteGrant`` for exactly ``operation`` on ``target``."""
        if not isinstance(grant, WriteGrant):
            raise OperationNotEnabled(
                f"{operation.value} needs an operator-approved single-use WriteGrant"
            )
        if grant.operation is not operation:
            raise OperationNotEnabled(f"the grant does not cover {operation.value}")
        if grant.target != target:
            raise OperationNotEnabled(f"the grant does not cover {target.identifier}")
        return grant

    def refuse_if_spent(self, grant: WriteGrant) -> None:
        """Refuse ``grant`` when it was already spent, without spending it."""
        with self._lock:
            spent = (grant.operation, grant.grant_id) in self._spent
        if spent:
            raise OperationNotEnabled("the grant was already used; a write needs a fresh one")

    def spend(self, grant: WriteGrant) -> None:
        """Mark ``grant`` used, refusing one already presented (atomic across threads)."""
        key = (grant.operation, grant.grant_id)
        with self._lock:
            if key in self._spent:
                raise OperationNotEnabled("the grant was already used; a write needs a fresh one")
            self._spent.add(key)
