"""bh configuration: ~/.beadhive/config.yaml (the one config file) + bundled assets.

The config holds more than labels — providers, orgs, exclude, dimensions, managed
rigs, and the Dolt backend — so it lives at ~/.beadhive/config.yaml
(override with $BH_HOME or $BH_CONFIG). Everything bh owns on a machine lives
under ~/.beadhive/: config.yaml, .env, docker-compose.yml, and the generated labels.md.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import MutableMapping
from importlib.resources import files
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

# Single source of truth for the tool's name, so a future rename only touches these two
# lines instead of every help string / error message that mentions the CLI by name.
BINARY_NAME = "beadhive"
BINARY_ALIAS = "bh"


class _Env(BaseSettings):
    """Every env var bh reads, one place. `env_prefix="BH_"` is the standing convention for
    any future field with no explicit alias; the fields below are the
    transition window — each still answers to its pre-rebrand `WS_*` name too (new wins when
    both are set; an empty string counts as unset, matching the old `_env_flag` behavior)."""

    model_config = SettingsConfigDict(env_prefix="BH_", extra="ignore", env_ignore_empty=True)

    home: str | None = Field(None, validation_alias=AliasChoices("BH_HOME", "WS_HOME"))
    config: str | None = Field(None, validation_alias=AliasChoices("BH_CONFIG", "WS_CONFIG"))
    hub: str | None = Field(None, validation_alias=AliasChoices("BH_HUB", "WS_HUB"))
    hq: str | None = Field(None, validation_alias=AliasChoices("BH_HQ", "WS_HQ"))
    cache: str | None = Field(None, validation_alias=AliasChoices("BH_CACHE", "WS_CACHE"))
    worktrees: str | None = Field(
        None, validation_alias=AliasChoices("BH_WORKTREES", "WS_WORKTREES")
    )
    debug: str | None = Field(None, validation_alias=AliasChoices("BH_DEBUG", "WS_DEBUG"))
    bd_pass_enabled: str | None = Field(
        None, validation_alias=AliasChoices("BH_BD_PASS_ENABLED", "WS_BD_PASS_ENABLED")
    )
    git_pass_enabled: str | None = Field(
        None, validation_alias=AliasChoices("BH_GIT_PASS_ENABLED", "WS_GIT_PASS_ENABLED")
    )
    skip_setup_check: str | None = Field(
        None, validation_alias=AliasChoices("BH_SKIP_SETUP_CHECK", "WS_SKIP_SETUP_CHECK")
    )
    role: str | None = Field(None, validation_alias=AliasChoices("BH_ROLE", "WS_ROLE"))
    dev: str | None = Field(None, validation_alias=AliasChoices("BH_DEV", "WS_DEV"))
    crew: str | None = Field(None, validation_alias=AliasChoices("BH_CREW", "WS_CREW"))
    genai_model: str | None = Field(
        None, validation_alias=AliasChoices("BH_GENAI_MODEL", "WS_GENAI_MODEL")
    )
    genai_system: str | None = Field(
        None, validation_alias=AliasChoices("BH_GENAI_SYSTEM", "WS_GENAI_SYSTEM")
    )
    observaloop_profile: str | None = Field(
        None, validation_alias=AliasChoices("BH_OBSERVALOOP_PROFILE", "WS_OBSERVALOOP_PROFILE")
    )


def _env(field: str) -> str | None:
    """One field's value (its `BH_*` name, falling back to the deprecated `WS_*` alias with a
    one-time warning). Re-instantiating `_Env()` per call is cheap (no I/O) and keeps this
    correct across env changes between calls (tests monkeypatch `os.environ` freely)."""
    value = getattr(_Env(), field)
    if value is not None:
        new_name, old_name = _Env.model_fields[field].validation_alias.choices
        if os.environ.get(new_name) is None and os.environ.get(old_name) is not None:
            from . import log  # lazy: keep config free of the log<->config import cycle

            log.get_logger(__name__).warning(
                "deprecated_env_var",
                old=old_name,
                new=new_name,
                hint=f"set {new_name} instead — {old_name} support will be removed later",
            )
    return value


_DEFAULT_HOME_OLD = Path("~/.ws").expanduser()
_DEFAULT_HOME_NEW = Path("~/.beadhive").expanduser()


def _home_migrated() -> bool:
    """Whether the new home is a genuine migrated (or deliberately fresh) install, vs. a stray
    artifact some code path wrote before migration ever ran. Bare directory
    existence isn't enough — a fixture cache dir or a setup-state.json is not proof of a real
    install, but a ``config.yaml`` is: nothing writes one except the operator (``config init``)
    or a completed move (which brings the old one across)."""
    return (_DEFAULT_HOME_NEW / "config.yaml").exists()


def _stale_home_path_keys(cfg, old_home: Path) -> list[str]:
    """Dotted keys in ``cfg`` whose string value is textually rooted under ``old_home`` — e.g.
    an operator-set ``worktrees.path: ~/.ws/wt``. A directory move can't fix these: they're just
    text, unrelated to the filesystem until something re-reads and expands them."""
    prefixes = (str(old_home), f"~/{old_home.name}")
    found: list[str] = []

    def walk(node, prefix: str) -> None:
        if isinstance(node, MutableMapping):
            for k, v in node.items():
                walk(v, f"{prefix}.{k}" if prefix else str(k))
        elif isinstance(node, str) and any(node.startswith(p) for p in prefixes):
            found.append(prefix)

    walk(cfg, "")
    return found


def _rewrite_stale_home_paths(cfg, old_home: Path, new_home: Path) -> list[str]:
    """Rewrite every stale-home-path config value in place (old_home prefix -> new_home prefix),
    preferring the ``~/<name>`` form so the rewritten value stays portable. Returns the dotted
    keys changed; caller decides whether/how to persist. Best-effort: a key set() never raises
    hard enough to abort the caller's own best-effort wrapper."""
    changed = []
    for key in _stale_home_path_keys(cfg, old_home):
        old_val = str(get_value(key, cfg)["value"])
        new_val = old_val.replace(str(old_home), str(new_home)).replace(
            f"~/{old_home.name}", f"~/{new_home.name}"
        )
        set_value(key, new_val, cfg=cfg)
        changed.append(key)
    return changed


