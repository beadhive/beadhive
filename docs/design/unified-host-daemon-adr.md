# Unified Beadhive host daemon contract

- **Status:** Accepted for implementation
- **Date:** 2026-08-24
- **Decision bead:** `bh-u562.5`
- **Spike epic:** `bh-u562`

## Decision

Beadhive will add one supervised, long-lived host daemon per designated operating-system account
and `BH_HOME`. The daemon owns the network listeners for MCP over HTTP, operator REST and SSE,
terminal WebSocket composition, and the non-MCP OpenAPI document. It does not replace direct
CLI execution, MCP over stdio, the HQ store, or the configured Dolt server mode.

This is a **GO** for a unified host daemon because the listener, session, supervision, and
terminal lifecycles require a common process boundary. Telemetry alone is explicitly not a
reason to add the daemon. The implementation must preserve the direct CLI and stdio paths and
must not make ordinary local work depend on daemon availability.

This ADR is the ratified v1 boundary. The implementation molecule must implement this boundary
without importing proof-only code as product code.

## Evidence consumed

The decision consumes all four spike verdicts:

| Verdict | Evidence used here |
| --- | --- |
| [`bh-u562.1`](../spikes/bh-u562.1-dolt-server-lifecycle.md) | `bd` already owns recovery for owned Dolt; shared and external targets fail fast; embedded Dolt is substantially slower under concurrent load. The daemon must not silently change the configured Dolt mode. |
| [`bh-u562.2`](../spikes/bh-u562.2-dolt-server-supervision.md) | OS-native supervision is the right recovery boundary for a per-account and `BH_HOME` daemon; direct CLI, stdio MCP, and HQ access must survive daemon loss. Real logout, sleep, reboot, and container lifecycle evidence remains a release gate. |
| [`bh-u562.3`](../spikes/bh-u562.3-unified-host-transport.md) | FastMCP HTTP preserves the current ten-tool schema and result envelope; sessionful and stateless modes work; MCP, REST, SSE, WebSocket, and OpenAPI routes can share one application and lifespan. Retention, real source wiring, and terminal behavior were not proven. |
| [`bh-u562.4`](../spikes/bh-u562.4-daemon-telemetry.md) | Healthy short-lived CLI export was lossless in the harness, while a dead receiver lost all duration-complete spans and made shutdown take about fifteen seconds. Telemetry therefore requires bounded failure behavior and explicit identity, but does not by itself justify a daemon. |

Proof harnesses, measurements, and generated schemas are evidence, not implementation artifacts.

## Process and ownership boundary

The v1 singleton key is `(designated OS account, canonical BH_HOME, host_id)`. “One daemon per
host” always means one daemon for that key, not one privileged machine-wide process. A second
process for the same key must fail before binding a listener.

The daemon owns:

- one Uvicorn/Starlette application and outer lifespan;
- MCP-over-HTTP sessions;
- operator HTTP requests and per-client SSE queues;
- terminal WebSocket connection composition and descendant cleanup;
- daemon-owned dependency probes and bounded connection pools;
- daemon-incarnation telemetry and shutdown coordination.

The daemon does not own:

- direct `bh` or `bd` CLI invocations;
- MCP over stdio;
- the HQ or hive repositories;
- the configured Dolt process when `bd` already owns it;
- lease renewal or mutation authority merely because a session is healthy;
- browser UI source-of-truth state.

Every state-changing request must resolve the full hive identity and outer run identity again,
then pass through the existing guarded mutation path. It must re-read the relevant lease and
epoch at the mutation boundary. Cached health, an MCP session, a terminal connection, or an
authenticated token never renews a lease and never grants mutation authority.

The process takes a host-local singleton lock before listener startup and writes a control
record containing, at minimum, the canonical `BH_HOME`, stable `bh.host.id`, current
`service.instance.id`, PID, listener address, start time, and contract version. Status and
factory responses must authenticate and verify the expected host identity; an open port alone
is not proof of daemon identity.

## One application and one lifespan

FastMCP's HTTP application is mounted into the host Starlette application. Its lifespan is
composed into the single outer application lifespan; no route creates a second daemon, event
loop, or independent shutdown sequence.

Startup order is:

