"""Onboard the current repo as a beads rig. Ports scripts/rig-init.sh.

classify → resolve kind → fork gate → derive/override prefix → enforce required-org
policy → bd init/materialize → register → declared installers → footprint.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import typer

from . import config, registry
from .identity import workspace_identity, workspace_root
from .run import (  # noqa: F401 — re-exported as rig.run; onboard actions + tests patch this seam
    retry_on_index_lock,
    run,
)


def _base(base) -> Path:
    """Normalize an optional target dir to a Path (current dir when None) — the single seam
    that lets `init` (and its helpers) operate on a directory other than the process cwd."""
    return Path(base) if base else Path(".")


def _deep_merge(a, b):
    """Merge b into a: dicts merge recursively; lists union (dedup, order-preserving) so
    installing our deny rule / SessionStart hook never clobbers the repo's existing ones
    and re-running is idempotent; scalars take b."""
    if isinstance(a, dict) and isinstance(b, dict):
        out = dict(a)
        for k, v in b.items():
            out[k] = _deep_merge(a[k], v) if k in a else v
        return out
    if isinstance(a, list) and isinstance(b, list):
        merged = list(a)
        for item in b:
            if item not in merged:
                merged.append(item)
        return merged
    return b


# ---- AGF hint stanza (AGENTS.md / CLAUDE.md) --------------------------------
# A small managed block pointing agent harnesses at `bh rig ready`, so a harness that reads
# AGENTS.md (Codex/others) or CLAUDE.md can still answer "is this repo set up for AGF?".
# Non-destructive: we only ever write our own marked block, never rewrite the user's
# surrounding content.

_AGF_MARK_START = "<!-- bh:agf:start"
_AGF_MARK_END = "<!-- bh:agf:end -->"


def _replace_agf_block(text: str, block: str) -> str:
    start = text.index(_AGF_MARK_START)
    end = text.index(_AGF_MARK_END, start) + len(_AGF_MARK_END)
    return text[:start] + block + text[end:]


def agf_context(cwd=None) -> dict | None:
    """Registry-driven AGF steering for session hooks (`bh rig context`).

    Inside a REGISTERED rig, returns ``{"text", "prefix", "kind", "furnish"}`` — the AGF hint
    body plus this rig's registry facts — so a user-level SessionStart hook can steer agents
    in zero-footprint rigs with zero repo files. Returns None outside a git repo under
    $GIT_WORKSPACE or in an unregistered repo. Local reads only — no network."""
    ident = workspace_identity(cwd=cwd)
    if ident is None:
        return None
    entry = registry.find_entry(config.load(), *ident)
    if entry is None:
        return None
    block = config.asset("AGF-hint.md").read_text()
    # The managed markers are for in-file stanzas; the hook payload carries only the body.
    text = "\n".join(ln for ln in block.splitlines() if not ln.startswith("<!--")).strip()
    furnish = registry.furnish_of(entry)
    facts = (
        f"\n\nThis hive: prefix `{entry['prefix']}`, kind `{entry['kind']}`, "
        f"footprint `{furnish}`."
    )
    return {
        "text": text + facts,
        "prefix": str(entry["prefix"]),
        "kind": str(entry["kind"]),
        "furnish": furnish,
    }


def _ensure_agf_hint(path: Path, force: bool, flag: str) -> None:
    """Ensure the managed AGF stanza is present in `path`.

    file absent → create; markers present → idempotent skip (`force` refreshes the block in
    place); markers absent but file exists → append (preserves existing content)."""
    block = config.asset("AGF-hint.md").read_text().strip()
    if not path.exists():
        path.write_text(block + "\n")
        typer.echo(f"✓ {flag}: {path.name} (AGF hint)")
        return
    text = path.read_text()
    if _AGF_MARK_START in text:
        if not force:
            typer.echo(f"• {flag}: {path.name} AGF hint present — skipped (use -f to refresh)")
            return
        path.write_text(_replace_agf_block(text, block))
        typer.echo(f"✓ {flag}: {path.name} AGF hint refreshed")
        return
    sep = "" if text.endswith("\n") else "\n"
    path.write_text(text + sep + "\n" + block + "\n")
    typer.echo(f"✓ {flag}: {path.name} AGF hint appended")


def _install_skills(force=False, base=None):
    """Copy bundled skills into ./skills, per-skill. Skip those already present unless force."""
    src = config.skills_src()
    dst = _base(base) / "skills"
    dst.mkdir(exist_ok=True)
    added, skipped = [], []
    for skill in sorted(p for p in src.iterdir() if p.is_dir()):
        target = dst / skill.name
        if target.exists() and not force:
            skipped.append(skill.name)
            continue
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(skill, target)
        added.append(skill.name)
    detail = ", ".join(added) if added else "none"
    kept = f"; {len(skipped)} kept" if skipped else ""
    typer.echo(f"✓ --skills: skills/ (+{len(added)}: {detail}{kept})")


def _link_skills_claude(force=False, base=None):
    """Symlink .claude/skills -> ../skills so Claude Code discovers them on launch."""
    base = _base(base)
    (base / ".claude").mkdir(exist_ok=True)
    link = base / ".claude/skills"
    want = Path("../skills")
    if link.is_symlink() and link.readlink() == want:
        return
    if link.is_symlink() or link.exists():
        if not force:
            typer.echo("• --skills+--claude: .claude/skills exists — skipped (use -f)")
            return
        if link.is_dir() and not link.is_symlink():
            shutil.rmtree(link)
        else:
            link.unlink()
    link.symlink_to(want)
    typer.echo("✓ --skills+--claude: .claude/skills -> ../skills")


def _install_agents_claude(force=False, base=None):
    """Copy bundled agent defs into .claude/agents/, per-file. Skip existing unless force."""
    src = config.agents_src()
    dst = _base(base) / ".claude" / "agents"
    dst.mkdir(parents=True, exist_ok=True)
    added, skipped = [], []
    for agent in sorted(p for p in src.iterdir() if p.suffix == ".md"):
        target = dst / agent.name
        if target.exists() and not force:
            skipped.append(agent.name)
            continue
        shutil.copy(agent, target)
        added.append(agent.name)
    detail = ", ".join(added) if added else "none"
    kept = f"; {len(skipped)} kept" if skipped else ""
    typer.echo(f"✓ --claude: .claude/agents/ (+{len(added)}: {detail}{kept})")


def _known_marketplace_path(name: str) -> str:
    """Local directory an already-registered Claude Code marketplace ``name`` points at.

    Reads ``~/.claude/plugins/known_marketplaces.json`` (Claude Code's registry of added
    marketplaces). Returns '' when the name is unknown, the file is absent/unreadable,
    or the marketplace is remote (github form) — the guard only compares local paths."""
    registry_file = Path.home() / ".claude" / "plugins" / "known_marketplaces.json"
    if not registry_file.is_file():
        return ""
    try:
        entry = json.loads(registry_file.read_text()).get(name) or {}
    except (OSError, json.JSONDecodeError):
        return ""
    source = entry.get("source") or {}
    if source.get("source") != "directory":
        return ""
    return str(source.get("path") or "")


def _install_plugin_claude(cfg, entry=None) -> None:
    """Install the agf plugin via the Claude Code marketplace (plugin mode).

    Runs ``claude plugin marketplace add <marketplace>`` to register the marketplace
    (idempotent — Claude Code ignores a duplicate add), then
    ``claude plugin install <plugin>@<mp> --scope <scope>`` to install the plugin.
    Both commands are passed to ``run()`` so tests can patch the seam.

    Writing NO .claude/agents/* files, NO ./skills copy, and NO .claude/skills symlink
    is a hard invariant: the plugin vends everything; only settings.json and the sandbox
    grant are written (those happen via the remaining _install_claude_settings /
    _install_sandbox_grant callers in _do_claude / onboard._do_claude)."""
    marketplace = config.claude_marketplace(cfg, entry)
    plugin = config.claude_plugin_name(cfg, entry)
    scope = config.claude_scope(cfg, entry)
    # Resolve the marketplace *name* first: `plugin install` addresses a marketplace
    # by its manifest name, not its path — for a local marketplace read the name from
    # disk; remote forms pass through.
    manifest = Path(marketplace) / ".claude-plugin" / "marketplace.json"
    mp_name = (
        json.loads(manifest.read_text()).get("name", marketplace)
        if manifest.is_file()
        else marketplace
    )
    # Guard: `claude plugin marketplace add` is last-writer-wins by
    # name — adding the same name from a different directory silently re-points the
    # user-level marketplace (hijack). Refuse to re-point; keep the existing registration.
    existing = _known_marketplace_path(mp_name) if manifest.is_file() else ""
    if existing and Path(existing).resolve() != Path(marketplace).resolve():
        typer.echo(
            f"⚠ --claude: marketplace '{mp_name}' already registered at {existing!r} — "
            f"refusing to re-point it at {marketplace!r}; keeping the existing registration. "
            f"If intended: claude plugin marketplace remove {mp_name}, then re-run.",
            err=True,
        )
    else:
        # Add marketplace to Claude Code's known list (idempotent at the same path).
        typer.echo(f"  claude plugin marketplace add {marketplace!r}")
        run(["claude", "plugin", "marketplace", "add", marketplace], check=True, capture=False)
    mp_ref = f"{plugin}@{mp_name}"
    typer.echo(f"  claude plugin install {mp_ref!r} --scope {scope!r}")
    run(
        ["claude", "plugin", "install", mp_ref, "--scope", scope],
        check=True,
        capture=False,
    )
    typer.echo(f"✓ --claude: agf plugin '{plugin}' installed (scope={scope}, mp={marketplace!r})")


def _install_claude_settings(base=None):
    base = _base(base)
    (base / ".claude").mkdir(exist_ok=True)
    addon = json.loads(config.asset("claude-settings.json").read_text())
    settings = base / ".claude/settings.json"
    merged = _deep_merge(json.loads(settings.read_text()), addon) if settings.exists() else addon
    settings.write_text(json.dumps(merged, indent=2) + "\n")
    typer.echo("✓ --claude: .claude/settings.json (SessionStart hook + bd-remember deny)")


# ---- observaloop profile + dashboard ----------------------------------------
# `--observaloop`: stand up this rig's per-rig observaloop profile and install the ws telemetry
# Grafana dashboard, following the `if claude:` installer pattern. Every step is
# best-effort — the ws.observaloop wrappers no-op (warn + None) when observaloop / docker is
# absent, so absence degrades to a warning + continue, never an abort.


def _load_observaloop_dashboard() -> dict:
    """Parse the ws-shipped Grafana dashboard model (assets/observaloop/ws-dashboard.json)."""
    return json.loads(config.observaloop_dashboard_asset().read_text())


def _load_observaloop_metrics_preset() -> dict:
    """Parse the bh-shipped CLI-metrics collector preset (cli-metrics-preset.yaml).

    YAML, parsed with the repo's ruamel parser (pyyaml is not a dependency); a plain dict of
    ``processors`` + ``metrics_pipeline_processors`` the observaloop adapter merges into the
    profile collector's metrics pipeline."""
    from ruamel.yaml import YAML

    return YAML(typ="safe").load(config.observaloop_metrics_preset_asset().read_text())


def _install_observaloop(cfg, entry: dict) -> None:
    """Ensure+up this rig's observaloop profile, apply the CLI-metrics collector preset, then the
    bh Grafana telemetry dashboard.

    Gating, in order, each a warn-and-continue (rig init still succeeds):
      * ``otel.enabled`` false — observaloop needs otel to receive anything; warn but still
        ensure the profile so a later `otel.enabled: true` flip just works.
      * no derivable profile name (unregistered prefix) — nothing to create; return.
      * ``observaloop.is_available()`` false (observaloop/docker absent or unreachable) — skip
        the profile + preset + dashboard.
      * preset apply (collector reshape) runs right after up — it reshapes the profile collector's
        metrics pipeline (strip_instance + promote_bh_attrs + deltatocumulative) so short-lived bh
        CLI metrics accumulate with bh.* labels. It needs only the collector (not Grafana), so it
        sits *before* the visualizer gate; a falsy apply (collector tool unavailable) warns and
        continues, and re-applying on re-init is idempotent (the adapter merges deterministically).
      * visualizer not reachable — ensure+up the profile but skip the Grafana dashboard (the
        ``grafana_*`` tools only exist when Grafana is the reachable visualizer).
    """
    from . import observaloop

    if not config.otel_enabled(cfg):
        typer.echo(
            "• --observaloop: otel.enabled is false — observaloop needs otel to receive "
            "telemetry; set `otel.enabled: true` to export.",
            err=True,
        )

    profile = config.observaloop_profile_name(cfg, entry)
    if not profile:
        typer.echo("• --observaloop: could not derive a profile name — skipped.", err=True)
        return

    if not observaloop.is_available(cfg):
        typer.echo(
            f"• --observaloop: observaloop unavailable — skipped (profile '{profile}' not "
            "created, dashboard not applied).",
            err=True,
        )
        return

    observaloop.ensure_profile(profile, cfg)
    observaloop.up(profile, cfg)
    typer.echo(f"✓ --observaloop: profile '{profile}' ensured + up.")

    # Collector reshape (independent of the visualizer): merge the CLI-metrics preset into the
    # profile collector's metrics pipeline so short-lived ws metrics promote ws.* attrs + delta-
    # accumulate. Best-effort + idempotent — a falsy apply warns and continues.
    preset = observaloop.apply_collector_preset(profile, _load_observaloop_metrics_preset(), cfg)
    if preset is None:
        typer.echo(
            "• --observaloop: CLI-metrics collector preset apply failed — continuing.", err=True
        )
    else:
        typer.echo("✓ --observaloop: CLI-metrics collector preset applied.")

    status = observaloop.visualizer_status(cfg)
    if not (isinstance(status, dict) and status.get("reachable")):
        typer.echo(
            "• --observaloop: visualizer not reachable — skipped the ws Grafana dashboard.",
            err=True,
        )
        return

    result = observaloop.apply_dashboards(_load_observaloop_dashboard(), cfg)
    if result is None:
        typer.echo("• --observaloop: dashboard apply failed — continuing.", err=True)
    else:
        typer.echo("✓ --observaloop: ws telemetry Grafana dashboard applied.")


# ---- sandbox worktree grant -------------------------------------------------
# Claude Code's sandbox makes cwd + the session tmpdir writable but NOT $HOME outside the
# project — so ws-managed worktrees under worktrees_root() (default ~/.ws/worktrees) are
# unwritable from a sandboxed session. We grant the rig's own worktree subtree
# (<root>/<provider>/<org>/<repo>) in .claude/settings.local.json (machine-local: the path
# is host-specific, so it must NOT go in the shared settings.json). Provisions FUTURE
# sandboxed sessions — settings are read at session start, not mid-run.


def _sandbox_subtree(cfg, provider: str, org: str, repo: str) -> str:
    """The rig's worktree subtree as a grant path — '~/'-relative when under $HOME (portable;
    Claude Code accepts '~/' in allowWrite), else absolute. Mirrors worktree.wt_dir's parent."""
    sub = (config.worktrees_root(cfg) / provider / org / repo).expanduser()
    if not sub.is_absolute():
        sub = Path(os.path.abspath(sub))
    try:
        return "~/" + str(sub.relative_to(Path.home()))
    except ValueError:
        return str(sub)


def _matches_hive(entry: str, triplet_suffix: str) -> bool:
    """True if a grant entry is THIS rig's subtree (under any root) — the relocation key."""
    p = os.path.expanduser(str(entry)).rstrip("/")
    return p.endswith("/" + triplet_suffix) or p == triplet_suffix


def _replace_for_hive(items, subtree: str, triplet_suffix: str) -> list:
    """Drop any prior entry for this rig (stale root after a move), then append the current
    subtree. Self-healing AND idempotent — re-running rewrites instead of accumulating."""
    kept = [x for x in (items or []) if not _matches_hive(x, triplet_suffix)]
    kept.append(subtree)
    return kept


def _merge_sandbox_grant(existing: dict, subtree: str, triplet_suffix: str) -> dict:
    """Immutable: return a new settings dict granting `subtree` write in both the bash
    sandbox (sandbox.filesystem.allowWrite) and the tool layer (permissions array)."""
    out = json.loads(json.dumps(existing or {}))  # deep copy — never mutate the caller's dict
    fs = out.setdefault("sandbox", {}).setdefault("filesystem", {})
    fs["allowWrite"] = _replace_for_hive(fs.get("allowWrite"), subtree, triplet_suffix)
    perms = out.setdefault("permissions", {})
    perms["additionalDirectories"] = _replace_for_hive(
        perms.get("additionalDirectories"), subtree, triplet_suffix
    )
    return out


def _git_exclude(rel: str, base=None) -> None:
    # ponytail: best-effort — keep the host-local settings file out of `git status` for rigs
    # that don't already ignore .claude/. Local .git/info/exclude, never the tracked .gitignore.
    base = _base(base)
    if not (base / ".git").is_dir():
        return
    exclude = base / ".git/info/exclude"
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if rel not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as fh:
            fh.write(rel + "\n")


# ---- declared-footprint convention (furnish axis) ---------------------------
# Onboarding makes in-repo changes ONLY when declared. The default is ZERO-footprint:
# .beads/ stays local-only behind a .git/info/exclude block, nothing is tracked, nothing is
# committed (bead state rides refs/dolt/data, not the working tree). FURNISHED rigs
# (furnish: full — an ownership-gated, conscious opt-in) TRACK their scaffolding in git
# (.beads config/metadata/issues.jsonl, .claude/settings.json, CLAUDE.md/AGENTS.md hints);
# bd's own .beads/.gitignore keeps the local-only pieces (dolt db, locks, backups) out, and
# host-local files live in .git/info/exclude (.ws/, .claude/settings.local.json). These
# helpers flip a repo between the two footprints after bd-init + the installers have run.

# bh-2w8d — bd fork auto-detection keys off git REMOTE TOPOLOGY (a distinct `upstream` remote),
# NOT GitHub's `isFork` flag: a repo GitHub reports as isFork:false still gets the exclude when
# it carries a local `upstream` remote (repro: origin alone → no exclude; origin + distinct
# upstream → exclude — expected bd behaviour, not a misfire, nothing to file upstream). The block
# ALSO changed shape across bd versions: bd ≤1.0.5 wrote `# Beads stealth mode …` + `.beads/`;
# bd ≥1.1.0 renamed the marker to `# Beads fork protection (bd init)` and added `**/RECOVERY*.md`
# + `**/SESSION*.md`. The remover must strip the WHOLE block regardless of wording/version, or a
# non-fork un-stealth leaves stray marker + pattern lines behind (the survey-row bug).

# Every exclude PATTERN bd's stealth / fork-protection block writes (both bd-version shapes).
_STEALTH_MARKERS = frozenset({".beads/", ".beads", "**/RECOVERY*.md", "**/SESSION*.md"})
# Its marker COMMENT line — `# Beads stealth mode …` (bd ≤1.0.5) / `# Beads fork protection …`
# (bd ≥1.1.0). Matched by prefix so either wording is stripped.
_STEALTH_COMMENT_PREFIXES = ("# Beads stealth mode", "# Beads fork protection")
_SCAFFOLD_PATHS = (".beads", ".claude", "CLAUDE.md", "AGENTS.md", "skills")
_SCAFFOLD_COMMIT_MSG = "chore(agf): hive scaffolding (beads + agent config)"
_SCAFFOLD_REPAIR_MSG = "chore(agf): hive scaffolding repair"
# The marker line bd init writes above the Beads/Dolt patterns it appends to the tracked root
# .gitignore (even under --setup-exclude) — the one tracked mutation zero-footprint must undo.
_BD_GITIGNORE_MARKER = "# Beads / Dolt files (added by bd init)"
_LOCK_RETRIES = 5
_LOCK_RETRY_SLEEP = 0.2  # seconds


