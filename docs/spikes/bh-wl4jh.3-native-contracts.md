# Spike `bh-wl4jh.3` — native schemas, projections, row ceilings, and provenance

**Bead:** `bh-wl4jh.3` · **Seat:** `dev/codex-wl4jh-3` · **Type:** research-only
(no product code)
**Feeds decision on:** `bh-wl4jh.4`, and through it `bh-6p2j`

## Question

Which Beads v1.3.0 contract features can make Beadhive's retained reads smaller, more typed, and
safer, and which can replace multi-operation Beadhive bookkeeping without weakening lifecycle
semantics?

The operator-requested priority is deliberate: API/schema compatibility first, serialized bytes
and approximate token reduction second, iteration speed through fewer or atomic operations third,
and provenance fourth. A fast projection that cannot say whether a field is absent or empty is
not a safe contract.

## Method

I inspected the exact Beads v1.3.0 source and installed binary at
`f45b249ce6b40ba62aecc03949e6371e8f7c79d8`, including the reflected schema, lite SQL
projection, show dependency projection, row-cap, HTTP projection, and provenance implementations.
I compared them with Beadhive at `68690363f1ca374cc76d2c34db99824f3c1398bf`, the merged
`bh-ie41e.1` service capability map, and `bh-wl4jh.1`'s coordination findings.

Payload probes were read-only against database `bh` through its normal managed configuration.
They used compact JSON and count bytes exactly with `wc -c`. Approximate token counts use the
transparent planning estimate `bytes / 4`; they are not tokenizer-specific billing figures.
The database was live, so the row counts are an observation at probe time rather than a fixture.

## Impact ranking

| rank | v1.3 feature | measured or structural impact | verdict |
|---|---|---|---|
| **1 — API and schema contracts** | `bd schema` reflects canonical Issue and Dependency Go structs as strict Draft 2020-12 schemas | deletes one hand-maintained model source for canonical import/export records and catches enum/field drift; it does **not** describe HTTP requests, command envelopes, list/show decorations, briefness, or provenance | **Narrow GO:** vendor and validate the exact schema, generate canonical models, then compose explicit command/projection models; do not call it an OpenAPI schema |
| **2 — token and payload reduction** | `list --brief`, `ready --brief`, `show --brief-deps`, and native count operations | on this hive: 58.7%, 80.1%, 54.0%, and 99.99959% fewer serialized bytes respectively | **GO at audited call sites:** carry projection intent out of band because brief JSON has no partial marker |
| **3 — iteration speed / atomic operations** | count instead of materialize-and-count; idempotent provenance append instead of read-modify-write linkage; service-side `brief=true` | count turns an unbounded/paginated materialization into one aggregate; a common one-SHA linkage can become two commands to one; projections keep one operation but greatly reduce hydration/parse/context cost | **GO for count after filter-parity tests; narrow provenance pilot:** projections alone are not atomicity |
| **4 — native provenance** | append-only, deterministic-id events with indexed issue/ref lookup | strong primitive for external artifact bindings, especially one Git SHA; no HTTP capability, no published schema, no transaction coupling to lifecycle mutation, and actor remains opaque | **Narrow GO after compatibility/backfill design:** retain review gates, lifecycle audit, and `git.commits` until every reader moves |
| **safety — `--max-rows`** | fetch ceiling for list, ready, dependency tree, and selected sweeps | returns exit 2 with no stdout rather than truncating or feeding partial JSON downstream | **GO as a per-call circuit breaker:** never treat it as pagination or set a blind process-wide cap |

This ordering agrees with `bh-ie41e.1`: a transport or projection is adoptable only after its
wire shape is explicit. It also agrees with `bh-wl4jh.1`: the largest operation-count win remains
native claim/CAS; the features here optimize read and audit paths rather than replacing seat,
review-SHA, molecule, or host-fence invariants.

## 1. Exact schema implications

### What `bd schema` guarantees

`bd schema` needs no database. In this build it emits `schema_version: 1` and two self-contained
Draft 2020-12 object schemas, `types.issue` and `types.dependency`. Both set
`additionalProperties: false`. The issue schema exposes 56 properties and requires only `id`,
`title`, `priority`, `created_at`, and `updated_at`; the dependency schema requires `issue_id`,
`depends_on_id`, `type`, and `created_at`. Status, issue-type, and dependency-type enums are
sourced from the same Go lists used by native validation.

That is materially stronger than copying field names into a downstream Pydantic model: the
schema is reflected from the structs that serialize canonical `--json` / export records. Pinning
the binary commit plus schema document gives Beadhive a testable source contract.

It is not a complete command contract:

