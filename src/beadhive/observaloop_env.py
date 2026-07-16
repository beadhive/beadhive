"""beadhive.observaloop_env — the per-worktree OTLP endpoint overlay (writer + loader).

Two halves of one mechanism that routes a worktree's ``bh`` telemetry to *its hive's* observaloop
profile endpoint, without teaching the CLI hot path observaloop's surface:

- **WRITER** ``write_worktree_env(worktree_path, profile, endpoint)`` — drops a bh-owned
  ``<worktree>/.bh/otel.env`` (``KEY=VALUE`` lines: ``OTEL_EXPORTER_OTLP_ENDPOINT`` +
  ``BH_OBSERVALOOP_PROFILE`` + optional ``OTEL_RESOURCE_ATTRIBUTES``) and ensures ``.bh/`` is
  gitignored in that worktree (reusing hive's ``.git/info/exclude`` append pattern, but resolving
  the exact exclude path git uses for *this* worktree). Called by Phase C's worktree-create hook
  (and by the loader's self-heal); this module only implements it.

- **LOADER** ``load_worktree_env(cfg)`` — invoked by ``cli._root`` *before* ``otel.init``. The
  COMMON path is a single ``is_file()`` check + at most one small read, with **NO**
  ``beadhive.observaloop`` import: when cwd is a managed (non-``verify-``) worktree and
  ``.bh/otel.env`` exists, its ``KEY=VALUE`` lines are overlaid into ``os.environ`` *without*
  clobbering any already-set var. Because ``config.otel_endpoint`` prefers
  ``OTEL_EXPORTER_OTLP_ENDPOINT`` and ``config.observaloop_profile`` reads
  ``BH_OBSERVALOOP_PROFILE``, telemetry then exports to the hive profile with the
  ``observaloop.profile`` attr set — no change to ``otel.init``. Only the SELF-HEAL branch (cache
  missing *and* observaloop enabled) lazily imports ``beadhive.observaloop`` to re-derive the
  profile + resolve the endpoint and (re)write the cache.

Best-effort throughout: every failure degrades to overlay-off; CLI startup is never blocked.
"""

from __future__ import annotations

import os
from pathlib import Path

from . import config, worktree

# The bh-owned overlay location inside a worktree, and the gitignore entry that hides it.
_WS_DIR = ".bh"
_ENV_FILE = "otel.env"
_GITIGNORE_ENTRY = ".bh/"

# The overlay's env keys. The endpoint + profile are what route telemetry to the hive profile;
# resource attrs are optional extra Resource enrichment (e.g. bh.profile=<name>).
_ENDPOINT_KEY = "OTEL_EXPORTER_OTLP_ENDPOINT"
_PROFILE_KEY = "BH_OBSERVALOOP_PROFILE"
_RESOURCE_KEY = "OTEL_RESOURCE_ATTRIBUTES"


# ---- writer (called by Phase C's create hook; here: implement + unit-test) ---


def write_worktree_env(
    worktree_path, profile: str, endpoint: str, *, resource_attrs: str = ""
) -> Path:
    """Write ``<worktree>/.bh/otel.env`` (the per-worktree endpoint overlay) and gitignore ``.bh/``.

    Emits ``KEY=VALUE`` lines — ``OTEL_EXPORTER_OTLP_ENDPOINT`` + ``BH_OBSERVALOOP_PROFILE`` always,
    plus ``OTEL_RESOURCE_ATTRIBUTES`` when ``resource_attrs`` is non-empty. Returns the env file
    path. ``.bh/`` is added to the worktree's git exclude so the cache never shows up in
    ``git status``. Best-effort on the exclude (no git / failure simply skips it)."""
    wt = Path(worktree_path)
    ws_dir = wt / _WS_DIR
    ws_dir.mkdir(parents=True, exist_ok=True)
    lines = [f"{_ENDPOINT_KEY}={endpoint}", f"{_PROFILE_KEY}={profile}"]
    if resource_attrs:
        lines.append(f"{_RESOURCE_KEY}={resource_attrs}")
    env_file = ws_dir / _ENV_FILE
    env_file.write_text("\n".join(lines) + "\n")
    _git_exclude(wt, _GITIGNORE_ENTRY)
    return env_file


