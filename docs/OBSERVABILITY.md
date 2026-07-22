# Observability

`bh` emits structured logs and — when opted in — OpenTelemetry traces, metrics, and logs.
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

The OTel features require the `[otel]` extra. (FastMCP is a core dependency, so the
installed `bh` already serves as an observaloop MCP client — see [MCP.md](MCP.md).)

```sh
pip install 'beadhive[otel]'
# or
uv tool install 'beadhive[otel]'
```

`just install` (the development recipe) does this automatically. Without the `[otel]` extra,
otel export is silently disabled: `bh doctor` reports `otel libs: unavailable` and no
signals are sent even when `otel.enabled: true` is set.

If `otel.enabled` is `true` but the extra is absent, bh warns once and continues — it never
crashes.

### Configuration

```yaml
otel:
  enabled: true
  endpoint: http://localhost:4317   # OTLP endpoint; env OTEL_EXPORTER_OTLP_ENDPOINT wins
  protocol: grpc                    # grpc (default) | http/protobuf
  headers:                          # optional: auth/routing headers for hosted collectors
    Authorization: "Bearer <token>"
  hive: workspace                   # stamped as bh.hive on every OTel Resource (optional)
```

The `OTEL_EXPORTER_OTLP_ENDPOINT` environment variable takes precedence over `otel.endpoint`
so you can point at a different collector without editing config:

```sh
OTEL_EXPORTER_OTLP_ENDPOINT=http://collector:4317 bh doctor
```

When both are unset, the OTLP exporter uses its built-in default (`localhost:4317`).

`otel.protocol` selects the OTLP wire format: `grpc` (default, port 4317) or `http/protobuf`
(port 4318). An unrecognised value fails loudly — there is no silent fallback to gRPC.

`otel.headers` is a string-to-string map threaded into every OTLP exporter (traces, metrics,
and logs). Use it to pass authentication tokens or routing keys required by hosted collectors
such as Grafana Cloud, Honeycomb, or Datadog's OTLP intake.

### Bring-your-own collector

bh emits OTLP signals but does not run a collector — you provide the endpoint. The quickest
option for local development is a single `docker run`:

```sh
docker run --rm -p 3000:3000 -p 4317:4317 -p 4318:4318 grafana/otel-lgtm
```

Then point bh at it:

```yaml
otel:
  enabled: true
  endpoint: http://localhost:4317   # gRPC (default)
  # endpoint: http://localhost:4318 # HTTP/protobuf; also set protocol: http/protobuf
```

Grafana is reachable at <http://localhost:3000> (admin / admin). The `bh otel up` command
(below) manages the same stack via a compose file for persistent local use.

### What is exported

When enabled, `bh` sets up:

- **Traces** — spans for CLI verbs and bead lifecycle events via a BatchSpanProcessor.
- **Metrics** — counters and histograms (merge duration, bead transitions, validation
  pass/fail, plus the commit-flow family: cycle time, stage breakdown, merge-slot contention,
  rework, validation duration, merge outcome, worktree-op duration) via a periodic OTLP metric
  reader.
- **Logs** — the structlog/stdlib root logger is bridged into OTel logs via a
  `LoggingHandler` so every diagnostic lands in the same backend.

All signals share one `Resource` with `service.name=bh`, `service.version`, and `bh.hive`
(when configured).

### Invocation metrics

bh emits invocation counters and latency histograms at the CLI and MCP entry seams, plus an
error counter at each boundary. All instruments are no-ops when otel is off.

| Metric | Kind | Unit | Tags |
|---|---|---|---|
| `bh.cli.invocations` | counter | 1 | `bh.cli.command`, `bh.cli.outcome` (`ok`\|`error`) |
| `bh.cli.duration` | histogram | s | same |
| `bh.mcp.tool.invocations` | counter | 1 | `bh.mcp.tool`, `bh.mcp.outcome` (`ok`\|`error`) |
| `bh.mcp.tool.duration` | histogram | s | same |
| `bh.errors` | counter | 1 | `bh.error.boundary` (`cli`\|`mcp`), `bh.error.kind` (exception class) |

Unhandled exceptions at either boundary are observed across all three signals: a structlog
`cli_command_error` or `mcp_tool_error` event (always, even otel-off), the active span's
status set to ERROR with the exception recorded, and `bh.errors` incremented. The user sees
a concise `✗ ExcType: message` line on stderr — never a raw traceback.

### Bead lifecycle metrics

`bh work` emits lifecycle metrics so the bead pipeline is chartable end-to-end.

