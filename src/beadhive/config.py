"""bh configuration: ~/.beadhive/config.yaml (the one config file) + bundled assets.

The config holds more than labels — providers, orgs, exclude, dimensions, managed
hives, and the Dolt backend — so it lives at ~/.beadhive/config.yaml
(override with $BH_HOME or $BH_CONFIG). Everything bh owns on a machine lives
under ~/.beadhive/: config.yaml, .env, docker-compose.yml, and the generated labels.md.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import sys
import tempfile
from collections.abc import Mapping, MutableMapping
from importlib.resources import files
from pathlib import Path

from pydantic import AliasChoices, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

# The one sibling import in this module, and deliberately so: `deps` is import-cheap by design
# (bh-hsus.2/.3 — no `typer`/`config`/`setup` at its own module level, so importing it here adds
# no meaningful cost to the ~40 modules that import `config`) and lazily imports `config` back
# only inside two of its OWN functions (never at its module level), so there is no cycle.
from . import deps as _deps

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
    image_manifest: str | None = Field(None, validation_alias=AliasChoices("BH_IMAGE_MANIFEST"))
    plugin_dir: str | None = Field(None, validation_alias=AliasChoices("BH_PLUGIN_DIR"))
    opencode_skills_home: str | None = Field(
        None, validation_alias=AliasChoices("BH_OPENCODE_SKILLS_HOME")
    )
    harness: str | None = Field(None, validation_alias=AliasChoices("BH_HARNESS"))
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


def layered(cfg, entry, section, key, default=None):
    """A layered config lookup: per-hive ``entry[section][key]`` > global ``[section][key]`` >
    ``default``. ``section`` may be dotted for a nested section (e.g. ``"work.dispatch"``)."""
    parts = section.split(".")
    hive = entry or {}
    for part in parts:
        hive = (hive or {}).get(part) or {}
    if key in hive:
        return hive[key]
    cfg = cfg if cfg is not None else load()
    glob = cfg or {}
    for part in parts:
        glob = (glob or {}).get(part) or {}
    return glob[key] if key in glob else default


_UNSET = object()


def layered_flag(cfg, entry, section, key="enabled", default=False):
    """A layered boolean flag over :func:`layered`: per-hive > global > ``default``, coerced to
    ``bool``. A present value wins even when falsy; only a truly-absent key yields ``default``."""
    val = layered(cfg, entry, section, key, _UNSET)
    return default if val is _UNSET else bool(val)


def home() -> Path:
    env = _env("home")
    return Path(env).expanduser() if env else _DEFAULT_HOME_NEW


def config_path() -> Path:
    env = _env("config")
    return Path(env).expanduser() if env else home() / "config.yaml"


def hub_dir() -> Path:
    """The aggregation hub beads DB (cross-hive view). Override with $BH_HUB."""
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
    """Minimal-clone caches for uncloned hives' beads data. Override with $BH_CACHE."""
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
    """Path to a file bundled in the package (assets/AGF-hint.md, etc.)."""
    return Path(str(files("beadhive.assets") / name))


def template(name: str) -> Path:
    """Path to a bundled template (templates/docker-compose.yml, etc.)."""
    return Path(str(files("beadhive.templates") / name))


def scaffold_home(force: bool = False, dry_run: bool = False) -> list[tuple[Path, bool]]:
    """Scaffold ``home()`` from bundled templates (``config.yaml``, ``docker-compose.yml``,
    ``docker-compose.otel.yml``, ``.env.example``) and mint ``host.yaml`` if absent — the exact
    mechanics ``bh config init`` (cli.py) drives, extracted so a second caller (``bh host
    provision`` — bh-twc8.1) can reuse the identical no-clobber semantics as its own first step
    without going through the CLI layer.

    Returns ``[(path, wrote)]`` for every file considered, in call order — ``wrote=True`` only
    when this call itself wrote it; an existing file is always left alone unless ``force``, and
    ``host.yaml`` is NEVER rewritten regardless of ``force`` (identity, not template output —
    see :mod:`beadhive.host`'s module docstring).

    ``dry_run=True`` previews with zero mutation: ``wrote`` reports what a live call WOULD
    write (missing, or ``force``-eligible); ``home()`` is not created and nothing is
    copied/minted."""
    from . import host  # local import: host.py imports config, so keep the cycle import-safe

    pairs = [
        (template("config.example.yaml"), config_path()),
        (template("docker-compose.yml"), compose_file()),
        (template("docker-compose.otel.yml"), otel_compose_file()),
        (template("env.example"), home() / ".env.example"),
    ]
    if not dry_run:
        home().mkdir(parents=True, exist_ok=True)

    results: list[tuple[Path, bool]] = []
    for src, dst in pairs:
        if dst.exists() and not force:
            results.append((dst, False))
            continue
        if dry_run:
            results.append((dst, True))
            continue
        shutil.copy(src, dst)
        results.append((dst, True))

    results.append((host.path(), (not host.path().exists()) if dry_run else host.mint_if_needed()))
    return results


def observaloop_dashboard_asset() -> Path:
    """Path to the bh-shipped Grafana dashboard model (assets/observaloop/bh-dashboard.json).

    The single bh telemetry dashboard `hive init --observaloop` applies via the observaloop
    adapter; bundled inside the package (under beadhive/assets) so it ships with the wheel."""
    return Path(str(files("beadhive.assets") / "observaloop" / "bh-dashboard.json"))


def observaloop_metrics_preset_asset() -> Path:
    """Path to the bh-shipped CLI-metrics collector preset (cli-metrics-preset.yaml).

    The proven short-lived-CLI metrics reshape (strip service.instance.id + promote bh.* attrs to
    datapoints + deltatocumulative) `hive init --observaloop` merges into the profile collector's
    metrics pipeline via the observaloop adapter; bundled inside the package (under beadhive/assets)
    so it ships with the wheel."""
    return Path(str(files("beadhive.assets") / "observaloop" / "cli-metrics-preset.yaml"))


def _plugin_root(cfg=None) -> Path:
    """Root of the bh plugin (skills/, agents/, .mcp.json), resolved from the installed
    marketplace clone — the plugin is not vendored in this repo (beadhive/claude-plugin is
    canonical). Reads the marketplace manifest's ``source`` for the plugin entry."""
    override = _Env().plugin_dir
    if override:
        return Path(override).expanduser()
    try:
        cfg = cfg if cfg is not None else load()
    except FileNotFoundError:
        cfg = {}
    plugin = claude_plugin_name(cfg)
    # No qualifying local clone → keep the historical package anchor (best effort for
    # src checkouts / tests; the remote fallback only applies to marketplace *values*).
    root = _marketplace_root(cfg, plugin) or Path(__file__).resolve().parents[2]
    manifest = root / ".claude-plugin" / "marketplace.json"
    try:
        for p in json.loads(manifest.read_text()).get("plugins") or []:
            if (p or {}).get("name") == plugin:
                return (root / str(p.get("source") or ".")).resolve()
    except (OSError, json.JSONDecodeError):
        pass
    return root  # marketplace root without a manifest entry — plugin at the root


def skills_src() -> Path:
    """Dir of plugin skills, resolved from the installed marketplace clone (``_plugin_root``)."""
    return _plugin_root() / "skills"


def agents_src() -> Path:
    """Dir of plugin agent defs, resolved like ``skills_src`` (see ``_plugin_root``)."""
    return _plugin_root() / "agents"


def opencode_skills_home() -> Path:
    """Global OpenCode skills dir (``~/.config/opencode/skills`` — OpenCode's skill discovery
    root; zero repo footprint, mirrors plugin-mode philosophy). Override entirely with
    ``$BH_OPENCODE_SKILLS_HOME`` so tests never touch the operator's real ``~/.config``."""
    override = _Env().opencode_skills_home
    if override:
        return Path(override).expanduser()
    return Path.home() / ".config" / "opencode" / "skills"


# ---- fleet base + host override (bh-e0y8.5) ---------------------------------
# A host reads ONE effective config, resolved from two files: the fleet-wide base
# (``fleet.yaml`` in the HQ store — identical on every host) with the host-local
# ``config.yaml`` deep-merged over it. WHICH keys may live on which side is data, not
# branches here: :mod:`beadhive.config_partition` owns the fleet/host split and the explicit
# allowlist of fleet keys a host may still override.
#
# Two deliberate asymmetries:
#   * :func:`load` is the READ path (merged); :func:`load_host` is the WRITE path — every
#     read-modify-write (``set_value``, the registry, ``bh hive enable``) loads through it so
#     ``save()`` can never bake fleet-wide truth into a host's own file (which would then read
#     back as a rejected host override of a fleet key).
#   * Absence degrades, never fails: no fleet.yaml → host-only (a host that has not cloned HQ
#     yet); no config.yaml → fleet-only. Only BOTH absent is the historical FileNotFoundError.

FLEET_FILE = "fleet.yaml"

# `--scope` values `bh config get/set/unset` accept (bh-e0y8.6) — which layer a read/write
# targets, as opposed to the merged `load()` view.
SCOPE_FLEET = "fleet"
SCOPE_HOST = "host"


