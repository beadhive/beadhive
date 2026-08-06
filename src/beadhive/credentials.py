"""Probe the CREDENTIALS bh's dependencies need, and name the exact command that fixes each gap.

``bh dep install`` places a binary; nothing authenticates it, and INSTALL.md says so in as many
words. On a laptop that is a shrug — you log in once in a browser. On a headless Linux node it is
the step where an unattended install silently produces a host that can clone nothing and run no
seat.

NAMED FOR WHAT IT PROBES, NOT FOR WHO OWNS THE CREDENTIAL (bh-hsus.6). This was ``harness_auth``,
and it probed ``gh`` — which is infrastructure, runs no seat, and is not a harness. The module was
named after one kind of member of the set it operated on, exactly like the ``bh harness auth``
verb, and that category error is why the bh-hsus epic exists. The probe LOGIC below is bh-q160.3's,
unchanged; only its name and its dispatch moved.

THERE IS NO REGISTRY HERE, DELIBERATELY. This module used to keep a ``PROBES`` dict keyed by name
with one hand-written probe function per key — an eighth registry of "external things bh depends
on", which is the thing bh-hsus exists to end. Auth-ness is a COLUMN on the dep row
(:class:`deps.Auth`), so the set of rows that need a credential DERIVES —
``deps.authenticated_deps()`` — and the per-row facts a probe needs live on the row. :func:`probe`
is ONE function over them. A named constant listing the same three rows would just be the registry
again under a nicer name.

WHAT THIS IS NOT: a login implementation. Every credential route belongs to somebody else's CLI
(``gh auth login``, ``claude setup-token``, ``codex login``), and this must not grow into a wrapper
around them. It PROBES, it REPORTS, and it names the exact command to run next.

CREDENTIAL VALUES NEVER LEAVE. A probe reports that a variable is SET, never what it contains, and
no report field is ever built from a secret's value — see :func:`_env_source`. That is the whole
reason the reports carry a ``how`` field rather than the credential: an operator debugging a
headless host needs to know a token arrived via the environment rather than a stored login, because
those two fail in completely different ways, and neither diagnosis needs the bytes. The macOS
Keychain query omits ``-w`` so ``security`` cannot print the secret even by accident.

The routes are not invented here — they mirror ``.env.example``'s "credentials" section, which is
where an operator meets them. If they diverge, that file is right and this is wrong.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from . import deps
from .run import run

#: Every probe that shells out is bounded. This verb targets HEADLESS hosts, where a hang is the
#: likeliest failure and is indistinguishable from a slow answer — ``gh auth status`` reaches the
#: network, and a probe that can block forever is worse than one that reports "unreachable".
PROBE_TIMEOUT = 15.0


@dataclass(frozen=True)
class AuthReport:
    """One row's answer to: is it here, is it authenticated, and how did the credential get in.

    ``how`` is deliberately a provenance label, never a value. ``detail`` carries the one extra
    fact worth surfacing per row (for gh, the protocol its credential covers — https-vs-ssh is
    what breaks HQ sync, bh-pc2a.30).
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


def _status_login(status: deps.StatusProbe) -> tuple[bool, str]:
    """*status.cmd* reduced to ``(logged_in, mined_detail)``.

    The command exits non-zero when no account is logged in, which is the signal — its text is
    only mined for ``status.detail_line``. Any failure to run it at all is reported as
    not-logged-in rather than raised: this is a probe, and a broken tool is a finding, not a crash.

    Timed out rather than trusted, per :data:`PROBE_TIMEOUT`.
    """
    try:
        proc = run(list(status.cmd), check=False, capture=True, timeout=PROBE_TIMEOUT)
    except (subprocess.TimeoutExpired, OSError):
        return False, ""
    if proc.returncode != 0:
        return False, ""
    if not status.detail_line:
        return True, ""
    text = f"{proc.stdout or ''}{proc.stderr or ''}"
    for line in text.splitlines():
        if status.detail_line in line:
            return True, line.split(":", 1)[1].strip()
    return True, ""


