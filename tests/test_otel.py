"""ws.otel — gated OTel SDK init: wiring when enabled+present, no-op+hint when absent, off
by default. The runnable acceptance check for bead.

The OTel SDK is an *optional* extra (`ws[otel]`) and is NOT installed in the default test env,
so every "libs present" assertion drives a **fake** OTel surface injected at the single
``_load_otel`` seam (MagicMocks recording constructor calls). The "libs absent" path makes that
same seam raise. That keeps the suite deterministic without the extra while still proving init
wires providers + OTLP exporters + the log bridge.
"""

from __future__ import annotations

import io
import json
import logging
from unittest.mock import MagicMock

import pytest

from ws import config, log, otel

_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Isolate each test: clear the module init guard + flush-on-exit state, scrub the OTLP
    endpoint env, and snapshot/restore root-logger handlers (init attaches a LoggingHandler)."""
    otel._initialized = False
    otel._providers = ()
    otel._atexit_registered = False
    monkeypatch.delenv(_ENDPOINT_ENV, raising=False)
    log._configured = False
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers.clear()
    yield
    log._configured = False
    otel._initialized = False
    otel._providers = ()
    otel._atexit_registered = False
    root.handlers.clear()
    root.handlers.extend(saved)


def _fake_otel() -> otel._Otel:
    """A fully-mocked OTel surface: every provider/exporter/processor is a MagicMock so init's
    wiring is observable (constructor args, set_*_provider calls) without the real SDK."""
    handler_cls = MagicMock(name="LoggingHandler")
    # The bridge handler lands on the stdlib root logger; give its instance a real int level so
    # the trailing `otel_initialized` log record passes stdlib's `levelno >= handler.level`.
    handler_cls.return_value.level = logging.NOTSET
    return otel._Otel(
        trace=MagicMock(name="trace"),
        metrics=MagicMock(name="metrics"),
        logs=MagicMock(name="logs"),
        Resource=MagicMock(name="Resource"),
        TracerProvider=MagicMock(name="TracerProvider"),
        BatchSpanProcessor=MagicMock(name="BatchSpanProcessor"),
        OTLPSpanExporter=MagicMock(name="OTLPSpanExporter"),
        MeterProvider=MagicMock(name="MeterProvider"),
        PeriodicExportingMetricReader=MagicMock(name="PeriodicExportingMetricReader"),
        OTLPMetricExporter=MagicMock(name="OTLPMetricExporter"),
        LoggerProvider=MagicMock(name="LoggerProvider"),
        BatchLogRecordProcessor=MagicMock(name="BatchLogRecordProcessor"),
        OTLPLogExporter=MagicMock(name="OTLPLogExporter"),
        LoggingHandler=handler_cls,
    )


# ---- import safety ----------------------------------------------------------


def test_import_is_safe_without_the_extra():
    # The whole point of lazy imports: importing the module pulls in nothing optional.
    import ws.otel  # noqa: F401  (re-import is a no-op; asserts it never raised at collect)


# ---- disabled by default ----------------------------------------------------


def test_disabled_by_default_no_op():
    assert config.otel_enabled({}) is False  # off unless explicitly enabled
    assert otel.init({}) is False  # disabled ⇒ nothing wired


def test_disabled_does_not_touch_otel(monkeypatch):
    # When disabled, init must short-circuit *before* trying to load the SDK.
    loaded = MagicMock(side_effect=AssertionError("_load_otel must not run when disabled"))
    monkeypatch.setattr(otel, "_load_otel", loaded)
    assert otel.init({"otel": {"enabled": False}}) is False


# ---- enabled but libs absent: graceful no-op + install hint -----------------


def test_enabled_libs_absent_noops_with_hint(monkeypatch):
    def _raise() -> otel._Otel:
        raise ImportError("No module named 'opentelemetry'")

    monkeypatch.setattr(otel, "_load_otel", _raise)

    buf = io.StringIO()
    log.configure(fmt="json", stream=buf)  # capture the warning through the real pipeline

    result = otel.init({"otel": {"enabled": True}})

    assert result is False  # no crash, no wiring
    record = json.loads(buf.getvalue().strip().splitlines()[-1])
    assert record["event"] == "otel_install_hint"
    assert record["level"] == "warning"
    assert "ws[otel]" in record["hint"]  # the actionable install hint


# ---- enabled + libs present: full wiring ------------------------------------


def test_enabled_present_wires_providers_exporters_and_bridge(monkeypatch):
    monkeypatch.setenv(_ENDPOINT_ENV, "http://collector:4317")
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda: fake)

    result = otel.init({"otel": {"enabled": True, "rig": "workspace"}})

    assert result is True

    # Resource: service.name=ws + version + rig.
    fake.Resource.create.assert_called_once()
    attrs = fake.Resource.create.call_args.args[0]
    assert attrs["service.name"] == "ws"
    assert "service.version" in attrs
    assert attrs["ws.rig"] == "workspace"
    resource = fake.Resource.create.return_value

    # Traces: provider(resource) → BatchSpanProcessor(OTLP(endpoint)) → set global.
    fake.TracerProvider.assert_called_once_with(resource=resource)
    fake.OTLPSpanExporter.assert_called_once_with(endpoint="http://collector:4317")
    fake.BatchSpanProcessor.assert_called_once_with(fake.OTLPSpanExporter.return_value)
    fake.TracerProvider.return_value.add_span_processor.assert_called_once_with(
        fake.BatchSpanProcessor.return_value
    )
    fake.trace.set_tracer_provider.assert_called_once_with(fake.TracerProvider.return_value)

    # Metrics: periodic reader over the OTLP metric exporter → meter provider → set global.
    fake.OTLPMetricExporter.assert_called_once_with(endpoint="http://collector:4317")
    fake.PeriodicExportingMetricReader.assert_called_once_with(
        fake.OTLPMetricExporter.return_value
    )
    fake.MeterProvider.assert_called_once_with(
        resource=resource, metric_readers=[fake.PeriodicExportingMetricReader.return_value]
    )
    fake.metrics.set_meter_provider.assert_called_once_with(fake.MeterProvider.return_value)

    # Logs: provider → BatchLogRecordProcessor(OTLP) → set global → LoggingHandler on root.
    fake.OTLPLogExporter.assert_called_once_with(endpoint="http://collector:4317")
    fake.BatchLogRecordProcessor.assert_called_once_with(fake.OTLPLogExporter.return_value)
    fake.LoggerProvider.return_value.add_log_record_processor.assert_called_once_with(
        fake.BatchLogRecordProcessor.return_value
    )
    fake.logs.set_logger_provider.assert_called_once_with(fake.LoggerProvider.return_value)
    fake.LoggingHandler.assert_called_once_with(
        level=logging.NOTSET, logger_provider=fake.LoggerProvider.return_value
    )
    assert fake.LoggingHandler.return_value in logging.getLogger().handlers  # bridge attached


def test_endpoint_defaults_when_env_unset(monkeypatch):
    # No OTEL_EXPORTER_OTLP_ENDPOINT and no config endpoint ⇒ exporters get no endpoint kwarg
    # (they fall back to their own built-in default).
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda: fake)

    assert otel.init({"otel": {"enabled": True}}) is True
    fake.OTLPSpanExporter.assert_called_once_with()
    fake.OTLPMetricExporter.assert_called_once_with()
    fake.OTLPLogExporter.assert_called_once_with()


def test_rig_omitted_when_unset(monkeypatch):
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda: fake)

    otel.init({"otel": {"enabled": True}})
    attrs = fake.Resource.create.call_args.args[0]
    assert "ws.rig" not in attrs  # blank rig is omitted, not emitted empty


# ---- idempotency ------------------------------------------------------------


def test_init_is_idempotent(monkeypatch):
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda: fake)

    assert otel.init({"otel": {"enabled": True}}) is True
    assert otel.init({"otel": {"enabled": True}}) is False  # already wired ⇒ no re-stamp
    fake.trace.set_tracer_provider.assert_called_once()  # provider set exactly once


# ---- flush-on-exit (cit.10) -------------------------------------------------
#
# ws is a short-lived CLI but the batch processors / periodic reader export on an interval, so
# init() must register an atexit hook that shuts the providers down (force-flushing the batch)
# before the process exits — otherwise spans/metrics/logs are silently dropped.


def test_init_registers_flush_on_exit(monkeypatch):
    # The success path must register exactly one atexit hook so the batch drains on exit.
    registered = []
    monkeypatch.setattr(otel.atexit, "register", lambda fn: registered.append(fn))
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda: fake)

    assert otel.init({"otel": {"enabled": True}}) is True
    assert registered == [otel.shutdown]  # one hook, the module's shutdown()


def test_shutdown_flushes_all_three_providers(monkeypatch):
    # The registered hook must call shutdown() (which force-flushes) on tracer/meter/logger.
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda: fake)
    assert otel.init({"otel": {"enabled": True}}) is True

    otel.shutdown()

    fake.TracerProvider.return_value.shutdown.assert_called_once_with()
    fake.MeterProvider.return_value.shutdown.assert_called_once_with()
    fake.LoggerProvider.return_value.shutdown.assert_called_once_with()
    assert otel.is_active() is False  # state reset so a fresh init() can re-wire


def test_shutdown_is_noop_when_not_initialized():
    # No init ⇒ nothing wired ⇒ shutdown is a safe no-op (never raises, nothing to flush).
    assert otel.is_active() is False
    otel.shutdown()  # must not raise


def test_disabled_registers_no_flush_hook(monkeypatch):
    # The off path must not register an atexit hook (zero-overhead when otel is disabled).
    registered = []
    monkeypatch.setattr(otel.atexit, "register", lambda fn: registered.append(fn))

    assert otel.init({"otel": {"enabled": False}}) is False
    assert registered == []
    assert otel._atexit_registered is False


def test_libs_absent_registers_no_flush_hook(monkeypatch):
    # Enabled-but-libs-absent is a no-op: no providers wired, so no flush hook either.
    registered = []
    monkeypatch.setattr(otel.atexit, "register", lambda fn: registered.append(fn))
    monkeypatch.setattr(otel, "_load_otel", lambda: (_ for _ in ()).throw(ImportError("absent")))

    assert otel.init({"otel": {"enabled": True}}) is False
    assert registered == []


def test_flush_hook_registered_once_across_reinit(monkeypatch):
    # Re-init (shutdown resets _initialized) must not stack duplicate atexit hooks.
    registered = []
    monkeypatch.setattr(otel.atexit, "register", lambda fn: registered.append(fn))
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda: fake)

    assert otel.init({"otel": {"enabled": True}}) is True
    otel.shutdown()  # resets _initialized so init() can re-wire
    assert otel.init({"otel": {"enabled": True}}) is True

    assert registered == [otel.shutdown]  # registered exactly once despite two inits


def test_shutdown_swallows_provider_errors(monkeypatch):
    # An exporter failure on exit must not raise out of the atexit hook (best-effort flush).
    fake = _fake_otel()
    fake.TracerProvider.return_value.shutdown.side_effect = RuntimeError("collector unreachable")
    monkeypatch.setattr(otel, "_load_otel", lambda: fake)
    assert otel.init({"otel": {"enabled": True}}) is True

    otel.shutdown()  # must not raise despite the tracer provider blowing up

    # The later providers are still flushed — one failure doesn't abort the rest.
    fake.MeterProvider.return_value.shutdown.assert_called_once_with()
    fake.LoggerProvider.return_value.shutdown.assert_called_once_with()


# ---- config accessors -------------------------------------------------------


def test_config_otel_defaults():
    assert config.otel_enabled({}) is False
    assert config.otel_endpoint({}) == ""
    assert config.otel_rig({}) == ""


def test_config_otel_endpoint_env_wins(monkeypatch):
    monkeypatch.setenv(_ENDPOINT_ENV, "http://env:4317")
    cfg = {"otel": {"endpoint": "http://cfg:4317"}}
    assert config.otel_endpoint(cfg) == "http://env:4317"  # env overrides config


def test_config_otel_endpoint_falls_back_to_config(monkeypatch):
    monkeypatch.delenv(_ENDPOINT_ENV, raising=False)
    cfg = {"otel": {"endpoint": "http://cfg:4317"}}
    assert config.otel_endpoint(cfg) == "http://cfg:4317"


def test_config_otel_overrides():
    cfg = {"otel": {"enabled": True, "rig": "myrig"}}
    assert config.otel_enabled(cfg) is True
    assert config.otel_rig(cfg) == "myrig"