def _git_locked_run(args: list[str], base: Path):
    """Run a git mutation, retrying briefly on transient index.lock contention.

    A `git commit` moments earlier (bd init's scaffolding commit, or a test harness commit)
    spawns a detached `git maintenance run --auto` in the same repo, which can transiently
    hold the index — retry instead of failing an otherwise-green onboard on that race. Shares the
    generalized retry seam with worktree mutations (bh-i6o7); `run` is passed so this module's
    patched subprocess seam is honored."""
    return retry_on_index_lock(
        run, args, cwd=str(base), check=False, capture=True,
        retries=_LOCK_RETRIES, sleep=_LOCK_RETRY_SLEEP,
    )


def _remove_stealth_exclude(base=None) -> bool:
    """Drop bd init's stealth / fork-protection block (marker comment + every pattern it wrote)
    from .git/info/exclude, keeping every other entry. Handles both bd-version shapes (bh-2w8d):
    bd ≤1.0.5's `# Beads stealth mode` + `.beads/` and bd ≥1.1.0's `# Beads fork protection
    (bd init)` + `.beads/` + `**/RECOVERY*.md` + `**/SESSION*.md`. Returns True when something was
    removed."""
    base = _base(base)
    exclude = base / ".git/info/exclude"
    if not exclude.exists():
        return False
    lines = exclude.read_text().splitlines()
    kept = [
        ln for ln in lines
        if ln.strip() not in _STEALTH_MARKERS
        and not ln.startswith(_STEALTH_COMMENT_PREFIXES)
    ]
    if kept == lines:
        return False
    exclude.write_text("\n".join(kept) + ("\n" if kept else ""))
    return True


