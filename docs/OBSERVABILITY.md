# Observability

`ws` emits structured logs and — when opted in — OpenTelemetry traces, metrics, and logs.
Everything is **disabled or no-op by default**; nothing exports without explicit configuration.

## Logging

Diagnostics flow through structlog on stderr (never stdout). Command results stay on stdout
so piped scripts and JSON-log parsing don't collide.

### Modes — `log.format`

Set in `~/.ws/config.yaml` under `log.format`:

| Value | Behaviour |
|---|---|
| `auto` | ConsoleRenderer (rich/ANSI) on a TTY; JSONRenderer otherwise. **Default.** |
| `rich` | Always ConsoleRenderer, even when stdout is piped. |
| `json` | Always JSONRenderer — one structured object per line, machine-friendly. |

### Level — `log.level`

`log.level` controls the minimum severity emitted. Accepts the standard names
(`debug`, `info`, `warning`, `error`, `critical`). Default: `info`.

```yaml
log:
  format: auto   # auto | rich | json
  level: info    # debug | info | warning | error | critical
```

### Log ↔ trace correlation

When OpenTelemetry is active (see below), every log record is enriched with `trace_id` and
`span_id` fields so logs and traces can be joined in Grafana.

## OpenTelemetry

OTel is **disabled by default** and requires the optional extra to export anything.

### Install the extra

```sh
pip install 'ws[otel]'
# or
uv tool install 'ws[otel]'
```

If `otel.enabled` is `true` but the extra is absent, ws warns once and continues — it never
crashes.

### Configuration

```yaml
otel:
  enabled: true
  endpoint: http://localhost:4317   # OTLP endpoint; env OTEL_EXPORTER_OTLP_ENDPOINT wins
  protocol: grpc                    # grpc (default) | http/protobuf
  headers:                          # optional: auth/routing headers for hosted collectors
    Authorization: "Bearer <token>"
  rig: workspace                    # stamped as ws.rig on every OTel Resource (optional)
```

The `OTEL_EXPORTER_OTLP_ENDPOINT` environment variable takes precedence over `otel.endpoint`
so you can point at a different collector without editing config:

```sh
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 ws doctor
```

When both are unset, the OTLP exporter uses its built-in default (`localhost:4317`).

`otel.protocol` selects the OTLP wire format: `grpc` (default, port 4317) or `http/protobuf`
(port 4318). An unrecognised value fails loudly — there is no silent fallback to gRPC.

`otel.headers` is a string-to-string map threaded into every OTLP exporter (traces, metrics,
and logs). Use it to pass authentication tokens or routing keys required by hosted collectors
such as Grafana Cloud, Honeycomb, or Datadog's OTLP intake.

### Bring-your-own collector

ws emits OTLP signals but does not run a collector — you provide the endpoint. The quickest
option for local development is a single `docker run`:

```sh
docker run --rm -p 3000:3000 -p 4317:4317 -p 4318:4318 grafana/otel-lgtm
```

Then point ws at it:

```yaml
otel:
  enabled: true
  endpoint: http://localhost:4317   # gRPC (default)
  # endpoint: http://localhost:4318 # HTTP/protobuf; also set protocol: http/protobuf
```

Grafana is reachable at <http://localhost:3000> (admin / admin). The `ws otel up` command
(below) manages the same stack via a compose file for persistent local use.

### What is exported

When enabled, `ws` sets up:

- **Traces** — spans for CLI verbs and bead lifecycle events via a BatchSpanProcessor.
- **Metrics** — counters and histograms (merge duration, bead transitions, validation
  pass/fail) via a periodic OTLP metric reader.
- **Logs** — the structlog/stdlib root logger is bridged into OTel logs via a
  `LoggingHandler` so every diagnostic lands in the same backend.

All signals share one `Resource` with `service.name=ws`, `service.version`, and `ws.rig`
(when configured).

### Invocation metrics

ws emits invocation counters and latency histograms at the CLI and MCP entry seams, plus an
error counter at each boundary. All instruments are no-ops when otel is off.

