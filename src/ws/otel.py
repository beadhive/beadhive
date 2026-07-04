"""ws.otel — gated OpenTelemetry SDK init (providers + OTLP exporters + log bridge).

This is the *init* seam for observability: it stands up Tracer / Meter / Logger providers on a
shared ``Resource`` (``service.name=ws`` + version + rig), wires each to an OTLP exporter
(endpoint from ``OTEL_EXPORTER_OTLP_ENDPOINT``) behind a batch processor, and bridges the
structlog/stdlib stream (cit.1's root-logger pipeline) into OTel logs via a ``LoggingHandler``.

**Gating is the whole point.** ``init()`` only does any of the above when *both* hold:

1. ``otel.enabled`` is true in config — **disabled by default**, so telemetry is strictly
   opt-in and nothing exports by accident; and
2. the OpenTelemetry SDK + OTLP exporter libs are importable (the ``ws[otel]`` extra).

Enabled-but-libs-absent is a **graceful no-op with an install hint** (a single warning through
the existing log pipeline) — never a crash. ``import ws.otel`` is always safe without the
extra: every opentelemetry import is lazy (inside ``_load_otel`` / ``init``), so module import
pulls in nothing optional.

Scope: this bead wires the SDK only. Emitting lifecycle spans/metrics is cit.3; the local LGTM
stack that receives the OTLP stream is cit.4.
"""

from __future__ import annotations

import atexit
import functools
import logging
import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from . import config

_SERVICE_NAME = "ws"

# Telemetry env stripped from a validation child's environment so a clean-checkout (or in-worktree)
# validation run never inherits — or exports through — the operator's otel setup. Every ``OTEL_*``
# var (incl. ``OTEL_EXPORTER_OTLP_ENDPOINT`` / ``OTEL_RESOURCE_ATTRIBUTES``) plus the observaloop
# profile selector are removed; ``OTEL_SDK_DISABLED=true`` is set so any OpenTelemetry SDK inside
# the validated code stays inert during validation.
_TELEMETRY_ENV_PREFIX = "OTEL_"
_TELEMETRY_ENV_KEYS = ("WS_OBSERVALOOP_PROFILE",)
_SDK_DISABLED_KEY = "OTEL_SDK_DISABLED"


def telemetry_neutral_env(base: dict[str, str] | None = None) -> dict[str, str]:
    """A copy of ``base`` (default ``os.environ``) scrubbed of telemetry config: every ``OTEL_*``
    var and ``WS_OBSERVALOOP_PROFILE`` are dropped and ``OTEL_SDK_DISABLED=true`` is forced on.

    Everything else (``PATH`` …) is preserved untouched. Used to spawn the rig's validation command
    (``ws work check`` / ``ws work submit``'s clean checkout) so the result never depends on, nor
    pollutes with, the operator's otel config — making ``check`` and ``submit`` agree regardless of
    the rig's ``otel.enabled`` / endpoint. The worktree overlay loader (``observaloop_env``) and the
    operator's own config both seed these vars into ``os.environ``, so without this the validation
    child would behave differently under an otel-enabled rig."""
    src = os.environ if base is None else base
    env = {
        k: v
        for k, v in src.items()
        if not k.startswith(_TELEMETRY_ENV_PREFIX) and k not in _TELEMETRY_ENV_KEYS
    }
    env[_SDK_DISABLED_KEY] = "true"
    return env

# Shown (once) when otel is enabled but the SDK/exporter libs aren't importable. Names the
# extra so the operator knows the exact fix — enabling without installing must not crash.
_INSTALL_HINT = (
    "otel.enabled is true but the OpenTelemetry SDK is not installed — "
    "telemetry export is OFF. Install the extra to enable it:  pip install 'ws[otel]'"
)

# Module guard: init() is idempotent — once providers are wired we don't re-stamp global
# providers or stack another LoggingHandler on the root logger.
_initialized = False

# Providers wired by init(), retained so the flush-on-exit handler can reach them. ws is a
# short-lived CLI and the batch span/log processors + periodic metric reader export on an interval,
# so without an explicit shutdown() (which force-flushes) the process exits before the batch drains
# and spans/metrics/logs are silently dropped.
_providers: tuple[Any, ...] = ()

# The atexit flush hook is registered at most once across re-inits: tests reset _initialized to
# re-wire, but this guard means we never stack duplicate handlers on the same shutdown() callable.
_atexit_registered = False


@dataclass
class _Otel:
    """The lazily-imported OpenTelemetry surface ``init`` wires together.

    Bundling every symbol behind one loader keeps all optional imports in a single seam: tests
    inject a fake ``_Otel`` (mocks) to assert wiring, or make ``_load_otel`` raise to exercise
    the absent-libs no-op — without the real extra installed in CI."""

    # signal API modules (carry set_*_provider)
    trace: Any
    metrics: Any
    logs: Any
    # shared
    Resource: Any
    # tracing
    TracerProvider: Any
    BatchSpanProcessor: Any
    OTLPSpanExporter: Any
    # metrics
    MeterProvider: Any
    PeriodicExportingMetricReader: Any
    OTLPMetricExporter: Any
    # DELTA preferred-temporality map ({instrument class: AggregationTemporality.DELTA}) for the
    # cumulative-prone metric kinds (Counter/Histogram/ObservableCounter); built in _load_otel so
    # the instrument-class imports stay on the gated path. Passed as the metric exporter's
    # preferred_temporality when delta is selected (the default for this short-lived CLI).
    metric_temporality_delta: Any
    # logs
    LoggerProvider: Any
    BatchLogRecordProcessor: Any
    OTLPLogExporter: Any
    LoggingHandler: Any


