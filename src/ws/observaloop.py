"""ws.observaloop — the single seam that drives observaloop as an MCP client.

observaloop exposes its automation **only** as the ``observaloop-mcp`` stdio MCP server (there is
no CLI), so this module is a thin, *gated*, best-effort ``fastmcp.Client`` adapter — the
control-plane analogue of ``ws.otel`` (which is the *export* seam). Everything observaloop-specific
— the MCP tool names, the manifest shape, the plugin install path — is confined here so the rest of
ws never learns observaloop's surface.

**Gating mirrors ``otel.py``.** ``is_available()`` is the predicate callers check first: it returns
``False`` (with a one-time, *correctly attributed* prereq hint) whenever observaloop can't be used,
distinguishing three failure modes so the hint never misdiagnoses: (a) ws itself lacks the optional
``fastmcp`` extra (``ws[mcp]``) needed to speak MCP → ``_MCP_EXTRA_HINT``; (b) no launch command
*resolves* (no config override + no plugin install) → ``_INSTALL_HINT`` (the observaloop-plugin
hint); (c) a command resolves and ``fastmcp`` is importable but the stdio server won't *reach* /
ping → ``_UNREACHABLE_HINT``. Every wrapper (``ensure_profile`` / ``up`` / ``down`` /
``endpoint_for`` / ``apply_dashboards`` / ``import_dashboards`` /
``apply_collector_preset``) is **best-effort**: observaloop or docker absent, or
the MCP call failing, becomes a logged warning + a ``None`` sentinel — **never** a raise, **never**
a block. ``import ws.observaloop`` is always safe: ``fastmcp`` is imported lazily inside the
client builder, so module import pulls in nothing optional.

Launch-command resolution prefers an explicit config override (``observaloop.command``); otherwise
it discovers the newest plugin install under ``~/.claude/plugins/cache/observaloop/observaloop/*/``
and runs it via ``uv run --directory <that> observaloop-mcp``. When neither resolves, the seam is
*unavailable* and every wrapper no-ops.
"""

from __future__ import annotations

import asyncio
import copy
import os
import shlex
from pathlib import Path
from typing import Any

from . import config

# Three distinct, correctly-attributed prereq hints (case b/a/c of ``is_available``). Each is shown
# at most once per process and names *its own* fix without crashing — the observaloop analogue of
# ``otel._INSTALL_HINT``. Conflating them (the original single hint) misdiagnosed a missing ws[mcp]
# extra as a missing observaloop plugin, so the cases are kept separate on purpose.

# (b) No launch command resolves: no ``observaloop.command`` override **and** no plugin install.
# observaloop ships as a Claude Code plugin whose automation lives behind the ``observaloop-mcp``
# stdio server; enabling routing without it present must degrade to a no-op, not an error.
_INSTALL_HINT = (
    "observaloop not found — telemetry routing is OFF. Install the plugin and ensure its MCP "
    "server is available (it runs as `observaloop-mcp` over stdio), or set `observaloop.command` "
    "in ws config to the launch argv. See the observaloop plugin docs."
)

# (a) ws itself was installed without the optional ``mcp`` extra, so ``fastmcp`` (the MCP client
# library) can't be imported — observaloop may well be installed; the gap is on the ws side. Do NOT
# blame observaloop here.
_MCP_EXTRA_HINT = (
    "ws was installed without the optional 'mcp' extra needed to talk to observaloop — telemetry "
    "routing is OFF. Reinstall ws with it: `uv tool install 'ws[otel,mcp]'` (or "
    "`pip install 'ws[mcp]'`)."
)

# (c) A command resolves and ``fastmcp`` is importable, but the stdio server can't be reached
# (uv/observaloop/docker absent, or the MCP handshake/ping failed) — both ws and the plugin are
# present; the *stack* isn't up.
_UNREACHABLE_HINT = (
    "observaloop found but its `observaloop-mcp` MCP server could not be reached — telemetry "
    "routing is OFF. Is the stack up? Is docker running? (the server runs as `observaloop-mcp` "
    "over stdio)."
)

# Where Claude Code caches plugin installs: one directory per installed version. Module-level so
# tests can repoint discovery at a temp tree without touching the real cache.
_PLUGIN_BASE = "~/.claude/plugins/cache/observaloop/observaloop"

