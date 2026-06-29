"""ws configuration: ~/.ws/config.yaml (the one config file) + bundled assets.

The config holds more than labels — providers, orgs, exclude, dimensions, managed
rigs, and the Dolt backend — so it lives at ~/.ws/config.yaml
(override with $WS_HOME or $WS_CONFIG). Everything ws owns on a machine lives
under ~/.ws/: config.yaml, .env, docker-compose.yml, and the generated labels.md.
"""

from __future__ import annotations

import os
import tempfile
from importlib.resources import files
from pathlib import Path

from ruamel.yaml import YAML

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


def skills_src() -> Path:
    """Dir of bundled skills. Prefer the wheel copy under ws/assets/skills; fall back to the
    repo-root skills/ for editable/dev installs (force-include only applies to built wheels)."""
    bundled = Path(str(files("ws.assets") / "skills"))
    if bundled.exists():
        return bundled
    return Path(__file__).resolve().parents[2] / "skills"  # ponytail: dev/editable fallback


def load():
    p = config_path()
    if not p.exists():
        raise FileNotFoundError(f"ws config not found at {p}\n  scaffold it with:  ws config init")
    return _yaml.load(p.read_text())


def save(data) -> None:
    config_path().parent.mkdir(parents=True, exist_ok=True)
    with config_path().open("w") as f:
        _yaml.dump(data, f)


def dolt_cfg(cfg=None):
    cfg = cfg if cfg is not None else load()
    return cfg.get("dolt", {}) or {}


def worktrees_cfg(cfg=None):
    cfg = cfg if cfg is not None else load()
    return cfg.get("worktrees", {}) or {}


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
    attributable to the managed repo it came from. Default ``""`` (attribute omitted)."""
    return str(otel_cfg(cfg).get("rig", "") or "")


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


def validate_cmd(cfg, entry):
    """How `ws work check/submit` validates a worktree (default `just check`)."""
    return str(work_value(cfg, entry, "validate_cmd", "just check"))


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
