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

### Install the extras

The OTel features require the `[otel]` extra. Install both `[otel]` and `[mcp]` together
so the installed `ws` can also serve as an observaloop MCP client (see [MCP.md](MCP.md)):

```sh
pip install 'ws[otel,mcp]'
# or
uv tool install 'ws[otel,mcp]'
```

`just install` (the development recipe) does this automatically. Without the `[otel]` extra,
otel export is silently disabled: `ws doctor` reports `otel libs: unavailable` and no
signals are sent even when `otel.enabled: true` is set.

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
  pass/fail, plus the commit-flow family: cycle time, stage breakdown, merge-slot contention,
  rework, validation duration, merge outcome, worktree-op duration) via a periodic OTLP metric
  reader.
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

### AGF lifecycle metrics

`ws work` emits lifecycle metrics so the bead pipeline is chartable end-to-end.

| Metric | Kind | Unit | Tags |
|---|---|---|---|
| `ws.work.bead.transitions` | counter | 1 | `ws.bead.transition` (assigned\|claimed\|abandoned\|review_pending\|merged\|molecule_landed) |
| `ws.work.merge.duration` | histogram | s | `ws.merge.kind` (bead\|molecule), `ws.merge.how` |
| `ws.work.validation.runs` | counter | 1 | `ws.validation.result` (pass\|fail), `ws.work.phase` (check\|submit\|molecule) |
| `ws.worktree.events` | counter | 1 | `ws.worktree.op` (create\|remove\|prune), `ws.worktree.outcome` (ok\|error), `ws.rig`, `ws.worktree` |

> **No bead/epic ids on metrics.** `ws.bead` / `ws.epic` are deliberately **not** metric labels —
> they are unbounded-ish control-plane ids that would explode metric cardinality. The bead/epic id
> rides the **verb span** instead (`otel.set_bead` stamps `ws.bead` + derived `ws.epic`), so a
> trace stays filterable by bead while the metric streams stay low-cardinality. Metric attributes
> are limited to bounded dimensions (`ws.rig`, kind/phase/result/how/op/outcome).

Agent-dispatch coordination is traced via an OpenTelemetry GenAI span (`invoke_agent {agent}`)
emitted by `record_agent_dispatch` each time the coordinator hands a bead to a developer crew.
The span carries `gen_ai.operation.name`, `gen_ai.system`, and `gen_ai.agent.name`; the bead
brief is attached as a droppable span event.

### Commit-flow (DORA) metrics

Beyond the coarse lifecycle counters above, `ws work` emits a **commit-flow** metric family at the
merge seam (and the worktree-fleet ops) so a rig's delivery pipeline is measurable in DORA/flow
terms — lead time, stage breakdown, queue contention, rework, and first-pass quality. Every
instrument is a no-op when otel is off and carries **bounded attributes only** (never a bead id).

| Metric | Kind | Unit | Tags |
|---|---|---|---|
| `ws.work.cycle_time` | histogram | s | `ws.merge.kind`, `ws.rig` |
| `ws.work.cycle_time.active` | histogram | s | `ws.merge.kind`, `ws.rig` |
| `ws.work.stage.coding` | histogram | s | `ws.merge.kind`, `ws.rig` |
| `ws.work.stage.review_wait` | histogram | s | `ws.merge.kind`, `ws.rig` |
| `ws.work.stage.merge_latency` | histogram | s | `ws.merge.kind`, `ws.rig` |
| `ws.work.rework.count` | histogram | 1 | `ws.merge.kind`, `ws.rig` |
| `ws.work.merge_slot.wait` | histogram | s | `ws.merge.kind`, `ws.rig` |
| `ws.work.merge_slot.hold` | histogram | s | `ws.merge.kind`, `ws.rig` |
| `ws.work.validation.duration` | histogram | s | `ws.work.phase` (check\|submit\|molecule), `ws.validation.result`, `ws.rig` |
| `ws.work.merge.outcome` | counter | 1 | `ws.merge.kind`, `ws.merge.how` (clean\|rebased\|union\|no_ff\|conflict), `ws.rig` |
| `ws.worktree.op.duration` | histogram | s | `ws.worktree.op` (create\|remove\|prune), `ws.worktree.outcome` (ok\|error), `ws.rig` |

**Flow definitions** (a bead's active cycle decomposes into the three stages):

- **cycle_time** = `now − created_at` (total lead time, idea→merged).
- **cycle_time.active** = `now − started_at` (work started→merged, excludes backlog wait).
- **stage.coding** = `review_pending_at − started_at` (start of work → first submit for review).
- **stage.review_wait** = `gate_closed_at − review_pending_at` (time a bead sits in review).
- **stage.merge_latency** = `now − gate_closed_at` (approved → actually merged; merge-queue wait).
- **rework.count** = number of `review→changes-requested` rounds for the bead.
- **merge_slot.wait / .hold** = contention on the rig's serialized merge slot (acquire wait,
  then hold duration around the land).
- **merge.outcome** = the realized merge path (`how`); `conflict` is emitted on the fail branch
  *before* the merge raises, so the success/conflict mix is chartable.

**Molecule asymmetry.** A molecule land (`ws work merge --molecule`) emits **cycle_time(.active) +
merge_slot + merge.outcome + validation.duration only** — never the per-bead `stage.*` or
`rework.count` (those are bead-scoped concepts, not epic-scoped).

