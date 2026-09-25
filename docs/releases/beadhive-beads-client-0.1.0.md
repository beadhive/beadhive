# Beadhive Beads client 0.1.0 handoff

**Release state:** source and wheel contract pinned for downstream integration.
This handoff does not publish a package, deploy a service, or change Beadhive's
default command transport.

## Immutable source and artifact record

| Item | Exact value |
|---|---|
| Package | `beadhive-beads-client==0.1.0` |
| Source commit | `80a9b65f6ff615527e3a092313a7b0f9c0cdcc74` |
| Package tree | `a2b7d70a1bbdc45ec54690f213630d31d7f81f24` |
| Generated SDK tree | `929e9e4c4cc3f6f033ee58cff64a44c429aa0750` |
| Session and matrix tree | `526ce243cda7cae3b5883a1c579dc7043714cca2` |
| Beads release source | `f45b249ce6b40ba62aecc03949e6371e8f7c79d8` |
| OpenAPI SHA-256 | `9a33a349e5266bdba916246594a4fc457c63e39d7affb5acec9bd5b75acde4dd` |
| Generator | `openapi-python-client==0.29.1`, locked in `uv.lock` |
| Wheel SHA-256 | `520e03fde792b4628ae89e58ef470cb7fdc51659c2bad7195119825cb1d296ae` |
| Source distribution SHA-256 | `007bdee766dc9cfdba0e1f17f93ca44e4aa0ea9a8c7fe3c0ced5eb583c840e24` |

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

The installed versioned matrix classifies each work/planning operation as
`api-ready`, `cli-compatibility`, `administrative` or `denied`. `api-ready`
describes the transport contract, not an already switched Beadhive command.
The selected HTTP writes require explicit trusted actor, CLI-compatible
defaults and hook policy at the downstream command boundary. The generated
SDK contains all source operations, but the supported session does not expose
issue delete or sweep. Gates, state dimensions, review, merge, Dolt sync,
backup and migration retain named CLI paths.

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
