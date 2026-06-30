"""`ws rig ready` — read-only AGF readiness check for the current rig.

Verifies core AGF setup (required) plus optional integrations, prints a yes/no verdict
(exit 0 ready / 1 not), and with ``-v`` a per-line-item breakdown. Read-only: no writes,
no bd/git lifecycle. Live observaloop/grafana probes run ONLY when the integration is
enabled (otherwise the line is N/A, never probed).
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import typer

from . import config, observaloop, registry, rig
from .identity import workspace_identity
from .run import run

# Same marker rig._ensure_agf_hint writes into AGENTS.md / CLAUDE.md.
AGF_MARKER = "<!-- ws:agf:start"

# state → glyph: ok=present/up, missing=required gap (fails), off=optional not set up,
# na=integration disabled so not probed.
_GLYPH = {"ok": "✓", "missing": "✗", "off": "•", "na": "-"}


class Check(NamedTuple):
    label: str
    required: bool
    state: str  # ok | missing | off | na
    detail: str = ""


def _repo_root(cwd=None) -> Path:
    res = run(["git", "rev-parse", "--show-toplevel"], check=False, capture=True, cwd=cwd)
    return Path(res.stdout.strip()) if res.returncode == 0 else Path.cwd()


def _has_bundled_skill() -> bool:
    """skills/ exists and holds at least one of the bundled role skills."""
    dst = Path("skills")
    if not dst.is_dir():
        return False
    names = {p.name for p in config.skills_src().iterdir() if p.is_dir()}
    return any((dst / n).is_dir() for n in names)


def _required(label: str, ok: bool, ok_detail: str, miss_detail: str) -> Check:
    return Check(label, True, "ok" if ok else "missing", ok_detail if ok else miss_detail)


def _observaloop_checks(cfg, entry) -> list[Check]:
    """observaloop profile + grafana dashboard — live-probed only when enabled."""
    if not config.observaloop_enabled(cfg, entry):
        na = "disabled (otel/observaloop off)"
        return [
            Check("observaloop profile", False, "na", na),
            Check("grafana dashboard", False, "na", na),
        ]
    profile = config.observaloop_profile_name(cfg, entry or {})
    if not observaloop.is_available(cfg):
        gap = "observaloop unavailable — install plugin or set observaloop.command"
        return [
            Check("observaloop profile", False, "off", gap),
            Check("grafana dashboard", False, "off", gap),
        ]
    proto = config.otel_protocol(cfg)
    endpoint = observaloop.endpoint_for(profile, proto, cfg) if profile else None
    prof = Check(
        "observaloop profile",
        False,
        "ok" if endpoint else "off",
        f"profile '{profile}' {'up' if endpoint else 'down — `ws rig init --observaloop`'}",
    )
    vis = observaloop.visualizer_status(cfg)
    reachable = isinstance(vis, dict) and vis.get("reachable")
    graf = Check(
        "grafana dashboard",
        False,
        "ok" if reachable else "off",
        "visualizer reachable" if reachable else "visualizer not reachable",
    )
    return [prof, graf]


def _grant_check(cfg, root: Path, provider: str, org: str, repo: str) -> Check:
    cur = rig.grant_is_current(cfg, root, provider, org, repo)
    if cur is None:
        return Check("sandbox grant", False, "off", "no grant — `ws rig init --claude`")
    if cur:
        return Check("sandbox grant", False, "ok", "current")
    return Check("sandbox grant", False, "off", "stale (rig moved) — `ws rig init --claude -f`")


def _hint_check(label: str, path: Path) -> Check:
    ok = path.exists() and AGF_MARKER in path.read_text(errors="ignore")
    return Check(
        label, False, "ok" if ok else "off",
        path.name if ok else "no AGF stanza — `ws rig init --agents` / `--claude`",
    )


def scan(cfg, ident, entry, root: Path) -> list[Check]:
    provider, org, repo = ident
    checks: list[Check] = []

    # ---- Required: core AGF ----
    if entry is not None:
        checks.append(
            Check("rig registered", True, "ok", f"prefix={entry['prefix']} kind={entry['kind']}")
        )
    else:
        checks.append(
            Check("rig registered", True, "missing", "not in managed_repos — `ws rig init`")
        )
    checks.append(
        _required(
            "beads initialized", Path(".beads").is_dir(), ".beads/", "missing — `ws rig init`"
        )
    )
    checks.append(
        _required(
            "PRIME.md", Path(".beads/PRIME.md").exists(),
            ".beads/PRIME.md", "missing — `ws rig init --prime`",
        )
    )
    checks.append(
        _required(
            "claude settings", Path(".claude/settings.json").exists(),
            ".claude/settings.json", "missing — `ws rig init --claude`",
        )
    )
    checks.append(
        _required("skills", _has_bundled_skill(), "skills/", "missing — `ws rig init --skills`")
    )

    # ---- Optional: integrations that could be set up ----
    checks.extend(_observaloop_checks(cfg, entry))
    checks.append(_grant_check(cfg, root, provider, org, repo))
    checks.append(_hint_check("AGENTS.md hint", root / "AGENTS.md"))
    checks.append(_hint_check("CLAUDE.md hint", root / "CLAUDE.md"))
    return checks


def _line(c: Check) -> None:
    detail = f"  {c.detail}" if c.detail else ""
    typer.echo(f"  {_GLYPH[c.state]} {c.label:<18}{detail}")


def _render_verbose(checks: list[Check]) -> None:
    typer.echo("# Required")
    for c in (c for c in checks if c.required):
        _line(c)
    typer.echo("\n# Optional")
    for c in (c for c in checks if not c.required):
        _line(c)
    typer.echo("")


def run_check(verbose: bool = False, cwd=None) -> None:
    """Scan the current rig and exit 0 (ready) / 1 (a required check failed)."""
    cfg = config.load()
    ident = workspace_identity(cwd)
    if ident is None:
        typer.echo("✗ not in a git repo under $GIT_WORKSPACE — not an AGF rig.", err=True)
        raise typer.Exit(1)
    provider, org, repo = ident
    entry = registry.find_entry(cfg, provider, org, repo)
    root = _repo_root(cwd)
    label = str(entry["prefix"]) if entry else repo

    checks = scan(cfg, ident, entry, root)
    failed = sum(1 for c in checks if c.required and c.state != "ok")

    if verbose:
        _render_verbose(checks)
    if failed:
        tail = "" if verbose else " (run -v for the breakdown)"
        typer.echo(f"✗ rig '{label}' not ready for AGF — {failed} required check(s) failed{tail}")
        raise typer.Exit(1)
    typer.echo(f"✓ rig '{label}' ready for AGF.")
    raise typer.Exit(0)
