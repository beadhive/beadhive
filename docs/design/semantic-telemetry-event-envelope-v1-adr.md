# ADR: Semantic telemetry and event-envelope v1

> Status: **decided** (2026-09-10). Decision bead: `bh-id9pp.1`.

## Context and boundary

Beadhive already exports operator-configured OpenTelemetry signals from legacy adapters, including
the finite daemon shutdown behavior delivered by `bh-q0lol.13`. It also has a separate
[proposal for pseudonymous product usage telemetry](../TELEMETRY.md). Neither surface gave the
operation and lifecycle kernels a transport-neutral definition of what they may emit.

The v1 boundary is `beadhive.kernel.telemetry`. Domain and application consumers construct an
immutable `EventEnvelope` and call the narrow `TelemetrySink` port. OpenTelemetry, queues,
exporters, networks, worker threads, collector configuration, and deployment stay outside the
kernel. The checked schema is
`urn:beadhive:wire-schema:telemetry.event-envelope:1`; an OTLP log, span, metric, JSON line, or test
record is an adapter projection of that semantic value rather than the domain model itself.

This decision does not enable telemetry, authorize collection, deploy a collector, or migrate an
existing instrumentation call. `bh-id9pp.2` owns no-op and recording sinks plus operation and
lifecycle instrumentation. `bh-id9pp.4` owns OpenTelemetry adapters and migration of CLI, daemon,
gateway, and plugin callers. `bh-id9pp.3` owns registration in the complete official wire release.

## Decision summary

| Concern | v1 decision |
| --- | --- |
| Dependency direction | Domain/application → semantic contract; bootstrap injects a sink; adapters → contract. No kernel or application OpenTelemetry SDK import. |
| Names and versions | A kernel-owned `beadhive.<capability>.<observation>` name plus an independent positive `event_version`; envelope shape is `schema_version=1`. |
| Correlation | Every event has `event_id` and `correlation_id`; optional `causation_id`, OTel-compatible `trace_id`, and `span_id` remain distinct. |
| Completion | `started` and `observed` have no completion fields. `completed` always has a bounded duration and one of succeeded, failed, timed-out, cancelled, or no-op. |
| Failure | Failed, timed-out, and cancelled events carry a closed error classification, bounded machine code, and retryability only—never exception text. |
| Identity | Service and changing process/daemon instance are required. Host, hive, bead, run, actor, seat, and plugin attribution are explicit nullable fields. |
| Attributes | At most 16 dimensions from a closed key registry; finite dimensions use closed value sets and registry identities use bounded dotted IDs. |
| Sink behavior | Emit is non-blocking and best-effort. Flush receives one finite total budget. Defensive callers convert every adapter exception to a telemetry result. |
| Compatibility | Canonical schema bytes are generated and drift-checked. Closed-union and structural same-major changes are checked by the repository compatibility policy. |

## Module boundary assessment

The explicit kernel contract was compared with extending either legacy `beadhive.otel` or
daemon-owned `beadhive.daemon_telemetry`. Scores are 0 (poor), 1 (mixed), or 2 (strong).

| Candidate | Cohesion | Coupling | State/effects | Port | Replaceable | Direction | Test closure | Total |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| `kernel/telemetry` semantic contract | 2 | 2 | 2 | 2 | 2 | 2 | 2 | 14 |
| extend `otel.py` | 1 | 0 | 0 | 1 | 0 | 0 | 1 | 3 |
| generalize `daemon_telemetry.py` | 1 | 0 | 1 | 1 | 0 | 0 | 1 | 4 |

The selected capability owns semantic observation shape and policy, not export side effects.
`TelemetrySink` is the named outbound port. Concrete sink/export state is adapter-owned. The
allowed direction is adapters and consumers toward the public kernel contract; the kernel never
imports an adapter or higher application layer. Its focused tests import no CLI, daemon, network,
OpenTelemetry, plugin implementation, or process runtime.

This bead establishes the enforceable contract boundary, not the completed runtime
modularization. The concrete no-op/recording implementation and higher-layer substitution tests
required to finish that migration deliberately remain with `bh-id9pp.2`; the real OTel contract
tests remain with `bh-id9pp.4`. Moving either into this decision bead would duplicate their filed
acceptance rather than close this boundary more cleanly.

## Event names and versioning

V1 registers three semantic families:

- `beadhive.operation.execution` for a canonical operation attempt;
- `beadhive.lifecycle.delivery` for one kernel lifecycle delivery; and
- `beadhive.telemetry.flush` for shutdown flush observation while a provider can still export.

Names describe the semantic fact, not its wire signal, exporter, Python function, CLI spelling,
HTTP path, plugin product, or outcome. Phase and outcome therefore do not create name variants.
For example, one operation attempt uses the same name for `started` and `completed`; the latter's
outcome distinguishes failure, timeout, cancellation, and no-op.

