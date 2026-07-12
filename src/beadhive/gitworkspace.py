"""Optional integration with orf/git-workspace.

When `git_workspace.enabled` is set, ws reads providers and orgs from the
git-workspace config (`$GIT_WORKSPACE/workspace*.toml`) so they don't have to be
restated in ws's own config. Each `[[provider]]` declares `provider` (type),
`name` (the org/user), and `path` (the dir segment ws derives `provider:` from).
"""

from __future__ import annotations

import tomllib
from glob import glob
from pathlib import Path

from .identity import workspace_root


def enabled(cfg) -> bool:
    return bool((cfg.get("git_workspace") or {}).get("enabled", False))


def config_paths(cfg) -> list[Path]:
    """The workspace*.toml file(s): explicit `path`, else glob the workspace root."""
    explicit = (cfg.get("git_workspace") or {}).get("path")
    if explicit:
        p = Path(explicit).expanduser()
        return [p] if p.exists() else []
    # workspace.toml + split workspace-*.toml configs, but NOT workspace-lock.toml
    found = sorted(glob(f"{workspace_root()}/workspace*.toml"))
    return [Path(p) for p in found if not p.endswith("-lock.toml")]


def _provider_entries(cfg):
    for path in config_paths(cfg):
        try:
            data = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError):
            continue
        yield from data.get("provider", [])


def providers(cfg) -> set[str]:
    """Provider labels = the dir segment (`path`) each provider clones into."""
    return {
        e.get("path") or e.get("provider")
        for e in _provider_entries(cfg)
        if e.get("path") or e.get("provider")
    }


def provider_host(cfg, path: str) -> str:
    """The real host (provider TYPE, e.g. 'github') behind a workspace `path` segment. `providers()`
    flattens `path or provider` into one set and loses this mapping, so a `path='contrib'
    provider='github'` block never reached the github fork probe (bh-rax6). '' when unknown."""
    for e in _provider_entries(cfg):
        if (e.get("path") or e.get("provider")) == path:
            return e.get("provider") or e.get("path") or ""
    return ""


def url_slug(url: str) -> str:
    """`owner/repo` from a git remote URL (scp `git@host:owner/repo`, ssh/https `…/owner/repo`),
    trailing `.git` stripped; '' if unparseable."""
    u = (url or "").strip().removesuffix(".git")
    if not u:
        return ""
    tail = u.split("://", 1)[-1] if "://" in u else u.split(":", 1)[-1]
    parts = [p for p in tail.split("/") if p]
    return f"{parts[-2]}/{parts[-1]}" if len(parts) >= 2 else ""


def upstreams(cfg) -> dict[str, str]:
    """'provider/org/repo' -> upstream `owner/repo` slug, from workspace-lock.toml
    `[[repo]].upstream` (a fork's recorded parent). The OFFLINE fork signal — no gh/network
    needed, and it survives a path!=host provider label (bh-rax6)."""
    out: dict[str, str] = {}
    lock = Path(workspace_root()) / "workspace-lock.toml"
    if not lock.exists():
        return out
    try:
        data = tomllib.loads(lock.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return out
    for repo in data.get("repo", []):
        parts = (repo.get("path") or "").split("/")
        slug = url_slug(repo.get("upstream") or "")
        if len(parts) >= 3 and slug:
            out[f"{parts[0]}/{parts[1]}/{parts[-1]}"] = slug
    return out


def orgs(cfg) -> set[str]:
    return {e["name"] for e in _provider_entries(cfg) if e.get("name")}


def repo_urls(cfg):
    """'provider/org/repo' -> clone URL, from workspace-lock.toml `[[repo]]`."""
    out = {}
    lock = Path(workspace_root()) / "workspace-lock.toml"
    if not lock.exists():
        return out
    try:
        data = tomllib.loads(lock.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return out
    for repo in data.get("repo", []):
        parts = (repo.get("path") or "").split("/")
        if len(parts) >= 3 and repo.get("url"):
            out[f"{parts[0]}/{parts[1]}/{parts[-1]}"] = repo["url"]
    return out


def tracked_repos(cfg):
    """(provider, org, repo) tuples from workspace-lock.toml `[[repo]].path`."""
    out = []
    lock = Path(workspace_root()) / "workspace-lock.toml"
    if not lock.exists():
        return out
    try:
        data = tomllib.loads(lock.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return out
    for repo in data.get("repo", []):
        parts = (repo.get("path") or "").split("/")
        if len(parts) >= 3:
            out.append((parts[0], parts[1], parts[-1]))
    return out
