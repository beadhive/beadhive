"""ws configuration: ~/.ws/config.yaml (the one config file) + bundled assets.

The config holds more than labels — providers, orgs, exclude, dimensions, managed
rigs, and the Dolt backend — so it lives at ~/.ws/config.yaml
(override with $WS_HOME or $WS_CONFIG). Everything ws owns on a machine lives
under ~/.ws/: config.yaml, .env, docker-compose.yml, and the generated labels.md.
"""

from __future__ import annotations

import os
import re
import tempfile
from collections.abc import MutableMapping
from importlib.resources import files
from pathlib import Path

from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

# Round-trip YAML so register/repos-sync edits preserve comments + the flow-style
# managed_repos entries. indent settings match the existing config layout.
_yaml = YAML()
_yaml.preserve_quotes = True
_yaml.indent(mapping=2, sequence=4, offset=2)
_yaml.width = 4096  # keep flow-style managed_repos entries on one line each


def home() -> Path:
    return Path(os.environ.get("WS_HOME", "~/.ws")).expanduser()


def config_path() -> Path:
    return Path(os.environ.get("WS_CONFIG", str(home() / "config.yaml"))).expanduser()


def hub_dir() -> Path:
    """The aggregation hub beads DB (cross-rig view). Override with $WS_HUB."""
    return Path(os.environ.get("WS_HUB", str(home() / "hub"))).expanduser()


def cache_dir() -> Path:
    """Minimal-clone caches for uncloned rigs' beads data. Override with $WS_CACHE."""
    return Path(os.environ.get("WS_CACHE", str(home() / "cache"))).expanduser()


def worktrees_ephemeral(cfg=None) -> bool:
    """Whether worktrees are ephemeral — default **true** (omit ⇒ true) for zero-config
    adoption. Ephemeral worktrees live in an OS temp dir, are session-scoped + disposable,
    and need no sandbox grant (the session tmpdir is already writable). Set
    `worktrees.ephemeral: false` for persistent worktrees under `worktrees.path` plus
    harness sandbox-grant management. Assumes agents dispose of worktrees promptly — there
    is no resume of abandoned long-running tasks yet."""
    return bool(worktrees_cfg(cfg).get("ephemeral", True))


def worktrees_root(cfg=None) -> Path:
    """Shadow root for ws-managed worktrees (a mirror of the triplet path, OUTSIDE
    $GIT_WORKSPACE). `$WS_WORKTREES` overrides everything (advanced/testing). Otherwise:
    ephemeral ⇒ <os-temp>/ws-worktrees (not overridable by config); persistent ⇒ config
    `worktrees.path` → ~/.ws/worktrees."""
    env = os.environ.get("WS_WORKTREES")
    if env:
        return Path(env).expanduser()
    if worktrees_ephemeral(cfg):
        return Path(tempfile.gettempdir()) / "ws-worktrees"
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
    return Path(str(files("ws.assets") / name))


def template(name: str) -> Path:
    """Path to a bundled template (templates/docker-compose.yml, etc.)."""
    return Path(str(files("ws.templates") / name))


def observaloop_dashboard_asset() -> Path:
    """Path to the ws-shipped Grafana dashboard model (assets/observaloop/ws-dashboard.json).

    The single ws telemetry dashboard `rig init --observaloop` applies via the observaloop
    adapter; bundled inside the package (under ws/assets) so it ships with the wheel."""
    return Path(str(files("ws.assets") / "observaloop" / "ws-dashboard.json"))


def observaloop_metrics_preset_asset() -> Path:
    """Path to the ws-shipped CLI-metrics collector preset (cli-metrics-preset.yaml).

    The proven short-lived-CLI metrics reshape (strip service.instance.id + promote ws.* attrs to
    datapoints + deltatocumulative) `rig init --observaloop` merges into the profile collector's
    metrics pipeline via the observaloop adapter; bundled inside the package (under ws/assets) so it
    ships with the wheel."""
    return Path(str(files("ws.assets") / "observaloop" / "cli-metrics-preset.yaml"))


def skills_src() -> Path:
    """Dir of bundled skills. Prefer the wheel copy under ws/assets/skills; fall back to the
    repo-root skills/ for editable/dev installs (force-include only applies to built wheels)."""
    bundled = Path(str(files("ws.assets") / "skills"))
    if bundled.exists():
        return bundled
    return Path(__file__).resolve().parents[2] / "skills"  # ponytail: dev/editable fallback


def agents_src() -> Path:
    """Dir of bundled agent defs. Prefer the wheel copy under ws/assets/agents; fall back to
    the repo-root .claude/agents/ for editable/dev installs (force-include only applies to
    built wheels)."""
    bundled = Path(str(files("ws.assets") / "agents"))
    if bundled.exists():
        return bundled
    return Path(__file__).resolve().parents[2] / ".claude" / "agents"  # ponytail: dev fallback


def load():
    p = config_path()
    if not p.exists():
        raise FileNotFoundError(f"ws config not found at {p}\n  scaffold it with:  ws config init")
    return _yaml.load(p.read_text())