class ConfigError(ValueError):
    """A config that cannot be resolved into one effective view — today: a host config
    overriding a fleet-only key (see :func:`fleet_override_violations`)."""


def fleet_path() -> Path:
    """Where the fleet-wide config base lives: ``fleet.yaml`` inside the HQ store
    (``hq_dir()``, i.e. ``$BH_HQ`` or ``~/.beadhive/hq``). Absent until a host has cloned HQ."""
    return hq_dir() / FLEET_FILE


def load_host():
    """The host-local config (``~/.beadhive/config.yaml``) exactly as written — no fleet base
    layered under it. The **write** side of the split: every read-modify-write path loads
    through here so ``save()`` only ever persists host-owned content.

    Raises ``FileNotFoundError`` when the file is absent (the historical ``load()`` behavior)."""
    p = config_path()
    if not p.exists():
        raise FileNotFoundError(
            f"{BINARY_ALIAS} config not found at {p}\n"
            f"  scaffold it with:  {BINARY_ALIAS} config init"
        )
    return _yaml.load(p.read_text())


def load_fleet():
    """The fleet-wide config base (:func:`fleet_path`), or an empty map when there is none.

    Never raises on absence — a host that has not cloned HQ is a first-class case, not an
    error (:func:`warn_missing_fleet_config_if_needed` is the operator-facing nudge). An
    empty/blank ``fleet.yaml`` reads the same as an absent one: no fleet truth to layer."""
    p = fleet_path()
    if not p.is_file():
        return CommentedMap()
    return _yaml.load(p.read_text()) or CommentedMap()


def _leaf_paths(node, prefix: str = ""):
    """Yield the dotted path of every LEAF in a nested config mapping — the same granularity
    :mod:`beadhive.config_partition` classifies (a container row is a namespace, not a value;
    an empty mapping sets nothing at all). A list is a leaf: sequence values are replaced
    wholesale, never merged element-wise."""
    if isinstance(node, Mapping):
        for key, value in node.items():
            yield from _leaf_paths(value, f"{prefix}.{key}" if prefix else str(key))
    elif prefix:
        yield prefix


def fleet_override_violations(host) -> list[str]:
    """Dotted keys ``host`` sets that belong to the FLEET partition and are NOT in
    ``FLEET_HOST_OVERRIDE_ALLOWLIST`` — a host trying to diverge on fleet-wide truth.

    Setting a fleet key host-side counts as an override attempt whether or not the fleet base
    happens to declare it today: a stale host copy of a fleet value is exactly the silent
    divergence this split exists to prevent. Keys neither side claims (``partition_of`` →
    ``None``) are left alone — unclassified is not a licence to reject."""
    from . import config_partition  # lazy: keeps the partition data out of import-time config

    return [
        path
        for path in _leaf_paths(host)
        if config_partition.partition_of(path) == config_partition.FLEET
        and not config_partition.is_host_overridable(path)
    ]


def _delete_leaf_pruning_empty(node: dict, dotted: str) -> None:
    """Delete ``dotted``'s leaf from ``node`` (a loaded round-trip mapping), pruning any
    ancestor mapping left completely empty by that removal — upward, one level at a time,
    stopping at the first ancestor that still has content.

    A thin compatibility shim over :func:`unset_value` (bh-o9x1): the prune-when-empty
    strategy this function pioneered — needed because ``ruamel.yaml``'s round-trip writer
    mis-serializes a mapping emptied down to its LAST remaining key, corrupting the file for
    every later parse — now lives inside ``unset_value`` itself, so every caller of the public
    primitive gets it for free. Kept as a separate name (rather than inlined at its call site
    in :func:`reconcile_host_after_fleet`) only because callers pass a plain already-loaded
    ``dict``/``CommentedMap`` and expect an in-place mutation with no return value, matching
    its original signature."""
    unset_value(dotted, cfg=node)


def reconcile_host_after_fleet() -> list[str]:
    """Drop FLEET-classified leaves the host's own ``config.yaml`` still carries once a real
    ``fleet.yaml`` exists. Returns the dotted paths dropped — empty when there was nothing to do.

    The collision this repairs (bh-w2u9): ``config.example.yaml`` ships those keys LIVE, written
    as if this host were about to found a fleet via ``bh hq init``. A host JOINING an existing
    fleet via ``bh hq clone`` inherits someone else's ``fleet.yaml``, and the template's own
    copies then collide with it — so every later ``config.load()`` raises and effectively every
    ``bh`` command breaks. The cloned ``fleet.yaml`` is authoritative, so the fix is to drop the
    host's now-stale copies. Never merges them anywhere — the opposite direction from
    ``bh config split``, which publishes a founding host's fleet-shaped leaves INTO ``fleet.yaml``.

    Gated on ``load()`` ITSELF raising :class:`ConfigError` — never a parallel "is there a
    conflict" check — so it only touches a config that is genuinely unloadable right now. No
    ``fleet.yaml``, or an empty one, is a no-op (matching ``load()``'s own degrade-to-host-only
    rule), and a FLEET key a host deliberately set stays untouched until it ACTUALLY conflicts."""
    try:
        load()
    except FileNotFoundError:
        return []
    except ConfigError:
        pass
    else:
        return []  # loads cleanly already — nothing to reconcile

    raw_host = load_host()
    violations = fleet_override_violations(raw_host)
    if not violations:
        return []  # defensive: load() raised for some OTHER reason
    for path in violations:
        _delete_leaf_pruning_empty(raw_host, path)
    save(raw_host)
    return violations


def load_reconciling() -> dict:
    """``load()``, self-healing a stale un-migrated host config FIRST when needed (bh-17eb).

    A handful of entry points (``hive.add``/``hive.init``, ``hq.init``) call the validating
    ``load()`` before ``registry.register()``'s fleet/host write routing ever runs — so a host
    whose OWN pre-existing ``config.yaml`` still carries un-migrated legacy content (every
    pre-0.7.0 flat config, the highest-value upgrade path) fails right there, before the
    self-healing routing that would have fixed it gets a chance. Retrying through
    :func:`reconcile_host_after_fleet` (the SAME repair ``bh hq clone`` already applies at the
    moment its own conflict is created) closes that ordering gap generically, for every such
    caller, without each one needing to know about the edge case.

    Falls through to ``load()``'s own :class:`ConfigError` — naming every offending key — when
    reconciling doesn't actually fix it (a genuine, unrelated fleet/host conflict), so the
    operator still sees an accurate, actionable message rather than a silently swallowed one."""
    try:
        return load()
    except ConfigError:
        reconcile_host_after_fleet()
        return load()


def _reject_fleet_overrides(host) -> None:
    """Fail loudly, naming every offending key, when the host config overrides a fleet-only
    key — never silently ignore the value and never silently apply it."""
    violations = fleet_override_violations(host)
    if not violations:
        return
    keys = "\n".join(f"  - {key}" for key in violations)
    raise ConfigError(
        f"host config {config_path()} overrides fleet-only key(s):\n{keys}\n"
        f"  these are fleet-wide truth and belong in {fleet_path()} — remove them from the "
        f"host config, or add the key to config_partition.FLEET_HOST_OVERRIDE_ALLOWLIST if a "
        f"per-host override is genuinely intended."
    )


def _reject_fleet_override_for_key(parts: list[str], value) -> None:
    """The ``set_value(..., scope=SCOPE_HOST)`` guard (bh-e0y8.6): apply
    :func:`_reject_fleet_overrides` — same function, same message, reused verbatim rather than
    reimplemented — to JUST the one key being set, so a ``--scope host`` set of a non-allowlisted
    fleet key is refused immediately instead of silently landing and only surfacing on the NEXT
    :func:`load`. Gated on a non-empty fleet base existing, mirroring `load`'s own
    degrade-to-host-only when there is no fleet.yaml to diverge from yet."""
    if not load_fleet():
        return
    nested: dict = {}
    node = nested
    for part in parts[:-1]:
        node = node.setdefault(part, {})
    node[parts[-1]] = value
    _reject_fleet_overrides(nested)


