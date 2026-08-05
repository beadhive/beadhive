"""Probe and guide harness credentials on a headless host (bh-q160.3).

``bh harness install`` places a binary; nothing authenticates it, and INSTALL.md says so in as
many words. On a laptop that is a shrug — you log in once in a browser. On a headless Linux
node it is the step where an unattended install silently produces a host that can clone nothing
and run no seat.

WHAT THIS IS NOT: a login implementation. Every credential route below belongs to somebody
else's CLI (``gh auth login``, ``claude setup-token``, ``codex login``), and this must not grow
into a wrapper around them. It PROBES, it REPORTS, and it names the exact command to run next.

CREDENTIAL VALUES NEVER LEAVE. A probe reports that a variable is SET, never what it contains,
and no report field is ever built from a secret's value — see :func:`_env_source`. That is the
whole reason the reports carry a ``how`` field rather than the credential: an operator debugging
a headless host needs to know a token arrived via the environment rather than a stored login,
because those two fail in completely different ways, and neither diagnosis needs the bytes.

The routes below are not invented here — they mirror ``.env.example``'s "harness auth" section,
which is where an operator meets them. If they diverge, that file is right and this is wrong.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .run import run

#: gh reads these in order, and either takes precedence over any stored login (.env.example:84).
#: That precedence is why a headless factory host needs no `gh auth login` at all.
_GH_TOKEN_VARS: tuple[str, ...] = ("GH_TOKEN", "GITHUB_TOKEN")

#: `gh auth status` reaches the network. Bounded because this verb targets headless hosts,
#: where a hang is the likeliest failure and is indistinguishable from a slow answer.
GH_PROBE_TIMEOUT = 15.0

#: Claude's headless routes, preferred first. CLAUDE_CODE_OAUTH_TOKEN is the account's own
#: credential — revocable, no API-billing path — and ANTHROPIC_API_KEY is the billing fallback.
_CLAUDE_TOKEN_VARS: tuple[str, ...] = ("CLAUDE_CODE_OAUTH_TOKEN", "ANTHROPIC_API_KEY")

_CODEX_TOKEN_VARS: tuple[str, ...] = ("OPENAI_API_KEY",)


@dataclass(frozen=True)
class AuthReport:
    """One target's answer to: is it here, is it authenticated, and how did the credential get in.

    ``how`` is deliberately a provenance label, never a value. ``detail`` carries the one extra
    fact worth surfacing per target (for gh, the protocol its credential covers — https-vs-ssh
    is what breaks HQ sync, bh-pc2a.30).
    """

    name: str
    installed: bool
    authenticated: bool
    how: str
    remedy: str
    path: str | None = None
    detail: str = ""

    @property
    def status_glyph(self) -> str:
        if not self.installed:
            return "✗"
        return "✓" if self.authenticated else "✗"


def _env_source(names: tuple[str, ...]) -> str | None:
    """The first of *names* that is set and non-empty — the NAME, never the value.

    Returning the variable name is the entire contract: it is what makes the report useful
    ("the token came from the environment, so a stored login is irrelevant") while keeping the
    secret out of stdout, logs and OTEL attributes alike.
    """
    for var in names:
        if os.environ.get(var, "").strip():
            return var
    return None


def _gh_auth_status() -> tuple[bool, str]:
    """``gh auth status`` reduced to (logged_in, protocol).

    gh exits non-zero when no account is logged in, which is the signal — the text is only
    mined for the protocol line. Any failure to run gh at all is reported as not-logged-in
    rather than raised: this is a probe, and a broken gh is a finding, not a crash.

    Timed out rather than trusted: ``gh auth status`` reaches the network, and this verb exists
    for headless hosts where the likeliest failure mode is a hang, not an error. A probe that
    can block forever is worse than one that reports "unreachable".
    """
    try:
        proc = run(["gh", "auth", "status"], check=False, capture=True, timeout=GH_PROBE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return False, ""
    text = f"{proc.stdout or ''}{proc.stderr or ''}"
    if proc.returncode != 0:
        return False, ""
    protocol = ""
    for line in text.splitlines():
        if "Git operations protocol:" in line:
            protocol = line.split(":", 1)[1].strip()
            break
    return True, protocol


def probe_gh() -> AuthReport:
    path = shutil.which("gh")
    if path is None:
        return AuthReport(
            name="gh",
            installed=False,
            authenticated=False,
            how="—",
            remedy="gh is not on PATH. Install it, or on a local-install host let the flake "
            "supply it (`nix develop`); on a dev machine `mise install` provides the pin.",
        )

    # Environment first, because gh itself resolves it first: a set token WINS over any stored
    # login, so reporting the stored one would describe a credential gh is not using.
    if var := _env_source(_GH_TOKEN_VARS):
        return AuthReport(
            name="gh",
            installed=True,
            authenticated=True,
            how=f"{var} (environment)",
            remedy="",
            path=path,
            detail="token auth covers https; ssh remotes are NOT covered by it (bh-pc2a.30)",
        )

    logged_in, protocol = _gh_auth_status()
    if logged_in:
        return AuthReport(
            name="gh",
            installed=True,
            authenticated=True,
            how="stored login (`gh auth login`)",
            remedy="",
            path=path,
            detail=f"git protocol: {protocol}" if protocol else "git protocol: unreported",
        )
    return AuthReport(
        name="gh",
        installed=True,
        authenticated=False,
        how="—",
        remedy="`gh auth login --web` — the DEVICE FLOW, the one route that works with no "
        "browser on this box: it prints a code you enter on any other machine. Or set GH_TOKEN "
        "in the environment, which takes precedence over any stored login.",
        path=path,
    )


def _stored_credential(dir_env: str, default: str, filename: str) -> Path | None:
    """Path to a harness's on-disk credential when it exists, else None.

    Presence means a login HAPPENED; it is not a validity check, and the reports say so. Probing
    validity would mean running the harness, which on a headless host is exactly the interactive
    prompt this verb exists to avoid.
    """
    base = os.environ.get(dir_env, "").strip() or default
    candidate = Path(base).expanduser() / filename
    return candidate if candidate.exists() else None


#: macOS Claude Code keeps its credential in the KEYCHAIN, not on disk, under this service name.
#: Found the hard way: the first cut of this module probed only for a file and reported a
#: working, logged-in install as "NOT authenticated" — the precise false negative this verb
#: exists to prevent, complete with a remedy that addressed nothing.
_CLAUDE_KEYCHAIN_SERVICE = "Claude Code-credentials"


def _claude_keychain_credential() -> bool:
    """True when macOS's Keychain holds a Claude Code credential.

    Queries only for EXISTENCE — ``find-generic-password`` without ``-w`` never prints the
    secret, so the value cannot reach stdout, a log, or an OTEL attribute even by accident.
    Non-Darwin returns False without shelling out, because `security` is a macOS binary and a
    missing-command error is not a finding worth reporting.
    """
    if sys.platform != "darwin":
        return False
    try:
        proc = run(
            ["security", "find-generic-password", "-s", _CLAUDE_KEYCHAIN_SERVICE],
            check=False,
            capture=True,
            timeout=GH_PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def probe_claude() -> AuthReport:
    path = shutil.which("claude")
    if path is None:
        return AuthReport(
            name="claude",
            installed=False,
            authenticated=False,
            how="—",
            remedy="`bh harness install claude` (proprietary — you accept Anthropic's terms).",
        )
    if var := _env_source(_CLAUDE_TOKEN_VARS):
        return AuthReport(
            name="claude",
            installed=True,
            authenticated=True,
            how=f"{var} (environment)",
            remedy="",
            path=path,
        )
    if _claude_keychain_credential():
        return AuthReport(
            name="claude",
            installed=True,
            authenticated=True,
            how="stored login (macOS Keychain)",
            remedy="",
            path=path,
            detail="Keychain item present (presence, not validity)",
        )
    if stored := _stored_credential("CLAUDE_CONFIG_DIR", "~/.claude", ".credentials.json"):
        return AuthReport(
            name="claude",
            installed=True,
            authenticated=True,
            how="stored login",
            remedy="",
            path=path,
            detail=f"credential present at {stored} (presence, not validity)",
        )
    return AuthReport(
        name="claude",
        installed=True,
        authenticated=False,
        how="—",
        remedy="Run `claude setup-token` on ANY machine that HAS a browser, then set "
        "CLAUDE_CODE_OAUTH_TOKEN here — it is the account's own credential and is revocable. "
        "ANTHROPIC_API_KEY is the fallback where API billing is the intended route.",
        path=path,
    )


def probe_codex() -> AuthReport:
    path = shutil.which("codex")
    if path is None:
        return AuthReport(
            name="codex",
            installed=False,
            authenticated=False,
            how="—",
            remedy="`bh harness install codex` (Apache-2.0).",
        )
    if var := _env_source(_CODEX_TOKEN_VARS):
        return AuthReport(
            name="codex",
            installed=True,
            authenticated=True,
            how=f"{var} (environment)",
            remedy="",
            path=path,
        )
    if stored := _stored_credential("CODEX_HOME", "~/.codex", "auth.json"):
        return AuthReport(
            name="codex",
            installed=True,
            authenticated=True,
            how="stored login",
            remedy="",
            path=path,
            detail=f"credential present at {stored} (presence, not validity)",
        )
    return AuthReport(
        name="codex",
        installed=True,
        authenticated=False,
        how="—",
        remedy="`codex login`, or set OPENAI_API_KEY where an API key is the billing route.",
        path=path,
    )


PROBES = {"gh": probe_gh, "claude": probe_claude, "codex": probe_codex}


def probe_all() -> list[AuthReport]:
    return [PROBES[name]() for name in ("gh", "claude", "codex")]


def unmet(reports: list[AuthReport]) -> list[str]:
    """The requirements a host must meet to be usable, as human-readable failures.

    gh is REQUIRED unconditionally: without it the host clones nothing, and a host that cannot
    clone cannot be onboarded at all. The harnesses are required as a PAIR-WISE OR — a seat
    needs one agent, not both, and forcing both would make a codex-only host fail a check it
    actually passes.
    """
    by_name = {r.name: r for r in reports}
    failures: list[str] = []
    if not by_name["gh"].authenticated:
        failures.append("gh is not authenticated — the host cannot clone or onboard")
    if not any(by_name[n].authenticated for n in ("claude", "codex")):
        failures.append("neither claude nor codex is authenticated — no seat can run")
    return failures


def render(reports: list[AuthReport]) -> list[str]:
    """The report block, as lines. Separated from printing so tests assert on content, and so
    the secret-free contract is checkable in one place rather than at every echo site."""
    lines = ["Harness credentials:"]
    for r in reports:
        if not r.installed:
            state = "not installed"
        else:
            state = "authenticated" if r.authenticated else "NOT authenticated"
        lines.append(f"  {r.status_glyph} {r.name:<7} {state}")
        if r.installed and r.authenticated:
            lines.append(f"      how: {r.how}")
        if r.detail:
            lines.append(f"      {r.detail}")
        if r.remedy:
            lines.append(f"      → {r.remedy}")
    return lines
