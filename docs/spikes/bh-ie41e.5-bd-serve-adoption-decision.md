# `bh-ie41e.5`: `bd serve` adoption decision

> **Partially superseded 2026-09-25.** The closed read allowlist, mandatory CLI mutation path,
> journal-first rollout, and local-only topology are replaced by
> [the API-first parallel-replacement ADR](../design/beads-api-first-parallel-replacement-adr.md).
> The exact contract digest, identity/security binding, typed failures, destructive-operation
> denial, no blind replay of ambiguous writes, and separate Factory activation gates remain valid.

## Decision

**NARROW GO for product architecture; no Factory operational GO.** Beadhive should implement a
private, contract-pinned, read-first `bd serve` adapter and a conservative baseline-plus-journal
accelerator behind its existing issue-data and `StateStreamProvider` seams. The service becomes
the preferred transport only for the allowlisted operations below after negotiation and parity
checks succeed. The current CLI remains the compatibility path, the authoritative full-state
baseline, and the only path for every lifecycle composite, mutation, coordination operation, and
administrative operation not explicitly allowlisted.

This decision does not enable the adapter by default, deploy a service, or approve Factory use.
The operator resolved gate `bh-552qd` on 2026-09-20 as a waiver allowing the product/API decision
to proceed without fresh Wave 3 measurements and without the missing cross-hive verdict. That is
not evidence from Factory. At decision time `bh-infra-920k.1`, `.2`, `.3`, and `.4` are open, no
committed `bh-infra-920k` artifact exists, and the Factory operations verdict has not been
rendered. Factory default-on activation remains blocked on that molecule proving topology and
resource ceilings, lifecycle supervision and security, canary fault recovery and rollback, then
recording its own reviewed GO.

The priority order is binding:

1. exact API/schema compatibility and unchanged lifecycle semantics;
2. narrow payload and downstream token reduction;
3. iteration speed, process/round-trip reduction, and only then separately proven atomic writes.

Historical subprocess evidence and disposable payload fixtures justify implementation work, not
a fleet-performance claim. No current HTTP latency, CPU, RSS, connection, or Factory recovery
measurement was taken for this decision.

## Evidence and limits

The decision consumes the four committed artifacts:

- `bh-ie41e.1-bd-serve-capability-map.md`: 143 adapter call sites; 49 have no native v0
  capability and at least 21 more are partial or dynamic. It establishes a real read surface and
  rejects wholesale replacement.
- `bh-ie41e.2-bd-serve-adapter.md`: exact v1.3.0 source contract digest, typed projections and
  failures, project binding, cursor behavior, a bounded stopped-service deadline, and the rule
  that an indeterminate mutation is never replayed through another transport.
- `bh-ie41e.3-journal-state-stream.md`: the baseline-before-head replay algorithm, crash-safe
  checkpoint shape, complete rebaseline matrix, and compatibility with the existing public
  snapshot/delta/epoch contracts.
- `bh-ie41e.4-bd-serve-benchmark.md`: bounded synthesis after measurements were waived. Historical
  snapshots show 57 warm/86 cold and an earlier 93 warm `bd` invocations; disposable fixtures
  show 98.6% full-to-brief and 93.7% export-to-event byte reductions. Those are historical and
  directional fixtures, not measured live HTTP or model-token results.

The exact v1.3.0 OpenAPI source is 497,361 bytes with SHA-256
`9a33a349e5266bdba916246594a4fc457c63e39d7affb5acec9bd5b75acde4dd`; the installed
release returned typed 404 at its advertised `/v0` discovery route. The implementation must
vendor or otherwise reproducibly pin that exact document. It must never generate a client from
runtime inference, a nearby tag, the issue-data schema, or capability names alone.

## Exact operation allowlist

The first implementation molecule may route only these v1.3.0 operations over HTTP:

