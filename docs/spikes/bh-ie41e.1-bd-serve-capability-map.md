# `bh-ie41e.1`: `bd serve` capability map

## Question

Can Beadhive replace the `bd` subprocesses used by state streaming, aggregation, work,
planning, gates, sync, backup, and migration with Beads 1.3.0's OpenAPI v0 service without
changing lifecycle semantics? Where it cannot, what is the explicit CLI/admin fallback?

## Method

This is an inventory spike; it changes no product code. I inspected the production Python call
sites on revision `ab0bc276efd213f84f7d9394c0be13f4ecc87a1d` (the certified toolchain's parent
revision), following calls through `bd.run`, `bd.json`, `bd.show`, `bd.state`, `bd.children`, the
`Engine` stream methods, and the direct administrative runners. Tests and documentation examples
were excluded. The syntactic inventory covers the 36 top-level modules whose names begin with
`state`, `work`, or `plan`, plus `coordination.py`, `sync_remote.py`, `backup.py`,
`storage_migrate.py`, and `engine.py`.

I compared those calls with a loopback `bd serve` from the certified binary, Beads 1.3.0
`f45b249ce6b40ba62aecc03949e6371e8f7c79d8`, schema v66, against database `bh`. The source of
truth for availability was `GET /v0/beads/context`, not an assumed endpoint. A probe for the
help-advertised OpenAPI document at `/v0` returned typed 404; consequently endpoint names below
are the v0 routes exercised or embedded in this exact binary, while capability names are copied
verbatim from context. This missing discoverable document is itself a negotiation constraint.

## Evidence

### Certified service surface

`GET /v0/beads/context` reported API `v0`, Beads `1.3.0`, backend `dolt`, schema-version field
`1`, and these 40 capabilities:

```text
config.get config.list config.set config.unset
dependencies.add dependencies.blocking dependencies.count dependencies.cycles
dependencies.list dependencies.remove dependencies.tree
events.list events.watch
issues.addComment issues.batchApply issues.batchClose issues.batchCreate
issues.casMetadata issues.claim issues.claimNext issues.close issues.count issues.create
issues.delete issues.get issues.list issues.query issues.related issues.release issues.reopen
issues.sweep issues.update
memories.forget memories.get memories.list memories.remember
project.enforce ready.count ready.list stats.get
```

The relevant routes in this build are:

| Capability | v0 operation |
|---|---|
| `issues.get` | `GET /v0/beads/issues/{id}` |
| `issues.list` | `GET /v0/beads/issues` |
| `issues.query` / `issues.count` | `POST /v0/beads/issues:query`; `POST /v0/beads/issues:count` |
| `issues.create` / `issues.batchCreate` | `POST /v0/beads/issues`; `POST /v0/beads/issues:batchCreate` |
| `issues.update` / `issues.batchApply` | `PATCH /v0/beads/issues/{id}`; `POST /v0/beads/issues:batchApply` |
| `issues.claim` / `issues.claimNext` / `issues.release` | `POST /v0/beads/issues/{id}:claim`; `POST /v0/beads/issues:claimNext`; `POST /v0/beads/issues/{id}:release` |
| `issues.close` / `issues.batchClose` / `issues.reopen` | `POST /v0/beads/issues/{id}:close`; `POST /v0/beads/issues:batchClose`; `POST /v0/beads/issues/{id}:reopen` |
| `issues.addComment` | `POST /v0/beads/issues/{id}/comments` |
| dependency capabilities | `GET /v0/beads/dependencies`, `POST /v0/beads/dependencies:add`, `POST /v0/beads/dependencies:remove`, and the named `/blocking`, `/tree`, `/cycles`, `/count` routes |
| `ready.list` / `ready.count` | `GET /v0/beads/ready`; `GET /v0/beads/ready:count` |
| config capabilities | `GET /v0/beads/config`, and `GET`/`PUT`/`DELETE /v0/beads/config/{key}` |
| events capabilities | `GET /v0/beads/events`; `GET /v0/beads/events:watch` |
| `stats.get` | `GET /v0/beads/stats` |