| Metric | Kind | Unit | Tags |
|---|---|---|---|
| `bh.work.bead.transitions` | counter | 1 | `bh.bead.transition` (assigned\|claimed\|abandoned\|review_pending\|merged\|molecule_landed) |
| `bh.work.merge.duration` | histogram | s | `bh.merge.kind` (bead\|molecule), `bh.merge.how` |
| `bh.work.validation.runs` | counter | 1 | `bh.validation.result` (pass\|fail), `bh.work.phase` (check\|submit\|molecule) |
| `bh.worktree.events` | counter | 1 | `bh.worktree.op` (create\|remove\|prune), `bh.worktree.outcome` (ok\|error), `bh.hive`, `bh.worktree` |

> **No bead/epic ids on metrics.** `bh.bead` / `bh.epic` are deliberately **not** metric labels —
> they are unbounded-ish control-plane ids that would explode metric cardinality. The bead/epic id
> rides the **verb span** instead (`otel.set_bead` stamps `bh.bead` + derived `bh.epic`), so a
> trace stays filterable by bead while the metric streams stay low-cardinality. Metric attributes
> are limited to bounded dimensions (`bh.hive`, kind/phase/result/how/op/outcome).

Agent-dispatch coordination is traced via an OpenTelemetry GenAI span (`invoke_agent {agent}`)
emitted by `record_agent_dispatch` each time the dispatcher hands a bead to a developer.
The span carries `gen_ai.operation.name`, `gen_ai.system`, and `gen_ai.agent.name`; the bead
brief is attached as a droppable span event.

### Commit-flow (DORA) metrics

Beyond the coarse lifecycle counters above, `bh work` emits a **commit-flow** metric family at the
merge seam (and the worktree-fleet ops) so a hive's delivery pipeline is measurable in DORA/flow
terms — lead time, stage breakdown, queue contention, rework, and first-pass quality. Every
instrument is a no-op when otel is off and carries **bounded attributes only** (never a bead id).

| Metric | Kind | Unit | Tags |
|---|---|---|---|
| `bh.work.cycle_time` | histogram | s | `bh.merge.kind`, `bh.hive` |
| `bh.work.cycle_time.active` | histogram | s | `bh.merge.kind`, `bh.hive` |
| `bh.work.stage.coding` | histogram | s | `bh.merge.kind`, `bh.hive` |
| `bh.work.stage.review_wait` | histogram | s | `bh.merge.kind`, `bh.hive` |
| `bh.work.stage.merge_latency` | histogram | s | `bh.merge.kind`, `bh.hive` |
| `bh.work.rework.count` | histogram | 1 | `bh.merge.kind`, `bh.hive` |
| `bh.work.merge_slot.wait` | histogram | s | `bh.merge.kind`, `bh.hive` |
| `bh.work.merge_slot.hold` | histogram | s | `bh.merge.kind`, `bh.hive` |
| `bh.work.validation.duration` | histogram | s | `bh.work.phase` (check\|submit\|molecule), `bh.validation.result`, `bh.hive` |
| `bh.work.merge.outcome` | counter | 1 | `bh.merge.kind`, `bh.merge.how` (clean\|rebased\|union\|no_ff\|conflict), `bh.hive` |
| `bh.worktree.op.duration` | histogram | s | `bh.worktree.op` (create\|remove\|prune), `bh.worktree.outcome` (ok\|error), `bh.hive` |

**Flow definitions** (a bead's active cycle decomposes into the three stages):

- **cycle_time** = `now − created_at` (total lead time, idea→merged).
- **cycle_time.active** = `now − started_at` (work started→merged, excludes backlog wait).
- **stage.coding** = `review_pending_at − started_at` (start of work → first submit for review).
- **stage.review_wait** = `gate_closed_at − review_pending_at` (time a bead sits in review).
- **stage.merge_latency** = `now − gate_closed_at` (approved → actually merged; merge-queue wait).
- **rework.count** = number of `review→changes-requested` rounds for the bead.
- **merge_slot.wait / .hold** = contention on the hive's serialized merge slot (acquire wait,
  then hold duration around the land).
- **merge.outcome** = the realized merge path (`how`); `conflict` is emitted on the fail branch
  *before* the merge raises, so the success/conflict mix is chartable.

**Molecule asymmetry.** A molecule land (`bh work merge --molecule`) emits **cycle_time(.active) +
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

`bh` is invoked as a fresh process for each CLI command. In the default OTel cumulative
temporality, two problems undermine metric usability:

- **Per-process `service.instance.id`**: the OTel SDK stamps a unique UUID on each process's
  Resource. Cumulative counters are keyed by that UUID, so each `bh` invocation starts its
  counter from zero — Prometheus sees a swarm of single-sample series that never accumulate
  and can never produce a useful `rate()` or `increase()`.
- **Resource attributes not visible as metric labels**: `bh.hive`, `bh.worktree`, `bh.role`,
  and `observaloop.profile` are stamped on the OTel Resource, not on metric datapoints.
  Prometheus does not automatically promote Resource attributes to series labels, so the
  dimensions needed for dashboard queries are missing without collector-side reshaping.

