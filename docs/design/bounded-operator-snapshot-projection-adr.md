# Compact byte-first operator snapshot ADR

**Status:** accepted by spike `bh-drhd2`  
**Date:** 2026-09-25  
**Implementation:** `bh-8iwzi`, then `bh-k9wrw`

## Context

Factory's host response is 9,401,780 bytes: 6,613 rich work items, 7,439 dependency bodies, and
other entity arrays. Frame Bridge correctly rejects it at the strict 1 MiB input boundary before
relay-side projection. Current-only filtering leaves 1,344 summaries, so the former 1,000-item
normal cutoff would reject a compact body that is expected to fit.

The local measured compact shape averaged 325.45 bytes per current item. That estimates 437,402
bytes for Factory's 1,344 summaries; 600 bytes per summary is 806,400 bytes. These are planning
measurements only. Acceptance is the exact final encoded response.

## Decision

The host snapshot becomes a bounded operational summary seed, not a rich entity/graph transfer.

1. Publish the unshipped strict contract at `schemaVersion: 1` with policy
   `beadhive.snapshot-summary/v1`. Each work item is one fixed compact DTO: bounded
   identity/title/status/readiness/type/priority,
   bounded labels and remaining count, safe assignee/owner, update time, and bounded
   blocker/open-gate/live-agent counts.
2. Exclude description, design, acceptance, notes, dependency/gate/schedule/assignment/agent
   bodies, recursive epic content, and rich host wrappers. Compute readiness and counts from the
   full authoritative same-hive state without serializing that state.
3. Eligible summaries are current product work, ordered by `in_progress`, `blocked`, `open`, then
   priority ascending, update time descending, and ID ascending.
4. Scan authoritative eligible records with an integer total and a bounded top-k selection that
   retains at most the best 4,096 references under the deterministic order. Detect overflow during
   the scan; never construct an unbounded summary collection and truncate it afterward. Sort and
   materialize only the bounded retained references.
5. The primary limit is the actual final UTF-8 body. Encode cursor, coverage, policy, revision,
   limits, counts, summaries, and envelope together; emit at most 917,504 bytes (896 KiB). Return
   the largest deterministic retained prefix whose complete final body fits.
6. The 4,096-summary structural cap is only CPU/memory/schema-abuse defense. Frame Bridge enforces
   that cap and preserves the unchanged 1,048,576-byte parser plus strict shape, field, string,
   label, and integer bounds. Bytes should bind first for realistic summaries; Factory's 1,344
   items are not rejected on count.
7. Coverage is complete when all eligible summaries fit. Otherwise it is partial with eligible
   and returned counts, `byte_budget` or `structural_cap`, policy version, source revision, and
   both limits.
8. Queue pages remain revision-pinned, fixed-summary views of at most 200 rows. Exact detail is a
   separate strictly allowlisted request with independent string, collection, disclosure, and
   encoded-byte limits for full text, dependencies, gates, agents, and actions.
9. One opaque hive-wide invalidation-only SSE subscription remains. Active panes refetch their
   bounded query after a revision change; there are no query-specific streams or field masks.
10. Authorization, cursor scope, cache identity, and all budgets are per exact hive. Gateway never
   concatenates multiple hive seeds or pages into an unbounded response.

## Consequences

The useful work count is determined by bytes, not an arbitrary normal item cutoff. Factory should
receive all 1,344 summaries with complete coverage; the production-scale test must prove that on
the exact v1 encoder. Pathological fields can reduce the deterministic prefix without exceeding
the wire limit, and pathological tiny items cannot exceed the 4,096 structural defense.

The producer no longer pays to serialize graph/detail bodies that Frame Bridge discards. Detail
cost moves to the user action that needs it. Frame Bridge, Gateway contract fixtures, OpenAPI/schema
artifacts, and app-dev must advance together in a reviewed versioned release handoff.

Rejected alternatives are: the 1,000 normal cutoff, rich byte-bounded entities, Frame-side
post-read filtering, pagination without a seed, higher parser limits, silent truncation, arbitrary
field expansion, query-specific streams, and cross-hive aggregation.

Supporting evidence and request bounds are in
[`bh-drhd2-overload-projection-policy.md`](../spikes/bh-drhd2-overload-projection-policy.md).
