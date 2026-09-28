"""bd's coordination surface — `bd gate` (create/check/resolve), `bd merge-slot`
(create/acquire/release/check), `bd heartbeat`, and `bd reclaim` (bh-c6dk.3).

The implementations moved to the ``beadhive-bd-cli`` library package's
``beadhive_bd_cli.coordination`` (bh-o3xuf): Beads 1.3 has no HTTP route for any
``beadhive_core.COORDINATION_OPERATIONS`` operation, so every one of them is a ``bd`` argv route and
lives with the others. This module is the shell's stable name for them: each function forwards to
the package, resolved lazily by name (:mod:`beadhive.bd_cli`), and supplies root's own ``bd``
invocation seam (:mod:`beadhive.bd`, the configured ``Engine``) as the transport — so wrapping
here adds no second subprocess-calling seam. The result types (``SlotStatus``,
``SlotAcquireResult``, ``ReclaimResult``, …) are the package's, re-exported by attribute.
"""

from __future__ import annotations

from typing import Any

from . import bd_cli

_RESULT_TYPES = frozenset(
    {
        "GateCheckResult",
        "GateCreateResult",
        "GateResolveResult",
        "HeartbeatResult",
        "ReclaimResult",
        "ReclaimedIssue",
        "SlotAcquireResult",
        "SlotReleaseResult",
        "SlotStatus",
    }
)


def _impl() -> Any:
    return bd_cli.package().coordination


def __getattr__(name: str) -> Any:
    if name in _RESULT_TYPES:
        return getattr(_impl(), name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def gate_create(cwd: Any, **kwargs: Any) -> Any:
    """`bd gate create --blocks <blocks> --type <gate_type> [...] --json` (see the package)."""
    return _impl().gate_create(bd_cli.transport(), cwd, **kwargs)


def gate_check(cwd: Any, **kwargs: Any) -> Any:
    """`bd gate check [--type T] [--dry-run] [--escalate] [--limit N] --json`."""
    return _impl().gate_check(bd_cli.transport(), cwd, **kwargs)


def gate_resolve(cwd: Any, gate_id: str, **kwargs: Any) -> Any:
    """`bd gate resolve <gate_id> [--reason R]`."""
    return _impl().gate_resolve(bd_cli.transport(), cwd, gate_id, **kwargs)


def merge_slot_create(cwd: Any, **kwargs: Any) -> bool:
    """`bd merge-slot create --json` — idempotent."""
    return bool(_impl().merge_slot_create(bd_cli.transport(), cwd, **kwargs))


def merge_slot_check(cwd: Any, **kwargs: Any) -> Any:
    """`bd merge-slot check --json`."""
    return _impl().merge_slot_check(bd_cli.transport(), cwd, **kwargs)


def merge_slot_acquire(cwd: Any, holder: str, **kwargs: Any) -> Any:
    """`bd merge-slot acquire --holder <holder> [--wait] --json`."""
    return _impl().merge_slot_acquire(bd_cli.transport(), cwd, holder, **kwargs)


def merge_slot_release(cwd: Any, **kwargs: Any) -> Any:
    """`bd merge-slot release [--holder H] --json`."""
    return _impl().merge_slot_release(bd_cli.transport(), cwd, **kwargs)


def heartbeat(cwd: Any, bead_id: str, **kwargs: Any) -> Any:
    """`bd heartbeat <id> --json` — refresh the lease on `bead_id`."""
    return _impl().heartbeat(bd_cli.transport(), cwd, bead_id, **kwargs)


def reclaim(cwd: Any, **kwargs: Any) -> Any:
    """`bd reclaim [...] --json` — revert stale leases."""
    return _impl().reclaim(bd_cli.transport(), cwd, **kwargs)