def _otlp_exporters(protocol: str):
    """Import + return the ``(span, metric, log)`` OTLP exporter classes for ``protocol``.

    ``grpc`` → the ``opentelemetry.exporter.otlp.proto.grpc.*`` exporters; ``http/protobuf`` →
    the ``...proto.http.*`` exporters. Both transports ship in the ``opentelemetry-exporter-otlp``
    extra, and both constructors accept ``endpoint=`` / ``headers=``. ``init`` has already
    validated ``protocol`` against ``config.OTEL_PROTOCOLS`` (so no silent grpc fallback); the
    ``else`` is the grpc default/back-compat branch."""
    if protocol == config.OTEL_PROTOCOL_HTTP:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    else:
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    return OTLPSpanExporter, OTLPMetricExporter, OTLPLogExporter


def _load_otel(protocol: str = config.OTEL_PROTOCOL_GRPC) -> _Otel:
    """Import the OpenTelemetry SDK + the OTLP exporter surface for ``protocol``, lazily.

    The exporter classes for all three signals are chosen by transport (``_otlp_exporters``);
    the rest of the SDK surface is transport-agnostic. Raises ``ImportError`` when the ``ws[otel]``
    extra is absent — the single import boundary so ``init`` can treat "otel not installed" as one
    catchable condition. Never called at module import time, so ``import ws.otel`` stays free of
    the optional dependency."""
    from opentelemetry import _logs as logs
    from opentelemetry import metrics, trace
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import Counter, Histogram, MeterProvider, ObservableCounter
    from opentelemetry.sdk.metrics.export import (
        AggregationTemporality,
        PeriodicExportingMetricReader,
    )
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    OTLPSpanExporter, OTLPMetricExporter, OTLPLogExporter = _otlp_exporters(protocol)

    # Map only the cumulative-prone kinds to DELTA; the SDK fills the rest of its instrument map
    # from its cumulative default, so UpDownCounter / ObservableUpDownCounter / ObservableGauge
    # stay cumulative (gauges have no temporality choice) without being listed here.
    delta = AggregationTemporality.DELTA
    metric_temporality_delta = {Counter: delta, Histogram: delta, ObservableCounter: delta}

    return _Otel(
        trace=trace,
        metrics=metrics,
        logs=logs,
        Resource=Resource,
        TracerProvider=TracerProvider,
        BatchSpanProcessor=BatchSpanProcessor,
        OTLPSpanExporter=OTLPSpanExporter,
        MeterProvider=MeterProvider,
        PeriodicExportingMetricReader=PeriodicExportingMetricReader,
        OTLPMetricExporter=OTLPMetricExporter,
        metric_temporality_delta=metric_temporality_delta,
        LoggerProvider=LoggerProvider,
        BatchLogRecordProcessor=BatchLogRecordProcessor,
        OTLPLogExporter=OTLPLogExporter,
        LoggingHandler=LoggingHandler,
    )


def _ws_version() -> str:
    """Best-effort package version for the Resource; never raise on a missing dist."""
    try:
        import importlib.metadata

        return importlib.metadata.version("ws")
    except Exception:  # pragma: no cover - dist metadata always present in practice
        return "0.0.0"


def _resource_attributes(cfg) -> dict[str, str]:
    """The Resource identity, stamped once at ``init()`` and shared by every signal
    (spans/metrics/logs). Always carries ``service.name``/``service.version``; enriches with the
    process's low-cardinality identity (the ``ws.provider``/``ws.org``/``ws.repo`` triplet,
    ``ws.rig``, ``ws.role``, ``ws.worktree``, ``observaloop.profile``) when each is known. Every
    enrichment attribute is **omitted when empty** — never a blank value. Built only inside ``init``
    (gated), so the off-path stays zero-cost and free of the worktree/identity import."""
    attrs = {
        "service.name": _SERVICE_NAME,
        "service.version": _ws_version(),
    }
    _enrich_resource(attrs, cfg)
    return attrs


def _enrich_resource(attrs: dict[str, str], cfg) -> None:
    """Add the low-cardinality identity attributes to ``attrs`` in place (each omitted when empty).

    ``worktree`` is imported lazily here (only ever reached inside gated ``init``) so importing
    ``ws.otel`` never pulls in typer/worktree on the off-path. The provider/org/repo triplet and the
    worktree leaf are resolved from cwd in one side-effect-free call; ``ws.rig`` falls back to the
    rig prefix derived from that triplet when ``otel.rig`` is unset; the ephemeral ``verify-``
    clean-checkout worktrees are excluded from ``ws.worktree`` (they aren't a real seat)."""
    from . import worktree  # lazy: keep ws.otel import free of typer/worktree on the off-path

    triplet, leaf = worktree.cwd_identity(cfg)
    if triplet:
        provider, org, repo = triplet
        attrs["ws.provider"] = provider
        attrs["ws.org"] = org
        attrs["ws.repo"] = repo
    rig = config.otel_rig(cfg) or _derived_rig(cfg, triplet)
    if rig:
        attrs["ws.rig"] = rig
    role = config.otel_role(cfg)
    if role:
        attrs["ws.role"] = role
    if leaf and not leaf.startswith(worktree.VERIFY_LEAF_PREFIX):
        attrs["ws.worktree"] = leaf
    profile = config.observaloop_profile(cfg)
    if profile:
        attrs["observaloop.profile"] = profile


