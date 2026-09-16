# Beadhive Frame Bridge Development profile

The **Beadhive Frame Bridge** is the per-frame adapter implemented in this repository. It exposes
one host daemon through the Gateway-owned `gateway.v1` contract. The multi-frame
**Beadhive Gateway**, its public route semantics, and cross-repository conformance package are
authored in the sibling `beadhive-gateway` repository.

The names describe different scopes: the Frame Bridge projects one Bead Frame; the Gateway
discovers and aggregates multiple frames. This Development profile remains separate from the
host daemon's loopback Operator API.

## Fixed boundary

- issuer: `https://rapid-snail-6758.clerk.accounts.dev`
- audience: `beadhive-gateway-dev`
- default cloud browser origin: `https://app-dev.beadhive.cloud`
- default cloud gateway origin: `https://gateway-dev.beadhive.cloud`
- opt-in local-desktop app origin: `tauri://localhost`
- opt-in local-desktop gateway origin: `http://127.0.0.1:8787`
- logical instance: `dev/demo`
- cloud DEV JWS algorithm: RS256

Cloud DEV verifies signature, exact issuer and audience, expiry, optional not-before, and a
non-empty Clerk subject. It then resolves that subject against the server-owned instance
registry. Tokens do not carry or select an instance scope. Revoked subjects and subjects absent
from the registry receive no runtime access. The exact local-desktop tuple does not construct a
Clerk verifier or read Clerk JWKS/subject policy. Instead, exact loopback Host plus exact Tauri
Origin selects one fixed server-owned local principal; that principal is authorized only for the
sealed `dev/demo` composition.

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
  that revision atomically and returns only `completed` plus its resulting revision. The Frame
  Bridge correlates the receipt with the input ID; runtime extras are discarded.
- `GET /v1/instances/dev/demo/events?cursor=<cursor>` is fetch-compatible SSE when discovery
  advertises `events`. A stream-capable snapshot carries its starting `eventCursor` as a
  canonical lowercase UUIDv4 producer epoch plus a non-negative sequence. Each emitted
  `snapshot-invalidated` event advances that same epoch by exactly one sequence and exposes only
  its cursor and resulting snapshot revision. Clients fetch a fresh snapshot after invalidation;
  event payloads never duplicate work-item, agent, transcript, or workspace data.

All calls require the exact Host and Origin from one complete Development network profile. Cloud
DEV additionally requires `Authorization: Bearer <token>`; its preflight permits GET with
Authorization, or POST to the exact refresh route with Authorization and Content-Type. The
local-desktop profile accepts no Authorization header: its GET preflight requests no headers and
its refresh preflight requests Content-Type only. Cloud and local-desktop values cannot be mixed,
and ordinary browser origins remain denied. All responses are `no-store`; the profile exposes no
generic write, terminal, transcript, local-path, or event-stream capability in this version.

The command body is bounded to 2 KiB and has no free-form argument. At invocation time the
Frame Bridge verifies the token again, resolves the subject against the server-owned instance
policy again, and verifies that the resolved instance still advertises `refresh`. Hidden commands
and instances share the stable not-found response. A runtime may revoke the subject or remove
the capability without trusting an earlier discovery response. Command execution has its own
deadline and concurrency bulkhead, separate from discovery and snapshots.

Event replay is owned by the runtime port. A reconnect supplies the last event cursor and the
runtime returns only retained successors. A stale cursor, retention gap, or producer restart
returns the fixed `resnapshot_required` response before streaming begins. An epoch change,
sequence gap, or malformed event observed after streaming begins emits one fixed
`resnapshot-required` control event and closes. Stream opens have a deadline, live streams have a
separate concurrency limit, and the Frame Bridge re-verifies the cloud token or the sealed local
request boundary plus current instance policy at least once per second even while the source is
idle. Scope or identity loss closes the stream without disclosing which policy changed.

## Stable errors