def _deep_merge(base, over):
    """``over`` layered onto ``base``: nested mappings merged recursively, every other value
    (scalar or list) replaced wholesale. Returns a NEW mapping — neither input is mutated, so
    the merged view can never write back through either source."""
    merged = copy.deepcopy(base)
    for key, value in over.items():
        current = merged.get(key)
        if isinstance(current, MutableMapping) and isinstance(value, Mapping):
            merged[key] = _deep_merge(current, value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def load():
    """The one effective config: the fleet base with the host config deep-merged over it.

    Precedence is host-over-fleet, but only where the host is ALLOWED to differ — a host key
    that lands on the fleet side of :mod:`beadhive.config_partition` and is not allowlisted
    raises :class:`ConfigError` naming it (:func:`_reject_fleet_overrides`). Both degradations
    are first-class: no ``fleet.yaml`` → the host config verbatim (byte-identical to
    pre-fleet behavior, and the state of every host that has not cloned HQ); no host
    ``config.yaml`` → the fleet base alone. Both absent still raises ``FileNotFoundError``
    pointing at ``bh config init``.

    Read-only and side-effect-free by contract: every ``bh`` invocation and ``log.configure``
    itself come through here, so the missing-fleet warning lives at the CLI seam instead
    (:func:`warn_missing_fleet_config_if_needed`)."""
    fleet = load_fleet()
    try:
        host = load_host()
    except FileNotFoundError:
        if not fleet:
            raise
        return fleet  # HQ cloned, no host config yet — the fleet base is enough to run on
    if not fleet:
        return host  # no fleet base to layer under: host-only, exactly as before
    _reject_fleet_overrides(host)
    return _deep_merge(fleet, host)


# ---- key provenance (`bh config show`, bh-e0y8.6) ----------------------------

PROVENANCE_FLEET = "fleet"
PROVENANCE_HOST = "host"
PROVENANCE_OVERRIDE = "override"


def key_provenance() -> dict[str, str]:
    """Origin layer for every LEAF key across the fleet base and host config — walked at the
    same leaf granularity :func:`fleet_override_violations` uses (:func:`_leaf_paths`), so
    `bh config show`'s labeling is consistent with that same partition data rather than a
    separate ad-hoc scheme.

    A key present ONLY in the fleet base is :data:`PROVENANCE_FLEET`; ONLY in the host config is
    :data:`PROVENANCE_HOST`; present in BOTH files — an allowlisted override, or an unclassified
    key both sides happen to set — is :data:`PROVENANCE_OVERRIDE`, the case worth calling out
    distinctly since it's exactly where a surprising value hides. Degrades like :func:`load`: no
    host config just means no host keys, never an error."""
    fleet_keys = set(_leaf_paths(load_fleet()))
    try:
        host = load_host()
    except FileNotFoundError:
        host = CommentedMap()
    host_keys = set(_leaf_paths(host))
    provenance: dict[str, str] = {}
    for key in fleet_keys | host_keys:
        in_fleet = key in fleet_keys
        in_host = key in host_keys
        if in_fleet and in_host:
            provenance[key] = PROVENANCE_OVERRIDE
        elif in_host:
            provenance[key] = PROVENANCE_HOST
        else:
            provenance[key] = PROVENANCE_FLEET
    return provenance


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


def save_fleet(data) -> None:
    """Persist `data` into the HQ working copy's fleet.yaml (:func:`fleet_path`) — the WRITE
    side of ``--scope fleet`` (bh-e0y8.6). Deliberately local-only: never commits or pushes the
    HQ store (that's `bh hq push`'s job, out of scope here) — just rewrites the file
    :func:`load_fleet` reads back."""
    fleet_path().parent.mkdir(parents=True, exist_ok=True)
    with fleet_path().open("w") as f:
        _yaml.dump(data, f)


# ---- one-time rig -> hive config-key migration (bh-41rh) --------------------
# The rig -> hive rename is a hard cutover (no dual-read forever), but a persisted
# ~/.beadhive/config.yaml may still carry the two pre-rename key names. A cheap, targeted,
# one-time migrate-on-load for exactly these two keys — NOT a general migration framework.
# Same placement rule as migrate_home_if_needed (home_migration.py): called once from an
# actual CLI invocation (cli._root), never from a bare load()/getter, so importing or
# reading config never has the side effect of writing real state to disk.
_HIVE_KEY_MIGRATIONS = (
    ("otel", "rig", "hive"),
    ("git_workspace", "rig_match", "hive_match"),
)

#: Legacy keys deleted outright (no replacement) — bh-hsus.4: git-workspace became a required
#: dep (`deps.py`, `required=ALWAYS`), so `git_workspace.enabled` (a manual toggle for an
#: integration that is no longer optional) is dead weight rather than a rename target. Same
#: one-time migrate-on-load posture as the rename table above — a persisted config carrying it
#: must not error, just quietly lose the key.
_LEGACY_KEY_REMOVALS = (("git_workspace", "enabled"),)


def migrate_hive_keys_if_needed() -> None:
    """Rename ``otel.rig`` -> ``otel.hive`` and ``git_workspace.rig_match`` ->
    ``git_workspace.hive_match``, and delete the legacy ``git_workspace.enabled`` flag
    (bh-hsus.4), in the persisted config, once. No-ops when the config file is absent (nothing
    to migrate yet) or none of the old keys are present (already migrated, or a fresh install)
    — idempotent, so the config round-trips clean from then on. Best-effort: never blocks the
    CLI on a migration hiccup."""
    try:
        cfg = load_host()  # write path: migrate the host's own file, never the merged view
    except FileNotFoundError:
        return
    migrated = []
    for section, old_key, new_key in _HIVE_KEY_MIGRATIONS:
        section_cfg = cfg.get(section)
        if not isinstance(section_cfg, MutableMapping) or old_key not in section_cfg:
            continue
        if new_key not in section_cfg:
            section_cfg[new_key] = section_cfg[old_key]
        del section_cfg[old_key]
        migrated.append(f"{section}.{old_key} -> {section}.{new_key}")
    for section, old_key in _LEGACY_KEY_REMOVALS:
        section_cfg = cfg.get(section)
        if not isinstance(section_cfg, MutableMapping) or old_key not in section_cfg:
            continue
        del section_cfg[old_key]
        migrated.append(f"{section}.{old_key} -> (removed)")
    if not migrated:
        return
    save(cfg)
    from . import log  # lazy: keep config free of the log<->config import cycle

    log.get_logger(__name__).warning("hive_config_keys_migrated", migrated=migrated)


# ---- lightest schema_version staleness warning (bh-5cgm.3) -------------------
# Deliberately NOT a migration: no rewrite, no transform engine — just a single best-effort
# nudge toward `bh config validate` when the persisted config predates the current schema.
# Same placement rule as migrate_hive_keys_if_needed / migrate_home_if_needed: called once
# from an actual CLI invocation (cli._root), never from a bare load()/getter, so importing or
# reading config never has this side effect (it has none anyway — this never writes).


def warn_stale_schema_version_if_needed() -> None:
    """Warn exactly once when the persisted config's ``schema_version`` is missing or older
    than :data:`beadhive.config_schema.SCHEMA_VERSION`. Silent when the config is absent (no
    config yet — `config init` is the guidance for that, not this), when
    ``schema_version`` is already current, or when it's newer than what this build knows
    (nothing stale to flag). Never writes; never raises — best-effort like its siblings."""
    try:
        cfg = load()
    except FileNotFoundError:
        return
    from .config_schema import SCHEMA_VERSION

    found = cfg.get("schema_version")
    if isinstance(found, int) and found >= SCHEMA_VERSION:
        return
    from . import log  # lazy: keep config free of the log<->config import cycle

    log.get_logger(__name__).warning(
        "config_schema_version_stale",
        found=found,
        current=SCHEMA_VERSION,
        hint=f"run `{BINARY_ALIAS} config validate` to check your config",
    )


def _hq_has_remote() -> bool:
    """Whether the HQ store is wired to a remote, i.e. is actually fleet-connected.

    Reads ``.git/config`` as TEXT rather than shelling out to ``git remote``: this runs on every
    single CLI invocation, so a subprocess here would be a per-command cost for a nudge. Same
    reason it does not go through :func:`hq_remote`, which falls back to a ``gh`` login lookup.

    A parse failure returns False — the caller only uses this to decide whether to NUDGE, so the
    quiet answer is the safe one."""
    try:
        return '[remote "' in (hq_dir() / ".git" / "config").read_text()
    except Exception:
        return False


def warn_missing_fleet_config_if_needed() -> None:
    """Warn once per invocation when this host has an HQ store but no ``fleet.yaml`` in it —
    the fleet base :func:`load` silently degrades away from.

    Same placement rule as its siblings: called from an actual CLI invocation (``cli._root``),
    never from a bare ``load()``/getter. Here that rule is load-bearing rather than stylistic —
    ``log.configure()`` reads config, so logging from inside ``load()`` would recurse.

    Deliberately silent when there is NO HQ store: a host that has never run ``bh hq init`` is
    not fleet-managed, so host-only is its normal steady state — warning on it would fire on
    every single ``bh`` invocation and train the operator to ignore the message.

    Silent for the same reason when the HQ store has NO REMOTE (bh-pc2a.31). ``bh hq init`` does
    not write ``fleet.yaml`` — only ``bh config split-migrate`` or cloning an HQ that already has
    one does — so a purely local HQ has no fleet config by construction, and warning about it
    fired on every command forever. That is precisely the train-them-to-ignore-it failure the
    paragraph above avoids, reached by a different door. A remote is what makes fleet config
    something to EXPECT; without one, host-only is again the normal steady state.

    Never writes; never raises."""
    if not hq_dir().is_dir() or fleet_path().is_file() or not _hq_has_remote():
        return
    from . import log  # lazy: keep config free of the log<->config import cycle

    log.get_logger(__name__).warning(
        "fleet_config_missing",
        expected=str(fleet_path()),
        hint="host-only config in effect until the HQ store provides a fleet.yaml",
    )


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
        "beads",
        "work",
        "hq",
        "release",
        "managed_repos",
        "log",
        "otel",
        "observaloop",
        "worktrees",
        "archive",
        "backup",
        "metadata",
        "passthrough",
        "claude",
        "harness",
        # `hitch` ships INSIDE bh and this module defines first-class accessors for it
        # (hitch_enabled / hitch_command / hitch_repo / hitch_config_dir_root), yet it was missing
        # here — so `bh config set hitch.enabled true` warned "unknown config section 'hitch' —
        # writing it anyway" at an operator following bh's own documented enablement steps. The
        # value was written correctly and the plugin read it, so the defect is only the message.
        # That is also why it is worth fixing: it tells someone doing the supported thing that
        # they are off the map (bh-m1roh).
        "hitch",
    }
)