def _derived_rig(cfg, triplet) -> str:
    """Auto-derive ``ws.rig`` from the managed-repo *prefix* (the rig's canonical name) matching
    ``triplet`` — so telemetry is rig-attributable without explicit ``otel.rig`` config. Falls back
    to the repo name when the rig isn't registered (matching the synthesized-entry convention);
    ``""`` when there's no triplet (the attribute is then omitted)."""
    if not triplet:
        return ""
    provider, org, repo = (str(x) for x in triplet)
    for e in config.managed_repos(cfg):
        if (str(e["provider"]), str(e["org"]), str(e["repo"])) == (provider, org, repo):
            return str(e.get("prefix", "") or repo)
    return repo


# Per-signal OTLP path each http exporter needs appended to the configured base endpoint. The
# http exporter uses an explicit ``endpoint=`` VERBATIM (it only appends this path when deriving
# the endpoint from ``OTEL_EXPORTER_OTLP_ENDPOINT`` itself), so a bare base would POST to ``/`` and
# 404. grpc has no per-signal path and keeps the bare base.
_OTLP_SIGNAL_PATHS = {"traces": "/v1/traces", "metrics": "/v1/metrics", "logs": "/v1/logs"}


def _signal_endpoint(base: str, protocol: str, signal: str) -> str:
    """The exporter ``endpoint`` for one ``signal`` (``traces``/``metrics``/``logs``) given the
    configured ``base``.

    For ``http/protobuf`` the explicit endpoint is used verbatim by the exporter, so the per-signal
    ``/v1/<signal>`` path is appended here (trailing slash stripped first) — otherwise traces /
    metrics / logs POST to the bare root and 404. Guarded against double-append: when the operator
    already pointed ``base`` at the right ``/v1/<signal>`` it's returned unchanged. grpc keeps the
    bare base (no path — the grpc exporter routes by RPC method, not URL path)."""
    if protocol != config.OTEL_PROTOCOL_HTTP:
        return base
    path = _OTLP_SIGNAL_PATHS[signal]
    trimmed = base.rstrip("/")
    if trimmed.endswith(path):
        return trimmed  # operator already supplied the signal path — don't double-append
    return trimmed + path


def _exporter_kwargs(cfg, protocol: str, signal: str) -> dict[str, Any]:
    """Constructor kwargs for one signal's OTLP exporter: ``endpoint`` (from
    ``OTEL_EXPORTER_OTLP_ENDPOINT``/config) and ``headers`` (auth/routing for hosted endpoints).

    Each is included only when set, so an unconfigured exporter is constructed with no kwargs and
    falls back to its own defaults (the http default endpoint already carries the signal path).
    ``headers`` are threaded identically across traces/metrics/logs; the ``endpoint`` is made
    per-signal for ``http/protobuf`` (``<base>/v1/<signal>``) and stays the bare base for grpc —
    see ``_signal_endpoint``."""
    kwargs: dict[str, Any] = {}
    endpoint = config.otel_endpoint(cfg)
    if endpoint:
        kwargs["endpoint"] = _signal_endpoint(endpoint, protocol, signal)
    headers = config.otel_headers(cfg)
    if headers:
        kwargs["headers"] = headers
    return kwargs


def _metric_exporter_kwargs(cfg, otel: _Otel, base: dict[str, Any]) -> dict[str, Any]:
    """The OTLP *metric* exporter's kwargs: the shared endpoint/headers plus — by default — a DELTA
    ``preferred_temporality`` map for Counter/Histogram/ObservableCounter (gauges + up/down counters
    stay cumulative via the SDK's defaults). ws is a short-lived CLI, so cumulative counters from
    each ephemeral process never accumulate; delta lets the collector sum across instances.

    The explicit preference is omitted (→ the SDK's cumulative default) when
    ``otel.metrics_temporality`` is ``cumulative``, or when the operator already set
    ``OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE`` — then the SDK's own env-based selection
    wins and we don't shadow it. Only the metric exporter is affected; traces/logs are untouched."""
    kwargs = dict(base)
    if os.environ.get(config.OTEL_METRICS_TEMPORALITY_ENV):
        return kwargs  # operator set the env var → defer to the SDK's env-based selection
    if config.otel_metrics_temporality(cfg) == config.OTEL_TEMPORALITY_DELTA:
        kwargs["preferred_temporality"] = otel.metric_temporality_delta
    return kwargs