def _git_exclude(worktree_path: Path, entry: str) -> None:
    """Append ``entry`` to the worktree's git exclude file iff absent — hive's ``_git_exclude``
    read-lines/append-if-missing pattern, but resolving the exact path git consults for THIS
    worktree (``git rev-parse --git-path info/exclude``) since a linked worktree's ``.git`` is a
    file, not a dir. Best-effort: no git / resolution failure simply skips (the cache just isn't
    auto-ignored, never an error)."""
    from .run import run

    res = run(
        ["git", "-C", str(worktree_path), "rev-parse", "--git-path", "info/exclude"],
        check=False,
        capture=True,
    )
    if res.returncode != 0:
        return
    rel = (res.stdout or "").strip()
    if not rel:
        return
    exclude = Path(rel)
    if not exclude.is_absolute():
        exclude = worktree_path / exclude
    lines = exclude.read_text().splitlines() if exclude.exists() else []
    if entry in lines:
        return
    exclude.parent.mkdir(parents=True, exist_ok=True)
    with exclude.open("a") as fh:
        fh.write(entry + "\n")


# ---- loader (invoked in cli._root BEFORE otel.init) -------------------------


def load_worktree_env(cfg=None) -> None:
    """Overlay ``<worktree>/.bh/otel.env`` into ``os.environ`` before ``otel.init`` (best-effort).

    COMMON path — cache present, or not a managed worktree, or observaloop off — is a single
    ``is_file()`` check + at most one small read, with **NO** ``beadhive.observaloop`` import. Only
    the self-heal branch (cache missing AND observaloop enabled) imports observaloop. Never raises:
    any failure degrades to overlay-off so the CLI starts regardless."""
    try:
        wt_dir = worktree.cwd_worktree_dir(cfg)
        if wt_dir is None:
            return  # main clone or outside the shadow root — no per-worktree overlay
        if wt_dir.name.startswith(worktree.VERIFY_LEAF_PREFIX):
            return  # ephemeral verify- clean-checkout worktree — not a seat, never overlaid
        env_file = wt_dir / _WS_DIR / _ENV_FILE
        if env_file.is_file():
            _apply_env(env_file)  # common path: one read, no observaloop import
            return
        _self_heal(cfg, wt_dir, env_file)  # cache miss — the only observaloop-touching branch
    except Exception:
        pass  # best-effort: never block CLI startup on the overlay


def _apply_env(env_file: Path) -> None:
    """Parse ``KEY=VALUE`` lines and set each in ``os.environ`` WITHOUT overwriting an already-set
    var (a real env / -e flag always wins the overlay). Blank lines, ``#`` comments, and lines
    without ``=`` are skipped."""
    for raw in env_file.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        if key and key not in os.environ:
            os.environ[key] = value.strip()


def _self_heal(cfg, wt_dir: Path, env_file: Path) -> None:
    """Cache miss: when observaloop is enabled AND available, re-derive the hive profile + resolve
    its endpoint and (re)write the cache, then load it. The ONLY branch that imports
    ``beadhive.observaloop`` — gated on ``observaloop_enabled`` (which already requires otel on)
    so the off-path returns before any observaloop import. Best-effort: any missing piece → no
    overlay."""
    cfg = cfg if cfg is not None else config.load()
    if not config.observaloop_enabled(cfg):
        return  # observaloop off → quick check, no observaloop import
    profile = config.observaloop_profile_name(cfg, _entry_for(cfg, wt_dir))
    if not profile:
        return
    from . import observaloop  # lazy: confine observaloop's surface to the self-heal branch

    if not observaloop.is_available(cfg):
        return
    endpoint = observaloop.endpoint_for(profile, config.otel_protocol(cfg), cfg)
    if not endpoint:
        return
    write_worktree_env(wt_dir, profile, endpoint)
    _apply_env(env_file)


def _entry_for(cfg, wt_dir: Path) -> dict:
    """The ``managed_repos`` entry whose triplet owns ``wt_dir`` (the three path segments before the
    leaf in ``<root>/<provider>/<org>/<repo>/<leaf>``), or a synthesized ``{prefix: repo}`` entry
    when the repo isn't registered (mirrors ``worktree._entry_for_path``'s fallback). Feeds
    ``config.observaloop_profile_name`` to name the hive profile."""
    provider, org, repo = wt_dir.parts[-4:-1]
    for e in config.managed_repos(cfg):
        if (str(e["provider"]), str(e["org"]), str(e["repo"])) == (provider, org, repo):
            return e
    return {"provider": provider, "org": org, "repo": repo, "prefix": repo}