def _repair_worktrees_after_move(cfg, new_home: Path) -> list[str]:
    """Git's own worktree bookkeeping stores absolute paths on both sides (the main repo's
    ``.git/worktrees/<name>/gitdir`` and the linked worktree's own ``.git`` file), so moving the
    home dir — and every persistent worktree under it — leaves each affected repo's
    ``git worktree list`` reporting every entry ``prunable`` until repaired. Walks the moved
    worktrees tree and runs ``git worktree repair`` once per owning repo (batched, not one call
    per worktree). Returns the repo paths repaired. Best-effort throughout: a repo this can't
    resolve (unregistered, moved, whatever) is simply left for a manual `git worktree repair`."""
    from .identity import workspace_root
    from .run import run

    wt_root = worktrees_root(cfg)
    try:
        wt_root.relative_to(new_home)
    except ValueError:
        return []  # worktrees live outside the home dir (ephemeral/OS-temp, or unaffected)
    if not wt_root.is_dir():
        return []

    ws_root = Path(workspace_root())
    by_repo: dict[str, list[str]] = {}
    for leaf in wt_root.glob("*/*/*/*"):
        if not leaf.is_dir():
            continue
        provider, org, repo = leaf.parts[-4:-1]
        main_repo = ws_root / provider / org / repo
        if main_repo.is_dir():
            by_repo.setdefault(str(main_repo), []).append(str(leaf))

    repaired = []
    for main_repo, paths in by_repo.items():
        res = run(["git", "worktree", "repair", *paths], cwd=main_repo, check=False)
        if res.returncode == 0:
            repaired.append(main_repo)
    return repaired


def migrate_home_if_needed() -> None:
    """One-time move of the pre-rebrand ~/.ws/ to ~/.beadhive/, including
    the two follow-on repairs a bare directory move can't do for free:
    rewriting config values that textually hardcode the old home path (e.g. a customized
    ``worktrees.path``), and re-linking every persistent worktree's git bookkeeping so it isn't
    left `prunable`.

    Deliberately NOT called from ``home()`` or any other getter: a plain read must never have
    the side effect of moving real state on disk, or every import/test/library call becomes a
    latent mutation hazard. Call this exactly once, from the one place that represents a real,
    intentional CLI invocation (``cli._root``) — never from a test or library import path.

    Only fires on the fully-default path — an explicit BH_HOME or legacy WS_HOME means the
    operator already made a deliberate choice, so migration stays out of the way. A genuinely
    migrated (or deliberately fresh) new home is a cheap no-op check (``_home_migrated``); a
    *stray* new home (no ``config.yaml`` — some code path wrote a cache file before migration
    ever ran) is cleared first so the real move isn't silently skipped forever."""
    if _env("home") is not None or not _DEFAULT_HOME_OLD.is_dir():
        return
    if _home_migrated():
        return
    if _DEFAULT_HOME_NEW.exists():
        shutil.rmtree(_DEFAULT_HOME_NEW)
    shutil.move(str(_DEFAULT_HOME_OLD), str(_DEFAULT_HOME_NEW))

    from . import log  # lazy: keep config free of the log<->config import cycle

    logger = log.get_logger(__name__)
    rewritten: list[str] = []
    repaired: list[str] = []
    try:
        cfg = load()
        rewritten = _rewrite_stale_home_paths(cfg, _DEFAULT_HOME_OLD, _DEFAULT_HOME_NEW)
        if rewritten:
            save(cfg)
        repaired = _repair_worktrees_after_move(cfg, _DEFAULT_HOME_NEW)
    except Exception as exc:  # best-effort: the directory move already succeeded either way
        logger.warning("home_dir_migration_followup_failed", error=str(exc))

    logger.warning(
        "home_dir_migrated",
        old=str(_DEFAULT_HOME_OLD),
        new=str(_DEFAULT_HOME_NEW),
        rewritten_config_keys=rewritten,
        repaired_repos=repaired,
    )


def home() -> Path:
    env = _env("home")
    return Path(env).expanduser() if env else _DEFAULT_HOME_NEW


def config_path() -> Path:
    env = _env("config")
    return Path(env).expanduser() if env else home() / "config.yaml"


def hub_dir() -> Path:
    """The aggregation hub beads DB (cross-rig view). Override with $BH_HUB."""
    env = _env("hub")
    return Path(env).expanduser() if env else home() / "hub"


def hq_dir() -> Path:
    """Factory HQ: the one durable central store — the aggregation primary that ALSO holds
    canonical hq-prefixed control-plane beads. Override with $BH_HQ. The evolved, durable form
    of the disposable ``hub_dir()`` (which it subsumes); LOCAL infra like hub/cache — no remote,
    never a git-workspace provider."""
    env = _env("hq")
    return Path(env).expanduser() if env else home() / "hq"


def cache_dir() -> Path:
    """Minimal-clone caches for uncloned rigs' beads data. Override with $BH_CACHE."""
    env = _env("cache")
    return Path(env).expanduser() if env else home() / "cache"


# Round-trip YAML so register/repos-sync edits preserve comments + the flow-style
# managed_repos entries. indent settings match the existing config layout.
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.width = 4096  # keep flow-style managed_repos entries on one line each


def worktrees_ephemeral(cfg=None) -> bool:
    """Whether worktrees are ephemeral — default **true** (omit ⇒ true) for zero-config
    adoption. Ephemeral worktrees live in an OS temp dir, are session-scoped + disposable,
    and need no sandbox grant (the session tmpdir is already writable). Set
    `worktrees.ephemeral: false` for persistent worktrees under `worktrees.path` plus
    harness sandbox-grant management. Assumes agents dispose of worktrees promptly — there
    is no resume of abandoned long-running tasks yet."""
    return bool(worktrees_cfg(cfg).get("ephemeral", True))


def worktrees_root(cfg=None) -> Path:
    """Shadow root for bh-managed worktrees (a mirror of the triplet path, OUTSIDE
    $GIT_WORKSPACE). `$BH_WORKTREES` overrides everything (advanced/testing). Otherwise:
    ephemeral ⇒ <os-temp>/bh-worktrees (not overridable by config); persistent ⇒ config
    `worktrees.path` → ~/.beadhive/worktrees."""
    env = _env("worktrees")
    if env:
        return Path(env).expanduser()
    if worktrees_ephemeral(cfg):
        return Path(tempfile.gettempdir()) / "bh-worktrees"
    path = worktrees_cfg(cfg).get("path") or str(home() / "worktrees")
    return Path(path).expanduser()


def docs_path() -> Path:
    return home() / "labels.md"


def compose_file() -> Path:
    return home() / "docker-compose.yml"


def otel_compose_file() -> Path:
    return home() / "docker-compose.otel.yml"


def env_file() -> Path:
    return home() / ".env"


def asset(name: str) -> Path:
    """Path to a file bundled in the package (assets/PRIME.md, etc.)."""
    return Path(str(files("beadhive.assets") / name))


def template(name: str) -> Path:
    """Path to a bundled template (templates/docker-compose.yml, etc.)."""
    return Path(str(files("beadhive.templates") / name))