def init(cfg=None) -> bool:
    """Initialize the OTel SDK **iff** enabled and the libs are present; else graceful no-op.

    Returns ``True`` when providers + OTLP exporters + the log bridge were wired, ``False`` for
    every no-op path (disabled, libs absent, or already initialized). Idempotent.
    """
    global _initialized, _providers

    from . import log  # local import: avoid a module-load cycle (log imports config too)

    logger = log.get_logger(__name__)

    if not config.otel_enabled(cfg):
        return False  # disabled by default — opt-in only
    if _initialized:
        return False  # providers already stamped; don't double-wire

    # Validate the transport up front, before touching the SDK, so a bad value fails with a clear
    # error regardless of whether the extra is installed — never a silent fallback to grpc.
    protocol = config.otel_protocol(cfg)
    if protocol not in config.OTEL_PROTOCOLS:
        raise ValueError(
            f"otel.protocol must be one of {list(config.OTEL_PROTOCOLS)}, got {protocol!r}"
        )

    try:
        otel = _load_otel(protocol)
    except ImportError:
        # Enabled but the extra isn't installed — warn with the install hint and no-op. Never
        # crash the tool just because someone flipped the flag without `pip install ws[otel]`.
        logger.warning("otel_install_hint", hint=_INSTALL_HINT)
        return False

    resource = otel.Resource.create(_resource_attributes(cfg))
    # endpoint + headers, per signal: headers are identical, but for http/protobuf each signal's
    # endpoint gets its own /v1/<signal> path (the http exporter uses an explicit endpoint
    # verbatim); grpc keeps the bare base for all three. See _signal_endpoint.

    # Traces: provider → BatchSpanProcessor(OTLP) → set as global tracer provider.
    tracer_provider = otel.TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        otel.BatchSpanProcessor(otel.OTLPSpanExporter(**_exporter_kwargs(cfg, protocol, "traces")))
    )
    otel.trace.set_tracer_provider(tracer_provider)

    # Metrics: provider with a periodic reader over the OTLP metric exporter (the metrics
    # analogue of a batch processor) → set as global meter provider. The metric exporter defaults
    # to DELTA temporality (this CLI is short-lived; see _metric_exporter_kwargs).
    metric_kwargs = _metric_exporter_kwargs(cfg, otel, _exporter_kwargs(cfg, protocol, "metrics"))
    metric_reader = otel.PeriodicExportingMetricReader(otel.OTLPMetricExporter(**metric_kwargs))
    meter_provider = otel.MeterProvider(resource=resource, metric_readers=[metric_reader])
    otel.metrics.set_meter_provider(meter_provider)

    # Logs: provider → BatchLogRecordProcessor(OTLP) → set as global logger provider, then
    # bridge cit.1's stdlib root logger (which structlog feeds) into OTel logs via a handler.
    logger_provider = otel.LoggerProvider(resource=resource)
    log_kwargs = _exporter_kwargs(cfg, protocol, "logs")
    logger_provider.add_log_record_processor(
        otel.BatchLogRecordProcessor(otel.OTLPLogExporter(**log_kwargs))
    )
    otel.logs.set_logger_provider(logger_provider)
    handler = otel.LoggingHandler(level=logging.NOTSET, logger_provider=logger_provider)
    logging.getLogger().addHandler(handler)

    # Retain the providers + register a flush-on-exit hook so this short-lived CLI drains its
    # batched spans/metrics/logs before the process exits (otherwise they're silently dropped).
    _providers = (tracer_provider, meter_provider, logger_provider)
    _instruments.clear()  # rebind metric instruments to the freshly-wired meter provider
    _initialized = True
    _register_flush_on_exit()
    logger.info(
        "otel_initialized",
        protocol=protocol,
        endpoint=config.otel_endpoint(cfg) or "<exporter-default>",
    )
    return True


def _register_flush_on_exit() -> None:
    """Register ``shutdown`` as an ``atexit`` hook — but at most once, even across re-inits.

    Only ``init``'s real-wiring path calls this, so the disabled / libs-absent no-ops never
    register a hook. The ``_atexit_registered`` guard means re-initializing (e.g. tests reset
    ``_initialized`` to re-wire) doesn't stack duplicate handlers."""
    global _atexit_registered
    if _atexit_registered:
        return
    atexit.register(shutdown)
    _atexit_registered = True


def shutdown() -> None:
    """Flush + shut down the wired providers so batched telemetry isn't dropped on exit.

    ``provider.shutdown()`` force-flushes the BatchSpanProcessor / PeriodicExportingMetricReader /
    BatchLogRecordProcessor, so a quick ``ws`` command's spans/metrics/logs reach the collector
    before the process exits. A no-op when ``init`` never wired real providers (off / libs-absent).
    Best-effort: an exporter failure on exit must not raise out of the atexit hook. Resets the init
    state so a fresh ``init`` can re-wire (useful for tests)."""
    global _initialized, _providers
    if not _initialized:
        return
    for provider in _providers:
        try:
            provider.shutdown()
        except Exception:  # pragma: no cover - never raise from the exit hook
            pass
    _providers = ()
    _instruments.clear()
    _initialized = False


# ---- emission surface (cit.3): gated tracer / meter + lifecycle metrics ------
#
# The hot-path side of observability: the run() subprocess seam and the `ws work` / `ws plan`
# verbs emit spans + metrics through these helpers. **Gating mirrors init():** until init() wires
# the real providers, every accessor returns a shared no-op shim — so emission is cheap and, just
# like the rest of this module, never imports opentelemetry. That keeps the default path (otel
# off, the ws[otel] extra possibly absent) zero-overhead and import-safe. ``is_active()`` is the
# fast predicate callers use to skip building span names/attributes entirely when telemetry is off.


class _NoopSpan:
    """A span that records nothing; also its own context manager so
    ``with tracer.start_as_current_span(...) as span`` is a zero-cost no-op when otel is off."""

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def set_attribute(self, *_a, **_k):
        pass

    def add_event(self, *_a, **_k):
        pass

    def set_status(self, *_a, **_k):
        pass

    def record_exception(self, *_a, **_k):
        pass

    def is_recording(self) -> bool:
        return False


class _NoopTracer:
    def start_as_current_span(self, _name, *_a, **_k):
        return _NOOP_SPAN