def _ensure_stealth_exclude(base=None) -> bool:
    """Inverse of `_remove_stealth_exclude`: make sure `.beads/` is stealth-excluded via
    .git/info/exclude (the zero-footprint convention). Writes bd's own ≥1.1.0 block shape so
    `_remove_stealth_exclude` strips it cleanly on a later furnish. Returns True when the
    block was written (False = already excluded)."""
    base = _base(base)
    probe = run(["git", "check-ignore", "-q", ".beads"], cwd=str(base), check=False)
    if getattr(probe, "returncode", 1) == 0:
        return False
    exclude = base / ".git/info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    sep = "" if not existing or existing.endswith("\n") else "\n"
    exclude.write_text(existing + sep + "\n# Beads fork protection (bd init)\n.beads/\n")
    return True


def _relocate_bd_gitignore(base=None) -> bool:
    """Move bd init's Beads/Dolt block from the tracked root .gitignore into .git/info/exclude.

    `bd init --setup-exclude` keeps `.beads/` out of the index but still appends its
    dolt-artifact patterns to .gitignore — a tracked mutation zero-footprint must not leave
    behind. The block (marker line through the next blank line / EOF) moves verbatim to the
    host-local exclude; a .gitignore bd created outright is deleted. True when relocated."""
    base = _base(base)
    gi = base / ".gitignore"
    if not gi.exists():
        return False
    lines = gi.read_text().splitlines()
    if _BD_GITIGNORE_MARKER not in lines:
        return False
    start = lines.index(_BD_GITIGNORE_MARKER)
    end = start + 1
    while end < len(lines) and lines[end].strip():
        end += 1
    block, kept = lines[start:end], lines[:start] + lines[end:]
    while kept and not kept[-1].strip():
        kept.pop()
    if kept:
        gi.write_text("\n".join(kept) + "\n")
    else:
        gi.unlink()
    exclude = base / ".git/info/exclude"
    exclude.parent.mkdir(parents=True, exist_ok=True)
    existing = exclude.read_text() if exclude.exists() else ""
    sep = "" if not existing or existing.endswith("\n") else "\n"
    exclude.write_text(existing + sep + "\n" + "\n".join(block) + "\n")
    return True


