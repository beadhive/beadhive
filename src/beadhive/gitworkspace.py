"""Optional integration with orf/git-workspace.

When `git_workspace.enabled` is set, bh reads repo groups from the git-workspace config
(`$GIT_WORKSPACE/workspace*.toml`) so they don't have to be restated in bh's own config. Each
`[[provider]]` block is a **repo group**, not a provider in itself: `provider` names the
auth/fetch mechanism (github/gitlab/gitea), `name` is the account/org the group queries, and
`path` is the dir segment the group clones into (defaults to `provider` when unset — the same
default git-workspace itself applies). Multiple groups may share one `provider` type, and a
group's `path` may differ from its `provider` (e.g. `path="contrib" provider="github"`) —
:class:`RepoGroup` models this explicitly so the mapping is never lost (bh-rax6 was a symptom of
flattening `path or provider` into a single label).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from glob import glob
from pathlib import Path

from .identity import workspace_root


@dataclass(frozen=True)
class RepoGroup:
    """One `[[provider]]` block from workspace*.toml, modeled as the repo group it actually is.

    `provider_type` is the auth/fetch mechanism (github/gitlab/gitea); `path` is the group's
    on-disk folder segment — what a rig's identity triplet's first segment actually names, NOT
    necessarily `provider_type`. `skip_forks`/`include`/`exclude` are git-workspace's own
    per-group repo filters, parsed here for visibility (bh doesn't enforce them; git-workspace
    does)."""

    provider_type: str
    account: str
    path: str
    skip_forks: bool = False
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


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


def groups(cfg) -> list[RepoGroup]:
    """Every `[[provider]]` block across the configured workspace*.toml sources, modeled as a
    :class:`RepoGroup`. `path` falls back to `provider_type` when the block omits it (mirroring
    git-workspace's own default); an entry with neither is skipped (it names no group)."""
    out: list[RepoGroup] = []
    for e in _provider_entries(cfg):
        provider_type = e.get("provider") or ""
        path = e.get("path") or provider_type
        if not path:
            continue
        out.append(
            RepoGroup(
                provider_type=provider_type,
                account=e.get("name") or "",
                path=path,
                skip_forks=bool(e.get("skip_forks", False)),
                include=tuple(e.get("include", []) or []),
                exclude=tuple(e.get("exclude", []) or []),
            )
        )
    return out


def providers(cfg) -> set[str]:
    """Provider labels = the dir segment (`path`) each repo group clones into. A thin view over
    :func:`groups`."""
    return {g.path for g in groups(cfg)}


def provider_host(cfg, path: str) -> str:
    """The real host (provider TYPE, e.g. 'github') behind a workspace `path` segment. `providers()`
    flattens each group down to its `path` and loses this mapping, so a `path='contrib'
    provider='github'` group never reached the github fork probe (bh-rax6). '' when unknown.
    A thin view over :func:`groups`."""
    for g in groups(cfg):
        if g.path == path:
            return g.provider_type or g.path or ""
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
    """'group/org/repo' -> upstream `owner/repo` slug, from workspace-lock.toml
    `[[repo]].upstream` (a fork's recorded parent). The OFFLINE fork signal — no gh/network
    needed, and it survives a path!=host group label (bh-rax6)."""
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
    """Accounts/orgs named by every repo group. A thin view over :func:`groups`."""
    return {g.account for g in groups(cfg) if g.account}


def repo_urls(cfg):
    """'group/org/repo' -> clone URL, from workspace-lock.toml `[[repo]]`."""
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
    """(group, org, repo) tuples from workspace-lock.toml `[[repo]].path`. The first element is
    the repo-group path segment, not necessarily the provider type — see :class:`RepoGroup`."""
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
