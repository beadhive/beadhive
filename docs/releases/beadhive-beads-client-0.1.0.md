# Beadhive Beads client 0.1.0 handoff

**Release state:** source and wheel contract pinned for downstream integration.
This handoff does not publish a package, deploy a service, or change Beadhive's
default command transport.

## Immutable source and artifact record

| Item | Exact value |
|---|---|
| Package | `beadhive-beads-client==0.1.0` |
| Source commit | `1c5feb0f9cac9a62affd0414c8a24daaa9cff9c5` |
| Package tree | `f7cddb715dbdeb04954c12e5456569e7becb583b` |
| Generated SDK tree | `929e9e4c4cc3f6f033ee58cff64a44c429aa0750` |
| Session and matrix tree | `434d14702bf2d76e6b07408c4c1c254d96adad04` |
| Beads release source | `f45b249ce6b40ba62aecc03949e6371e8f7c79d8` |
| OpenAPI SHA-256 | `9a33a349e5266bdba916246594a4fc457c63e39d7affb5acec9bd5b75acde4dd` |
| Generator | `openapi-python-client==0.29.1`, locked in `uv.lock` |
| Wheel SHA-256 | `59118977c53b9c4b0ed97ed512f9615b7fcf9c76976b19e4f57a45e09376a3a0` |
| Source distribution SHA-256 | `aa72a8e4404f3b8c337fb2334f8f76a4a9ba38cdc268a7dff4830d15d2a0dded` |

The wheel and source distribution were built from the clean source tree with
`uv build packages/beadhive-beads-client --no-build-isolation`. The wheel has
165 entries, including the generated problem model, handwritten session and
installed `operation_matrix_v1.json`. A new Python 3.11 virtual environment
installed the wheel and its eight runtime dependencies offline; isolated
imports of both packages, generated models, session and matrix passed without
loading root `beadhive`. A second independent build produced identical wheel
and source distribution SHA-256 values.

The checked-in `regenerate.py` compares all 159 generated Python files against
the pinned OpenAPI source and fails on SHA or code drift. After one locked
dependency install, `just beads-client-check` performs that comparison and
package-local policy tests offline. The real-service conformance result and
parity findings are in
[`bh-97fo0.3-beads-v13-client-conformance.md`](../proof/bh-97fo0.3-beads-v13-client-conformance.md).

## Endpoint contract

Downstream `beadhive-core` imports `beadhive_beads_client.BeadsSession` and
generated `beads_v1_3` request/response types. It supplies an
`ExpectedContext(project_id, database, required_capabilities, repo_root)`
from the selected hive. For a local service, use `LocalEndpoint` with a fixed
loopback port, repository root and exact `bd` executable; a token file is
optional. For an externally managed or team service, use `RemoteEndpoint`
with an HTTPS URL and bearer token. A loopback HTTP endpoint is allowed for
local development. `GET /v0/beads/context` reveals the expected project ID
and database; a local workspace also records them in `.beads/metadata.json`.

Both modes authenticate and verify `v0`, Beads `1.3.0`, project/database
identity, required capabilities and a real ready query before work requests.
The session stamps `Bd-Project-Id` on operational requests and uses a bounded
request deadline. Local supervision bounds startup and shutdown. Remote mode
does not spawn `bd`. The verified bearer token never appears in a process
argument. No service topology is inferred from the endpoint URL; embedded,
external Dolt and team service are advertised Beads deployment properties.

The installed versioned matrix classifies every operation required by the filed
`bh-bwnys.1` and `bh-sy36q.2/.3/.5` consumers as
`api-ready`, `cli-compatibility`, `administrative` or `denied`. `api-ready`
describes the transport contract, not an already switched Beadhive command.
The selected HTTP writes require explicit trusted actor, CLI-compatible
defaults and hook policy at the downstream command boundary. The generated
SDK contains all source operations, but the supported session does not expose
issue delete or sweep. Proven session operations include comment append,
ownership-fenced claim release, metadata compare-and-set, and labels through
guarded update. Gates, state dimensions, renewable lease heartbeat/reclaim,
merge slots, unproved batch variants, review, merge, Dolt sync, backup and
migration retain named CLI paths.

The handoff intentionally records a blocker for the first downstream approval
and bounce slice: Beads v1.3 has no gate lookup/create/resolve route,
`createIssue` cannot set `await_type`, and generic close is not a proven gate
resolution. Those commands must consume the named CLI compatibility routes or
be replanned; the client does not fabricate gate semantics from generic issue
operations.

## Rollback and handoff

The installed `beadhive` compatibility shell keeps its current CLI path until
a later command cohort opts into this package at the top-level composition
boundary. To roll back a selected command, return that command's composition
choice to its named CLI path before dispatching new writes. For an HTTP write
whose response was lost, first read the issue and audit history to reconcile
its outcome; never replay through CLI merely because HTTP timed out. Keep the
package and matrix pinned while investigating drift. A version, context or
capability mismatch fails closed.

`bh-bwnys` and `bh-sy36q` may consume this source tree, wheel and matrix as the
stable foundation for the parallel core. They own command-specific policy,
output and legacy-code deletion. Factory or team-service activation is a
separate operational decision.