- list/show/ready rows add command-specific fields such as `parent`, `dependency_count`,
  `dependent_count`, `comment_count`, `revision`, or nested shallow issue rows;
- validating the first measured `bd list` row directly against `types.issue` correctly failed
  because `parent` and the three count fields are additional properties;
- array-producing commands do not uniformly carry a top-level `schema_version` envelope;
- `--brief` returns the same JSON record shape with heavy fields omitted, while its internal
  `IsLitePartial` marker has `json:"-"` and never crosses the wire;
- `--brief-deps` nests shallow **Issue** objects plus `dependency_type`, not canonical
  `types.dependency` records;
- the schema contains neither provenance events nor HTTP request/response, error, cursor, or
  pagination types; `/v0/beads/context`'s HTTP `schema_version: 1` and Dolt schema v66 are also
  independent version axes.

Most importantly, `bh-ie41e.1` found no retrievable OpenAPI document in the certified server.
`bd schema` cannot fill that gap: it describes canonical CLI/export records, not the 40 service
operations. Generated HTTP clients remain blocked on an exact vendored or served OpenAPI
contract.

### Generated and validated model candidate

Adopt three layers rather than one permissive catch-all:

1. At toolchain upgrade, run the pinned `bd schema`, require the expected binary commit and
   `schema_version`, validate both documents with `Draft202012Validator.check_schema`, and vendor
   the byte-stable artifact under a versioned Beads contract directory.
2. Generate strict `BdIssueRecord` and `BdDependencyRecord` models from those two definitions.
   Use them only for canonical import/export records. Keep `extra="forbid"`; silently accepting
   an upstream field defeats the drift signal this feature provides.
3. Hand-compose small command contracts around the generated base: `BdListRow` adds parent and
   counts, `BdShowRow` adds revision/dependency decorations, and HTTP page/error models remain
   separate. Generate a `BdBriefIssue` schema by removing the six heavy properties and make the
   adapter return a wrapper such as `Projected[ BdBriefIssue ]` whose `projection="brief"` is
   supplied by the request, never inferred from blank fields.

The schema-generation gate should validate representative full export records, then separately
validate measured list, ready, show, and error fixtures against their composed command schemas.
This fits Beadhive's existing Draft 2020-12 tooling. Do not generate models at every invocation;
generate and diff them only when the pinned Beads artifact changes.

Compatibility layers that must remain are Beadhive's command-envelope/version contracts,
legacy aliases such as `acceptance`, timestamp normalization, source-system/origin policy,
dependency decorations, gate/state records, and HTTP capability negotiation. The upstream
schema validates storage records; it does not authorize a lifecycle transition or establish
projection parity.

## 2. Measured payload and token reductions

| read on database `bh` | rows | full bytes (~tokens) | narrow bytes (~tokens) | saved |
|---|---:|---:|---:|---:|
| `list --all --limit 0 --json` → add `--brief` | 4,456 | 10,578,095 (~2,644,524) | 4,366,054 (~1,091,514) | 6,212,041 bytes / ~1,553,010 tokens (**58.7%**) |
| `ready --limit 0 --json` → add `--brief` | 588 | 2,134,164 (~533,541) | 424,170 (~106,042) | 1,709,994 bytes / ~427,498 tokens (**80.1%**) |
| `show bh-wl4jh.3 --json` → add `--brief-deps` | 1 issue, 3 dependencies | 5,479 (~1,370) | 2,518 (~630) | 2,961 bytes / ~740 tokens (**54.0%**) |
| matched full-list cardinality → `count --include-infra --json` | 4,456 | 10,578,095 (~2,644,524) | 43 (~11) | 10,578,052 bytes / ~2,644,513 tokens (**99.99959%**) |

The brief list projection drops `description`, `design`, `acceptance_criteria`, `notes`,
`payload`, and `waiters` at SQL selection time. Only `description` and `acceptance_criteria` were
present in the measured first full row, illustrating why savings depend on corpus content. Heavy
fields remain usable in filters because they remain in `WHERE`; they are simply not selected.

The brief dependency probe retained exactly these identity fields on each dependency:
`id`, `title`, `status`, `priority`, `issue_type`, `created_at`, `updated_at`, and
`dependency_type`. It removed descriptions, designs, acceptance, notes, actors, labels, metadata,
and lease fields from nested dependencies while leaving the outer issue whole.

`--brief` intentionally makes omitted text indistinguishable from real empty text in JSON. The
internal `IsLitePartial` flag does not serialize. Therefore a safe caller must know from its own
request that the row is partial; no code may inspect a missing/empty description and conclude the
issue has no description. The same rule applies to `--skip-labels` and skipped counts: absence is
request metadata, not a fact about the issue.

