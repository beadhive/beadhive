# Spike `bh-drhd2` — byte-first snapshot policy for large hives

**Bead:** `bh-drhd2` · **Seat:** `dev/projection-policy`  
**Type:** research-only (no product code)  
**Feeds decision on:** `bh-8iwzi` and `bh-k9wrw`

## Question

Can Core return all useful current-work summaries for the 1,344-item Factory hive while keeping
the actual encoded daemon response below the strict Frame Bridge 1 MiB input limit, preserving
truthful coverage, deterministic ordering, strict validation, and lazy access to full bead detail?

This spike does not ask whether to raise the input limit, weaken validators, transmit the full
entity graph, or make omission silent. It ships no product code.

## Method

On 2026-09-25 I inspected the producer, daemon contract, Frame Bridge parser and disclosure
allowlist, SSE relay, and the existing queue/detail API. I compared the supplied Factory capture
with compact JSON aggregates from the canonical local database:

```sh
bd list --all --brief --limit 0 --json | jq '<status, compact-summary, and byte aggregates>'
```

The compact estimate projected only bounded summary fields: ID, title, status, issue type,
priority, labels, assignee, and update time. It is an estimate, not the acceptance authority. The
implementation must encode the complete final response, including cursor, coverage, policy,
limits, and envelope, then measure those exact UTF-8 bytes before emission.

I compared current-only fail-closed, a fixed item cutoff, pagination-only initialization, rich
entity selection, and a compact byte-first seed with lazy detail. I assessed determinism,
truthfulness, CPU/memory defense, snapshot/SSE agreement, cross-hive isolation, and pane reads.

## Evidence

1. The exact Factory response is 9,401,780 bytes for 6,613 work items and 7,439 dependencies.
   Work items account for about 4.57 MB and dependencies about 3.68 MB; assignments, schedules,
   epics, gates, agents, cursors, coverage, and wrappers make up the rest. A rich work item is
   606--1,176 bytes (median 640).
2. `hive_operator_snapshot()` currently emits every rich issue wrapper plus large related arrays.
   Frame Bridge must read that body through its strict 1 MiB parser before it can validate or
   discard fields. Relay-side filtering is therefore too late (`src/beadhive/operator_contract.py`,
   `src/beadhive/frame_bridge_upstream.py`).
3. Current-only eligibility removes 5,233 closed and 36 deferred Factory items but still leaves
   1,344 summaries: 1,313 open, 30 in progress, and one blocked. A 1,000-item normal cutoff would
   unnecessarily mark or reject this hive even when a compact response fits the byte budget.
4. The canonical local corpus had 1,243 current visible records. Its measured compact summary
   projection was 404,532 bytes, or 325.45 bytes per item. Linear application to 1,344 Factory
   summaries estimates 437,402 bytes before the final envelope and the optional bounded readiness
   counts. Sensitivity at 500 and 600 bytes per summary is 672,000 and 806,400 bytes respectively,
   both below the 917,504-byte producer target before envelope accounting.
5. At the measured 325.45-byte average, 896 KiB holds about 2,819 summaries before envelope
   overhead. Thus a separate 4,096-summary structural cap is defensive: for realistic summaries,
   bytes bind well before that cap. Factory's 1,344 eligible summaries are below it and must not
   be rejected by count. Pathological tiny summaries remain structurally bounded for CPU/memory
   and schema-abuse defense.
6. Core already exposes revision-pinned `ready`, `active`, `blocked`, and `recent` queue pages of
   at most 200 fixed rows and a separate exact-detail endpoint. Cursors bind hive, source revision,
   normalized query, ordering, and offset; a revision change returns stable conflict
   (`src/beadhive/operator_work_items.py`, `src/beadhive/operator_api.py`).
7. Readiness does not require transmitting the graph. The producer can derive readiness and
   blocker/open-gate/live-agent counts from the authoritative full state, then emit bounded scalar
   summaries. Description, design, acceptance, notes, dependency bodies, gates, schedules,
   assignments, agent bodies, and recursive epic content belong behind exact detail.
8. SSE already carries revision invalidation instead of item bodies. One hive-wide invalidation
   can make an active pane refetch its bounded revision-pinned query without per-filter replay
   state or missed membership transitions.

### Policy comparison

| policy | byte safe | Factory 1,344 | completeness and cost |
|---|---|---|---|
| Current-only plus 1,000 fail-closed | yes | rejected | truthful but leaves production unavailable |
| First 1,000 rich entities | no | partial/rejected | count does not bound graph or string bytes |
| Pagination only, no seed | yes | complete over pages | loses one small atomic initialization snapshot |
| Rich byte-bounded seed | yes after encoding | uncertain | wastes budget on wrappers/arrays Frame discards |
| Compact byte-first seed + lazy detail | yes | estimated complete | maximal useful seed; detail paid only on demand |

### Selected producer contract

- **Compact contract:** replace the producer-facing rich seed with the unshipped strict summary
  contract at `schemaVersion: 1` and policy `beadhive.snapshot-summary/v1`. Frame Bridge, Gateway
  fixtures, OpenAPI/schema artifacts, and app-dev handoff publish that one contract together. It
  accepts only the fixed compact shape and no arbitrary extra fields.
