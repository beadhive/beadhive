"""The error taxonomy of the bead tracker adapter port. Standard library only.

Every message is already scrubbed of credentials by the adapter that raises it. ``retryable``
tells a consumer whether trying again later can succeed; adapters retry reads (and writes a
rate limit refused) only within their ``RetryBudget``, and never retry an ambiguous write.
"""

from __future__ import annotations

from datetime import timedelta

from .records import Operation


class TrackerError(Exception):
    """Base class of every port error."""

    retryable = False


class AuthenticationFailed(TrackerError):
    """The credential is invalid, revoked or expired."""


class PermissionDenied(TrackerError):
    """The credential is valid but may not perform ``operation``."""

    def __init__(self, message: str, *, operation: Operation | None, needed: str | None = None):
        super().__init__(message)
        self.operation = operation
        self.needed = needed  # the tracker's own name for the missing permission, when it says


class NotVisible(TrackerError):
    """Missing, or hidden from this credential (trackers often report forbidden as missing)."""


class TargetStateRefused(TrackerError):
    """Archived project, locked thread, comments disabled: a state problem, not a credential."""


class PayloadRejected(TrackerError):
    """Authorised, but the content was refused: the write **was** permitted."""


class RateLimited(TrackerError):
    """Refused by a rate limit (proof that nothing was written) and beyond the retry budget."""

    retryable = True

    def __init__(self, message: str, *, retry_after: timedelta | None, kind: str):
        super().__init__(message)
        self.retry_after = retry_after
        self.kind = kind  # "quota" (a per-window allowance) or "throttle" (a burst limit)


class TransientFailure(TrackerError):
    """A read failed for a reason that may clear (a server error, a dropped connection)."""

    retryable = True


class WriteOutcomeUnknown(TrackerError):
    """A write may or may not have happened (cut off in transit, a server error). Never retried
    by the adapter; the consumer reconciles by listing comments and matching
    ``OwnMarker.outbox_id``."""


class OperationNotEnabled(TrackerError):
    """Refused before any request: not enabled, wrong grant, reused grant, unsupported."""


class LabelNotOwned(OperationNotEnabled):
    """Refused before any request: a label outside ``LabelOwnership``."""


# -- IssueResolver refusals (amendment A1, external-intake-module-adr.md section 5) ----------


class UrlNotRecognized(TrackerError):
    """The URL is not one this adapter's grammar knows (another host, not an issue page)."""


class OtherProject(TrackerError):
    """The URL names an issue of a project other than the one this adapter is bound to."""


class ChangeRequestUrl(TrackerError):
    """The URL (or the item it names) is a change request, which is not a work item."""


__all__ = [
    "AuthenticationFailed",
    "ChangeRequestUrl",
    "LabelNotOwned",
    "NotVisible",
    "OperationNotEnabled",
    "OtherProject",
    "PayloadRejected",
    "PermissionDenied",
    "RateLimited",
    "TargetStateRefused",
    "TrackerError",
    "TransientFailure",
    "UrlNotRecognized",
    "WriteOutcomeUnknown",
]
