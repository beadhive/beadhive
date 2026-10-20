"""Per-hive lock and cross-host ownership for a remote-relocation transaction (ADR P1).

Local exclusion is a deliberate re-export of ``hive_migration_lock`` (relocation and engine-mode
migration must exclude each other; stale-PID detection retained). Cross-host ownership reuses the
existing HQ hive lease / primary model (:func:`beadhive.guard.primary_state`): a transaction may
only be driven from the host that holds the hive's lease. No new mechanism.
"""

from __future__ import annotations

import contextlib
import socket
from pathlib import Path

from .storage_migrate import HiveLocked, hive_migration_lock

__all__ = [
    "HiveLocked",
    "NotLeaseHolder",
    "hive_migration_lock",
    "holder_host_id",
    "require_lease",
    "transaction_lock",
]


class NotLeaseHolder(RuntimeError):
    """This host does not hold the hive's lease; the message names the holder and the remedy."""


def holder_host_id() -> str:
    """This host's minted identity (``host.host_id``), falling back to the hostname."""
    from . import host

    try:
        return host.host_id()
    except FileNotFoundError:
        return socket.gethostname()


def require_lease(cfg: dict, hive_id: str, hive_dir: Path | None = None) -> str:
    """Refuse (``NotLeaseHolder``) unless this host holds ``hive_id``'s lease; returns the host
    id. Where the multi-host model is not in force (no HQ clone, never adopted) there is nothing
    to gate and the single-host default applies, exactly as ``guard.guard_primary`` does."""
    from . import guard

    state = guard.primary_state(hive_id, cfg=cfg, hive_dir=hive_dir)
    me = holder_host_id()
    if state is None:
        return me
    prefix, this_host, lease = state
    refusal = guard._not_primary(prefix, this_host, lease, verb="migrate", reason="transaction")
    if refusal:
        raise NotLeaseHolder(refusal)
    return this_host or me


@contextlib.contextmanager
def transaction_lock(cfg: dict, hive_id: str, hive_dir: Path | None = None):
    """Hold the lease check AND the per-hive migration lock; yields this host's id, to pass to
    :class:`~beadhive.migration_ledger.PhaseLedger` as ``host``."""
    host_id = require_lease(cfg, hive_id, hive_dir)
    with hive_migration_lock(cfg, hive_id):
        yield host_id