| Logical use | Required capability and route | Conditions |
|---|---|---|
| issue detail/guard read | `issues.get`: `GET /v0/beads/issues/{id}` | Full typed detail including opaque decimal-string revision; parity fixture required before activation. |
| bounded issue rows | `issues.list`: `GET /v0/beads/issues` | Separate summary type; `brief=true` only for consumers that do not need omitted free-form fields; exhaust cursors for complete reads. |
| narrow filtered rows/counts | `issues.query` / `issues.count`: `POST /v0/beads/issues:query`, `POST /v0/beads/issues:count` | Only when the consumer's required projection and ordering are fixture-proven; never substitute a count/page for a complete snapshot. |
| ready rows/count | `ready.list` / `ready.count`: `GET /v0/beads/ready`, `GET /v0/beads/ready:count` | Preserve gated semantics, ordering, filters, budgets, and cursor exhaustion. |
| dependency reads | `dependencies.list`, `.blocking`, `.count`, `.tree`, `.cycles`: their named `GET` routes | Exact edge kind, direction, metadata, ordering, and completeness must match the CLI consumer. |
| journal catch-up | `events.list`: `GET /v0/beads/events?since=N&limit=L` | Private accelerator only, after an authoritative baseline; page until last sequence equals returned head. |
| optional low-fanout follow | `events.watch`: `GET /v0/beads/events:watch` | Opt-in only where a held connection is justified; polling is the default fleet shape; 48-stream server cap is not a capacity approval. |

This is a closed allowlist. In particular, `config.*`, `stats.get`, `issues.related`, memories,
all issue/dependency mutations, and every destructive operation are not adopted merely because
the server advertises them. `issues.claim`, `issues.claimNext`, guarded `issues.update`,
`issues.create`, `issues.batchApply`, `issues.casMetadata`, dependency writes, close/reopen, and
comments are later candidates only through independent lifecycle beads that prove hooks, actor,
event, revision/CAS, idempotency, reconciliation, and composite-boundary parity.

The private client must deny `issues.delete` and `issues.sweep`. `bd compact`, `bd flatten`, raw
Dolt push, and Dolt GC remain prohibited operationally, not hidden fallbacks.

## Negotiation, topology, and security boundary

For every endpoint, the client performs this sequence before an allowlisted operation:

1. verify `GET /healthz`, then authenticate and fetch `/v0/beads/context`;
2. bind API `v0`, Beads version/commit lineage, HTTP schema version, backend, repo root, beads
   directory, database, project ID, endpoint, and required capabilities to one expected hive;
3. send `Bd-Project-Id` on every operation and prove database readiness with
   `GET /v0/beads/ready?limit=1`;
4. reject identity, auth, project, Host, schema, or capability drift rather than falling through
   to a less constrained transport.

The product topology is one supervised service endpoint bound to exactly one local workspace and
one hive context. It listens only on loopback, is never load balanced, never shared across hive
identities, and is never started per request or per agent. Transparent clone/replica failover and
remote service access are prohibited because v1.3.0 exposes no clone, branch, journal, or server
epoch. The adapter must assume endpoint replacement can change continuity domains.

The Beadhive host runtime is the product-level owner of endpoint discovery, process handle,
readiness, context cache, deadlines, graceful `SIGTERM`, telemetry, and fallback selection for a
locally enabled service. Callers and browser/Gateway clients never own or address `bd serve`.
The Factory lifecycle owner is deliberately **not selected here**: `bh-infra-920k.2` must choose
and prove direct systemd ownership versus host-daemon child ownership before Factory activation.
Whichever it selects must preserve the one-workspace binding and expose a single supervised
contract to the product adapter.

The network/auth boundary is loopback plus bearer authentication, strict Host checking, and the
negotiated project ID. The host daemon derives `TrustedActor` from its trusted seat and injects
it; an untrusted downstream request cannot assert actor provenance. The raw service receives no
Gateway, Cloudflare Tunnel, browser, or other public route. Tokens are read from least-privilege
service-owned storage and never logged. Factory token-file mode, rotation/revocation, executable
pin, unit permissions, log redaction, alerts, and proof that no public route exists remain gates
owned by `bh-infra-920k.2` and `.3`.

