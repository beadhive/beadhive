# beadhive-core

The API-first Beadhive command handlers described in
[the parallel-replacement ADR](../../docs/design/beads-api-first-parallel-replacement-adr.md).
They depend on `beadhive-beads-client` (the pinned Beads v1.3 SDK and `BeadsSession`) and never
import the root `beadhive` application, `bd` argv helpers, Dolt, Pants, or stateful test fixtures.

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

- `test_core_review_policy.py` — policy over generated-client transport fixtures and fake ports.
- `test_core_package_boundary.py` — import independence and operation-matrix route classification.
- `test_core_review_real_service.py` — opt-in (`-m real_service`) proof of the HTTP writes and
  their audit actor against a scratch hive's service started with `bh host beads start`; see its
  module docstring.