def _problem(level: str, message: str) -> dict:
    return {"level": level, "message": message}


def _not_set_message(dotted: str) -> str:
    """'{dotted} is not set', plus a did-you-mean suggestion when *dotted* is close to (but
    not) a real BeadhiveConfig key — e.g. a typo like ``otel.protcol`` — never for a
    hopelessly-unrelated or genuinely-just-unset key (config_schema.suggest_key's cutoff)."""
    from . import config_schema

    suggestion = config_schema.suggest_key(dotted)
    message = f"{dotted} is not set"
    if suggestion:
        message += f" — did you mean '{suggestion}'?"
    return message


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
    literal_checked = False
    if dotted == "otel.protocol" and value not in OTEL_PROTOCOLS:
        problems.append(
            _problem("error", f"otel.protocol must be one of {list(OTEL_PROTOCOLS)}, got {value!r}")
        )
        literal_checked = True  # otel.py's own OTEL_PROTOCOLS check already covers this key
    if parts[-1] == "enabled" and not isinstance(value, bool):
        problems.append(
            _problem("error", f"{dotted} must be a boolean (true|false), got {value!r}")
        )
    if dotted == "archive.window_days" and (not isinstance(value, int) or value <= 0):
        problems.append(
            _problem("error", f"archive.window_days must be a positive integer, got {value!r}")
        )
    if not literal_checked:
        # bh-aidze: a value outside a `Literal[...]` field's declared range (e.g.
        # `dolt.backend: shared-server` — not a member of colima|docker|podman|none) used to be
        # accepted verbatim: this generic check names the offending key, value, and the
        # permitted set, refusing the write the same way the hand-written checks above do.
        from . import config_schema

        choices = config_schema.literal_choices(dotted)
        if choices is not None and value not in choices:
            allowed = "|".join(str(c) for c in choices)
            problems.append(_problem("error", f"{dotted} must be one of {allowed}, got {value!r}"))
    if parts[0] not in KNOWN_SECTIONS:
        from . import config_schema

        message = f"unknown config section '{parts[0]}' — writing it anyway"
        suggestion = config_schema.suggest_key(dotted)
        if suggestion:
            message += f" (did you mean '{suggestion}'?)"
        problems.append(_problem("warning", message))
    return problems


# ---- Literal-range drift (bh-aidze) -------------------------------------------
# `_validate` above closes the WRITE path (`bh config set`); a value can still arrive by hand-
# editing config.yaml directly, which never goes through `_validate` at all. These two
# functions close the LOAD path: `literal_violations` finds every such drifted leaf in a loaded
# config, and `warn_literal_violations_if_needed` is the CLI-seam nudge (same placement rule as
# `warn_stale_schema_version_if_needed`) that surfaces it on every invocation without `load()`
# itself gaining a side effect.


def literal_violations(cfg=None) -> list[dict]:
    """Every LEAF in *cfg* (default: the merged :func:`load` view) whose persisted value falls
    outside its schema field's declared ``Literal[...]`` range — e.g. ``dolt.backend:
    shared-server``, which is deliberately NOT a member (see ``DoltConfig``'s docstring: that's
    bd's shared `dolt sql-server`, a different subsystem).

    Returns ``[{"key", "value", "choices", "default"}]`` — ``default`` is the schema default
    that is EFFECTIVE in place of the invalid value (every getter in this module falls back to
    it via ``.get(key, default)`` / the group-selector tolerance in ``deps.py``), empty when
    clean. Deliberately does not walk into ``managed_repos[]`` entries — those are dynamically
    keyed per-hive overrides, a different exposure surface than a single scalar leaf, and out
    of scope here (see the module's `_leaf_paths`, which already treats a list as one leaf)."""
    from . import config_schema

    cfg = cfg if cfg is not None else load()
    violations: list[dict] = []
    for dotted in _leaf_paths(cfg):
        choices = config_schema.literal_choices(dotted)
        if choices is None:
            continue
        found, value = _descend(cfg, dotted.split("."))
        if not found or value in choices:
            continue
        violations.append(
            {
                "key": dotted,
                "value": value,
                "choices": choices,
                "default": config_schema.field_default(dotted),
            }
        )
    return violations


def warn_literal_violations_if_needed() -> None:
    """Warn once per invocation, naming the key, the offending value, the allowed set, and the
    default now in effect, for every persisted value outside its schema Literal's range
    (bh-aidze) — the load-time half `_validate` alone can't cover (a hand-edited config.yaml
    never goes through `set_value`). Deliberately a WARNING, not a raise: the rest of the
    config still loads and every OTHER key still works, so failing the whole CLI over one
    drifted value would be disproportionate — `bh config validate` is the harder gate for an
    operator who wants one. Silent when the config is absent (`config init` guidance covers
    that already) or fully clean. Never writes; never raises (matches its siblings)."""
    try:
        cfg = load()
    except FileNotFoundError:
        return
    violations = literal_violations(cfg)
    if not violations:
        return
    from . import log

    logger = log.get_logger(__name__)
    for v in violations:
        allowed = "|".join(str(c) for c in v["choices"])
        logger.warning(
            "config_literal_value_invalid",
            key=v["key"],
            value=v["value"],
            allowed=allowed,
            effective=v["default"],
            hint=(
                f"config: {v['key']} = {v['value']!r} is not one of {allowed} "
                f"(using default {v['default']!r})"
            ),
        )


def _descend(cfg, parts: list[str]):
    """Walk ``parts`` through ``cfg``. Returns (found, value)."""
    node = cfg
    for part in parts:
        if not isinstance(node, MutableMapping) or part not in node:
            return (False, None)
        node = node[part]
    return (True, node)


def get_value(dotted: str, cfg=None, scope: str | None = None) -> dict:
    """Read a dotted config key. Returns {ok, problems, value}; ok=False (no raise) when unset.

    Reads through the merged view (:func:`load`) by default. ``scope=SCOPE_HOST`` /
    ``SCOPE_FLEET`` (bh-e0y8.6) reads the named layer's raw file instead
    (:func:`load_host`/:func:`load_fleet`) — unmerged, so e.g. a fleet-only key is invisible
    with ``scope=SCOPE_HOST``. Ignored when an explicit ``cfg`` is supplied."""
    parts = _split_key(dotted)
    if cfg is None:
        if scope == SCOPE_HOST:
            cfg = load_host()
        elif scope == SCOPE_FLEET:
            cfg = load_fleet()
        else:
            cfg = load()
    found, value = _descend(cfg, parts)
    if not found:
        problem = _problem("error", _not_set_message(dotted))
        return {"ok": False, "problems": [problem], "value": None}
    return {"ok": True, "problems": [], "value": value}


def set_value(
    dotted: str, raw: str, as_json: bool = False, cfg=None, scope: str | None = None
) -> dict:
    """Set a dotted config key on the round-trip map and persist. Intermediate maps are
    auto-vivified as CommentedMaps. Returns {ok, problems, old, new}; on a validation error
    nothing is written. Loads + saves the real config unless ``cfg`` is supplied (MCP/testing).

    ``scope`` (bh-e0y8.6) picks the WRITE target: ``SCOPE_HOST`` (default) is the host's own
    file (:func:`load_host`/:func:`save`); ``SCOPE_FLEET`` is the HQ working copy's
    ``fleet.yaml`` (:func:`load_fleet`/:func:`save_fleet`) — never committed/pushed here, that's
    `bh hq push`'s job. A host-scope write of a key that belongs to the fleet partition and
    isn't allowlisted is refused with the exact message :func:`load` raises for the same key
    (:func:`_reject_fleet_override_for_key` — reused verbatim, not reimplemented)."""
    parts = _split_key(dotted)
    value = coerce_value(raw, as_json)
    problems = _validate(parts, value)
    persist = cfg is None
    scope = scope or SCOPE_HOST

    if persist and scope == SCOPE_HOST:
        try:
            _reject_fleet_override_for_key(parts, value)
        except ConfigError as exc:
            problems.append(_problem("error", str(exc)))
            return {"ok": False, "problems": problems, "old": None, "new": None}

    if persist:
        cfg = load_fleet() if scope == SCOPE_FLEET else load_host()

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
        save_fleet(cfg) if scope == SCOPE_FLEET else save(cfg)
    return {"ok": True, "problems": problems, "old": old, "new": value}


