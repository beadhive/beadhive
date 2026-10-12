"""Driver-error classification by DB-API ``args[0]`` code (hq-storage-port-adr section 2).

The adapter never imports PyMySQL: an error is classified by its numeric ``args[0]`` alone (the
MySQL server or client error number every format-paramstyle MySQL driver reports there), plus
``TimeoutError`` and an optional root-injected deadline predicate for the operation budget.

Where an error happens decides what it means:

* **before the commit point** -- nothing can have committed: a serialization failure is a
  ``Conflict``, anything else is ``StorageUnavailable``;
* **at the commit point** (``COMMIT``, ``CALL DOLT_COMMIT``, ``CALL DOLT_PUSH``, an engine push)
  -- a serialization failure is a ``Conflict``; otherwise the outcome is ``Unknown`` with the
  reason :func:`unknown_reason` names;
* **after the commit point** (reading the result back) -- ``Unknown`` with reason
  ``"transport"`` (or ``"deadline"``).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Final

from beadhive_hq_storage import UnknownReason

__all__ = [
    "COMMIT_ERROR",
    "CONFLICT_CODES",
    "DEADLINE_CODES",
    "DUPLICATE_KEY",
    "DeadlinePredicate",
    "SERIALIZATION_FAILURE",
    "TRANSPORT_CODES",
    "error_code",
    "is_conflict",
    "is_deadline",
    "unknown_reason",
]

SERIALIZATION_FAILURE: Final = 1213
"""``ER_LOCK_DEADLOCK``: Dolt's serialization failure at commit. A lost CAS (condition 5)."""
LOCK_WAIT_TIMEOUT: Final = 1205
"""``ER_LOCK_WAIT_TIMEOUT``: treated like 1213, as root's publication CAS already does."""
CONFLICT_CODES: Final = frozenset({SERIALIZATION_FAILURE, LOCK_WAIT_TIMEOUT})
DUPLICATE_KEY: Final = 1062
"""``ER_DUP_ENTRY``: a racing insert of the same natural key."""
COMMIT_ERROR: Final = 1180
"""``ER_ERROR_DURING_COMMIT``: the server reported the commit itself failed mid-way."""
DEADLINE_CODES: Final = frozenset({3024, 1317})
"""``ER_QUERY_TIMEOUT`` and ``ER_QUERY_INTERRUPTED``: the statement budget ran out."""
TRANSPORT_CODES: Final = frozenset({2006, 2013, 2055})
"""``CR_SERVER_GONE_ERROR``, ``CR_SERVER_LOST``, ``CR_SERVER_LOST_EXTENDED``: the connection
dropped while the result was awaited."""

DeadlinePredicate = Callable[[BaseException], bool]
"""Root-injected: whether an exception is the transport's operation deadline (root's
``SqlTransportError("SQL operation deadline exceeded")`` carries no numeric code)."""


def error_code(exc: BaseException) -> int | None:
    """The DB-API ``args[0]`` error number, or ``None`` when the error carries none."""
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int) and not isinstance(args[0], bool):
        return args[0]
    return None


def is_conflict(exc: BaseException) -> bool:
    """A serialization failure (or lock-wait timeout): a lost race, reported as ``Conflict``."""
    return error_code(exc) in CONFLICT_CODES


def is_deadline(exc: BaseException, deadline: DeadlinePredicate | None = None) -> bool:
    if isinstance(exc, TimeoutError) or error_code(exc) in DEADLINE_CODES:
        return True
    return deadline is not None and bool(deadline(exc))


def unknown_reason(
    exc: BaseException, *, deadline: DeadlinePredicate | None = None
) -> UnknownReason:
    """The ``Unknown.reason`` for an error AT the commit point.

    ``"deadline"`` when the operation budget ran out; ``"transport"`` when the connection dropped
    while the acknowledgment was awaited (the request may have committed); ``"commit"`` for every
    other failure of the commit itself (``1180`` and any unclassified server or engine error)."""
    if is_deadline(exc, deadline):
        return "deadline"
    if error_code(exc) in TRANSPORT_CODES:
        return "transport"
    return "commit"