**bh's fix** ships as two pieces that work together:

**1. Delta temporality by default.** The OTLP metric exporter defaults to `DELTA` for
counters and histograms (`otel.metrics_temporality`, default `delta`). Each short-lived
process reports only its own delta; the collector accumulates across processes. Override to
`cumulative` via `otel.metrics_temporality: cumulative` in config, or set the standard env
`OTEL_EXPORTER_OTLP_METRICS_TEMPORALITY_PREFERENCE` (env takes precedence over config).

**2. CLI-metrics collector preset** (`cli-metrics-preset.yaml`, applied automatically by
`bh hive init --observaloop`). The preset reshapes only the metrics pipeline of the profile
collector (traces and logs are unchanged):

- `resource/strip_instance` — deletes `service.instance.id` from the Resource so all `bh`
  processes share one resource stream instead of fragmenting into per-process series.
- `transform/promote_ws_attrs` — copies `bh.hive`, `bh.worktree`, `bh.role`, and
  `observaloop.profile` from Resource attributes onto every metric datapoint, making them
  queryable as Prometheus series labels.
- `deltatocumulative` — accumulates the delta pushes into running totals so `rate()` and
  `increase()` return meaningful data.

Metric panels in the bundled dashboard (the `$bh_hive` and `$bh_worktree` template variables
and the per-worktree breakdown's `observaloop_profile` grouping) require the preset and delta
temporality to be applied. Trace panels work without the preset.

> **Use a dedicated per-hive profile for metrics.** The preset reshape only takes on a
> profile-scoped collector that `bh` controls. The shared, compose-managed `default` collector
> (the one a hive exporting to `:4317` lands on) accepts `collector_set_config` but never reloads
> it, so the reshape silently no-ops — `bh` detects the non-persist (re-fetch + compare) and warns
> rather than reporting a false success. For per-hive metrics, stand up a dedicated profile:
> `observaloop.enabled` + `bh hive init --observaloop`.

Verify end-to-end with:

```sh
just metrics-verify
```

The harness (`tests/test_metrics_verify.py`) emits counter samples with delta temporality,
then polls Prometheus to confirm: the series carries `bh_hive` and `observaloop_profile`
labels, has no `service_instance_id`, and that `rate()` returns data.

### Grafana dashboard panels

The bundled `bh-telemetry` dashboard (applied by `bh hive init --observaloop`) includes rows
covering these instruments:

**Bead lifecycle row** (`bh.work.*`):

- **Bead transitions** — `increase(bh.work.bead.transitions[$flow_window])` split by
  `bh.bead.transition`; the count of each lifecycle stage over the flow window.
- **Merge duration p50/p95** — `histogram_quantile` over `bh.work.merge.duration` split by
  `bh.merge.kind`; compares bead vs molecule merge latency at the 50th and 95th percentiles.
- **Validation pass/fail** — `increase(bh.work.validation.runs[$flow_window])` split by
  `bh.validation.result` and `bh.work.phase`; exposes which phase (check, submit, molecule) is
  failing.
- **Agent dispatch spans** — a Tempo/TraceQL panel (`{ name =~ "invoke_agent.*" }`) listing
  dispatcher-to-developer dispatch spans with `gen_ai.agent.name` and status.

**Worktree events row** (`bh.worktree.events`):

- **Worktree events by op + outcome** — `increase(bh.worktree.events[$flow_window])` split by
  `bh.worktree.op` (create/remove/prune) and `bh.worktree.outcome` (ok/error); surfaces
  worktree churn and any provisioning errors.

**Commit Flow row** (the commit-flow / DORA family) — throughput (`increase` of
`bh.work.merge.outcome` by kind), cycle time p50/p95 (+active), the coding/review_wait/merge_latency
stage breakdown, flow efficiency % (`coding / (coding + review_wait + merge_slot_wait)`), review
clearance p90, merge-slot wait/hold p95, rework rounds p90 + total, first-pass yield (pass/total
validation by phase), validation duration p95, the merge-outcome mix by `bh.merge.how`, abandon
rate (`abandoned / (abandoned + merged)`), and worktree-op duration p95 + worktree errors.

**Units convention.** Latency/duration panels (CLI/MCP/merge/cycle/stage/slot/validation/worktree-op
histograms) stay in **seconds**; the CLI/MCP RED panels stay as `rate()`; **counter** panels
(bead transitions, validation runs, worktree events, throughput, merge-outcome mix, worktree
errors) are re-unitted to `increase(...[$flow_window])` with a **short** unit so a discrete count
over the chosen window is read directly.

All panels respect the `$bh_hive` and `$bh_worktree` template variables (both
`label_values(...)`-driven with `allValue: .*`), and the count/throughput panels also respect the
**`$flow_window`** variable — a custom selector defaulting to **1h** with options **5m / 15m / 1h /
1d** that sets the `increase()` window. Scoping to a specific hive/worktree (and window) filters
every panel to that context. This is especially useful for watching `bh` under its own
integration-test fixtures: run the verify harness with `otel.enabled: true` pointing at a local
collector and the bead-lifecycle + worktree-events + Commit Flow panels populate in real time,
scoped to the fixture's hive/worktree identity.

## LGTM stack — `bh otel up`

`bh` ships a bundled `grafana/otel-lgtm` compose file that brings up a complete local
observability stack with a single command.

```sh
bh otel up    # start Grafana + OTel Collector + Loki + Tempo + Mimir
bh otel down  # stop
bh otel logs  # stream container logs
bh otel ps    # show service status
```

| Port | Service |
|---|---|
| 3000 | Grafana UI — <http://localhost:3000> (default credentials: admin / admin) |
| 4317 | OTLP gRPC collector |
| 4318 | OTLP HTTP/protobuf collector |

The compose file is seeded to `~/.ws/docker-compose.otel.yml` on first `bh otel up`. The
container runtime is shared with the Dolt backend setting (`dolt.backend`: `colima` \|
`docker` \| `podman` \| `none`).

After `bh otel up`, point bh at the local stack:

```yaml
otel:
  enabled: true
  endpoint: http://localhost:4317
```

Then run any bh command and open Grafana → Explore → Loki (logs) / Tempo (traces) /
Prometheus (metrics).

## Verification

`tests/test_otel_verify.py` is an opt-in live harness that confirms telemetry actually flows
from bh to a real OTLP collector. Install the required extra first:

```sh
uv sync --extra otel
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
  (`service.name=bh`)
- **Metrics** — `bh.cli.invocations`, `bh.cli.duration`, `bh.mcp.tool.invocations`,
  `bh.mcp.tool.duration`, `bh.errors` (two entries — boundary `cli` and `mcp`)
- **Logs** — `otel_initialized` and `mcp_tool_error` records bridged via LoggingHandler

The harness skips by default — both `WS_OTEL_VERIFY` and `OTEL_EXPORTER_OTLP_ENDPOINT` must
be set — so `just check` (CI default) needs no collector.

## GenAI spans (experimental)

Experimental support for the OpenTelemetry `gen_ai.*` semantic conventions is landing via a
parallel bead (cit.5). When active, AI-model interactions emit spans with standard `gen_ai.*`
attributes (model, input/output tokens, finish reason). These appear in Tempo alongside the
regular bh verb spans.

## Session telemetry parity — Claude vs OpenCode seats

`bh role <seat> --harness opencode` gets the same session-level telemetry attribution as the
`claude` harness — no OpenCode-specific plumbing needed:

- **`BH_ROLE`** — `role.launch()` execs the harness process (`opencode --agent <seat>`, same as
  `claude --agent <seat>`) with `BH_ROLE` set in its environment. Any `bh` call made from a shell
  or MCP client running inside that OpenCode session inherits `BH_ROLE` the same way it would
  under Claude Code — ordinary OS process-env inheritance, not a harness-specific mechanism — so
  `config.otel_role` stamps the right `bh.role` on every span regardless of which harness spawned
  the seat.
- **`.bh/otel.env`** — the per-worktree OTLP endpoint overlay (`observaloop_env.load_worktree_env`,
  invoked by `cli._root` before every `bh` command) is keyed purely on **cwd**
  (`worktree.cwd_worktree_dir`), not on which process or harness launched `bh`. A `bh` command run
  from an OpenCode seat's shell picks up the same hive-scoped endpoint overlay as one run from a
  Claude Code seat in the same worktree.
- **`gen_ai.system`** — `config.otel_genai_system` falls back to `harness_name()` (`BH_HARNESS` env
  → per-hive `harness:` config → global `harness` config → `"claude"`) when `otel.genai.system` /
  `BH_GENAI_SYSTEM` aren't set explicitly. A dispatch span for a bead worked from an OpenCode seat
  is therefore tagged `gen_ai.system=opencode`, not a hardcoded `claude`.

**One accepted non-parity**: `bh statusline` (`role.statusline()`) is Claude-TUI-only. It parses
Claude Code's TUI stdin JSON contract (`agent.name`, `workspace.repo.{owner,name}`) to render
`⬡ <hive> · <role>`. OpenCode has no equivalent stdin status-line contract, so there is no
OpenCode-side status line — this does not affect telemetry attribution, which flows entirely
through `BH_ROLE` + `.bh/otel.env` + `harness_name()` above.

## `bh doctor` observability status

`bh doctor` reports the resolved observability configuration:

```text
# Observability
  log.format: auto
  log.level: info
  otel.enabled: false
  otel libs: unavailable (install: pip install 'beadhive[otel]')
  endpoint: (not set)
```
