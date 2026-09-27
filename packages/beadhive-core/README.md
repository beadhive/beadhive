# beadhive-core

The API-first Beadhive command handlers described in
[the parallel-replacement ADR](../../docs/design/beads-api-first-parallel-replacement-adr.md).
They depend on `beadhive-beads-client` (the pinned Beads v1.3 SDK and `BeadsSession`) and never
import the root `beadhive` application, `bd` argv helpers, Dolt, Pants, or stateful test fixtures.

## Operation routing (bh-l5sxi.1)

`beadhive_core.RoutingTable` (`beadhive_core/routing.py`) turns the bh-97fo0.3 operation matrix
(`beadhive_beads_client.load_operation_matrix`) into one named route per operation — never a
generic storage-provider abstraction, and never an automatic HTTP-to-CLI fallback. Every one of
the matrix's 64 operations is exactly one of four kinds, taken verbatim from its classification:

| Kind | Type | Selector | Carries |
|---|---|---|---|
| `api-ready` | `ApiRoute` | `select_api(name, capabilities)` | `capability`, `session_method`, `evidence` |
| `cli-compatibility` | `CompatibilityRoute` | `select_cli(name)` | `reason` |
| `administrative` | `CompatibilityRoute` | `select_administrative(name)` | `reason` |
| `denied` | `DeniedRoute` | unroutable — every selector raises `OperationDenied` | `reason` |

`select_api` raises `beadhive_beads_client.CapabilityMissing` when the caller's negotiated
capabilities don't cover the route's required capability, and `RouteMismatch` when the named
operation isn't actually api-ready — both **before** a caller can reach the session method, so an
unsupported capability fails before any mutation is attempted. `RoutingTable.call_api(session,
name, *args, **kwargs)` is the one dispatch point: it selects the route against
`session.context.capabilities` and calls the exact `BeadsSession` method the matrix names — never
a method chosen generically at runtime. `select_cli` / `select_administrative` only name the
route and its reason; the compatibility shell still owns the actual `bd` invocation (`work_review.py`'s
`CliGateOperations` / `CliStateOperations` and `coordination.py` are the existing examples).
Every selector accepts an optional `RoutingObserver` so the chosen route can be attributed in
telemetry.

`COORDINATION_OPERATIONS` names every lease, heartbeat, reclaim, merge-slot and gate operation
that must stay `cli-compatibility`: their exclusivity and staleness guarantees come from the real
service or the real `bd` binary (`tests/test_coordination_int.py`, `tests/test_merge_slot.py`),
never from an in-memory stand-in, and a policy test fails the day the installed matrix moves one
of them to `api-ready` without a deliberate, evidenced bead.

`beadhive_core.review`'s `GATE_ROUTES` / `STATE_ROUTES` are now resolved through this table
(`select_cli` at import time) instead of as bare literals, so the same "the matrix is the only
source of truth" guarantee covers the ports `ReviewCommands` already depends on.

## Review: approve and bounce

`beadhive_core.ReviewCommands` owns the review policy only: which open gates count as review
gates, the warden-only `security:*` and releaser-only `release-hold:` resolutions, the human-gate
and self-review guards, the allowed review transitions, the operator-facing result lines, and the
observer notifications. Durable issue behavior goes to Beads over HTTP:

| Effect | Route |
|---|---|
| Read the bead (status, assignee, labels, revision) | `issues.get` |
| Approve: drop stale `review:*` labels | `issues.update`, guarded by revision |
| Bounce: record the feedback as a comment | `issues.addComment` |

Beads v1.3 has no gate lookup/create/resolve route and no state-dimension route, so those are two
narrow ports the compatibility shell implements over its named `bd` CLI routes:

- `GateOperations` — `work.gate.lookup`, `work.gate.resolve`
- `StateOperations` — `work.state.update` (`bd set-state` writes the transition event that rework
  metrics read, plus the `review:<value>` label)

Gate semantics are never fabricated from generic issue operations: no generic close stands in for a
gate resolution, and nothing here creates a gate. Every read happens before the first write; writes
stop at the first failure, and an ambiguous HTTP write is reported, never replayed.

The installed `beadhive` shell selects these handlers at one seam, `src/beadhive/work_review.py`,
which resolves the hive's one supervised Beads service (`bh host beads`, see
[`docs/BEADS-SERVICE.md`](../../docs/BEADS-SERVICE.md)). Neither the core nor the shell ever
starts `bd serve`: with no verified service running, approve and bounce fail closed and name
`bh host beads start --hive <hive>`. Session factories signal that with the client's
`ServiceError` or the core's `SessionUnavailable` (embedded-Dolt hives cannot be served).

## Tests

- `test_core_routing_policy.py` — every matrix row resolves to exactly one typed route; capability
  gating, denial, and route-kind mismatches; `call_api` dispatch against a generated-client
  transport fixture. No Beads state emulator.
- `test_core_routing_real_service.py` — opt-in (`BEADS_ROUTING_SCRATCH=1 ... -m real_service`)
  proof that the routed `work.merge-slot.acquire` CLI path is still exclusive under contention,
  against a disposable scratch hive this test creates itself (never a managed hive).
- `test_core_review_policy.py` — policy over generated-client transport fixtures and fake ports.
- `test_core_package_boundary.py` — import independence and operation-matrix route classification.
- `test_core_review_real_service.py` — opt-in (`-m real_service`) proof of the HTTP writes and
  their audit actor against a scratch hive's service started with `bh host beads start`; see its
  module docstring.