1. acquire and validate the singleton lock;
2. initialize stable host identity and changing incarnation identity;
3. initialize telemetry once;
4. validate listener and security configuration;
5. initialize bounded daemon-owned resources and dependency probes;
6. start accepting traffic.

Shutdown order is:

1. stop accepting new work and mark readiness false;
2. reject new MCP sessions, SSE subscriptions, activity publishes, and terminal attaches;
3. cancel or drain in-flight work within route-specific limits;
4. close MCP sessions and SSE clients;
5. cancel terminal process groups and verify descendant cleanup;
6. close daemon-owned pools;
7. flush telemetry within the remaining configured budget and record the outcome;
8. remove the control record and release the singleton lock.

The total graceful shutdown budget is configurable and finite. Container stop grace and OS
supervisor timeouts must be at least that budget. A telemetry receiver outage may not extend it.

## Stable v1 routes

The versioned product route table is:

| Method | Route | Scope | Contract |
| --- | --- | --- | --- |
| `GET`, `POST`, `DELETE` | `/mcp` | `mcp:control` | FastMCP streamable HTTP transport |
| `GET` | `/api/v1/factory` | `operator:read` | Host/factory identity and daemon status |
| `GET` | `/api/v1/hives/{hive_id}/snapshot` | `operator:read` | Authoritative per-hive snapshot and event cursor |
| `GET` | `/api/v1/hives/{hive_id}/events` | `operator:read` | Authenticated SSE stream and bounded replay |
| `GET` | `/api/v1/runs/{run_id}/activity` | `operator:read` | Authoritative activity view for one exact outer run |
| `POST` | `/api/v1/runs/{run_id}/activity` | `activity:publish` | Durable, idempotent activity append |
| `POST` | `/api/v1/terminal/attach-token` | `terminal:attach` | Mint a short-lived, single-use WebSocket attach credential |
| WebSocket | `/ws/terminal` | `terminal:attach` | `bh-terminal.v1` terminal composition |
| `GET` | `/openapi.json` | `operator:read` | Non-MCP OpenAPI 3.1 contract |
| `GET` | `/health` | public | Minimal liveness only |

The attach-token route is the one production addition to the proof route table. It resolves a
browser constraint: browser WebSocket APIs cannot set an `Authorization` header. The route
must authenticate the normal bearer token, bind the resulting credential to the host, target,
audience, and requesting principal, and return a short-TTL, single-use value. The browser sends
that value in a WebSocket subprotocol value, never in a URL. Non-browser clients may use the
same flow. Long-lived bearer tokens in query strings are forbidden.

The UI adapter may configure `/api/v1` as its base URL and append the relative snapshot and
event paths. The server contract remains the full routes above.

`/health` discloses only liveness, readiness, and an opaque contract/version indicator. It does
not disclose hives, paths, tokens, run IDs, dependency addresses, or operator data. Dependency
health belongs in authenticated factory/status output and telemetry.

## Canonical identities

`hive_id` is the full canonical, encoded provider/organization/repository identity. The router
decodes it once, rejects unsafe or ambiguous encodings, and resolves it through the exact
registry. Prefix matching and UI-only short IDs are not accepted by the daemon.

The outer `run_id` in an activity route is exact. An activity payload may not override it. The
implementation must keep these concepts distinct:

- canonical hive identity;
- outer run identity;
- MCP session identity;
- SSE subscription identity;
- SSE producer epoch and sequence;
- source revision or store cursor;
- stable host identity;
- changing daemon incarnation identity.

Logs and spans may correlate these values, but no one value is a substitute for another.

## MCP compatibility and migration

MCP over stdio remains the default compatibility path and keeps the current tool names,
schemas, result envelopes, error semantics, and process behavior. MCP over HTTP is additive and
opt-in during migration.

The HTTP default is sessionful streamable HTTP because subscriptions and server notifications
require continuity. A configured stateless mode is permitted for clients that do not require
those features. Both modes expose the same tool schema and result envelope as stdio. Transport
selection must not change mutation authorization, hive resolution, or redaction.

Migration proceeds in three stages:

1. ship HTTP behind explicit listener configuration while stdio remains unchanged;
2. run schema, result, authorization, restart, and concurrent-mutation conformance against both
   transports;
3. advertise HTTP to compatible clients while retaining stdio as the daemon-independent
   fallback.