def _history_has_scaffold(base: Path) -> bool:
    """True when a scaffold commit already exists anywhere in history — the repair signal:
    a later pass adds furniture the original scaffolding missed."""
    res = run(
        ["git", "log", "-n", "1", "--format=%H", "-F", "--grep", _SCAFFOLD_COMMIT_MSG],
        cwd=str(base), check=False, capture=True,
    )
    return res.returncode == 0 and bool((res.stdout or "").strip())


def _head_is_unpushed_scaffold(base: Path) -> bool:
    """True when HEAD is a scaffold commit that exists on no remote ref — safe to amend."""
    subject = run(
        ["git", "log", "-1", "--format=%s"], cwd=str(base), check=False, capture=True
    )
    if subject.returncode != 0 or (subject.stdout or "").strip() not in (
        _SCAFFOLD_COMMIT_MSG, _SCAFFOLD_REPAIR_MSG,
    ):
        return False
    on_remote = run(
        ["git", "branch", "-r", "--contains", "HEAD"], cwd=str(base), check=False, capture=True
    )
    return on_remote.returncode == 0 and not (on_remote.stdout or "").strip()


def _commit_scaffolding(base=None) -> bool:
    """Stage + commit the onboarding artifacts (furnished-rig convention).

    Only known artifact paths are staged; git's ignore rules still apply (a path a repo
    deliberately gitignores is skipped via check-ignore rather than force-added), so
    settings.local.json and bd's local-only .beads files never land in the commit.
    Re-runs never litter duplicate identically-titled commits: an unpushed scaffold commit at
    HEAD is amended in place, and a genuine repair pass (a scaffold commit already exists in
    history) commits under a distinct subject.
    Returns True when a commit was created/amended (False = tree already clean)."""
    base = _base(base)
    paths = [
        p for p in _SCAFFOLD_PATHS
        if (base / p).exists()
        and run(["git", "check-ignore", "-q", p], cwd=str(base), check=False).returncode != 0
    ]
    if not paths:
        return False
    _git_locked_run(["git", "add", "--", *paths], base)
    if run(["git", "diff", "--cached", "--quiet"], cwd=str(base), check=False).returncode == 0:
        return False
    if _head_is_unpushed_scaffold(base):
        commit_args = ["git", "commit", "-q", "--amend", "--no-edit"]
    else:
        msg = _SCAFFOLD_REPAIR_MSG if _history_has_scaffold(base) else _SCAFFOLD_COMMIT_MSG
        commit_args = ["git", "commit", "-q", "-m", msg]
    committed = _git_locked_run(commit_args, base)
    if committed.returncode != 0:
        # Best-effort: a late commit failure (missing identity, hook) must not fail an
        # otherwise-green onboard — the staged scaffolding stays for the operator.
        typer.echo(
            "⚠ scaffold: could not commit hive scaffolding — left staged "
            "(commit it to match the furnished-hive convention).",
            err=True,
        )
        return False
    return True