The server's own help establishes four semantic differences that matter to Beadhive: HTTP
mutations do **not** run hooks; actor is caller-asserted provenance rather than authenticated
identity; durability is per-request rather than CLI per-command auto-commit; and the advertised
capability set describes the build, not launch flags. The service refuses `--readonly`, so a
successfully started server always advertises mutation capability.

### Reconciled call-site inventory

The AST inventory found **143 Beads-adapter call sites**. The following rows are exhaustive and
sum to 143. A row is a syntactic production call site, not runtime frequency; shared helpers can
serve many commands.

| CLI/helper family | Sites | Exact API mapping | Decision |
|---|---:|---|---|
| `show` | 42 | `issues.get` | HTTP read candidate; verify full projection, especially dependencies, labels, metadata, comments and revision. |
| `state` | 13 | none | retain CLI `bd state`; no state-dimension capability. |
| `gate …` | 13 | none | retain CLI; create/list/resolve and typed blocking-gate semantics are absent. |
| `set-state` | 11 | none | retain CLI; must remain paired with gate/lifecycle updates. |
| `close` | 8 | `issues.close` / `issues.batchClose` | candidate only after actor, hook, reason/event, and idempotency parity tests. |
| `children` | 8 | partial: `issues.list` plus dependency projection | keep CLI initially; `bd children`'s parent-edge, closed/include-infra, unbounded semantics need a parity adapter. |
| `update` | 7 | `issues.update`; claim-shaped sites map to `issues.claim` | mutation fallback policy applies; atomic label/assignee/status and revision behavior must match. |
| `merge-slot …` | 6 | none | retain CLI; distributed acquire/check/release is a coordination primitive, not an issue patch. |
| dynamic argument vectors | 5 | mixed | retain CLI until the caller identifies and negotiates the concrete operation. These are work read forwarding and lifecycle helper vectors. |
| `ready` | 5 | `ready.list` | HTTP read candidate; preserve `--gated`, limit-zero, ordering, and pagination semantics locally. |
| `swarm …` | 5 | none | retain CLI; create/list/status contract absent. |
| `list` | 4 | `issues.list` | HTTP read candidate; client must exhaust cursors where CLI uses `--limit 0`. |
| `dep add/remove` | 4 | `dependencies.add` / `dependencies.remove` | mutation fallback policy applies; preserve edge type and error/idempotency behavior. |
| `label add/remove` | 4 | partial: `issues.update` / `issues.batchApply` | retain CLI until atomic set-vs-add/remove and expected-version semantics are proven. |
| `reopen` | 2 | `issues.reopen` | mutation fallback policy applies. |
| `child_rows` | 1 | partial: `issues.list` plus dependency projection | same fallback as `children`. |
| `note` | 1 | `issues.addComment` | mutation fallback policy applies; confirm `note` event/projection equivalence. |
| `assign` | 1 | partial: `issues.update` | retain CLI until assignment event, actor, hook, and conflict behavior match. |
| `comments` | 1 | partial: `issues.get` | retain CLI if the issue projection omits or truncates comment history. |
| `create` | 1 | `issues.create` | mutation fallback policy applies; planning also needs returned ID and label/dependency parity. |
| `import` | 1 | none | retain CLI; JSONL import is intentionally administrative. |
| **Total** | **143** | | |

The direct administrative seams add these operation families; they are deliberately counted by
family rather than added to the 143 adapter-site total, because `_bd(args)` is a dynamic runner:

| Area | Discovered direct operations | API result |
|---|---|---|
| state streaming / aggregation | `export -o`; `gate list --all --limit 0 --json` | no export or gate capability; retain CLI. The eventual event stream may replace polling, but it does not reproduce the current full snapshot by itself. |
| sync | filtered `list`; Dolt status/remote/commit/push/pull through the engine | list can use `issues.list`; all Dolt publication remains CLI/admin. |
| backup | `backup remove`, `backup init`, `backup sync` | unsupported; retain CLI/admin. |
| migration | `config get/set`, `init --reinit-local`, `bootstrap`, `dolt start`, plus verification reads | config has exact API operations, but migration is a quiesced administrative transaction and remains entirely CLI/admin; do not split it across transports. |

Thus the unsupported inventory reconciles as **49 of 143 adapter sites with no native
capability** (`state` 13 + gates 13 + `set-state` 11 + merge-slot 6 + swarm 5 + import 1), plus
the direct export, Dolt sync/publication, backup, initialization/bootstrap/server-management,
and schema-migration families. Another **21 of 143 are only partial/dynamic mappings**
(`children` 8 + update 7 + dynamic 5 + child_rows 1); label, assignment, and comments are called
out separately in the matrix because even apparently representable fields need semantic parity.

### Capability negotiation rules

1. On connection, require a successful authenticated (when configured) `GET /healthz`, then a
   successful `GET /v0/beads/ready?limit=1`; liveness alone is not readiness.
2. Fetch `/v0/beads/context` and bind the connection to the expected repo root, beads directory,
   database, backend, API version `v0`, and an allowlisted Beads version/schema lineage. A server
   for the wrong hive is a hard failure, never a fallback candidate.
3. Negotiate **per logical operation**, using the advertised capability string. Never infer one
   operation from a neighboring capability (`issues.update` does not imply atomic label-add,
   gates, state dimensions, or assignment semantics).
4. Reads may fall back to CLI when the service is unreachable, returns `503`, lacks a required
   capability, or fails projection-parity validation. Typed `4xx` domain errors do not fall back:
   replaying through CLI could turn a real not-found/conflict into a different result.
5. Cursor exhaustion is mandatory for every former `--limit 0` read. The adapter must rebuild
   the exact current projections and ordering rather than expose a page as a complete fleet.
6. Cache negotiated context by process/address, invalidate on reconnect or version/context
   change, and emit transport/capability telemetry so benchmark results distinguish HTTP from
   fallback subprocesses.
7. Absence of a discoverable OpenAPI document is a release gate for generated clients. Until
   the exact document is vendored or made retrievable, use a small private hand-written client
   for explicitly adopted operations only.

### Current Beadhive versus v1.3 adoption candidates

The ranking below is deliberately contract-first. Avoiding process startup is useful only after
the wire schema and lifecycle result are equivalent; an endpoint that is faster but silently
drops a field, hook, actor, event, or conflict is not an optimization.

#### 1. API and schema compatibility

The first deliverable should be a checked-in, exact v0 contract (or a reliably retrievable one)
and generated/validated request and response models. Contract tests must compare CLI JSON with
HTTP for full issues, list pages, ready rows, dependency edges, errors, revisions, metadata,
labels, comments, events, and pagination. `/context`'s `schema_version: 1` is the **HTTP contract
schema**, not Dolt schema v66; both must be bound independently. This work blocks every candidate
below and is more important than its speed ranking.

#### 2. Token and payload reduction

Prefer projections that avoid repeatedly sending full issue objects to the host daemon and then
to an agent. `issues.query`/`issues.count`, `ready.count`, dependency count/blocking operations,
and cursor-bounded `issues.list` can replace full exports when the consumer needs only a count,
ID set, or changed page. The adapter should expose typed narrow results, not reproduce CLI prose.
This reduces serialized bytes and downstream model tokens; it does **not** by itself remove a
coordination race. Full export remains the recovery/reconciliation baseline until the events
study proves journal continuity.

#### 3. Iteration speed and atomic operations

Counts below are the number of `bd` subprocesses at the named production seam in the ordinary
successful path, excluding Git, validation, and publication subprocesses. “Variable” means the
code fans out by bead/gate/member. Counts are intentionally conservative; shared guards can add
reads at higher layers. An HTTP call still has one loopback round trip, but avoids a fresh Go
process and database/client setup.

