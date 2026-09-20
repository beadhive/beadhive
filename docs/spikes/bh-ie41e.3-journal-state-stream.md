# `bh-ie41e.3`: baseline-plus-journal state streaming

## Question

Can Beadhive use Beads 1.3.0's clone-local events journal to avoid a full issue export on every
state-stream refresh, while preserving Beadhive's public snapshot/delta/resync and
producer-epoch/cursor contracts? What must force a full rebaseline?

## Method

This is a research spike; it changes no product code. I inspected the installed Beads 1.3.0
binary and its exact source revision
`f45b249ce6b40ba62aecc03949e6371e8f7c79d8`, including the checked-in OpenAPI v0 document,
the journal read and truncation implementation, and the consistent-export path. I compared that
contract with the current `PollingStateStreamProvider` and the public Beadhive stream and host
daemon contracts.

I also used two disposable embedded-Dolt workspaces. The first exercised enable/disable,
ordinary updates, duplicate dependency adds, raw SQL, manual retention truncation, and payload
size. The second deliberately dropped the legacy audit `events` table to reproduce upstream
[#6142](https://github.com/gastownhall/beads/issues/6142). No managed hive database or product
code was changed.

## Evidence

### 1. Exact API and event schema

The adoption boundary is the exact v1.3.0 API/schema, not the similarity of two JSON objects.
`GET /v0/beads/context` must advertise both `events.list` and, if used, `events.watch`; the
response must remain bound to the expected `api_version`, `bd_version`, `project_id`, `database`,
backend, Dolt mode, repository root, and beads directory. The capability is build-level. An
enabled workspace is established only by a successful event read; a correctly implemented
server may advertise the capability and return `409 events_journal_disabled`.

The canonical record is the same `eventsjournal.Record` on CLI and HTTP. Its stable shape is:

| Field | Contract |
|---|---|
| `seq` | signed 64-bit integer, strictly increasing and gapless in commit order within one clone-local journal; `since` is exclusive |
| `ts` | UTC RFC 3339 timestamp; a label, **not** an ordering key |
| `op` | `create`, `update`, `close`, `delete`, `dep_add`, `dep_remove`, or `comment` |
| `issue_id` | canonical mutated issue id |
| `actor` | optional caller-asserted provenance; absence means system/unknown |
| `issue` | full post-mutation issue snapshot, always present; `null` for delete |
| `dep` | present only for dependency operations as `{kind,target,metadata}` |
| `comment` | present only for comments as `{id,author,text,created_at,source}` |

`GET /v0/beads/events?since=N&limit=L` returns `{head, records}` with a non-null list. `limit`
is 1–10,000, default 1,000. A page is complete only when its last `seq` equals `head`; page
length is not a completion signal. A checkpoint at or above `head` is a successful empty page.
The read returns `410 events_journal_truncated` with `since`, `floor`, and `head` for a missing
prefix or interior hole. The same typed problem is the sole in-band `truncated` event on the SSE
watch route. Disabled journals return 409, and watch saturation returns 503; neither is an empty
successful journal.

The journal tables, `bd_events_journal` and `bd_events_seq`, are `dolt_ignore` working-set
tables. They are not committed, pushed, pulled, or federated. A sequence belongs to one clone
and active branch, not to the logical project or database name. `/context` has no clone id,
branch id, journal epoch, or server-instance id. That omission is decisive: `project_id` plus
`database` cannot prove that a saved sequence still names the same journal.

### 2. Replay state machine

The journal is an accelerator over an authoritative full baseline, never the source from which
Beadhive invents a complete state. A safe adapter has these states:

| State | Action | Exit condition |
|---|---|---|
| `NEGOTIATE` | Read and bind `/context`; require the exact capabilities and allowlisted schema/version behavior. | Bound identity is acceptable; otherwise use the current polling adapter. |
| `HEAD_BEFORE` | Read the journal with `since = MaxInt64`, `limit = 1` and retain returned `head = H0`. | A successful enabled-journal response. 409 falls back; any identity uncertainty rebaselines. |
| `BASELINE` | Run the existing complete full export and the independent full gate read. Normalize the export into the canonical issue/dependency projection. | Complete baseline `B`; failures leave the previous published state intact. |
| `CATCH_UP` | Page records strictly after `H0`, applying them in ascending sequence until the last applied sequence equals the page `head`. Re-read gates through their current path. | State mirror and durable checkpoint are installed together. 410 or an unknown record forces `REBASELINE`. |
| `FOLLOW` | Poll `events.list`, or watch when reaction latency justifies a held connection. Apply, durably checkpoint, then publish. Continue polling non-journaled gates. | Normal steady state; transient 503/watch loss resumes from the last durable sequence. |
| `REBASELINE` | Stop publishing deltas from the old continuity domain. Discard the journal-derived mirror/checkpoint and return to `NEGOTIATE`. | A new authoritative baseline is installed. |
| `FALLBACK` | Use today's full-export plus gate-polling implementation. | Re-negotiate only on a deliberate configuration/service transition. |

`HEAD_BEFORE -> BASELINE -> CATCH_UP` closes the snapshot/journal race without requiring a new
atomic server endpoint. Proxied-server export runs under one read transaction; the classic
embedded/direct-server export uses several complete reads. The sandwich is safe for either:
every covered mutation and its journal record commit in the same write transaction, and replay
after the export overwrites any older or torn issue/edge observation. For any mutation `m`:

1. If `m` commits before `H0`, every later baseline read observes the committed transaction and
   replay starts after it.
2. If `m` commits after `H0` but before or during the baseline reads, replay contains it whether
   a particular baseline read observed its before-state or after-state. Applying the ordered
   full issue snapshot, delete, or dependency upsert after the baseline settles on the
   post-mutation state.
3. If `m` commits after the last baseline read, replay alone supplies it.

Therefore the installed state equals an authoritative state at the catch-up head. Taking the
head *after* the baseline would be wrong: a mutation between the snapshot and that head could
be absent from the baseline and skipped by replay.

Apply records as follows:

- `create`, `update`, and `close`: run the snapshot through the exact current export inclusion
  and normalization policy, then replace the issue by id or remove it if it is outside the
  public stream scope (infra, template, ephemeral, owner exclusion, or another configured cut);
- `delete`: remove the issue and all locally held edges whose endpoint is deleted;
- `dep_add`: upsert `(issue_id, target, kind)` and replace its metadata, then replace the source
  issue snapshot;
- `dep_remove`: remove that exact edge if present, then replace the source snapshot when non-null
  (a cascading delete can legitimately have no surviving source snapshot);
- `comment`: does not change the current public state-stream projection; retain or skip it only
  according to a separately typed comment consumer.

Dependency add is deliberately an upsert. The disposable probe added the same `blocks` edge
twice and received two consecutive `dep_add` records (seq 14 and 15) with the same edge and full
source snapshot. Treating the second as an error would reject documented behavior; applying it
as an upsert converges.

### 3. Checkpoint durability and duplicate delivery

The durable unit is not a naked sequence number. Persist one atomic checkpoint bundle containing:

- the normalized issue and dependency mirror, or a content-addressed reference to it;
- last fully applied journal `seq` and observed `head`;
- the complete negotiated context identity and server URL;
- an adapter format/schema version;
- a digest over the mirror and binding fields;
- the gate snapshot/revision kept separately, because gates are not journaled.

Write a new bundle to a temporary file, `fsync` it, atomically rename it, then publish the new
in-memory revision. Advance `seq` only after every record in the transaction is reflected in the
durable mirror. A crash before rename replays the batch; a crash after rename resumes after it.
At-least-once delivery is consequently harmless: replacement issue snapshots, deletes of an
absent id, edge upserts, and removal of an absent edge are idempotent.

The checkpoint must never be persisted ahead of its mirror. It must also never be reused when
its context binding is incomplete or changed. A corrupt/missing bundle is a rebaseline, not an
attempt to infer position from timestamps or the highest locally observed issue revision.

### 4. Rebaseline and fallback matrix

These are the complete continuity decisions for this adapter. “Rebaseline” means rebuild issues
and edges from a full export, retain the current separate gate read, and restart journal replay
from a newly sampled pre-baseline head.

| Observation or operation | Required action | Why |
|---|---|---|
| First enablement, or 409 followed by a newly enabled journal | **Rebaseline** | Enabling does not backfill the disabled interval. |
| 410 `events_journal_truncated`, including an in-stream `truncated` event | **Rebaseline** | Accepting `floor - 1` would knowingly keep a gap; Beadhive requires complete current state. |
| Interior journal hole | **Rebaseline** | The API exposes the intact prefix, but the final mirror still needs authoritative state. |
| Saved checkpoint is greater than a newly observed `head` | **Rebaseline** | Journal tables/counter were recreated or restored; reading would otherwise stall as “caught up.” |
| Server URL, `project_id`, database, repository root, beads directory, API/version contract, or negotiated capabilities change | **Rebaseline** | The checkpoint binding changed. |
| Known endpoint/service replacement can route to a different clone, or the configured single-workspace binding is lost | **Rebaseline** | v1.3.0 exposes no journal/server epoch. A same-URL replica swap can reuse or exceed the old sequence undetectably. |
| Clone/replica changes, fresh clone, restored journal tables, or replica failover | **Rebaseline** | Every clone owns an unrelated sequence space. Context cannot positively identify it. |
| Active Dolt branch changes | **Rebaseline** | Journal records are branch-local; context does not expose branch identity. |
| `bd sync`, `bd dolt pull`, Dolt merge, federation merge, or conflict resolution | **Rebaseline after success** | Merged data did not pass through the local mutation seam. |
| Raw SQL/DML (`bd sql` or direct Dolt SQL) | **Rebaseline after success** | It bypasses journal writes. The probe changed a title while seq stayed 15. |
| Compaction, restore/apply, or another documented bypass | **Rebaseline after success** | These rewrite authoritative state outside the journal vocabulary. |
| Journal disabled while writers continue | **Fallback and rebaseline before re-entry** | Writes in the disabled interval are permanently absent. |
| Unknown `op`, invalid payload, non-contiguous records, decreasing head, or checkpoint/mirror digest failure | **Rebaseline; disable acceleration until investigated if repeated** | Continuity or schema equivalence is not provable. |
| Full baseline/export or normalization is partial | **Keep old published state; retry/fallback** | Never install a partial authoritative mirror as a complete one. |
| Ordinary duplicate record after reconnect | Apply idempotently or discard when `seq <= checkpoint`; **no rebaseline** | At-least-once delivery is expected. |
| Watch drops, 503 `events_watch_saturated`, transient `busy`, or transport timeout at the same constrained endpoint/context | Resume by polling from the durable checkpoint; **no immediate rebaseline** | A TCP reconnect alone is normal. The topology must still guarantee one bound clone; the endpoint-replacement rule above wins otherwise. |
| A normal issue/dependency mutation through a journal-enabled Beads path | Incremental apply | Mutation and record commit atomically. |
| Gate, merge-slot, state-dimension, or swarm change | Continue the existing separate poll/read | Those operations have no journal event contract in v1.3.0. |

The adapter should additionally schedule a low-frequency authoritative reconciliation even when
no trigger fires. This is not permission to hide mismatches: a digest mismatch is telemetry,
forces a rebaseline, and disables acceleration after a bounded repeat threshold. It is the only
defense against bypasses performed outside Beadhive's command wrappers and same-identity replica
replacement that v1.3.0 cannot identify.

### 5. Missed-event and duplicate-event convergence

Let `S(t)` be the authoritative issue/edge state and `R(B, E)` be replay of ordered records `E`
over baseline `B` using the idempotent rules above.

- With no uncovered write, the three-way commit ordering proof in §2 gives
  `R(B, events(H0, Hcatchup]) = S(Hcatchup)`.
- Replaying any already-applied prefix again leaves the result unchanged, because each operation
  is a replacement, set deletion, edge upsert, or edge deletion. Thus
  `R(R(B, E), E) = R(B, E)`.
- If one covered event is genuinely unavailable, the next full rebaseline installs `S(t)`
  directly, regardless of the missing operation. The observed retention probe pruned seq 1–14;
  reading from 13 failed with `events_journal_truncated`, floor 15, head 16. Rebaseline is what
  converts that loud gap into a correct current state.
- A later event's full issue snapshot can incidentally heal missed issue fields, as observed when
  seq 16 carried the title and priority changed through raw SQL/disabled journaling. It cannot
  prove that a deleted issue or unjournaled dependency edit was healed, because issue snapshots
  do not inline dependencies and deleted rows emit nothing later. Therefore “wait for another
  event” is never a recovery strategy; rebaseline is.

### 6. Public epoch, cursor, and frame compatibility

The Beads journal checkpoint remains private adapter state. It must not replace either public
Beadhive cursor:

- `bh stream` continues to start every session with a full canonical `snapshot`, use opaque
  revisions, emit full-replacement `delta.changed` records, and use `resync` only mid-session.
  A revision may include an adapter-instance nonce and content digest, but consumers never see or
  parse the Beads sequence. Restart recovery may use the durable mirror internally without
  promising durable public replay, which remains outside the v1 stream contract.
- The host daemon continues to own `producerEpoch`, `sequence`, `baseSequence`, retention, and
  `<producerEpoch>:<sequence>` SSE ids. Beads `seq` is source metadata only. When any matrix row
  requires rebaseline or continuity cannot be proven, the relay rotates `producerEpoch`, emits
  `reset` as sequence 1, obtains an authoritative snapshot, and only then emits later deltas.
  Journal sequence must never be substituted for public sequence or compared with it.

The public normalized schema also stays curated. Journal `issue` is an input to the existing
normalizer, not a public pass-through; backend metadata, raw actor fields, comments, and Beads
event operation names remain private unless separately added to the Beadhive contract.

### 7. Observed #6142 behavior

Upstream #6142 concerns the legacy audit table named `events`, not the new
`bd_events_journal`. A workspace can have a valid schema-version marker and healthy reads while
the physical `events` table is absent. The first issue mutation then fails while trying to write
audit history.

The disposable Beads 1.3.0/schema-v66 probe reproduced the failure by dropping only `events`:

```text
bd create ... --json -> exit 1
failed to record event ... Error 1146: table not found: events
bd list             -> healthy; failed id absent (transaction rolled back)
bd migrate schema   -> exit 0, "Schema already at v66"
bd doctor           -> embedded-mode checks do not include this physical-table check
```

This does **not** create a missed journal event: the issue mutation rolls back too. It does mean
readiness and `/healthz`/read-only baselines can look green while the workspace is write-dead,
and neither rebaseline nor journal fallback repairs it. Adoption therefore needs a physical
schema preflight or upstream repair that verifies/recreates required tables; until then, a
mutation failure naming missing `events` is a hard operational fault, not an indeterminate event
gap. The shared production lineage's earlier canary did not reproduce #6142, but that fact does
not make a schema-version marker sufficient proof for other databases or fresh clones.

### 8. Ranked impact

#### 1. API and schema correctness

This is the gating impact. Vendor or generate against the exact OpenAPI v0 `EventRecord`,
`EventsPage`, and typed problem documents; bind context; keep Beads sequences private; implement
every matrix trigger; and add a stable replica/branch/journal epoch upstream before permitting
transparent service failover. Without those properties, the fast path can silently call a
different replica “caught up.”

The initial implementation should therefore be loopback/single-workspace only, with conservative
rebaseline on service replacement and a periodic full reconciliation. A load-balanced or remote
multi-replica service is **not** adoptable on v1.3.0's current identity contract.

#### 2. Payload and token reduction

On the 12-issue disposable fixture, a full JSONL export was 5,381 bytes while one issue-update
journal record was 341 bytes: **93.7% fewer bytes**, or 15.8× smaller. This is a directional
fixture, not a production benchmark; larger descriptions make the baseline grow with the whole
database while one event grows with one changed issue.

The reduction is primarily inside the adapter/database boundary. Beadhive's public delta already
contains only changed canonical entities, so this does not by itself promise a 93.7% reduction
in model-facing tokens. It removes repeated full-export parsing and lets downstream work stay
bounded to changed ids; any token claim must be measured on the final canonical frames, not
inferred from the raw journal bytes.

#### 3. Operations, round trips, and iteration speed

Today's hive refresh uses two `bd` subprocesses: a full export and a full gate list. After one
baseline, the steady fast path replaces the export subprocess with one bounded HTTP page or a
watch stream, but must retain the gate read. Thus a quiet refresh goes from **two CLI process
starts and one full issue payload** to **one CLI process start plus one small HTTP request**. It
does not make the whole refresh atomic, because gates and issue events still come from separate
contracts.

Catch-up may require multiple HTTP pages, and a full rebaseline costs the original export again.
Watch improves reaction latency rather than backlog throughput and is capped at 48 streams per
server; polling remains the default for fleet-scale consumers. The highest iteration-speed win
is avoiding an O(all issues) export on every quiet or one-issue refresh, not reducing correctness
checks or collapsing gate operations the API does not support.

## Verdict (NARROW GO)

Proceed with a private, negotiated baseline-plus-journal accelerator for a loopback,
single-workspace Beads service. Keep the existing full export as the authoritative baseline and
reconciliation path, keep gate polling, persist mirror plus checkpoint atomically, and implement
the full rebaseline matrix before enabling the fast path.

Do **not** treat the journal as a durable public Beadhive replay log, expose its sequence as a
public cursor, or enable transparent replica failover. Beads 1.3.0 lacks the journal epoch,
clone identity, and branch identity needed to prove those cases. Sync, merge, raw SQL,
restore/compaction, enablement gaps, truncation, service identity ambiguity, and counter reset all
return to a full baseline.

## Recommendation

Implement the adapter behind the current `StateStreamProvider` seam in three separately gated
steps: exact generated event/problem models and context binding; a crash-safe mirror/checkpoint
store plus deterministic replay tests; then the HTTP poll/watch transport with telemetry and
fallback. Test duplicate batches, every matrix trigger, checkpoint crash points, dependency
upserts, and baseline/event races against a real disposable Beads workspace. Ask upstream for a
stable `(clone, branch, journal epoch)` identity in `/context` before considering remote or
load-balanced deployment, and track #6142's physical-schema repair independently of the stream
optimization.