def _install_sandbox_grant(cfg, provider: str, org: str, repo: str, base=None) -> None:
    # Ephemeral worktrees live in the (already sandbox-writable) OS temp dir — no grant to
    # write. Grants are a persistent-mode (ephemeral=false) feature.
    if config.worktrees_ephemeral(cfg):
        typer.echo("✓ --claude: ephemeral worktrees (OS temp) — no sandbox grant needed")
        return
    base = _base(base)
    (base / ".claude").mkdir(exist_ok=True)
    f = base / ".claude/settings.local.json"
    existing = json.loads(f.read_text()) if f.exists() else {}
    subtree = _sandbox_subtree(cfg, provider, org, repo)
    merged = _merge_sandbox_grant(existing, subtree, f"{provider}/{org}/{repo}")
    f.write_text(json.dumps(merged, indent=2) + "\n")
    _git_exclude(".claude/settings.local.json", base)
    typer.echo(f"✓ --claude: sandbox grant → .claude/settings.local.json ({subtree})")


def granted_subtree(clone: Path, provider: str, org: str, repo: str) -> str | None:
    """The grant entry for this rig in `clone`'s settings.local.json, or None if absent.
    Used by `ws doctor` to detect a stale grant after worktrees_root() moves."""
    f = clone / ".claude" / "settings.local.json"
    if not f.exists():
        return None
    try:
        data = json.loads(f.read_text())
    except (OSError, ValueError):
        return None
    items = (((data.get("sandbox") or {}).get("filesystem") or {}).get("allowWrite")) or []
    suffix = f"{provider}/{org}/{repo}"
    return next((x for x in items if _matches_hive(x, suffix)), None)


