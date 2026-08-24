# Public bead and runtime readers

> **Status:** accepted · **Date:** 2026-08-24 · **Bead:** `bh-e8s3i.4`
>
> **Public module:** `beadhive.public_readers`

This contract composes three shipped observer sources for downstream relays without combining
their authority, identity, revision, freshness, or process-lifetime domains.

## Sources remain separate

| public reader | authority and scope | revision | cancellation |
|---|---|---|---|
| `BeadFrameReader` | canonical injected `StateStreamProvider`; factory/hub/hive bead lifecycle and operator entities | the provider's opaque snapshot/delta revision, unchanged | closing iteration exits `StreamProcessScope`, which owns the bd adapter process tree |
| `AgentRunSnapshot` | one explicitly named host and dispatch-summary source; coarse `AgentRunSummary` process state | opaque digest of the bytes observed at that source | bounded snapshot read |
| `RunJournalTailReader` | one explicitly named host/source and one immutable outer `run_id`; rich append-only activity | that run's opaque `source_revision` only | required `StopToken`, whose wait interrupts a live poll |

`BeadFrameReader` yields `state_stream.stream_frames` values directly. It does not translate,
re-envelope, or reinterpret snapshot-first ordering, partial state, `as_of`, replacement deltas,
or operator arrays. Its provider factory receives the reader-owned `StreamProcessScope`; consumer
code does not spawn bd or perform list/show fan-out. `BeadFrameReader.polling` is today's concrete
composition, not a backend dependency in the public frame types.

## Hive and bead correlation

The bead stream's `hive` is the registry repo slug, for example `beadhive`. Run journals carry the
full registered identity, for example `github/beadhive/beadhive`. `HiveCorrelation` is the only
bridge: it is constructed from a complete registry row (`provider`, `org`, `repo`) and maps that
exact full identity to that exact repo slug. It ignores the registry prefix. A prefix, bead-id
prefix, description, filesystem directory, dispatch sink filename, or sanitized service instance
is never evidence of hive identity.

After the explicit hive mapping succeeds, exact bead-id equality joins bead state to a journal.
A journal whose `bead` is JSON `null` remains run-scoped and never joins a bead.

## Summary snapshots do not fabricate journal correlation

`AgentRunSummary.session_id` is the seat-process identifier emitted by the dispatch sink. It is
not the journal's outer `run_id` and is not `provider_continuation`. No landed dispatch-summary
writer carries outer `run_id`, so exact summary-to-journal correlation is unavailable.
`AgentRunSnapshot.journal_correlation` therefore always reports `unavailable` with an explicit
reason. Consumers may correlate both sources to the same exact bead, but must not claim that a
particular summary session and journal attempt are the same process attempt until writer truth
adds that fact.

Each summary snapshot names `host_id` and `source_id`; neither is inferred. Its revision is an
opaque content token. `complete` describes a parseable observed file, not live writer freshness.
A missing source is `unknown`, an unreadable or failed projection is `degraded`, and malformed or
incomplete source content is `partial`. All may contain an empty summary tuple, but only the
coverage fields say what that emptiness means. A copied file always has freshness `unknown`, even
when its observed bytes are complete and recent.

## Run-scoped tail and resynchronization

`RunJournalTailReader` takes one path, explicit `host_id`, explicit `source_id`, and the expected
outer `run_id`. Its frames are scoped to that run alone:

- `snapshot` carries the accepted records currently observable for the run;
- `delta` carries records after an exact matching opaque `source_revision`;
- `resync` carries no records and requires a following full `snapshot` when a requested revision
  is unknown or the previously observed revision disappeared after source replacement/truncation.

There is no host-wide journal cursor or total ordering across journal files. File order is the
only order inside one run. Revisions are compared for equality only; consumers never parse,
sort, increment, or manufacture them.

The first accepted record fixes `run_id`, `hive`, `bead`, `driver`, `provider`, and
`manifest_digest`. Identity drift, duplicate revisions, continuation drift, a continuation that
aliases `run_id`, or an invalid complete line degrades the source and the offending record is not
merged. A final incomplete line is ignored with partial coverage while a writer may still be
appending it. Missing/unreadable files remain unknown/degraded rather than authoritative empty.
Copied journal files never claim live freshness.

Every live tail requires a `StopToken`. `StopToken.stop()` interrupts the poll wait; no caller has
to wait for the full interval. Bead-provider process cleanup remains independently owned by
`StreamProcessScope`; stopping a journal tail neither signals nor changes a bead lifecycle.

## Non-authority guarantees

None of these readers advances claims, gates, assignment, review, merge, retries, provider
continuation, process exit classification, or journal contents. Runtime records never override
bead state. Bead state never invents process facts. Source absence or degradation changes only
coverage reported to observers.
