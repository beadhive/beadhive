"""Read-only capability evidence: the ``bh-mqvdq`` tri-state probe as a pure function.

**Write permission cannot be proven with a read.** No ``GET`` reveals a fine-grained token's
``Issues: write`` grant, an organization policy, SSO state for one org, an interaction limit or a
locked thread. So each operation gets a :class:`~beadhive.modules.tracker.contracts.Tri` from one
``GET /user`` and one ``GET /repos/{owner}/{name}`` (REST, deliberately not GraphQL, whose
``viewerPermission`` is a ``POST``; "the probe sends only GET" is a transport-level test):

* ``NO`` is definitive: the credential was rejected (``401``) or the repository is invisible
  (``404``); it is archived or disabled; issues are off; no credential was sent (no
  ``permissions`` object); a classic or OAuth token's scopes lack ``repo`` (or ``public_repo`` on
  a public repository); for ``labels.change``, a role below triage.
* ``YES`` means only "not refuted": a classic or OAuth token with a sufficient scope and role.
* ``UNKNOWN``: no ``X-OAuth-Scopes`` header (a fine-grained or App installation token, whose
  per-repository grant no read can show) or an answer the probe could not interpret. The first
  approved write is then the probe, and its refusal is classified, never retried.

The response headers decide the credential kind (``X-OAuth-Scopes`` present or not), never the
token prefix.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from beadhive.modules.tracker.contracts import Operation, Tri

__all__ = ["RepositoryFacts", "probe_operation", "scopes_of"]


@dataclass(frozen=True)
class RepositoryFacts:
    """What one ``GET /repos/{owner}/{name}`` showed (``refused`` when it was not readable)."""

    readable: bool
    reason: str = ""  # one scrubbed line when not readable
    definitive: bool = True  # a refusal that proves absence (401/404), versus could-not-tell
    private: bool = False
    archived: bool = False
    disabled: bool = False
    has_issues: bool = True
    permissions: Mapping[str, Any] | None = None
    scopes: frozenset[str] | None = None  # None: no X-OAuth-Scopes header (unscoped token)

    @classmethod
    def from_reply(cls, body: Any, headers: Mapping[str, str]) -> RepositoryFacts:
        row = body if isinstance(body, dict) else {}
        permissions = row.get("permissions")
        return cls(
            readable=True,
            private=bool(row.get("private")),
            archived=bool(row.get("archived")),
            disabled=bool(row.get("disabled")),
            has_issues=row.get("has_issues") is not False,
            permissions=permissions if isinstance(permissions, dict) else None,
            scopes=scopes_of(headers),
        )


def scopes_of(headers: Mapping[str, str]) -> frozenset[str] | None:
    """The classic or OAuth token's scopes; ``None`` when the header is absent (unscoped)."""
    raw = headers.get("x-oauth-scopes")
    if raw is None:
        return None
    return frozenset(scope.strip() for scope in raw.split(",") if scope.strip())


def probe_operation(op: Operation, facts: RepositoryFacts) -> tuple[Tri, str]:
    """``(tri, one-line reason)`` for ``op`` given the repository facts."""
    if not facts.readable:
        return (Tri.NO if facts.definitive else Tri.UNKNOWN), facts.reason
    if not facts.has_issues:
        return Tri.NO, "issues are disabled on this repository"
    if facts.archived or facts.disabled:
        if op in (Operation.ISSUES_READ, Operation.COMMENTS_READ):
            return Tri.YES, "repository readable (archived or disabled: read-only)"
        return Tri.NO, "repository is archived or disabled and read-only"
    if op in (Operation.ISSUES_READ, Operation.COMMENTS_READ):
        return Tri.YES, "repository readable"
    permissions = facts.permissions
    if permissions is None:
        return Tri.NO, "no credential was sent (the repository shows no permissions)"
    role_ok = bool(
        permissions.get("triage")
        or permissions.get("push")
        or permissions.get("maintain")
        or permissions.get("admin")
    )
    if op is Operation.LABELS_CHANGE and not role_ok:
        return Tri.NO, "role on the repository is below triage"
    if facts.scopes is None:
        return (
            Tri.UNKNOWN,
            "fine-grained or installation token: 'Issues: write' cannot be read; "
            "the first approved write is the probe",
        )
    if "repo" in facts.scopes or ("public_repo" in facts.scopes and not facts.private):
        return Tri.YES, "token scope and role allow it (not proven until the first write)"
    return Tri.NO, "token scopes lack repo (or public_repo for a public repository)"
