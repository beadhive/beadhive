# Authenticated host daemon

The unified host daemon is an opt-in network surface. Direct `bh`/`bd` CLI and MCP stdio remain
daemon-independent. The checked consumer record is
[`live-contract.json`](../tests/fixtures/host_daemon/v1/live-contract.json); it is the endpoint
handoff for `bhui-unkw` and the input to `bhui-61s9.11`.

Its live composition proof starts the product application on a real pre-bound Uvicorn TCP
listener over a real `bd` hive, then concurrently exercises direct `bh`, MCP stdio, authenticated
HTTP MCP, and operator REST. The deterministic `durable-append-held` barrier keeps an authenticated
SSE subscription, sessionful HTTP MCP, and MCP stdio open while the BAML publication is pending;
the proof performs a guarded `config_set`, direct CLI read, stdio discovery, and REST reads before
releasing the durable append. It checks `durable-ack-before-sse`, exact activity identity and
payload, and same-epoch contiguous cursor order. The `refusal-cli-stdio-refusal` outage sequence
stops and joins Uvicorn, observes TCP refusal, starts fresh direct-CLI and stdio clients, then
observes refusal again so neither client can hide an auto-start or listener rebind. Finally it
checks FastMCP, credential-session, operator-SSE, state-broker, telemetry, and Uvicorn teardown.
The fixture's evidence node IDs are validated with pytest collection rather than treated as
unchecked prose.

## Install and credentials

Install Beadhive normally, initialize `BH_HOME`, and create one owner-only verifier file outside
the repository. Provisioning returns a bearer once; store that bearer in the client secret store,
not in config, shell history, a URL, logs, or this fixture. The verifier file itself contains only
digests and must remain mode `0600`.

```python
from pathlib import Path
from time import time

from beadhive.daemon_auth import provision_credential_file
from beadhive.daemon_contract import AuthScope

credential = provision_credential_file(
    Path("/absolute/private/path/daemon-credentials.json"),
    credential_id="operator-ui",
    audience="beadhive-host",
    principal="operator:local",
    scopes=(AuthScope.OPERATOR_READ,),
    expires_at=int(time()) + 86400,
)
# Transfer this one-time value directly into the client secret store.
print(credential.bearer.reveal_for_authority())
```

Provision separate credentials for `mcp:control`, `operator:read`, `activity:publish`, and
`terminal:attach`; one scope never implies another. Configure the absolute verifier path and the
exact browser origin in the host-scoped configuration:

```yaml
host:
  daemon:
    enabled: true
    bind: 127.0.0.1
    port: 8420
    auth:
      credential_file: /absolute/private/path/daemon-credentials.json
    cors:
      allowed_origins: ["http://127.0.0.1:3000"]
    http:
      allowed_hosts: ["127.0.0.1"]
    mcp:
      mode: sessionful
```

Use `bh host daemon install`, then `bh host daemon start` and `bh host daemon status`. On macOS
this installs a per-user LaunchAgent. On Linux it installs a persistent `systemd --user` unit only
when the user manager/linger capability is available. Containers supervise the daemon as their
main workload; they do not run an OS supervisor inside the container.

## Opt-in migration

Keep existing clients on MCP stdio while bringing up the authenticated listener. Validate
`/health`, then authenticate `/openapi.json` and `/api/v1/factory`. Move one client at a time to
`/mcp`; sessionful is the default and stateless is supported when notifications are unnecessary.
An HTTP failure never silently starts a daemon or falls back. Keep the original stdio client
configuration until the HTTP consumer has passed reconnect and restart checks.

Loopback still requires authentication. A non-loopback bind additionally requires direct TLS or
an explicitly trusted TLS-terminating proxy. CORS origins, HTTP `Host`, WebSocket `Origin`, and
trusted proxy addresses are exact allowlists; bearer credentials travel only in the
`Authorization` header.

## Exact UI endpoint recipe

Use base URL `http://127.0.0.1:8420` for the local authenticated profile and encode the two slashes
in `github/beadhive/beadhive` as uppercase `%2F` when inserting the canonical hive path value.

1. `GET /api/v1/factory` with `operator:read`; select the exact `hiveId` and its `encodedHiveId`.
2. Optionally page `GET /api/v1/factory/hives` for bounded queue counts and availability.
3. `GET /api/v1/hives/{encodedHiveId}/snapshot` with `operator:read`; render it before live data
   and retain its `cursor` (`subscriptionId`, `producerEpoch`, and `sequence`).
4. Open `GET /api/v1/hives/{encodedHiveId}/events` with `operator:read`. Resume with either
   `Last-Event-ID: {producerEpoch}:{sequence}` or byte-identical `?after=...`, never conflicting
   values. The SSE `id` must match the event payload. On `409`, `reset`, a gap, or an epoch change,
   discard live state, fetch a new snapshot, and create one new subscription.
5. Read `GET /api/v1/runs/{encodedRunId}/activity` with `operator:read`. BAML, Hitch, and Beadhive
   publish to the same route with `activity:publish`, an exact path/payload run ID, and a stable
   idempotency key. Treat only durable `created` or `duplicate` acknowledgement as success.
6. Discover MCP tools at `/mcp`; MCP schema discovery, not OpenAPI, owns their schemas.

The terminal routes intentionally return `terminal.unavailable` with
`code=pty_verdict_pending` and `verdictBead=bh-lx6e.3`. Do not manufacture terminal availability.

## Beads service supervision

When a hive is enabled with `bh host beads enable`, the daemon also supervises that hive's
`bd serve`. Commands, files, measured cost, and shared-Dolt connection limits are in
[Beads service](BEADS-SERVICE.md).

## Daemon-down behavior

Direct CLI and MCP stdio continue against their configured stores. MCP HTTP, operator REST and
OpenAPI are unavailable. SSE disconnects and later replays a retained cursor or resnapshots.
Activity publishers retain a bounded idempotent retry until success or explicit expiry/drop.
Daemon restart invalidates HTTP MCP sessions and changes the producer epoch when continuity cannot
be proved; clients create a new MCP session and follow the snapshot/reset rule above.

The listener is not a complete remote-HQ transport: under `bh-pc2a.30`, HQ bootstrap and sync are
still SSH-only, so an HTTPS-token-only container cannot sync HQ. Run those operations from a host.

## Rollback

Stop the authenticated daemon, restore the prior host configuration and credential verifier, and
return HTTP MCP clients to MCP stdio. Operator clients remain visibly offline; do not point them at
fixtures or an embedded fallback. The Beadhive UI Node relay remains a test oracle and rollback
path through `bhui-61s9`; do not retire it in this cutover.

Release evidence has two distinct stages after this source lands:

1. `bh-q0lol` owns exact-final-tip real container 7/7 integration proof. That evidence closes this
   source integration boundary; it does not certify an unavailable external host.
2. The deferred `bh-hxbln` certification molecule owns Darwin LaunchAgent and persistent Linux
   `systemd --user` release certification through its executable real-host matrix `bh-hxbln.1`.
   That matrix must rerun all seven container cells. All cells must use the
   same exact release-candidate revision and complete within its seven-day freshness window.
   This required rerun establishes complete release-matrix coherence; it does not replace q0lol's
   proof or transfer its exact-final-tip container integration ownership.

This handoff makes no Darwin or persistent-Linux release claim; those targets remain release-gated
until the deferred matrix passes.
