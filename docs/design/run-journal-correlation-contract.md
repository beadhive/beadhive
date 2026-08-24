# Run-journal correlation contract

> **Status:** accepted · **Date:** 2026-08-24 · **Bead:** `bh-e8s3i.1`
>
> **Machine contract:**
> [`run-journal-v1.schema.json`](../schemas/run-journal-v1.schema.json) and
> [`run-journal-v1.example.jsonl`](../schemas/run-journal-v1.example.jsonl)
>
> This contract defines correlation and observation. It does not move process ownership, make
> the journal a lifecycle store, or implement the writers and readers. Those are downstream
> beads in `bh-e8s3i`.

## Decision

One **outer launch attempt** has one immutable `run_id` and one append-only JSONL journal. The
launch owner mints the id and attempts to create the journal before creating the process. A retry
is a new attempt with a new `run_id` and a new journal. Continuing a provider conversation still
creates a new outer run; its provider-native continuation is carried separately.

Every complete line is a standalone `beadhive.run-journal/v1` record. The required correlation
envelope is repeated on every line so a tail reader never needs an earlier header:

| field | contract |
|---|---|
| `version` | exactly `beadhive.run-journal/v1`; incompatible shapes get a new major contract |
| `source_revision` | unique, stable and opaque; consumers may compare or return it as a cursor, but never parse, sort, or manufacture it |
| `timestamp_ms` | integer Unix epoch milliseconds when the named writer observed the fact; file order, not wall-clock order, orders records |
| `run_id` | immutable outer attempt id, minted before spawn and never reused for a retry |
| `hive` | required exact registered hive identity; prefix inference is forbidden |
| `bead` | exact bead id or JSON `null`; an empty or guessed id is invalid |
| `driver` | resolved execution driver (`baml`, `hitch`, or a future named driver) |
| `provider` | resolved provider (`claude-code`, `codex`, or a future named provider); never inferred from `driver` |
| `manifest_digest` | `sha256:` digest of the exact validated launch-manifest bytes |
| `provider_continuation` | provider-native continuation handle or `null`; it is never an alias for `run_id` |
| `writer` | the component that directly observed the activity fact |
| `activity` | allowlisted codes and numeric/process/provider facts; never arbitrary captured content |

The first valid record fixes `run_id`, `hive`, `bead`, `driver`, `provider`, and
`manifest_digest` for the file. Later records MUST repeat those values byte-for-byte.
`provider_continuation` may move from `null` to the exact handle observed by the provider and is
then repeated unchanged. A consumer that sees any other identity change marks the source
degraded and does not merge the two identities.

`source_revision` is a record/cursor token, not a timestamp, byte offset, git SHA, sequence
number, or lifecycle revision. File order is authoritative for journal order. A reader resumes
after the matching opaque revision and reports a missing revision as a resync requirement.

## Writer ownership

An outer owner may propagate a fact it received unchanged. It may not turn an inference into an
observation. The `writer` value always names the component whose direct observation produced the
line.

| writer | facts it may originate | facts it must not invent |
|---|---|---|
| `beadhive.local-loop` | outer `run_id`; exact hive/bead; journal creation; spawn pid/pgid; cancellation rung and signals; reap result; harvest and child exit facts | effective provider/model, provider continuation, or a bead lifecycle status inferred from process exit |
| `beadhive.role` | explicit-role request validation; qualified artifact and manifest digest; resolved driver/provider request; its own child-process and parsed-result boundary | LocalLoop decisions, direct-Hitch liveness, or provider facts not present in the typed provider result |
| `agent-hitch.direct` | a directly launched Hitch run; Hitch-resolved manifest/driver/provider; process facts Hitch supervises; provider continuation Hitch directly observes | LocalLoop/bh-role activity or BAML use merely because an artifact exists |
| `baml.provider` | whether BAML was actually used; effective provider/model; provider-native continuation; typed usage/cost/outcome returned by that provider path | outer process-group/reap facts or bead lifecycle state |

The resolved `driver` and `provider` are separate facts. In particular, `driver=baml` does not
mean `provider=claude-code`, and a Claude or Codex request does not prove BAML ran. A launch owner
records the provider selected by the validated manifest; the provider writer later confirms the
effective fact. A mismatch is a launch/result contract error, never a reason to rewrite the
identity of an existing journal.

The digest is similarly provenance-bearing. `beadhive.role` owns the digest of a qualified BAML
launch manifest it validated; direct Hitch owns the digest of its resolved Hitch manifest.
LocalLoop propagates the validated value unchanged and does not hash a nearby file as a guess.

## Propagation contract

LocalLoop and `bh role` propagate the outer context through the child environment without
changing who owns the process. The v1 variable names are:

```text
BH_RUN_JOURNAL_VERSION=beadhive.run-journal/v1
BH_RUN_JOURNAL_PATH=<absolute run-scoped JSONL path>
BH_RUN_ID=<immutable outer attempt id>
BH_RUN_HIVE=<exact registered hive identity>
BH_RUN_BEAD=<exact bead id>                    # absent when bead is null
BH_RUN_DRIVER=<resolved driver>
BH_RUN_PROVIDER=<resolved provider>
BH_RUN_MANIFEST_DIGEST=sha256:<64 lowercase hex characters>
```

