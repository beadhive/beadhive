"""Port error types (hq-storage-port-adr section 2).

A serialization failure at commit or ``rowcount 0`` on a CAS is a ``Conflict`` outcome, never
an error. Messages never carry a secret.
"""

from __future__ import annotations

__all__ = [
    "CapabilityUnsupported",
    "IntegrityViolation",
    "InvalidRequest",
    "NotProvisioned",
    "StorageError",
    "StorageUnavailable",
]


class StorageError(Exception):
    """Base of every port error."""


class StorageUnavailable(StorageError):
    """Nothing was sent: the mutation is definitely not applied."""


class NotProvisioned(StorageError):
    """The schema, row, table or revision the operation needs is absent (for example a CAS
    from ``ABSENT`` on an UPDATE-only kind, or a fence that is not cut over)."""


class CapabilityUnsupported(StorageError):
    """The adapter refuses an optional capability (for example ``consistent_read`` on Git)."""


class InvalidRequest(StorageError):
    """Malformed key, body or id; refused before any I/O."""


class IntegrityViolation(StorageError):
    """Store state breaks a port invariant (a second writer row, a regressed epoch). Fails
    closed and is never retried."""
