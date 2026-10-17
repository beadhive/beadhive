"""sync_policy.py — which bead-state transports a hive's contribution policy allows (bh-0azjq).

`bh hive sync` is a bulk verb: one ``--all`` run walks every registered hive and, for an ordinary
internal hive, pulls then pushes its Dolt bead state and publishes its unpushed Git branches. An
EXTERNAL contribution target is different. Its bead store is a read-only federation of somebody
else's upstream, and its Git ``origin`` is our writable contribution fork, whose branches are
published only through the explicit, gated contribution flow — never as a side effect of a fleet
sweep. This module derives that boundary from authoritative configuration so sync never has to
rely on a Git ``upstream`` pushurl (``disabled://…``) happening to protect Dolt.

A hive is **pull-only** when any of these hold (each is reported as a reason):

- the registry entry declares ``contribution: pull`` (bh-uxam.1);
- the registry entry is ``kind: external`` with no ``contribution`` override (external hives are
  pull-only by default — the Contribution plane's read rail);
- the hive's effective Beads config (``.beads/config.yaml``) sets ``no-push: true``.

The refusal is a CORRECTNESS rule, not an opt-in: no flag (``--push``, ``--remote``, ``--force``)
re-enables a push for a pull-only hive. Ordinary internal hives are unaffected.

For the pull leg, :func:`pull_route` resolves the declared upstream bead source among the hive's
configured Dolt remotes (which may be Git-backed ``git+…`` remotes riding ``refs/dolt/data``),
distinguishing it from the Git ``origin`` fork, and diagnoses a ``sync.remote`` that disagrees
with it. Every function here is pure apart from reading the hive's own config files; the bd
subprocess that lists Dolt remotes stays in ``sync_remote`` (an allowlisted bd-admin module).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from . import bd

PULL_ONLY = "pull-only"
READ_WRITE = "read-write"

# The conventional name a contributor's checkout gives the canonical upstream — the Git remote
# AND, for a Git-backed Dolt store, the Dolt remote that reads ``refs/dolt/data`` from it.
UPSTREAM_REMOTE = "upstream"


@dataclass(frozen=True)
class SyncPolicy:
    """One hive's effective bead-sync policy. ``pull_only`` forbids every push leg."""

    pull_only: bool = False
    reasons: tuple[str, ...] = ()
    upstream: str = ""  # declared upstream ``owner/repo`` from the registry, "" when undeclared

    @property
    def label(self) -> str:
        return PULL_ONLY if self.pull_only else READ_WRITE

    def describe(self) -> str:
        return f"{self.label} ({'; '.join(self.reasons)})" if self.reasons else self.label


@dataclass(frozen=True)
class PullRoute:
    """Where a pull-only hive's pull leg reads from, plus any routing diagnostics.

    ``remote`` is the Dolt remote NAME to pass as ``bd dolt pull --remote``; "" means bd's own
    default. ``refused`` is set when a declared upstream exists but no Dolt remote reaches it —
    pulling the default (usually the writable fork) would silently read the wrong source.
    """

    remote: str = ""
    url: str = ""
    notes: tuple[str, ...] = ()
    refused: str = ""


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


def _beads_no_push(clone_path: Path) -> bool:
    return _truthy(bd.config_yaml(clone_path).get("no-push"))


def resolve(entry, clone_path: Path) -> SyncPolicy:
    """The effective policy for one registered hive (see the module docstring)."""
    entry = entry or {}
    kind = str(entry.get("kind", "") or "")
    contribution = str(entry.get("contribution", "") or "")
    reasons: list[str] = []
    if contribution == "pull":
        reasons.append("registry contribution=pull")
    elif kind == "external" and not contribution:
        reasons.append("registry kind=external (contribution defaults to pull)")
    if clone_path.exists() and _beads_no_push(clone_path):
        reasons.append(".beads/config.yaml no-push=true")
    return SyncPolicy(
        pull_only=bool(reasons),
        reasons=tuple(reasons),
        upstream=str(entry.get("upstream", "") or ""),
    )