### Safe call-site recommendations

| Beadhive seam | projection | why safe / required guard |
|---|---|---|
| `worktree_inventory.impl__probe_store` | `list --limit 1 --brief` | asks only whether any readable issue exists; bounded existence is safer than materializing the default page |
| `worktree_inventory._batch_evidence_for_entry` | add `--brief` | reads id, labels, status, and integration parent; none of the six omitted fields participates |
| `doctor._orphan_container_branches` | add `--brief` | builds only the set of issue ids |
| `work_metrics.backfill_stale_review_labels` | add `--brief` | consumes id from already label/status-filtered rows |
| internal `work next`, `localloop.claimable_now`, and automatic epic picker ready reads | add `--brief` | consume id, status, type, assignee, labels, dependencies/counts, and order; all are retained |
| scheduling-only child snapshots | add an explicit `brief=True` path to `bd.child_rows` | schedule/group/finish guards consume topology, labels, type, and status; do not change the shared default because work brief/review prints body text |
| work assignment/submission/merge guard calls to `bd.show` | opt in to `--brief-deps` | outer issue remains complete and nested dependency identity/topology remains; audit any caller that reads nested labels/metadata before broadening |
| dashboards or guards needing only a cardinality | native `count` / HTTP `issues.count` or `ready.count` | one aggregate instead of fetching and parsing every row; require exact filter parity and compare count to list in contract tests |

Keep full payloads for state-stream export/reconciliation, `complexity_backfill` (which reasons
over issue text), plan/work brief and review rendering, triage and outbound editorial queues,
public/MCP passthroughs that promise native JSON, and any release estimator that consumes body
text. Projection is an internal adapter choice, not a silent public response change.

One count wrinkle deserves a regression test: during the probe, bare `bd count --json` returned
5,906 while `bd count --include-infra --json` returned 4,456, the value matching
`bd list --all --include-infra --limit 0`. That flag/result relationship is surprising relative
to the help text. The measured 43-byte comparison uses the matching 4,456 result; Beadhive must
not replace a list with count until the exact filter pair is pinned in a parity fixture.

## 3. Iteration speed and atomic operation effects

Projections do not by themselves reduce the number of commands: one full list and one brief list
are each one subprocess/round trip. They reduce database column hydration, serialization,
Python JSON parsing, and agent context. On the measured ready set the same operation crosses the
boundary with about 427,498 fewer estimated tokens, so repeated scheduler/agent turns can inspect
the authoritative set without paying for bodies they never read.

Narrow aggregate and provenance primitives can reduce operations:

| current Beadhive shape | native candidate | operation effect | atomicity effect |
|---|---|---|---|
| fetch one or more complete pages, parse rows, then `len`/group | `issues.count`, `ready.count`, or CLI `count` with the same filters | potentially N paginated HTTP round trips and a large payload → **one aggregate round trip** | count is one database snapshot; it avoids a page-to-page drifted cardinality |
| discover whether any issue exists by default list | `list --limit 1 --brief` | one operation remains, but bounded to one narrow row | no new atomicity; removes accidental default-page work |
| `git_linkage.record_commits` for the common one-SHA case: `show` current metadata then `update --set-metadata` | idempotent `provenance record --kind commit\|land --ref-kind git-sha` | **2 bd operations → 1** (50% fewer) | deterministic insert removes read-modify-write/lost-update window |
| repeated list/ready HTTP pages when bodies are unused | service `brief=true`; use count when only cardinality is needed | same page count for brief; count can collapse all pages to one | projection is not atomic; count is |

Do not overstate the provenance win. Recording N rebased SHAs is N native events because v1.3
exposes no provenance batch record; Beadhive's current metadata array can append N SHAs in one
update after one read. Native provenance is better for the common single commit/land binding and
for concurrent producers, not automatically cheaper for bulk history.

The larger lifecycle atomics remain those identified by `bh-wl4jh.1` and `bh-ie41e.1`:
`claimNext` can collapse choose+claim to one transaction; batchApply/CAS may collapse member
writes; `casMetadata` can protect metadata. Schema/projection adoption should make those API
results typed, but it does not make a generic update atomic or eliminate Beadhive authorization.

## 4. Native provenance

The native log has a useful, narrow contract:

- kinds are `cut`, `claim`, `suspend`, `resume`, `handoff`, `commit`, `land`, and `used`;
- reference kinds are `git-sha`, `pr`, `work-id`, `transcript`, and `branch`; Git SHA requires
  exactly 40 lowercase hexadecimal characters;
