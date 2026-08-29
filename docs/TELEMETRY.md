# Proposed architecture: pseudonymous product usage telemetry

This design adds a default-on, separately consented product-usage stream for the `bh` CLI,
daemon, and MCP server. It uses OTLP/HTTP for its standard envelope and transport, while
remaining completely isolated from bh's existing operator-configured OpenTelemetry
observability.

## Goals

The system should answer:

- How many distinct bh installations are active?
- Which CLI commands, MCP tools, and daemon operations are used?
- Which bh versions, operating systems, and architectures remain active?
- What are the coarse success, error, and latency distributions?
- How does usage vary over time for one pseudonymous installation?

It must never collect or associate:

- Accounts, email addresses, usernames, authentication identities, or license identities.
- Repository, organization, hive, branch, worktree, bead, epic, or provider identifiers.
- Paths, URLs, command values, positional arguments, prompts, MCP inputs or results.
- Logs, stack traces, exception messages, stdout, or stderr.
- Hostnames, hardware identifiers, MAC addresses, or raw platform machine IDs.
- IP addresses or User-Agent strings in retained telemetry.

## Architectural boundary

```text
Operator observability
─────────────────────────────────────────────────────────────────
bh process
  └── existing operator OTel provider
        └── operator-configured endpoint
              Rich logs, traces, metrics, repository context
              Explicit opt-in; controlled by the operator


Product usage telemetry
─────────────────────────────────────────────────────────────────
bh CLI ─────┐
bh MCP ─────┼── consent → allowlisted event builder
bh daemon ──┘                    │
                                 ▼
                    local SQLite queue/accumulator
                                 │
                      detached OTLP sender
                                 │ OTLP/HTTP protobuf + TLS
                                 ▼
             telemetry-prod.beadhive.cloud/v1/logs
                                 │
                       nginx ingress proxy
                                 │
                 OpenTelemetry Collector Contrib
                ┌────────────────┴────────────────┐
                ▼                                 ▼
      sanitized usage events          low-cardinality service
            in ClickHouse              health/volume metrics
                │
        materialized aggregates
                │
        internal aggregate reports
```

Product telemetry will have its own:

- Private OTel provider instantiated only by the detached sender.
- Fixed first-party endpoint.
- Instrumentation scope and service name.
- Event schema and allowlist.
- Local queue.
- Collector pipeline.
- Database and retention policy.
- Consent and deletion behavior.

Nothing from the existing operator OTel Resource, spans, logs, or metrics will be forwarded into
product telemetry.

## Why OTLP structured log events

OTLP provides:

- A standard protobuf envelope.
- Typed timestamps, event names, resources, scopes, and attributes.
- HTTP transport over port 443.
- TLS, compression, standard status handling, and exporter implementations.
- Compatibility with the self-hosted OpenTelemetry Collector.
- Server-side processors for filtering, transformation, batching, and routing.