`schema_version` versions the envelope shape and is exactly `1`. `event_version` versions the
meaning of one event name and begins at `1`. Event-name syntax is open for forward compatibility,
so consumers ignore an unknown name while core producers still require a reviewed registry
addition. Changing the meaning or unit of an existing name requires a new event version;
changing a required field, type, closed union, or redaction meaning requires a new envelope/schema
major. Adapters preserve both versions even when their transport has its own version field.

## Envelope semantics

Every serialized envelope contains every top-level field. A fact unavailable at the observation
point is `null`; a producer must not guess or derive it from unrelated state.

### Phase, outcome, error, and duration

- `started` marks entry into work. It has no outcome, duration, or error.
- `observed` is an instantaneous state/measurement fact. It has no completion fields.
- `completed` terminates the attempt and requires `duration_ms` from a monotonic elapsed clock.
  The wall-clock `occurred_at` is the observation timestamp and must not be subtracted to compute
  duration.
- `succeeded` means the requested effect completed.
- `failed` means work terminated unsuccessfully for a classified reason.
- `timed-out` means a declared deadline expired and requires error classification `timeout`.
- `cancelled` means cooperative or forced cancellation and requires classification `cancellation`.
- `no-op` means the operation validly performed no effect, such as an already-satisfied state. It
  is an application outcome, not a synonym for disabled telemetry.

Failure-like outcomes require exactly one `EventError`. Its classification is one of validation,
configuration, authorization, conflict, dependency, timeout, cancellation, internal, or
unavailable. `code` is a stable machine code of at most 128 characters and `retryable` is a
boolean. Exception messages, stack traces, stdout/stderr, response bodies, and dynamically built
text never cross this boundary.

### Correlation and identity

`event_id` identifies one envelope. `correlation_id` groups one logical operation/lifecycle tree.
A nested event inherits correlation and names its direct parent's event ID as `causation_id`; a
root uses null. An event cannot cause itself. `trace_id` and `span_id` are optional lowercase
non-zero W3C/OTel identifiers and a span never appears without its trace. Correlation IDs are not
silently inferred from trace IDs, run IDs, provider continuations, sessions, or bead IDs.

`occurred_at` is UTC with a literal `Z` and no more than six fractional-second digits. The schema
uses portable regular-expression alternatives for years 0001–9999, Gregorian leap years, each
month's valid days, and numeric hour, minute, and second ranges instead of relying on an optional
JSON Schema format checker. The domain model also parses the value with the standard-library
datetime implementation so its calendar validation cannot silently weaken if the schema is
consumed by a validator with different optional format-checking behavior.

Identity is an allowlist:

- `service` and `instance_id` identify the emitting service and current process/daemon instance;
- `host_id`, exact registered `hive_id`, `bead_id`, and outer `run_id` appear only when directly
  known and permitted for that configured operator stream;
- `actor` and closed `seat` identify the Beadflow actor where appropriate; and
- `plugin_id` plus optional strict SemVer `plugin_version` attribute plugin-originated work. A
  version without a plugin ID is invalid.

Plugin attribution never carries a plugin-owned mapping or callback payload. A plugin can select
only kernel-owned event/attribute fields and values validated against the same contract.

## Redaction and bounded cardinality

The envelope is constructed from an allowlist; it is never produced by serializing an input,
exception, config object, plugin object, environment, request, or log record and scrubbing it
afterward. The following are forbidden in every field and diagnostic:

- credentials, tokens, cookies, authorization headers, key material, credentialed URLs, or secret
  values;
- prompts, task or bead prose, instructions, model/tool inputs or outputs, transcripts, argv,
  stdout, or stderr;
- raw environment names or values and configuration fragments;
- arbitrary or absolute filesystem paths, URL paths, query values, client addresses, headers, or
  user agents;
- exception messages, stack traces, unbounded error text, or response bodies; and
- unclassified plugin payloads, metadata maps, or extension attributes.

The attribute registry is closed. V1 allows: `operation.kind`, `operation.name`,
`lifecycle.event`, `surface`, `transport`, `http.method`, `http.status-class`, `dependency`,
`reason.code`, `retry.count`, `sampling.decision`, and `shutdown.phase`. Finite dimensions have
closed values in code and schema. `operation.name` must come from the canonical operation catalog;
`lifecycle.event` from the lifecycle registry; reason codes from the owning checked registry.
Their dotted grammar and length limit are defense in depth, not permission to insert dynamic
customer, path, exception, plugin, or request values.

Identity fields are suitable for event/span/log correlation but are not metric dimensions.
Adapters must not place host, instance, hive, bead, run, actor, plugin instance, event,
correlation, causation, trace, or span IDs on metrics. Metric adapters choose only finite attribute
dimensions and collapse unregistered raw values to an explicitly registered `other` value where
the registry provides one; otherwise they drop the dimension or event.

