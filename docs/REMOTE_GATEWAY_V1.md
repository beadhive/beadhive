# Development gateway contract

`gateway.v1` is the authenticated browser contract for the initial Development
runtime. It is a separate application profile from the unchanged loopback Operator API.

## Fixed boundary

- issuer: `https://rapid-snail-6758.clerk.accounts.dev`
- audience: `beadhive-gateway-dev`
- browser origin: `https://app.dev.beadhive.cloud`
- gateway origin: `https://gateway.dev.beadhive.cloud`
- logical instance: `dev/demo`
- JWS algorithm: RS256

The gateway verifies signature, exact issuer and audience, expiry, optional not-before, and a
non-empty subject. It then resolves that subject against the server-owned instance registry.
Tokens do not carry or select an instance scope. Revoked subjects and subjects absent from the
registry receive no runtime access.

## Endpoints

- `GET /v1/instances?limit=50` returns the caller's bounded authorized instance page. For this
  profile it contains either `dev/demo` or no items and always has `nextCursor: null`.
- `GET /v1/instances/dev/demo/snapshot` returns a `gateway.v1` envelope containing snapshot
  schema version 1. Only the explicitly projected work-item and agent summary fields cross the
  remote boundary. The initial profile fails unavailable rather than serializing more than 1,000
  work items, 256 agents, or 64 labels on one work item. Millisecond timestamps are non-negative
  integers no greater than JavaScript's exact integer limit (`2^53 - 1`).
- `POST /v1/instances/dev/demo/commands/refresh` invokes the sole initial command only when
  discovery advertises `refresh`. Its exact JSON input is schema version 1, a browser-generated
  correlation ID restricted to a canonical lowercase UUIDv4, and the
  expected `sha256:<64 lowercase hex>` snapshot revision. The runtime command authority checks
  that revision atomically and returns only `completed` plus its resulting revision. The gateway
  correlates the receipt with the input ID; runtime extras are discarded.
- `GET /v1/instances/dev/demo/events?cursor=<cursor>` is fetch-compatible SSE when discovery
  advertises `events`. A stream-capable snapshot carries its starting `eventCursor` as a
  canonical lowercase UUIDv4 producer epoch plus a non-negative sequence. Each emitted
  `snapshot-invalidated` event advances that same epoch by exactly one sequence and exposes only
  its cursor and resulting snapshot revision. Clients fetch a fresh snapshot after invalidation;
  event payloads never duplicate work-item, agent, transcript, or workspace data.

All calls require `Authorization: Bearer <token>` and the exact Development `Origin`. Browser
preflight permits GET with Authorization, or POST to the exact refresh route with Authorization
and Content-Type. All responses are `no-store`; the profile exposes no generic write, terminal,
transcript, local-path, or event-stream capability in this version.

The command body is bounded to 2 KiB and has no free-form argument. At invocation time the
gateway verifies the token again, resolves the subject against the server-owned instance policy
again, and verifies that the resolved instance still advertises `refresh`. Hidden commands and
instances share the stable not-found response. A runtime may revoke the subject or remove the
capability without trusting an earlier discovery response. Command execution has its own
deadline and concurrency bulkhead, separate from discovery and snapshots.

Event replay is owned by the runtime port. A reconnect supplies the last event cursor and the
runtime returns only retained successors. A stale cursor, retention gap, or producer restart
returns the fixed `resnapshot_required` response before streaming begins. An epoch change,
sequence gap, or malformed event observed after streaming begins emits one fixed
`resnapshot-required` control event and closes. Stream opens have a deadline, live streams have a
separate concurrency limit, and the gateway re-verifies the token and current instance policy at
least once per second even while the source is idle. Scope or identity loss closes the stream
without disclosing which policy changed.

## Stable errors

Errors use only `{error: {code, message, retryable}}`. Invalid signatures or claims share
`authentication_failed` (401), origin or Host rejection uses `request_denied` (403), hidden or
unauthorized resources use `resource_not_found` (404), and unusable internal snapshots use
`runtime_unavailable` (503). A stale expected revision uses the fixed `scope_conflict` (409)
shape; an unusable event cursor uses fixed `resnapshot_required` (409). No failure reflects a
token, claim, policy membership, path, command input, or internal exception.

The executable conformance contract is in `tests/test_remote_gateway.py`; response construction
is guarded by a recursive exact-value and wire-type `remote_payload_is_allowlisted` check before
JSON serialization. Runtime sources implement an async, cancellation-aware port and must move
any blocking storage access behind their own cancellable boundary. Discovery availability,
snapshot availability, snapshot reads, commands, and stream opens have independent concurrency
bulkheads. Calls have a five-second deadline, saturation fails unavailable immediately instead
of creating an internal queue, and ASGI shutdown cancels and joins every admitted runtime
operation or event read.

## Owned-host Development profile

The `beadhive-gateway` entry point is the deployable `dev/demo` profile. It binds only
`127.0.0.1:8787` and reads the real registered `github/beadhive/beadhive` snapshot and retained
event stream from the existing loopback host daemon at `127.0.0.1:8420`. It never reads a fixture
or accepts a browser-selected hive. Its `refresh` command performs a revision-checked refresh of
that authoritative source.

The launcher accepts Clerk public JWKS and the authorized Development subject list only through
mode-0600 service credential files. Under systemd, the default names are
`clerk-jwks.json` and `authorized-subjects.json` below `CREDENTIALS_DIRECTORY`; optional explicit
paths exist for other service managers. The process never accepts keys, subjects, origins,
audiences, instance IDs, listener addresses, or local source locations as command arguments.

[`deploy/systemd/beadhive-gateway-dev.service.example`](../deploy/systemd/beadhive-gateway-dev.service.example)
is the least-privilege user-service template. It has no capabilities, writable home, device
access, or mutable system paths. Cloudflared remains a separate service and credential boundary.
The local health probe is:

```sh
curl --fail --silent --show-error \
  --header 'Host: gateway.dev.beadhive.cloud' \
  http://127.0.0.1:8787/healthz
```

The health response contains only liveness and `gateway.v1`; requests carrying a browser Origin
are refused. Readiness of the real data source remains visible through authenticated discovery as
`online` or `offline`.