- deterministic id is SHA-256-derived from `source:issue:kind:(ref or occurred_at)` and duplicate
  insert is a no-op, so retrying a known request is idempotent;
- ref-less events require caller-owned `occurred_at`; event time and ingest time are distinct;
- events are append-only individually and indexed by issue, ref, and kind, but deleting an issue
  cascades to its provenance rows;
- `actor`, `ref`, and payload are opaque. `source` is required, and `ingest-backfill` is reserved
  for reconstructed records; ordinary `provenance record` explicitly rejects that source;
- `provenance_events` is not in v1.3.0's merge auto-resolve allowlist. Concurrent inserts on
  separate replicas can require manual conflict resolution even though their content-addressed
  union would be commutative.

This maps well to Beadhive's `git.commits` linkage. A native `commit` or `land` event can bind a
bead to one full SHA without reading and rewriting a JSON array, and `provenance by-ref` provides
the reverse lookup the flat metadata value lacks. Suggested producers are closed, trusted names
such as `beadhive-submit`, `beadhive-merge`, and `beadhive-backfill`; the actor must still be
derived from the trusted seat, never copied from an untrusted client.

The compatibility migration is necessarily staged:

1. Dual-write native events and `git.commits`, with failures visible but preserving today's
   post-merge non-fatal rule.
2. Design a privileged import/backfill path for old arrays (the ordinary record command cannot
   use reserved source `ingest-backfill`), and compare both readers over complete history.
3. Move history-policy and linkage readers to provenance, retaining review-SHA gates and Git
   reachability checks.
4. Remove metadata writes only after all supported Beads versions have provenance and rollback
   can reconstruct the old view.

Native provenance does **not** replace field-mutation events, operational state beads, exact-SHA
review gates, validation attestations, actor authentication, or branch reachability. It is absent
from both `bd schema` and the certified service's 40 capabilities, so initial adoption remains a
retained CLI subprocess unless upstream adds its API and schema. Coordinate any multi-replica
pilot with `bh-wl4jh.2`; do not increase federation conflict exposure merely to remove a local
read-modify-write.

## 5. `--max-rows` semantics and safe use

The live probe `list --all --limit 0 --max-rows 1 --json` found the second row, emitted no JSON
to stdout, printed a diagnostic to stderr, and exited **2**. Source confirms the implementation
queries at most cap+1 where needed and deliberately touches no stdout on failure, preventing a
half-rendered array from masquerading as valid partial data. Zero disables the cap; an explicit
flag overrides `BEADS_MAX_ROWS`, including `--max-rows 0` disabling an inherited cap.

It is a circuit breaker, not a result limit:

- `--limit N` requests at most N successful rows; `--max-rows N` refuses an over-cap query;
- a cap failure is not an empty result, not truncation, and not retryable by silently dropping
  the cap;
- `list` and `dep tree` honor the cap on direct and proxied-server routes; `ready` supports it on
  the direct route but explicitly refuses a nonzero cap under proxied-server mode;
- `find-duplicates` and `graph` also expose the cap, while selected doctor/lint sweeps read only
  `BEADS_MAX_ROWS`;
- `bd.json` currently collapses every nonzero command into `None`, so callers cannot distinguish
  this safety trip from an outage until the adapter preserves exit code 2 as a typed result.

Start with explicit per-call caps on exact full-corpus/list and ready reads whose expected maximum
comes from a Beadhive policy (batch cap, molecule cap, managed-worktree count) or a deliberately
high operator-configured safety ceiling. Surface a typed `row_ceiling_exceeded` error with the
query and cap. Do not set `BEADS_MAX_ROWS` globally in the Beadhive service: unrelated legitimate
full snapshots would start looking like failed/empty reads, and different commands have different
proxy support. Exact reconciliation/export paths should fail loud and ask the operator to refine
or raise the cap; they must never continue from partial state.

## Verdict

**Narrow GO, contract-first.** The v1.3 schema is ready to replace a hand-maintained canonical
Issue/Dependency model source, and brief/count projections show large real reductions on this
hive. The first adoption slice should vendor and validate the pinned schema, generate strict
canonical models, add explicit projection-aware command wrappers, then turn on `--brief` only at
the audited internal call sites above. Add typed row-ceiling failures at the same boundary.

It is **NO-GO** to treat `bd schema` as OpenAPI, infer partialness from blank fields, change public
JSON payloads silently, enable a blind global row cap, or delete `git.commits`/review audit today.
Pilot provenance as a dual-written single-SHA binding after the typed read boundary exists. This
sequence captures the token and iteration-speed gains without coupling them to the separate
`bd serve` transport verdict or weakening Beadhive's lifecycle contracts.