## Sink, sampling, no-op, and shutdown

`TelemetrySink.emit` returns one closed disposition: accepted, sampled-out, disabled,
capacity-dropped, or failed. Sampling occurs at the sink boundary under a configured bounded rule;
it never changes the application result and never uses secret or arbitrary payload content as a
sampling key. An emitted event may say `sampling.decision=recorded`. No synthetic envelope is
created merely to report that the original was sampled out or disabled.

The disabled/no-op sink planned by `bh-id9pp.2` returns `disabled` without allocating exporters,
starting workers, writing files, or contacting a service. `Outcome.NO_OP` remains a completed
application fact and may still be accepted by an enabled sink. Queue exhaustion returns
`capacity-dropped`; it cannot block or backpressure application work.

`emit` must be non-blocking and must not raise. Core callers nevertheless use `emit_non_fatal`,
which turns a nonconforming adapter exception or invalid disposition into `failed`. The wrapper
does not log the exception text or recursively emit another event through the failing sink.

`TelemetrySink.flush(timeout_seconds)` receives one finite positive total budget and returns
completed, timed-out, failed, or no-op plus bounded elapsed milliseconds. It is called once as the
final host shutdown phase, after application/session/resource draining, with no more than the
parent shutdown budget remaining. The adapter owns enforcement of the deadline across all of its
providers/exporters; it must not multiply the budget per exporter, abandon an unbounded helper
thread, retry after expiry, or emit after provider close. `flush_non_fatal` contains exceptions,
but an adapter contract test must prove wall-clock boundedness because a wrapper cannot recover
time already lost inside a nonconforming synchronous implementation.

Flush failure or timeout is observable only as the flush result/diagnostic captured while the
provider can still export. It never changes an application outcome, exit code, lifecycle result,
or the completion of shutdown. Export durability belongs in the collector or an explicitly
designed bounded adapter queue, never in domain state.

## Deterministic schema and compatibility

The source is `beadhive.kernel.telemetry.schema.event_envelope_schema`. It uses standard-library
data only and has no import-time side effect. `scripts/render_telemetry_schema.py` writes canonical
UTF-8 JSON with sorted keys, two-space indentation, and one trailing newline to
`docs/schemas/telemetry-event-envelope-v1.schema.json`. Its `--check` mode is part of
`just wire-schema-compat`, so checked bytes cannot drift from the model.

Focused contract tests validate representative start/completion outcomes, reject every forbidden
attribute family, and run the repository's same-major compatibility comparator against a closed
outcome-union mutation. Once `bh-id9pp.3` registers the artifact in the official release manifest,
the existing release comparison also prevents in-place published changes and compares candidate
releases with the prior major-compatible artifact. That registration and complete release
inventory stay out of this bead.

## Reconciliation with existing telemetry work

### `docs/TELEMETRY.md`

That document remains a proposal for a distinct, separately consented pseudonymous product-usage
pipeline. It is not the semantic kernel contract and its proposed default-on state is not enacted
here. Product usage forbids repository, hive, bead, run, actor, plugin, and account identities even
though some are permitted in an operator-configured semantic envelope. A future product adapter
must construct its narrower event directly from approved dimensions; it must not serialize or
forward an `EventEnvelope` wholesale. The product proposal's fixed endpoint, installation JWS,
SQLite queue, dependency promotion, collector, and ClickHouse deployment are outside this
molecule unless separately ratified.

### `bh-q0lol.13`

The closed daemon telemetry bead remains authoritative for today's concrete adapter behavior:
one provider per daemon lifespan, stable host and changing instance identity, finite CLI/stdio and
daemon flush budgets, route-template RED signals, exact active gauges, bounded labels, and flush
observation before provider close. This ADR does not rename its metrics or weaken its wall-clock
tests. `bh-id9pp.4` maps those existing facts to the semantic port, preserves dashboard names or
publishes a versioned migration, and keeps collector/deployment lifecycle outside core.

## Rejected designs

### Put the port in `otel.py`

That makes OpenTelemetry terminology and SDK lifecycle the inward dependency and prevents a
small recording fake from testing domain/application consumers. OTel is one adapter.

### Accept arbitrary attribute mappings and redact later

Key blacklists miss aliases and nested plugin/config payloads; value scrubbing cannot bound
cardinality or recover classification. V1 accepts only typed fields and registered dimensions.

### Make a sink exception or flush failure part of product correctness

Observability must describe control flow rather than control it. A telemetry failure cannot turn
a successful operation or orderly shutdown into failure.

### Deploy or embed a collector

Collection infrastructure is an integration concern. Core publishes an outbound port and stable
contract only.