def observaloop_dashboard_asset() -> Path:
    """Path to the bh-shipped Grafana dashboard model (assets/observaloop/bh-dashboard.json).

    The single bh telemetry dashboard `rig init --observaloop` applies via the observaloop
    adapter; bundled inside the package (under beadhive/assets) so it ships with the wheel."""
    return Path(str(files("beadhive.assets") / "observaloop" / "bh-dashboard.json"))


def observaloop_metrics_preset_asset() -> Path:
    """Path to the bh-shipped CLI-metrics collector preset (cli-metrics-preset.yaml).

    The proven short-lived-CLI metrics reshape (strip service.instance.id + promote bh.* attrs to
    datapoints + deltatocumulative) `rig init --observaloop` merges into the profile collector's
    metrics pipeline via the observaloop adapter; bundled inside the package (under beadhive/assets)
    so it ships with the wheel."""
    return Path(str(files("beadhive.assets") / "observaloop" / "cli-metrics-preset.yaml"))


def skills_src() -> Path:
    """Dir of bundled skills. Prefer the wheel copy under ws/assets/skills; fall back to the
    repo-root plugins/agf/skills/ for editable/dev installs (force-include only applies to
    built wheels)."""
    bundled = Path(str(files("beadhive.assets") / "skills"))
    if bundled.exists():
        return bundled
    # ponytail: dev/editable fallback — plugin dir is the canonical source
    return Path(__file__).resolve().parents[2] / "plugins" / "agf" / "skills"


def agents_src() -> Path:
    """Dir of bundled agent defs. Prefer the wheel copy under ws/assets/agents; fall back to
    the repo-root plugins/agf/agents/ for editable/dev installs (force-include only applies to
    built wheels)."""
    bundled = Path(str(files("beadhive.assets") / "agents"))
    if bundled.exists():
        return bundled
    # ponytail: dev/editable fallback — plugin dir is the canonical source
    return Path(__file__).resolve().parents[2] / "plugins" / "agf" / "agents"


def load():
    p = config_path()
    if not p.exists():
        raise FileNotFoundError(
            f"{BINARY_ALIAS} config not found at {p}\n"
            f"  scaffold it with:  {BINARY_ALIAS} config init"
        )
    return _yaml.load(p.read_text())


def _guard_hq_registry_controller() -> None:
    """Backstop for the §2.1 control-plane partitioning: block a controller session from mutating
    the Head Office registry (~/.beadhive/config.yaml) at the persistence choke point. The seat is
    read from the BH_DEV/BH_CREW env (or their deprecated WS_ equivalents) a controller session
    carries — no subprocess in the save hot path. Only the hard controller-read-only rule is
    enforced here; finer partition ownership is guarded at the higher-level write verbs where the
    partition is known."""
    from . import guard

    actor = _env("dev") or _env("crew") or ""
    guard.guard_controller_readonly(actor)


def save(data) -> None:
    _guard_hq_registry_controller()  # §2.1: controller is read-only over the HQ registry
    config_path().parent.mkdir(parents=True, exist_ok=True)
    with config_path().open("w") as f:
        _yaml.dump(data, f)


# ---- dotted-path get/set/unset (control-plane config mutation) ---------------
# Generic read/write/delete over the round-trip CommentedMap so operators (and, via T4,
# the MCP server) can toggle otel/features without hand-editing config.yaml. Mutations
# load() → edit the CommentedMap in place → save(), so comments and the flow-style
# managed_repos entries survive untouched. Core returns {ok, problems, old, new}.

# Top-level sections ws knows about. Writing under any other top-level key is allowed
# (user sections stay writable) but WARNs rather than rejecting.
KNOWN_SECTIONS = frozenset(
    {
        "delimiter",
        "providers",
        "orgs",
        "exclude",
        "dimensions",
        "dolt",
        "work",
        "managed_repos",
        "log",
        "otel",
        "observaloop",
        "worktrees",
        "archive",
        "metadata",
        "passthrough",
        "claude",
    }
)


def _problem(level: str, message: str) -> dict:
    return {"level": level, "message": message}


def _has_errors(problems) -> bool:
    return any(p["level"] == "error" for p in problems)


def _split_key(dotted: str) -> list[str]:
    """Split a dotted config key into path parts, rejecting empty/blank keys."""
    parts = [p for p in str(dotted).split(".") if p != ""]
    if not parts:
        raise ValueError(f"empty config key: {dotted!r}")
    return parts


def coerce_value(raw: str, as_json: bool = False):
    """Coerce a CLI string to a typed scalar. ``--json`` parses the value verbatim (lists,
    maps, or any JSON literal); otherwise ``true``/``false`` → bool, an all-digit string → int,
    and everything else stays a string."""
    if as_json:
        import json

        return json.loads(raw)
    low = raw.lower()
    if low in ("true", "false"):
        return low == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    return raw


def _validate(parts: list[str], value) -> list[dict]:
    """Permissive validation — a tiny known-key map enforces enums, otherwise anything goes.
    Returns a list of {level, message}; ``error`` rejects the write, ``warning`` proceeds."""
    problems: list[dict] = []
    dotted = ".".join(parts)
    if dotted == "otel.protocol" and value not in OTEL_PROTOCOLS:
        problems.append(
            _problem("error", f"otel.protocol must be one of {list(OTEL_PROTOCOLS)}, got {value!r}")
        )
    if parts[-1] == "enabled" and not isinstance(value, bool):
        problems.append(
            _problem("error", f"{dotted} must be a boolean (true|false), got {value!r}")
        )
    if dotted == "archive.window_days" and (not isinstance(value, int) or value <= 0):
        problems.append(
            _problem("error", f"archive.window_days must be a positive integer, got {value!r}")
        )
    if parts[0] not in KNOWN_SECTIONS:
        problems.append(
            _problem("warning", f"unknown config section '{parts[0]}' — writing it anyway")
        )
    return problems


def _descend(cfg, parts: list[str]):
    """Walk ``parts`` through ``cfg``. Returns (found, value)."""
    node = cfg
    for part in parts:
        if not isinstance(node, MutableMapping) or part not in node:
            return (False, None)
        node = node[part]
    return (True, node)


def get_value(dotted: str, cfg=None) -> dict:
    """Read a dotted config key. Returns {ok, problems, value}; ok=False (no raise) when unset."""
    parts = _split_key(dotted)
    cfg = cfg if cfg is not None else load()
    found, value = _descend(cfg, parts)
    if not found:
        return {"ok": False, "problems": [_problem("error", f"{dotted} is not set")], "value": None}
    return {"ok": True, "problems": [], "value": value}


