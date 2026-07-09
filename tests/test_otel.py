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
import sys
import types
from unittest.mock import MagicMock

import pytest

from beadhive import config, log, otel

_ENDPOINT_ENV = "OTEL_EXPORTER_OTLP_ENDPOINT"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Isolate each test: clear the module init guard + flush-on-exit state, scrub the OTLP
    endpoint env, and snapshot/restore root-logger handlers (init attaches a LoggingHandler).

    Also neutralize the cwd-derived Resource identity enrichment (triplet / ws.rig / ws.worktree)
    + the ws.role / observaloop.profile env so a test's Resource attrs don't depend on where the
    suite happens to run — tests that exercise enrichment re-inject ``worktree.cwd_identity``."""
    from beadhive import worktree

    otel._initialized = False
    otel._providers = ()
    otel._atexit_registered = False
    monkeypatch.delenv(_ENDPOINT_ENV, raising=False)
    monkeypatch.delenv("WS_ROLE", raising=False)
    monkeypatch.delenv("WS_OBSERVALOOP_PROFILE", raising=False)
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE", raising=False)
    monkeypatch.setattr(worktree, "cwd_identity", lambda *a, **k: (None, ""))
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
        # Sentinel DELTA preference map: init threads this straight into the metric exporter's
        # preferred_temporality kwarg when delta is selected (the default).
        metric_temporality_delta=MagicMock(name="metric_temporality_delta"),
        LoggerProvider=MagicMock(name="LoggerProvider"),
        BatchLogRecordProcessor=MagicMock(name="BatchLogRecordProcessor"),
        OTLPLogExporter=MagicMock(name="OTLPLogExporter"),
        LoggingHandler=handler_cls,
    )


# ---- import safety ----------------------------------------------------------


def test_import_is_safe_without_the_extra():
    # The whole point of lazy imports: importing the module pulls in nothing optional.
    import beadhive.otel  # noqa: F401  (re-import is a no-op; asserts it never raised at collect)


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
    def _raise(*_a, **_k) -> otel._Otel:
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
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)

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

    # Metrics: periodic reader over the OTLP metric exporter → meter provider → set global. The
    # metric exporter also gets the DELTA preferred_temporality map by default (short-lived CLI).
    fake.OTLPMetricExporter.assert_called_once_with(
        endpoint="http://collector:4317", preferred_temporality=fake.metric_temporality_delta
    )
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
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)

    assert otel.init({"otel": {"enabled": True}}) is True
    fake.OTLPSpanExporter.assert_called_once_with()
    # Even with no endpoint, the metric exporter still gets the default DELTA preference.
    fake.OTLPMetricExporter.assert_called_once_with(
        preferred_temporality=fake.metric_temporality_delta
    )
    fake.OTLPLogExporter.assert_called_once_with()


def test_rig_omitted_when_unset(monkeypatch):
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)

    otel.init({"otel": {"enabled": True}})
    attrs = fake.Resource.create.call_args.args[0]
    assert "ws.rig" not in attrs  # blank rig is omitted, not emitted empty (and no cwd derivation)


# ---- Resource identity enrichment (triplet / ws.rig / ws.role / ws.worktree / ----------------
#      observaloop.profile). _resource_attributes is the pure builder; the autouse fixture
#      neutralizes cwd derivation, so each test re-injects worktree.cwd_identity for its case.


def _patch_cwd_identity(monkeypatch, triplet, leaf):
    from beadhive import worktree

    monkeypatch.setattr(worktree, "cwd_identity", lambda *a, **k: (triplet, leaf))


def test_triplet_present_when_in_managed_repo(monkeypatch):
    _patch_cwd_identity(monkeypatch, ("github", "acme", "widgets"), "bead-7")
    attrs = otel._resource_attributes({"otel": {"enabled": True}})
    assert attrs["ws.provider"] == "github"
    assert attrs["ws.org"] == "acme"
    assert attrs["ws.repo"] == "widgets"


def test_triplet_omitted_outside_managed_repo(monkeypatch):
    _patch_cwd_identity(monkeypatch, None, "")
    attrs = otel._resource_attributes({"otel": {"enabled": True}})
    for k in ("ws.provider", "ws.org", "ws.repo", "ws.worktree"):
        assert k not in attrs


def test_rig_autoderived_from_prefix_when_unset(monkeypatch):
    _patch_cwd_identity(monkeypatch, ("github", "acme", "widgets"), "")
    cfg = {
        "otel": {"enabled": True},
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "widgets", "prefix": "wid"}
        ],
    }
    assert otel._resource_attributes(cfg)["ws.rig"] == "wid"  # prefix, not repo


def test_rig_config_wins_over_autoderive(monkeypatch):
    _patch_cwd_identity(monkeypatch, ("github", "acme", "widgets"), "")
    cfg = {
        "otel": {"enabled": True, "rig": "explicit"},
        "managed_repos": [
            {"provider": "github", "org": "acme", "repo": "widgets", "prefix": "wid"}
        ],
    }
    assert otel._resource_attributes(cfg)["ws.rig"] == "explicit"


def test_rig_autoderive_falls_back_to_repo_when_unregistered(monkeypatch):
    _patch_cwd_identity(monkeypatch, ("github", "acme", "widgets"), "")
    assert otel._resource_attributes({"otel": {"enabled": True}})["ws.rig"] == "widgets"


def test_role_from_env(monkeypatch):
    monkeypatch.setenv("WS_ROLE", "developer")
    assert otel._resource_attributes({"otel": {"enabled": True}})["ws.role"] == "developer"


def test_role_from_config(monkeypatch):
    assert otel._resource_attributes({"otel": {"enabled": True, "role": "merger"}})["ws.role"] == (
        "merger"
    )


def test_role_omitted_when_unset(monkeypatch):
    assert "ws.role" not in otel._resource_attributes({"otel": {"enabled": True}})


def test_worktree_present_for_managed_leaf(monkeypatch):
    _patch_cwd_identity(monkeypatch, ("github", "acme", "widgets"), "bead-7")
    assert otel._resource_attributes({"otel": {"enabled": True}})["ws.worktree"] == "bead-7"


def test_worktree_excludes_verify_leaf(monkeypatch):
    _patch_cwd_identity(monkeypatch, ("github", "acme", "widgets"), "verify-bead-7")
    assert "ws.worktree" not in otel._resource_attributes({"otel": {"enabled": True}})


def test_observaloop_profile_from_observaloop_section(monkeypatch):
    cfg = {"otel": {"enabled": True}, "observaloop": {"profile": "debug-loop"}}
    assert otel._resource_attributes(cfg)["observaloop.profile"] == "debug-loop"


def test_observaloop_profile_from_otel_key(monkeypatch):
    cfg = {"otel": {"enabled": True, "observaloop_profile": "triage"}}
    assert otel._resource_attributes(cfg)["observaloop.profile"] == "triage"


def test_observaloop_profile_omitted_by_default(monkeypatch):
    assert "observaloop.profile" not in otel._resource_attributes({"otel": {"enabled": True}})


# ---- per-span bead/epic (otel.set_bead) -------------------------------------


class _RecordingSpan:
    """A minimal span that records set_attribute calls and reports it is recording."""

    def __init__(self, recording=True):
        self._recording = recording
        self.attrs: dict = {}

    def is_recording(self) -> bool:
        return self._recording

    def set_attribute(self, key, value):
        self.attrs[key] = value


def test_set_bead_stamps_bead_and_epic(monkeypatch):
    span = _RecordingSpan()
    otel._initialized = True
    monkeypatch.setattr(otel, "get_current_span", lambda: span)
    otel.set_bead("ag-1.2")
    assert span.attrs == {"ws.bead": "ag-1.2", "ws.epic": "ag-1"}


def test_set_bead_top_level_omits_epic(monkeypatch):
    span = _RecordingSpan()
    otel._initialized = True
    monkeypatch.setattr(otel, "get_current_span", lambda: span)
    otel.set_bead("ag-1")
    assert span.attrs == {"ws.bead": "ag-1"}  # no '.' → no molecule → ws.epic omitted


def test_set_bead_noop_when_otel_off(monkeypatch):
    span = _RecordingSpan()
    otel._initialized = False  # off-path
    monkeypatch.setattr(otel, "get_current_span", lambda: span)
    otel.set_bead("ag-1.2")
    assert span.attrs == {}  # never touched the span


def test_set_bead_noop_when_span_not_recording(monkeypatch):
    span = _RecordingSpan(recording=False)
    otel._initialized = True
    monkeypatch.setattr(otel, "get_current_span", lambda: span)
    otel.set_bead("ag-1.2")
    assert span.attrs == {}  # no recording span → nothing stamped


def test_set_bead_noop_for_empty_bead(monkeypatch):
    span = _RecordingSpan()
    otel._initialized = True
    monkeypatch.setattr(otel, "get_current_span", lambda: span)
    otel.set_bead("")
    assert span.attrs == {}


# ---- transport selection (otel.protocol) ------------------------------------
#
# otel.protocol picks the OTLP exporter CLASS for all three signals: grpc → the proto.grpc.*
# exporters, http/protobuf → the proto.http.* exporters. The extra is absent in this env, so the
# selection is exercised by injecting fake exporter leaf modules at the import paths _load_otel
# resolves — asserting the right trio of classes comes back per protocol.

# (exporter submodule, class attribute) for the three signals, in span/metric/log order.
_EXPORTER_LEAVES = (
    ("trace_exporter", "OTLPSpanExporter"),
    ("metric_exporter", "OTLPMetricExporter"),
    ("_log_exporter", "OTLPLogExporter"),
)


def _install_fake_exporters(monkeypatch) -> dict[str, dict[str, type]]:
    """Register fake grpc + http OTLP exporter leaf modules in sys.modules, each exporting a
    distinct sentinel class, so ``_otlp_exporters`` resolves to observable classes per transport.
    Returns ``{transport: {class_name: sentinel_class}}`` for assertions."""
    sentinels: dict[str, dict[str, type]] = {"grpc": {}, "http": {}}
    for transport in ("grpc", "http"):
        for submod, cls_name in _EXPORTER_LEAVES:
            sentinel = type(f"{transport}_{cls_name}", (), {})
            sentinels[transport][cls_name] = sentinel
            mod_name = f"opentelemetry.exporter.otlp.proto.{transport}.{submod}"
            module = types.ModuleType(mod_name)
            setattr(module, cls_name, sentinel)
            monkeypatch.setitem(sys.modules, mod_name, module)
    return sentinels


def test_otlp_exporters_grpc_selects_grpc_classes(monkeypatch):
    sentinels = _install_fake_exporters(monkeypatch)
    span, metric, log_exp = otel._otlp_exporters(config.OTEL_PROTOCOL_GRPC)
    assert span is sentinels["grpc"]["OTLPSpanExporter"]
    assert metric is sentinels["grpc"]["OTLPMetricExporter"]
    assert log_exp is sentinels["grpc"]["OTLPLogExporter"]


def test_otlp_exporters_http_selects_http_classes(monkeypatch):
    sentinels = _install_fake_exporters(monkeypatch)
    span, metric, log_exp = otel._otlp_exporters(config.OTEL_PROTOCOL_HTTP)
    assert span is sentinels["http"]["OTLPSpanExporter"]
    assert metric is sentinels["http"]["OTLPMetricExporter"]
    assert log_exp is sentinels["http"]["OTLPLogExporter"]


def test_default_protocol_is_grpc(monkeypatch):
    # No otel.protocol configured ⇒ init threads "grpc" into _load_otel (back-compat default).
    seen = {}

    def _capture(protocol=config.OTEL_PROTOCOL_GRPC):
        seen["protocol"] = protocol
        return _fake_otel()

    monkeypatch.setattr(otel, "_load_otel", _capture)
    assert otel.init({"otel": {"enabled": True}}) is True
    assert seen["protocol"] == config.OTEL_PROTOCOL_GRPC


def test_http_protocol_threaded_into_load_otel(monkeypatch):
    seen = {}

    def _capture(protocol=config.OTEL_PROTOCOL_GRPC):
        seen["protocol"] = protocol
        return _fake_otel()

    monkeypatch.setattr(otel, "_load_otel", _capture)
    assert otel.init({"otel": {"enabled": True, "protocol": "http/protobuf"}}) is True
    assert seen["protocol"] == "http/protobuf"


def test_invalid_protocol_fails_clearly(monkeypatch):
    # An unknown transport must raise a clear error — never a silent fallback to grpc, and never
    # the libs-absent install-hint no-op. The validation fires before _load_otel is reached.
    monkeypatch.setattr(
        otel, "_load_otel", MagicMock(side_effect=AssertionError("must not load on bad protocol"))
    )
    with pytest.raises(ValueError, match="otel.protocol"):
        otel.init({"otel": {"enabled": True, "protocol": "kafka"}})


# ---- per-signal http endpoint path ----------------------
#
# The http/protobuf exporter uses an explicit ``endpoint=`` VERBATIM — it does NOT append the
# per-signal path the way it would when deriving from OTEL_EXPORTER_OTLP_ENDPOINT — so a bare base
# POSTs to ``/`` and 404s. init() must give each http signal its own ``<base>/v1/<signal>`` endpoint
# while grpc keeps the bare base (the grpc exporter routes by RPC method, not URL path).


def test_signal_endpoint_grpc_keeps_base():
    # grpc has no per-signal URL path — every signal uses the bare base, unchanged.
    for signal in ("traces", "metrics", "logs"):
        assert (
            otel._signal_endpoint("http://c:4317", config.OTEL_PROTOCOL_GRPC, signal)
            == "http://c:4317"
        )


def test_signal_endpoint_http_appends_per_signal_path():
    base = "http://localhost:4326"
    assert otel._signal_endpoint(base, config.OTEL_PROTOCOL_HTTP, "traces") == f"{base}/v1/traces"
    assert otel._signal_endpoint(base, config.OTEL_PROTOCOL_HTTP, "metrics") == f"{base}/v1/metrics"
    assert otel._signal_endpoint(base, config.OTEL_PROTOCOL_HTTP, "logs") == f"{base}/v1/logs"


def test_signal_endpoint_http_strips_trailing_slash():
    # A trailing slash on the base must not yield a doubled ``//v1/...``.
    assert (
        otel._signal_endpoint("http://localhost:4326/", config.OTEL_PROTOCOL_HTTP, "traces")
        == "http://localhost:4326/v1/traces"
    )


def test_signal_endpoint_http_no_double_append_when_prepathed():
    # Operator already pointed the base at the signal path ⇒ don't append it twice.
    assert (
        otel._signal_endpoint(
            "http://localhost:4326/v1/metrics", config.OTEL_PROTOCOL_HTTP, "metrics"
        )
        == "http://localhost:4326/v1/metrics"
    )
    # Trailing slash on an already-pathed base is normalized off, still no double-append.
    assert (
        otel._signal_endpoint(
            "http://localhost:4326/v1/traces/", config.OTEL_PROTOCOL_HTTP, "traces"
        )
        == "http://localhost:4326/v1/traces"
    )


def test_http_init_gives_each_exporter_its_v1_signal_endpoint(monkeypatch):
    # End-to-end through init(): the three http exporters each get <base>/v1/<signal>.
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)

    result = otel.init(
        {"otel": {"enabled": True, "protocol": "http/protobuf", "endpoint": "http://localhost:4326"}}
    )

    assert result is True
    fake.OTLPSpanExporter.assert_called_once_with(endpoint="http://localhost:4326/v1/traces")
    fake.OTLPMetricExporter.assert_called_once_with(
        endpoint="http://localhost:4326/v1/metrics",
        preferred_temporality=fake.metric_temporality_delta,
    )
    fake.OTLPLogExporter.assert_called_once_with(endpoint="http://localhost:4326/v1/logs")


def test_grpc_init_keeps_bare_base_endpoint(monkeypatch):
    # grpc (the default) must keep the bare base on every signal — no /v1/<signal> appended.
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)

    result = otel.init(
        {"otel": {"enabled": True, "protocol": "grpc", "endpoint": "http://collector:4317"}}
    )

    assert result is True
    fake.OTLPSpanExporter.assert_called_once_with(endpoint="http://collector:4317")
    fake.OTLPMetricExporter.assert_called_once_with(
        endpoint="http://collector:4317", preferred_temporality=fake.metric_temporality_delta
    )
    fake.OTLPLogExporter.assert_called_once_with(endpoint="http://collector:4317")


# ---- headers threading (otel.headers) ---------------------------------------


def test_headers_threaded_to_all_three_exporters(monkeypatch):
    # otel.headers must reach every signal's exporter constructor, alongside the endpoint.
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)
    headers = {"authorization": "Bearer tok", "x-scope-orgid": "team"}

    result = otel.init(
        {"otel": {"enabled": True, "endpoint": "http://collector:4318", "headers": headers}}
    )

    assert result is True
    fake.OTLPSpanExporter.assert_called_once_with(endpoint="http://collector:4318", headers=headers)
    fake.OTLPMetricExporter.assert_called_once_with(
        endpoint="http://collector:4318",
        headers=headers,
        preferred_temporality=fake.metric_temporality_delta,
    )
    fake.OTLPLogExporter.assert_called_once_with(endpoint="http://collector:4318", headers=headers)


def test_headers_omitted_when_unset(monkeypatch):
    # No headers configured ⇒ no headers kwarg (exporters keep their endpoint-only signature).
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)

    assert otel.init({"otel": {"enabled": True, "endpoint": "http://c:4317"}}) is True
    fake.OTLPSpanExporter.assert_called_once_with(endpoint="http://c:4317")
    fake.OTLPMetricExporter.assert_called_once_with(
        endpoint="http://c:4317", preferred_temporality=fake.metric_temporality_delta
    )
    fake.OTLPLogExporter.assert_called_once_with(endpoint="http://c:4317")


# ---- metric temporality -------------------------------
#
# ws is a short-lived CLI, so cumulative OTLP counters from each ephemeral process never
# accumulate. init() therefore defaults the *metric* exporter to DELTA temporality for the
# cumulative-prone kinds (Counter/Histogram/ObservableCounter via the delta preference map);
# gauges + up/down counters stay cumulative. An operator forces cumulative with
# otel.metrics_temporality=cumulative, or by setting the OTel-standard env var (which the SDK
# reads itself — we then omit our explicit map so we don't shadow its selection). Traces/logs
# are untouched throughout.

_TEMPORALITY_ENV = "OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE"


def test_metric_exporter_uses_delta_temporality_by_default(monkeypatch):
    # Default (no config, no env): the metric exporter is built with the DELTA preference map,
    # while traces/logs get no temporality kwarg.
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)

    assert otel.init({"otel": {"enabled": True}}) is True
    fake.OTLPMetricExporter.assert_called_once_with(
        preferred_temporality=fake.metric_temporality_delta
    )
    fake.OTLPSpanExporter.assert_called_once_with()  # traces untouched
    fake.OTLPLogExporter.assert_called_once_with()  # logs untouched


def test_metric_exporter_cumulative_config_omits_preference(monkeypatch):
    # otel.metrics_temporality=cumulative ⇒ no preferred_temporality (SDK's cumulative default).
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)

    assert otel.init({"otel": {"enabled": True, "metrics_temporality": "cumulative"}}) is True
    fake.OTLPMetricExporter.assert_called_once_with()
    assert "preferred_temporality" not in fake.OTLPMetricExporter.call_args.kwargs


def test_metric_exporter_env_preference_defers_to_sdk(monkeypatch):
    # The OTel-standard env var is set ⇒ defer to the SDK's own env-based selection: we omit our
    # explicit map even though config would otherwise default to delta.
    monkeypatch.setenv(_TEMPORALITY_ENV, "cumulative")
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)

    assert otel.init({"otel": {"enabled": True}}) is True
    fake.OTLPMetricExporter.assert_called_once_with()
    assert "preferred_temporality" not in fake.OTLPMetricExporter.call_args.kwargs


def test_metric_exporter_delta_config_explicit(monkeypatch):
    # Explicit otel.metrics_temporality=delta is identical to the default ⇒ the delta map is set.
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)

    assert otel.init({"otel": {"enabled": True, "metrics_temporality": "delta"}}) is True
    fake.OTLPMetricExporter.assert_called_once_with(
        preferred_temporality=fake.metric_temporality_delta
    )


def test_otel_metrics_temporality_accessor():
    # default → delta; config override → cumulative; env wins over config.
    assert config.otel_metrics_temporality({}) == "delta"
    assert config.otel_metrics_temporality({"otel": {"metrics_temporality": "cumulative"}}) == (
        "cumulative"
    )


def test_otel_metrics_temporality_env_wins(monkeypatch):
    monkeypatch.setenv(_TEMPORALITY_ENV, "Cumulative")  # case-insensitive, env beats config delta
    assert config.otel_metrics_temporality({"otel": {"metrics_temporality": "delta"}}) == (
        "cumulative"
    )


def test_delta_temporality_map_targets_cumulative_prone_kinds():
    # Real-SDK check (skipped without the ws[otel] extra): the delta map marks
    # Counter/Histogram/ObservableCounter DELTA and leaves gauges + up/down counters cumulative.
    pytest.importorskip("opentelemetry.sdk.metrics")
    from opentelemetry.sdk.metrics import (
        Counter,
        Histogram,
        ObservableCounter,
        ObservableGauge,
        ObservableUpDownCounter,
        UpDownCounter,
    )
    from opentelemetry.sdk.metrics.export import AggregationTemporality

    loaded = otel._load_otel(config.OTEL_PROTOCOL_GRPC)
    delta_map = loaded.metric_temporality_delta
    assert delta_map[Counter] == AggregationTemporality.DELTA
    assert delta_map[Histogram] == AggregationTemporality.DELTA
    assert delta_map[ObservableCounter] == AggregationTemporality.DELTA
    # The cumulative kinds are intentionally absent (SDK fills them cumulative from its default).
    assert UpDownCounter not in delta_map
    assert ObservableUpDownCounter not in delta_map
    assert ObservableGauge not in delta_map


# ---- idempotency ------------------------------------------------------------


def test_init_is_idempotent(monkeypatch):
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)

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
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)

    assert otel.init({"otel": {"enabled": True}}) is True
    assert registered == [otel.shutdown]  # one hook, the module's shutdown()


def test_shutdown_flushes_all_three_providers(monkeypatch):
    # The registered hook must call shutdown() (which force-flushes) on tracer/meter/logger.
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)
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
    monkeypatch.setattr(
        otel, "_load_otel", lambda *_a, **_k: (_ for _ in ()).throw(ImportError("absent"))
    )

    assert otel.init({"otel": {"enabled": True}}) is False
    assert registered == []


def test_flush_hook_registered_once_across_reinit(monkeypatch):
    # Re-init (shutdown resets _initialized) must not stack duplicate atexit hooks.
    registered = []
    monkeypatch.setattr(otel.atexit, "register", lambda fn: registered.append(fn))
    fake = _fake_otel()
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)

    assert otel.init({"otel": {"enabled": True}}) is True
    otel.shutdown()  # resets _initialized so init() can re-wire
    assert otel.init({"otel": {"enabled": True}}) is True

    assert registered == [otel.shutdown]  # registered exactly once despite two inits


def test_shutdown_swallows_provider_errors(monkeypatch):
    # An exporter failure on exit must not raise out of the atexit hook (best-effort flush).
    fake = _fake_otel()
    fake.TracerProvider.return_value.shutdown.side_effect = RuntimeError("collector unreachable")
    monkeypatch.setattr(otel, "_load_otel", lambda *_a, **_k: fake)
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


def test_config_otel_protocol_defaults_to_grpc():
    assert config.otel_protocol({}) == "grpc"
    assert config.otel_protocol({"otel": {}}) == "grpc"


def test_config_otel_protocol_override():
    assert config.otel_protocol({"otel": {"protocol": "http/protobuf"}}) == "http/protobuf"


def test_config_otel_headers_defaults_to_empty():
    assert config.otel_headers({}) == {}
    assert config.otel_headers({"otel": {}}) == {}


def test_config_otel_headers_map_is_stringified():
    cfg = {"otel": {"headers": {"authorization": "Bearer t", "x-tenant": 42}}}
    assert config.otel_headers(cfg) == {"authorization": "Bearer t", "x-tenant": "42"}


# ---- telemetry-neutral validation env --------------------


def test_telemetry_neutral_env_scrubs_otel_and_profile_keeps_rest():
    """The validation child's env drops every OTEL_* var + WS_OBSERVALOOP_PROFILE, forces
    OTEL_SDK_DISABLED=true, and preserves non-telemetry env (PATH …) untouched."""
    base = {
        "OTEL_EXPORTER_OTLP_ENDPOINT": "http://localhost:4317",
        "OTEL_RESOURCE_ATTRIBUTES": "ws.rig=mr",
        "OTEL_EXPORTER_OTLP_PROTOCOL": "grpc",
        "WS_OBSERVALOOP_PROFILE": "dev",
        "PATH": "/sentinel/bin",
        "HOME": "/home/dev",
    }

    env = otel.telemetry_neutral_env(base)

    assert not any(k.startswith("OTEL_") and k != "OTEL_SDK_DISABLED" for k in env)
    assert "WS_OBSERVALOOP_PROFILE" not in env
    assert env["OTEL_SDK_DISABLED"] == "true"
    assert env["PATH"] == "/sentinel/bin"  # non-telemetry env preserved
    assert env["HOME"] == "/home/dev"
    assert base["OTEL_EXPORTER_OTLP_ENDPOINT"] == "http://localhost:4317"  # base not mutated


def test_telemetry_neutral_env_defaults_to_os_environ(monkeypatch):
    """Called bare, it scrubs the *process* env (the worktree overlay seeds OTEL_* there)."""
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4318")
    monkeypatch.setenv("WS_OBSERVALOOP_PROFILE", "prof")

    env = otel.telemetry_neutral_env()

    assert "OTEL_EXPORTER_OTLP_ENDPOINT" not in env
    assert "WS_OBSERVALOOP_PROFILE" not in env
    assert env["OTEL_SDK_DISABLED"] == "true"
