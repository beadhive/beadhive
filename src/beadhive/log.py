"""ws.log — the structlog logging foundation (dual-mode + stdlib bridge).

One pipeline, two faces: a TTY gets ``ConsoleRenderer`` (rich/ANSI, human-friendly);
anything non-interactive gets ``JSONRenderer`` (one structured object per line, machine
-friendly). Which face is chosen comes from config — ``log.format`` (``auto`` | ``rich`` |
``json``, ``auto`` TTY-detects) and ``log.level`` — never from scattered call sites.

Diagnostics / lifecycle events flow through here onto **stderr**; command *results* (the
user-facing UX payload) stay on ``typer.echo`` / stdout. That split keeps machine-parseable
output (JSON logs on stderr) from colliding with the human result on stdout.

stdlib bridge: a single ``ProcessorFormatter`` on the root logger runs both structlog events
and foreign stdlib ``logging`` records through the same processor chain + renderer, so a
third-party library's ``logging.getLogger(...).info(...)`` lands in the same shape as ours.

OTel hook: ``add_open_telemetry_spans`` is wired into the chain now but **no-ops cleanly when
the OpenTelemetry SDK is absent** — it imports lazily and returns the event unchanged. SDK
init (providers/exporters) is a later, optional bead (cit.2 / ``ws[otel]``); nothing here
imports or requires opentelemetry.
"""

from __future__ import annotations

import logging
import sys
from typing import Any, TextIO

import structlog

from . import config

# Level-name → numeric, reusing structlog's own table so we accept the same aliases it does
# (warn/warning, exception/error, …). Unknown names fall back to INFO rather than raising —
# logging config should never crash the tool.
_NAME_TO_LEVEL = structlog.processors.NAME_TO_LEVEL
_DEFAULT_LEVEL = logging.INFO

# Module-level guard so repeated get_logger() / configure() calls are idempotent (the last
# explicit configure wins; lazy callers don't reconfigure underneath an explicit setup).
_configured = False


def add_open_telemetry_spans(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """Enrich the event with the active OTel span's ``trace_id`` / ``span_id`` for
    log↔trace correlation — but **no-op when OpenTelemetry is not installed** or there is no
    recording span. This is a pure hook: it never imports opentelemetry at module load and
    swallows the ImportError so ``get_logger()`` works with no otel present.
    """
    try:
        from opentelemetry import trace
    except Exception:  # pragma: no cover - otel absent is the default path (cit.2 adds it)
        return event_dict

    span = trace.get_current_span()
    ctx = span.get_span_context() if span is not None else None
    if ctx is None or not getattr(ctx, "is_valid", False):
        return event_dict

    event_dict["trace_id"] = format(ctx.trace_id, "032x")
    event_dict["span_id"] = format(ctx.span_id, "016x")
    return event_dict


def _resolve_level(level: str | int | None) -> int:
    """A level name/number → numeric logging level, defaulting to INFO on anything odd."""
    if level is None:
        return _DEFAULT_LEVEL
    if isinstance(level, int):
        return level
    return _NAME_TO_LEVEL.get(str(level).strip().lower(), _DEFAULT_LEVEL)


def _use_rich(fmt: str, stream: TextIO) -> bool:
    """Whether to render rich/ANSI: ``rich`` forces it, ``json`` forbids it, ``auto`` (or any
    unknown value) defers to whether the destination stream is an interactive TTY."""
    fmt = (fmt or "auto").strip().lower()
    if fmt == "rich":
        return True
    if fmt == "json":
        return False
    return bool(getattr(stream, "isatty", lambda: False)())


def _renderer(fmt: str, stream: TextIO):
    """The terminal processor for the chosen face: ConsoleRenderer (rich) or JSONRenderer."""
    if _use_rich(fmt, stream):
        return structlog.dev.ConsoleRenderer(colors=True)
    return structlog.processors.JSONRenderer()


def _safe_cfg():
    """Load ws config for log.* settings, tolerating its absence — logging must initialize
    even before ``ws config init`` has run, so a missing/broken config falls back to {}."""
    try:
        return config.load()
    except Exception:
        return {}


# Processors shared by structlog-native events and foreign stdlib records (the
# ProcessorFormatter foreign_pre_chain), in order. The otel hook sits here so both paths get
# span correlation once cit.2 lands; today it no-ops.
_SHARED_PROCESSORS = [
    structlog.contextvars.merge_contextvars,
    structlog.stdlib.add_log_level,
    structlog.processors.TimeStamper(fmt="iso", utc=True),
    structlog.processors.StackInfoRenderer(),
    structlog.processors.format_exc_info,
    add_open_telemetry_spans,
]


def configure(
    *,
    level: str | int | None = None,
    fmt: str | None = None,
    stream: TextIO | None = None,
) -> None:
    """(Re)configure the structlog + stdlib logging pipeline.

    ``level`` / ``fmt`` default to ``log.level`` / ``log.format`` from beadhive config when not
    given explicitly (tests force a face by passing ``fmt=``). ``stream`` defaults to stderr
    — diagnostics never touch stdout. Idempotent: safe to call repeatedly; the latest call
    wins and re-points the single root handler.
    """
    global _configured

    cfg = _safe_cfg()
    resolved_fmt = fmt if fmt is not None else config.log_format(cfg)
    resolved_level = _resolve_level(level if level is not None else config.log_level(cfg))
    out = stream if stream is not None else sys.stderr

    # structlog side: shared chain, then hand off to the stdlib ProcessorFormatter which owns
    # the actual rendering (so structlog + stdlib records render identically).
    structlog.configure(
        processors=[
            *_SHARED_PROCESSORS,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(resolved_level),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # stdlib side: one formatter, one stderr handler on the root logger. foreign_pre_chain
    # runs the shared processors over records that did NOT originate in structlog.
    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=_SHARED_PROCESSORS,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            _renderer(resolved_fmt, out),
        ],
    )
    handler = logging.StreamHandler(out)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(resolved_level)

    _configured = True


def get_logger(name: str | None = None) -> Any:
    """Return a structlog bound logger, configuring the pipeline on first use.

    Works with no OpenTelemetry installed (the span processor no-ops). Lazy configuration
    means importing ws never forces logging setup, but the first diagnostic call gets a fully
    wired stderr pipeline reflecting current config.
    """
    if not _configured:
        configure()
    return structlog.get_logger(name)
