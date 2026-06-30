"""Onboard the current repo as a beads rig. Ports scripts/rig-init.sh.

classify → resolve kind → fork gate → derive/override prefix → enforce required-org
policy → bd init → register → optional `prime` agent integration.
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import typer

from . import config, registry
from .identity import workspace_identity, workspace_root
from .run import run


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


def _install_prime_md(force=False, base=None):
    base = _base(base)
    (base / ".beads").mkdir(exist_ok=True)
    dst = base / ".beads/PRIME.md"
    if dst.exists() and not force:
        typer.echo("• --prime: .beads/PRIME.md exists — skipped (use -f to overwrite)")
        return
    shutil.copy(config.asset("PRIME.md"), dst)
    typer.echo("✓ --prime: .beads/PRIME.md installed")


# ---- AGF hint stanza (AGENTS.md / CLAUDE.md) --------------------------------
# A small managed block pointing agent harnesses at `ws rig ready` + .beads/PRIME.md, so a
# harness that reads AGENTS.md (Codex/others) or CLAUDE.md — but not the SessionStart bd-prime
# hook — can still answer "is this repo set up for AGF?". Non-destructive: we only ever write
# our own marked block, never rewrite the user's surrounding content.

_AGF_MARK_START = "<!-- ws:agf:start"
_AGF_MARK_END = "<!-- ws:agf:end -->"


def _replace_agf_block(text: str, block: str) -> str:
    start = text.index(_AGF_MARK_START)
    end = text.index(_AGF_MARK_END, start) + len(_AGF_MARK_END)
    return text[:start] + block + text[end:]


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
# Grafana dashboard, following the `if prime:`/`if claude:` installer pattern. Every step is
# best-effort — the ws.observaloop wrappers no-op (warn + None) when observaloop / docker is
# absent, so absence degrades to a warning + continue, never an abort.


def _load_observaloop_dashboard() -> dict:
    """Parse the ws-shipped Grafana dashboard model (assets/observaloop/ws-dashboard.json)."""
    return json.loads(config.observaloop_dashboard_asset().read_text())


def _load_observaloop_metrics_preset() -> dict:
    """Parse the ws-shipped CLI-metrics collector preset (cli-metrics-preset.yaml).

    YAML, parsed with the repo's ruamel parser (pyyaml is not a dependency); a plain dict of
    ``processors`` + ``metrics_pipeline_processors`` the observaloop adapter merges into the
    profile collector's metrics pipeline."""
    from ruamel.yaml import YAML

    return YAML(typ="safe").load(config.observaloop_metrics_preset_asset().read_text())


def _install_observaloop(cfg, entry: dict) -> None:
    """Ensure+up this rig's observaloop profile, apply the CLI-metrics collector preset, then the
    ws Grafana telemetry dashboard.

    Gating, in order, each a warn-and-continue (rig init still succeeds):
      * ``otel.enabled`` false — observaloop needs otel to receive anything; warn but still
        ensure the profile so a later `otel.enabled: true` flip just works.
      * no derivable profile name (unregistered prefix) — nothing to create; return.
      * ``observaloop.is_available()`` false (observaloop/docker absent or unreachable) — skip
        the profile + preset + dashboard.
      * preset apply (collector reshape) runs right after up — it reshapes the profile collector's
        metrics pipeline (strip_instance + promote_ws_attrs + deltatocumulative) so short-lived ws
        CLI metrics accumulate with ws.* labels. It needs only the collector (not Grafana), so it
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


def _matches_rig(entry: str, triplet_suffix: str) -> bool:
    """True if a grant entry is THIS rig's subtree (under any root) — the relocation key."""
    p = os.path.expanduser(str(entry)).rstrip("/")
    return p.endswith("/" + triplet_suffix) or p == triplet_suffix


def _replace_for_rig(items, subtree: str, triplet_suffix: str) -> list:
    """Drop any prior entry for this rig (stale root after a move), then append the current
    subtree. Self-healing AND idempotent — re-running rewrites instead of accumulating."""
    kept = [x for x in (items or []) if not _matches_rig(x, triplet_suffix)]
    kept.append(subtree)
    return kept


