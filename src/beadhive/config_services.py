"""Typed accessors for runtime, host, telemetry, and service domains."""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from .config_binding import FacadeBinding

_config = FacadeBinding(f"{__package__}.config")
KNOWN_HARNESSES: tuple[str, ...] = ()


def bind(api) -> None:
    global KNOWN_HARNESSES
    _config.bind(api)
    KNOWN_HARNESSES = tuple(d.name for d in api._seat_runners())


def load():
    return _config.load()


def _env(field):
    return _config._env(field)


def layered_flag(cfg, entry, section, key="enabled", default=False):
    return _config.layered_flag(cfg, entry, section, key, default)


def home():
    return _config.home()


def worktrees_ephemeral(cfg=None) -> bool:
    return bool(worktrees_cfg(cfg).get("ephemeral", True))


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
    owner = _config.gh_login(cwd)
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


def host_dispatch_cfg(cfg=None):
    """The `host.dispatch:` subsection (or {}) — unattended-dispatch supervision, per-HOST
    (bh-e7r9q.4/.5). Not layered per-hive like `work.dispatch`: which supervisor exists is a
    fact about this machine."""
    return host_cfg(cfg).get("dispatch", {}) or {}


def dispatch_supervisor_backend(cfg=None) -> str:
    """`host.dispatch.backend` — which `beadhive.dispatch_supervisor` backend installs/starts/
    persists the per-hive dispatch loop. Default 'systemd' (the only one implemented)."""
    return str(host_dispatch_cfg(cfg).get("backend", "systemd") or "systemd")


def dispatch_max_epics_in_flight(cfg=None) -> int:
    """`host.dispatch.max_epics_in_flight` — how many `bh work loop <epic>` children the
    hive-level picker (`bh host dispatch run`) runs at once. Default 3. Values below 1 clamp
    to 1 (a cap of 0 would be a picker that can never dispatch)."""
    try:
        return max(int(host_dispatch_cfg(cfg).get("max_epics_in_flight", 3)), 1)
    except (TypeError, ValueError):
        return 3


def dispatch_hive_poll_interval(cfg=None) -> float:
    """`host.dispatch.poll_interval` — seconds the hive-level picker sleeps between passes.
    Default 10.0."""
    try:
        value = float(host_dispatch_cfg(cfg).get("poll_interval", 10.0))
    except (TypeError, ValueError):
        return 10.0
    return value if value > 0 else 10.0


def dispatch_stale_after_seconds(cfg=None) -> float:
    """`host.dispatch.stale_after_seconds` — doctor flags a RUNNING dispatch loop as stalled
    when no pass has landed in this many seconds. Default 900 (15 min)."""
    try:
        value = float(host_dispatch_cfg(cfg).get("stale_after_seconds", 900.0))
    except (TypeError, ValueError):
        return 900.0
    return value if value > 0 else 900.0


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
def harness_name(cfg=None, entry=None) -> str:
    """Which agent harness execs the seat process: ``claude`` (default) or ``opencode``.
    ``BH_HARNESS`` env wins, then the per-hive ``entry['harness']`` override, then global
    config ``harness``, else ``claude``."""
    env = _config._Env().harness
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
    return _config._Env().image_manifest


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


def repowise_cfg(cfg=None):
    """The top-level ``repowise`` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("repowise", {}) or {}


def repowise_enabled(cfg, entry=None) -> bool:
    """Whether the optional repowise index integration is enabled for this hive."""
    return layered_flag(cfg, entry, "repowise")


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
    override = archive_cfg(cfg).get("dir")
    if override:
        return Path(str(override)).expanduser()
    return Path(_config._identity_module().workspace_root()) / ".archived"


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


# ---- agent-steering alerts ----------------------------------------------------


def alerts_cfg(cfg=None):
    """The host-local ``alerts`` section (or {})."""
    cfg = cfg if cfg is not None else load()
    return cfg.get("alerts", {}) or {}


def alerts_worktree_cap_mb(cfg=None) -> int:
    """Per-hive managed-worktree footprint cap in MB (0 disables, default 5120)."""
    return int(alerts_cfg(cfg).get("worktree_cap_mb", 5120))


def alerts_disk_free_floor_mb(cfg=None) -> int:
    """Host free-disk floor in MB (0 disables, default 10240)."""
    return int(alerts_cfg(cfg).get("disk_free_floor_mb", 10240))


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


__all__ = [
    "dolt_cfg",
    "beads_cfg",
    "beads_engine",
    "worktrees_cfg",
    "managed_repos",
    "hq_cfg",
    "gh_login",
    "hq_remote",
    "host_cfg",
    "host_lease_cfg",
    "host_lease_renew_interval",
    "host_lease_ttl",
    "host_dispatch_cfg",
    "dispatch_supervisor_backend",
    "dispatch_max_epics_in_flight",
    "dispatch_hive_poll_interval",
    "dispatch_stale_after_seconds",
    "log_cfg",
    "log_format",
    "log_level",
    "KNOWN_HARNESSES",
    "harness_name",
    "otel_cfg",
    "otel_enabled",
    "otel_endpoint",
    "otel_hive",
    "otel_role",
    "OTEL_PROTOCOL_GRPC",
    "OTEL_PROTOCOL_HTTP",
    "OTEL_PROTOCOLS",
    "otel_protocol",
    "otel_headers",
    "OTEL_METRICS_TEMPORALITY_ENV",
    "OTEL_TEMPORALITY_DELTA",
    "OTEL_TEMPORALITY_CUMULATIVE",
    "otel_metrics_temporality",
    "otel_genai_cfg",
    "otel_genai_model",
    "otel_genai_system",
    "_env_flag",
    "passthrough_cfg",
    "_pass_enabled",
    "bd_pass_enabled",
    "git_pass_enabled",
    "skip_setup_check",
    "image_manifest_override",
    "observaloop_cfg",
    "observaloop_profile",
    "observaloop_enabled",
    "_sanitize_profile_name",
    "observaloop_profile_name",
    "orca_cfg",
    "orca_enabled",
    "orca_worktrees_enabled",
    "orca_worktrees_fallback",
    "orca_data_path",
    "repowise_cfg",
    "repowise_enabled",
    "hitch_cfg",
    "hitch_enabled",
    "hitch_command",
    "hitch_repo",
    "hitch_config_dir_root",
    "archive_cfg",
    "archive_dir",
    "archive_window_days",
    "backup_cfg",
    "backup_hq_keep",
    "backup_hive_cap_mb",
    "backup_hive_rotate_keep",
    "backup_migrate_keep",
    "backup_total_warn_mb",
    "alerts_cfg",
    "alerts_worktree_cap_mb",
    "alerts_disk_free_floor_mb",
    "metadata_cfg",
    "metadata_ttl",
    "metadata_background_reload",
    "hub_cfg",
    "hub_sync_background",
    "hub_bulk_sync",
]
__all__.insert(0, "worktrees_ephemeral")
