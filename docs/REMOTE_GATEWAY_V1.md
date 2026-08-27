# Development gateway read contract

`gateway.v1` is the authenticated, read-only browser contract for the initial Development
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
  work items, 256 agents, or 64 labels on one work item.

Both calls require `Authorization: Bearer <token>` and the exact Development `Origin`. Browser
preflight permits only GET and Authorization. All responses are `no-store`; the profile exposes
no write, command, terminal, transcript, local-path, or event-stream capability in this version.

## Stable errors

Errors use only `{error: {code, message, retryable}}`. Invalid signatures or claims share
`authentication_failed` (401), origin or Host rejection uses `request_denied` (403), hidden or
unauthorized resources use `resource_not_found` (404), and unusable internal snapshots use
`runtime_unavailable` (503). No failure reflects a token, claim, policy membership, path, or
internal exception.

The executable conformance contract is in `tests/test_remote_gateway.py`; response construction
is guarded by a recursive exact-value and wire-type `remote_payload_is_allowlisted` check before
JSON serialization. Runtime source callbacks and projection run outside the ASGI event loop.