def unset_value(dotted: str, cfg=None, scope: str | None = None) -> dict:
    """Delete a dotted config key from the round-trip map and persist. Returns
    {ok, problems, old, new=None}; ok=False (no write) when the key is absent.

    Prunes any ancestor mapping left completely empty by the deletion, upward, one level at a
    time, stopping at the first ancestor that still has content — never leaves a bare
    ``section: {}`` behind (bh-o9x1). That matters because ``ruamel.yaml``'s round-trip writer
    mis-serializes a mapping emptied down to its LAST remaining key (the deleted key's attached
    comment metadata is orphaned onto the now-empty ``CommentedMap`` and corrupts the emitted
    block indentation — a genuine ruamel round-trip bug, confirmed against ruamel directly,
    independent of this module), so leaving an emptied section behind would silently corrupt
    ``config.yaml`` on write and only surface as a YAML scan error on the NEXT read. Removing
    the ancestor's own key from ITS parent instead sidesteps the bug rather than working around
    its symptom, so this primitive is safe to call repeatedly against every key of a section
    without the caller needing to know about the edge case.

    ``scope`` (bh-e0y8.6) picks the layer like :func:`set_value`: ``SCOPE_HOST`` (default,
    :func:`load_host`/:func:`save`) or ``SCOPE_FLEET`` (:func:`load_fleet`/:func:`save_fleet`)."""
    parts = _split_key(dotted)
    persist = cfg is None
    scope = scope or SCOPE_HOST
    if persist:
        cfg = load_fleet() if scope == SCOPE_FLEET else load_host()

    chain = [cfg]
    node = cfg
    for part in parts[:-1]:
        child = node.get(part) if isinstance(node, MutableMapping) else None
        if not isinstance(child, MutableMapping):
            return {
                "ok": False,
                "problems": [_problem("error", _not_set_message(dotted))],
                "old": None,
                "new": None,
            }
        node = child
        chain.append(node)

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
    for anc in range(len(parts) - 1, 0, -1):
        if chain[anc]:  # still has content — stop pruning upward
            break
        del chain[anc - 1][parts[anc - 1]]
    if persist:
        save_fleet(cfg) if scope == SCOPE_FLEET else save(cfg)
    return {"ok": True, "problems": [], "old": old, "new": None}


def set_hive_feature_flag(entry, feature: str, enabled: bool) -> dict:
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


def beads_cfg(cfg=None):
    """The `beads:` section (bh-dw3e.5) — which backend `bh`'s Engine seam (`engine.py`)
    routes bead operations through. `engine` defaults to `bd` (the only adapter implemented
    today; `br`/`bw`/`nodb` land in sibling beads)."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("beads", {}) or {}


def beads_engine(cfg=None) -> str:
    return str(beads_cfg(cfg).get("engine") or "bd")


def worktrees_cfg(cfg=None):
    cfg = cfg if cfg is not None else load()
    return cfg.get("worktrees", {}) or {}


def managed_repos(cfg=None):
    """The list of managed hive entries (`managed_repos`), or [] — handles a missing key / None
    cfg so callers (e.g. otel hive derivation) can iterate without their own load()/guard."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("managed_repos", []) or []


# ---- hq (Factory HQ remote, bh-e0y8.1) --------------------------------------