def set_value(dotted: str, raw: str, as_json: bool = False, cfg=None) -> dict:
    """Set a dotted config key on the round-trip map and persist. Intermediate maps are
    auto-vivified as CommentedMaps. Returns {ok, problems, old, new}; on a validation error
    nothing is written. Loads + saves the real config unless ``cfg`` is supplied (MCP/testing)."""
    parts = _split_key(dotted)
    value = coerce_value(raw, as_json)
    problems = _validate(parts, value)
    persist = cfg is None
    cfg = cfg if cfg is not None else load()

    node = cfg
    for i, part in enumerate(parts[:-1]):
        child = node.get(part)
        if child is None:
            child = CommentedMap()
            node[part] = child
        elif not isinstance(child, MutableMapping):
            here = ".".join(parts[: i + 1])
            problems.append(_problem("error", f"cannot descend into '{here}': it is a scalar"))
            return {"ok": False, "problems": problems, "old": None, "new": None}
        node = child

    leaf = parts[-1]
    old = node.get(leaf)
    if _has_errors(problems):
        return {"ok": False, "problems": problems, "old": old, "new": None}
    node[leaf] = value
    if persist:
        save(cfg)
    return {"ok": True, "problems": problems, "old": old, "new": value}


def unset_value(dotted: str, cfg=None) -> dict:
    """Delete a dotted config key from the round-trip map and persist. Returns
    {ok, problems, old, new=None}; ok=False (no write) when the key is absent."""
    parts = _split_key(dotted)
    persist = cfg is None
    cfg = cfg if cfg is not None else load()

    node = cfg
    for part in parts[:-1]:
        child = node.get(part) if isinstance(node, MutableMapping) else None
        if not isinstance(child, MutableMapping):
            return {
                "ok": False,
                "problems": [_problem("error", f"{dotted} is not set")],
                "old": None,
                "new": None,
            }
        node = child

    leaf = parts[-1]
    if not isinstance(node, MutableMapping) or leaf not in node:
        return {
            "ok": False,
            "problems": [_problem("error", f"{dotted} is not set")],
            "old": None,
            "new": None,
        }
    old = node[leaf]
    del node[leaf]
    if persist:
        save(cfg)
    return {"ok": True, "problems": [], "old": old, "new": None}


def set_rig_feature_flag(entry, feature: str, enabled: bool) -> dict:
    """Set ``<feature>.enabled`` on a managed_repos entry (already resolved by the caller).

    Thin sugar over the dotted-path core: delegates to ``_validate`` for the
    ``*.enabled → bool`` check, auto-vivifies the ``<feature>`` sub-map as a flow-style
    CommentedMap (matching the flow-style layout of managed_repos entries), and writes the
    value in-place. Does **not** load or save — the caller owns the cfg lifecycle (load
    before calling, ``config.save(cfg)`` after if the call succeeds).

    Returns ``{ok, problems, old, new}``.
    """
    parts = [feature, "enabled"]
    problems = _validate(parts, enabled)
    if _has_errors(problems):
        return {"ok": False, "problems": problems, "old": None, "new": None}
    sub = entry.get(feature)
    if sub is None:
        sub = CommentedMap()
        sub.fa.set_flow_style()
        entry[feature] = sub
    elif not isinstance(sub, MutableMapping):
        err = _problem("error", f"cannot descend into '{feature}': it is a scalar")
        return {"ok": False, "problems": problems + [err], "old": None, "new": None}
    old = sub.get("enabled")
    sub["enabled"] = enabled
    return {"ok": True, "problems": problems, "old": old, "new": enabled}


def dolt_cfg(cfg=None):
    cfg = cfg if cfg is not None else load()
    return cfg.get("dolt", {}) or {}


def worktrees_cfg(cfg=None):
    cfg = cfg if cfg is not None else load()
    return cfg.get("worktrees", {}) or {}


def managed_repos(cfg=None):
    """The list of managed rig entries (`managed_repos`), or [] — handles a missing key / None
    cfg so callers (e.g. otel rig derivation) can iterate without their own load()/guard."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("managed_repos", []) or []


# ---- logging (ws.log foundation) --------------------------------------------


def log_cfg(cfg=None):
    """The global `log` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("log", {}) or {}


def log_format(cfg=None) -> str:
    """Render mode for diagnostics: ``auto`` (TTY-detect) | ``rich`` | ``json``.

    Default ``auto`` — ConsoleRenderer on a TTY, JSONRenderer otherwise."""
    return str(log_cfg(cfg).get("format", "auto"))


def log_level(cfg=None) -> str:
    """Minimum level for diagnostics (``debug``/``info``/``warning``/…). Default ``info``."""
    return str(log_cfg(cfg).get("level", "info"))


# ---- OpenTelemetry (ws.otel — gated SDK init) -------------------------------