## Fallback and failure matrix

| Observation | Read action | Mutation/action boundary |
|---|---|---|
| Service disabled by configuration | Use the existing pinned CLI path. | All writes remain CLI in the initial rollout. |
| Pre-dispatch connect refusal or missing allowlisted capability | Bounded retry, then the operation's explicit CLI read fallback. | A later adopted write may choose CLI only before any request was dispatched. |
| Mid-response reset or read timeout | Retry the idempotent read within budget, then explicit CLI read fallback. | Any possibly dispatched write is indeterminate; reconcile, never blind-replay. |
| `503 busy` / watch saturation | Honor `Retry-After`; resume journal polling from the durable checkpoint; use bounded read fallback when policy permits. | No generic write fallback. |
| `503 db_unavailable` | Surface the shared database outage after bounded retry; CLI is not presumed to recover the same database. | No replay. |
| `400 invalid_cursor` | Restart the identical immutable query without the cursor, within its row/page budget. | Not applicable. |
| Optional read optimization rejected as unknown parameter | Remove only that understood optional optimization or use its explicit CLI read path. | A write fails compatibility; never weaken its schema. |
| Other `400`, `401`, Host/project/context mismatch | Fail closed as client, auth, or security/configuration error. | No fallback. |
| Domain `404` or known `409` | Return typed absence/refusal; stale revision requires re-read and re-decision. | No fallback or stale-body retry. |
| Route `404` despite advertised capability | Quarantine the client/server pair as a contract violation. | No automatic replay. |
| Unknown `4xx` / `5xx` | Fail contract or retry boundedly by class; emit request ID and transport telemetry. | Conservatively indeterminate when dispatch/commit cannot be excluded. |
| Projection/parity check fails before publication | Keep the old published state and use the proven CLI read path. | Operation remains unadopted. |
| Journal disabled/newly enabled, continuity uncertain, or rebaseline trigger fires | Stop journal deltas and rebuild from full CLI export plus independent gate read. | Gates/state/lifecycle continue on CLI. |

There is no broad “HTTP failed, run `bd`” handler. Typed domain refusals are final. A read fallback
records operation, hive/context, transport, capability, page/byte counts, reason, request ID, and
deadline. A future write result is one of success, typed final refusal, or indeterminate and must
carry enough evidence for operation-specific reconciliation.

Lifecycle composites use one semantic transport for the whole critical section. Gate plus state,
claim plus worktree provisioning, release/abandon, merge-slot ownership, close plus metadata,
planning/file/import, grouped submit/merge, sync, backup, and migration remain complete CLI/admin
composites. They must not be split merely to reduce subprocesses.

## Baseline-plus-journal state algorithm

The journal is an accelerator over authoritative state, never the authoritative public log:

1. **NEGOTIATE:** bind the exact context and require `events.list` (and `events.watch` only if
   selected). A disabled journal or incompatible service enters the current polling fallback.
2. **HEAD_BEFORE:** read events with `since=MaxInt64&limit=1` and retain head `H0`.
3. **BASELINE:** run the existing full CLI export and independent full gate read. Normalize only
   a complete result; a partial result leaves the previous published state intact.
4. **CATCH_UP:** page records strictly after `H0` in ascending contiguous sequence until the last
   applied sequence equals each returned head; re-read gates through their existing path.
5. **INSTALL:** atomically persist normalized issue/edge mirror, last sequence and head, complete
   bound context and endpoint, adapter format version, digest, and separately held gate revision.
   Write/fsync a temporary bundle, rename atomically, then publish the in-memory revision.
6. **FOLLOW:** poll by default (watch only when explicitly budgeted), apply records idempotently,
   persist mirror and checkpoint together, then publish. Keep polling non-journaled gates.
7. **RECONCILE:** periodically obtain an authoritative full baseline. A digest mismatch emits
   telemetry, rebaselines, and disables acceleration after a bounded repeated-mismatch threshold.