class _NoopInstrument:
    """A counter/histogram that drops every sample."""

    def add(self, *_a, **_k):
        pass

    def record(self, *_a, **_k):
        pass


class _NoopMeter:
    def create_counter(self, *_a, **_k):
        return _NOOP_INSTRUMENT

    def create_histogram(self, *_a, **_k):
        return _NOOP_INSTRUMENT


_NOOP_SPAN = _NoopSpan()
_NOOP_TRACER = _NoopTracer()
_NOOP_INSTRUMENT = _NoopInstrument()
_NOOP_METER = _NoopMeter()

# Instruments are created once per metric name against the real meter and cached, so we don't
# re-create — and let the SDK re-warn — on every emission. init() clears this so a fresh provider
# rebinds; the no-op path never touches it.
_instruments: dict[str, Any] = {}


def is_active() -> bool:
    """Whether the OTel SDK is wired (``init()`` succeeded). The zero-overhead gate: emitters
    check this first and skip building span names/attributes when telemetry is off."""
    return _initialized


def get_tracer(name: str = _SERVICE_NAME):
    """The active tracer, or a cheap no-op until ``init()`` wires a real provider. The no-op path
    never imports opentelemetry, so callers stay import-safe without the ``ws[otel]`` extra."""
    if not _initialized:
        return _NOOP_TRACER
    from opentelemetry import trace

    return trace.get_tracer(name)


def get_meter(name: str = _SERVICE_NAME):
    """The active meter, or a cheap no-op until ``init()`` wires a provider (cf. get_tracer)."""
    if not _initialized:
        return _NOOP_METER
    from opentelemetry import metrics

    return metrics.get_meter(name)


def get_current_span():
    """The active span from the OTel API, or the shared no-op span until ``init()`` wires a real
    provider. Mirrors get_tracer/get_meter: the off-path never imports opentelemetry, so boundary
    error handling stays import-safe + zero-cost without the ``ws[otel]`` extra."""
    if not _initialized:
        return _NOOP_SPAN
    from opentelemetry import trace

    return trace.get_current_span()


def span(name: str, attributes: dict[str, Any] | None = None):
    """Start ``name`` as the current span (a context manager). No-op + zero-cost when otel is off.
    The single span seam the run() subprocess wrapper and the ws work/plan verbs emit through."""
    return get_tracer().start_as_current_span(name, attributes=attributes or {})


