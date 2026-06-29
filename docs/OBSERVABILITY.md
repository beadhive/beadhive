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
  endpoint: http://localhost:4317   # OTLP gRPC endpoint (ws otel up default)
  # endpoint: http://localhost:4318 # OTLP HTTP/protobuf alternative
  rig: workspace                    # stamped as ws.rig on every OTel Resource (optional)
```

The `OTEL_EXPORTER_OTLP_ENDPOINT` environment variable takes precedence over `otel.endpoint`
so you can point at a different collector without editing config:

```sh
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 ws doctor
```

When both are unset, the OTLP exporter uses its built-in default (`localhost:4317`).

### What is exported

When enabled, `ws` sets up:

- **Traces** — spans for CLI verbs and bead lifecycle events via a BatchSpanProcessor.
- **Metrics** — counters and histograms (merge duration, bead transitions, validation
  pass/fail) via a periodic OTLP metric reader.
- **Logs** — the structlog/stdlib root logger is bridged into OTel logs via a
  `LoggingHandler` so every diagnostic lands in the same backend.

All signals share one `Resource` with `service.name=ws`, `service.version`, and `ws.rig`
(when configured).

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