It does not remove the need for a bh-specific event schema. It standardizes the transport and
outer model, while bh defines the narrow vocabulary inside it. See the
[OTLP specification](https://opentelemetry.io/docs/specs/otlp/).

The product stream will use the OTLP logs signal as structured events:

- `event_name` identifies the event.
- `body` is absent or a fixed constant.
- Resource attributes contain process-wide facts.
- Log attributes contain strictly allowlisted event dimensions.
- No arbitrary application log text is accepted.

Prometheus and StatsD are not suitable as client ingestion protocols:

- Prometheus is primarily pull-based, and a unique installation ID would create
  high-cardinality series.
- Pushgateway has lifecycle and stale-series problems for distributed user installations.
- StatsD assumes a nearby UDP collector and provides no durable delivery or structured event
  contract.
- Prometheus remains useful for low-cardinality operational metrics derived after ingestion.

## Installation identity

Generate a random UUIDv4 on the first telemetry-enabled invocation:

```python
installation_id = str(uuid.uuid4())
```

Store it at:

```text
${BH_HOME}/telemetry/installation-id
```

Security and lifecycle:

- Parent directory mode: `0700`.
- File mode: `0600`.
- Create atomically with exclusive-create semantics.
- If concurrent processes race, one ID wins and all others read it.
- Never derive it from any machine or account property.
- Never copy it into normal bh configuration, logs, diagnostics, auth, or API requests.
- `bh telemetry disable` deletes the identifier and all queued data.
- `bh telemetry reset-id` deletes it and immediately creates a new one if telemetry remains
  enabled.
- Clearing `BH_HOME` or reinstalling without preserving state naturally creates a new identity.

This is an installation/profile identifier, not proof of a unique physical host. Different OS
accounts or `BH_HOME` values on one computer may produce separate IDs; several bh installations
sharing one `BH_HOME` share the ID.

A random per-process UUID becomes `service.instance.id`. This groups events within one CLI, MCP,
or daemon process but is not retained across restarts.

The telemetry identifier must be isolated by design:

- No account-linking endpoint.
- No account or authentication database accessible from the telemetry database role.
- No authentication headers or cookies sent to telemetry.
- A repository-wide test prevents `installation_id` from appearing in non-telemetry HTTP
  requests.
- Telemetry is never used for billing, licensing, authentication, or enforcement.

## Consent and controls

Precedence:

1. Truthy `DO_NOT_TRACK`: hard-off.
2. Truthy `BH_TELEMETRY_DISABLED`: hard-off.
3. Process launch `--no-telemetry`: off for that process.
4. Host-local `usage_telemetry.enabled`.
5. Default: enabled.

False or empty `DO_NOT_TRACK` values fall through rather than enabling telemetry over a stored
opt-out.

Commands:

```sh
bh telemetry status
bh telemetry sample
bh telemetry enable
bh telemetry disable
bh telemetry reset-id
```

Launch controls:

```sh
bh daemon serve --no-telemetry
bh mcp serve --no-telemetry
bh-mcp --no-telemetry
```

Configuration:

```yaml
usage_telemetry:
  enabled: false
```

The configuration section is host-local. Fleet, HQ, hive, repository, or remote configuration
must not be able to enable telemetry or redirect its endpoint.

`bh telemetry status` reports:

- Effective state.
- Which setting determined the state.
- Installation ID, if one exists.
- Queue record count and logical size.
- Last successful upload time.
- Fixed destination hostname.
- Link to `docs/TELEMETRY.md`.

`bh telemetry sample` displays the exact representative OTLP event fields without queuing or
transmitting anything.

A one-time interactive notice is shown only after it is successfully written to a TTY. It is
suppressed for JSON output, completion, hooks, daemon startup, and MCP protocol output.

## Event envelope

Common OTLP Resource:

```yaml
service.name: beadhive-usage
service.version: 0.15.1
service.instance.id: <random process UUID>
beadhive.installation.id: <persistent random UUID>
beadhive.surface: cli | mcp_stdio | mcp_http | daemon
os.type: linux | darwin | windows
host.arch: amd64 | arm64 | other
process.runtime.name: cpython
process.runtime.version: "3.12"
ci: true | false
container: true | false
```

Only Python major/minor is collected, not the full patch/build string.

Common event attributes:

```yaml
beadhive.schema.version: 1
beadhive.event.id: <random event UUID>
beadhive.event.count: 1
```

The OTLP timestamp supplies event time. Aggregated events additionally contain a five-minute
window boundary rather than individual call timestamps.

### Event: CLI command completed

```yaml
event_name: beadhive.cli.command.completed
body: null

attributes:
  beadhive.command: work.claim
  beadhive.outcome: ok
  beadhive.duration_bucket: 1s-10s
  beadhive.event.count: 1
```

The command name comes from the fully resolved Typer/Click command tree, never by parsing raw
`argv`.

Outcomes:

```text
ok
usage_error
config_error
dependency_error
internal_error
interrupted
```

Duration buckets:

```text
<100ms
100ms-1s
1s-10s
10s-60s
1m-10m
10m+
```

No flags are collected in v1. A future schema may add explicitly approved flag names, but never
their values. Positional, variadic, passthrough, plugin, and extension arguments remain permanently
excluded.

Informational invocations such as help, version, completion, and telemetry-management commands do
not produce usage events.

### Event: MCP operation aggregate

```yaml
event_name: beadhive.mcp.operation
body: null

attributes:
  beadhive.operation.kind: tool
  beadhive.operation.name: work_show
  beadhive.transport: stdio
  beadhive.outcome: ok
  beadhive.duration_bucket: 100ms-1s
  beadhive.window: 5m
  beadhive.event.count: 37
```

Allowed operation kinds:

```text
tool
resource
```

Only registered tool names and resource-template names are recorded. MCP argument keys, values,
resolved URIs, content, prompts, and results are excluded.

High-volume MCP events are aggregated locally over five-minute windows by:

```text
operation kind
operation name
transport
outcome
duration bucket
bh version
```

### Event: daemon operation aggregate

```yaml
event_name: beadhive.daemon.operation
body: null

attributes:
  beadhive.operation.name: operator_status
  beadhive.outcome: ok
  beadhive.status_class: 2xx
  beadhive.duration_bucket: 100ms-1s
  beadhive.window: 5m
  beadhive.event.count: 12
```

Only registered route or internal operation names are allowed. Actual URL paths, path variables,
queries, ports, bind addresses, client IPs, request headers, and User-Agent are excluded.

Health and readiness probes do not generate usage events.

### Event: long-lived process lifecycle

```yaml
event_name: beadhive.process.lifecycle
body: null

attributes:
  beadhive.process.kind: daemon
  beadhive.lifecycle.action: started
```

Kinds:

```text
daemon
mcp_stdio
mcp_http
```

Actions:

```text
started
stopped
active
```

- `started` occurs once after readiness.
- `stopped` is best-effort and includes a shutdown reason and uptime bucket.
- `active` occurs no more than once per 24-hour period for a running but otherwise idle service.

The daily active event allows active-install estimation without treating an indefinitely running
daemon's original start date as ongoing activity.

Long-running server entrypoints do not also emit a generic CLI command event, avoiding double
counting.

## Local queue and accumulator

Use Python's standard-library SQLite under:

```text
${BH_HOME}/telemetry/queue.sqlite3
```

SQLite is preferable to JSONL because the CLI, MCP server, and daemon may write concurrently. It
provides atomic transactions, bounded claiming, retry leases, and simple purging without another
dependency.

Logical limits:

- Maximum 1,000 pending aggregate records.
- Maximum 1 MiB of serialized event payloads.
- Maximum age seven days.
- Oldest records dropped first.
- Five-minute aggregation windows.
- Maximum outgoing batch: 100 records or 64 KiB.
- Network timeout: two seconds.
- Exponential retry with jitter.
- Retry `429` and `5xx`; discard permanent schema-related `4xx`.
- Telemetry writes use a very short SQLite busy timeout and fail silently rather than delaying
  commands.

CLI commands insert one completed event.

MCP and daemon processes maintain an in-memory aggregation map and periodically upsert completed
windows. Losing the current partial window during a crash is acceptable; telemetry must never
become part of product correctness.

A hidden detached sender:

```text
bh telemetry _flush
```

claims due rows under a lease, constructs OTLP log records, and performs one batch export. Payloads
and credentials are never placed in subprocess arguments.

Successful rows are deleted. Failed transient batches have their lease released with a future
retry time. Expired or excess rows are pruned before every insert.

## Self-hosted collection stack

### DNS and environments

```text
telemetry-prod.beadhive.cloud
telemetry-staging.beadhive.cloud
```

Official releases contain a fixed environment and endpoint. Repository or user configuration
cannot redirect telemetry.

Development builds default to disabled and may target staging only through an explicit developer
build setting.

### Ingress proxy

Self-host nginx in front of the Collector:

- TLS termination.
- Only `POST /v1/logs`.
- Maximum request body 64 KiB.
- Request and connection rate limits.
- No access logs.
- No request-body, header, query-string, or IP retention.
- Fixed upstream to the Collector.
- Generic success/error responses.

There is no embedded client API secret. A secret distributed in an open-source CLI would not
authenticate genuine installations. The endpoint must treat all data as untrusted and rate-limit
accordingly.

### OpenTelemetry Collector Contrib

Dedicated usage pipeline:

```text
otlphttp receiver
→ memory limiter
→ event/schema filter
→ attribute allowlist transform
→ batch processor
→ ClickHouse exporter
```

The filter permits only:

- Known `beadhive.*` event names.
- Supported schema versions.
- Expected signal type and service name.
- Required attributes with valid bounded values.

The transform processor removes every resource and record attribute not on the explicit allowlist.
This is defense-in-depth against SDK-added metadata and malformed or malicious payloads. See the
[Transform processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/transformprocessor)
and
[Filter processor](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/processor/filterprocessor).

The Collector batch processor provides transport batching, not statistical aggregation. See the
[Batch processor](https://github.com/open-telemetry/opentelemetry-collector/tree/main/processor/batchprocessor).

The Signal-to-Metrics connector may produce low-cardinality operational counters and histograms,
but will not be the canonical analytics path because its logs-to-metrics support is currently alpha
and does not perform stateful time-window aggregation. See the
[Signal-to-Metrics connector](https://github.com/open-telemetry/opentelemetry-collector-contrib/tree/main/connector/signaltometricsconnector).

Installation IDs must never become Prometheus labels.

### ClickHouse

Self-host ClickHouse as the canonical store.

Proposed tables:

- `usage_window_events`
  - Locally aggregated CLI/MCP/daemon event records.
  - Contains pseudonymous installation ID.
  - TTL: 90 days.
- `usage_daily_installation`
  - One row per installation/day/dimension set.
  - Supports active-install and longitudinal usage analysis.
  - TTL: 13 months.
- `usage_daily_aggregate`
  - Global counts, histograms, and mergeable approximate-distinct states.
  - Does not retain individual installation IDs.
  - Long-term retention.

Materialized views generate the daily tables from accepted window events.

Backups are encrypted and follow the same deletion lifecycle. Database credentials and network
access are separate from account, product, and control-plane storage.

No commercial analytics product, hosted event service, advertising platform, or third-party
product analytics SDK receives the data.

If the infrastructure itself runs at a cloud provider, public wording must still identify that
provider as an infrastructure processor. The accurate claim is that Beadhive does not sell or
disclose telemetry to analytics providers and stores it only in Beadhive-controlled infrastructure.

## Implementation roadmap

### Phase 1: governance and schema

- Add `docs/TELEMETRY.md`.
- Add versioned event-schema definitions.
- Add public sample payloads and collection exclusions.
- Add host-local `usage_telemetry.enabled`.
- Add consent precedence tests.
- Establish schema-change review requirements.

### Phase 2: identity and local persistence

- Implement atomic random installation ID creation.
- Implement enable, disable, status, sample, and reset commands.
- Implement SQLite spool, leases, limits, pruning, and retry metadata.
- Verify opt-out creates no identifier, queue, subprocess, or network traffic.
- Verify disable removes the identifier and queued records.

### Phase 3: staging collection

- Deploy `telemetry-staging.beadhive.cloud`.
- Deploy nginx, OTel Collector Contrib, and ClickHouse.
- Implement Collector allowlist/filter pipeline.
- Establish TTLs, backups, receiver monitoring, and aggregate views.
- Run only development/debug clients against staging.

### Phase 4: CLI instrumentation

- Instrument the resolved command execution boundary centrally.
- Record command path, outcome, duration bucket, and common environment.
- Exclude help/version/completion/telemetry/server entrypoints.
- Add tests covering every registered CLI leaf.

### Phase 5: MCP and daemon instrumentation

- Reuse existing centralized MCP measurement wrappers.
- Add five-minute local aggregation.
- Add daemon operation and lifecycle events.
- Add daily active events.
- Verify MCP stdout protocol remains untouched.

### Phase 6: production rollout

- Deploy `telemetry-prod.beadhive.cloud`.
- Publish disclosure before default-on release.
- Canary with internal/release-candidate builds.
- Enable default-on behavior in the public release.
- Monitor queue behavior, rejection rates, and command latency.
- Publish aggregate usage reporting where appropriate.

## Release gates

Default-on collection cannot ship until tests prove:

- `DO_NOT_TRACK` disables every binary and background sender.
- Host-local disable cannot be overridden by fleet or repository configuration.
- Opt-out deletes the local queue and installation ID.
- Command arguments, paths, errors, MCP inputs/results, and account identifiers cannot enter
  serialized events.
- Unknown attributes are stripped at the Collector.
- Network failure cannot change command results or materially delay execution.
- Queue size and age remain bounded under extended outage.
- Concurrent CLI, MCP, and daemon writers cannot corrupt or duplicate claimed batches.
- No product or authentication request includes the telemetry installation ID.
- Receiver, proxy, and storage configurations do not retain network metadata.
- Documentation and serialized schema remain synchronized.

## Approved design decisions

1. Stable random, telemetry-only installation UUID.
2. Default-on pseudonymous collection with hard opt-outs.
3. Structured OTLP log events over HTTPS.
4. Separate product and operator OTel providers and pipelines.
5. SQLite client buffering and five-minute MCP/daemon aggregation.
6. Self-hosted nginx → OTel Collector Contrib → ClickHouse.
7. Ninety-day window-event and thirteen-month daily-installation retention.
8. No commercial analytics processor or account association.