| Ranked Beadhive operation | Current `bd` subprocess / round-trip count | Candidate HTTP operation | Explicit fallback | Effect |
|---|---:|---|---|---|
| state-stream hive refresh | 2 per hive (`export`, `gate list`) | paginated `issues.list` plus `events.watch`; no gate endpoint | keep both CLI calls until journal continuity **and** a gate contract exist | Mostly startup/payload reduction; events can remove polling delay, not gate races. |
| `work issue` / repeated guards | 1 `show` each | `issues.get` | CLI `show` on pre-request connection/capability failure | Startup and payload reduction only. |
| ready/read scheduling | 1 `ready` per query, with additional child/show reads depending on plan | `ready.list`, narrow counts, then dependency reads | CLI `ready`/`children` | Startup and payload reduction only unless `claimNext` is adopted. |
| `work claim` | 3 at assignment seam (`show`, `update --claim`, confirm `show`) | **one** `issues.claim` with expected version; eventually `issues.claimNext` for choose-and-claim | retain the complete CLI claim composite; never replay an indeterminate HTTP mutation | `claim` can remove the read/write race if the API enforces revision; `claimNext` can atomically remove the scheduler choose/claim race. Hooks and worktree provisioning remain outside HTTP. |
| release/abandon claim | 3 (`set-state`, `update` release fields, residue `show`) | `issues.release` with an ownership/revision guard, plus a separate state contract | retain the complete CLI composite | A guarded release removes lost-owner races; without state support this is not yet adoptable as a composite. |
| assign | 2 at assignment seam (`show`, `assign`) | conditional `issues.update` | CLI `assign` | Expected-version update could remove stale-assignee races; an unconditional patch saves startup only. |
| add/remove dependency | 1 mutation, normally surrounded by caller reads | `dependencies.add` / `dependencies.remove` | CLI `dep` | Atomic endpoint preserves one-edge mutation and may remove client read/patch races; verify type/idempotency. |
| label add/remove | 1 per label; batch/group paths fan out by member | `issues.batchApply` with expected versions | CLI `label add/remove` | Batch apply reduces N startups; only server-side per-item revision checks remove lost-update races. |
| close one / close group | 1 CLI invocation (possibly many IDs), plus guards | `issues.close` / `issues.batchClose` with expected versions | CLI close composite | Batch close is one atomic service unit only if its documented failure semantics are all-or-nothing; otherwise it is chiefly startup reduction. |
| update metadata after Git commit | 1 `update --set-metadata` | `issues.casMetadata` | CLI update | CAS removes the lost-update race and is preferable to generic update, not merely faster. |
| planning file/create | 1 create per record plus dependency/gate/state calls; import can batch records | `issues.batchCreate`, then `dependencies.add`; gates/state stay CLI | keep the whole planner filing transaction on CLI | Batch creation reduces startup, but mixed HTTP/CLI cannot make filing atomic and complicates rollback. |
| approval/bounce | variable gate resolves + state update | no gate/state operation | complete CLI composite | No adoption candidate yet; generic issue update would weaken the contract. |
| grouped submit/merge | member-count state/label reads and writes plus merge-slot and gates | `issues.batchApply`/`batchClose` cover only issue fields | complete CLI composite | Potential N-to-1 startup reduction later; no race is removed while merge-slot/gate/state remain absent. |
| backup, sync publication, migration | variable administrative command sequence | none (config endpoints are insufficient) | CLI/admin only | Neither hot-path nor safely splittable; no HTTP adoption. |

Expected-version support must be verified in the exact request schema. The capability list alone
does not promise it for `issues.update`, `batchApply`, or `batchClose`. Where ordinary update has
no revision precondition, retain CLI or redesign around `issues.casMetadata`; do not claim an
atomicity improvement from transport alone.

#### 4. Provenance