Errors use only `{error: {code, message, retryable}}`. Invalid signatures or claims share
`authentication_failed` (401), origin or Host rejection uses `request_denied` (403), hidden or
unauthorized resources use `resource_not_found` (404), and unusable internal snapshots use
`runtime_unavailable` (503). A stale expected revision uses the fixed `scope_conflict` (409)
shape; an unusable event cursor uses fixed `resnapshot_required` (409). No failure reflects a
token, claim, policy membership, path, command input, or internal exception.

The executable conformance contract is in `tests/test_frame_bridge.py`; response construction
is guarded by a recursive exact-value and wire-type `frame_bridge_payload_is_allowlisted` check
before JSON serialization. Runtime sources implement an async, cancellation-aware port and must move
any blocking storage access behind their own cancellable boundary. Discovery availability,
snapshot availability, snapshot reads, commands, and stream opens have independent concurrency
bulkheads. Calls have a five-second deadline, saturation fails unavailable immediately instead
of creating an internal queue, and ASGI shutdown cancels and joins every admitted runtime
operation or event read.

## Owned-host Development profile

The `beadhive-frame-bridge` entry point is the deployable `dev/demo` profile. Core does not install
a `beadhive-gateway` command; that name is reserved for the multi-frame Gateway. The Frame Bridge
binds only `127.0.0.1:8787` and reads the real registered `github/beadhive/beadhive` snapshot and
retained event stream from the existing loopback host daemon at `127.0.0.1:8420`. It never reads
a fixture or accepts a browser-selected hive. Its `refresh` command performs a revision-checked
refresh of that authoritative source. The Development demo projection includes current `open`,
`in_progress`, and `blocked` work while omitting closed/deferred history and internal `event` and
`gate` records. The Frame Bridge's independent 1,000-item fail-closed bound still applies after this
selection.

The cloud launcher accepts Clerk public JWKS, the authorized Development subject list, and one
independently scoped host-daemon bearer only through mode-0600 service credential files. Under
systemd, the default names are `clerk-jwks.json`, `authorized-subjects.json`, and `daemon-bearer`
below `CREDENTIALS_DIRECTORY`; optional explicit paths exist for other service managers. The
local-desktop profile requires only `daemon-bearer` and does not read the Clerk files. The
daemon bearer requires only `operator:read`, is attached only to the fixed loopback daemon
snapshot and event requests, and is never derived from or replaced by a remote caller token.
The process never accepts keys, subjects, bearer values, free-form origins, audiences, instance
IDs, listener addresses, or local source locations as command arguments. The optional environment
variables `BEADHIVE_FRAME_BRIDGE_JWKS_FILE`, `BEADHIVE_FRAME_BRIDGE_SUBJECTS_FILE`, and
`BEADHIVE_FRAME_BRIDGE_DAEMON_CREDENTIAL_FILE` carry file paths only; the unreleased
`BEADHIVE_GATEWAY_*` aliases are absent.

Cloud is the default network profile. Local testing of an installed `Beadhive-Dev.app` opts into
the only other sealed tuple at process startup:

```sh
BEADHIVE_FRAME_BRIDGE_NETWORK_PROFILE=local-desktop beadhive-frame-bridge
```

That process still binds only `127.0.0.1:8787`; it admits requests without Authorization only
when Host is exactly `127.0.0.1:8787` and Origin is exactly `tauri://localhost`. It rejects Clerk
bearers in this mode, ordinary browser origins such as `http://127.0.0.1` and `http://localhost`,
and alternate Host spellings. An unknown profile value fails startup. The profile selector
carries no endpoint value or credential.

[`deploy/systemd/beadhive-frame-bridge-dev.service.example`](../deploy/systemd/beadhive-frame-bridge-dev.service.example)
is the least-privilege user-service template. It has no capabilities, writable home, device
access, or mutable system paths. Cloudflared remains a separate service and credential boundary
for the default cloud profile; the opt-in local-desktop profile connects directly over loopback.
The local health probe is:

```sh
curl --fail --silent --show-error \
  --header 'Host: 127.0.0.1:8787' \
  http://127.0.0.1:8787/healthz
```

The health response contains only liveness and `gateway.v1`; requests carrying a browser Origin
are refused. Readiness of the real data source remains visible through profile-authorized
discovery as `online` or `offline`.
