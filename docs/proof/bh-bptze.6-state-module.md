# `bh-bptze.6` state capability extraction

## Boundary and baseline

The exact starting revision was `5f0abfde3196381523faf216eb455607670bb43f` with tree
`2b8ceb8cf05a69057f802cef0b10be2746ed1308`. It was clean and Good-signed. The strict
pre-edit comparison was split by authority: 71 durable-validation tests, 172 stream/activity/
journal reader tests, and 71 operator/gateway/MCP projection tests all passed (314 total).

The selected state candidate scores 13/14 (`2,1,2,2,2,2,2`) for cohesion, coupling, data/effect
ownership, port narrowness, replaceability, dependency direction, and test-closure reduction.
The remaining coupling point is deliberate: outer adapters still compose existing filesystem,
process, Dolt, and transport behavior. A validation-only extraction was rejected because it would
leave the shared stream/activity query contract outside its named owner. A whole operator/daemon
move was rejected because transport session, wire, authorization, and lifecycle policy are separate
owners and would make port narrowness and dependency direction hard stops.

## North-star and dependency direction

`beadhive.modules.state` owns:

- immutable validation run facts that retain the complete versioned manifest payload;
- the state-stream v1 scopes, normalized entities, snapshot/delta/resync frames, opaque cursor
  semantics, replay ordering, and payload projection;
- agent-run and run-journal activity/query DTOs with explicit coverage and freshness;
- `ValidationRecordStorage`, `StateProjectionReader`, `ActivityProjectionReader`, `StateClock`,
  and `StateNotifier` outbound ports; and
- `ValidationRecordService` and `ReadProjectionService` application boundaries.

Concrete callbacks and the system clock live in `beadhive.state_services`, outside the capability.
They are built per composition so existing monkeypatch seams and resource ownership stay live.
`beadhive.state_stream` and `beadhive.agent_run_summary` remain compatibility facades whose public
types and functions are identical to the module-owned objects. `beadhive.public_readers` remains an
outer filesystem/process adapter while returning module-owned query models.

Durable validation manifest writes and exact latest-run queries enter the typed validation service
without changing path layout, random identity, atomic replacement, schema, replay, qualification,
or migration behavior. Operator source reads and the state-stream CLI enter the typed read service;
operator wire projection modules import the module DTOs directly. Gateway and MCP retain their
existing typed read/catalog boundaries and do not gain private storage imports.

## Explicit CQRS limit and compatibility invariants

The read service exists only for already-landed snapshot/replay/aggregation needs. It does not add
an event bus, command model, event store, independently synchronized duplicate model, or runtime-only
lifecycle authority. Commands continue through their existing work, planning, and host application
services. Beads remains lifecycle authority; validation executions remain immutable run facts; stream
provider revisions remain opaque and provider-instance scoped; journal outer run identity remains
distinct from dispatch session identity.

Preserved behavior includes snapshot-first ordering, unknown-cursor resync, exact hive/run identity,
coverage/freshness honesty, validation run/use separation, canonical-vs-imported ordering, typed
runner-protocol validation, CLI NDJSON and signal/broken-pipe behavior, operator HTTP payload shape,
and existing facade/patch names.

## Live-work reconciliation

`bh-bptze.5 — Extract planning, filing, and repair contracts without work/report import cycles`
owns plan/spec validation and report/triage boundaries. Its approved change was merged first, and
this commit was rebased onto capability tip `4457e9a5986035a86d2fee57a483e8ac11fae46a`.
Excluding the proof and shared composition registries, the implementation payload's stable patch
ID remained `63bc5dba00b36bbdf32f04c617612873fbe90482`; range-diff changes are limited to this
updated proof, the composed planning/state closure counts/assertions, and the exact combined import
snapshot. This change does not touch planning, report, or triage implementation.

`bh-q0lol.1 — Define daemon configuration, wire contracts, and exact identity resolution` owns
daemon route/event/cursor/auth schemas and currently changes only daemon configuration/contract
paths. This extraction consumes no daemon implementation and does not edit those files.
`bh-12hb7 — Advertise zero-event generated Development hives as snapshot-only` owns the generated
gateway catalog capability advertisement; the catalog and its manifest remain untouched.

## Isolation and validation evidence

Pure fixture tests construct both application services with in-memory ports and no filesystem,
daemon, network, process, or storage startup. An import/effect sentinel rejects dependencies back to
CLI, MCP, operator API, public readers, legacy state/validation modules, or run journals. Callback
contract tests prove compatibility facades and full manifest payload retention.

Recorded checkpoints:

- untouched strict baseline: 314 passed across the three authority groups;
- first pure stream/activity transition: 42 passed;
- public-reader and operator/CLI wiring transition: 59 passed;
- durable validation storage/query transition: 71 passed;
- pure service, callback adapter, and independence transition: 9 passed; and
- registered `just test-module state`: 86 passed in 15.94 seconds; and
- strict post-migration comparison: the same 71 validation, 172 state/read, and 71 transport
  tests passed (314 total).

The composed import graph is 281 files and 2,463 edges. Relative to the approved planning tip, the
state extraction reduces the reviewed legacy snapshot from 83 to 82 cyclic modules, 276 to 272
cyclic edges, and 306 to 298 cyclic symbols, while retaining the same five owned legacy components.
The exact reviewed snapshot was updated;
there are no new boundary exceptions. Ruff checked and format-checked 708 files, the closure
registry is 23 present and zero absent, and Markdown lint checked 181 files with no issues. The
authoritative full `just check` remains submit-owned and is not replaced by this evidence.