def trace_verb(name: str):
    """Decorator wrapping a CLI verb body in a span named ``name`` — a no-op + zero-cost passthrough
    when otel is off. Signature-preserving (``functools.wraps``) so Typer still introspects the
    wrapped verb's original parameters."""

    def deco(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if not _initialized:
                return fn(*args, **kwargs)
            with get_tracer().start_as_current_span(name):
                return fn(*args, **kwargs)

        return wrapper

    return deco


def _epic_of(bead: str) -> str:
    """The molecule/epic id for a bead — everything before the LAST ``.`` (bd sub-id convention,
    e.g. ``ag-1.2.3`` → ``ag-1.2``), or ``""`` for a top-level bead with no ``.``."""
    epic, sep, _ = bead.rpartition(".")
    return epic if sep else ""


def set_bead(bead: str) -> None:
    """Stamp ``ws.bead`` (+ derived ``ws.epic``) onto the active span — the per-invocation identity
    the Resource can't carry (bead/epic are per-verb, not process-global). Call it inside a traced
    verb so the verb span (and its children) are filterable by bead/molecule, consistently with the
    bead/epic that already ride the lifecycle metrics. No-op + zero-cost when otel is off or there's
    no recording span; ``ws.epic`` is omitted for a top-level bead. Both are low-cardinality
    control-plane ids — safe to index, and never placed in a span NAME."""
    if not _initialized or not bead:
        return
    span = get_current_span()
    if not span.is_recording():
        return
    span.set_attribute("ws.bead", bead)
    epic = _epic_of(bead)
    if epic:
        span.set_attribute("ws.epic", epic)


def _instrument(kind: str, name: str, **kwargs):
    """Lazily create + cache the named counter/histogram against the active meter; the shared
    no-op instrument when otel is off (so no opentelemetry import, no allocation)."""
    if not _initialized:
        return _NOOP_INSTRUMENT
    inst = _instruments.get(name)
    if inst is None:
        inst = getattr(get_meter(), f"create_{kind}")(name, **kwargs)
        _instruments[name] = inst
    return inst


def record_merge_duration(seconds: float, attributes: dict[str, Any] | None = None) -> None:
    """Histogram of ``ws work merge`` wall-clock seconds (a bead land or a molecule land)."""
    _instrument(
        "histogram", "ws.work.merge.duration", unit="s", description="ws work merge wall time"
    ).record(seconds, attributes or {})


def count_bead_transition(transition: str, attributes: dict[str, Any] | None = None) -> None:
    """Counter of bead-lifecycle transitions (e.g. merged, molecule_landed, review_pending)."""
    attrs = {"ws.bead.transition": transition}
    if attributes:
        attrs.update(attributes)
    _instrument(
        "counter", "ws.work.bead.transitions", unit="1", description="bead lifecycle transitions"
    ).add(1, attrs)


def record_worktree_event(
    op: str, outcome: str = "ok", attributes: dict[str, Any] | None = None
) -> None:
    """Counter of worktree-lifecycle events tagged ``ws.worktree.op`` (create|remove|prune) +
    ``ws.worktree.outcome`` (ok|error). The worktree-fleet analogue of ``count_bead_transition``:
    the create (``_do_add`` chokepoint, verify- excluded) / remove / prune seams emit through here,
    so worktree churn is chartable. Callers pass ``ws.rig`` / ``ws.worktree`` in ``attributes``
    where known. No-op + zero overhead when otel is off — gated by ``_instrument`` (no opentelemetry
    import on the off-path)."""
    attrs = {"ws.worktree.op": op, "ws.worktree.outcome": outcome}
    if attributes:
        attrs.update(attributes)
    _instrument(
        "counter", "ws.worktree.events", unit="1", description="worktree lifecycle events"
    ).add(1, attrs)


def count_validation(passed: bool, attributes: dict[str, Any] | None = None) -> None:
    """Counter of validation runs, tagged pass/fail (the rig validation-command result)."""
    attrs = {"ws.validation.result": "pass" if passed else "fail"}
    if attributes:
        attrs.update(attributes)
    _instrument(
        "counter", "ws.work.validation.runs", unit="1", description="validation pass/fail"
    ).add(1, attrs)


def record_cli_invocation(command: str, outcome: str, seconds: float) -> None:
    """Invocation counter + latency histogram for the CLI command-entry seam.

    Emits ``ws.cli.invocations`` (counter, unit=1) and ``ws.cli.duration`` (histogram, unit=s)
    tagged with ``ws.cli.command`` (the top-level subcommand, e.g. ``work`` or ``config``) and
    ``ws.cli.outcome`` (``ok`` or ``error``). Mirrors the zero-cost contract of the other helpers:
    no-op + no opentelemetry import when otel is off. Called from the ``ctx.call_on_close`` hook
    registered in ``ws.cli._root`` after the subcommand completes."""
    attrs = {"ws.cli.command": command, "ws.cli.outcome": outcome}
    _instrument(
        "counter", "ws.cli.invocations", unit="1", description="CLI command invocations"
    ).add(1, attrs)
    _instrument(
        "histogram", "ws.cli.duration", unit="s", description="CLI command wall time"
    ).record(seconds, attrs)


def record_mcp_invocation(tool: str, outcome: str, seconds: float) -> None:
    """Counter + histogram for a single MCP tool invocation tagged with tool name + outcome.

    ``outcome`` is ``"ok"`` on success or ``"error"`` when the tool raised (including
    ``ToolError``). No-op + zero overhead when otel is off — gated entirely by
    ``_instrument``, so no opentelemetry import on the off-path."""
    attrs = {"ws.mcp.tool": tool, "ws.mcp.outcome": outcome}
    _instrument(
        "counter",
        "ws.mcp.tool.invocations",
        unit="1",
        description="MCP tool invocation count",
    ).add(1, attrs)
    _instrument(
        "histogram",
        "ws.mcp.tool.duration",
        unit="s",
        description="MCP tool wall time",
    ).record(seconds, attrs)


def record_mcp_resource_invocation(resource: str, outcome: str, seconds: float) -> None:
    """Counter + histogram for a single MCP resource read tagged with resource URI + outcome.

    ``outcome`` is ``"ok"`` on success or ``"error"`` when the handler raised (including
    ``ResourceError``). No-op + zero overhead when otel is off — gated entirely by
    ``_instrument``, so no opentelemetry import on the off-path.  Uses the ``ws.mcp.resource``
    tag namespace (distinct from ``ws.mcp.tool``) so resource and tool signals can be queried
    and alerted on independently."""
    attrs = {"ws.mcp.resource": resource, "ws.mcp.outcome": outcome}
    _instrument(
        "counter",
        "ws.mcp.resource.invocations",
        unit="1",
        description="MCP resource invocation count",
    ).add(1, attrs)
    _instrument(
        "histogram",
        "ws.mcp.resource.duration",
        unit="s",
        description="MCP resource read wall time",
    ).record(seconds, attrs)


def count_passthrough(surface: str, allowed: bool) -> None:
    """Counter of raw ``ws bd`` / ``ws git`` passthrough invocations — the fallback signal.

    Tagged ``ws.passthrough.surface`` (``bd`` / ``git``) + ``ws.passthrough.allowed``
    (``True`` when the gate let it through, ``False`` when ``bd_pass_enabled`` /
    ``git_pass_enabled`` blocked it). Every hit is an agent reaching past the first-class
    convention verbs (``ws work`` / ``ws plan``), so the allowed/gated mix tracks how often a
    first-class verb is missing or undiscovered. Both attributes are low-cardinality (a
    two-value enum apiece) — safe to index. No-op + zero overhead when otel is off: gated by
    ``_instrument`` so the off-path never imports opentelemetry or allocates. unit=1."""
    _instrument(
        "counter",
        "ws.passthrough.invocations",
        unit="1",
        description="raw bd/git passthrough invocations (fallback from convention verbs)",
    ).add(1, {"ws.passthrough.surface": surface, "ws.passthrough.allowed": allowed})


# ---- commit-flow metrics (hqfy.1) -------------------------------------------
#
# DORA-flavoured flow metrics emitted at the `ws work` merge seam (cycle/stage/slot/rework/outcome
# + validation duration) and the worktree-fleet ops (op duration). Every helper mirrors the
# gated/cached/no-op contract of the rest of this module: until ``init()`` wires a provider they
# return the shared no-op instrument (no opentelemetry import, no allocation). Attributes are
# bounded / low-cardinality and supplied by the caller — **never** bead/epic ids on these metric
# points (the bead id rides the verb SPAN via ``set_bead``, not the metric stream).

# The three flow stages a bead's cycle decomposes into; ``record_stage`` validates against these so
# a typo can't silently mint a new ``ws.work.stage.<typo>`` series.
_STAGES = ("coding", "review_wait", "merge_latency")


def record_cycle_time(seconds: float, attributes: dict[str, Any] | None = None) -> None:
    """Histogram of total bead cycle time (created → merged) in wall seconds."""
    _instrument(
        "histogram", "ws.work.cycle_time", unit="s", description="bead cycle time created→merged"
    ).record(seconds, attributes or {})


def record_cycle_time_active(seconds: float, attributes: dict[str, Any] | None = None) -> None:
    """Histogram of active bead cycle time (started/in_progress → merged) in wall seconds."""
    _instrument(
        "histogram",
        "ws.work.cycle_time.active",
        unit="s",
        description="bead active cycle time started→merged",
    ).record(seconds, attributes or {})


def record_stage(stage: str, seconds: float, attributes: dict[str, Any] | None = None) -> None:
    """Histogram of one flow stage's duration in wall seconds; ``stage`` is one of
    ``coding`` / ``review_wait`` / ``merge_latency`` (validated — an unknown stage raises, so a
    typo never mints a stray ``ws.work.stage.<typo>`` series)."""
    if stage not in _STAGES:
        raise ValueError(f"stage must be one of {list(_STAGES)}, got {stage!r}")
    _instrument(
        "histogram", f"ws.work.stage.{stage}", unit="s", description=f"{stage} stage duration"
    ).record(seconds, attributes or {})


def record_rework(rounds: float, attributes: dict[str, Any] | None = None) -> None:
    """Histogram of review rework rounds for a bead (count of review→changes-requested), unit=1."""
    _instrument(
        "histogram", "ws.work.rework.count", unit="1", description="review rework rounds per bead"
    ).record(rounds, attributes or {})


def record_merge_slot_wait(seconds: float, attributes: dict[str, Any] | None = None) -> None:
    """Histogram of time spent waiting to acquire the rig merge slot, in wall seconds."""
    _instrument(
        "histogram", "ws.work.merge_slot.wait", unit="s", description="merge slot acquire wait"
    ).record(seconds, attributes or {})


def record_merge_slot_hold(seconds: float, attributes: dict[str, Any] | None = None) -> None:
    """Histogram of time the rig merge slot was held (acquire → release), in wall seconds."""
    _instrument(
        "histogram", "ws.work.merge_slot.hold", unit="s", description="merge slot hold time"
    ).record(seconds, attributes or {})


def record_validation_duration(seconds: float, attributes: dict[str, Any] | None = None) -> None:
    """Histogram of validation-command wall time (``check`` / clean-checkout runs), wall seconds."""
    _instrument(
        "histogram",
        "ws.work.validation.duration",
        unit="s",
        description="validation command wall time",
    ).record(seconds, attributes or {})


def count_merge_outcome(attributes: dict[str, Any] | None = None) -> None:
    """Counter of merge outcomes — the caller tags ``ws.merge.how`` (ff/rebased/union/conflict) +
    ``ws.merge.kind`` / ``ws.rig`` — so the success/conflict mix is chartable. unit=1."""
    _instrument(
        "counter", "ws.work.merge.outcome", unit="1", description="merge outcomes by how"
    ).add(1, attributes or {})


def record_worktree_op_duration(seconds: float, attributes: dict[str, Any] | None = None) -> None:
    """Histogram of a single worktree git op's wall time (add/remove/prune), in wall seconds. The
    caller tags ``ws.worktree.op`` / ``ws.worktree.outcome`` / ``ws.rig``."""
    _instrument(
        "histogram", "ws.worktree.op.duration", unit="s", description="worktree git op wall time"
    ).record(seconds, attributes or {})


# ---- boundary error handling (cit/dqw.4) ------------------------------------
#
# The error side of the CLI + MCP seams (which dqw.2/dqw.3 already time + tag ok/error). On an
# *unhandled* exception at a boundary, the seam handler logs it via structlog, then calls these two
# to record it on the active span (ERROR) and bump an error counter. Both no-op + zero-cost when
# otel is off — like the rest of this module they never import opentelemetry on the off-path.


def record_exception(exc: BaseException) -> None:
    """Record ``exc`` on the active span and set the span status ERROR — no-op when otel is off or
    there's no recording span. The span-side of boundary error handling: pairs with ``count_error``
    + a structlog line at each instrumented seam. Cheap off-path: returns before importing
    opentelemetry when not active, so the default path stays import-free."""
    if not _initialized:
        return
    span = get_current_span()
    if not span.is_recording():
        return  # no active recording span (e.g. boundary outside any span) → nothing to mark
    from opentelemetry.trace import Status, StatusCode

    span.record_exception(exc)
    span.set_status(Status(StatusCode.ERROR, str(exc)))


def count_error(boundary: str, kind: str, attributes: dict[str, Any] | None = None) -> None:
    """Counter of unhandled errors at an instrumented boundary, tagged ``ws.error.boundary``
    (``cli`` / ``mcp``) + ``ws.error.kind`` (the exception class name). No-op + zero overhead when
    otel is off — gated by ``_instrument`` (no opentelemetry import on the off-path). Distinct from
    the dqw.2/dqw.3 invocation counters (which already tag outcome=error), so the two never
    double-count: this measures *errors*, those measure *invocations*."""
    attrs = {"ws.error.boundary": boundary, "ws.error.kind": kind}
    if attributes:
        attrs.update(attributes)
    _instrument(
        "counter", "ws.errors", unit="1", description="unhandled errors at instrumented boundaries"
    ).add(1, attrs)


# ---- agentic GenAI spans (cit.5) — EXPERIMENTAL -----------------------------
#
# EXPERIMENTAL: emits OpenTelemetry **GenAI** semantic-convention spans for the
# coordinator->developer dispatch. The coordinator is itself an agent loop, so handing a bead to a
# developer crew is modeled as the GenAI ``invoke_agent`` operation. The GenAI semconv is young and
# still experimental upstream (attribute/event names may churn across semconv releases) — treat the
# ``gen_ai.*`` names below as a moving target and re-check the spec before relying on them.
#
# Two rules shape this surface:
#   * ``gen_ai.*`` *attributes* carry only low-cardinality control-plane facts — operation, system,
#     model, agent name, token usage — so they're safe to index and query.
#   * brief / feedback *content* is carried as span **EVENTS**, never span attributes, so the
#     Collector can drop it wholesale (PII / size) without touching the span's queryable shape.
#
# Gating mirrors the rest of this module: until ``init()`` wires a real provider, the dispatch span
# is the shared no-op (zero-cost, no opentelemetry import) — cheap when otel is off.

# gen_ai.operation.name values (semconv enum subset we emit).
GEN_AI_OP_INVOKE_AGENT = "invoke_agent"
GEN_AI_OP_EXECUTE_TOOL = "execute_tool"
GEN_AI_OP_READ_RESOURCE = "read_resource"

# Default gen_ai.system — the harness driving the agent loop. Overridable per dispatch.
_GEN_AI_SYSTEM = "claude"

# Span-event names for droppable message content (semconv GenAI event names). Both brief and
# reviewer feedback are *inputs* to the developer agent, so both are user messages; the
# ``ws.genai.content_kind`` event attribute distinguishes them.
_GEN_AI_EVENT_USER = "gen_ai.user.message"


def _genai_attributes(operation: str, *, system: str, model: str, agent: str, extra=None) -> dict:
    """Build the low-cardinality ``gen_ai.*`` span attributes. Content (brief/feedback) is *never*
    placed here — it rides span events via the ``record_*_event`` helpers below."""
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": operation,
        "gen_ai.system": system or _GEN_AI_SYSTEM,
    }
    if model:
        attrs["gen_ai.request.model"] = model
    if agent:
        attrs["gen_ai.agent.name"] = agent
    if extra:
        attrs.update(extra)
    return attrs