# The stdio MCP tool names this seam drives (observaloop's profile/grafana surface). Confined here
# so observaloop's tool vocabulary never leaks into the rest of ws.
_TOOL_PROFILE_CREATE = "profile_create"
_TOOL_PROFILE_UP = "profile_up"
_TOOL_PROFILE_DOWN = "profile_down"
_TOOL_PROFILE_STATUS = "profile_status"
_TOOL_PROFILE_IMPORT = "profile_import"
_TOOL_GRAFANA_APPLY = "grafana_apply_dashboard"
_TOOL_VISUALIZER_STATUS = "visualizer_status"
_TOOL_COLLECTOR_GET_CONFIG = "collector_get_config"
_TOOL_COLLECTOR_SET_CONFIG = "collector_set_config"

# One-time-hint guard: like otel, we surface the install/prereq hint at most once per process so an
# unavailable observaloop doesn't spam the log on every wrapper call.
_hint_shown = False


class _Unavailable(Exception):
    """Internal: raised when no launch command resolves — caught by ``_invoke`` to no-op + hint."""


# ---- launch-command resolution ----------------------------------------------


def _version_key(name: str) -> tuple[int, ...]:
    """Sortable key for a plugin version dir name (``0.2.1`` → ``(0, 2, 1)``).

    Non-numeric segments sort lowest (``-1``) so a malformed dir never wins ``max()`` over a real
    semver. Keeps the "newest install" pick robust without a packaging dependency."""
    parts: list[int] = []
    for tok in name.split("."):
        try:
            parts.append(int(tok))
        except ValueError:
            parts.append(-1)
    return tuple(parts)


def _newest_plugin_dir() -> Path | None:
    """The highest-version observaloop plugin install dir, or ``None`` when nothing is installed.

    Globs the per-version subdirs of ``_PLUGIN_BASE`` and picks the greatest by ``_version_key``.
    Absent base / no version dirs → ``None`` (the unavailable path)."""
    base = Path(_PLUGIN_BASE).expanduser()
    if not base.is_dir():
        return None
    candidates = [p for p in base.iterdir() if p.is_dir()]
    if not candidates:
        return None
    return max(candidates, key=lambda p: _version_key(p.name))


def _resolve_command(cfg=None) -> list[str] | None:
    """Resolve the ``observaloop-mcp`` launch argv, or ``None`` when it can't be resolved.

    Prefers an explicit ``observaloop.command`` config override (a string → ``shlex.split``, or a
    list → used verbatim); else discovers the newest plugin install and runs it under its own env
    via ``uv run --directory <install> observaloop-mcp``. ``None`` is the single "unavailable"
    signal every caller keys off."""
    override = config.observaloop_cfg(cfg).get("command")
    if override:
        if isinstance(override, str):
            return shlex.split(override)
        return [str(tok) for tok in override]
    plugin_dir = _newest_plugin_dir()
    if plugin_dir is not None:
        return ["uv", "run", "--directory", str(plugin_dir), "observaloop-mcp"]
    return None


# ---- fastmcp client (lazy; the only optional-import boundary) ----------------


def _fastmcp_importable() -> bool:
    """Whether the optional ``fastmcp`` extra (``ws[mcp]``) is importable — checked lazily so module
    import never pulls it in.

    Uses ``importlib.util.find_spec`` (locates without executing) so the probe has no import side
    effects and stays cheap; a missing spec or any lookup error → ``False``. This is what
    ``is_available`` keys off to attribute a missing-extra failure to ws itself (case a) rather than
    misdiagnosing it as an absent observaloop plugin."""
    import importlib.util

    try:
        return importlib.util.find_spec("fastmcp") is not None
    except (ImportError, ValueError):
        return False