These variables are inherited inputs, not authority. A child rejects a conflicting pre-existing
value rather than silently replacing it, and it appends only facts it owns. The environment
names may be disclosed by `--explain`; their values, the journal path included, are not copied
into activity records.

Provider continuation travels through the driver's typed provider input/output. It deliberately
has no `BH_RUN_ID` compatibility alias and MUST NOT be recovered from a legacy `session_id` that
merely echoed the outer id. A resume may supply an already-known continuation, but the new outer
attempt still receives a new `run_id`.

## Append and read semantics

- A journal is one run-scoped file, opened append-only. Records are never updated, deleted, or
  compacted in place.
- One serialized JSON object plus its newline is emitted in one write. Writers use `O_APPEND`
  and bound each line to the platform atomic-write limit used by the dispatch sink (4096 bytes
  on the supported Linux path). No writer emits a multi-line record.
- The launch owner attempts directory/file creation and the initial `run.created` append before
  process creation. Concurrent child writers receive the already-resolved path and context.
- Readers ignore a final incomplete line while it may still be in flight. Invalid complete
  lines, duplicate revisions, identity drift, or a missing resume revision make coverage
  explicitly `degraded`; they are never silently repaired.
- Files are host-local runtime data. A remote reader cannot claim freshness without a named
  relay/colocation guarantee, and copied files never imply live coverage.

The schema intentionally keeps `activity` closed and code-shaped. Adding a fact means adding an
optional schema property (compatible) or cutting a new contract version (incompatible); it does
not mean dumping an upstream result object into JSON.

## Lifecycle state, coarse runtime state, and rich activity are different sources

There are three deliberately separate views:

1. **Beads is lifecycle authority.** Claims, gates, assignment, review, merge and terminal bead
   state come only from beads and ordinary `bh work` operations. No journal record can advance,
   reconstruct, or override them.
2. **`AgentRunSummary` is coarse host-local runtime state.** Its
   `starting | active | waiting | finished | failed | unknown` projection answers what a process
   appears to be doing. It is not bead state and is allowed to be partial or stale.
3. **The run journal is rich append-only activity.** It carries correlation, spawn/cancel/reap,
   provider resolution, continuation, usage and outcomes. A reader may derive a summary from
   these events, but writers never store a mutable `status` field as truth.

A downstream join keeps all three sources labelled. Exact `hive` + `bead` correlates lifecycle
and runtime; `run_id` selects one outer attempt; `provider_continuation` correlates provider
history. Missing `bead` means the run cannot be joined to a bead and MUST remain run-scoped.

## Degradation is observable and outcome-neutral

Journal I/O is observability, not control flow. Failure to create, open, append, flush, or read a
journal MUST NOT change any claim, wait, cancellation rung, signal, reap, retry, harvest, exit
classification, bead mutation, or process exit code. The same launch and lifecycle operations run
with a disabled/no-op journal writer.

The failure still has to be visible outside the failed sink:

- the component emits one structured `run_journal_write_failed` diagnostic through its normal
  stderr/log channel with `run_id`, `hive`, optional `bead`, operation, and exception class;
- the host-local runtime/source descriptor reports `degraded`, the last successful
  `source_revision` if any, and a dropped-record count;
- if a later append succeeds, its record may set `activity.journal_degraded=true`, but that later
  record is not a substitute for the out-of-band diagnostic.

Diagnostics exclude the attempted JSON, path contents, and raw exception text when it can embed
data. Repeated failures may be rate-limited, but the degradation state and drop count cannot be.

## Redaction and data minimisation

The journal is an allowlist, not a redacted transcript. Writers serialize the schema fields they
own directly and never serialize then scrub an upstream object.

Excluded everywhere, including diagnostics:

- credentials, bearer material, cookies, auth headers, API keys, secret environment values and
  credentialed URLs;
- task text, bead title/description, prompts, instructions, model messages, tool arguments or
  results, transcripts, stdout/stderr, argv, and environment values;
- arbitrary exception text or absolute workspace/config paths.

`manifest_digest` records identity without manifest content. `reason_code` and `outcome_code` are
bounded machine codes, not free text. `provider_continuation` is an opaque non-secret handle. If a
provider exposes only a bearer credential as its continuation mechanism, the credential stays in
the provider's credential store and the journal carries a non-secret handle to it; a secret is
never made acceptable by naming it “continuation”.

File permissions are private by default (`0700` parent, `0600` file). Permissions are defence in
depth, not permission to weaken the exclusions above.

## Compatibility checklist

A v1 implementation is conformant only if it proves:

- creation and `run.created` are attempted before spawn; retry mints a different `run_id`;
- all valid records pass the schema and retain the immutable identity tuple;
- a provider continuation is structurally separate and never equals an echoed outer id by
  construction;
- LocalLoop, explicit `bh role`, direct Hitch and BAML-provider fixtures emit only facts their
  named writer directly observes;
- concurrent writers leave complete parseable lines with unique opaque revisions;
- injected sink failures are diagnosed while lifecycle calls, process results and exit codes
  remain identical to the no-failure control;
- forbidden content and credential fixtures leave no bytes in the journal or diagnostic.
