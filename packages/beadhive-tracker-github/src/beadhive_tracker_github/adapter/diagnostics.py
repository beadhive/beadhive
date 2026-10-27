"""The exact, scrubbed one-line refusal diagnostics and their port errors.

The wording is the ``bh-mqvdq`` table adopted by tracker-adapter-port-adr.md section 2 ("The
first refused write gets an exact, scrubbed one-line diagnostic"). The same lines name a refused
read, so an operator sees one vocabulary whichever operation hit the refusal:

======================================  ====================================================
GitHub answered                         Diagnostic (and port error)
======================================  ====================================================
``401``                                 bad credentials naming the token's PROVENANCE
                                        (``AuthenticationFailed``)
``403`` + ``X-GitHub-SSO``              authorize the token for the organization (SAML SSO)
                                        (``PermissionDenied``)
``403`` "Resource not accessible ..."   the token lacks the permission, naming the
                                        ``X-Accepted-GitHub-Permissions`` value
                                        (``PermissionDenied`` with ``needed``)
``403``/``429`` + ``Retry-After`` or    a rate limit, NOT a permission refusal
``X-RateLimit-Remaining: 0``            (``RateLimited``; decided in :mod:`.retry`)
``403`` "archived" / "locked"           a state problem, not a credential problem
                                        (``TargetStateRefused``)
``404``                                 invisible to this token (GitHub reports "forbidden"
                                        as "not found") (``NotVisible``)
``410``                                 issues are disabled (``TargetStateRefused``); on one
                                        issue "This issue was deleted" (``NotVisible``)
``422``                                 payload rejected AFTER authorization: the write was
                                        permitted (``PayloadRejected``)
======================================  ====================================================

Only ``401`` and ``404`` were observed live (``bh-mqvdq`` Evidence 10, on read-only GETs); the
other rows are the documented forms. The ``comment.create`` write path is checked against a
replay of every row (``tests/recorded/comment_create_refusals.json``, each exchange labelled with
its evidence); re-verifying them against LIVE responses waits for the first operator-approved
write, recorded on bead ``bh-03s4w``.

Every line is one line, at most :data:`MESSAGE_LIMIT` characters of upstream text, and passes
through :func:`beadhive_plugins.credentials.scrub` (the exact resolved token value, then every
token shape). ``source`` is always the credential's provenance (``env:GITHUB_TOKEN``), never its
value.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from beadhive.modules.tracker.contracts import (
    AuthenticationFailed,
    NotVisible,
    Operation,
    PayloadRejected,
    PermissionDenied,
    TargetStateRefused,
    TrackerError,
)
from beadhive_plugins.credentials import scrub

__all__ = [
    "ANONYMOUS",
    "MESSAGE_LIMIT",
    "clean",
    "diagnose",
    "is_rate_limited",
    "refusal_error",
]

#: How much upstream text a diagnostic repeats.
MESSAGE_LIMIT = 160

#: The provenance named when no credential was sent.
ANONYMOUS = "no credential (anonymous)"


def clean(text: str, secrets: Sequence[str] = ()) -> str:
    """Upstream text made safe to show: scrubbed, one line, bounded."""
    return " ".join(scrub(text, secrets).split())[:MESSAGE_LIMIT]


def _header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is None:
        value = headers.get(name.lower())
    return value


def is_rate_limited(status: int, headers: Mapping[str, str]) -> bool:
    """A rate-limit answer: proof that nothing was done, so the request may be retried."""
    if status == 429:
        return True
    return status == 403 and (
        _header(headers, "retry-after") is not None
        or _header(headers, "x-ratelimit-remaining") == "0"
    )


def diagnose(
    status: int,
    headers: Mapping[str, str],
    message: str,
    *,
    source: str | None,
    secrets: Sequence[str] = (),
) -> str:
    """The one-line diagnostic for a refused request (``message`` is GitHub's ``message``)."""
    message = clean(message, secrets)
    lowered = message.lower()
    provenance = clean(source or ANONYMOUS, secrets)
    if status == 401:
        return f"401 bad credentials: the token from {provenance} is invalid, revoked or expired"
    if is_rate_limited(status, headers):
        return f"{status} rate limited (not a permission refusal): back off; do NOT mark writes off"
    if status == 403:
        if _header(headers, "x-github-sso") is not None:
            return "403 SSO: authorize this token for the organization (SAML SSO) and retry"
        if "archived" in lowered:
            return "403 repository is archived (read-only)"
        if "locked" in lowered:
            return "403 conversation is locked (not a credential problem)"
        if "resource not accessible" in lowered:
            accepted = clean(_header(headers, "x-accepted-github-permissions") or "", secrets)
            need = f" (endpoint needs {accepted})" if accepted else ""
            return f"403 token lacks the permission{need}: {message}"
        return f"403 refused: {message or 'no message'}"
    if status == 404:
        return (
            "404: the repository or issue is invisible to this token (GitHub reports "
            "'forbidden' as 'not found'); check the token's repository access and scope"
        )
    if status == 410:
        if "deleted" in lowered:
            return "410 the issue was deleted"
        return "410 issues are disabled on this repository"
    if status == 422:
        return f"422 payload rejected after authorization ({message}); NOT a capability failure"
    return f"{status} unexpected: {message or 'no message'}"


def refusal_error(
    status: int,
    headers: Mapping[str, str],
    message: str,
    *,
    source: str | None,
    operation: Operation | None,
    secrets: Sequence[str] = (),
) -> TrackerError:
    """The port error for a non-retryable refusal, carrying its diagnostic as the message.

    Rate limits and server errors are not decided here (see :mod:`.retry`)."""
    line = diagnose(status, headers, message, source=source, secrets=secrets)
    lowered = message.lower()
    if status == 401:
        return AuthenticationFailed(line)
    if status == 403:
        if _header(headers, "x-github-sso") is None and (
            "archived" in lowered or "locked" in lowered
        ):
            return TargetStateRefused(line)
        needed = _header(headers, "x-accepted-github-permissions")
        return PermissionDenied(
            line, operation=operation, needed=clean(needed, secrets) if needed else None
        )
    if status == 404:
        return NotVisible(line)
    if status == 410:
        return NotVisible(line) if "deleted" in lowered else TargetStateRefused(line)
    if status == 422:
        return PayloadRejected(line)
    return TrackerError(line)