HTTP's `actor` is caller-asserted audit provenance, exactly as `--actor` is locally; bearer
authentication authorizes access but does not authenticate that actor identity. The host daemon
must derive actor from its trusted Beadhive seat and inject it, never accept an arbitrary actor
from an untrusted downstream request. Preserve request ID, Beadhive operation, hive/database,
seat, issue revision, resulting event/revision, transport, fallback reason, and indeterminate
outcome in telemetry. Hook suppression must be an explicit per-operation policy recorded beside
the actor; otherwise HTTP and CLI histories have the same name but different side effects.

#### Separate slice: federation correctness

Federation is not a hot-path speed candidate. Current federation status/list-peers/add-peer/sync
and Dolt publication are administrative CLI operations, and no advertised v0 capability maps to
them. Study them separately around authority, remote identity, epoch fencing, partial failure,
and replay/reconciliation. A future federation API should be adopted for stronger correctness
and observability, not counted as subprocess savings; until then all federation and publication
operations remain CLI/admin-only.

### Mutation fallback policy

The initial adapter is **read-preferred, CLI-write**. HTTP writes are opt-in per operation after
contract tests demonstrate all of: equivalent actor audit fields; equivalent reason/event
records; explicit expected-version or CAS behavior where Beadhive currently relies on a guarded
read-modify-write; complete response projection; retry/idempotency rules; and an explicit answer
for skipped CLI hooks. A missing capability, connection failure, timeout, or `5xx` **must not
automatically replay a mutation through CLI**, because the first request may have committed.
Return an indeterminate mutation result, reconcile by a fresh read/event lookup, and only retry
when an operation-specific idempotency proof says it is safe. Domain `4xx` responses are final.

Lifecycle composites keep one transport for the whole critical section. In particular gate +
state transitions, claim + worktree provisioning, merge-slot ownership, close + metadata, sync,
backup, and migration must not be half HTTP and half CLI. Destructive `issues.delete` and
`issues.sweep` are outside Beadhive's hot-path adoption and are denied by the client even when
advertised; `bd compact`, `bd flatten`, raw Dolt push, and Dolt GC remain prohibited operationally.

### Unsupported-operation inventory

- Coordination: every gate operation, state dimensions, swarm operations, and merge-slot
  create/check/acquire/release.
- Snapshot/interchange: export and import.
- Administration: Dolt status/remotes/commit/push/pull/start, initialization, bootstrap, schema
  migration, doctor/SQL verification, and backup init/remove/sync.
- Semantic gaps: CLI hooks; authenticated actor identity; atomic label add/remove and assignment;
  CLI-compatible parent/child and comments projections; expected-version coverage for ordinary
  issue updates; and a retrievable OpenAPI document.
- Deliberately unadopted destructive capabilities: issue delete and sweep.

## Verdict (GO)

**GO, narrowly, for a negotiated private client that replaces proven-equivalent reads first.**
The service has exact primitives for issue get/list, ready, dependency reads, config, and events,
so the next adapter and journal spikes have a real surface to evaluate. It is **NO-GO for a
wholesale subprocess replacement or for native Beadhive coordination today**: 49/143 adapter
sites have no capability, at least 21 more are partial/dynamic, and the lifecycle's gates,
state, swarms, and merge-slot contract is wholly absent.

## Recommendation

Build the next spike's adapter behind the existing `Engine` seam with context-bound capability
negotiation and telemetry. Start with `issues.get`, exhaustively paginated `issues.list`, and
`ready.list`; keep state-stream export/gate polling as the correctness baseline while the journal
spike evaluates `events.watch`. Keep every mutation and every administrative operation on the
CLI until per-operation parity tests satisfy the mutation policy above. Treat native coordination
and contract adoption as a later upstream/API design: first-class gate, state, swarm, merge-slot,
actor/authentication, hook-policy, expected-version, and projection contracts are prerequisites,
not behavior to emulate accidentally with generic issue patches.