def _keychain_credential(service: str) -> bool:
    """True when macOS's Keychain holds an item for *service*.

    Queries only for EXISTENCE — ``find-generic-password`` without ``-w`` never prints the
    secret, so the value cannot reach stdout, a log, or an OTEL attribute even by accident.
    Non-Darwin returns False without shelling out, because `security` is a macOS binary and a
    missing-command error is not a finding worth reporting; a row with no Keychain service
    returns False the same way, without a platform check ever mattering.
    """
    if not service or sys.platform != "darwin":
        return False
    try:
        proc = run(
            ["security", "find-generic-password", "-s", service],
            check=False,
            capture=True,
            timeout=PROBE_TIMEOUT,
        )
    except (subprocess.TimeoutExpired, OSError):
        return False
    return proc.returncode == 0


def _stored_credential(stored: deps.StoredCredential) -> Path | None:
    """Path to a tool's own on-disk credential when it exists, else None.

    Presence means a login HAPPENED; it is not a validity check, and the reports say so. Probing
    validity would mean running the tool, which on a headless host is exactly the interactive
    prompt this verb exists to avoid.
    """
    base = os.environ.get(stored.dir_env, "").strip() or stored.default_dir
    candidate = Path(base).expanduser() / stored.filename
    return candidate if candidate.exists() else None


def _absent_remedy(dep: deps.Dep) -> str:
    """The remedy for a tool that is not installed — never a command that would refuse.

    A row's ``absent_remedy`` is prose, so it can name ``bh dep install <name>`` for a tool bh
    does NOT install. codex is exactly that: ``install.cmd is None`` (three plane-specific routes
    and no universal one), so ``bh dep install codex`` exits 1 and then prints three DIFFERENT
    routes — the operator is sent to a dead end that contradicts the thing that sent them.

    This is the same rule ``harness.missing_hint`` already applies, and applying it in only one
    of the two places is why bh-tccp was the FIFTH instance of one shape (docs/design/
    dependency-taxonomy-adr.md names the first four). bh-hsus.6 modernised remedy strings to name
    the canonical ``bh dep install`` verb without re-checking which rows can actually be
    installed, so the change that fixed the wording is what introduced this one. Deriving it from
    ``install.cmd`` means a new uninstallable row cannot reintroduce the shape by wording alone.
    """
    auth = dep.auth
    remedy = auth.absent_remedy if auth else ""
    if dep.install is not None and dep.install.cmd is None and dep.install.note:
        return dep.install.note
    return remedy


def probe(dep: deps.Dep) -> AuthReport:
    """Stage 2 for one row: is *dep* here, is it authenticated, and how did the credential arrive.

    Ordered the way the tools themselves RESOLVE a credential — environment first (a set token
    wins over any stored login, so reporting the stored one would describe a credential the tool
    is not using), then the platform credential store, then an on-disk credential, then a status
    command. Each row carries only the steps that apply to it, so this single order reproduces
    bh-q160.3's three hand-written probes exactly: gh is env → status, claude is env → Keychain →
    file, codex is env → file.
    """
    auth = dep.auth
    if auth is None:
        raise ValueError(f"{dep.name} has no credential gate — probe rows from authenticated_deps")

    path = shutil.which(dep.binary)
    if path is None:
        return AuthReport(
            name=dep.name,
            installed=False,
            authenticated=False,
            how="—",
            remedy=_absent_remedy(dep),
        )

    if var := _env_source(auth.env_vars):
        return AuthReport(
            name=dep.name,
            installed=True,
            authenticated=True,
            how=f"{var} (environment)",
            remedy="",
            path=path,
            detail=auth.env_detail,
        )

    if _keychain_credential(auth.keychain_service):
        return AuthReport(
            name=dep.name,
            installed=True,
            authenticated=True,
            how="stored login (macOS Keychain)",
            remedy="",
            path=path,
            detail="Keychain item present (presence, not validity)",
        )

    if auth.stored is not None and (stored := _stored_credential(auth.stored)):
        return AuthReport(
            name=dep.name,
            installed=True,
            authenticated=True,
            how="stored login",
            remedy="",
            path=path,
            detail=f"credential present at {stored} (presence, not validity)",
        )

    if auth.status is not None:
        logged_in, mined = _status_login(auth.status)
        if logged_in:
            label = auth.status.detail_label
            return AuthReport(
                name=dep.name,
                installed=True,
                authenticated=True,
                how=auth.status.how,
                remedy="",
                path=path,
                detail=f"{label}: {mined or 'unreported'}" if label else "",
            )

    return AuthReport(
        name=dep.name,
        installed=True,
        authenticated=False,
        how="—",
        remedy=auth.remedy,
        path=path,
    )