Daemon restart invalidates HTTP sessions. Clients reconnect and establish a new session; the
server never claims session resurrection. No ordinary MCP request auto-starts the daemon and
no failed HTTP call silently falls back to an embedded server.

## Snapshot, SSE, replay, and reset

Each subscription is scoped to one exact hive and one logical `subscriptionId`. A producer
epoch names one incarnation of the ordering state. Sequence numbers are positive and strictly
increase within `(hive_id, subscriptionId, producerEpoch)`. Every event carries:

- `subscriptionId`;
- `producerEpoch`;
- `sequence`;
- `baseSequence`, equal to the immediately preceding sequence;
- a typed payload and source metadata appropriate to that type.

The SSE `id` is exactly `<producerEpoch>:<sequence>`, and its values must match the payload.
Heartbeats are real sequenced events, not out-of-band comments, so they detect gaps and advance
the cursor.

A snapshot is assembled from the authoritative source and returns an `eventCursor` for the same
logical feed. Publication and snapshotting must use a boundary that prevents a state change
from falling between the returned state and cursor.

Clients reconnect with either `Last-Event-ID` or the byte-identical `after` query value.
Supplying conflicting values is `400`. If the cursor is in the current retained epoch and
range, the server replays every event strictly after it in order and then continues live. An
unknown epoch, expired cursor, future sequence, or retention gap returns `409` with an explicit
resnapshot instruction; it never returns a silent empty success.

Replay storage is bounded per hive and by total memory. Each client queue is bounded
independently. A slow client that exceeds its queue is disconnected with an observable reason
and must resnapshot. Backpressure may not make an unbounded buffer or block source ingestion
for all clients.

When source continuity cannot be proven, the relay creates a fresh `producerEpoch`, emits a
sequenced `reset` as the first event in that epoch, and requires an authoritative resnapshot
before later deltas are applied. Deltas from the old epoch are never applied after reset. Reset
scope is the affected hive and subscription, not the whole factory.

Source revisions and Dolt/HQ cursors remain source metadata; they do not become SSE sequence
numbers. The implementation must test concurrent snapshot/publication, replay edges, heartbeat
gaps, reset, retention expiry, and slow consumers against real sources rather than only an
in-memory proof.

## Activity ingestion

The activity publisher supplies a stable idempotency key and uses bounded retry with an
explicit expiry/drop policy. The daemon validates the exact outer run, scope, size, and schema,
then durably appends or identifies a duplicate before acknowledging success. An in-memory
enqueue is not an acknowledgement.

The event may be published to live consumers only after durable append/deduplication.
Redelivery with the same key is harmless and produces one logical activity record. Daemon loss
makes publication visibly unavailable; the publisher retains bounded retry rather than
pretending delivery succeeded.

## Authentication and network exposure

All routes except `/health` require authentication before dispatch. V1 uses bearer credentials
with explicit audience, expiry, principal, and scopes. Token comparison is constant-time;
tokens and attach credentials are redacted from logs, errors, spans, URLs, and control records.

The scopes are intentionally separate:

- `mcp:control` for MCP tool traffic;
- `operator:read` for factory, snapshot, event, activity-read, and OpenAPI traffic;
- `activity:publish` for activity writes;
- `terminal:attach` for attach-token minting and terminal connection.

A token may hold more than one scope, but one scope does not imply another. Rotation and
revocation must affect new requests immediately. Long-lived MCP, SSE, and terminal sessions
must be revalidated or closed within a documented bounded interval after expiry or revocation.

The listener binds to loopback by default, and loopback traffic still requires bearer
authentication. A non-loopback bind is refused unless the daemon has direct TLS configured or
an explicitly trusted TLS-terminating proxy is configured. In proxy mode, forwarded headers
are honored only from configured proxy addresses.

CORS uses an exact configured origin allowlist, never wildcard with credentials. HTTP `Host`
and WebSocket `Origin` are validated. TLS verification, secure cipher defaults, request/body
limits, connection limits, token rate limits, and session limits are release requirements for
remote exposure.

## Terminal composition

The daemon owns `/ws/terminal`; a second PTY listener is not allowed. The WebSocket negotiates
`bh-terminal.v1`, validates a single-use attach credential, resolves an allowed target, and
provides explicit start, output, input, resize, exit, error, and cancellation messages.