def hq_cfg(cfg=None):
    """The `hq:` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("hq", {}) or {}


def gh_login(cwd=None) -> str:
    """The active `gh` account's login, or "" when gh is absent, logged out, or unreachable.

    HOST identity, not workspace identity — deliberately the same answer no matter which repo
    the caller is standing in (bh-mw97). Never raises: an unavailable `gh` is a derivation
    miss for the caller to handle, not an error to propagate."""
    import subprocess

    try:
        done = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=cwd,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (done.stdout or "").strip() if done.returncode == 0 else ""


def hq_remote(cfg=None, cwd=None) -> str:
    """`<owner>/beadhive-hq` remote for the Factory HQ store (`bh hq init`/`clone`'s target).

    Explicit `hq.remote` wins; else derives `<owner>` from the logged-in `gh` identity.

    Deliberately NOT derived from the workspace/cwd identity (bh-mw97). HQ is a fleet
    SINGLETON, so reading its owner out of whichever hive the operator happens to be standing
    in made `bh hq init` wire a different HQ per cwd — and a guess that resolves to a
    reachable-but-wrong org wires it silently. Host identity is cwd-invariant; workspace
    identity is not. Returns "" when neither an explicit value nor a gh login exists — the
    callers (`bh hq init` / `clone`) prompt rather than guess."""
    explicit = str(hq_cfg(cfg).get("remote", "") or "")
    if explicit:
        return explicit
    owner = gh_login(cwd)
    return f"{owner}/beadhive-hq" if owner else ""


# ---- host (multi-host primary / host lease, bh-ytbb.6) ----------------------


def host_cfg(cfg=None):
    """The `host:` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("host", {}) or {}


def host_lease_cfg(cfg=None):
    """The `host.lease:` subsection (or {}) — **host** lease (host <-> hive), never bd's
    *worker* lease (worker <-> issue). See ADR Amendment 1 §5."""
    return host_cfg(cfg).get("lease", {}) or {}


def host_lease_renew_interval(cfg=None) -> float:
    """`host.lease.renew_interval` — seconds between host-lease renewals (default 300 = 5 min,
    ADR Amendment 1 §3). Fleet-scoped: every host must agree, or they disagree about who may
    write."""
    return float(host_lease_cfg(cfg).get("renew_interval", 300.0))


def host_lease_ttl(cfg=None) -> float:
    """`host.lease.ttl` — seconds a host lease stays valid without renewal (default 1800 =
    30 min, ADR Amendment 1 §3). This is the BASELINE a host's manifest `role` scales
    (`host_lease.ttl_for_role`), not a per-host value."""
    return float(host_lease_cfg(cfg).get("ttl", 1800.0))


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


# ---- harness (seat launcher: claude|opencode) -------------------------------
# Which agent harness `ws role <seat>` execs (see ws.role.launch). Per-hive-overridable like
# claude/observaloop/orca above, but a bare top-level field (not a subsection) since it's a
# single scalar, not a group of related settings.

# Derived from `deps.seat_runners()` (bh-hsus.5), not hand-mirrored — this used to be a second
# hand-written tuple kept in sync with `role.KNOWN_HARNESSES` only by a characterization test
# noticing when they drifted apart. Both now read the same table.
KNOWN_HARNESSES = tuple(d.name for d in _deps.seat_runners())


def harness_name(cfg=None, entry=None) -> str:
    """Which agent harness execs the seat process: ``claude`` (default) or ``opencode``.
    ``BH_HARNESS`` env wins, then the per-hive ``entry['harness']`` override, then global
    config ``harness``, else ``claude``."""
    env = _Env().harness
    if env:
        return env
    cfg = cfg if cfg is not None else load()
    if entry and entry.get("harness"):
        return str(entry["harness"])
    return str((cfg or {}).get("harness", "") or "") or "claude"


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


def otel_hive(cfg=None) -> str:
    """The hive name stamped onto the Resource (``bh.hive`` attribute) so telemetry is
    attributable to the managed repo it came from. Default ``""`` — when unset ``bh.otel``
    auto-derives ``bh.hive`` from the hive prefix owning cwd (so the attribute is still present)."""
    return str(otel_cfg(cfg).get("hive", "") or "")


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


def otel_genai_system(cfg=None, entry=None) -> str:
    """``gen_ai.system`` (the harness) for dispatch spans. ``BH_GENAI_SYSTEM`` (or the
    deprecated ``WS_GENAI_SYSTEM``) env wins, then config ``otel.genai.system``, else
    ``harness_name(cfg, entry)`` — so an ``opencode`` seat attributes as ``opencode``, not a
    hardcoded ``"claude"``."""
    return (
        _env("genai_system")
        or str(otel_genai_cfg(cfg).get("system", "") or "")
        or harness_name(cfg, entry)
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


def image_manifest_override() -> str | None:
    """``BH_IMAGE_MANIFEST`` — relocates the in-image component manifest that
    ``bh setup check`` reads instead of probing. Unset outside a Beadhive image."""
    return _Env().image_manifest


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


def observaloop_enabled(cfg, entry=None) -> bool:
    """True only when the observaloop enable flag is set AND ``otel_enabled`` is true.

    Observaloop requires otel to be active; if otel is disabled, this returns False
    regardless of the observaloop flag. The flag itself is resolved with per-hive
    ``entry['observaloop']['enabled']`` > global ``observaloop.enabled`` > default False.
    """
    if not otel_enabled(cfg):
        return False
    return layered_flag(cfg, entry, "observaloop")


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
    """Derive the per-hive observaloop profile name from the hive prefix, sanitized.

    This is the single source of truth that Phase C and the overlay use to name
    the per-hive observaloop docker profile. Deterministic: same input → same name.

    Accepts either:
    - a ``managed_repos`` entry dict (must have a ``'prefix'`` key) — used directly.
    - a hive identifier string — looked up in ``managed_repos`` by prefix.

    Returns ``""`` when the prefix cannot be resolved (unregistered string hive id
    or entry without a prefix). Profile names are sanitized via ``_sanitize_profile_name``.
    """
    if isinstance(entry_or_identity, dict):
        prefix = str(entry_or_identity.get("prefix", "") or "")
    else:
        hive_id = str(entry_or_identity)
        matched = next(
            (e for e in managed_repos(cfg) if str(e.get("prefix", "")) == hive_id),
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


def orca_enabled(cfg, entry=None) -> bool:
    """True only when the orca enable flag is set.

    orca registers git-workspace clones, but this no longer AND-gates on a separate
    ``git_workspace.enabled`` flag (bh-hsus.4 deleted it): git-workspace is now a required dep
    (``deps.py``, ``required=ALWAYS``), always present, so there is nothing left for the gate to
    test. The flag is resolved with per-hive ``entry['orca']['enabled']`` > global
    ``orca.enabled`` > default False.
    """
    return layered_flag(cfg, entry, "orca")


def orca_worktrees_enabled(cfg, entry=None) -> bool:
    """True only when worktree delegation is flagged on AND orca itself is enabled.

    Resolved with per-hive ``entry['orca']['worktrees']`` > global ``orca.worktrees``
    (either a bare bool or a ``{"enabled": ...}`` mapping) > default False, then AND-gated
    on :func:`orca_enabled` (mirrors ``orca_enabled``)."""
    if not orca_enabled(cfg, entry):
        return False
    hive_worktrees = ((entry or {}).get("orca") or {}).get("worktrees")
    if hive_worktrees is not None:
        return bool(hive_worktrees)
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


# ---- hitch (agent-hitch launch integration — optional plugin) ---------------


def hitch_cfg(cfg=None):
    """The top-level `hitch` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("hitch", {}) or {}


def hitch_enabled(cfg, entry=None) -> bool:
    """Whether the hitch launch integration is on. **No AND-gate on another plugin** — unlike
    ``orca_enabled`` (which requires git-workspace, since orca registers git-workspace's own
    clones), hitch shares no data or state with git-workspace / orca / observaloop: it resolves
    a Hitch Pack profile into a Config Directory and launches a harness against it, entirely
    independent of whether any other integration is on. Layered: per-hive
    ``entry['hitch']['enabled']`` > global ``hitch.enabled`` > default False."""
    return layered_flag(cfg, entry, "hitch")


def hitch_command(cfg=None) -> str:
    """The `hitch` CLI command/path (``hitch.command``, default ``"hitch"``)."""
    return str(hitch_cfg(cfg).get("command") or "hitch")


def hitch_repo(cfg=None) -> Path | None:
    """Path to the agent-hitch checkout (``hitch.repo``) providing ``profiles/local.yaml`` +
    ``catalogs/local.yaml`` + ``packs/``. ``None`` when unset — the launch verb refuses with a
    clear message rather than guessing a location."""
    raw = hitch_cfg(cfg).get("repo")
    return Path(str(raw)).expanduser() if raw else None


def hitch_config_dir_root(cfg=None) -> Path:
    """Root the Config Directory registry + build output resolve against (hitch's own ``--root``).

    **Always persistent — deliberately NOT wired to ``worktrees.ephemeral``**
    (ADR Amendment 5; bh-og0q.8). A hitch Config Directory holds Claude Code's OAuth session
    state (``.claude.json``), which nothing regenerates; a worktree holds only what git can
    reconstruct. Those are different durability requirements, so they do not share a flag —
    unlike :func:`worktrees_root`, this ignores :func:`worktrees_ephemeral` entirely. There is
    also no dedicated `hitch.ephemeral` knob: persistent is the only correct value (an
    ephemeral Config Directory forces re-login at every seat launch, defeating unattended
    operation), so no setting is exposed for it. ``hitch.root`` overrides the location;
    otherwise ``~/.beadhive/hitch``. Reuse across launches (rather than a from-scratch rebuild)
    follows from resolving to the same path every time — ``hitch up`` itself only builds a
    Config Directory that is absent (Amendment 5)."""
    override = hitch_cfg(cfg).get("root")
    path = override or str(home() / "hitch")
    return Path(path).expanduser()


# ---- archive (soft-archive graveyard) ---------------------------------------


def archive_cfg(cfg=None):
    """The global `archive` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("archive", {}) or {}


def archive_dir(cfg=None) -> Path:
    """Root directory for soft-archived clones.

    Reads ``archive.dir`` with a graceful fallback to ``workspace_root()/.archived`` so
    ``ws hive retire`` (which archives into this dir) works even when the section is unset."""
    from .identity import workspace_root

    override = archive_cfg(cfg).get("dir")
    if override:
        return Path(str(override)).expanduser()
    return Path(workspace_root()) / ".archived"


def archive_window_days(cfg=None) -> int:
    """Number of days an archived clone is kept before it is eligible for pruning (default 30).

    ``ws hive archive prune`` uses this as the default ``--older-than`` threshold."""
    return int(archive_cfg(cfg).get("window_days", 30))


# ---- backup retention (bh-cmqp.2, bh-5009a) ----------------------------------
# See docs/design/backup-retention-boundary-adr.md for the boundary between the four backup
# roots this section's keys govern — two are auto-pruned (bh owns the write path end to end),
# one is operator-invoked (bd owns the write path), one needs no pruning code at all (keep-1
# by construction). See the ADR for why each got the policy it did.


def backup_cfg(cfg=None):
    """The global `backup` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("backup", {}) or {}


def backup_hq_keep(cfg=None) -> int:
    """Dated directories kept under ``backup.hq_root()`` (default 3), newest first — never
    clamped below 1 by the caller that applies this (see ``backup.prune_hq_backups``).

    Lowered 5 -> 3 by bh-5009a: an HQ set is roughly the size of HQ's own store (~138 MB
    post-GC on the reference host), so five is most of a gigabyte held against a
    once-per-lifetime event. The alternative — pruning the pre-push set once the push succeeds
    — was rejected; see ``backup.total_warning`` for why."""
    return int(backup_cfg(cfg).get("hq_keep", 3))


def backup_hive_cap_mb(cfg=None) -> int:
    """Size threshold (MB) for a hive's ``.beads/backup/`` past which `bh backup reclaim
    --root hive` rotates it (default 500)."""
    return int(backup_cfg(cfg).get("hive_cap_mb", 500))


def backup_hive_rotate_keep(cfg=None) -> int:
    """Rotated ``.beads/backup.<timestamp>/`` generations kept after a `--root hive` reclaim
    (default 3), newest first."""
    return int(backup_cfg(cfg).get("hive_rotate_keep", 3))


def backup_migrate_keep(cfg=None) -> int:
    """Pre-migration backup sets kept PER HIVE under ``backup.migrate_root()`` (default 3),
    newest first — pruned automatically right after a migration verifies a new one. Per hive,
    not across the root: a fleet migration would otherwise let one hive's sets evict another
    hive's only one."""
    return int(backup_cfg(cfg).get("migrate_keep", 3))


def backup_total_warn_mb(cfg=None) -> int:
    """Total across every backup root past which `bh backup usage` warns (default 2048).
    ``0`` disables the warning. Host-scoped like the rest of this section — how much of THIS
    machine's disk is reasonable to hold as insurance is a local judgement."""
    return int(backup_cfg(cfg).get("total_warn_mb", 2048))


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


# ---- hub aggregate sync (bh.hub) --------------------------------------------


def hub_cfg(cfg=None):
    """The global `hub` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("hub", {}) or {}


def hub_sync_background(cfg=None) -> bool:
    """Whether the fleet-wide aggregation walk (`hub.sync()`'s `bd repo sync` over every
    registered hive) triggered from `hive onboard` / `hq push` runs in a best-effort daemon
    thread instead of blocking the triggering command (default ``True``; bh-d5jhc.1). Mirrors
    `metadata_background_reload`'s shape exactly — same gate, same one-throwaway-thread
    contract (`hub.sync_background`). Set config key ``hub.background_sync: false`` to skip the
    deferred fleet-wide refresh entirely (the triggering hive's own export/add still lands
    synchronously either way — see `hub.sync_one`); an explicit `bh sync` / `bh hq push
    --hub-sync` / `bh hive onboard --hub-sync` still refreshes the aggregate on demand."""
    return bool(hub_cfg(cfg).get("background_sync", True))


def hub_bulk_sync(cfg=None) -> bool:
    """Whether `hub.sync()`'s fleet-wide hydration uses `hub_bulk`'s cross-database bulk copy
    (bh-l7sm8) for every CO-LOCATED hive (one on the same shared Dolt server as the aggregate),
    instead of paying `bd repo sync`'s own per-edge recursive-CTE ancestry check — measured at
    ~398x for the copy itself (bh-z4z52). Falls back to `bd repo sync` automatically, per hive,
    for anything not co-located (four hydrated hives on this fleet today) AND for a co-located
    hive whose bulk copy itself fails partway (never silently dropped — see `hub_bulk
    .run_bulk_pass`).

    Default ``True`` (operator decision, 2026-08-09). The alternative default is not "safe", it
    is "pay a known upstream defect's 398x penalty on every fleet refresh" — you do not opt IN to
    avoiding a defect. Flipping this default is FLEET-SAFE because the fast path only ever engages
    for CO-LOCATED hives: a host whose hives are not on a shared Dolt server (any host still on
    embedded storage) falls through to `bd repo sync` per hive exactly as before, so enabling it
    by default changes nothing for those hosts. This remains a REVERSIBLE STOPGAP for
    [[bh-z4z52]], not yet a long-proven path — it writes cross-database into a
    derived, rebuildable READ CACHE (`guard.guard_hub` refuses writes to it, so the blast radius
    of a bad copy is "re-run `bh sync`", not data loss), but it is new code exercising a SQL
    surface (`bd sql` against another database on the same server) nothing else in this codebase
    uses yet. Set ``hub.bulk_sync: true`` to opt in; unset (or set back to ``false``) to return
    to `bd repo sync` unconditionally for every hive — the escape hatch this bead exists to keep
    open until [[bh-z4z52]]'s upstream fix lands."""
    return bool(hub_cfg(cfg).get("bulk_sync", True))


# ---- ws work (integration-plane driver) -------------------------------------


def work_cfg(cfg=None):
    """The global `work` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("work", {}) or {}


def work_value(cfg, entry, key, default=None):
    """A work setting: per-hive `entry['work'][key]` > global `work[key]` > default."""
    return layered(cfg, entry, "work", key, default)


def validate_cmd(cfg, entry, phase=None, main_gate=False):
    """How `ws work check/submit/merge` validates a worktree (default `just check`).

    With a ``phase`` (submit | merge | molecule | postland | union), a per-point override at
    ``work.validate.<phase>`` (per-hive > global) wins, else falls back to ``work.validate_cmd``.
    ``phase=None`` keeps the legacy single-command behavior. When ``main_gate`` (the operation
    targets the shared integration branch), a ``<phase>-main`` override is preferred over
    ``<phase>`` — so an ad-hoc bead landing on main can run the full suite while a molecule member's
    merge into ``mol/<epic>`` stays fast. Lets a hive run a fast subset at the frequent intermediate
    points and the full suite only at the main-merge boundary.

    A declared toolchain (bh-d0kb) is knowledge-only and is NEVER consulted here — its
    ``suggested_validate_cmd`` is something an agent proposes to the operator, who sets
    ``work.validate_cmd`` explicitly."""
    per = work_value(cfg, entry, "validate", {}) or {}
    keys = [f"{phase}-main", phase] if (phase and main_gate) else [phase]
    for key in keys:
        if key and key in per:
            return str(per[key])
    return str(work_value(cfg, entry, "validate_cmd", "just check"))


def validate_cmd_is_configured(cfg, entry) -> bool:
    """Whether the operator has explicitly set ``work.validate_cmd`` (per-hive or global),
    as opposed to silently riding the built-in ``just check`` default. Feeds the
    ``bh doctor`` / ``bh hive ready`` nudge (bh-l44i): a *named* weak gate (the operator
    chose it, even if it's compile-only) is fine; an *unnamed* one — nobody ever looked —
    is what quietly lets test regressions merge clean.

    Whether an unconfigured default actually looks test-free is a separate question — see
    ``validate_probe.probe_validate_cmd``, which resolves (rather than pattern-matches) the
    command against the hive's own justfile."""
    return layered(cfg, entry, "work", "validate_cmd", _UNSET) is not _UNSET


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


def work_landing(cfg, entry):
    """How merge/finish land onto the SHARED integration branch: local (default — a --no-ff
    merge in the clone) | pr (PR-only-main repos: push the branch + open a GitHub PR; CI and
    the PR merge take over the postland role, `work land` completes the close). Unknown values
    fall back to local. Only the shared-branch boundary is PR-governed — a bead landing into
    its molecule container (`wt/bead/epic/<epic>`) always merges locally."""
    mode = str(work_value(cfg, entry, "landing", "local"))
    return mode if mode in ("local", "pr") else "local"


def push_remote(cfg, entry):
    """The git remote branch pushes target: submit's out-of-process (`gh:*`) publish and the
    `landing: pr` push. Config key `work.push_remote`, default origin.

    A `kind=external` (contribution) hive always resolves to `origin` — that's the fork
    onboarding forked+cloned us write access to (bh-uxam.1); `work.push_remote` is a
    same-repo-family knob (e.g. `landing: pr`) and must never redirect a contribution push
    at `upstream`, which is pull-only."""
    if str((entry or {}).get("kind", "")) == "external":
        return "origin"
    return str(work_value(cfg, entry, "push_remote", "origin"))


def integration_branch(cfg, entry):
    """The branch a bead branch merges back to / is measured against (default main)."""
    return str(work_value(cfg, entry, "integration_branch", "main"))


def pr_base(cfg, entry):
    """The PR base branch NAME for a `kind=external` (contribution) hive — the branch on
    `upstream` a contribution ultimately lands on. Reuses `integration_branch` (default
    "main"): for a contribution hive that config key stops meaning "the local branch we
    merge onto" and instead names the upstream branch a worktree bases off of / a PR
    targets (`worktree.pr_base_ref` resolves the actual `upstream/<name>` git ref)."""
    return integration_branch(cfg, entry)


def max_commits(cfg, entry):
    """submit rejects a branch with more than this many commits over the base (default 10)."""
    return int(work_value(cfg, entry, "max_commits", 10))


def enforce_signing(cfg, entry) -> bool:
    """Whether the merge path refuses a branch carrying any commit git cannot verify as TRUSTED
    (default False). See `config_schema.WorkConfig.enforce_signing` for what it gates, why it is
    off by default, and why it has no grandfathering clause."""
    return bool(work_value(cfg, entry, "enforce_signing", False))


def batch_max_size(cfg, entry):
    """Max issues a planner-declared `batch:<group>` may hold (handled+validated+merged as one
    unit). Default 5 — keeps a batch bubble small enough to stay reviewable / bisectable."""
    return int(work_value(cfg, entry, "batch_max_size", 5))


def dispatch_value(cfg, entry, key, default=None):
    """A work.dispatch setting: per-hive `entry['work']['dispatch'][key]` >
    global `work.dispatch[key]` > default (work_value, one level deeper)."""
    return layered(cfg, entry, "work.dispatch", key, default)


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


def dispatch_max_action_retries(cfg, entry):
    """The `bh work next` loop-breaker threshold: escalate once a bead's own event record already
    shows N identical failed attempts of the same action. Config key
    `work.dispatch.max_action_retries`, default 2 (so the third attempt escalates).

    The count is DERIVED by counting event beads (`work_next.attempt_count`) — this knob sets a
    threshold, never a stored counter. Values below 1 clamp to 1: a threshold of 0 would escalate
    before anything had been tried."""
    return max(int(dispatch_value(cfg, entry, "max_action_retries", 2)), 1)


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
    a review gate is the same person who authored the bead (a rubber-stamp risk — including an
    agent self-approving its own dispatched work). `hard` (default, bh-e5kv) BLOCKS the
    self-approval so a `type:human` review gate always gets an independent sign-off; `advise` WARNS
    but lets the approval through — an explicit opt-out for a hive that knowingly runs a live-human-
    watching collapsed session (`review_mode: self`) and wants the shortcut back. Config key
    `work.dispatch.reviewer_cross_seat`; unknown values fall back to `hard` (fail closed — the
    review gate is a security boundary, not a UX nicety; was `advise` before bh-e5kv, which let the
    same self-approval action land sometimes blocked, sometimes merely warned, depending on
    whether the calling agent chose to heed an advisory message)."""
    mode = str(dispatch_value(cfg, entry, "reviewer_cross_seat", "hard"))
    return mode if mode in ("advise", "hard") else "hard"


def union_globs(cfg, entry) -> list:
    """Globs naming append-only files eligible for union conflict resolution.

    Resolved: per-hive ``entry['work']['conflict']['union_globs']`` > global
    ``work.conflict.union_globs`` > default ``[]`` (union disabled).
    """
    hive_conflict = ((entry or {}).get("work") or {}).get("conflict") or {}
    if "union_globs" in hive_conflict:
        return list(hive_conflict["union_globs"])
    glob_conflict = work_cfg(cfg).get("conflict") or {}
    if "union_globs" in glob_conflict:
        return list(glob_conflict["union_globs"])
    return []


def work_identity(cfg, entry, actor=""):
    """Merged agent identity profile (per-hive work.identity over global), normalized to
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
    hive = dict(((entry or {}).get("work", {}) or {}).get("identity", {}) or {})
    merged = {**glob, **hive}
    # `devs` is the canonical key; `crews` is the deprecated legacy alias (devs wins on collision).
    devs = {
        **(glob.get("crews") or {}),
        **(hive.get("crews") or {}),
        **(glob.get("devs") or {}),
        **(hive.get("devs") or {}),
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


def claim_authority(cfg, entry) -> str:
    """Named `ClaimAuthority` (claim_authority.py) `bh work claim`/`submit` use to mint + resolve
    the acting seat: default `local` (Tier 0, `LocalTrustAuthority` — LOCAL-TRUST ONLY, see that
    module's docstring). Config key `work.identity.authority`, layered per-hive over global."""
    glob = dict(work_cfg(cfg).get("identity", {}) or {})
    hive = dict(((entry or {}).get("work", {}) or {}).get("identity", {}) or {})
    merged = {**glob, **hive}
    return str(merged.get("authority") or "local")


# ---- release (release-order planning, bh-k2j8) -------------------------------
# Advisory release-order policy consulted by the dispatcher's start-verdict and the
# merger's merge-order (release_order.py, sibling beads) — never obeyed blindly, and a
# no-op when unset (falls back to today's FCFS behavior).
# Precedence: per-hive entry['release'][key] > global release[key] > built-in default.


def release_cfg(cfg=None) -> dict:
    """The global `release` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("release", {}) or {}


def release_value(cfg, entry, key: str, default=None):
    """A release setting: per-hive `entry['release'][key]` > global `release[key]` > default."""
    return layered(cfg, entry, "release", key, default)


def release_strategy(cfg, entry) -> str:
    """Named release strategy the scorer registry resolves (default stable-versioning)."""
    return str(release_value(cfg, entry, "strategy", "stable-versioning"))


def release_enforce_hold(cfg, entry) -> bool:
    """Whether a release:breaking bead gets a hard-blocking `release-hold:` gate filed at
    planning time, rather than advisory ordering only (default false)."""
    return bool(release_value(cfg, entry, "enforce_hold", False))


def release_fix_churn_budget(cfg, entry) -> int:
    """Max release:fix beads flushed ahead of features in the current patch window before
    further fixes yield to additive work (default 3)."""
    return int(release_value(cfg, entry, "fix_churn_budget", 3))


def release_conflict_estimator(cfg, entry) -> str:
    """Named ConflictEstimator the start-verdict path consults (default file-overlap, the
    bundled floor implementation)."""
    return str(release_value(cfg, entry, "conflict_estimator", "file-overlap"))


# ---- release channel staleness (bh-7daa6.6) ---------------------------------
# How long `stable` may trail `latest` before `bh doctor` says so. Per-hive-overridable like the
# rest of `release.*`, because the right number is a function of the hive's own release cadence.
# REPORTING ONLY: doctor always exits 0, so no value of either knob can gate a merge or a release
# — a lagging `stable` is the normal state during a soak, which is what the channel is FOR.


def release_channel_stale_days(cfg, entry) -> int:
    """Days the OLDEST unpromoted release may sit before `stable` is called stale. Default **14**;
    ``0`` disables the age check.

    **Why 14, measured rather than picked.** Over beadhive's own `v0.1.0..v0.8.4` — 22 releases
    across 26.1 days — the gap between consecutive releases was: median **0.56 d**, mean 1.24 d,
    p90 2.55 d, **max 9.68 d**. Any age threshold below that observed maximum fires on a repo where
    nothing is wrong (nobody had anything to promote yet), and a warning that fires when nothing is
    wrong is one operators mute. 14 is the smallest round number strictly above the observed
    maximum, with headroom for a quiet fortnight.

    **Why the age threshold is the one that carries the default.** It degrades correctly as cadence
    changes: a slower cadence produces *fewer* unpromoted releases, so the clock simply starts
    later. A count threshold has no such property (see ``release_channel_stale_releases``).

    Reproduce the measurement with::

        git for-each-ref --sort=creatordate --format='%(creatordate:unix)' 'refs/tags/v*'
    """
    return int(release_value(cfg, entry, "channel_stale_days", 14))


def release_channel_stale_releases(cfg, entry) -> int:
    """Releases `stable` may trail `latest` by before being called stale. Default **0 = off**.

    **Why the count check ships disabled.** At beadhive's measured cadence it carries no
    information: `v0.8.1 → v0.8.4` is three releases in **0.1 days**, so a "3 releases behind"
    warning would fire two and a half hours into an ordinary patch burst, every burst. "More than N
    releases behind" is meaningless without knowing cadence, and at this cadence the honest value
    of N is "don't". It stays configurable because a project releasing monthly is in the opposite
    situation — there, three releases behind is a quarter of neglect and the age clock is the blunt
    one. Set it to a positive integer to enable; it ORs with the age check, never replaces it.
    """
    return int(release_value(cfg, entry, "channel_stale_releases", 0))


# ---- claude Code plugin distribution (ws.claude) ----------------------------
# Controls how `ws hive init --claude` installs AGF seat agents + role skills:
#   source=plugin (default) — install the bh Claude Code plugin via the marketplace;
#     agents and skills come from the plugin, nothing is written to .claude/agents/ or ./skills/
#   source=copy (legacy) — copy agents to .claude/agents/ and skills to ./skills/ (old behaviour)
#
# Precedence: per-hive entry['claude'][key] > global claude[key] > built-in default.


def claude_cfg(cfg=None) -> dict:
    """The global `claude` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("claude", {}) or {}


def claude_value(cfg, entry, key: str, default=None):
    """A claude setting: per-hive `entry['claude'][key]` > global `claude[key]` > default."""
    return layered(cfg, entry, "claude", key, default)


def claude_source(cfg=None, entry=None) -> str:
    """Distribution strategy for seat agents + role skills.

    ``plugin`` (default) — install the ``bh`` Claude Code plugin via the configured
    marketplace; nothing is written to ``.claude/agents/`` or ``./skills/``.
    ``copy`` (legacy) — copy agents + skills into the hive as tracked files (old behaviour).
    Unknown values fall back to ``plugin``."""
    val = str(claude_value(cfg, entry, "source", "plugin"))
    return val if val in ("plugin", "copy") else "plugin"


def claude_scope(cfg=None, entry=None) -> str:
    """Install scope for the bh plugin: ``user`` (default) or ``project``."""
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


# Canonical remote marketplace (owner/repo form the Claude CLI fetches itself) — the
# fallback when no local clone vends the plugin (e.g. a uv tool / wheel install).
REMOTE_MARKETPLACE = "beadhive/claude-plugin"


def _marketplace_root(cfg, plugin: str) -> Path | None:
    """Anchor for local marketplace values: the PRIMARY CLONE of the registered hive
    whose marketplace manifest vends ``plugin``.

    Anchoring at ``Path(__file__)`` (the running package) is wrong whenever the dev
    CLI runs from an ephemeral bead worktree — it registers the user-level marketplace
    at a path that is reclaimed after merge (dangling marketplace,) —
    and lands in site-packages for wheel installs, where no marketplace exists. The
    registry knows the durable location: hives live at $GIT_WORKSPACE/provider/org/repo,
    so scan ``managed_repos`` for the hive hosting the plugin's marketplace. The package
    anchor survives only when it REALLY hosts a marketplace manifest vending ``plugin``
    (a genuine src checkout) — under a wheel / uv tool install parents[2] is the
    interpreter lib dir where no manifest can exist, so return None and let the caller
    fall back to the canonical remote form."""
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
    anchor = Path(__file__).resolve().parents[2]  # package anchor (src checkout only)
    if _manifest_lists_plugin(anchor / ".claude-plugin" / "marketplace.json", plugin):
        return anchor
    return None  # no local marketplace anywhere — caller falls back to the remote form


def claude_marketplace(cfg=None, entry=None) -> str:
    """Marketplace path/identifier for the bh plugin.

    Remote forms (owner/repo, https://…) pass through untouched — the Claude CLI
    fetches them itself. Local values (``.``/``./…``/``/…``/``~/…``) resolve to an
    absolute path: explicit absolute values resolve directly; relative values anchor
    at the registered hive's primary clone (see ``_marketplace_root``) because the
    current Claude CLI rejects a bare ``.``, a relative path would register the
    invoker's cwd, and the running package may live in an ephemeral worktree or in
    site-packages. When no local clone vends the plugin (every field install), the
    default resolves to the canonical remote form ``REMOTE_MARKETPLACE``."""
    val = str(claude_value(cfg, entry, "marketplace", "."))
    if not val.startswith((".", "/", "~")):
        return val  # remote form (owner/repo, https://…) — pass through
    local = Path(val).expanduser()
    if local.is_absolute():
        return str(local.resolve())  # explicit absolute path — no anchor needed
    root = _marketplace_root(cfg, claude_plugin_name(cfg, entry))
    if root is None:
        return REMOTE_MARKETPLACE  # no local marketplace to anchor at — remote fallback
    return str((root / local).resolve())


def claude_plugin_name(cfg=None, entry=None) -> str:
    """Name of the Claude Code plugin that vends Beadflow seat agents. Default ``bh``."""
    return str(claude_value(cfg, entry, "plugin", "bh"))
