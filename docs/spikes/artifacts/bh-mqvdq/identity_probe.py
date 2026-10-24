"""Spike bh-mqvdq prototype: credential resolution, actor discovery, write-capability probing and
own-comment recognition for a GitHub adapter. RESEARCH ARTIFACT: never imported by src/.

Only HTTP GET is ever issued. Dependencies: httpx (already a root dependency; the throwaway venv
installs it). Every function takes its collaborators as arguments (token source, httpx client) so
the whole file is testable offline with httpx.MockTransport.
"""

from __future__ import annotations

import enum
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

import httpx

# --------------------------------------------------------------------------------------------
# 1. Credentials: resolution order, provenance, scrubbing
# --------------------------------------------------------------------------------------------

#: bh's existing order (run._fill_github_token): GITHUB_TOKEN, then GH_TOKEN, then `gh auth token`.
#: NOTE gh itself reads GH_TOKEN first (observed, spike Evidence 3): an adapter that shells out to
#: gh must pass the one token it resolved as GH_TOKEN, or two identities silently diverge.
ENV_ORDER = ("GITHUB_TOKEN", "GH_TOKEN")

#: Documented GitHub token prefixes. A HEURISTIC to pick a probe strategy, never a security
#: decision: the response headers (below) are the evidence that overrides it.
_PREFIXES = (
    ("github_pat_", "fine-grained-pat"),
    ("ghp_", "classic-pat"),
    ("gho_", "oauth"),  # `gh auth login`, OAuth apps
    ("ghu_", "app-user"),  # GitHub App user-to-server
    ("ghs_", "app-installation"),
    ("ghr_", "refresh"),
)
#: Anything shaped like a GitHub token, for the pattern scrub (defence in depth: it catches a token
#: bh did NOT resolve itself, e.g. one echoed back in an error body).
TOKEN_PATTERN = re.compile(r"\b(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})\b")
REDACTED = "***"


@dataclass(frozen=True)
class ResolvedToken:
    """A token plus WHERE it came from. ``value`` is excluded from repr/str so an accidental
    f-string, log record or traceback of the object cannot leak it; ``source`` is safe to print."""

    value: str = field(repr=False)
    source: str  # "env:GITHUB_TOKEN" | "env:GH_TOKEN" | "gh-auth-token" | "injected"

    def __str__(self) -> str:
        return f"<token from {self.source}>"


#: The injected port. Richer than github_projection.TokenSource (which returns a bare str and so
#: loses provenance) but the same shape: a callable the composition root supplies.
TokenSource = Callable[[Mapping[str, str]], "ResolvedToken | None"]


def resolve_token(
    environ: Mapping[str, str], gh_auth_token: Callable[[], str | None] = lambda: None
) -> ResolvedToken | None:
    """Pure resolution: env vars in ENV_ORDER (blank == unset), then the injected `gh auth token`
    callable. Returns None when nothing resolves; never raises with a value in the message."""
    for var in ENV_ORDER:
        value = (environ.get(var) or "").strip()
        if value:
            return ResolvedToken(value, f"env:{var}")
    derived = (gh_auth_token() or "").strip()
    return ResolvedToken(derived, "gh-auth-token") if derived else None