The production backend must consume the verdict of `bh-lx6e.3`. Until that verdict and its
safety evidence are accepted, terminal availability remains disabled or reports an explicit
unavailable response; this ADR does not infer a safe PTY backend from the route-composition
proof.

The implementation must provide bounded input/output queues, slow-consumer behavior, maximum
sessions, idle and absolute timeouts, resize validation, audit events, and process-group
cancellation. Disconnect and daemon shutdown must cancel the whole descendant tree and verify
cleanup. Arbitrary shell or command selection is not granted merely by `terminal:attach`;
targets come from the allowlisted contract ratified with the PTY spike.

## Dolt and HQ boundaries

The daemon uses the configured Dolt target mode; it never changes owned, shared, external, or
embedded semantics to improve its own availability.

- For owned mode, `bd` remains responsible for lazy process recovery.
- For shared mode, target loss is a visible hard failure until the configured server returns.
- For genuine external mode, the daemon must never run a command that can create an empty
  shadow server on the vacated address.
- Embedded mode remains supported where configured, despite its measured concurrency cost.

The daemon may keep a bounded client pool only if its library and target support a real pool.
It must not publish invented pool metrics for per-request clients.

HQ remains a passive/direct store. The daemon may expose authenticated views derived from HQ,
but it does not exclusively own HQ and it does not prevent direct CLI or stdio access. Existing
lease, epoch, host-fence, and exact-hive guards remain authoritative. Known HQ restore and
remote host-fence gaps are owned by their existing beads, not hidden in this implementation.

## Supervision and install contract

The supported supervisors are:

| Environment | Default | Required behavior |
| --- | --- | --- |
| macOS | per-user LaunchAgent | one instance for the designated account and `BH_HOME`, restart after crash, bounded stop, logs and control record visible to status/Doctor |
| Linux | `systemd --user` with persistent user manager/linger checked | restart after crash, start without an interactive shell when configured, bounded stop, explicit guidance when linger is unavailable |
| Container | Compose/orchestrator with the daemon as the main workload under an init/reaper | restart policy, `/health` healthcheck, stop grace at least the shutdown budget, writable persistent `BH_HOME` and configured credentials |

A macOS LaunchDaemon or Linux system unit is an explicit dedicated-account deployment, not the
workstation default. The daemon does not run as root merely to appear machine-wide.

`bh host daemon status` reports lock/control-record state, verified host identity, listener
state, supervisor state when detectable, and authenticated dependency readiness. Doctor gains
a separate Host Daemon section. Setup distinguishes structural failures from optional/advisory
daemon readiness. Hive readiness may warn about an unavailable configured daemon but must not
block direct CLI or stdio work.

Release validation must exercise real crash recovery, login/logout, sleep/wake, and reboot on
supported macOS and Linux configurations, plus container restart, healthcheck, stop, and
descendant reaping. The spike's process-local supervisor harness is insufficient for release.

## Daemon-down behavior

There is no hidden embedded fallback and ordinary requests do not auto-start the daemon.

| Surface | Behavior while daemon is down |
| --- | --- |
| `bh`/`bd` CLI | Continues directly, subject to the configured Dolt target's normal behavior |
| MCP stdio | Continues directly with the existing contract |
| MCP HTTP | Visibly unavailable; prior sessions are gone and clients reconnect after recovery |
| Operator REST/OpenAPI | Visibly unavailable |
| Operator SSE | Disconnected; reconnect and replay if possible, otherwise resnapshot |
| Activity POST | Visibly unavailable; publisher performs bounded idempotent retry |
| Terminal WebSocket | Unavailable; existing sessions are cancelled and descendants cleaned up |
| HQ direct access | Continues for CLI and stdio |
| Owned Dolt | Retains `bd` lazy recovery behavior |
| Shared/external Dolt | Fails according to the configured target; no shadow server is created |

## Telemetry

Telemetry is initialized once in the outer daemon lifespan and flushed once during bounded
shutdown. The daemon uses a distinct service identity such as `service.name=bh-host-daemon`, a
stable `bh.host.id`, and a new `service.instance.id` for every incarnation. Metrics
configuration must preserve the instance identity needed to distinguish restarts.