def _build_client(command: list[str]):
    """Build a ``fastmcp.Client`` over a stdio transport to ``command``.

    The spawned server's stderr is redirected to ``/dev/null`` via ``log_file=Path(os.devnull)``
    so that FastMCP's startup banner and uvicorn/anyio INFO lines never reach the user's terminal.
    ``GRPC_VERBOSITY=NONE`` in the subprocess env suppresses the gRPC C-core
    ``FD from fork parent still in poll list`` warning that appears when the grpc channel is
    created in a subprocess. Neither change affects the adapter's own ``_warn_once``/logger path,
    which runs in the ws process and is entirely separate.

    ``fastmcp`` is imported here and nowhere else, so ``import ws.observaloop`` stays free of the
    optional ``ws[mcp]`` extra. ``command[0]`` is the executable, ``command[1:]`` its args (e.g.
    ``uv run --directory <dir> observaloop-mcp``)."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    # Quiet env: suppress gRPC C-core fork-fd warning in the spawned process.
    # These are merged with mcp's get_default_environment() (HOME, PATH, …) — they don't
    # replace it — so the subprocess still has a sane PATH for uv/observaloop-mcp.
    _QUIET_ENV = {"GRPC_VERBOSITY": "NONE", "GRPC_TRACE": ""}

    transport = StdioTransport(
        command=command[0],
        args=command[1:],
        env=_QUIET_ENV,
        log_file=Path(os.devnull),
    )
    return Client(transport)


async def _call_tool(command: list[str], tool: str, args: dict[str, Any]) -> Any:
    """Spawn the stdio server, call ``tool`` with ``args``, and return the structured result data.

    The async fastmcp seam: ``async with client`` connects (launching the stdio subprocess), then
    ``call_tool`` round-trips. Returns ``CallToolResult.data`` (the tool's structured dict). This is
    the function unit-tests fake to drive the wrappers without a live server."""
    client = _build_client(command)
    async with client:
        result = await client.call_tool(tool, args)
    return result.data


async def _ping(command: list[str]) -> bool:
    """Best-effort reachability probe: connect to the stdio server and ``ping``.

    ``True`` only when the server launches and answers. Any failure (uv/observaloop/docker absent,
    handshake error) propagates as an exception for ``is_available`` to treat as unreachable."""
    client = _build_client(command)
    async with client:
        return await client.ping()


def _run(coro):
    """Drive an async coroutine to completion in a private event loop (``asyncio.run``).

    ws is a synchronous CLI with no ambient loop, so a fresh loop per call is correct and keeps the
    async fastmcp client fully behind a sync surface."""
    return asyncio.run(coro)


# ---- gating + best-effort dispatch ------------------------------------------


def _warn_once(hint: str) -> None:
    """Emit the install/prereq hint through the log pipeline at most once per process."""
    global _hint_shown
    if _hint_shown:
        return
    from . import log  # lazy: keep ws.observaloop import free of the log↔config cycle (cf. otel)

    log.get_logger(__name__).warning("observaloop_install_hint", hint=hint)
    _hint_shown = True


def is_available(cfg=None) -> bool:
    """Whether observaloop is usable: a command resolves, ``fastmcp`` is importable, **and** the
    server answers a ping.

    The gate callers check first (mirrors ``otel.is_active``). Returns ``False`` — surfacing the
    one-time, *correctly-attributed* prereq hint — in three distinct cases, checked in order so each
    blames the right thing:

    * **(b)** no command resolves (no override + no plugin install) → ``_INSTALL_HINT``.
    * **(a)** a command resolves but ws lacks the ``ws[mcp]`` extra (``fastmcp`` not importable) →
      ``_MCP_EXTRA_HINT``. Checked *before* probing, so a missing extra is never misread as an
      unreachable/absent observaloop, and the probe (which would ``ImportError``) is skipped.
    * **(c)** command + ``fastmcp`` present, but the stdio server can't be reached
      (uv/observaloop/docker absent, handshake failure) → ``_UNREACHABLE_HINT``.

    Order matters: command-resolution is checked first so the plugin-absent case (b) short-circuits
    before the extra check, keeping each hint unambiguous. Best-effort: the reachability probe never
    raises out of here."""
    command = _resolve_command(cfg)
    if command is None:
        _warn_once(_INSTALL_HINT)
        return False
    if not _fastmcp_importable():
        _warn_once(_MCP_EXTRA_HINT)
        return False
    try:
        reachable = bool(_run(_ping(command)))
    except Exception:
        reachable = False
    if not reachable:
        _warn_once(_UNREACHABLE_HINT)
    return reachable


def _invoke(tool: str, args: dict[str, Any], *, cfg=None) -> Any | None:
    """Best-effort dispatch of one MCP ``tool`` call → its structured data, or ``None`` on any miss.

    Resolves the launch command, runs the async call in a private loop, and returns the tool's data
    dict. Unavailable (no command) → one-time hint + ``None``; a launch/handshake/call failure → a
    logged warning + ``None``. observaloop's own soft-failures (tools that return ``{"error": …}``
    rather than raising) are surfaced as a warning but the dict is still returned for the caller to
    inspect. **Never** raises and **never** blocks — the contract every wrapper relies on."""
    from . import log  # lazy (see _warn_once)

    logger = log.get_logger(__name__)
    command = _resolve_command(cfg)
    if command is None:
        _warn_once(_INSTALL_HINT)
        return None
    try:
        data = _run(_call_tool(command, tool, args))
    except Exception as exc:  # uv/observaloop absent, handshake error, tool raised, timeout, …
        logger.warning("observaloop_call_failed", tool=tool, error=str(exc))
        return None
    if isinstance(data, dict) and data.get("error"):
        logger.warning("observaloop_tool_error", tool=tool, error=str(data["error"]))
    return data


# ---- thin sync wrappers (the public seam) -----------------------------------


def ensure_profile(name: str, cfg=None) -> dict | None:
    """Ensure observaloop profile ``name`` exists (``profile_create``); idempotent server-side.

    Returns the profile manifest dict on success, ``None`` when observaloop is unavailable or the
    call fails."""
    return _invoke(_TOOL_PROFILE_CREATE, {"name": name}, cfg=cfg)


def up(name: str, cfg=None) -> dict | None:
    """Bring profile ``name``'s collector up (``profile_up``). ``None`` when unavailable/failed."""
    return _invoke(_TOOL_PROFILE_UP, {"name": name}, cfg=cfg)