def scrub(text: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    """The adapter's ONE rendering of untrusted text for output/logs/errors: exact-value replace
    (github_projection._scrub's rule) PLUS the token-shape pattern. Longest secret first so a
    token that is a prefix of another cannot leave a tail."""
    for secret in sorted((s for s in secrets if s), key=len, reverse=True):
        text = text.replace(secret, REDACTED)
    return TOKEN_PATTERN.sub(REDACTED, text)


def classify_by_prefix(token: str) -> str:
    for prefix, kind in _PREFIXES:
        if token.startswith(prefix):
            return kind
    return "classic-pat" if re.fullmatch(r"[0-9a-f]{40}", token) else "unknown"


def kind_from_headers(headers: httpx.Headers) -> str:
    """Header evidence beats the prefix. ``X-OAuth-Scopes`` is present (possibly empty) on classic
    PATs and OAuth tokens and ABSENT on fine-grained PATs and App installation tokens."""
    return "scoped" if "x-oauth-scopes" in headers else "unscoped"


# --------------------------------------------------------------------------------------------
# 2. Actor discovery
# --------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Actor:
    """Compared by (id, type) only. ``login`` is carried for display and is mutable (renames)."""

    id: int
    type: str  # "User" | "Bot" | "Organization"
    login: str = field(default="", compare=False)

    @property
    def key(self) -> tuple[int, str]:
        return (self.id, self.type)


class ProbeUnavailable(RuntimeError):
    """The probe could not complete; ``reason`` is a state, never a credential value."""


def discover_actor(client: httpx.Client) -> Actor | None:
    """GET /user. None for a token that cannot call /user (App installation token: 403 'Resource
    not accessible by integration'). 401 raises ProbeUnavailable('bad credentials')."""
    res = client.get("/user")
    if res.status_code == 401:
        raise ProbeUnavailable("bad credentials (401): token invalid, revoked or expired")
    if res.status_code == 403 and "integration" in res.text:
        return None
    res.raise_for_status()
    body = res.json()
    return Actor(id=int(body["id"]), type=str(body["type"]), login=str(body.get("login", "")))


def app_bot_actor(client: httpx.Client, slug: str) -> Actor:
    """The actor an installation token will comment as: the PUBLIC user ``<slug>[bot]``."""
    body = client.get(f"/users/{slug}%5Bbot%5D").raise_for_status().json()
    return Actor(id=int(body["id"]), type=str(body["type"]), login=str(body["login"]))


# --------------------------------------------------------------------------------------------
# 3. Write-capability probe (read-only; tri-state)
# --------------------------------------------------------------------------------------------


class Tri(enum.Enum):
    YES = "yes"  # every OBSERVABLE necessary condition holds. NOT a proof (see spike).
    NO = "no"  # a necessary condition is provably absent. Definitive.
    UNKNOWN = "unknown"  # a necessary condition cannot be observed with a GET.


@dataclass(frozen=True)
class Capability:
    state: Tri
    reason: str  # one line, safe to print (no token, no body)
    needs: str = ""  # what the operator would change / what the first write will tell us


def _scopes(headers: httpx.Headers) -> set[str]:
    return {s.strip() for s in headers.get("x-oauth-scopes", "").split(",") if s.strip()}


def probe_capability(client: httpx.Client, owner: str, repo: str, operation: str) -> Capability:
    """Decide, with ONE GET of the repository, whether ``operation`` ('comment' | 'label') is
    likely possible. ``comment`` is allowed to any authenticated user on a public repo with issues
    enabled; ``label`` (labels, assignees, edit) needs triage or better."""
    res = client.get(f"/repos/{owner}/{repo}")
    if res.status_code == 401:
        return Capability(Tri.NO, "credential rejected (401)", "fix or replace the token")
    if res.status_code == 404:
        return Capability(
            Tri.NO,
            "repository not visible to this credential (404 also hides private repos)",
            "grant the token access to the repository",
        )
    if res.status_code != 200:
        return Capability(Tri.UNKNOWN, f"repository read returned {res.status_code}")
    body = res.json()
    if body.get("archived") or body.get("disabled"):
        return Capability(Tri.NO, "repository is archived/disabled and read-only")
    if body.get("has_issues") is False:
        return Capability(Tri.NO, "issues are disabled on this repository")
    perms = body.get("permissions")
    if perms is None:
        return Capability(Tri.NO, "no credential was sent (no `permissions` object in response)")
    role_ok = bool(perms.get("push") or perms.get("triage") or perms.get("admin"))
    if operation == "label" and not role_ok:
        return Capability(Tri.NO, "role on the repository is below triage", "ask for triage access")

    scoped = kind_from_headers(res.headers) == "scoped"
    if scoped:  # classic PAT / OAuth (incl. `gh auth login`)
        scopes = _scopes(res.headers)
        public = not body.get("private", False)
        if "repo" in scopes or (public and "public_repo" in scopes):
            return Capability(Tri.YES, f"classic/OAuth scope sufficient ({sorted(scopes)})")
        return Capability(
            Tri.NO,
            "token scopes lack `repo` (or `public_repo` for a public repository)",
            "re-issue with repo / public_repo",
        )
    # Unscoped: fine-grained PAT or App installation token. `permissions` here is the identity's
    # role, NOT the token's per-repository grant, and 'Issues: write' is not readable by any GET.
    return Capability(
        Tri.UNKNOWN,
        "fine-grained/installation token: 'Issues: write' cannot be read; role looks sufficient"
        if role_ok or operation == "comment"
        else "fine-grained/installation token: grant not readable",
        "the first write is the probe; expect 403 'Resource not accessible by ...' if not granted",
    )


# --------------------------------------------------------------------------------------------
# 4. The diagnostic the FIRST refused write produces (pure: response in, text out)
# --------------------------------------------------------------------------------------------


def diagnose_refused_write(res: httpx.Response, source: str = "unknown") -> str:
    """Map a refused write to one actionable, scrubbed line. The adapter calls this only on a
    non-2xx write response; it never retries a refusal. ``source`` is the token PROVENANCE."""
    status = res.status_code
    try:
        message = str(res.json().get("message", ""))
    except ValueError:
        message = ""
    message = scrub(message)[:160]
    accepted = res.headers.get("x-accepted-github-permissions", "")
    if status == 401:
        return f"401 bad credentials: the token from {source} is invalid, revoked or expired"
    if status == 403:
        if "x-github-sso" in res.headers:
            return "403 SSO: authorize this token for the organization (SAML SSO) and retry"
        if res.headers.get("retry-after") or res.headers.get("x-ratelimit-remaining") == "0":
            return "403 rate limited (not a permission refusal): back off; do NOT mark writes off"
        if "archived" in message.lower():
            return "403 repository is archived (read-only)"
        if "locked" in message.lower():
            return "403 conversation is locked (not a credential problem)"
        if "Resource not accessible" in message:
            need = f" (endpoint needs {accepted})" if accepted else ""
            return f"403 token lacks the permission{need}: {message}"
        return f"403 refused: {message or 'no message'}"
    if status == 404:
        return (
            "404 on write: the repository/issue is invisible to this token (GitHub reports "
            "'forbidden' as 'not found'); check token repo access and scope"
        )
    if status == 410:
        return "410 issues are disabled on this repository"
    if status == 422:
        return f"422 payload rejected after authorization ({message}); NOT a capability failure"
    return f"{status} unexpected on write: {message}"


# --------------------------------------------------------------------------------------------
# 5. Own-comment recognition
# --------------------------------------------------------------------------------------------

MARKER_RE = re.compile(
    r"^<!-- bh:v1 hive=(?P<hive>[\w./-]+) install=(?P<install>[0-9a-f-]{8,36}) -->$"
)


def marker(hive: str, install: str) -> str:
    return f"<!-- bh:v1 hive={hive} install={install} -->"


def stamp(body: str, hive: str, install: str) -> str:
    """Append the footer marker as the LAST line of every comment the adapter posts."""
    return f"{body.rstrip()}\n\n{marker(hive, install)}"


def _footer_marker(body: str) -> re.Match[str] | None:
    lines = [line for line in body.splitlines() if line.strip()]
    return MARKER_RE.match(lines[-1].strip()) if lines else None


@dataclass(frozen=True)
class Provenance:
    own: bool
    basis: str  # which rule fired; recorded for audit


def classify_comment(
    comment: Mapping,
    *,
    actors: frozenset[tuple[int, str]],
    hive: str,
    outbox_ids: frozenset[int] = frozenset(),
    app_id: int | None = None,
) -> Provenance:
    """Order of evidence:
    1. comment id in the local outbox   -> own (recorded at post time; strongest, but per-install)
    2. performed_via_github_app.id == app_id AND actor is a Bot we know -> own (a human cannot post
       as the bot; sufficient for a dedicated App)
    3. actor (id, type) in the known-actor set AND footer marker names this hive -> own
    Otherwise NOT own. Rule 3's actor test is NECESSARY but not sufficient: a shared token used
    by a human has the same (id, type) and no marker. The marker alone is never trusted: it must
    be the last non-empty line and the author must be a known actor."""
    user = comment.get("user") or {}
    key = (int(user.get("id", -1)), str(user.get("type", "")))
    if int(comment.get("id", -1)) in outbox_ids:
        return Provenance(True, "outbox")
    via = comment.get("performed_via_github_app") or {}
    if app_id is not None and via.get("id") == app_id and key in actors and key[1] == "Bot":
        return Provenance(True, "app")
    if key not in actors:
        return Provenance(False, "other-actor")
    found = _footer_marker(str(comment.get("body") or ""))
    if found and found["hive"] == hive:
        return Provenance(True, "marker")
    return Provenance(False, "same-actor-no-marker")
