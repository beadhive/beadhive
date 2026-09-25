# Beads v1.3 client conformance and operation matrix

- **Date:** 2026-09-25
- **Beads runtime:** `1.3.0 (f45b249ce: HEAD@f45b249ce6b4)`
- **OpenAPI source:** `f45b249ce6b40ba62aecc03949e6371e8f7c79d8`, SHA-256
  `9a33a349e5266bdba916246594a4fc457c63e39d7affb5acec9bd5b75acde4dd`
- **SDK generator:** `openapi-python-client==0.29.1`, uv locked
- **Generated client source commit:** `ff4b35e9`; reviewed session and matrix source
  commit: `1c5feb0f`
- **Versioned installed matrix:**
  `packages/beadhive-beads-client/src/beadhive_beads_client/operation_matrix_v1.json`

The proof used a disposable Git repository under `/tmp`, initialized with
`bd init --proxied-server --prefix conf`. One `bd serve` listened on loopback
without authentication and a second listened with a test bearer token. The
project ID and database were read from `/v0/beads/context` and supplied to
`ExpectedContext`. A third service was started and stopped by `LocalEndpoint`.
No managed hive database, Factory endpoint or production deployment was used.

Run the opt-in suite against a new scratch service with the following variables:

```sh
BEADS_V13_URL=http://127.0.0.1:<port> \
BEADS_V13_AUTH_URL=http://127.0.0.1:<auth-port> \
BEADS_V13_AUTH_TOKEN=<test-token> \
BEADS_V13_PROJECT_ID=<context.project_id> \
BEADS_V13_DATABASE=<context.database> \
BEADS_V13_WORKSPACE=<absolute-scratch-repo> \
uv run --locked --package beadhive-beads-client \
  pytest packages/beadhive-beads-client/tests/test_real_service.py -q
```

The review-fix run passed **3 tests in 4.20 seconds**. It proved:

| Contract | Observation |
|---|---|
| Context and credentials | Missing bearer token is refused at context; valid token reaches ready. Wrong project ID is refused before work access. |
| Reads | Typed 404, issue detail, ready list and two-page issue traversal. Cursor pages did not repeat a row. |
| Relationships | Real-service add, list, remove and re-add dependency; re-add restored the blocked selection. |
| Create and update | HTTP creates, guarded patch, stale revision `409 precondition_failed`, label add/remove and readback. |
| Metadata and feedback | Metadata CAS proved a successful swap and a stale-expectation refusal; comment append proved author and body readback. |
| Lifecycle | Claim, atomic claim-next excluding a blocked issue, ownership-fenced release, guarded close and reopen. |
| Concurrency | Two simultaneous claims on one issue yielded exactly one success and one `already_claimed` refusal. |
| Ambiguous response | A transport sent one real PATCH, discarded its response, and raised `ReadTimeout`. The session raised `IndeterminateWrite`, did not retry, and a detail read found the committed notes. |
| Local supervision | A session started a fixed-port `bd serve`, negotiated context and ready, then terminated and reaped the process. |
| Audit | `bd history <id> --events` reported the caller-supplied actor for HTTP writes. |

The proof exposed two differences that downstream command cutovers must handle:

1. HTTP create refused omitted `issue_type`. An omitted `priority` stored `0`,
   whereas the CLI create in the same workspace defaulted to `2`. The proven
   create requests therefore sent `issue_type="task"` and `priority=2`
   explicitly. The HTTP create response left `created_by` absent even though
   legacy audit history recorded the supplied actor. Callers must not infer
   authenticated identity from `actor` or `created_by`.
2. The installed `bd serve --help` contract states that HTTP mutations do not
   invoke CLI `on_update` hooks. The real proof verified legacy audit events,
   but the durable `events-journal` remained disabled. Its advertised
   `events.list` capability is a build capability, not proof that this
   workspace has a journal. Commands requiring CLI hook behavior remain on
   explicit compatibility paths until their shell policy is redesigned.

In `operation_matrix_v1.json`, **api-ready means the generated transport and
session operation have a proven wire contract**. It does not switch an existing
`bh work` or `bh plan` command. The matrix has one named row for every operation
required by `bh-bwnys.1` and `bh-sy36q.2/.3/.5`, including comments, labels,
metadata, gates, leases, heartbeat, reclaim, release, merge-slot coordination,
molecule polling, and batch/partial-failure variants. The downstream core must preserve command
output, identity, hooks, validation, review gates and Git safety before a
top-level command cutover. `batchApply`, batch lifecycle operations and
administration stay explicit CLI paths. Delete and sweep are denied by the
Beadhive session surface despite being present in the complete generated wire
SDK. Reads and writes never silently fall back from HTTP to CLI.

The v1.3 OpenAPI and live service expose no focused gate operation. Gate rows
are ordinary issues on reads, but `createIssue` cannot set `await_type` or
atomically reproduce `bd gate create --blocks`; no HTTP route implements gate
resolution. Treating generic issue close as gate resolution would be an
unproved semantic substitution. `work.gate.*`, `plan.gate.*`, approval, bounce,
and kickoff transitions therefore remain explicit CLI compatibility. This is a
downstream blocker for `bh-bwnys.1`'s desired HTTP gate mutation contract, not
an absent detail to emulate in the client.

Likewise, HTTP claim does not grant the renewable CLI lease and v1.3 has no
heartbeat, expired-lease reclaim, or merge-slot endpoints. The dedicated HTTP
release route is API-ready for ownership-fenced claim release, while the wider
lease release convention remains CLI compatibility. Generated batch-create,
batch-apply, and batch-close routes remain compatibility operations until their
atomic and partial-failure behavior has real-service evidence.