**At-merge bd-read contract (best-effort + skew-guarded).** The cycle/stage/rework values are
derived at merge time from cheap `bd` reads: the bead's `created_at`/`started_at` (reused from the
`bd show` already done for the merge guard), `bd list --parent <id> --include-infra` (the
`review→pending` event's `created_at` + the `review→changes-requested` event count), and
`bd gate list --all` (the review gate's open/closed timestamps, matched by the `review <sha>`
reason). These reads are **strictly best-effort**: every read is wrapped so a slow or failing
`bd` never blocks a successful merge — it simply records nothing for the affected metric. Any
**negative delta** (clock skew / out-of-order events) is **skipped**, never recorded. The merge
itself has already happened by the time these fire, so telemetry can never turn a green land red.

### Short-lived-process metrics

`ws` is invoked as a fresh process for each CLI command. In the default OTel cumulative
temporality, two problems undermine metric usability:

- **Per-process `service.instance.id`**: the OTel SDK stamps a unique UUID on each process's
  Resource. Cumulative counters are keyed by that UUID, so each `ws` invocation starts its
  counter from zero — Prometheus sees a swarm of single-sample series that never accumulate
  and can never produce a useful `rate()` or `increase()`.
- **Resource attributes not visible as metric labels**: `ws.rig`, `ws.worktree`, `ws.role`,
  and `observaloop.profile` are stamped on the OTel Resource, not on metric datapoints.
  Prometheus does not automatically promote Resource attributes to series labels, so the
  dimensions needed for dashboard queries are missing without collector-side reshaping.

**ws's fix** ships as two pieces that work together:

**1. Delta temporality by default.** The OTLP metric exporter defaults to `DELTA` for
counters and histograms (`otel.metrics_temporality`, default `delta`). Each short-lived
process reports only its own delta; the collector accumulates across processes. Override to
`cumulative` via `otel.metrics_temporality: cumulative` in config, or set the standard env
`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` (env takes precedence over config).

**2. CLI-metrics collector preset** (`cli-metrics-preset.yaml`, applied automatically by
`ws rig init --observaloop`). The preset reshapes only the metrics pipeline of the profile
collector (traces and logs are unchanged):

- `resource/strip_instance` — deletes `service.instance.id` from the Resource so all `ws`
  processes share one resource stream instead of fragmenting into per-process series.
- `transform/promote_ws_attrs` — copies `ws.rig`, `ws.worktree`, `ws.role`, and
  `observaloop.profile` from Resource attributes onto every metric datapoint, making them
  queryable as Prometheus series labels.
- `deltatocumulative` — accumulates the delta pushes into running totals so `rate()` and
  `increase()` return meaningful data.

Metric panels in the bundled dashboard (the `$ws_rig` and `$ws_worktree` template variables
and the per-worktree breakdown's `observaloop_profile` grouping) require the preset and delta
temporality to be applied. Trace panels work without the preset.

Verify end-to-end with:

```sh
just metrics-verify
```

The harness (`tests/test_metrics_verify.py`) emits counter samples with delta temporality,
then polls Prometheus to confirm: the series carries `ws_rig` and `observaloop_profile`
labels, has no `service_instance_id`, and that `rate()` returns data.

### Grafana dashboard panels

The bundled `ws-telemetry` dashboard (applied by `ws rig init --observaloop`) includes rows
covering these instruments:

**AGF lifecycle row** (`ws.work.*`):

- **Bead transitions** — `increase(ws.work.bead.transitions[$flow_window])` split by
  `ws.bead.transition`; the count of each lifecycle stage over the flow window.
- **Merge duration p50/p95** — `histogram_quantile` over `ws.work.merge.duration` split by
  `ws.merge.kind`; compares bead vs molecule merge latency at the 50th and 95th percentiles.
- **Validation pass/fail** — `increase(ws.work.validation.runs[$flow_window])` split by
  `ws.validation.result` and `ws.work.phase`; exposes which phase (check, submit, molecule) is
  failing.
- **Agent dispatch spans** — a Tempo/TraceQL panel (`{ name =~ "invoke_agent.*" }`) listing
  coordinator-to-developer dispatch spans with `gen_ai.agent.name` and status.

**Worktree events row** (`ws.worktree.events`):

- **Worktree events by op + outcome** — `increase(ws.worktree.events[$flow_window])` split by
  `ws.worktree.op` (create/remove/prune) and `ws.worktree.outcome` (ok/error); surfaces
  worktree churn and any provisioning errors.

**Commit Flow row** (the commit-flow / DORA family) — throughput (`increase` of
`ws.work.merge.outcome` by kind), cycle time p50/p95 (+active), the coding/review_wait/merge_latency
stage breakdown, flow efficiency % (`coding / (coding + review_wait + merge_slot_wait)`), review
clearance p90, merge-slot wait/hold p95, rework rounds p90 + total, first-pass yield (pass/total
validation by phase), validation duration p95, the merge-outcome mix by `ws.merge.how`, abandon
rate (`abandoned / (abandoned + merged)`), and worktree-op duration p95 + worktree errors.

**Units convention.** Latency/duration panels (CLI/MCP/merge/cycle/stage/slot/validation/worktree-op
histograms) stay in **seconds**; the CLI/MCP RED panels stay as `rate()`; **counter** panels
(bead transitions, validation runs, worktree events, throughput, merge-outcome mix, worktree
errors) are re-unitted to `increase(...[$flow_window])` with a **short** unit so a discrete count
over the chosen window is read directly.

All panels respect the `$ws_rig` and `$ws_worktree` template variables (both
`label_values(...)`-driven with `allValue: .*`), and the count/throughput panels also respect the
**`$flow_window`** variable — a custom selector defaulting to **1h** with options **5m / 15m / 1h /
1d** that sets the `increase()` window. Scoping to a specific rig/worktree (and window) filters
every panel to that context. This is especially useful for watching `ws` under its own
integration-test fixtures: run the verify harness with `otel.enabled: true` pointing at a local
collector and the AGF-lifecycle + worktree-events + Commit Flow panels populate in real time,
scoped to the fixture's rig/worktree identity.

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