Replay replaces issue snapshots for create/update/close, removes issue plus held edges on delete,
upserts dependency triples plus metadata on `dep_add`, removes the exact edge on `dep_remove`, and
does not change the current public state projection for comments. Duplicate sequences at or below
the durable checkpoint are discarded; reapplying a batch is safe. The checkpoint is never
advanced ahead of the durable mirror.

Every one of these observations forces a full rebaseline and a new pre-baseline head:

- first enablement, newly enabled journal after 409, missing/corrupt/incomplete checkpoint;
- 410 truncation, an in-stream truncation signal, interior hole, non-contiguous record, unknown
  operation/payload, decreasing head, or saved checkpoint greater than new head;
- endpoint URL, project/database/repo/beads directory, API/version/schema, capabilities, or other
  bound context change;
- known/suspected service, clone, replica, or active branch replacement, including same-URL
  failover, because v1.3.0 has no stable journal epoch;
- successful `bd sync`, Dolt pull/merge, federation merge/conflict resolution, raw SQL/DML,
  restore/apply, compaction, or another write that bypasses the journal;
- disabled-journal write interval, checkpoint/mirror digest failure, or authoritative periodic
  reconciliation mismatch.

An ordinary duplicate, same-context TCP reconnect, watch drop, saturation, busy response, or
transport timeout does not alone force rebaseline; resume by polling from the durable checkpoint.
The endpoint-replacement rule wins whenever clone continuity is uncertain. Missing legacy audit
table `events` is a hard write-dead schema fault, not a journal gap; readiness needs a physical
schema preflight or upstream repair and must not report that rebaseline repaired it.

Beads sequence stays private. `bh stream` still begins with a canonical snapshot and preserves
opaque revisions and full-replacement deltas. The host daemon still owns `producerEpoch`, public
sequence/base sequence, SSE IDs, retention, reset, and resync. Rebaseline rotates the public
producer epoch, emits reset sequence 1, installs an authoritative snapshot, then resumes deltas.

## Rollout, measurement gates, and rollback

1. **Contract:** vendor and digest-pin the exact OpenAPI; generate/validate distinct context,
   problem, summary, detail, page, dependency, event, and result types. Add CLI/HTTP parity
   fixtures before routing any consumer.
2. **Narrow reads:** implement negotiation, deadlines, telemetry, and default-off shadow reads for
   `issues.get`, bounded list/query/count, ready, and dependency reads. Compare normalized results
   against CLI without changing callers.
3. **Payload:** expose partial summaries explicitly and route only consumers proven not to require
   omitted fields. Measure serialized canonical bytes and downstream token-bearing payloads; do
   not project synthetic fixture percentages onto production traffic.
4. **State acceleration:** add the crash-safe mirror/checkpoint and deterministic replay/rebaseline
   tests, then polling transport; watch is a later opt-in under a connection budget.
5. **Local opt-in:** enable on disposable/local single-workspace hives with immediate CLI read
   fallback and periodic reconciliation. Keep public wire behavior unchanged.
6. **Implementation benchmark:** before any default-on product rollout, require at least 30% lower
   median latency for the adopted read mix, p95 no worse than subprocess, bounded recovery, and a
   narrow projection/event reduction of at least 50% for its consumer.
7. **Factory gate:** `bh-infra-920k.1-.4` must additionally prove the selected bounded topology,
   idle service RSS at most 100 MiB per hive, at most two steady Dolt connections per idle
   service, no unbounded restart/recovery growth, lifecycle/security/observability, and one-hive
   fault canary/rollback. Only its reviewed GO may make Factory default-on.
8. **Writes later:** file separate lifecycle changes for an atomic endpoint only when it improves
   correctness and passes full hook/actor/event/CAS/idempotency/reconciliation parity. Transport
   speed alone is insufficient.