def grant_is_current(cfg, clone: Path, provider: str, org: str, repo: str):
    """None = no grant; True = matches current root; False = stale (rig moved root)."""
    granted = granted_subtree(clone, provider, org, repo)
    if granted is None:
        return None
    want = _sandbox_subtree(cfg, provider, org, repo)
    return os.path.realpath(os.path.expanduser(granted)) == os.path.realpath(
        os.path.expanduser(want)
    )


def _parse_triplet(hive_id: str):
    """Split a `provider/org/repo` triplet, or abort with a clear error. Registry-only:
    the repo need not be cloned, so we never touch the filesystem here."""
    parts = hive_id.split("/")
    if len(parts) != 3 or not all(parts):
        typer.echo(f"✗ expected a provider/org/repo triplet, got '{hive_id}'", err=True)
        raise typer.Exit(1)
    return parts[0], parts[1], parts[2]


def add(hive_id, prefix="", kind="", upstream=""):
    """Register a rig from a provider/org/repo triplet — registry-only, no cwd required and
    no `bd init` (the repo may be uncloned). Mirrors `registry.register` scope."""
    provider, org, repo = _parse_triplet(hive_id)
    cfg = config.load()
    if not prefix:
        prefix, warns = registry.derive_prefix(provider, org, repo, kind, cfg)
        for w in warns:
            typer.echo(w, err=True)
    registry.register(provider, org, repo, prefix, kind, upstream)


