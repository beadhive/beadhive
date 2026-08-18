"""`bh hive ready` — read-only AGF readiness check for the current hive.

Verifies core AGF setup (required) plus optional integrations, prints a yes/no verdict
(exit 0 ready / 1 not), and with ``-v`` a per-line-item breakdown. Read-only: no writes,
no bd/git lifecycle. Live observaloop/grafana probes run ONLY when the integration is
enabled (otherwise the line is N/A, never probed).
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import typer

from . import (
    config,
    dolt_health,
    gitworkspace_plugin,
    hive,
    hive_schema,
    observaloop,
    otel,
    plugins,
    registry,
    store_locator,
    validate_probe,
)
from .hive import _is_plugin_installed  # shared with the installer (defined in hive.py)
from .identity import workspace_identity
from .run import run

# Same marker hive._ensure_agf_hint writes into AGENTS.md / CLAUDE.md.
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


def _otel_sdk_check(cfg) -> Check:
    """Required (fails readiness) once ``otel.enabled`` is true: the config asserts a capability
    the binary must actually have. Config default is false (bh-vy4t9), so this is N/A — never
    probed, never failing — for every default install; it only engages for a hive that opted in.
    Mirrors the ``otel_install_hint`` warning's own gap detection (``beadhive.otel.init``) so
    readiness and the per-invocation hint agree on what "installed" means."""
    if not config.otel_enabled(cfg):
        return Check("otel SDK", False, "na", "disabled (otel.enabled=false)")
    return _required(
        "otel SDK",
        otel.sdk_importable(),
        "opentelemetry SDK installed",
        "otel.enabled=true but the SDK is not installed — "
        f"pip install '{config.BINARY_NAME}[otel]'",
    )


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


def _git_workspace_check(cfg, entry) -> Check:
    """git-workspace's readiness line, called directly rather than through `_plugin_checks`
    (bh-hsus.4): it is a required dep (`deps.py`, `required=ALWAYS`), not a `plugins.Plugin`, so
    it has no `enabled` flag to gate the generic loop on — it is always live-probed, same as
    `_dolt_server_check` / `_schema_version_check` below."""
    state, detail = gitworkspace_plugin.readiness(cfg, entry) or ("off", "unknown")
    return Check("git-workspace", False, state, detail)


def _grant_check(cfg, root: Path, provider: str, org: str, repo: str) -> Check:
    """Claude-specific: is `.claude/settings.local.json`'s allowWrite grant current for this
    hive's worktree root (`hive._install_sandbox_grant`)? Codex has no equivalent grant file
    bh can write — its own reachability is reported separately by `_codex_sandbox_check`, so
    a green line here says nothing about a Codex session (bh-rpzaj)."""
    cur = hive.grant_is_current(cfg, root, provider, org, repo)
    if cur is None:
        return Check(
            "sandbox grant", False, "off", f"no grant — `{config.BINARY_ALIAS} hive init --claude`"
        )
    if cur:
        return Check("sandbox grant", False, "ok", "current")
    return Check(
        "sandbox grant",
        False,
        "off",
        f"stale (hive moved) — `{config.BINARY_ALIAS} hive init --claude -f`",
    )


def _codex_sandbox_check(cfg) -> Check:
    """bh-rpzaj: unlike Claude (`_grant_check`), bh cannot write Codex a grant file — its
    writable roots come from however `codex` itself was launched. This reports whether the
    hive's *persistent* worktree root falls inside Codex's DEFAULT sandbox (cwd + $TMPDIR,
    `config.codex_default_sandbox_covers`) so the gap surfaces here instead of showing up
    later as a Codex sub-agent unable to edit a worktree `bh work assign`/`claim` already
    provisioned. Ephemeral worktrees (the default) already live in the OS temp dir, always
    reachable, so this is N/A for them."""
    if config.worktrees_ephemeral(cfg):
        return Check(
            "codex sandbox", False, "na", "ephemeral worktrees (OS temp) — always reachable"
        )
    wt_root = config.worktrees_root(cfg)
    if config.codex_default_sandbox_covers(wt_root):
        return Check("codex sandbox", False, "ok", f"{wt_root} is under cwd/$TMPDIR")
    return Check(
        "codex sandbox",
        False,
        "off",
        f"{wt_root} is outside Codex's default sandbox (cwd + $TMPDIR) — "
        "set worktrees.ephemeral: true, move worktrees.path under $TMPDIR, "
        f"or launch codex with --add-dir {wt_root}",
    )


def _deprecation_checks(root: Path) -> list[Check]:
    """Warn-level drift signals (never fail the gate): legacy .beads/PRIME.md (deprecated —
    steering is bh-owned) and bd-authored CLAUDE.md 'BEADS INTEGRATION' blocks (their embedded
    template drifts with the installed bd binary; the bh AGF stanza is canonical)."""
    checks: list[Check] = []
    if (root / ".beads/PRIME.md").exists():
        checks.append(
            Check(
                "PRIME.md",
                False,
                "warn",
                ".beads/PRIME.md is deprecated — remove it (steering is bh-owned)",
            )
        )
    claude_md = root / "CLAUDE.md"
    if claude_md.exists() and "BEGIN BEADS INTEGRATION" in claude_md.read_text(errors="ignore"):
        checks.append(
            Check(
                "bd CLAUDE.md block",
                False,
                "warn",
                "bd-authored BEADS INTEGRATION block present — bh's AGF stanza is "
                "canonical; remove the block (its embedded template drifts with bd)",
            )
        )
    return checks


def _validate_cmd_check(cfg, entry, root: Path) -> Check:
    """Nudge for bh-l44i: an operator who never set `work.validate_cmd` is riding the
    `just check` default. A named override — even a compile-only one — is a deliberate choice
    and stays green; only an unconfigured default that RESOLVES (via validate_probe, following
    the hive's own justfile — not a string guess) to provably test-free gets the warn. Anything
    the probe can't resolve (no justfile, a non-`just` command, an unresolvable recipe) stays
    green too — an unconfirmed guess is not grounds for a warning (bh-l44i's rework: the naive
    substring heuristic fired on the fleet-wide dominant `just check` -> ... -> pytest chain)."""
    cmd = config.validate_cmd(cfg, entry)
    if config.validate_cmd_is_configured(cfg, entry):
        return Check("validate_cmd", False, "ok", f"configured: {cmd!r}")
    probe = validate_probe.probe_validate_cmd(cmd, root)
    if probe is True:
        return Check(
            "validate_cmd",
            False,
            "warn",
            f"default {cmd!r} does not look like it runs tests — set work.validate_cmd "
            "explicitly if that's intentional (a compile-only default silently lets test "
            "regressions merge clean)",
        )
    detail = f"default: {cmd!r} (runs tests)" if probe is False else f"default: {cmd!r}"
    return Check("validate_cmd", False, "ok", detail)


def _dolt_server_check(root: Path) -> Check:
    """Store-engine liveness (bh-areg.3): embedded mode has no liveness question at all (the
    engine is in-process) — ``na`` there, matching observaloop's disabled-integration
    convention, so an unmigrated hive (the common case today) is unaffected.

    Advisory only (``warn``, never ``missing``/required) when a mode-(a) hive's shared server
    is unreachable — copying `setup.dolt_fix_advisory`'s shape (informs without blocking,
    per this bead's own DESIGN note): a down server is an OPERATIONAL fact that changes hour
    to hour, not a structural AGF-setup gap, so it must never flip `bh hive ready`'s exit code.
    """
    mode = store_locator.dolt_mode(root)
    mismatch = dolt_health.mismatch_reason(root)
    if mismatch:
        return Check("dolt server", False, "warn", mismatch)
    if mode != "server":
        return Check("dolt server", False, "na", "embedded (no server)")
    probe = dolt_health.probe_shared_server()
    if probe.reachable:
        return Check("dolt server", False, "ok", probe.detail)
    return Check(
        "dolt server",
        False,
        "warn",
        f"{probe.detail} — bd verbs will hard-fail until it's back; start it with "
        "`bd dolt start` (bh does not auto-start it or fall back to embedded)",
    )


def _schema_version_check(entry, root: Path) -> Check:
    """Read-only: this hive's recorded bd schema version vs. THIS host's bd (`bh-wnly`) — read
    from HQ's `hive_schema` record, WITHOUT opening this hive's own store (AC1 + AC5; `root` is
    accepted for signature symmetry with the other checks but deliberately unused — the whole
    point is not touching it). `bh doctor` is the refresh trigger (see `hive_schema`'s module
    docstring); this check only READS what doctor last recorded, so a hive `bh doctor` has never
    reached shows up honestly as "never recorded" (`warn`), never a silent green tick (AC4)."""
    del root  # unused — see docstring: this check must not touch the hive's own checkout/store
    if entry is None:
        return Check("bd schema version", False, "na", "hive not registered")
    hq_dir = config.hq_dir()
    if not (hq_dir / ".beads").is_dir():
        return Check("bd schema version", False, "na", "no Factory HQ — `bh hq init`")
    local = dolt_health.local_bd_schema_version()
    if local.version is None:
        return Check("bd schema version", False, "na", "bd unavailable — can't judge")

    record = hive_schema.try_load(hq_dir, entry["provider"], entry["org"], entry["repo"])
    if record is None:
        return Check(
            "bd schema version",
            False,
            "warn",
            f"never recorded — run `{config.BINARY_ALIAS} doctor` to populate",
        )
    age_days = hive_schema.age_seconds(record) / 86400.0
    advisory = dolt_health.schema_skew_advisory(str(entry["prefix"]), local, record.schema_version)
    if advisory:
        return Check(
            "bd schema version",
            False,
            "warn",
            f"v{record.schema_version} ahead of this bd's v{local.version} "
            f"(recorded {age_days:.1f}d ago)",
        )
    if hive_schema.is_stale(record):
        return Check(
            "bd schema version",
            False,
            "warn",
            f"last confirmed {age_days:.1f}d ago (v{record.schema_version}) — unverified "
            f"since; run `{config.BINARY_ALIAS} doctor` to refresh",
        )
    return Check(
        "bd schema version",
        False,
        "ok",
        f"v{record.schema_version} <= this bd's v{local.version} (confirmed {age_days:.1f}d ago)",
    )


def _hint_check(label: str, path: Path) -> Check:
    ok = path.exists() and AGF_MARKER in path.read_text(errors="ignore")
    return Check(
        label,
        False,
        "ok" if ok else "off",
        path.name
        if ok
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
                "hive registered",
                True,
                "missing",
                f"not in managed_repos — `{config.BINARY_ALIAS} hive init`",
            )
        )
    checks.append(
        _required(
            "beads initialized",
            Path(".beads").is_dir(),
            ".beads/",
            f"missing — `{config.BINARY_ALIAS} hive init`",
        )
    )
    # Escalation parent: every host needs a kind=hq hive so `bh escalate` always has a
    # target — 'escalation parent: none' is on track to become invalid config (bh-ufne).
    hq_entry = registry.hive_of_kind(cfg, registry.HQ_KIND)
    checks.append(
        _required(
            "escalation parent",
            hq_entry is not None,
            f"HQ registered (kind={registry.HQ_KIND})",
            f"no kind=hq hive — `{config.BINARY_ALIAS} hq init`",
        )
    )
    # Declared footprint: tracked furniture is required only on furnished hives;
    # zero-footprint hives (the default) are green without any repo files.
    furnished = registry.furnish_of(entry) == "full" if entry is not None else False
    settings_ok = Path(".claude/settings.json").exists()
    if furnished:
        checks.append(
            _required(
                "claude settings",
                settings_ok,
                ".claude/settings.json",
                f"missing — `{config.BINARY_ALIAS} hive init --claude`",
            )
        )
    else:
        checks.append(
            Check(
                "claude settings",
                False,
                "ok" if settings_ok else "na",
                ".claude/settings.json"
                if settings_ok
                else f"zero-footprint hive — `{config.BINARY_ALIAS} hive init --furnish` to add",
            )
        )
    plugin_mode = config.claude_source(cfg, entry) == "plugin"
    plugin_name = config.claude_plugin_name(cfg, entry)
    skills_ok = _has_bundled_skill(cfg, entry)
    agents_ok = _has_bundled_agent(cfg, entry)
    skills_ok_detail = (
        f"agf plugin '{plugin_name}' installed" if (plugin_mode and skills_ok) else "skills/"
    )
    agents_ok_detail = (
        f"agf plugin '{plugin_name}' installed"
        if (plugin_mode and agents_ok)
        else ".claude/agents/"
    )
    skills_miss = (
        f"plugin '{plugin_name}' not installed — `{config.BINARY_ALIAS} hive init --claude`"
        if plugin_mode
        else f"missing — `{config.BINARY_ALIAS} hive init --skills`"
    )
    agents_miss = f"missing — `{config.BINARY_ALIAS} hive init --claude`"
    # In plugin mode skills/agents come from the user-level plugin (no repo files) and stay
    # required; local-copy mode only makes sense on a furnished hive.
    skills_agents_required = plugin_mode or furnished
    checks.append(
        Check(
            "skills",
            skills_agents_required,
            "ok" if skills_ok else ("missing" if skills_agents_required else "off"),
            skills_ok_detail if skills_ok else skills_miss,
        )
    )
    checks.append(
        Check(
            "agents",
            skills_agents_required,
            "ok" if agents_ok else ("missing" if skills_agents_required else "off"),
            agents_ok_detail if agents_ok else agents_miss,
        )
    )
    checks.extend(_deprecation_checks(root))

    # ---- Optional: integrations that could be set up ----
    checks.append(_validate_cmd_check(cfg, entry, root))
    checks.append(_dolt_server_check(root))
    checks.append(_schema_version_check(entry, root))
    checks.append(_otel_sdk_check(cfg))
    checks.extend(_observaloop_checks(cfg, entry))
    checks.append(_git_workspace_check(cfg, entry))
    checks.extend(_plugin_checks(cfg, entry))
    checks.append(_grant_check(cfg, root, provider, org, repo))
    checks.append(_codex_sandbox_check(cfg))
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
    """Scan the current hive and exit 0 (ready) / 1 (a required check failed)."""
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
