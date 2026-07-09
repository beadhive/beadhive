"""Metrics live-verification harness — opt-in; skipped by default in CI.

PURPOSE
-------
Confirms that ws CLI metrics are USABLE end-to-end when the CLI-metrics preset + delta
temporality are applied to the collector profile.  Proves the fix: a single accumulating
series per (rig, command) — NOT one-per-process — carrying ws.rig / observaloop.profile /
ws.role labels, and no service_instance_id fragmentation.

This is NOT a mocked test; it drives the real OTel SDK, emits multiple metric samples via
the otel helpers, then queries Prometheus to assert the resulting series shape.

PREREQUISITES
-------------
1. A running OTLP collector with the CLI-metrics preset applied to its profile:
   - delta-to-cumulative conversion (deltatocumulative processor or connector)
   - service.instance.id stripped from resource attributes or labels
   - ws.* resource attributes promoted to datapoint labels
     (e.g. resource_to_telemetry or a transform processor)

2. Prometheus scraping that collector, reachable at WS_OTEL_VERIFY_PROM
   (default: http://localhost:9090)

3. The otel.rig + observaloop.profile attributes present in the Resource.  The rig is
   auto-derived from cwd when otel.rig is unset; the profile comes from
   WS_OBSERVALOOP_PROFILE or observaloop.profile in config.  Both must resolve for the
   label assertions to pass — this is intentional: the test verifies the full preset path.

HOW TO RUN
----------
1. Apply the CLI-metrics preset to your rig's collector profile (ws rig init --observaloop
   does this automatically in):

       ws rig init --observaloop  # stamps the preset onto the active profile

2. Start the rig's collector stack:

       # e.g. grafana/otel-lgtm or your rig's docker-compose
       docker run --rm -p 4317:4317 -p 9090:9090 grafana/otel-lgtm

3. Run the harness:

       just metrics-verify
       # or explicitly:
       WS_METRICS_VERIFY=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \\
           WS_OTEL_VERIFY_PROM=http://localhost:9090 WS_OBSERVALOOP_PROFILE=<profile> \\
           uv run pytest tests/test_metrics_verify.py -v -s

   HTTP transport (port 4318):

       WS_METRICS_VERIFY=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \\
           WS_OTEL_PROTOCOL=http/protobuf WS_OTEL_VERIFY_PROM=http://localhost:9090 \\
           WS_OBSERVALOOP_PROFILE=<profile> \\
           uv run pytest tests/test_metrics_verify.py -v -s

WHAT IS ASSERTED
----------------
After emitting several ws.cli.invocations counter samples with delta temporality:

  1. ws_cli_invocations_total exists in Prometheus (metric survived the collector pipeline)
  2. service_instance_id is ABSENT from the series labels
     (the preset strips it — without it every process creates a new series)
  3. ws_rig is PRESENT in the labels (preset promoted the ws.rig resource attribute)
  4. observaloop_profile is PRESENT in the labels (preset promoted observaloop.profile)
  5. rate(ws_cli_invocations_total[10m]) returns a non-empty result
     (series is accumulating via deltatocumulative — rate() has usable data)

GATING
------
WS_METRICS_VERIFY and OTEL_EXPORTER_OTLP_ENDPOINT must both be set; absent either, every
test in this module is skipped cleanly so `just check` (CI default) needs no collector.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

from beadhive import config, otel

_SKIP_REASON = (
    "live-metrics verification skipped — "
    "set WS_METRICS_VERIFY=1 and OTEL_EXPORTER_OTLP_ENDPOINT to run against a live "
    "collector with the CLI-metrics preset applied; "
    "set WS_OTEL_VERIFY_PROM (default http://localhost:9090) for the Prometheus endpoint"
)

pytestmark = pytest.mark.skipif(
    not (os.getenv("WS_METRICS_VERIFY") and os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")),
    reason=_SKIP_REASON,
)

# Prometheus HTTP API endpoint (default: localhost:9090).
_PROM_URL = os.getenv("WS_OTEL_VERIFY_PROM", "http://localhost:9090")
# Command label stamped on the test emissions — lets queries scope to this harness only.
_VERIFY_COMMAND = "metrics_verify"
# How long to wait for the metric to appear in Prometheus (collector receive + scrape delay).
_POLL_TIMEOUT = 90
# Seconds between Prometheus polls.
_POLL_INTERVAL = 5


@pytest.fixture(scope="module", autouse=True)
def _emit_metrics():
    """Initialize the real OTel SDK with delta temporality, emit metric samples, flush.

    Emits several ws.cli.invocations counter increments tagged with a known command label
    so the downstream assertions can scope queries to this harness's emissions only.
    Shuts down (force-flushing the batch processor) before yielding so samples reach the
    collector before any test starts polling Prometheus.

    Mirrors the _live_otel fixture in test_otel_verify.py: same guard pattern, same
    otel.shutdown() bookend, same skip when init() returns False.
    """
    if not (os.getenv("WS_METRICS_VERIFY") and os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT")):
        yield
        return

    # Reset any leftover init state from earlier test modules.
    otel.shutdown()

    protocol = os.getenv("WS_OTEL_PROTOCOL", config.OTEL_PROTOCOL_GRPC)
    # Enable otel with explicit delta temporality — the prerequisite the preset relies on.
    cfg = {"otel": {"enabled": True, "protocol": protocol, "metrics_temporality": "delta"}}
    initialized = otel.init(cfg)
    if not initialized:
        pytest.skip(
            "otel.init() returned False — install the ws[otel] extra "
            "(uv sync --extra otel) and verify OTEL_EXPORTER_OTLP_ENDPOINT is reachable"
        )

    # Emit several CLI invocation metrics with the harness-specific command label.
    # Three separate emissions simulate the delta pattern: each is a fresh process delta;
    # the collector's deltatocumulative processor accumulates them into one series.
    for _ in range(3):
        t0 = time.monotonic()
        time.sleep(0.01)
        otel.record_cli_invocation(_VERIFY_COMMAND, "ok", time.monotonic() - t0)

    # Force-flush: provider.shutdown() drains the batch processor so samples reach
    # the collector here, not at atexit, giving the poll loop the full timeout window.
    otel.shutdown()

    yield  # tests run after the flush; Prometheus polling happens inside the test


# ---------------------------------------------------------------------------
# Prometheus query helpers
# ---------------------------------------------------------------------------


def _prom_query(expr: str) -> list[dict]:
    """Instant-vector Prometheus HTTP API query; returns the result list (may be empty)."""
    url = f"{_PROM_URL}/api/v1/query?{urllib.parse.urlencode({'query': expr})}"
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            payload = json.loads(resp.read())
    except urllib.error.URLError as exc:
        raise RuntimeError(
            f"Cannot reach Prometheus at {_PROM_URL}: {exc}\n"
            "Set WS_OTEL_VERIFY_PROM to your Prometheus HTTP endpoint."
        ) from exc
    if payload.get("status") != "success":
        raise RuntimeError(f"Prometheus query error: {payload.get('error', payload)}")
    return payload["data"]["result"]


def _poll_prom(expr: str, *, timeout: int) -> list[dict]:
    """Poll Prometheus until ``expr`` returns non-empty results or ``timeout`` seconds pass."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        results = _prom_query(expr)
        if results:
            return results
        time.sleep(_POLL_INTERVAL)
    return []