def down(name: str, cfg=None) -> dict | None:
    """Take profile ``name``'s collector down (``profile_down``). ``None`` when unavailable."""
    return _invoke(_TOOL_PROFILE_DOWN, {"name": name}, cfg=cfg)


def endpoint_for(name: str, protocol: str, cfg=None) -> str | None:
    """The profile's OTLP endpoint for ``name``, **protocol-matched** to ``protocol``.

    observaloop's instrumentation env only ever returns the HTTP endpoint, so we read the profile's
    manifest (via ``profile_status``) and pick the port for the requested transport: ``grpc`` →
    ``otlp_grpc_port`` (returned scheme-less, ``localhost:<port>``, matching observaloop's own grpc
    form); anything else (``http/protobuf``) → ``otlp_http_port`` (``http://localhost:<port>``).
    ``None`` when observaloop is unavailable, the profile/port is unknown, or the call fails."""
    status = _invoke(_TOOL_PROFILE_STATUS, {"name": name}, cfg=cfg)
    if not isinstance(status, dict):
        return None
    manifest = status.get("manifest") or {}
    if protocol == config.OTEL_PROTOCOL_GRPC:
        port = manifest.get("otlp_grpc_port")
        return f"localhost:{port}" if port else None
    port = manifest.get("otlp_http_port")
    return f"http://localhost:{port}" if port else None


def visualizer_status(cfg=None) -> dict | None:
    """Report the human-facing visualizer backend + reachability (``visualizer_status``).

    Returns the status dict (e.g. ``{"visualizer": "grafana", "reachable": True, …}``) or ``None``
    when observaloop is unavailable/failed. Callers gate dashboard installs on
    ``status.get("reachable")``: the ``grafana_*`` tools only register when Grafana is the
    configured, reachable visualizer, so applying a dashboard without it is a guaranteed miss."""
    return _invoke(_TOOL_VISUALIZER_STATUS, {}, cfg=cfg)


def profile_status(name: str, cfg=None) -> dict | None:
    """Return profile ``name``'s raw status dict (``profile_status``), or ``None`` when unavailable.

    The public analogue of the internal ``_invoke(_TOOL_PROFILE_STATUS, …)`` call used by
    ``endpoint_for``.  CLI commands (e.g. ``ws observaloop status``) call this to display
    up/down state and manifest info without touching private seams."""
    return _invoke(_TOOL_PROFILE_STATUS, {"name": name}, cfg=cfg)


def apply_dashboards(dashboard: dict, cfg=None) -> dict | None:
    """Create/update a Grafana dashboard (``grafana_apply_dashboard``); thin pass-through (Phase C).

    Returns the tool result (uid + url) or ``None`` when unavailable/failed."""
    return _invoke(_TOOL_GRAFANA_APPLY, {"dashboard": dashboard}, cfg=cfg)


def import_dashboards(name: str, repo_dir: str | None = None, cfg=None) -> dict | None:
    """Import a repo's committed ``.observaloop/`` into profile ``name`` (``profile_import``).

    Thin pass-through used by Phase C; ``None`` when unavailable/failed."""
    return _invoke(_TOOL_PROFILE_IMPORT, {"name": name, "repo_dir": repo_dir}, cfg=cfg)


# ---- collector preset (metrics reshape) -------------------------------------