def otel_cfg(cfg=None):
    """The global `otel` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("otel", {}) or {}


def otel_enabled(cfg=None) -> bool:
    """Whether to initialize the OTel SDK. **Default false** — disabled unless explicitly
    turned on, so the SDK + OTLP export are opt-in (no telemetry escapes by accident)."""
    return bool(otel_cfg(cfg).get("enabled", False))


def otel_endpoint(cfg=None) -> str:
    """OTLP collector endpoint. ``OTEL_EXPORTER_OTLP_ENDPOINT`` (the OTel-standard env) wins,
    then config ``otel.endpoint``, else ``""`` (let the exporter use its built-in default)."""
    return os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or str(otel_cfg(cfg).get("endpoint", ""))


def otel_rig(cfg=None) -> str:
    """The rig name stamped onto the Resource (``bh.rig`` attribute) so telemetry is
    attributable to the managed repo it came from. Default ``""`` — when unset ``bh.otel``
    auto-derives ``bh.rig`` from the rig prefix owning cwd (so the attribute is still present)."""
    return str(otel_cfg(cfg).get("rig", "") or "")


def otel_role(cfg=None) -> str:
    """``bh.role`` stamped onto the Resource — the seat this process runs as (e.g.
    ``dispatcher`` / ``developer`` / ``merger``), so telemetry is filterable by role.
    ``BH_ROLE`` (or the deprecated ``WS_ROLE``) env wins, then config ``otel.role``, else
    ``""`` (attribute omitted)."""
    return _env("role") or str(otel_cfg(cfg).get("role", "") or "")


# Valid otel.protocol transports — the two OTLP wire formats the ``opentelemetry-exporter-otlp``
# extra ships. The value selects the exporter CLASS for all three signals (traces/metrics/logs).
OTEL_PROTOCOL_GRPC = "grpc"
OTEL_PROTOCOL_HTTP = "http/protobuf"
OTEL_PROTOCOLS = (OTEL_PROTOCOL_GRPC, OTEL_PROTOCOL_HTTP)


def otel_protocol(cfg=None) -> str:
    """OTLP transport selecting the exporter class for every signal: ``grpc`` (default, for
    back-compat) or ``http/protobuf``. Returned verbatim — ``ws.otel.init`` validates it against
    ``OTEL_PROTOCOLS`` and fails loudly on anything else (no silent fallback to grpc)."""
    return str(otel_cfg(cfg).get("protocol", "") or OTEL_PROTOCOL_GRPC)


def otel_headers(cfg=None) -> dict[str, str]:
    """Headers threaded into every OTLP exporter constructor — e.g. an auth token for a hosted
    collector. A ``str: str`` map; default ``{}`` (no headers). Keys/values are stringified so a
    YAML-numeric token still passes through cleanly."""
    headers = otel_cfg(cfg).get("headers", {}) or {}
    return {str(k): str(v) for k, v in dict(headers).items()}


# Preferred OTLP *metric* temporality. The OTel-standard env that pre-selects it (the SDK reads
# this itself when no ``preferred_temporality`` is passed to the exporter).
OTEL_METRICS_TEMPORALITY_ENV = "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"
OTEL_TEMPORALITY_DELTA = "delta"
OTEL_TEMPORALITY_CUMULATIVE = "cumulative"


def otel_metrics_temporality(cfg=None) -> str:
    """Preferred OTLP *metric* temporality: ``delta`` (default) or ``cumulative``.

    ``ws`` is a short-lived CLI: each invocation is a fresh process, so cumulative counters never
    accumulate (Prometheus sees a swarm of single-sample series). ``ws.otel.init`` therefore
    defaults the OTLP metric exporter to DELTA for Counter/Histogram so each process reports its
    delta and the collector sums across instances. The OTel-standard
    ``OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`` env wins (an operator who set it gets the
    SDK's own env-based selection), then config ``otel.metrics_temporality``, else ``delta``.
    Returned lower-cased; ``init`` treats anything other than ``delta`` as "omit the explicit
    preference" (the SDK's cumulative default, or its env selection)."""
    return (
        os.environ.get(OTEL_METRICS_TEMPORALITY_ENV)
        or str(otel_cfg(cfg).get("metrics_temporality", "") or "")
        or OTEL_TEMPORALITY_DELTA
    ).lower()


def otel_genai_cfg(cfg=None):
    """The ``otel.genai`` subsection (or {}) — EXPERIMENTAL config for the agentic GenAI spans
    (cit.5) describing the harness driving the dispatcher agent loop."""
    return otel_cfg(cfg).get("genai", {}) or {}


def otel_genai_model(cfg=None) -> str:
    """``gen_ai.request.model`` for dispatcher->developer dispatch spans. ``BH_GENAI_MODEL``
    (or the deprecated ``WS_GENAI_MODEL``) env wins, then config ``otel.genai.model``, else
    ``""`` (attribute omitted when unknown)."""
    return _env("genai_model") or str(otel_genai_cfg(cfg).get("model", "") or "")


def otel_genai_system(cfg=None) -> str:
    """``gen_ai.system`` (the harness) for dispatch spans. ``BH_GENAI_SYSTEM`` (or the
    deprecated ``WS_GENAI_SYSTEM``) env wins, then config ``otel.genai.system``, else
    ``"claude"`` (the default harness)."""
    return (
        _env("genai_system")
        or str(otel_genai_cfg(cfg).get("system", "") or "")
        or "claude"
    )


# ---- passthrough gating (bh bd / bh git) ------------------------------------


def _env_flag(field: str):
    """Tri-state read of a boolean env var (by its `_Env` field name): True/False for a
    recognized token, else None (unset/empty → fall through to config)."""
    raw = _env(field)
    if raw is None:
        return None
    return raw.strip().lower() in ("1", "true", "yes", "on")


def passthrough_cfg(cfg=None):
    """The top-level `passthrough` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("passthrough", {}) or {}


def _pass_enabled(cfg, field: str, key: str, default: bool) -> bool:
    """Resolve a passthrough gate — precedence env > config > default, with the debug
    umbrella forcing on above all. The per-command env (bd_pass_enabled / git_pass_enabled)
    wins, then config ``passthrough.<key>``, else ``default``."""
    if _env_flag("debug"):
        return True
    env = _env_flag(field)
    if env is not None:
        return env
    val = passthrough_cfg(cfg).get(key)
    if val is not None:
        return bool(val)
    return default


def bd_pass_enabled(cfg=None) -> bool:
    """Whether the user-facing ``bh bd`` passthrough runs. **Default false** — the raw bd
    surface is gated so agents reach for the convention verbs (``bh work``, ``bh plan``)
    instead of hand-driving beads. ``BH_BD_PASS_ENABLED`` (or ``BH_DEBUG``) re-enables it;
    config key ``passthrough.bd_enabled``."""
    return _pass_enabled(cfg, "bd_pass_enabled", "bd_enabled", False)


def git_pass_enabled(cfg=None) -> bool:
    """Whether the ``bh git`` passthrough runs. **Default true** — git is left open.
    ``BH_GIT_PASS_ENABLED`` / config ``passthrough.git_enabled`` can turn it off; ``BH_DEBUG``
    forces it on."""
    return _pass_enabled(cfg, "git_pass_enabled", "git_enabled", True)


def skip_setup_check() -> bool:
    """Whether the post-install setup gate is bypassed (debug escape hatch).
    ``BH_SKIP_SETUP_CHECK`` (or the deprecated ``WS_SKIP_SETUP_CHECK``) truthy skips it."""
    return bool(_env_flag("skip_setup_check"))


# ---- observaloop (telemetry routing/profile — wired live in Phase B/C) ------


def observaloop_cfg(cfg=None):
    """The top-level `observaloop` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("observaloop", {}) or {}


def observaloop_profile(cfg=None) -> str:
    """The observaloop profile stamped onto the Resource (``observaloop.profile``) so the
    collector can route/shape a process's telemetry by profile. ``BH_OBSERVALOOP_PROFILE``
    (or the deprecated ``WS_OBSERVALOOP_PROFILE``) env wins, then top-level
    ``observaloop.profile``, then ``otel.observaloop_profile``, else ``""`` (attribute
    omitted). Defaults unset here — Phase B/C wires the live value."""
    return (
        _env("observaloop_profile")
        or str(observaloop_cfg(cfg).get("profile", "") or "")
        or str(otel_cfg(cfg).get("observaloop_profile", "") or "")
    )


def _observaloop_flag(cfg, entry) -> bool:
    """Resolve the observaloop enable flag: per-rig entry > global > default False."""
    rig_enabled = ((entry or {}).get("observaloop") or {}).get("enabled")
    if rig_enabled is not None:
        return bool(rig_enabled)
    glob = observaloop_cfg(cfg)
    if "enabled" in glob:
        return bool(glob["enabled"])
    return False


def observaloop_enabled(cfg, entry=None) -> bool:
    """True only when the observaloop enable flag is set AND ``otel_enabled`` is true.

    Observaloop requires otel to be active; if otel is disabled, this returns False
    regardless of the observaloop flag. The flag itself is resolved with per-rig
    ``entry['observaloop']['enabled']`` > global ``observaloop.enabled`` > default False.
    """
    if not otel_enabled(cfg):
        return False
    return _observaloop_flag(cfg, entry)


def _sanitize_profile_name(s: str) -> str:
    """Sanitize a raw prefix to a valid observaloop/docker profile name.

    Rules: lowercase, ``[a-z0-9-]`` only (non-matching chars → ``-``), consecutive
    hyphens collapsed, leading/trailing hyphens stripped. Deterministic: same input
    always produces the same output.
    """
    s = s.lower()
    s = re.sub(r"[^a-z0-9-]", "-", s)
    s = re.sub(r"-+", "-", s)
    return s.strip("-")


def observaloop_profile_name(cfg, entry_or_identity) -> str:
    """Derive the per-rig observaloop profile name from the rig prefix, sanitized.

    This is the single source of truth that Phase C and the overlay use to name
    the per-rig observaloop docker profile. Deterministic: same input → same name.

    Accepts either:
    - a ``managed_repos`` entry dict (must have a ``'prefix'`` key) — used directly.
    - a rig identifier string — looked up in ``managed_repos`` by prefix.

    Returns ``""`` when the prefix cannot be resolved (unregistered string rig id
    or entry without a prefix). Profile names are sanitized via ``_sanitize_profile_name``.
    """
    if isinstance(entry_or_identity, dict):
        prefix = str(entry_or_identity.get("prefix", "") or "")
    else:
        rig_id = str(entry_or_identity)
        matched = next(
            (e for e in managed_repos(cfg) if str(e.get("prefix", "")) == rig_id),
            None,
        )
        if matched is None:
            return ""
        prefix = str(matched.get("prefix", "") or "")
    return _sanitize_profile_name(prefix)


# ---- orca (repo registry integration — first plugin) ------------------------


def orca_cfg(cfg=None):
    """The top-level `orca` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("orca", {}) or {}


def _orca_flag(cfg, entry) -> bool:
    """Resolve the orca enable flag: per-rig entry > global > default False."""
    rig_enabled = ((entry or {}).get("orca") or {}).get("enabled")
    if rig_enabled is not None:
        return bool(rig_enabled)
    glob = orca_cfg(cfg)
    if "enabled" in glob:
        return bool(glob["enabled"])
    return False


def orca_enabled(cfg, entry=None) -> bool:
    """True only when the orca enable flag is set AND git-workspace is enabled.

    orca registers git-workspace clones, so it requires the git-workspace integration; if
    it is off, this returns False regardless of the orca flag. The flag itself is resolved
    with per-rig ``entry['orca']['enabled']`` > global ``orca.enabled`` > default False.
    """
    from . import gitworkspace  # lazy: avoid an import cycle

    if not gitworkspace.enabled(cfg):
        return False
    return _orca_flag(cfg, entry)


def orca_worktrees_enabled(cfg, entry=None) -> bool:
    """True only when worktree delegation is flagged on AND orca itself is enabled.

    Resolved with per-rig ``entry['orca']['worktrees']`` > global ``orca.worktrees``
    (either a bare bool or a ``{"enabled": ...}`` mapping) > default False, then AND-gated
    on :func:`orca_enabled` (mirrors ``_orca_flag`` / ``orca_enabled``)."""
    if not orca_enabled(cfg, entry):
        return False
    rig_worktrees = ((entry or {}).get("orca") or {}).get("worktrees")
    if rig_worktrees is not None:
        return bool(rig_worktrees)
    glob = orca_cfg(cfg).get("worktrees")
    if isinstance(glob, dict):
        return bool(glob.get("enabled", False))
    if glob is not None:
        return bool(glob)
    return False


def orca_worktrees_fallback(cfg=None) -> bool:
    """Global ``orca.worktrees.fallback`` — default False (HARD FAIL when the runtime is down)."""
    glob = orca_cfg(cfg).get("worktrees")
    if isinstance(glob, dict):
        return bool(glob.get("fallback", False))
    return False


def orca_data_path(cfg=None) -> Path:
    """Path to orca's on-disk state (orca-data.json).

    Reads ``orca.data_path`` (expanduser) with a platform-aware default:
    ``~/Library/Application Support/orca/orca-data.json`` on darwin,
    ``~/.config/orca/orca-data.json`` elsewhere."""
    override = orca_cfg(cfg).get("data_path")
    if override:
        return Path(str(override)).expanduser()
    if sys.platform == "darwin":
        return Path("~/Library/Application Support/orca/orca-data.json").expanduser()
    return Path("~/.config/orca/orca-data.json").expanduser()


# ---- archive (soft-archive graveyard) ---------------------------------------


def archive_cfg(cfg=None):
    """The global `archive` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("archive", {}) or {}


def archive_dir(cfg=None) -> Path:
    """Root directory for soft-archived clones.

    Reads ``archive.dir`` with a graceful fallback to ``workspace_root()/.archived`` so
    ``ws rig retire`` (which archives into this dir) works even when the section is unset."""
    from .identity import workspace_root

    override = archive_cfg(cfg).get("dir")
    if override:
        return Path(str(override)).expanduser()
    return Path(workspace_root()) / ".archived"


def archive_window_days(cfg=None) -> int:
    """Number of days an archived clone is kept before it is eligible for pruning (default 30).

    ``ws rig archive prune`` uses this as the default ``--older-than`` threshold."""
    return int(archive_cfg(cfg).get("window_days", 30))


# ---- workspace-metadata cache (ws.metadata) ---------------------------------


def metadata_cfg(cfg=None):
    """The global `metadata` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("metadata", {}) or {}


def metadata_ttl(cfg=None) -> float:
    """Coarse TTL backstop for the workspace-metadata cache, in seconds (default 300).

    ``0`` = always-fresh/bypass (never serve cached), negative = never-expire (fingerprint-only).
    Config key ``metadata.ttl``."""
    return float(metadata_cfg(cfg).get("ttl", 300))


def metadata_background_reload(cfg=None) -> bool:
    """Whether per-repo invalidation kicks a threaded refresh so a later read serves a warm entry
    (default ``True``). Set config key ``metadata.background_reload: false`` to invalidate only."""
    return bool(metadata_cfg(cfg).get("background_reload", True))


# ---- ws work (integration-plane driver) -------------------------------------


def work_cfg(cfg=None):
    """The global `work` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("work", {}) or {}


def work_value(cfg, entry, key, default=None):
    """A work setting: per-rig `entry['work'][key]` > global `work[key]` > default."""
    rig = (entry or {}).get("work", {}) or {}
    if key in rig:
        return rig[key]
    glob = work_cfg(cfg)
    if key in glob:
        return glob[key]
    return default


def validate_cmd(cfg, entry, phase=None, main_gate=False):
    """How `ws work check/submit/merge` validates a worktree (default `just check`).

    With a ``phase`` (submit | merge | molecule | postland | union), a per-point override at
    ``work.validate.<phase>`` (per-rig > global) wins, else falls back to ``work.validate_cmd``.
    ``phase=None`` keeps the legacy single-command behavior. When ``main_gate`` (the operation
    targets the shared integration branch), a ``<phase>-main`` override is preferred over
    ``<phase>`` — so an ad-hoc bead landing on main can run the full suite while a molecule member's
    merge into ``mol/<epic>`` stays fast. Lets a rig run a fast subset at the frequent intermediate
    points and the full suite only at the main-merge boundary."""
    per = work_value(cfg, entry, "validate", {}) or {}
    keys = [f"{phase}-main", phase] if (phase and main_gate) else [phase]
    for key in keys:
        if key and key in per:
            return str(per[key])
    return str(work_value(cfg, entry, "validate_cmd", "just check"))


def validation_mode(cfg, entry):
    """Which merge boundaries re-validate the integration tip:
    relaxed (default — today: submit + assembled-mol pre-land only) |
    conservative (also re-test the tip after every per-bead merge AND post-land) |
    loose (trust per-bead submits — skip even the assembled-mol pre-land check).
    Unknown values fall back to relaxed."""
    mode = str(work_value(cfg, entry, "validation", "relaxed"))
    return mode if mode in ("relaxed", "conservative", "loose") else "relaxed"


def demo_cmd(cfg, entry):
    """How `ws work review --demo` exercises the feature with the real app (default none)."""
    return str(work_value(cfg, entry, "demo_cmd", ""))


def review_gate(cfg, entry):
    """bd gate type opened at submit: human | timer | gh:run | gh:pr (default human)."""
    return str(work_value(cfg, entry, "review_gate", "human"))


def integration_branch(cfg, entry):
    """The branch a bead branch merges back to / is measured against (default main)."""
    return str(work_value(cfg, entry, "integration_branch", "main"))


def max_commits(cfg, entry):
    """submit rejects a branch with more than this many commits over the base (default 10)."""
    return int(work_value(cfg, entry, "max_commits", 10))


def batch_max_size(cfg, entry):
    """Max issues a planner-declared `batch:<group>` may hold (handled+validated+merged as one
    unit). Default 5 — keeps a batch bubble small enough to stay reviewable / bisectable."""
    return int(work_value(cfg, entry, "batch_max_size", 5))


def dispatch_value(cfg, entry, key, default=None):
    """A work.dispatch setting: per-rig `entry['work']['dispatch'][key]` >
    global `work.dispatch[key]` > default (mirrors work_value, one level deeper)."""
    rig = ((entry or {}).get("work") or {}).get("dispatch") or {}
    if key in rig:
        return rig[key]
    glob = work_cfg(cfg).get("dispatch") or {}
    if key in glob:
        return glob[key]
    return default


def dispatch_mode(cfg, entry):
    """How the coordinator dispatches ready beads: fanout (one bead per developer
    sub-agent) | collapsed (batch beads into a shared session) | auto (choose by budget).
    Config key `work.dispatch.mode`, default fanout. Unknown values fall back to fanout."""
    mode = str(dispatch_value(cfg, entry, "mode", "fanout"))
    return mode if mode in ("fanout", "collapsed", "auto") else "fanout"


def dispatch_max_depth(cfg, entry):
    """How deep the coordinator may nest sub-agent dispatch: 0 (no sub-agents) |
    1 | 2. Config key `work.dispatch.max_depth`, default 2. Out-of-range values clamp to 2."""
    depth = int(dispatch_value(cfg, entry, "max_depth", 2))
    return depth if depth in (0, 1, 2) else 2


def dispatch_max_beads_per_session(cfg, entry):
    """Max beads a single collapsed dispatch session may hold before the coordinator
    fans out instead. Config key `work.dispatch.max_beads_per_session`, default 8."""
    return int(dispatch_value(cfg, entry, "max_beads_per_session", 8))


def dispatch_auto_budget(cfg, entry):
    """Budget (in m-sized-beads worth of work) an `auto`-mode session may absorb before
    the coordinator splits it. Config key `work.dispatch.auto_budget`, default 8."""
    return int(dispatch_value(cfg, entry, "auto_budget", 8))


def dispatch_review_mode(cfg, entry):
    """Who reviews a dispatched bead: self (the developer self-reviews) | fresh (a
    separate reviewer seat). Config key `work.dispatch.review_mode`, default self.
    Unknown values fall back to self.

    `paired` (two seats sign off) depends on the resumable-agent spike and is not yet
    wired; selecting it does NOT silently no-op — it falls back to `fresh` with a
    warning so the bead still gets an independent reviewer rather than an unreviewed
    gate."""
    mode = str(dispatch_value(cfg, entry, "review_mode", "self"))
    if mode == "paired":
        from . import log  # lazy: keep config free of the log↔config import cycle

        log.get_logger(__name__).warning(
            "review_mode_paired_fallback",
            requested="paired",
            effective="fresh",
            reason="paired review depends on the resumable-agent spike; not yet wired",
        )
        return "fresh"
    return mode if mode in ("self", "fresh") else "self"


def dispatch_reviewer_cross_seat(cfg, entry):
    """The reviewer cross-seat policy (roles/RBAC matrix §3): what happens when the seat approving
    a review gate is the same person who authored the bead (a rubber-stamp risk). `advise`
    (default) WARNS but lets the approval through; `hard` BLOCKS the self-approval so the rig gets
    the split-review guarantee. Config key `work.dispatch.reviewer_cross_seat`; unknown values fall
    back to `advise` (advisory by default — not a blanket framework rule)."""
    mode = str(dispatch_value(cfg, entry, "reviewer_cross_seat", "advise"))
    return mode if mode in ("advise", "hard") else "advise"


def union_globs(cfg, entry) -> list:
    """Globs naming append-only files eligible for union conflict resolution.

    Resolved: per-rig ``entry['work']['conflict']['union_globs']`` > global
    ``work.conflict.union_globs`` > default ``[]`` (union disabled).
    """
    rig_conflict = ((entry or {}).get("work") or {}).get("conflict") or {}
    if "union_globs" in rig_conflict:
        return list(rig_conflict["union_globs"])
    glob_conflict = work_cfg(cfg).get("conflict") or {}
    if "union_globs" in glob_conflict:
        return list(glob_conflict["union_globs"])
    return []


def work_identity(cfg, entry, actor=""):
    """Merged agent identity profile (per-rig work.identity over global), normalized to
    {mode, name, email, signing_key, sign}. mode defaults to 'agent' when any field is set,
    else 'supervised' (inherit the human's git/signing config — stamp nothing).

    Per-developer attribution: when `actor` (a dev/<name>) names an entry in the `devs` mapping
    (`work.identity.devs[dev/<name>]` → {email, signing_key, sign, optional name}), that
    developer's overrides layer over the base identity so each developer's commits are authored +
    SSH-signed as its own seat — real ledger attribution, distinct from the human and from
    sibling developers. Default behavior is unchanged when no devs are configured or `actor` is
    empty.

    Key decision (bead .28): the mapping key is `devs` (matching the `dev/` seat prefix per the
    roles/RBAC matrix). The legacy key `crews` is still honored as a DEPRECATED alias — `devs`
    entries win on collision — so existing configs keep resolving through the migration window
    (removed later per limn/kkke sequencing)."""
    glob = dict(work_cfg(cfg).get("identity", {}) or {})
    rig = dict(((entry or {}).get("work", {}) or {}).get("identity", {}) or {})
    merged = {**glob, **rig}
    # `devs` is the canonical key; `crews` is the deprecated legacy alias (devs wins on collision).
    devs = {
        **(glob.get("crews") or {}),
        **(rig.get("crews") or {}),
        **(glob.get("devs") or {}),
        **(rig.get("devs") or {}),
    }
    merged.pop("crews", None)
    merged.pop("devs", None)
    if actor and actor in devs:
        merged = {**merged, **(dict(devs[actor] or {}))}
    mode = merged.get("mode") or ("agent" if merged else "supervised")
    return {
        "mode": mode,
        "name": merged.get("name"),
        "email": merged.get("email"),
        "signing_key": merged.get("signing_key"),
        "sign": bool(merged.get("sign", False)),
    }


# ---- claude Code plugin distribution (ws.claude) ----------------------------
# Controls how `ws rig init --claude` installs AGF seat agents + role skills:
#   source=plugin (default) — install the agf Claude Code plugin via the marketplace;
#     agents and skills come from the plugin, nothing is written to .claude/agents/ or ./skills/
#   source=copy (legacy) — copy agents to .claude/agents/ and skills to ./skills/ (old behaviour)
#
# Precedence: per-rig entry['claude'][key] > global claude[key] > built-in default.


def claude_cfg(cfg=None) -> dict:
    """The global `claude` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("claude", {}) or {}


def claude_value(cfg, entry, key: str, default=None):
    """A claude setting: per-rig `entry['claude'][key]` > global `claude[key]` > default."""
    rig = (entry or {}).get("claude", {}) or {}
    if key in rig:
        return rig[key]
    glob = claude_cfg(cfg)
    if key in glob:
        return glob[key]
    return default


def claude_source(cfg=None, entry=None) -> str:
    """Distribution strategy for seat agents + role skills.

    ``plugin`` (default) — install the ``agf`` Claude Code plugin via the configured
    marketplace; nothing is written to ``.claude/agents/`` or ``./skills/``.
    ``copy`` (legacy) — copy agents + skills into the rig as tracked files (old behaviour).
    Unknown values fall back to ``plugin``."""
    val = str(claude_value(cfg, entry, "source", "plugin"))
    return val if val in ("plugin", "copy") else "plugin"


def claude_scope(cfg=None, entry=None) -> str:
    """Install scope for the agf plugin: ``user`` (default) or ``project``."""
    val = str(claude_value(cfg, entry, "scope", "user"))
    return val if val in ("user", "project") else "user"


def _manifest_lists_plugin(manifest: Path, plugin: str) -> bool:
    """True when a marketplace manifest exists and vends ``plugin``."""
    if not manifest.is_file():
        return False
    try:
        data = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return False
    return any((p or {}).get("name") == plugin for p in data.get("plugins") or [])


def _marketplace_root(cfg, plugin: str) -> Path:
    """Anchor for local marketplace values: the PRIMARY CLONE of the registered rig
    whose marketplace manifest vends ``plugin``.

    Anchoring at ``Path(__file__)`` (the running package) is wrong whenever the dev
    CLI runs from an ephemeral bead worktree — it registers the user-level marketplace
    at a path that is reclaimed after merge (dangling marketplace,) —
    and lands in site-packages for wheel installs, where no marketplace exists. The
    registry knows the durable location: rigs live at $GIT_WORKSPACE/provider/org/repo,
    so scan ``managed_repos`` for the rig hosting the plugin's marketplace. Falls back
    to the package anchor only when no registered rig qualifies (unregistered dev
    checkout, tests)."""
    from .identity import workspace_root  # function-level: avoids config↔identity cycle

    try:
        cfg = cfg if cfg is not None else load()
    except FileNotFoundError:
        cfg = {}
    ws_root = Path(workspace_root())
    for e in cfg.get("managed_repos", []) or []:
        root = ws_root / str(e.get("provider", "")) / str(e.get("org", "")) / str(e.get("repo", ""))
        if _manifest_lists_plugin(root / ".claude-plugin" / "marketplace.json", plugin):
            return root
    return Path(__file__).resolve().parents[2]  # package anchor, same as skills_src


def claude_marketplace(cfg=None, entry=None) -> str:
    """Marketplace path/identifier for the agf plugin.

    Default ``"."`` is the marketplace repo root (the ws repo doubles as its own
    marketplace). Local values (``.``/``./…``/``/…``/``~/…``) resolve to an absolute
    path anchored at the registered rig's primary clone (see ``_marketplace_root``):
    the current Claude CLI rejects a bare ``.``, a relative path would register the
    invoker's cwd, and the running package may live in an ephemeral worktree or in
    site-packages. Remote forms (owner/repo, https://…) pass through untouched."""
    val = str(claude_value(cfg, entry, "marketplace", "."))
    if not val.startswith((".", "/", "~")):
        return val  # remote form (owner/repo, https://…) — pass through
    root = _marketplace_root(cfg, claude_plugin_name(cfg, entry))
    return str((root / Path(val).expanduser()).resolve())


def claude_plugin_name(cfg=None, entry=None) -> str:
    """Name of the Claude Code plugin that vends AGF seat agents. Default ``agf``."""
    return str(claude_value(cfg, entry, "plugin", "agf"))
