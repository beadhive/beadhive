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
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from . import config

_SERVICE_NAME = "ws"

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
    # logs
    LoggerProvider: Any
    BatchLogRecordProcessor: Any
    OTLPLogExporter: Any
    LoggingHandler: Any


def _load_otel() -> _Otel:
    """Import the OpenTelemetry SDK + OTLP (gRPC) exporter surface, lazily.

    Raises ``ImportError`` when the ``ws[otel]`` extra is absent — the single import boundary so
    ``init`` can treat "otel not installed" as one catchable condition. Never called at module
    import time, so ``import ws.otel`` stays free of the optional dependency."""
    from opentelemetry import _logs as logs
    from opentelemetry import metrics, trace
    from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
    from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

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
    """The Resource identity: ``service.name=ws`` + version, plus ``ws.rig`` when configured
    (omitted when empty rather than emitting a blank attribute)."""
    attrs = {
        "service.name": _SERVICE_NAME,
        "service.version": _ws_version(),
    }
    rig = config.otel_rig(cfg)
    if rig:
        attrs["ws.rig"] = rig
    return attrs


def _endpoint_kwargs(cfg) -> dict[str, str]:
    """OTLP exporter endpoint kwargs from ``OTEL_EXPORTER_OTLP_ENDPOINT`` (via config). Empty
    when unset so the exporter falls back to its own default — every exporter gets the same
    endpoint, the one knob the bead specifies."""
    endpoint = config.otel_endpoint(cfg)
    return {"endpoint": endpoint} if endpoint else {}


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

    try:
        otel = _load_otel()
    except Exception:
        # Enabled but the extra isn't installed — warn with the install hint and no-op. Never
        # crash the tool just because someone flipped the flag without `pip install ws[otel]`.
        logger.warning("otel_install_hint", hint=_INSTALL_HINT)
        return False

    resource = otel.Resource.create(_resource_attributes(cfg))
    endpoint_kwargs = _endpoint_kwargs(cfg)

    # Traces: provider → BatchSpanProcessor(OTLP) → set as global tracer provider.
    tracer_provider = otel.TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        otel.BatchSpanProcessor(otel.OTLPSpanExporter(**endpoint_kwargs))
    )
    otel.trace.set_tracer_provider(tracer_provider)

    # Metrics: provider with a periodic reader over the OTLP metric exporter (the metrics
    # analogue of a batch processor) → set as global meter provider.
    metric_reader = otel.PeriodicExportingMetricReader(otel.OTLPMetricExporter(**endpoint_kwargs))
    meter_provider = otel.MeterProvider(resource=resource, metric_readers=[metric_reader])
    otel.metrics.set_meter_provider(meter_provider)

    # Logs: provider → BatchLogRecordProcessor(OTLP) → set as global logger provider, then
    # bridge cit.1's stdlib root logger (which structlog feeds) into OTel logs via a handler.
    logger_provider = otel.LoggerProvider(resource=resource)
    logger_provider.add_log_record_processor(
        otel.BatchLogRecordProcessor(otel.OTLPLogExporter(**endpoint_kwargs))
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
    logger.info("otel_initialized", endpoint=config.otel_endpoint(cfg) or "<exporter-default>")
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


def count_validation(passed: bool, attributes: dict[str, Any] | None = None) -> None:
    """Counter of validation runs, tagged pass/fail (the rig validation-command result)."""
    attrs = {"ws.validation.result": "pass" if passed else "fail"}
    if attributes:
        attrs.update(attributes)
    _instrument(
        "counter", "ws.work.validation.runs", unit="1", description="validation pass/fail"
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