def rm(hive_id):
    """Unregister a rig by id (per `rig_match`) — registry-scoped only: resolve → drop the
    managed_repos entry → save. Does NOT touch .beads/labels/the repo."""
    entry = registry.resolve_hive(config.load(), hive_id)
    registry.unregister(str(entry["provider"]), str(entry["org"]), str(entry["repo"]))


def _run_onboard(ctx, dry_run: bool, skip_check: str) -> None:
    """Build the onboarding DAG for ``ctx`` and run the two-phase executor.

    Shared tail of ``onboard``/``init``: assemble the steps, parse ``--skip-check`` into ids,
    run the preflight gate + topological execution, then print the ready line (non-dry-run)."""
    from . import onboard as _ob

    ctx.steps = _ob.build_steps(ctx)
    skips = [s.strip() for s in skip_check.split(",") if s.strip()] if skip_check else []
    _ob.run_onboard(ctx, dry_run=dry_run, skip_checks=skips)
    if not dry_run:
        typer.echo(f"✓ hive '{ctx.prefix}' ready ({ctx.kind}).")


def onboard(
    hive_id, clone_url="", furnish=None, claude=False, skills=False, observaloop=False,
    agents=False, plugins=None, force=False, kind="", prefix="", yes=False, dry_run=False,
    skip_check="",
):
    """End-to-end onboard a rig from a local folder or a remote repo — a thin wrapper that builds
    the onboarding ``Ctx`` and calls ``onboard.run_onboard``.

    Resolves target = workspace_root()/provider/org/repo. The two-phase runner clones it down
    (when absent + --clone-url) inside its Phase-A preflight gate, runs the enabled steps in
    topological order, and syncs the hub last. Threading cwd=target (not os.chdir) lets one verb
    stand a rig up wherever it lives on disk. ``--dry-run`` lists every check id and mutates
    nothing; ``--skip-check`` downgrades an overridable failure (e.g. dirty-tree) to a warning."""
    from . import onboard as _ob

    provider, org, repo = _parse_triplet(hive_id)
    target = Path(workspace_root()) / provider / org / repo
    ctx = _ob.Ctx(
        hive=f"{provider}/{org}/{repo}", target=str(target),
        provider=provider, org=org, repo=repo, clone_url=clone_url, cwd=str(target),
        cfg=config.load(), furnish=furnish, claude=claude, skills=skills,
        observaloop=observaloop, agents=agents, plugins=plugins or [], force=force, yes=yes,
        kind=kind, prefix=prefix, do_hub_sync=True,
    )
    _run_onboard(ctx, dry_run, skip_check)