- **Eligibility:** current `in_progress`, `blocked`, and `open` product work. Closed, deferred,
  event, and gate records are excluded.
- **Order:** status tier `in_progress`, `blocked`, `open`; then numeric priority ascending,
  `updatedAt` descending, and ID ascending. No process order or current clock participates.
- **Fixed work-item DTO:** bounded `id`, `title`, `status`, derived `readiness`, `issueType`,
  numeric `priority`, at most 12 bounded labels plus `remainingLabelCount`, contract-safe
  `assignee` and `owner`, `updatedAt`, and bounded `blockerCount`, `openGateCount`, and
  `liveAgentCount`. Every field is explicit; there are no arbitrary expansions.
- **No rich related arrays:** the seed carries no descriptions, designs, acceptance criteria,
  notes, dependency/gate/schedule/assignment/agent bodies, or recursive epic content. Readiness
  counts are derived internally from complete same-hive state. If a minimal context reference is
  later proven necessary, it needs its own bounded typed field and must fit the same final body.
- **Bounded byte-first algorithm:** scan eligible authoritative records while counting them, but
  retain at most the best 4,096 record references under the deterministic order in a bounded
  top-k structure. An additional eligible record sets structural overflow and replaces the
  retained worst record only when it ranks earlier. Sort only those retained references, then
  materialize at most 4,096 compact summaries. Encode each candidate prefix with its final cursor,
  coverage, policy version, source revision, counts, limits, and envelope, retaining the largest
  prefix whose complete response is at most 917,504 bytes. No unbounded summary collection exists.
- **Limits:** the producer target is 917,504 bytes (896 KiB), leaving 128 KiB beneath the unchanged
  strict 1,048,576-byte Frame Bridge input limit. Frame Bridge raises its old 1,000-item validator
  to the versioned 4,096 defensive cap while retaining strict byte, shape, string, label, and
  integer validation.
- **Coverage:** complete when every eligible summary fits. If the byte budget or structural cap
  omits work, coverage is explicitly partial with eligible and returned counts, stable reason
  (`byte_budget` or `structural_cap`), policy version, source revision, and both limits. Omission is
  never inferred from array length.
- **Per-hive scope:** authorization, limits, ordering, coverage, cursor, and cache identity belong
  to one exact hive. Gateway never concatenates hive seeds into an unbounded body.
- **Events:** snapshot installation and SSE use the same source/projection revision. One opaque
  hive-wide invalidation stream remains; a source change causes active panes to refetch.

For Factory, the measured compact estimate is about 437 KB. Even the conservative 600-byte
summary sensitivity is about 806 KB before the envelope, so all 1,344 summaries are expected to
fit beneath 896 KiB. The implementation acceptance test decides with actual v1 encoding: if the
complete final body fits, coverage must be complete; it may not stop at 1,000. If actual bounded
titles, labels, counts, cursor, and coverage push it over, a deterministic prefix fits and coverage
truthfully reports `byte_budget`.

### Lazy queue and exact-detail contract

The snapshot initializes the overview. A pane then requests a finite `ready`, `active`, `blocked`,
or `recent` view. Optional inputs are normalized priority, label, assignee, type, and immediate
parent filters plus page size and opaque cursor. Limits remain at most five priorities, eight
labels, 64 UTF-8 bytes per label, 256 bytes for assignee/type/parent, a 4 KiB cursor, and 200 rows.
Queue pages use the same fixed summary DTO, their own measured byte limit, and exact cursor scope.

Clicking or expanding an item calls exact detail. That separately allowlisted DTO owns bounded
description, design, acceptance criteria, notes, dependencies, gates, agents, and advertised
actions under an independent encoded-byte limit and stable too-large response. Large detail never
inflates the seed or a queue page.

One opaque hive-wide SSE subscription remains authoritative. Invalidation makes the client refetch
only its active pane. Query-specific streams, arbitrary field masks/expansions, arbitrary purpose
or status arrays, raw timestamps, recursive graph expansion, regex/glob/OR expressions, and
arbitrary agent/session filters remain rejected because they create disclosure, byte, cache, CPU,
or replay cardinality hazards.

## Verdict — **GO**

A compact byte-first v1 seed should carry all 1,344 Factory summaries while remaining well below
the strict 1 MiB input boundary. Exact final encoding is the authority; a 4,096-summary cap is only
defense in depth. Revision-pinned queues and separately bounded exact detail preserve access to
everything intentionally absent from the seed.

## Recommendation

Land `bh-tyclz` first, then implement `bh-8iwzi` (**Implement compact byte-first snapshots with
truthful coverage**) as the production unblock: compact v1 DTO, bounded selection, exact 896 KiB
measurement, 4,096 defensive cap, coverage, Frame validation, SSE agreement, Factory regression,
and versioned release handoff.

Follow with `bh-k9wrw` (**Relay revision-pinned paginated work-item reads through Frame Bridge**):
strict summary pages, independently bounded exact detail, cursor/error semantics, per-hive cache
and authorization, and Gateway/app-dev conformance. Neither follow-up may raise the 1 MiB parser,
silently omit work, relay rich seed entities, accept arbitrary fields, or create query streams.