def save(data) -> None:
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
        "delimiter", "providers", "orgs", "exclude", "dimensions", "dolt", "work",
        "managed_repos", "log", "otel", "observaloop", "worktrees", "archive",
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
    """The rig name stamped onto the Resource (``ws.rig`` attribute) so telemetry is
    attributable to the managed repo it came from. Default ``""`` — when unset ``ws.otel``
    auto-derives ``ws.rig`` from the rig prefix owning cwd (so the attribute is still present)."""
    return str(otel_cfg(cfg).get("rig", "") or "")


def otel_role(cfg=None) -> str:
    """``ws.role`` stamped onto the Resource — the seat this process runs as (e.g.
    ``coordinator`` / ``developer`` / ``merger``), so telemetry is filterable by role.
    ``WS_ROLE`` env wins, then config ``otel.role``, else ``""`` (attribute omitted)."""
    return os.environ.get("WS_ROLE") or str(otel_cfg(cfg).get("role", "") or "")


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
    (cit.5) describing the harness driving the coordinator agent loop."""
    return otel_cfg(cfg).get("genai", {}) or {}


def otel_genai_model(cfg=None) -> str:
    """``gen_ai.request.model`` for coordinator->developer dispatch spans. ``WS_GENAI_MODEL`` env
    wins, then config ``otel.genai.model``, else ``""`` (attribute omitted when unknown)."""
    return os.environ.get("WS_GENAI_MODEL") or str(otel_genai_cfg(cfg).get("model", "") or "")


def otel_genai_system(cfg=None) -> str:
    """``gen_ai.system`` (the harness) for dispatch spans. ``WS_GENAI_SYSTEM`` env wins, then
    config ``otel.genai.system``, else ``"claude"`` (the default harness)."""
    return (
        os.environ.get("WS_GENAI_SYSTEM")
        or str(otel_genai_cfg(cfg).get("system", "") or "")
        or "claude"
    )


# ---- observaloop (telemetry routing/profile — wired live in Phase B/C) ------


def observaloop_cfg(cfg=None):
    """The top-level `observaloop` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("observaloop", {}) or {}


def observaloop_profile(cfg=None) -> str:
    """The observaloop profile stamped onto the Resource (``observaloop.profile``) so the
    collector can route/shape a process's telemetry by profile. ``WS_OBSERVALOOP_PROFILE`` env
    wins, then top-level ``observaloop.profile``, then ``otel.observaloop_profile``, else ``""``
    (attribute omitted). Defaults unset here — Phase B/C wires the live value."""
    return (
        os.environ.get("WS_OBSERVALOOP_PROFILE")
        or str(observaloop_cfg(cfg).get("profile", "") or "")
        or str(otel_cfg(cfg).get("observaloop_profile", "") or "")
    )


def _observaloop_flag(cfg, entry) -> bool:
    """Resolve the observaloop enable flag: per-rig entry > global > default False."""
    rig_enabled = (((entry or {}).get("observaloop") or {}).get("enabled"))
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


# ---- ws work (integration-plane driver) -------------------------------------


def work_cfg(cfg=None):
    """The global `work` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("work", {}) or {}


def work_value(cfg, entry, key, default=None):
    """A work setting: per-rig `entry['work'][key]` > global `work[key]` > default."""
    rig = ((entry or {}).get("work", {}) or {})
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


def union_globs(cfg, entry) -> list:
    """Globs naming append-only files eligible for union conflict resolution.

    Resolved: per-rig ``entry['work']['conflict']['union_globs']`` > global
    ``work.conflict.union_globs`` > default ``[]`` (union disabled).
    """
    rig_conflict = (((entry or {}).get("work") or {}).get("conflict") or {})
    if "union_globs" in rig_conflict:
        return list(rig_conflict["union_globs"])
    glob_conflict = (work_cfg(cfg).get("conflict") or {})
    if "union_globs" in glob_conflict:
        return list(glob_conflict["union_globs"])
    return []


def work_identity(cfg, entry, actor=""):
    """Merged agent identity profile (per-rig work.identity over global), normalized to
    {mode, name, email, signing_key, sign}. mode defaults to 'agent' when any field is set,
    else 'supervised' (inherit the human's git/signing config — stamp nothing).

    Per-crew attribution: when `actor` (a crew/<name>) names an entry in the `crews` mapping
    (`work.identity.crews[crew/<name>]` → {email, signing_key, sign, optional name}), that
    crew's overrides layer over the base identity so each developer's commits are authored +
    SSH-signed as its own crew — real ledger attribution, distinct from the human and from
    sibling crews. Default behavior is unchanged when no crews are configured or `actor` is
    empty."""
    glob = dict(work_cfg(cfg).get("identity", {}) or {})
    rig = dict(((entry or {}).get("work", {}) or {}).get("identity", {}) or {})
    merged = {**glob, **rig}
    crews = {**(glob.get("crews") or {}), **(rig.get("crews") or {})}
    merged.pop("crews", None)
    if actor and actor in crews:
        merged = {**merged, **(dict(crews[actor] or {}))}
    mode = merged.get("mode") or ("agent" if merged else "supervised")
    return {
        "mode": mode,
        "name": merged.get("name"),
        "email": merged.get("email"),
        "signing_key": merged.get("signing_key"),
        "sign": bool(merged.get("sign", False)),
    }
