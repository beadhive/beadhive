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
from .identity import workspace_identity
from .run import run


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


def _install_prime_md(force=False):
    Path(".beads").mkdir(exist_ok=True)
    dst = Path(".beads/PRIME.md")
    if dst.exists() and not force:
        typer.echo("• --prime: .beads/PRIME.md exists — skipped (use -f to overwrite)")
        return
    shutil.copy(config.asset("PRIME.md"), dst)
    typer.echo("✓ --prime: .beads/PRIME.md installed")


def _install_skills(force=False):
    """Copy bundled skills into ./skills, per-skill. Skip those already present unless force."""
    src = config.skills_src()
    dst = Path("skills")
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


def _link_skills_claude(force=False):
    """Symlink .claude/skills -> ../skills so Claude Code discovers them on launch."""
    Path(".claude").mkdir(exist_ok=True)
    link = Path(".claude/skills")
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


def _install_claude_settings():
    Path(".claude").mkdir(exist_ok=True)
    addon = json.loads(config.asset("claude-settings.json").read_text())
    settings = Path(".claude/settings.json")
    merged = _deep_merge(json.loads(settings.read_text()), addon) if settings.exists() else addon
    settings.write_text(json.dumps(merged, indent=2) + "\n")
    typer.echo("✓ --claude: .claude/settings.json (SessionStart hook + bd-remember deny)")


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


def _git_exclude(rel: str) -> None:
    # ponytail: best-effort — keep the host-local settings file out of `git status` for rigs
    # that don't already ignore .claude/. Local .git/info/exclude, never the tracked .gitignore.
    if not Path(".git").is_dir():
        return
    exclude = Path(".git/info/exclude")
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if rel not in lines:
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with exclude.open("a") as fh:
            fh.write(rel + "\n")


def _install_sandbox_grant(cfg, provider: str, org: str, repo: str) -> None:
    # Ephemeral worktrees live in the (already sandbox-writable) OS temp dir — no grant to
    # write. Grants are a persistent-mode (ephemeral=false) feature.
    if config.worktrees_ephemeral(cfg):
        typer.echo("✓ --claude: ephemeral worktrees (OS temp) — no sandbox grant needed")
        return
    Path(".claude").mkdir(exist_ok=True)
    f = Path(".claude/settings.local.json")
    existing = json.loads(f.read_text()) if f.exists() else {}
    subtree = _sandbox_subtree(cfg, provider, org, repo)
    merged = _merge_sandbox_grant(existing, subtree, f"{provider}/{org}/{repo}")
    f.write_text(json.dumps(merged, indent=2) + "\n")
    _git_exclude(".claude/settings.local.json")
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


def init(
    prime=False, claude=False, skills=False, force=False,
    kind="", prefix="", yes=False, dry_run=False,
):
    ident = workspace_identity()
    if ident is None:
        typer.echo("not in a git repo under $GIT_WORKSPACE", err=True)
        raise typer.Exit(1)
    provider, org, repo = ident

    cfg = config.load()
    cls = registry.classify(provider, org, repo, cfg)
    upstream = ""
    if cls == "excluded":
        typer.echo(f"✗ {provider}/{org}/{repo} is excluded by the registry — refusing.", err=True)
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

    # required-org prefix policy is an invariant at registration — always enforced.
    if registry.org_policy(cfg, org) == "required":
        code = registry.org_code(cfg, org)
        if not prefix.startswith(f"{code}-"):
            typer.echo(
                f"✗ prefix '{prefix}' violates required-org policy (expected {code}-*)", err=True
            )
            raise typer.Exit(1)

    typer.echo(f"rig: {provider}/{org}/{repo}")
    detail = f"  kind={kind}  prefix={prefix}  prime={prime}  claude={claude}  skills={skills}"
    typer.echo(detail + (f"  upstream={upstream}" if upstream else ""))
    if dry_run:
        typer.echo("(dry-run — nothing changed)")
        return

    if Path(".beads").exists():
        # ponytail: already-initialized beads; skip bd init so re-runs (e.g. to add
        # --skills) are idempotent instead of aborting on the existing Dolt DB.
        typer.echo("ℹ beads already initialized — skipping bd init.")
    else:
        env = dict(os.environ, BD_NON_INTERACTIVE="1")
        bd_init = ["bd", "init", "--prefix", prefix, "--skip-agents", "--skip-hooks"]
        run(bd_init + ["--non-interactive"], env=env)
    registry.register(provider, org, repo, prefix, kind, upstream)
    if prime:
        _install_prime_md(force)
    if claude:
        _install_claude_settings()
        _install_sandbox_grant(cfg, provider, org, repo)
    if skills:
        _install_skills(force)
        if claude:
            _link_skills_claude(force)
    typer.echo(f"✓ rig '{prefix}' ready ({kind}).")