def _content_event(span, kind: str, content) -> None:
    """Attach message ``content`` as a span EVENT (never an attribute). EXPERIMENTAL.

    The brief / feedback can be large and may carry PII, so it rides as a droppable span event:
    the Collector can strip these events without losing the span's queryable ``gen_ai.*`` shape."""
    if not content:
        return
    span.add_event(
        _GEN_AI_EVENT_USER,
        {"gen_ai.message.role": "user", "ws.genai.content_kind": kind, "content": str(content)},
    )


def record_brief_event(span, brief) -> None:
    """EXPERIMENTAL: record a dispatch *brief* as a droppable content event (never an attribute)."""
    _content_event(span, "brief", brief)


def record_feedback_event(span, feedback) -> None:
    """EXPERIMENTAL: record reviewer *feedback* (changes-requested) as a droppable content event."""
    _content_event(span, "feedback", feedback)


def set_token_usage(span, *, input_tokens=None, output_tokens=None) -> None:
    """EXPERIMENTAL: record ``gen_ai.usage.*`` token counts when available (often absent at the
    dispatch seam — the developer's actual token spend is observed elsewhere)."""
    if input_tokens is not None:
        span.set_attribute("gen_ai.usage.input_tokens", input_tokens)
    if output_tokens is not None:
        span.set_attribute("gen_ai.usage.output_tokens", output_tokens)