| Metric | Kind | Unit | Tags |
|---|---|---|---|
| `ws.cli.invocations` | counter | 1 | `ws.cli.command`, `ws.cli.outcome` (`ok`\|`error`) |
| `ws.cli.duration` | histogram | s | same |
| `ws.mcp.tool.invocations` | counter | 1 | `ws.mcp.tool`, `ws.mcp.outcome` (`ok`\|`error`) |
| `ws.mcp.tool.duration` | histogram | s | same |
| `ws.errors` | counter | 1 | `ws.error.boundary` (`cli`\|`mcp`), `ws.error.kind` (exception class) |

Unhandled exceptions at either boundary are observed across all three signals: a structlog
`cli_command_error` or `mcp_tool_error` event (always, even otel-off), the active span's
status set to ERROR with the exception recorded, and `ws.errors` incremented. The user sees
a concise `✗ ExcType: message` line on stderr — never a raw traceback.

## LGTM stack — `ws otel up`

`ws` ships a bundled `grafana/otel-lgtm` compose file that brings up a complete local
observability stack with a single command.

```sh
ws otel up    # start Grafana + OTel Collector + Loki + Tempo + Mimir
ws otel down  # stop
ws otel logs  # stream container logs
ws otel ps    # show service status
```

| Port | Service |
|---|---|
| 3000 | Grafana UI — <http://localhost:3000> (default credentials: admin / admin) |
| 4317 | OTLP gRPC collector |
| 4318 | OTLP HTTP/protobuf collector |

The compose file is seeded to `~/.ws/docker-compose.otel.yml` on first `ws otel up`. The
container runtime is shared with the Dolt backend setting (`dolt.backend`: `colima` \|
`docker` \| `podman` \| `none`).

After `ws otel up`, point ws at the local stack:

```yaml
otel:
  enabled: true
  endpoint: http://localhost:4317
```

Then run any ws command and open Grafana → Explore → Loki (logs) / Tempo (traces) /
Prometheus (metrics).

## Verification

`tests/test_otel_verify.py` is an opt-in live harness that confirms telemetry actually flows
from ws to a real OTLP collector. Install the required extras first:

```sh
uv sync --extra otel --extra mcp
```

Start a collector (the `docker run` one-liner above works), then run:

```sh
just otel-verify                          # gRPC, default endpoint http://localhost:4317
just otel-verify http://localhost:4318    # different endpoint

# HTTP/protobuf transport:
WS_OTEL_PROTOCOL=http/protobuf just otel-verify http://localhost:4318

# Or run pytest directly:
WS_OTEL_VERIFY=1 OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4317 \
    uv run pytest tests/test_otel_verify.py -v -s
```

After the run, check your collector for:

- **Traces** — spans `cli.verify`, `mcp.verify`, `cli.error.verify`, `mcp.error.verify`
  (`service.name=ws`)
- **Metrics** — `ws.cli.invocations`, `ws.cli.duration`, `ws.mcp.tool.invocations`,
  `ws.mcp.tool.duration`, `ws.errors` (two entries — boundary `cli` and `mcp`)
- **Logs** — `otel_initialized` and `mcp_tool_error` records bridged via LoggingHandler

The harness skips by default — both `WS_OTEL_VERIFY` and `OTEL_EXPORTER_OTLP_ENDPOINT` must
be set — so `just check` (CI default) needs no collector.

## GenAI spans (experimental)

Experimental support for the OpenTelemetry `gen_ai.*` semantic conventions is landing via a
parallel bead (cit.5). When active, AI-model interactions emit spans with standard `gen_ai.*`
attributes (model, input/output tokens, finish reason). These appear in Tempo alongside the
regular ws verb spans.

## `ws doctor` observability status

`ws doctor` reports the resolved observability configuration:

```text
# Observability
  log.format: auto
  log.level: info
  otel.enabled: false
  otel libs: unavailable (install: pip install 'ws[otel]')
  endpoint: (not set)
```