Rollback is configuration-only: stop routing allowlisted reads to HTTP, terminate the supervised
service gracefully, discard only the private adapter checkpoint if invalid, and continue through
the existing pinned CLI/export/gate path without data rollback or public cursor changes. A failed
service rollout must not require undoing authoritative Beads data. Factory rollback details are
still evidence owed by `bh-infra-920k.3`.

## Compatibility promise

- Existing CLI commands, lifecycle results, hooks, actor semantics, error/redaction behavior,
  ordering, complete-read semantics, and administrative recovery remain supported.
- Existing `bh stream`, host snapshot/SSE, Gateway, and browser contracts do not expose HTTP
  routes, Beads records, capabilities, request IDs, or journal sequences.
- Default configuration remains CLI/polling until its staged gates pass. A missing or older
  service is an explicit compatibility mode for allowlisted reads, not a degraded mutation mode.
- No caller may observe a partial list as complete, a brief row as full, a wrong-hive fallback,
  an invented empty state, or a stale journal as caught up.
- HTTP mutation support is not implied by this GO. Unsupported and administrative operations
  remain pinned CLI/admin behavior for the lifetime of this compatibility promise.

## Replan and existing-work inventory

Invoke `bh:replan` on existing epic `bh-97fo0` and file the named implementation molecule
**“Supervised typed `bd serve` read transport and journal accelerator.”** Link it to
`bh-ie41e.1-.5` and decompose it in the rollout order above: pinned contract/models; negotiation
and failure algebra; narrow read parity and telemetry; crash-safe baseline/journal mirror;
supervisor seam and compatibility fallback; shadow/local opt-in; benchmark and activation
decision; immutable release/handoff.

Exact changes to existing beads:

| Existing bead | Required replan disposition |
|---|---|
| `bh-97fo0` — Make host-daemon snapshot runtime self-contained | Rewrite the CLI-only design into the named dual-transport implementation molecule; the pinned CLI remains mandatory fallback and baseline. |
| `bh-97fo0.1` — Define explicit bd executable contract for host-daemon snapshots | Rewrite, not close: own the pinned OpenAPI/client/context contract **and** preserve explicit validated `bd` executable configuration for fallback/admin paths. Its CLI-only premise is superseded. |
| `bh-97fo0.2` — Update hardened systemd deployment for snapshot runtime dependencies | Rewrite, not close: define the generic supervised endpoint seam and local opt-in hardening, but do not select or ship a Factory owner before `bh-infra-920k.2/.4`. Preserve least privilege and rollback. |
| `bh-97fo0.3` — Add clean-service snapshot integration coverage | Rewrite, not close: cover HTTP negotiation/parity/faults, CLI read fallback, full export/gate baseline, journal replay/rebaseline, and sanitized service conditions. |
| `bh-97fo0.4` — Release host-daemon runtime fix and publish infra handoff | Preserve and retarget to the reviewed dual-transport release; do not obsolete its immutable release or infra handoff duty. |
| `bh-infra-920k.1-.4` | Do not supersede or rewrite from this hive. They remain the required Factory topology/capacity, supervision/security, canary, and operational-decision gates. |
| `bh-infra-wib3` — Converge and certify the Development live Gateway runtime | Preserve its pinned CLI/admin fallback and live Gateway scope. Replan it only after `bh-infra-920k.4` records GO; service transport is then an internal host detail, never a new public route. |
| `bh-infra-64ql.5` — Certify live-only `/demo` and fixture/static `/tour` separation | Preserve acceptance. Its receipt records CLI compatibility versus an approved adapter, but raw `bd serve` is neither required nor exposed. Any exact infra rewrite remains owned by `bh-infra-920k.4`. |

No current bead is made wholly obsolete. What is superseded is the CLI-only architectural premise
of `bh-97fo0.1-.3`, wholesale HTTP replacement, runtime-generated clients, transparent replica
failover, and any assumption that resolving `bh-552qd` constituted a Factory verdict. The
historical process-count work remains useful as a baseline; the unmeasured latency/resource
thresholds remain rollout gates rather than results.