def _merge_sandbox_grant(existing: dict, subtree: str, triplet_suffix: str) -> dict:
    """Immutable: return a new settings dict granting `subtree` write in both the bash
    sandbox (sandbox.filesystem.allowWrite) and the tool layer (permissions array)."""
    out = json.loads(json.dumps(existing or {}))  # deep copy — never mutate the caller's dict
    fs = out.setdefault("sandbox", {}).setdefault("filesystem", {})
    fs["allowWrite"] = _replace_for_rig(fs.get("allowWrite"), subtree, triplet_suffix)
    perms = out.setdefault("permissions", {})
    perms["additionalDirectories"] = _replace_for_rig(
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
    return next((x for x in items if _matches_rig(x, suffix)), None)


def grant_is_current(cfg, clone: Path, provider: str, org: str, repo: str):
    """None = no grant; True = matches current root; False = stale (rig moved root)."""
    granted = granted_subtree(clone, provider, org, repo)
    if granted is None:
        return None
    want = _sandbox_subtree(cfg, provider, org, repo)
    return os.path.realpath(os.path.expanduser(granted)) == os.path.realpath(
        os.path.expanduser(want)
    )


def _parse_triplet(rig_id: str):
    """Split a `provider/org/repo` triplet, or abort with a clear error. Registry-only:
    the repo need not be cloned, so we never touch the filesystem here."""
    parts = rig_id.split("/")
    if len(parts) != 3 or not all(parts):
        typer.echo(f"✗ expected a provider/org/repo triplet, got '{rig_id}'", err=True)
        raise typer.Exit(1)
    return parts[0], parts[1], parts[2]


def add(rig_id, prefix="", kind="", upstream=""):
    """Register a rig from a provider/org/repo triplet — registry-only, no cwd required and
    no `bd init` (the repo may be uncloned). Mirrors `registry.register` scope."""
    provider, org, repo = _parse_triplet(rig_id)
    cfg = config.load()
    if not prefix:
        prefix, warns = registry.derive_prefix(provider, org, repo, kind, cfg)
        for w in warns:
            typer.echo(w, err=True)
    registry.register(provider, org, repo, prefix, kind, upstream)


def rm(rig_id):
    """Unregister a rig by id (per `rig_match`) — registry-scoped only: resolve → drop the
    managed_repos entry → save. Does NOT touch .beads/labels/the repo."""
    entry = registry.resolve_rig(config.load(), rig_id)
    registry.unregister(str(entry["provider"]), str(entry["org"]), str(entry["repo"]))


def onboard(
    rig_id, clone_url="", prime=False, claude=False, skills=False, observaloop=False,
    agents=False, force=False, kind="", prefix="", yes=False,
):
    """End-to-end onboard a rig from a local folder or a remote repo, converging the two paths:
    resolve target = workspace_root()/provider/org/repo; if it's absent and `--clone-url` is
    given, `git clone` it down; run the full `rig init` logic with cwd=target; then sync the hub.

    Threading cwd=target (rather than os.chdir) lets one verb stand a rig up wherever it lives on
    disk — an already-cloned folder onboards with no clone, a remote one clones first."""
    from . import hub

    provider, org, repo = _parse_triplet(rig_id)
    target = Path(workspace_root()) / provider / org / repo
    if not target.exists():
        if not clone_url:
            typer.echo(
                f"✗ {target} does not exist — pass --clone-url to clone it down first.", err=True
            )
            raise typer.Exit(1)
        target.parent.mkdir(parents=True, exist_ok=True)
        typer.echo(f"• cloning {clone_url} → {target}")
        run(["git", "clone", clone_url, str(target)])
    else:
        typer.echo(f"• onboarding existing folder {target}")

    init(
        prime=prime, claude=claude, skills=skills, observaloop=observaloop, agents=agents,
        force=force, kind=kind, prefix=prefix, yes=yes, cwd=str(target),
    )
    hub.sync()


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
    `provider/org/repo` triplets. `candidates` are tracked repos NOT yet registered as rigs
    (the ones you could `ws rig add`); `registered` are the rigs already in the registry.
    """
    from . import gitworkspace

    cfg = cfg if cfg is not None else config.load()
    registered = {f"{e['provider']}/{e['org']}/{e['repo']}" for e in cfg.get("managed_repos", [])}
    tracked = {f"{p}/{o}/{r}" for (p, o, r) in gitworkspace.tracked_repos(cfg)}
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
            typer.echo("# No unregistered repos — every tracked repo is already a rig.")
            return
        typer.echo(f"# Available to register ({len(rows)}) — run 'ws rig add <provider/org/repo>'")
    else:
        rows = result["registered"]
        if not rows:
            typer.echo("# No registered rigs.")
            return
        typer.echo(f"# Registered rigs ({len(rows)})")
    for row in rows:
        typer.echo(f"  {row}")


def init(
    prime=False, claude=False, skills=False, observaloop=False, agents=False, force=False,
    kind="", prefix="", yes=False, dry_run=False, cwd=None,
):
    # `cwd` is the target rig dir (None = process cwd). Threaded — not os.chdir — so `onboard`
    # can run the full init against a freshly cloned/local repo elsewhere on disk: identity is
    # derived with cwd=, bd init runs there, and every file installer writes under `base`.
    base = _base(cwd)
    ident = workspace_identity(cwd=cwd)
    if ident is None:
        typer.echo("not in a git repo under $GIT_WORKSPACE", err=True)
        raise typer.Exit(1)
    provider, org, repo = ident

    cfg = config.load()
    # Non-destructive re-init: an existing managed_repos entry means this rig is already
    # configured. Whether we may rewrite its settings is gated below — `--force` re-registers
    # from scratch (as a fresh init would), a targeted `--prefix`/`--kind` changes only that
    # field, and a plain re-init preserves everything (the regression that clobbered a working
    # prefix → workspace and invalidated every label rig-wide).
    existing = registry.find_entry(cfg, provider, org, repo)
    prefix_override = bool(prefix)
    kind_override = bool(kind)
    upstream = ""

    if existing is not None and not force:
        # Already configured, no full re-init requested: start from the recorded entry and
        # apply ONLY explicit overrides. Skip classification + the fork opt-in gate so a
        # re-run (e.g. to add --skills) on a tracked fork never re-trips it or re-derives —
        # and never silently clobbers the registered prefix/kind/upstream.
        prefix = prefix or str(existing["prefix"])
        kind = kind or str(existing["kind"])
        upstream = str(existing.get("upstream", "") or "")
        # Mismatch diagnostic: the preserve path bypasses derivation, so a
        # user who expected `rig init` to "fix" the prefix gets no signal that auto-derivation
        # WOULD produce a different value. When the user passed no --prefix, recompute what the
        # bypassed derivation would yield (cheap, no `gh` — reuse the registered kind) and, if
        # it differs from the registered prefix we're keeping, name both + the override.
        if not prefix_override:
            derived, _ = registry.derive_prefix(provider, org, repo, kind, cfg)
            if derived != prefix:
                typer.echo(
                    f"note: derived prefix '{derived}' differs from the registered prefix "
                    f"'{prefix}' — keeping the registered one (use --prefix to change it)",
                    err=True,
                )
    else:
        # Fresh rig, or --force: classify + derive from scratch (original behavior).
        cls = registry.classify(provider, org, repo, cfg)
        if cls == "excluded":
            typer.echo(
                f"✗ {provider}/{org}/{repo} is excluded by the registry — refusing.", err=True
            )
            raise typer.Exit(1)
        elif cls == "org-native":
            kind = kind or "org-native"
        elif cls.startswith("fork upstream="):
            upstream = cls[len("fork upstream=") :]
            kind = kind or "fork"
        else:  # personal-or-prototype
            kind = kind or "prototype"

        if kind == "fork" and not yes:
            suffix = f" of {upstream}" if upstream else ""
            typer.echo(f"ℹ {provider}/{org}/{repo} is a fork{suffix} — beads is OFF by default.")
            typer.echo("  To track it anyway: ws rig init --kind fork --yes")
            raise typer.Exit(0)

        if not prefix:
            prefix, warns = registry.derive_prefix(provider, org, repo, kind, cfg)
            for w in warns:
                typer.echo(w, err=True)

    # Heads-up under --force: --force re-derives and WILL replace the
    # registered prefix. If the user passed no --prefix and the freshly derived value differs
    # from what was registered, surface the change rather than swapping it silently.
    if existing is not None and force and not prefix_override and str(existing["prefix"]) != prefix:
        typer.echo(
            f"note: derived prefix '{prefix}' differs from the registered prefix "
            f"'{existing['prefix']}' — re-registering as '{prefix}' (--force).",
            err=True,
        )

    # Re-register only for a fresh rig, an explicit --force, or a targeted --prefix/--kind
    # override; otherwise leave the registered settings untouched.
    reconfigure = existing is None or force or prefix_override or kind_override

    # required-org prefix policy is an invariant at registration — always enforced.
    if registry.org_policy(cfg, org) == "required":
        code = registry.org_code(cfg, org)
        if not prefix.startswith(f"{code}-"):
            typer.echo(
                f"✗ prefix '{prefix}' violates required-org policy (expected {code}-*)", err=True
            )
            raise typer.Exit(1)

    typer.echo(f"rig: {provider}/{org}/{repo}")
    detail = (
        f"  kind={kind}  prefix={prefix}  prime={prime}  claude={claude}  "
        f"skills={skills}  observaloop={observaloop}  agents={agents}"
    )
    typer.echo(detail + (f"  upstream={upstream}" if upstream else ""))
    if dry_run:
        typer.echo("(dry-run — nothing changed)")
        return

    if (base / ".beads").exists():
        # ponytail: already-initialized beads; skip bd init so re-runs (e.g. to add
        # --skills) are idempotent instead of aborting on the existing Dolt DB.
        typer.echo("ℹ beads already initialized — skipping bd init.")
    else:
        env = dict(os.environ, BD_NON_INTERACTIVE="1")
        bd_init = ["bd", "init", "--prefix", prefix, "--skip-agents", "--skip-hooks"]
        run(bd_init + ["--non-interactive"], env=env, cwd=cwd)
    if reconfigure:
        registry.register(provider, org, repo, prefix, kind, upstream)
    else:
        # Already configured and no intentional change requested — preserve the registry
        # entry untouched and warn, listing what already exists.
        typer.echo(
            f"ℹ rig already configured: prefix '{prefix}' (kind={kind})"
            + (f", upstream {upstream}" if upstream else "")
            + " — settings preserved (use --force to re-register, or --prefix <p> to change "
            "just the prefix).",
            err=True,
        )
    if prime:
        _install_prime_md(force, base)
    if claude:
        _install_claude_settings(base)
        _install_sandbox_grant(cfg, provider, org, repo, base)
        _ensure_agf_hint(base / "CLAUDE.md", force, "--claude")
    if agents:
        _ensure_agf_hint(base / "AGENTS.md", force, "--agents")
    if skills:
        _install_skills(force, base)
        if claude:
            _link_skills_claude(force, base)
    if observaloop:
        # Best-effort, fully isolated: an unexpected failure anywhere in the observaloop wiring
        # must never abort `rig init`, so the whole installer is fenced behind try/except on top
        # of each wrapper already being a no-op on absence.
        try:
            _install_observaloop(cfg, {"prefix": prefix})
        except Exception as exc:  # pragma: no cover - defensive: wrappers never raise
            typer.echo(f"• --observaloop: skipped ({exc}) — rig init continues.", err=True)
    typer.echo(f"✓ rig '{prefix}' ready ({kind}).")