Direct CLI and stdio processes keep their existing independent initialization and healthy
atexit drain. They additionally need a bounded exporter/force-flush budget and an explicit
timeout/failure result. A dead collector may not reproduce the measured fifteen-second
exporter shutdown or twenty-four-second command path.

Durability belongs in a local collector/agent persistent queue rather than daemon memory.
Cross-invocation work uses explicit run and work-session identifiers and span links; process
lifetime, PID, current directory, filename prefixes, MCP session, and daemon instance are not
correlation substitutes.

Daemon instrumentation includes:

- request rate, errors, and duration by route template, not raw path;
- active and total MCP sessions, SSE clients, terminal connections, disconnects, and
  cancellations;
- bounded queue depth, backpressure, replay gaps, and resets;
- daemon uptime, incarnation/restart, shutdown duration, and flush outcome;
- explicit HQ and Dolt dependency probes;
- pool metrics only for a real daemon-owned pool.

Sensitive and unbounded values, including tokens, full paths, arbitrary IDs, and terminal
content, are forbidden as metric labels.

## OpenAPI and contract conformance

`/openapi.json` is an OpenAPI 3.1 document for the non-MCP HTTP contract. It defines security
requirements, exact parameters, encoded canonical identities, request and response bodies, SSE
cursor errors, activity idempotency, attach-token behavior, standard redacted errors, and status
codes. It is protected by `operator:read`; `/health` remains documented as public and minimal.

MCP schema discovery remains the authority for MCP tool schemas. OpenAPI must not invent a
second MCP description.

The implementation's conformance suite must cover:

- stdio versus HTTP tool names, schemas, results, and errors;
- sessionful and stateless HTTP operation;
- exact hive and run identity resolution, including encoded slash and ambiguity attacks;
- scope separation, token expiry/revocation/rotation, CORS, Host, Origin, TLS, and
  forwarded-header trust;
- MCP session exhaustion and restart with active sessions;
- snapshot/SSE atomicity, replay, heartbeat, reset, retention, and slow consumers;
- durable activity dedupe and daemon-loss retry;
- terminal attach-token replay, limits, backpressure, disconnect, shutdown, and descendant
  cleanup;
- concurrent CLI, stdio, HTTP, and operator mutations through the same guards;
- real platform supervision and container lifecycle;
- bounded telemetry failure and redaction.

## Container and dependent work

The container consequence is a single daemon listener/main workload, but it is not a complete
remote HQ entrypoint today. HQ bootstrap and sync remain SSH-dependent until `bh-pc2a.30`
resolves that transport. The implementation must integrate that result rather than claim the
HTTP listener replaces it.

Likewise, terminal implementation consumes `bh-lx6e.3`; it does not duplicate the PTY spike or
treat route composition as PTY safety evidence.

The current Node relay remains a development/test oracle and rollback path only. It is not
shipped, supervised, or documented as a second product relay after daemon cutover. It may be
removed only after the UI adapter passes the daemon snapshot/SSE conformance matrix.
AgentGuides integration is optional and non-blocking.

## Rollout and release boundary

The implementation molecule is filed from this accepted contract with kickoff initially
pending. Its final conformance child owns the cutover decision. Release requires all of the
following:

1. the platform lifecycle matrix passes on real supported hosts and containers;
2. daemon-down behavior matches the table above without regressions to CLI or stdio;
3. the HTTP/stdio and operator contract suites pass;
4. the terminal safety verdict is consumed and its failure-mode suite passes;
5. telemetry failure is bounded and identity/correlation are correct;
6. dependent container/HQ limitations are explicit and not marketed as solved;
7. generated OpenAPI and checked documentation match the running routes.

Until those gates pass, HTTP MCP and remote operator exposure remain opt-in. Direct CLI and
stdio remain the rollback paths.

## Consequences

The accepted design has one coherent owner for network sessions and their cleanup, while
keeping short-lived local workflows independent. It creates an intentional operational
component that must be installed, authenticated, monitored, and supervised. It also makes
failure visible rather than hiding daemon or shared-store loss behind implicit fallback.

This decision supersedes proof-only route and lifecycle choices where this document is more
specific, notably the browser-safe terminal attach-token route and the full SSE replay/reset
semantics. It does not supersede the existing Dolt target-mode, lease/epoch, HQ, PTY-safety, or
container-HQ work owned by their respective beads.