def probe_all() -> list[AuthReport]:
    """Every row with a stage-2 gate, in table order — DERIVED from the table, never listed."""
    return [probe(dep) for dep in deps.authenticated_deps()]


def unmet(reports: list[AuthReport], cfg: dict | None = None) -> list[str]:
    """The requirements a host must meet to be usable, as human-readable failures.

    THE SELECTOR MODEL — and a deliberate reversal of bh-q160.3's accepted behaviour (bh-hsus.6,
    operator-approved 2026-08-05). This used to OR over ``(claude, codex)``: "a seat needs one
    agent, not both", so a codex-only host passed. That rested on a premise bh-hsus.2's spike
    verified FALSE — codex 0.146.0 has no ``--agent`` equivalent and ``bh role --harness codex``
    is REJECTED, so a codex-only host was passing a gate it then failed at, which is strictly
    worse than failing the gate. bh-hsus.5 has since made codex ``required="never"`` and removed
    it from the ``agent`` group, so the table already says codex can never satisfy this
    requirement; this makes the gate agree. Net effect: a codex-only host now either fails
    ``--check`` or launches a seat, never both.

    The rule is now the table's own, with no pairwise special case left anywhere: every row
    REQUIRED under *cfg* that carries a credential must have it. The OR did not move, it
    disappeared — ``deps.required_deps()`` selects claude XOR opencode via ``config.harness_name``,
    and gh is required unconditionally because without it the host clones nothing, and a host that
    cannot clone cannot be onboarded at all.
    """
    by_name = {r.name: r for r in reports}
    failures: list[str] = []
    for dep in deps.required_deps(cfg):
        if dep.auth is None:
            continue
        report = by_name.get(dep.name)
        if report is not None and report.authenticated:
            continue
        state = "is not authenticated" if report is not None and report.installed else "is missing"
        # Name WHERE the choice was made, so an operator told "claude must be authenticated" can
        # see that it is a config selection rather than a law.
        chosen = f" (`{deps.GROUPS[dep.group].selector}` selects it)" if dep.group else ""
        failures.append(f"{dep.name} {state} — {dep.auth.consequence}{chosen}")
    return failures


def render(reports: list[AuthReport]) -> list[str]:
    """The report block, as lines. Separated from printing so tests assert on content, and so
    the secret-free contract is checkable in one place rather than at every echo site."""
    lines = ["Credentials:"]
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


def run_login(name: str) -> AuthReport:
    """Drive *name*'s login flow, then RE-PROBE and return the fresh report.

    Only a flow that works with NO BROWSER on the box is worth driving from here — gh's device
    flow, which prints a code you enter on any other machine. ``claude setup-token`` must run
    where a browser IS, and ``codex login`` is the same shape; running either on the headless host
    this verb targets would just hang on a prompt, so for those the honest action is the remedy
    text, and this returns their unchanged probe rather than pretending to have done something.
    Which flow is which is a fact about the ROW (``Auth.headless_login``), not a name checked here.

    The re-probe is the point: a login flow that reports its own success is a flow that lies
    when the credential did not actually land.
    """
    dep = deps.by_name(name)
    if dep.auth is not None and dep.auth.headless_login:
        run(list(dep.auth.login), check=False)
    return probe(dep)
