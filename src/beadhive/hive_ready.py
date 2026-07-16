"""`bh rig ready` — read-only AGF readiness check for the current rig.

Verifies core AGF setup (required) plus optional integrations, prints a yes/no verdict
(exit 0 ready / 1 not), and with ``-v`` a per-line-item breakdown. Read-only: no writes,
no bd/git lifecycle. Live observaloop/grafana probes run ONLY when the integration is
enabled (otherwise the line is N/A, never probed).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import NamedTuple

import typer

from . import config, hive, observaloop, plugins, registry
from .identity import workspace_identity
from .run import run

# Same marker rig._ensure_agf_hint writes into AGENTS.md / CLAUDE.md.
AGF_MARKER = "<!-- bh:agf:start"

# state → glyph: ok=present/up, missing=required gap (fails), off=optional not set up,
# na=integration disabled so not probed, warn=optional degradation (never fails the gate).
_GLYPH = {"ok": "✓", "missing": "✗", "off": "•", "na": "-", "warn": "!"}


class Check(NamedTuple):
    label: str
    required: bool
    state: str  # ok | missing | off | na | warn
    detail: str = ""


def _repo_root(cwd=None) -> Path:
    res = run(["git", "rev-parse", "--show-toplevel"], check=False, capture=True, cwd=cwd)
    return Path(res.stdout.strip()) if res.returncode == 0 else Path.cwd()


def _is_plugin_installed(plugin: str) -> bool:
    """True when a Claude Code plugin named ``plugin`` is installed (any scope/marketplace).

    Reads ``~/.claude/plugins/installed_plugins.json`` and checks whether any key starts
    with ``<plugin>@`` — the installed-plugin-key format Claude Code uses internally."""
    installed_file = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
    if not installed_file.exists():
        return False
    try:
        data = json.loads(installed_file.read_text())
        return any(k.startswith(f"{plugin}@") for k in (data.get("plugins") or {}))
    except Exception:
        return False


def _has_bundled_skill(cfg=None, entry=None) -> bool:
    """True when role skills are available: plugin installed (plugin mode) OR local skills/ dir.

    In plugin mode: accepts the agf plugin install as equivalent to a local skills copy.
    In copy mode (or when plugin is not installed): falls back to the local skills/ check."""
    if config.claude_source(cfg, entry) == "plugin":
        plugin = config.claude_plugin_name(cfg, entry)
        if _is_plugin_installed(plugin):
            return True
        # Local override (.claude/agents/<seat>.md) also OK even in plugin mode.
    dst = Path("skills")
    if not dst.is_dir():
        return False
    names = {p.name for p in config.skills_src().iterdir() if p.is_dir()}
    return any((dst / n).is_dir() for n in names)


def _has_bundled_agent(cfg=None, entry=None) -> bool:
    """True when seat agents are available: plugin installed (plugin mode) OR local .claude/agents/.

    In plugin mode: accepts the agf plugin install as equivalent to local agent files.
    A local .claude/agents/<seat>.md override also satisfies the check (it outranks the plugin
    and will load instead).  In copy mode: only local files count."""
    if config.claude_source(cfg, entry) == "plugin":
        plugin = config.claude_plugin_name(cfg, entry)
        if _is_plugin_installed(plugin):
            return True
    dst = Path(".claude") / "agents"
    if not dst.is_dir():
        return False
    names = {p.name for p in config.agents_src().iterdir() if p.suffix == ".md"}
    return any((dst / n).is_file() for n in names)


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
        f"profile '{profile}' "
        f"{'up' if endpoint else f'down — `{config.BINARY_ALIAS} hive init --observaloop`'}",
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


def _plugin_checks(cfg, entry) -> list[Check]:
    """Generic readiness line per registered plugin — loops plugins.registry() so no integration
    is hardcoded here. Disabled plugins are N/A (never live-probed, mirroring the observaloop
    convention); enabled plugins run their live ``readiness`` probe for an ok/missing state."""
    checks: list[Check] = []
    for p in plugins.registry():
        if p.readiness is None:
            continue
        if not p.enabled(cfg, entry):
            checks.append(Check(p.name, False, "na", "disabled"))
            continue
        state, detail = p.readiness(cfg, entry) or ("off", "unknown")
        checks.append(Check(p.name, False, state, detail))
    return checks


def _grant_check(cfg, root: Path, provider: str, org: str, repo: str) -> Check:
    cur = hive.grant_is_current(cfg, root, provider, org, repo)
    if cur is None:
        return Check(
            "sandbox grant", False, "off", f"no grant — `{config.BINARY_ALIAS} hive init --claude`"
        )
    if cur:
        return Check("sandbox grant", False, "ok", "current")
    return Check(
        "sandbox grant", False, "off",
        f"stale (hive moved) — `{config.BINARY_ALIAS} hive init --claude -f`",
    )


def _deprecation_checks(root: Path) -> list[Check]:
    """Warn-level drift signals (never fail the gate): legacy .beads/PRIME.md (deprecated —
    steering is bh-owned) and bd-authored CLAUDE.md 'BEADS INTEGRATION' blocks (their embedded
    template drifts with the installed bd binary; the bh AGF stanza is canonical)."""
    checks: list[Check] = []
    if (root / ".beads/PRIME.md").exists():
        checks.append(
            Check("PRIME.md", False, "warn",
                  ".beads/PRIME.md is deprecated — remove it (steering is bh-owned)")
        )
    claude_md = root / "CLAUDE.md"
    if claude_md.exists() and "BEGIN BEADS INTEGRATION" in claude_md.read_text(errors="ignore"):
        checks.append(
            Check("bd CLAUDE.md block", False, "warn",
                  "bd-authored BEADS INTEGRATION block present — bh's AGF stanza is "
                  "canonical; remove the block (its embedded template drifts with bd)")
        )
    return checks


def _hint_check(label: str, path: Path) -> Check:
    ok = path.exists() and AGF_MARKER in path.read_text(errors="ignore")
    return Check(
        label, False, "ok" if ok else "off",
        path.name if ok
        else f"no AGF stanza — `{config.BINARY_ALIAS} hive init --agents` / `--claude`",
    )


def scan(cfg, ident, entry, root: Path) -> list[Check]:
    provider, org, repo = ident
    checks: list[Check] = []

    # ---- Required: core AGF ----
    if entry is not None:
        checks.append(
            Check("hive registered", True, "ok", f"prefix={entry['prefix']} kind={entry['kind']}")
        )
    else:
        checks.append(
            Check(
                "hive registered", True, "missing",
                f"not in managed_repos — `{config.BINARY_ALIAS} hive init`",
            )
        )
    checks.append(
        _required(
            "beads initialized", Path(".beads").is_dir(), ".beads/",
            f"missing — `{config.BINARY_ALIAS} hive init`",
        )
    )
    # Declared footprint: tracked furniture is required only on furnished rigs;
    # zero-footprint rigs (the default) are green without any repo files.
    furnished = registry.furnish_of(entry) == "full" if entry is not None else False
    settings_ok = Path(".claude/settings.json").exists()
    if furnished:
        checks.append(
            _required(
                "claude settings", settings_ok,
                ".claude/settings.json", f"missing — `{config.BINARY_ALIAS} hive init --claude`",
            )
        )
    else:
        checks.append(
            Check(
                "claude settings", False, "ok" if settings_ok else "na",
                ".claude/settings.json" if settings_ok
                else f"zero-footprint hive — `{config.BINARY_ALIAS} hive init --furnish` to add",
            )
        )
    plugin_mode = config.claude_source(cfg, entry) == "plugin"
    plugin_name = config.claude_plugin_name(cfg, entry)
    skills_ok = _has_bundled_skill(cfg, entry)
    agents_ok = _has_bundled_agent(cfg, entry)
    skills_ok_detail = (
        f"agf plugin '{plugin_name}' installed" if (plugin_mode and skills_ok)
        else "skills/"
    )
    agents_ok_detail = (
        f"agf plugin '{plugin_name}' installed" if (plugin_mode and agents_ok)
        else ".claude/agents/"
    )
    skills_miss = (
        f"plugin '{plugin_name}' not installed — `{config.BINARY_ALIAS} hive init --claude`"
        if plugin_mode else f"missing — `{config.BINARY_ALIAS} hive init --skills`"
    )
    agents_miss = f"missing — `{config.BINARY_ALIAS} hive init --claude`"
    # In plugin mode skills/agents come from the user-level plugin (no repo files) and stay
    # required; local-copy mode only makes sense on a furnished rig.
    skills_agents_required = plugin_mode or furnished
    checks.append(
        Check("skills", skills_agents_required,
              "ok" if skills_ok else ("missing" if skills_agents_required else "off"),
              skills_ok_detail if skills_ok else skills_miss)
    )
    checks.append(
        Check("agents", skills_agents_required,
              "ok" if agents_ok else ("missing" if skills_agents_required else "off"),
              agents_ok_detail if agents_ok else agents_miss)
    )
    checks.extend(_deprecation_checks(root))

    # ---- Optional: integrations that could be set up ----
    checks.extend(_observaloop_checks(cfg, entry))
    checks.extend(_plugin_checks(cfg, entry))
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
        typer.echo("✗ not in a git repo under $GIT_WORKSPACE — not an AGF hive.", err=True)
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
        typer.echo(f"✗ hive '{label}' not ready for AGF — {failed} required check(s) failed{tail}")
        raise typer.Exit(1)
    typer.echo(f"✓ hive '{label}' ready for AGF.")
    raise typer.Exit(0)