def normalize_url(url: str) -> str:
    """A comparable identity for a Git/Dolt remote URL: ``host/owner/repo`` for network URLs
    (scheme, ``git+`` prefix, credentials, ``.git`` suffix and case dropped), the absolute path
    for ``file://`` / bare paths. ``git@host:owner/repo`` (scp form) normalizes like https."""
    raw = (url or "").strip()
    if not raw:
        return ""
    if raw.startswith("git+"):
        raw = raw[len("git+") :]
    if "://" not in raw and ":" in raw and not raw.startswith("/"):
        user_host, _, path = raw.partition(":")
        raw = f"ssh://{user_host}/{path}"
    if "://" not in raw:
        path = raw
        host = ""
    else:
        parts = urlsplit(raw)
        host = "" if parts.scheme == "file" else (parts.hostname or "")
        path = parts.path
    path = path.rstrip("/")
    if path.endswith(".git"):
        path = path[: -len(".git")]
    if host:
        return f"{host.lower()}/{path.strip('/').lower()}"
    return path


def _names_upstream(url: str, upstream: str) -> bool:
    """True when ``url`` points at the declared ``owner/repo`` upstream."""
    if not upstream:
        return False
    return normalize_url(url).lower().endswith("/" + upstream.strip("/").lower())


def pull_route(
    policy: SyncPolicy,
    *,
    dolt_remotes: list[dict],
    git_upstream_url: str = "",
    git_origin_url: str = "",
    sync_remote_url: str = "",
    explicit_remote: str = "",
) -> PullRoute:
    """Resolve the pull source for a pull-only hive.

    Precedence: an explicit ``--remote`` (a pull is a read, so any named remote is allowed);
    else the Dolt remote whose URL matches the declared upstream (registry ``upstream`` or the
    Git ``upstream`` remote's fetch URL); else a Dolt remote literally named ``upstream``. When
    an upstream is declared but no Dolt remote reaches it the pull is refused with a repair
    hint. With no declared upstream at all (e.g. an internal hive that is pull-only only via
    ``no-push``) bd's default remote is used unchanged.
    """
    remotes = [r for r in dolt_remotes if isinstance(r, dict) and r.get("name")]
    declared = git_upstream_url or policy.upstream
    if explicit_remote:
        match = next((r for r in remotes if r["name"] == explicit_remote), None)
        route = PullRoute(remote=explicit_remote, url=str((match or {}).get("url", "")))
    else:
        match = None
        git_up = normalize_url(git_upstream_url)
        for r in remotes:
            url = str(r.get("url", ""))
            if (git_up and normalize_url(url) == git_up) or _names_upstream(url, policy.upstream):
                match = r
                break
        if match is None:
            match = next((r for r in remotes if r["name"] == UPSTREAM_REMOTE), None)
        if match is None:
            if not declared:
                return PullRoute()
            want = git_upstream_url or f"https://github.com/{policy.upstream}.git"
            target = want if want.startswith("git+") else f"git+{want}"
            return PullRoute(
                refused=(
                    f"no Dolt remote reaches the declared upstream bead source ({declared}); "
                    f"repair without publishing: bd dolt remote add {UPSTREAM_REMOTE} {target}"
                )
            )
        route = PullRoute(remote=str(match["name"]), url=str(match.get("url", "")))

    notes: list[str] = []
    origin = normalize_url(git_origin_url)
    if route.url:
        source = f"bead source: Dolt remote '{route.remote}' ({route.url})"
        if origin and normalize_url(route.url) != origin:
            source += f"; Git origin {git_origin_url} is the code-only contribution fork"
        notes.append(source)
    if sync_remote_url and route.url and normalize_url(sync_remote_url) != normalize_url(route.url):
        where = (
            "the writable Git origin fork"
            if origin and normalize_url(sync_remote_url) == origin
            else "not the declared upstream"
        )
        notes.append(
            f"sync.remote {sync_remote_url} is {where}, disagreeing with the bead source "
            f"{route.url}; repair without publishing: bd config set sync.remote {route.url}"
        )
    return PullRoute(remote=route.remote, url=route.url, notes=tuple(notes))
