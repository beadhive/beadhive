"""git-workspace: a required dep (bh-hsus.4), not an optional integration.

bh reads repo groups from the git-workspace config so they don't have to be restated in bh's own
config. WHERE that config lives has three layers with a defined precedence — see
:func:`config_paths`, which is the one resolver (bh-9bkj). Each `[[provider]]` block is a **repo
group**, not
a provider in itself: `provider` names the auth/fetch mechanism (github/gitlab/gitea), `name` is
the account/org the group queries, and `path` is the dir segment the group clones into (defaults
to `provider` when unset — the same default git-workspace itself applies). Multiple groups may
share one `provider` type, and a group's `path` may differ from its `provider` (e.g.
`path="contrib" provider="github"`) — :class:`RepoGroup` models this explicitly so the mapping
is never lost (bh-rax6 was a symptom of flattening `path or provider` into a single label).

There used to be a `git_workspace.enabled` config flag gating all of this — deleted (not
deprecated) in bh-hsus.4: `setup.PROBE_TABLE` already required the `git-workspace` binary
unconditionally (`deps.py`'s `git-workspace` row is `required=ALWAYS`), so a separate manual
on/off toggle defaulting to *off* was required and optional at the same time. Every reader here
degrades gracefully to "nothing configured yet" when no `workspace*.toml` exists — callers no
longer gate on an `enabled()` predicate, they just call through and get empty results.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
import time
import tomllib
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from .identity import workspace_root
from .modules.config.adapters.workspace_selection import (
    external_configs as _external_configs,
)
from .modules.config.adapters.workspace_selection import (  # noqa: F401 - compatibility export
    glob_configs,
    selected_git_sources,
)

_SEED_TOML = (
    "# seeded by `bh config init` — bh's internal workspace root.\n"
    "# `bh hive onboard` (or `git workspace add`) appends [[provider]] blocks here.\n"
)
_CENTRAL_PREFIX = "workspace-bh-"
_GENERATED_MANIFEST = ".bh-workspace-generated.json"


@contextmanager
def workspace_projection_lock(root: Path, *, timeout: float = 30.0):
    """Serialize projection and git-workspace invocation across local bh processes."""
    root = Path(root)
    if root.is_symlink():
        raise ValueError("workspace root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(root / ".bh-workspace-projection.lock", flags, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError("workspace projection lock is not a regular file")
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("workspace projection lock deadline exceeded") from None
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)


@dataclass(frozen=True)
class WorkspaceSource:
    name: str
    content: str
    path: Path | None = None
    backend_identity: str = ""
    revision: str = ""


@dataclass(frozen=True)
class RepoGroup:
    """One `[[provider]]` block from workspace*.toml, modeled as the repo group it actually is.

    `provider_type` is the auth/fetch mechanism (github/gitlab/gitea); `path` is the group's
    on-disk folder segment — what a hive's identity triplet's first segment actually names, NOT
    necessarily `provider_type`. `skip_forks`/`include`/`exclude` are git-workspace's own
    per-group repo filters, parsed here for visibility (bh doesn't enforce them; git-workspace
    does)."""

    provider_type: str
    account: str
    path: str
    skip_forks: bool = False
    include: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()


def workspace_sources(cfg) -> list[WorkspaceSource]:
    """Raw ordered files from the first selected workspace tier."""
    from . import config

    if config.fleet_sql_selected():
        snapshot = config.fleet_snapshot()
        return [
            WorkspaceSource(
                item.path,
                item.content,
                None,
                snapshot.backend_identity,
                snapshot.commit_revision,
            )
            for item in snapshot.documents
            if item.path == "workspace.toml"
            or (
                item.path.startswith("workspace-")
                and item.path.endswith(".toml")
                and item.path != "workspace-lock.toml"
            )
        ]
    paths = selected_git_sources(cfg, root=Path(workspace_root()), hq_dir=Path(config.hq_dir()))
    return [WorkspaceSource(path.name, path.read_text(), path) for path in paths]


def materialize_workspace_sources(cfg, root: Path) -> list[Path]:
    """Stage only derived files needed by the git-workspace child.

    The manifest owns a reserved filename namespace; external source files and
    workspace-lock.toml are never replaced or unlinked.
    """
    root = Path(root)
    if root.is_symlink():
        raise ValueError("workspace root must not be a symlink")
    from . import config

    if config.fleet_sql_selected() and _external_configs(root):
        raise ValueError("committed workspace child input is shadowed by unowned local files")
    root.mkdir(parents=True, exist_ok=True)
    sources = workspace_sources(cfg)
    manifest = root / _GENERATED_MANIFEST
    try:
        old = (
            json.loads(manifest.read_text())
            if manifest.is_file() and not manifest.is_symlink()
            else {}
        )
    except (OSError, ValueError):
        old = {}
    old_names = set(old.get("files", [])) if isinstance(old, dict) else set()
    old_names = {
        name
        for name in old_names
        if isinstance(name, str)
        and name.startswith(_CENTRAL_PREFIX)
        and name.endswith(".toml")
        and "/" not in name
    }

    selected = []
    generated = []
    for index, source in enumerate(sources):
        if source.path is not None and source.path.parent.resolve() == root.resolve():
            selected.append(source.path)
            continue
        identity = (source.revision or source.name).encode()
        digest = hashlib.sha256(identity).hexdigest()[:12]
        # git-workspace discovers workspace*.toml in lexical filename order.
        # Put the selected ordinal first so both SQL and off-root Git sources
        # retain their precedence when their names/revisions hash differently.
        name = f"{_CENTRAL_PREFIX}{index:03d}-{digest}.toml"
        target = root / name
        if target.exists() and name not in old_names:
            raise ValueError("unowned generated workspace path exists")
        if target.is_symlink() and name not in old_names:
            raise ValueError("unowned generated workspace link exists")
        fd, temporary = tempfile.mkstemp(prefix=".bh-workspace-", dir=root)
        try:
            with os.fdopen(fd, "w") as stream:
                stream.write(source.content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        generated.append(name)
        selected.append(target)
    payload = {
        "backend_identity": sources[0].backend_identity if sources else "",
        "revision": sources[0].revision if sources else "",
        "files": generated,
    }
    fd, temporary = tempfile.mkstemp(prefix=".bh-workspace-manifest-", dir=root)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(payload, stream, sort_keys=True)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, manifest)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    for name in old_names - set(generated):
        (root / name).unlink(missing_ok=True)
    return selected


def lock_committed_workspace_sources(cfg, root: Path, *, run_child):
    """Run the pinned child on only committed inputs, then promote its lockfile.

    git-workspace 1.10.1 has no config-path flag: `lock` globs every
    workspace*.toml directly under --workspace. A private staging root avoids
    mixing stale external files into its provider input. The resulting lockfile
    is then atomically installed in the unchanged real clone root, where
    `update` reads the lockfile and preserves existing repo paths.
    """
    root = Path(root)
    if root.is_symlink():
        raise ValueError("workspace root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".bh-sql-workspace-", dir=root.parent) as name:
        stage = Path(name)
        materialize_workspace_sources(cfg, stage)
        result = run_child(
            ["git", "workspace", "--workspace", str(stage), "lock"],
            check=False,
            capture=True,
            timeout=300,
            github_token=True,
        )
        if result.returncode:
            return result
        source = stage / "workspace-lock.toml"
        info = source.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4 * 1024 * 1024:
            raise ValueError("committed workspace child lockfile invalid")
        body = source.read_bytes()
        try:
            parsed = tomllib.loads(body.decode("utf-8"))
        except (UnicodeError, tomllib.TOMLDecodeError):
            raise ValueError("committed workspace child lockfile invalid") from None
        if not isinstance(parsed.get("repo"), list):
            raise ValueError("committed workspace child lockfile invalid")
        fd, temporary = tempfile.mkstemp(prefix=".bh-workspace-lock-", dir=root)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(body)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, root / "workspace-lock.toml")
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
    return result


def config_paths(cfg) -> list[Path]:
    """The workspace*.toml file(s) bh reads the fleet's repo groups from.

    THREE LAYERS, FIRST NON-EMPTY WINS — never a union (bh-9bkj). Two layers merged would be a
    provider list whose order is filesystem-dependent, and whose two halves can disagree:

    1. an explicit ``git_workspace.path`` — the escape hatch, and it always wins;
    2. ``$GIT_WORKSPACE/workspace*.toml`` — an **externally managed** workspace, where the
       operator maintains the provider list themselves. Unchanged from before this bead, so a
       host in that shape keeps working exactly as it did;
    3. ``<hq_dir>/workspace*.toml`` — an **internally managed** workspace, where the fleet's
       providers live in HQ and a host inherits them by cloning it (host_answers.py's "a host
       that clones HQ inherits all of it"). Before bh-9bkj this layer did not exist, so
       ``bh hq clone`` left the file on disk (measured on beadhive-factory: 1470 bytes, seven
       providers) and bh still told the operator to "place one".

    NO INTERNAL-VS-EXTERNAL CONFIG KEY, deliberately. The question bh-9bkj asks is whether the
    distinction needs declaring; it does not, because layer 2 already expresses it. A host that
    keeps its own ``workspace*.toml`` under ``$GIT_WORKSPACE`` IS the external shape and wins by
    ordering; a host with only HQ's copy IS the internal shape. A key nobody sets is a key that
    drifts, and the drifted value would silently pick the wrong provider list — the same class of
    surprise this fixes. A host that legitimately has BOTH gets its own file, which is the more
    deliberate act of the two: HQ's copy arrives by clone, the local one was written on purpose.

    Note this resolves what **bh** reads. The `git-workspace` BINARY takes only
    ``--workspace <dir>`` and looks for ``workspace*.toml`` inside it — see
    ``host_provision._link_workspace_config`` for how a resolved config outside that directory is
    made reachable to the child (bh-28ha).
    SQL source documents have no local source path; use workspace_sources for reads.
    """
    from . import config  # lazy: config imports deps/schema, this module is a leaf reader

    if config.fleet_sql_selected():
        return []  # Committed SQL documents have no local authoritative path.
    return selected_git_sources(cfg, root=Path(workspace_root()), hq_dir=Path(config.hq_dir()))


def is_seeded(root) -> bool:
    """Whether an internal root already has a source workspace TOML (not its lockfile)."""
    return bool(_external_configs(Path(root)))


def ensure_seeded(root) -> bool:
    """Idempotently create and minimally seed a bh-owned internal workspace root.

    Existing ``workspace*.toml`` content is never rewritten. Returns whether the operation
    created either the directory or its initial source file.
    """
    from . import config

    if config.fleet_sql_selected():
        return False
    root = Path(root)
    created = not root.is_dir()
    root.mkdir(parents=True, exist_ok=True)
    if is_seeded(root):
        return created
    (root / "workspace.toml").write_text(_SEED_TOML)
    return True


def _provider_entries(cfg):
    for source in workspace_sources(cfg):
        try:
            data = tomllib.loads(source.content)
        except (OSError, tomllib.TOMLDecodeError):
            if source.path is None:
                raise ValueError("committed workspace configuration invalid") from None
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


def deep_nested_paths(cfg) -> list[str]:
    """workspace-lock.toml `[[repo]].path` entries nested deeper than the three levels
    (`<group>/<org>/<repo>`) `orca.discover_repos`'s filesystem walk assumes — e.g. a
    multi-owner group path `<group>/<owner>/<owner2>/<repo>`. The lockfile-based readers here
    (`tracked_repos`/`repo_urls`/`upstreams`) already tolerate this (they key off the first and
    last path segment), but orca's on-disk walk never discovers such a clone — surfaced so
    `doctor` can warn about the gap instead of silently missing it."""
    out: list[str] = []
    lock = Path(workspace_root()) / "workspace-lock.toml"
    if not lock.exists():
        return out
    try:
        data = tomllib.loads(lock.read_text())
    except (OSError, tomllib.TOMLDecodeError):
        return out
    for repo in data.get("repo", []):
        path = repo.get("path") or ""
        if len(path.split("/")) > 3:
            out.append(path)
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