# ---- discover: registerable repos (rig ls --available) ----------------------
# Phase 1 of: surface candidate repos to register without making the
# operator type provider/org/repo triplets blind. Pure reuse — no new deps/auth/live API.
# ponytail: Phase 2 (live `gh repo list <org>` / `git workspace fetch`-backed listing of
# repos not yet in the lock file) is a tracked follow-up, to be gated behind a flag.


def available(cfg=None) -> dict:
    """Structured core for `rig ls --available` + the `rigs_available` MCP tool.

    Diffs git-workspace's tracked repos (read from `workspace-lock.toml` — already fetched,
    ZERO API calls; see `gitworkspace.tracked_repos`) against the registered `managed_repos`.
    Returns ``{"candidates": [...], "registered": [...]}`` — each a sorted list of
    `<group>/org/repo` triplets (the first segment is the repo-group path, not necessarily the
    provider TYPE; see gitworkspace.RepoGroup). `candidates` are tracked repos NOT yet registered
    as rigs (the ones you could `ws rig add`); `registered` are the rigs already in the registry.
    """
    from . import gitworkspace

    cfg = cfg if cfg is not None else config.load()
    registered = {f"{e['provider']}/{e['org']}/{e['repo']}" for e in cfg.get("managed_repos", [])}
    tracked = {f"{group}/{org}/{repo}" for (group, org, repo) in gitworkspace.tracked_repos(cfg)}
    return {
        "candidates": sorted(tracked - registered),
        "registered": sorted(registered),
    }


def ls(show_available: bool = False) -> None:
    """CLI: list rigs. Default lists registered rigs; `--available` lists discoverable-but-
    unregistered candidate repos (from the lock file). Both views share `available()`'s core."""
    result = available()
    if show_available:
        rows = result["candidates"]
        if not rows:
            typer.echo("# No unregistered repos — every tracked repo is already a hive.")
            return
        typer.echo(
            f"# Available to register ({len(rows)}) — "
            f"run '{config.BINARY_ALIAS} hive add <provider/org/repo>'"
        )
    else:
        rows = result["registered"]
        if not rows:
            typer.echo("# No registered hives.")
            return
        typer.echo(f"# Registered hives ({len(rows)})")
    for row in rows:
        typer.echo(f"  {row}")


def init(
    furnish=None, claude=False, skills=False, observaloop=False, agents=False, plugins=None,
    force=False, kind="", prefix="", yes=False, dry_run=False, skip_check="", cwd=None,
):
    """Onboard the current (already-local) repo as a rig — a thin wrapper that builds the
    onboarding ``Ctx`` and calls ``onboard.run_onboard`` (no clone, no hub sync).

    ``cwd`` is the target rig dir (None = process cwd). Threaded — not os.chdir — so ``onboard``
    can run against a freshly cloned/local repo elsewhere on disk: identity is derived with cwd=,
    bd init runs there, and every file installer writes under that base. All preserve/reconfigure
    behavior, the fork/prefix-policy gates, and the installer dispatch now live in the onboard
    step table (``onboard.build_steps``); ``--dry-run`` lists every check id and mutates nothing;
    ``--skip-check`` downgrades an overridable failure to a warning."""
    from . import onboard as _ob

    ident = workspace_identity(cwd=cwd)
    if ident is None:
        typer.echo("not in a git repo under $GIT_WORKSPACE", err=True)
        raise typer.Exit(1)
    provider, org, repo = ident
    target = str(_base(cwd).resolve())
    ctx = _ob.Ctx(
        hive=f"{provider}/{org}/{repo}", target=target,
        provider=provider, org=org, repo=repo, cwd=cwd, cfg=config.load(),
        furnish=furnish, claude=claude, skills=skills, observaloop=observaloop, agents=agents,
        plugins=plugins or [], force=force, yes=yes, kind=kind, prefix=prefix, do_hub_sync=False,
    )
    _run_onboard(ctx, dry_run, skip_check)