# ---------------------------------------------------------------------------
# The live check
# ---------------------------------------------------------------------------


def test_metrics_accumulate_with_labels():
    """ws metrics form one accumulating series per (rig, command) with ws.* labels.

    Asserts the five conditions that prove the CLI-metrics preset + delta fix works:

    1. ws_cli_invocations_total exists in Prometheus — metric survived the pipeline.
    2. service_instance_id is ABSENT — preset stripped per-process fragmentation.
    3. ws_rig is PRESENT — preset promoted the ws.rig resource attribute to a label.
    4. observaloop_profile is PRESENT — preset promoted observaloop.profile to a label.
    5. rate() is non-empty — series accumulates (deltatocumulative), rate() has data.

    Failure on (2)-(4) most likely means the CLI-metrics preset is not applied to the
    collector profile.  See the module docstring for the full prerequisites.
    """
    # 1. Poll until the metric appears; allows for collector-receive + Prometheus scrape delay.
    metric_q = f'ws_cli_invocations_total{{ws_cli_command="{_VERIFY_COMMAND}"}}'
    results = _poll_prom(metric_q, timeout=_POLL_TIMEOUT)
    assert results, (
        f"ws_cli_invocations_total{{ws_cli_command={_VERIFY_COMMAND!r}}} not found in "
        f"Prometheus after {_POLL_TIMEOUT}s.\n"
        "Check that OTEL_EXPORTER_OTLP_ENDPOINT points to a collector with the CLI-metrics "
        "preset and that WS_OTEL_VERIFY_PROM points to the Prometheus endpoint."
    )

    labels = results[0]["metric"]

    # 2. service_instance_id must be absent — the preset strips the per-process UUID so all
    #    ws processes share a single series rather than each creating their own.
    assert "service_instance_id" not in labels, (
        f"service_instance_id is present in labels: {labels}\n"
        "The CLI-metrics preset should strip this resource attribute.  "
        "Verify the preset's resource filter / transform is applied to the collector profile."
    )

    # 3. ws_rig must be present — promoted from the ws.rig resource attribute by the preset.
    assert "ws_rig" in labels, (
        f"ws_rig is absent from labels: {labels}\n"
        "The preset promotes ws.* resource attributes to datapoint labels.  "
        "Ensure otel.rig is set in config or is auto-derived from cwd, "
        "and verify the preset's resource_to_telemetry / transform is applied."
    )

    # 4. observaloop_profile must be present — promoted from the observaloop.profile resource attr.
    assert "observaloop_profile" in labels, (
        f"observaloop_profile is absent from labels: {labels}\n"
        "Set WS_OBSERVALOOP_PROFILE or observaloop.profile in config so the resource "
        "carries the profile attribute, and verify the preset promotes it to labels."
    )

    # 5. rate() must return data — proves the series accumulates across delta pushes.
    #    Uses a 10-minute window to span at least two Prometheus scrape intervals (~15s each).
    rate_q = (
        f'rate(ws_cli_invocations_total{{ws_cli_command="{_VERIFY_COMMAND}"}}[10m])'
    )
    rate_results = _poll_prom(rate_q, timeout=60)
    assert rate_results, (
        "rate(ws_cli_invocations_total[10m]) returned empty after 60s.\n"
        "The series exists but rate() has no data — the deltatocumulative processor may "
        "not be accumulating correctly, or fewer than two Prometheus scrapes have occurred.  "
        "Verify the preset's pipeline includes the deltatocumulative connector/processor."
    )