def _parse_collector_yaml(text: str) -> dict:
    """Parse a collector-config YAML **string** into a plain dict (repo's ruamel parser).

    The live ``collector_get_config`` returns the OTel config as a YAML string under ``config``;
    the merge needs a dict. Uses ``YAML(typ="safe")`` — the same loader ``rig`` uses for the preset
    asset (pyyaml is not a dependency). A non-mapping document degrades to ``{}`` so the caller's
    best-effort contract holds."""
    from ruamel.yaml import YAML  # lazy: ruamel is a core dep but keep module import minimal

    parsed = YAML(typ="safe").load(text)
    return parsed if isinstance(parsed, dict) else {}


def _dump_collector_yaml(config_dict: dict) -> str:
    """Serialize a merged collector config dict back to a YAML **string** (repo's ruamel parser).

    The MCP ``collector_set_config(config: str, profile)`` requires a YAML string — passing the
    merged dict makes pydantic reject it (``Input should be a valid string``), so the apply silently
    no-ops. Dump to a string so the set actually lands."""
    import io

    from ruamel.yaml import YAML  # lazy (see _parse_collector_yaml)

    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    buf = io.StringIO()
    yaml.dump(config_dict, buf)
    return buf.getvalue()


def _unwrap_collector(raw: dict) -> dict:
    """The collector config dict out of a ``collector_get_config`` result.

    observaloop returns the OTel config under a ``config`` key — as a YAML **string** live (parsed
    to a dict here), or as a nested dict (faked tests / older shapes). Accept both, plus a bare
    config dict (no ``config`` key), so the merge always operates on a dict."""
    inner = raw.get("config")
    if isinstance(inner, str):
        return _parse_collector_yaml(inner)
    return inner if isinstance(inner, dict) else raw


def _merge_metrics_preset(collector: dict, preset: dict) -> dict:
    """A **new** collector config with the metrics preset spliced in — never mutates ``collector``.

    Deep-copies the fetched config, then (a) merges the preset's processor *definitions* into the
    top-level ``processors`` map (adding strip_instance / promote_ws_attrs / deltatocumulative
    alongside the profile's existing ones) and (b) replaces *only* the ``metrics`` pipeline's
    ``processors`` list with the preset's ordered names. The metrics pipeline's receivers/exporters
    (e.g. ``otlp/lgtm``) and the traces/logs pipelines are left untouched."""
    merged = copy.deepcopy(collector)

    processors = dict(merged.get("processors") or {})
    processors.update(preset.get("processors") or {})
    merged["processors"] = processors

    service = dict(merged.get("service") or {})
    pipelines = dict(service.get("pipelines") or {})
    metrics = dict(pipelines.get("metrics") or {})
    metrics["processors"] = list(preset.get("metrics_pipeline_processors") or [])
    pipelines["metrics"] = metrics
    service["pipelines"] = pipelines
    merged["service"] = service
    return merged


def apply_collector_preset(profile: str, preset: dict, cfg=None) -> dict | None:
    """Merge ``preset`` into profile ``profile``'s collector config and set it back (best-effort).

    The metrics-reshape analogue of ``apply_dashboards``: fetch the profile collector's current
    config (``collector_get_config``), splice the preset's processors + metrics-pipeline ordering
    into a fresh copy via ``_merge_metrics_preset``, and write it back (``collector_set_config``).
    Merge-and-set (rather than per-processor patching) is deterministic — the resulting pipeline is
    a pure function of the current config + preset.

    The collector config crosses the MCP seam as a YAML **string** both ways: the fetched config is
    parsed from YAML to a dict for the merge (``_unwrap_collector``), and the merged dict is
    serialized back to a YAML string before ``collector_set_config`` — whose signature is
    ``collector_set_config(config: str, profile)``. Passing the dict directly makes the server
    reject it (``Input should be a valid string``) and the apply silently no-ops.

    Best-effort + graceful like every other wrapper: observaloop unavailable, or the get returning
    no usable config dict, → a logged/no-op ``None`` (the set is skipped, never a half-applied
    config); a failing set → ``None``. **Never** raises, **never** blocks."""
    current = _invoke(_TOOL_COLLECTOR_GET_CONFIG, {"profile": profile}, cfg=cfg)
    if not isinstance(current, dict):
        return None
    merged = _merge_metrics_preset(_unwrap_collector(current), preset)
    config_yaml = _dump_collector_yaml(merged)
    return _invoke(_TOOL_COLLECTOR_SET_CONFIG, {"config": config_yaml, "profile": profile}, cfg=cfg)