@contextmanager
def record_agent_dispatch(
    *,
    agent: str,
    model: str = "",
    system: str = _GEN_AI_SYSTEM,
    brief=None,
    feedback=None,
    attributes=None,
):
    """EXPERIMENTAL: open a GenAI ``invoke_agent`` span for a coordinator->developer dispatch.

    This is the clean callable the dispatch path uses (the in-code seam is ``ws work assign``,
    which hands a bead to a developer crew). The span name follows the GenAI convention
    ``invoke_agent {agent}``; ``gen_ai.*`` attributes carry the model/operation/agent control-plane
    facts, while the *brief* (and any *feedback*) is recorded as a droppable span EVENT. No-op +
    zero-cost when otel is off — like the rest of this module it never imports opentelemetry on the
    off path. ``outcome`` is carried by span status: an exception escaping the ``with`` marks the
    span ERROR (the SDK's default), so a failed dispatch is visible without a custom attribute.

    Yields the span so the caller can attach token usage or post-dispatch feedback events.
    """
    attrs = _genai_attributes(
        GEN_AI_OP_INVOKE_AGENT, system=system, model=model, agent=agent, extra=attributes
    )
    name = f"{GEN_AI_OP_INVOKE_AGENT} {agent}" if agent else GEN_AI_OP_INVOKE_AGENT
    with get_tracer().start_as_current_span(name, attributes=attrs) as span:
        record_brief_event(span, brief)
        record_feedback_event(span, feedback)
        yield span
