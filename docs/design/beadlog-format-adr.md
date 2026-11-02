# ADR: BeadLog v0 format — op-log events, content-addressed ids, capabilities and loss reports

- **Status:** Accepted for format version `0.1.0`. The format may still change while the
  `beadlog` package is `Private :: Do Not Upload` (the XTDB verdict, `bh-5wu0u`, is GO; decision
  `bh-psu1b`); any change follows [Versioning and compatibility](#11-versioning-and-compatibility).
  Amended 2026-10-31 with the XTDB spike findings: see
  [Amendment 2026-10-31](#amendment-2026-10-31-xtdb-spike-findings).
- **Date:** 2026-10-30
- **Decision owner:** Beadhive maintainers
- **Bead:** `bh-65jen`, under epic `bh-ebopn` (BeadLog v0 format and the shared backfill engine)
- **Architecture:** [`backfill-platform-adr.md`](backfill-platform-adr.md) decides *that*
  BeadLog is the interchange and plan format and which package owns it. This ADR decides the
  bytes: archive layout, event envelope, id rule, kinds, ordering and merge, capabilities,
  loader degradation, erasure and redaction, and versioning.
- **Research:** [`beadlog-store-landscape-research.md`](beadlog-store-landscape-research.md)
  (spike `bh-o1x4r`): store survey, measurements and every source consulted. Its full source
  list is reproduced under [References](#references).
- **XTDB spike evidence (amendment 2026-10-31):**
  [`bh-o1x4r`](../spikes/bh-o1x4r-beadlog-store-landscape.md),
  [`bh-w56ts`](../spikes/bh-w56ts-xtdb-bead-shape.md),
  [`bh-cntr0`](../spikes/bh-cntr0-xtdb-operational-fit.md),
  [`bh-3jp2g`](../spikes/bh-3jp2g-xtdb-bitemporal-load.md).
- **Machine contract:** [`docs/schemas/beadlog/v0.1/`](../schemas/beadlog/v0.1/) —
  `manifest.schema.json`, `event.schema.json`, `loss-report.schema.json`, and `examples/`
  (a worked archive, valid and invalid examples, and `id-vectors.json`). Guarded by
  `tests/test_beadlog_schemas.py`.
- **Consumers:** `bh-lycrj` (`beadlog` domain: ids, merge, fold, diff — must reproduce the id
  vectors below byte-for-byte), `bh-oj64o` (archive I/O and the loader-conformance kit), the bd
  loader, and the XTDB spike `bh-5wu0u`, whose verdict refines the XTDB mapping.
- **Not a wire-operation schema.** BeadLog is an archive format. Nothing here adds to the
  published Beadhive wire schemas (`docs/schemas/wire/`) or to the CLI.

## Context

Bead history is eroding and the stores that hold it disagree about time. On hive `bh`, beads
date from 2026-06-29 but Dolt history starts 2026-07-25 and the bd `events` table 2026-08-07
(platform ADR, Context). [`BEAD_GROUPS.md`](../BEAD_GROUPS.md) already requires that "an
imported or backfilled record keeps its original occurrence time distinct from the time the store
recorded it" (two time axes, "Every bead has a lifecycle, and history is its journal"), that
identity is `(group, bead)` with readable prefixes as aliases ("Identity beyond readable
prefixes"), and that erasure reaches archives while retention removal does not.

The store survey found that no store holds all three time axes natively, that every store's
native ids or sequence numbers are source- or replica-local, and that recorded-time
preservation, wherever it exists (XTDB v2.2+ backfill transactions, Datomic `:db/txInstant`), is
**monotone-only**: a backdated transaction must still be later than the latest one. The bd/Dolt
store has no valid time at all; neither does the upstream BDP core events profile.

The platform ADR fixed the shape: a ZIP of size-partitioned JSONL op-log parts, atomic events,
three time fields, SHA-256 ids over RFC 8785 canonical JSON of the identity fields only, set
add/remove, scalar last-writer-wins, capabilities that loaders refuse when unknown, and a loss
report. What it left open — and what an implementation cannot guess — is the exact field list,
the canonicalization rule, the per-kind payloads, the null and tie rules, the capability
vocabulary, and how each store degrades. Those are cheap to change now and expensive once
archives exist.

### Decision drivers

1. **Two independent implementations must agree byte-for-byte** on every event id.
2. **Re-extraction is idempotent:** extracting the same history twice yields the same ids.
3. **Merge is a join:** union of logs is commutative, associative and idempotent.
4. **Loss is explicit:** a store that cannot keep something says so, per event and member.
5. **Valid time is first class**, recorded time is preserved where a store can, and the
   monotone constraint is declared rather than discovered.
6. **Erasure propagates; retention and redaction do not** (BEAD_GROUPS).
7. **Stdlib-only** implementation in the `beadlog` package (platform ADR §2).

## Decision

### 1. Archive layout

A BeadLog is a ZIP file with extension `.bdlg` and media type
`application/vnd.beadhive.beadlog+zip` (unregistered vendor tree; `.bdl` collides with existing
formats, `.bdlg` did not at research time). The container follows the EPUB OCF `mimetype`
convention and borrows the Frictionless Data Package idea of a manifest describing every member,
with a digest per member like a Python wheel `RECORD`.

| Member | Required | Content |
|---|---|---|
| `mimetype` | yes | First entry, STORED (no compression, no extra field), exactly the ASCII media type, no newline |
| `MANIFEST.json` | yes | Second entry; validates against `manifest.schema.json` |
| `schema/manifest.schema.json`, `schema/event.schema.json` | yes | Byte copies of the schemas for the archive's `format_version` |
| `events/<dir>/part-NNNN.jsonl` | yes | Event parts for the group whose `groups[].dir` is `<dir>`; every kind except `checkpoint` |
| `checkpoints/<dir>/part-NNNN.jsonl` | no | `checkpoint` events only |
| `index/recorded.jsonl` | no | Global recorded-time replay index (capability `recorded-time-monotone-load`) |

Rules:

- **Parts.** UTF-8, no BOM, one JSON object per line, every line terminated by LF. Parts of one
  group are numbered from `0000` without gaps; writers split by size (a writer setting, so a
  reader assumes nothing about part size) and never split an event across parts. Concatenating a
  group's parts in number order is sorted by `(bead.group, bead.id, ordering key)` — see
  [§6](#6-ordering-fold-and-merge) — with group and bead id compared as UTF-8 bytes. Writers
  SHOULD serialize each line with sorted keys and no insignificant whitespace (the example does);
  readers MUST NOT depend on it.
- **Index.** `index/recorded.jsonl` has one line per event in the archive (events and
  checkpoints): `{"id", "line", "part", "recorded_at"}`, `line` 1-based, sorted by
  `(recorded_at, id)` with null `recorded_at` last. It exists so a loader that must replay in
  transaction-time order does not have to sort the whole archive. It is present if and only if
  the manifest declares `recorded-time-monotone-load`; the manifest schema enforces both
  directions.
- **Manifest `files[]`** lists every member except `mimetype` and `MANIFEST.json`, sorted by
  path, with `role`, `sha256` (lowercase hex of the member bytes), `bytes`, and for JSONL members
  `records` and `sorted_by`.
- **Reproducible archives.** Entries after the first two are in path order; every entry has
  modification time 1980-01-01 00:00:00 and mode 0644; no directory entries; UTF-8 names
  (Reproducible Builds archive guidance). `created_at` in the manifest is optional and, when
  present, is the maximum `extracted_at`, never the wall clock.
- **ZIP64.** Readers MUST support ZIP64; writers use it only when needed.

Manifest members (normative detail in the schema):

| Member | Meaning |
|---|---|
| `format_version` | Semver of the format, `0.1.PATCH` for this ADR |
| `generator` | `{name, version, uri?}` of the writing tool |
| `groups[]` | `{id, dir, aliases?, description?}`; `id` is the durable group identity used in every `bead.group` |
| `time_semantics` | Constant for v0.1 (`occurred_at` valid time, `recorded_at` source transaction time, `extracted_at` extraction time, UTC, order `occurred_at, recorded_at, id`), so an archive is self-describing |
| `capabilities` | `{required[], optional[]}` — [§7](#7-capabilities) |
| `redaction` | `{applied, policy?, withheld?}` — [§10](#10-erasure-retention-and-redaction) |
| `files[]` | Member inventory as above |
| `counts` | `{events, beads, by_kind}` |
| `plan` | Optional: `{pass, schema_version, corpus_hash, plan_hash, judge{engine, version, model}}` when the archive is a backfill plan (capability `plan`) |
| `annotations` | Ignorable extension object; `bdlg.`-prefixed keys reserved |

#### Worked example archive

The example lives unpacked under `docs/schemas/beadlog/v0.1/examples/archive/`; packing it in
the order below gives the archive. The unpacked tree omits `mimetype`, whose content is fixed (the
media type above): a packer always writes it first. Its `MANIFEST.json` `files[]` carries the
authoritative byte counts and digests.

```text
member                             method   records
mimetype                           STORED   -
MANIFEST.json                      DEFLATE  -
checkpoints/bh/part-0000.jsonl     DEFLATE  1
events/bh/part-0000.jsonl          DEFLATE  21
index/recorded.jsonl               DEFLATE  22
schema/event.schema.json           DEFLATE  -
schema/manifest.schema.json        DEFLATE  -
```

It holds four beads in one group (`urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60`, alias
`beadhive/core`, dir `bh`) and exercises every kind: a task reconstructed partly from a dated
planning doc (day precision, no recorded time) and partly from bd events; an epic first seen in a
git commit; a decision retired with a successor; a mistaken bead created and deleted; a comment
that was erased (only its tombstone remains); and a checkpoint of the task after its last event.
It declares `tombstones` required and `checkpoints` and `recorded-time-monotone-load` optional.

### 2. Event envelope

One event is one JSON object; `event.schema.json` is normative for shape
(`additionalProperties: false` at the envelope and in every `data` payload).

| Member | Type | Required | Identity | Merge of equal-id events |
|---|---|---|---|---|
| `id` | `sha256:<64 hex>` | yes | — (the result) | equal by definition |
| `kind` | one of the 12 kinds | yes | yes | equal |
| `bead` | `{group, id}` | yes | yes | equal |
| `occurred_at` | timestamp or null | yes | yes (dropped when null) | equal |
| `occurred_precision` | `second`…`year` | no | yes | equal |
| `recorded_at` | timestamp or null | yes | yes (dropped when null) | equal |
| `extracted_at` | timestamp | yes | **no** | minimum |
| `source.system` | `^[a-z][a-z0-9._-]*$` | yes | yes | equal |
| `source.ref` | string | yes | yes | equal |
| `source.evidence[]` | `{type, ref, note?}` | no | **no** | set union |
| `provenance` | `observed` \| `derived` \| `inferred` | yes | yes | equal |
| `confidence` | number 0..1 | no | **no** | maximum |
| `actor` | string or null | no | yes (dropped when null) | equal |
| `lamport` | integer ≥ 0 | no | yes | equal |
| `prev[]` | event ids, sorted, unique | no | yes | equal |
| `data` | per-kind object | yes | yes | equal |
| `redacted[]` | JSON Pointers | no | **no** | [§10](#10-erasure-retention-and-redaction) |
| `annotations` | object | no | **no** | per key, smallest JCS value |

- **Bead identity** is `(group, id)` per BEAD_GROUPS. `group` must be declared in
  `MANIFEST.json` `groups[]`. Prefixes are not identity. v0.1 has no alias kind: a bead id is
  never rewritten in an archive, and alias events arrive in a later version behind a required
  capability ([§11](#11-versioning-and-compatibility)).
- **Provenance** follows the BDP `origin` idea: `observed` facts were read from a system of
  record (bd `events`, a git commit); `derived` facts were computed deterministically from
  observed ones (a checkpoint, a merge commit's bead id); `inferred` facts are judgments (a
  planning doc implies a bead was opened that day). `confidence` defaults to 1 for observed.
- **Annotations** are the only ignorable extension point. Values in `annotations` follow the
  same no-float rule as identity material ([§4](#4-event-id)) so that merge stays deterministic.

### 3. Time model

| Field | Axis | SQL:2011 | XTDB v2 | CloudEvents 1.0.2 | BDP core events |
|---|---|---|---|---|---|
| `occurred_at` | valid time: when it happened in the world | application-time period start | `_valid_from` | `time` ("when the occurrence happened") | none (degrades to `annotations`) |
| `recorded_at` | source transaction time: when the source store recorded it | system-versioned period start | `_system_from`, settable only by v2.2+ monotone backfill | — | `time` (recording time, not a total order) |
| `extracted_at` | when the extractor read it | — | — | — | — |

- **Timestamp form.** RFC 3339 in UTC with a `Z` suffix, no offset, seconds always present,
  fractional seconds only when non-zero, without trailing zeros, at most nine digits
  (`2026-08-08T14:30:00.25Z`, never `…:00.250Z` or `…+00:00`). One spelling per instant keeps ids
  stable; the schema pattern enforces it. Timestamps compare as instants, not strings
  (`…:00.25Z` is later than `…:00Z` although it sorts earlier as text).
- **Null.** `occurred_at` is null when the valid time is unknown; `recorded_at` is null for
  proposals (not yet recorded anywhere) and for history whose recording time was lost (bd events
  before 2026-08-07). Both members are always present; null is explicit.
- **Precision.** `occurred_precision` says the written instant is the start of a coarser interval
  (a doc dated 2026-07-03 gives `2026-07-03T00:00:00Z` with `day`). Absent means as precise as
  written. A loader that cannot keep precision reports `precision.lost`.
- `extracted_at` is excluded from the id, so re-extraction does not mint new events.

### 4. Event id

The id rule is the contract `bh-lycrj` and every other implementation must reproduce exactly.

1. **Identity projection.** Build a new JSON object containing, from the event, exactly these
   members, each only when present **and not null**:
   `kind`, `bead`, `occurred_at`, `occurred_precision`, `recorded_at`, `provenance`, `actor`,
   `lamport`, `prev`, `data`; plus `source` reduced to exactly `{"system", "ref"}`.
   Everything else is excluded: `id`, `extracted_at`, `confidence`, `source.evidence`,
   `redacted`, `annotations`, and any future non-identity member. The drop-null rule applies
   to these top-level members only; inside `bead`, `data` and `source` every member is kept as
   written, and a `null` inside `data` is significant (a `set` to null clears the field).
2. **Admissible values.** Identity material contains only strings, booleans, null, arrays,
   objects, and integers in the I-JSON safe range ±(2^53 − 1). **Non-integer numbers are
   forbidden**, and integers must be written without fraction or exponent (`2`, never `2.0` or
   `2e0`); a value that needs a fraction is carried as a string. This removes the only hard part
   of RFC 8785 (ECMAScript number formatting). Strings must be valid Unicode (no lone
   surrogates) and are **not** normalized (no NFC/NFD).
3. **Canonicalize** the projection with RFC 8785 (JCS). Under rule 2 that means: no whitespace;
   object members sorted by key compared as UTF-16 code-unit sequences; strings escaped as
   ECMAScript `JSON.stringify` does — `\"`, `\\`, `\b`, `\t`, `\n`, `\f`, `\r`, other characters
   below U+0020 as `\u00XX` with lowercase hex, every other character (including all non-ASCII)
   emitted literally; integers in plain decimal.
4. **Hash** the UTF-8 bytes of the canonical text with SHA-256. The id is `sha256:` followed by
   the 64-character lowercase hex digest.

Consequences of the rule:

- Two events that differ only in excluded members have the **same id** and merge
  ([§6](#6-ordering-fold-and-merge)). Two observations of the same fact through different
  sources (`source.system`/`source.ref` differ) have **different ids**; reconcile, not the id,
  decides whether they duplicate each other.
- **Ids are minted once and never recomputed by a loader or an exporter.** A store that keeps a
  loaded event reports it back with its original id (bd: `metadata.bdlg.*.event_id`); a store's
  own recording time of a proposal does not rewrite the proposal's null `recorded_at`.
- Readers SHOULD verify ids (`beadlog verify`); an event with `redacted` cannot be verified.
- Python's `json.dumps(projection, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`
  produces the same text only while no object key contains a character above U+FFFF: Python
  sorts keys by code point, and code-point and UTF-16 order differ exactly there. Implementations
  must sort by UTF-16 code units.

#### Worked id examples

These are events from the example archive, with the projection text that is hashed and the
resulting id. All five, with their full events, are in
`docs/schemas/beadlog/v0.1/examples/id-vectors.json`; the test recomputes each one.

**`phase-observed`** — a minimal observed event (bd event 102: the task moves to
`in_progress`). Every identity member present, no optional ones:

```text
{"actor":"dev/alice","bead":{"group":"urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60","id":"bh-ex1"},"data":{"phase":"in_progress"},"kind":"phase","occurred_at":"2026-08-07T09:15:00Z","provenance":"observed","recorded_at":"2026-08-07T09:15:02Z","source":{"ref":"bd:events/102","system":"bd"}}
id: sha256:5c23babf711db4ba84779147b3da5bf9926e5e39b5a9bcb37f42fc64eb87a0a2
```

**`create-day-precision`** — an inferred create from a planning doc. The event has
`recorded_at: null`, no `actor`, `confidence: 0.75`, an `evidence` entry and an
`extracted_at`; none of these appear in the projection:

```text
{"bead":{"group":"urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60","id":"bh-ex1"},"data":{"fields":{"priority":2,"title":"Draft the BeadLog ADR"},"type":"task"},"kind":"create","occurred_at":"2026-07-03T00:00:00Z","occurred_precision":"day","provenance":"inferred","source":{"ref":"docs:docs/plans/beadlog.md#L1","system":"docs"}}
id: sha256:f7519bd9c6cce36e7b8dbbbc48244f974824a9258bf3850368b5043efc595c89
```

**`comment-escaping`** — the newline is escaped as `\n`; `ï`, `é` and `✓` are raw UTF-8 bytes,
never `\u` escapes:

```text
{"actor":"dev/alice","bead":{"group":"urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60","id":"bh-ex1"},"data":{"body":"Schemas live under docs/schemas/beadlog/v0.1.\nNaïve café check: ✓"},"kind":"comment","occurred_at":"2026-08-09T08:00:00Z","provenance":"observed","recorded_at":"2026-08-09T08:00:00Z","source":{"ref":"bd:events/151","system":"bd"}}
id: sha256:04e0feab060ea7b1eab97f9195bd8c0d321491a5d3a45a6e0b55a1c07896dd8f
```

**`set-metadata-integer`** — a dotted metadata field with an integer value:

```text
{"actor":"dev/alice","bead":{"group":"urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60","id":"bh-ex1"},"data":{"field":"metadata.estimate_minutes","value":90},"kind":"set","occurred_at":"2026-08-11T09:30:00Z","provenance":"observed","recorded_at":"2026-08-11T09:30:01Z","source":{"ref":"bd:events/172","system":"bd"}}
id: sha256:43967e8c8b23f20557485c32929c1c65557b21b811ee0de0c63ca5aa857a29af
```

**`set-clear-null`** — the `null` inside `data` is kept (it clears `assignee`), unlike a
top-level null:

```text
{"actor":"dev/alice","bead":{"group":"urn:uuid:5f0c9a52-8a43-4f0e-9a5e-1c7d2b3e4f60","id":"bh-ex1"},"data":{"field":"assignee","value":null},"kind":"set","occurred_at":"2026-10-30T21:40:00Z","provenance":"observed","recorded_at":"2026-10-30T21:40:03Z","source":{"ref":"bd:events/301","system":"bd"}}
id: sha256:f2e7dd23ba7d1b59c5bc6e3e5a9b7c5c5c9c380bfc565a7560708a84326d9c0f
```

The erased comment in the example (bd event 153) had id
`sha256:c935ef49a4d0c644a7d60b15527b128f4e5347f93da55737d113835fb9d63087`; only the tombstone
naming that id is in the archive.

### 5. Kind taxonomy

Twelve kinds, exactly the platform ADR's list. "Facet" is the unit last-writer-wins resolves
over ([§6](#6-ordering-fold-and-merge)).

| Kind | `data` payload | Semantics | Facet | Inverse (rollback) |
|---|---|---|---|---|
| `create` | `{type, fields{title, …}}` | The bead exists; sets `type`, phase `open` and each listed field. Labels and edges are **not** in `create`; they follow as add events | existence; `type`; `phase`; each `fields.<name>` | `retire` (fix forward: undoing a create closes or retires, never deletes) |
| `set` | `{field, value}` | Scalar assignment of one field; `value: null` clears it | `fields.<field>` | `set` of the same field to its folded value just before the event (null if none) |
| `label_add` | `{label}` | Set element added | label `<label>` | `label_remove` of the same label |
| `label_remove` | `{label}` | Set element removed | label `<label>` | `label_add` of the same label |
| `edge_add` | `{type, target{group, id}}` | Directed edge from `bead` to `target` added | edge `(type, target)` | `edge_remove` of the same edge |
| `edge_remove` | `{type, target{group, id}}` | Edge removed | edge `(type, target)` | `edge_add` of the same edge |
| `comment` | `{body}` | Append-only; each comment is its own element, identified by its event id; `actor` is the author | none (append) | none; removal is erasure (`tombstone`) |
| `phase` | `{phase, reason?}` | Lifecycle phase transition (`open`, `in_progress`, `blocked`, `deferred`, `closed`, or a type-declared phase); never `retired` or `deleted` | `phase` | `phase` to the folded phase just before the event (a reopen) |
| `retire` | `{successor?, reason?}` | Phase becomes `retired`; the bead stays readable with its links and may name a successor (BEAD_GROUPS "Retiring a bead is a phase transition, not a deletion") | `phase` | `phase` to the folded phase just before the event |
| `delete` | `{reason?}` | The bead should not exist; it leaves current views, its history stays | existence | `create` re-seeding the folded pre-delete state (type, fields, phase), then label and edge adds |
| `tombstone` | `{scope: bead\|events, events[]?, reason}` | Erasure: content that must not exist. `events` lists erased event ids (scope `events`); scope `bead` erases every non-tombstone event of the bead. Never carries the erased content | none; applied to every cut | none, by design |
| `checkpoint` | `{state{type, phase, fields, labels[], edges[], retired?, deleted}, covers_through{occurred_at, recorded_at, id}}` | Full folded state after the event named by `covers_through`; `provenance` is always `derived`; `occurred_at`/`recorded_at` equal those of `covers_through`. Null fields are omitted from `state.fields`; `labels` sorted; `edges` sorted | none | none (not a state change) |

Field and vocabulary rules:

- **Field names** match `^[a-z][a-z0-9_]*(\.[A-Za-z0-9_-]+)*$`. Core fields: `title`,
  `description`, `design`, `acceptance_criteria`, `notes`, `priority` (integer), `type`,
  `assignee`, `owner`, `external_ref`, `due_at`, `defer_until`. Metadata keys are separate scalar
  fields addressed as `metadata.<key>`; the whole `metadata` object is never one field.
  Reserved, and rejected by the schema: `id`, `status`, `phase`, `labels`, `dependencies`,
  `edges`, `comments`, `metadata`, `retired`, `deleted` — those are kinds, not fields. A loader
  that does not know a field reports `field.unsupported`.
- **Edge direction** is from `bead` to `target`, following bd: "A depends on B" is
  `edge_add` on A with target B; a child names its parent with type `parent-child`. Core edge
  types: `blocks`, `parent-child`, `related`, `discovered-from`, `supersedes`, `duplicates`.
- **Bead types** and phases are open lowercase vocabularies; `retired` and `deleted` are not
  phase names (they have their own kinds).

### 6. Ordering, fold and merge

**Ordering key.** Every event has the total-order key `(k1, k2, id)`:

- `k1` = `occurred_at` if not null, else `recorded_at` if not null, else +∞;
- `k2` = `recorded_at` if not null, else +∞;
- `id` compared as ASCII.

Timestamps compare as instants. An undated proposal (both times null) therefore applies after
all dated history. Ties at equal times are broken by id: deterministic, not causal. A producer
that knows the causal order of same-instant events should give them distinct times; `lamport`
and `prev` are carried for clock-ordered sources (the git-bug model) and reserved for a future
causal ordering mode, but do not change the v0.1 key.

**Fold** (state of one bead at a cut). Sort the bead's events by the key and keep those at or
before the cut. A valid-time cut at instant T keeps events with `k1 ≤ T` (undated events only
appear at the open "now" cut). A bitemporal "as recorded at R" read first drops events whose
`recorded_at` is null or later than R, then folds.

- Each facet in the table above resolves **last-writer-wins on the ordering key**: the latest
  event touching the facet decides it. `create` counts as an assignment of `type`, `phase: open`
  and each field it lists; `retire` as an assignment of `phase: retired` (plus `retired` details).
- **Existence:** the bead exists iff the latest of its `create`/`delete` events is a `create`.
- **Labels and edges** form last-writer-wins element sets: an element is present iff its latest
  add/remove event is an add. Sets always change by add/remove, so applying the same event twice
  is harmless and order of arrival does not matter.
- Facets are independent: an `edge_add` that sorts before the bead's `create` (the example's
  task, where both carry the same day-precision instant) is still folded.
- **Comments** are every non-erased `comment` at or before the cut, in key order.
- **Tombstones** apply to every cut, past and future: erased events are treated as if they never
  existed. With scope `bead` the bead keeps its identity and is reported as erased.
- **Checkpoints.** If the merged log has no non-checkpoint event of the bead at or before
  `covers_through`, the latest such checkpoint **seeds** the bead (as a `create` plus an add for
  each label and edge, all at the `covers_through` key); the archive must then declare
  `checkpoint-seeded`. Otherwise the checkpoint is an **assertion**: `verify` folds the events
  and reports any mismatch. The example's checkpoint is an assertion, and the test re-folds it.

**Merge.** `union_merge(logs)`:

1. Union all events by `id`. For equal ids the identity projections must be byte-identical; if
   they are not, the merge **fails** with an id-collision error — it never picks one.
2. Combine the non-identity members of equal-id events: `extracted_at` minimum; `confidence`
   maximum of those present; `source.evidence` set union, de-duplicated and sorted by the JCS
   bytes of each item; `annotations` per top-level key, the value whose JCS bytes are smallest;
   `redacted` per [§10](#10-erasure-retention-and-redaction).
3. Apply tombstones: drop every erased event. Tombstones are absorbing — merging an older
   archive that still holds erased content erases it again.
4. Write the result in part order.

Each combining rule is a semilattice join, so `union_merge` is commutative, associative and
idempotent (the property tests in `bh-lycrj`). `diff(proposed, baseline)` — the events of
`proposed` whose ids are absent from `baseline`, plus per-bead folded-state deltas — is defined
over this fold in `bh-lycrj` and is what reconcile classifies.

### 7. Capabilities

An archive declares what a reader must understand (`capabilities.required`) and what it may
ignore while reporting the loss (`capabilities.optional`). **A reader refuses an archive whose
`required` list names a capability it does not implement.** The v0.1 manifest schema encodes the
vocabulary as a closed enum for `required` (so an unknown required capability fails validation)
and an open pattern for `optional`.

| Capability | Class | Declared when | A reader without it |
|---|---|---|---|
| `tombstones` | required (schema-enforced when `counts.by_kind.tombstone ≥ 1`) | any tombstone is present | refuses: it would resurrect erased content |
| `checkpoint-seeded` | required, never optional | some bead's history in the archive begins at a checkpoint | refuses: folding without the seed gives wrong state |
| `redaction` | required (schema-enforced when `redaction.applied`) | events carry `redacted` | refuses: ids are unverifiable and members are missing |
| `checkpoints` | optional | checkpoints present that are derivable from the archive's events | ignores `checkpoints/` |
| `recorded-time-monotone-load` | optional (schema-enforced iff `index/recorded.jsonl` is present) | the archive ships the global recorded-time index, so recorded time can be preserved by a monotone replay | loads in part order; recorded time is not preserved (`recorded-time.reassigned`) |
| `lamport` | optional | events carry `lamport`/`prev` | ignores them |
| `plan` | optional (schema-enforced when `plan` is present) | the archive is a backfill plan | treats it as a plain archive |

A producer may also put an optional capability in `required` to insist on it. Future additions
that change meaning — a new kind, a new envelope member — arrive as new **required** capability
names (`kind:alias` is the planned first), so a v0.1 reader refuses them instead of
misreading them.

**Loader capabilities** describe the target store and appear in the loss report:

| Loader capability | Meaning |
|---|---|
| `valid-time` | stores `occurred_at` natively per facet |
| `per-element-valid-time` | labels and edges keep their own valid periods |
| `recorded-time` | can set arbitrary recorded time (no surveyed store) |
| `recorded-time-monotone-load` | can set recorded time only by replaying strictly increasing transaction times into a store with no later transaction |
| `erase` | physically removes content across all history |
| `metadata-fallback` | can keep degraded values in per-bead metadata |
| `erase-logical` | *(added 2026-10-31, [A1](#a1-erase-is-logical-in-xtdb))* erased content is absent from every valid-time and system-time read, but its bytes may survive in the store's log and files until the store is rebuilt; mutually exclusive with `erase` |

### 8. Loader degradation and the loss report

A loader maps events onto a store. Whatever it cannot keep as written, it degrades and
**reports** — it never drops silently.

| Store class | Examples | `occurred_at` | `recorded_at` | Null `recorded_at` | Labels/edges | Tombstone | Typical loss codes |
|---|---|---|---|---|---|---|---|
| No valid time | bd/Dolt; BDP core events | kept in metadata (bd `metadata.bdlg.*`) or `annotations` (BDP); bd native `created_at`/`closed_at` only on create/close, pending verification that `bd import` honors them | store-assigned commit time; original kept beside `occurred_at` | store time | current state only | delete; history may retain content | `valid-time.degraded`, `recorded-time.reassigned`, `erasure.incomplete` |
| Valid time, no recorded-time preservation | SQL:2011 application-time tables; XTDB loading into a store with later transactions; Datomic-style application time | native | store-assigned; original kept as a column or metadata | store time | native valid periods where per-element rows exist | store-dependent | `recorded-time.reassigned`, `recorded-time.monotone-violation` |
| Monotone-only recorded-time preservation | XTDB v2.2+ `SYSTEM_TIME` backfill into an empty store (or one whose latest transaction precedes the archive's first `recorded_at`) | native | preserved by replaying `index/recorded.jsonl`: one transaction per distinct `recorded_at`, strictly increasing | one final transaction at load time, reported | native, per-element rows | `ERASE` | `recorded-time.null` only |
| Full bitemporal | none surveyed | native | native | store time | native | native | none |

The corollary from the research holds: an archive loaded **after** newer transactions exist
keeps valid time but loses recorded-time preservation — row 3 degrades to row 2 for the whole
load, never silently for part of it.

**Loss report contract** (`loss-report.schema.json`; example `examples/valid/loss-report.json`):

- Every load — dry run and apply — emits one report naming the archive (`manifest_sha256`,
  `format_version`), the loader (`name`, `version`, loader capabilities), the target and the
  mode. A dry run and the apply of the same plan report the same losses.
- One `losses[]` entry per event, identity member and code: `{event_id, code, member (JSON
  Pointer), disposition (degraded | reassigned | approximated | dropped), stored_as, detail}`.
  Losses of non-identity members (`extracted_at`, `confidence`, `evidence`, `annotations`) never
  change state and are counted in `summary.by_code` only.
- A loader **refuses** (no load, an error rather than a report) on an unknown required
  capability, an id collision, or a schema-invalid event. `erasure.incomplete` is a loss that the
  apply envelope must surface for operator acknowledgement.

Core loss codes:

| Code | Meaning |
|---|---|
| `valid-time.degraded` | `occurred_at` kept outside native valid time (metadata, annotations) or nowhere |
| `valid-time.missing` | `occurred_at` was null; the store used recorded or load time |
| `precision.lost` | `occurred_precision` could not be kept |
| `recorded-time.reassigned` | the store assigned its own transaction time |
| `recorded-time.null` | a null `recorded_at` received load time |
| `recorded-time.monotone-violation` | monotone replay impossible (target has later transactions) |
| `field.unsupported` | a field has no place in the store |
| `phase.approximated` | a phase or `retire` mapped onto a nearby native state |
| `capability.ignored` | an optional archive capability was ignored |
| `erasure.incomplete` | erased content may survive in store history |
| `edge.dangling-target` | an edge target is not present in the store |
| `provenance.dropped` | `provenance` could not be kept |

Loaders may add codes as `x-<name>`.

### 9. Worked loader mappings

These are mapping tables, not implementations. Store-side SQL and keys are illustrative and are
fixed by the respective loader beads.

#### bd / Dolt (loader #1)

Occurrence time goes to `metadata.bdlg.<facet>` = `{occurred_at, recorded_at, event_id}`, where
`<facet>` is `created`, `phase`, `field:<name>`, `label:<label>` or `edge:<type>:<group>/<id>`,
holding the latest applied event of that facet. Native `created_at`/`closed_at` are used only on
create and close, pending the open verification that `bd import` honors supplied values.

| BeadLog | bd operation | Time handling | Loss |
|---|---|---|---|
| `create` | create through `bd import` (via `import_labeled`, which adds identity labels) | `created_at` ← `occurred_at` if honored; `metadata.bdlg.created` | `valid-time.degraded` if not honored; `recorded-time.reassigned` |
| `set` | `bd update` of the field; `metadata.<key>` for `metadata.<key>` | `metadata.bdlg.field:<name>` | `valid-time.degraded`, `recorded-time.reassigned` |
| `label_add` / `label_remove` | `bd label add` / `remove` | `metadata.bdlg.label:<label>` | as above |
| `edge_add` / `edge_remove` | `bd dep add` / `remove` with the edge type | `metadata.bdlg.edge:…` | as above; `edge.dangling-target` |
| `comment` | `bd comments add` | comments carry no metadata | `valid-time.degraded` with `stored_as: null` |
| `phase` | `bd update --status`; `bd close --reason`; reopen | `closed_at` ← `occurred_at` on close if honored; `metadata.bdlg.phase` | as above |
| `retire` | close with the reason and `metadata.bdlg.retired = {successor, reason}` | as `phase` | `phase.approximated` |
| `delete` | `bd delete` | — | `recorded-time.reassigned` |
| `tombstone` | scope `bead`: `bd delete`; scope `events`: remove the erased content where bd has a verb for it | Dolt history still holds it; the loader never runs `bd compact`/`flatten` or Dolt GC | `erasure.incomplete` |
| `checkpoint` | seeds an absent bead via `bd import`; otherwise ignored | — | none |

Reading bd back (the baseline BeadLog) uses the bulk `events` table, `dolt_log` and first-parent
`chore(merge)` commits, as the platform ADR §5 requires, and re-emits stored `event_id`s.

**As implemented (`bh-b8aur`, bd 1.3.0).** The loader is `beadhive.backfill_bd`; the
current-state reader is the `bd` strategy in `beadlog_sources.bd`. Measured on a scratch store:

- `bd import` honors supplied `created_at`, `closed_at`, `started_at` and the `created_at` of
  dependencies and comments, so a **new** bead keeps those valid times natively and reports no
  `valid-time.degraded` for them. `bd update`, `bd close`, `bd dep add` and `bd comments add`
  stamp the store's own time, so every change to an existing bead degrades to metadata.
- `metadata.bdlg` is one nested document. `bd update --metadata` replaces a top-level key
  whole, so the loader always rewrites the full document. Each entry holds the event's whole
  identity (`event_id`, `kind`, both times, `source`, `provenance`, and `actor`,
  `occurred_precision`, `lamport` and `prev` when set). Every kind except `set` also stores
  `data`; a `set` takes the field's current value. The reader re-emits an entry only when its
  recomputed id matches and it agrees with the row. Comments get their own `comment:<event id>`
  entries, so they round-trip too.
- bd keeps current state only. A batch event that decides no facet is reported `dropped` as
  `x-bd.superseded`. Other bd-specific codes: `x-bd.default-filled` (bd fills `priority` 0),
  `x-bd.title-required`, `x-bd.labels-added` (`bd import` adds the identity triplet),
  `x-bd.unknown-status`, `x-bd.missing-bead`, `x-bd.foreign-group`, `x-bd.redacted`,
  `x-bd.erased`, `x-bd.deleted`, and `x-bd.non-identity` (counted in `summary.by_code` only).
- `delete`, and bead-scope `tombstone` deletion, run `bd delete --force` only when the loader
  is built with `delete_enabled=True`. That is off by default; otherwise they are reported
  dropped (`x-bd.delete-disabled`, or `erasure.incomplete` for tombstones) and nothing is
  deleted. Undoing a `create` closes the bead with reason `backfill-rollback`.
- Called with bare events, as the apply envelope calls it, the report's `archive` block carries
  the SHA-256 of the batch's sorted event ids. `beadlog.load_archive` replaces it with the
  archive's manifest digest.

#### XTDB v2 (first bi-temporal target; spike `bh-5wu0u` GO)

*Amended 2026-10-31:* the spike verdict is in. The table below is the pre-spike mapping; where it
disagrees with the [Amendment 2026-10-31](#amendment-2026-10-31-xtdb-spike-findings), the
amendment governs (see [A8](#a8-what-this-supersedes-in-8-and-9) for the row-by-row list).

| BeadLog | XTDB v2 | Notes |
|---|---|---|
| bead | a document in table `beads`, `INSERT INTO beads RECORDS {…}` with nested `fields` (struct) | `_id` = JCS of the bead ref, e.g. `{"group":"urn:uuid:…","id":"bh-ex1"}`; schemaless, types inferred |
| `create` | `INSERT INTO beads RECORDS {_id, _valid_from: occurred_at, type, phase: 'open', fields}` | |
| `set` | `UPDATE beads FOR PORTION OF VALID_TIME FROM occurred_at TO <end> SET fields = <folded fields with the change>` (or a nested-field update if the spike finds one) | **never `PATCH`**: absent or null keys keep old values, so it cannot clear a field. `<end>` = `occurred_at` of the earliest already-loaded later assignment of the same facet, else open-ended — so an out-of-valid-order replay does not overwrite later values |
| `label_add` / `label_remove` | rows `(bead_id, label)` in table `labels`, `_id` = JCS of `{bead, label}`; add = `INSERT … _valid_from`; remove = `DELETE FROM labels FOR PORTION OF VALID_TIME FROM occurred_at TO <end>` | each element keeps its own valid periods; a nested list would version as a whole |
| `edge_add` / `edge_remove` | rows `(from, type, to)` in table `edges`, `_id` = JCS of `{bead, type, target}`, as for labels | no foreign keys: the loader checks dangling targets itself (`edge.dangling-target`) |
| `comment` | `INSERT INTO comments RECORDS {_id: event id, bead_id, body, actor, _valid_from}` | |
| `phase` / `retire` | `UPDATE beads FOR PORTION OF VALID_TIME FROM occurred_at TO <end> SET phase = …` (+ `successor` for retire) | |
| `delete` | `DELETE FROM beads FOR PORTION OF VALID_TIME FROM occurred_at` | history stays readable via `FOR ALL VALID_TIME` |
| `tombstone` | `ERASE FROM <table> WHERE …` over `beads`, `labels`, `edges`, `comments` for scope `bead`; `ERASE FROM comments WHERE _id IN (…)` for listed events | removes across all valid and system time |
| `checkpoint` | seeds an absent bead with an `INSERT`; otherwise ignored | |
| `occurred_at` null | `_valid_from` = `recorded_at`, else load time | `valid-time.missing` |
| `recorded_at` | replay `index/recorded.jsonl`; one transaction per distinct `recorded_at` declaring `SYSTEM_TIME = <recorded_at>` (v2.2+ "initial backfill"; must be strictly later than the latest completed transaction) | null `recorded_at` → final transaction at database time (`recorded-time.null`); a target with later transactions → no `SYSTEM_TIME` at all (`recorded-time.monotone-violation` for every event) |

Unverified, for the spike: whether `INSERT` with a later `_valid_from` closes an earlier row; pgwire
type fidelity for nested structs, lists and timestamps from Python; load cost at `bh` scale.

### 10. Erasure, retention and redaction

BEAD_GROUPS draws the line: **retention** removal is a per-store fact that does not propagate,
**erasure** of content that must not exist propagates to every copy.

- **Retention** is not an event. An archive whose history for a bead starts later than the bead
  holds the earliest kept state as a checkpoint and declares `checkpoint-seeded`. An archive
  replica may keep more history than the working store.
- **Erasure** is a `tombstone`. Writers drop the erased events' content from every part and
  index; the tombstone carries ids, a reason code and nothing of the content; merge re-applies
  it to any copy that still has the content. Erasure is irreversible in the format.
  *Known limitation:* an erased event's id is a hash of its identity material, so for
  low-entropy content an attacker who knows the other members could confirm a guess against the
  listed id. Use scope `bead` (which lists no ids) when that matters.
- **Delete** (the bead should not exist) and **retire** (the bead is superseded) are ordinary
  lifecycle events; neither removes history.
- **Redaction** is an export-time disclosure policy, not a fact about the bead, and it does not
  propagate as deletion. The manifest declares `redaction.applied`, the `policy`, and what was
  `withheld` (member pointers such as `/data/body`, event and bead counts). Each affected event
  keeps its original id and lists the withheld members in `redacted`; those members are absent.
  Following BEAD_GROUPS, a bead citing a private one can be published with the private edge's
  `target` withheld instead of hiding the whole bead. An applied redaction makes `redaction` a
  required capability. In a merge, an unredacted copy of an event supersedes a redacted one;
  between two redacted copies the result keeps every member present in either and `redacted`
  becomes the intersection of their pointer lists.

### 11. Versioning and compatibility

- `format_version` is semver. The schema directory is per `MAJOR.MINOR`
  (`docs/schemas/beadlog/v0.1/`). Readers accept every `PATCH` of a `MAJOR.MINOR` they know;
  `PATCH` releases only clarify prose and examples and never change a schema's accepted set.
- **Before 1.0** a `MINOR` bump may break: a reader refuses an unknown `0.MINOR`.
- **From 1.0** a reader refuses an unknown `MAJOR`; it may read a newer `MINOR` of a known
  `MAJOR` if it implements every capability in `capabilities.required`. New `MINOR` features that
  change meaning (kinds, envelope members) are therefore always behind a new required
  capability; additive optional features use optional capabilities or `annotations`.
- **The id rule is frozen** for all of `0.1.x`, and from 1.0 for a whole `MAJOR`. Changing the
  projection, the admissible values or the hash is a new `MINOR` before 1.0 and a new `MAJOR`
  after, and archives of the two cannot be union-merged by id.
- Every archive embeds its own schemas under `schema/`, so it remains checkable without
  network access.

## Alternatives considered

- **JSON Patch deltas (RFC 6902).** Array-index operations (`/labels/3`) are order-fragile and
  not idempotent: replaying or merging two logs changes the meaning of indices. Rejected (also in
  the platform ADR).
- **JSON Merge Patch (RFC 7396).** Cannot add one element to a set (arrays are replaced whole),
  and uses `null` to mean "remove", so a field cannot be set to null and an absent key is
  indistinguishable from "unchanged". Rejected.
- **Post-image journal** (bd v0 `EventRecord`, BDP `representation: postimage`). Each record is
  the whole bead after a change: no deltas to merge, no occurrence time, and the diff between
  consecutive images is lossy (who changed which field when two edits land together). Rejected
  as the event model; kept as the `checkpoint` kind, which BDP-style and bd loaders want as a
  starting state.
- **CloudEvents as the event format.** CloudEvents fixes transport metadata (`id`, `source`,
  `type`, `time`) but its `id` is producer-assigned rather than content-derived, and it has one
  time attribute. BeadLog events can be wrapped as CloudEvents for transport (`type` =
  `dev.beadhive.beadlog.<kind>`, `subject` = the bead ref, `time` = `occurred_at`) without the
  format depending on it.
- **Producer-assigned ids (UUIDs, store sequence numbers).** Store ids and sequences are
  source- or replica-local (Beads v0 `seq`, BDP `id`), and random ids break idempotent
  re-extraction. Content addressing follows git-bug's operation ids.
- **Ids over the whole event, including `extracted_at` and `confidence`.** Re-extraction or a
  re-scored judgment would mint new ids and duplicate events. Rejected.
- **Full RFC 8785 including floats.** Correct but needs ECMAScript number formatting in every
  implementation, the usual source of cross-language mismatches; nothing in identity material
  needs a fraction. Rejected in favor of the integer-only rule.
- **One time field, or two.** No surveyed store has all three axes, and conflating valid and
  recorded time loses exactly what BEAD_GROUPS requires. Rejected.

## Consequences

- `bh-lycrj` can implement ids, merge and fold from this document alone and must reproduce
  `id-vectors.json`; `tests/test_beadlog_schemas.py` already holds an independent serializer
  that does.
- Loaders are declarative about what they lose; the bd loader's first archive of `bh`
  (`bh-4kec3`) will produce a loss report dominated by `valid-time.degraded` and
  `recorded-time.reassigned`, which is the honest picture of bd today.
- Recorded-time preservation into XTDB works only for a fresh (or older) store. Incremental loads
  after the first keep valid time and report the recorded-time loss.
- The integer-only rule means producers must stringify fractional values (`"0.5"`) in fields and
  metadata. `confidence` is unaffected because it is not identity.
- Two sources reporting the same fact produce two events; deduplication is reconcile's job
  (NEW / PRESENT / NOISE / DRIFT), not the id's.
- Erasure leaves the erased events' ids behind in tombstones (see the limitation in §10).
- Adding a kind (aliases first) needs a new `MINOR`, a schema directory and a required
  capability; v0.1 readers will refuse such archives rather than misread them.

## Amendment 2026-10-31: XTDB spike findings

Spike molecule `bh-5wu0u` returned **GO** and decision `bh-psu1b` adopts XTDB v2 (2.2.x) as the
first bi-temporal BeadLog loader. This amendment records the spike evidence as decisions before
the loader is built. It changes **no** envelope member, kind, id rule or schema (see
[A7](#a7-schema-and-package-impact)); it fixes loader and index semantics that
[§7](#7-capabilities), [§8](#8-loader-degradation-and-the-loss-report) and
[§9](#9-worked-loader-mappings) left open. Where this amendment and the XTDB table in §9 or the
store-class table in §8 disagree, **the amendment governs** ([A8](#a8-what-this-supersedes-in-8-and-9)).
Each item cites the spike document and section that supports it.

| Spike | Document | Settles |
|---|---|---|
| `bh-o1x4r` | [store landscape](../spikes/bh-o1x4r-beadlog-store-landscape.md) | Why XTDB is the only surveyed candidate that natively honors both time axes; bd `import` and Dolt `--date` limits (unchanged) |
| `bh-w56ts` | [bead shape](../spikes/bh-w56ts-xtdb-bead-shape.md) | Table layout (b), `_id` rule, write and read patterns, type contract |
| `bh-cntr0` | [operational fit](../spikes/bh-cntr0-xtdb-operational-fit.md) | Footprint, deployment posture, two-package split, `beadlog.store` manifest sketch |
| `bh-3jp2g` | [bitemporal load](../spikes/bh-3jp2g-xtdb-bitemporal-load.md) | Monotone `SYSTEM_TIME` rules, index grouping, nulls, same-instant writes, `ERASE` |

### A1. ERASE is logical in XTDB

Evidence: [`bh-3jp2g` E7](../spikes/bh-3jp2g-xtdb-bitemporal-load.md) and its recommendation 5.
One wall-clock transaction ran `ERASE` for a single bead over `beads`, `bead_labels`,
`bead_edges` and `bead_comments`. The bead then vanished from every valid-time and system-time
read (default, `FOR VALID_TIME AS OF`, `FOR SYSTEM_TIME AS OF`, both `FOR ALL` axes, and
`BETWEEN`), and every other bead was intact. The erased title was **still in the transaction
log** (`log/LOG`, `log/REPLICA_LOG`) and in three block files straight after the `ERASE`, 150 s
later and at teardown. Compaction and log retention might remove the bytes eventually; neither
was observed.

Decisions:

1. **A loader declares its erasure kind.** The loader capability `erase` keeps its §7 meaning:
   physically removes content across all history. A store whose erase is complete only for reads
   declares the new loader capability **`erase-logical`**: the erased content is absent from
   every valid-time and system-time read, and its bytes may survive in the store's log and files
   until the store is rebuilt. A loader declares at most one of the two. The XTDB loader declares
   `erase-logical`. The loss-report `loader.capabilities` pattern already accepts the name.
2. **A logical erase is a reported loss.** The XTDB loader reports `erasure.incomplete` for each
   tombstone it applies, naming the kind in `detail`. The existing conformance rule (a store
   without `erase` reports `erasure.incomplete` for each tombstone) holds unchanged, and the
   apply envelope surfaces it for operator acknowledgement as §8 already requires.
3. **The guarantee is about reads, not bytes.** Nothing in the loader or the report may claim
   physical removal from an XTDB store.
4. **Physical purge is rebuild.** XTDB is a rebuildable projection of the BeadLog (`bh-cntr0`
   E5), so the only way to remove erased bytes is to rebuild the database from a purged BeadLog:
   1. Make the BeadLog itself purged: the tombstone is merged in and no part, checkpoint or
      index line carries the erased content ([§10](#10-erasure-retention-and-redaction)).
   2. Stop the node (the operator owns start and stop; the plugin never does).
   3. Delete the **whole** data directory, `log/` and `buffers/` both, and every copy of it. A
      stop-and-copy backup (`bh-cntr0` E5) holds the same bytes; that follows from E5 and from
      E7's finding that the bytes live in those files.
   4. Start a fresh node on an empty real-disk data directory (pinned image, `bh-cntr0`).
   5. Run the initial monotone load of the purged BeadLog ([A2](#a2-recorded-time-monotone-load-semantics)).
      About 6 minutes at `bh` scale (`bh-3jp2g` E6: 359.6 s; `bh-cntr0` E5).
   6. Check, as an operator step, that the erased text is absent from the new data directory.
      The spike did this with `grep` over the log and block files. XTDB does not attest it.
5. **Open, not decided here.** Whether a tombstone also erases **inbound** edge rows owned by
   other beads. The spike left 31 dangling `bead_edges` rows pointing at the erased bead, since
   `ERASE` did exactly what its predicate said (E7, consequence 1). Until decided, the loader
   reports them as `edge.dangling-target`. When the erased bytes leave the log and block files
   under compaction or log retention is also unmeasured (`bh-3jp2g` recommendation 5).

### A2. Recorded-time-monotone-load semantics

Evidence: [`bh-3jp2g` E4, E5, E6](../spikes/bh-3jp2g-xtdb-bitemporal-load.md) and recommendation 2.
In 2.2.0, preserving recorded time is a **one-shot, all-or-nothing property of a fresh
database**. The loader cannot recover from a mistake in place.

The capability `recorded-time-monotone-load` (§7) means an **initial load** into a **fresh
database**, with these rules:

- **Fresh database.** A fresh node (empty data directory) or an `ATTACH DATABASE` database on the
  node (own clock, S13; its storage durability is unverified, so prefer a fresh node).
- **Shape.** One `BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '<recorded_at>')` transaction
  per distinct `recorded_at` (syntax S1a; `SETTING SYSTEM_TIME` is not valid for writes). Commit
  SYNC. `COMMIT ASYNC` is allowed only if the loader then checks `xt.txs`, because it returns
  success even when the transaction aborts (S10).
- **Strict ordering, client side.** The loader checks that each transaction's `recorded_at` is
  **strictly greater** than the previous one *before* submitting. A tie or an out-of-order
  transaction aborts at `COMMIT`, is stamped with wall-clock time, and ends backfill: every later
  backfill transaction aborts too, even a retry at +1 µs (S2 to S5).
- **Future-time refusal.** The loader refuses any `recorded_at` later than the wall clock. A
  future `SYSTEM_TIME` is accepted but hides its rows from default reads until the clock passes
  it (S11e). Because the replay is ascending, checking the last one suffices.
- **Target check.** The loader refuses to start when the target has any transaction at or after
  the first `recorded_at`. That target falls to the second store class in §8 (no `SYSTEM_TIME` at
  all; `recorded-time.monotone-violation` for every event).
- **Abort means discard and reload.** On **any** abort, including a non-time abort that XTDB
  would let the load survive (S12, which silently loses that transaction's data), the loader
  fails the load, the database is discarded, and the load restarts from an empty one. It never
  retries in place.
- **Wall-clock phase last.** A plain wall-clock transaction also ends backfill (S6c, S11d), so
  everything that needs one runs after the whole replay: the null-`recorded_at` tail
  ([A4](#a4-null-recorded_at)) and any `ERASE` ([A1](#a1-erase-is-logical-in-xtdb)). The spike ran
  `ERASE` only as a wall-clock transaction after a complete load; `ERASE` inside a `SYSTEM_TIME`
  transaction was not tried.
- **Incremental loads.** Every load after the first uses plain wall-clock transactions. System
  time is the load time, valid time is still `occurred_at`, and the report declares
  `recorded-time.reassigned` for those events.
- **Cost.** One transaction per distinct `recorded_at` is the price of keeping recorded time. At
  `bh` scale it is 28,402 transactions for 59,387 ops, 359.6 s, a 552 MiB peak and 370 MB of log
  (E6). It pulls against the "batch several hundred documents" guidance of
  [`bh-cntr0` E3](../spikes/bh-cntr0-xtdb-operational-fit.md); preserving recorded time wins in
  the initial load, and batching applies to the incremental loads.

### A3. `index/recorded.jsonl` ordering and grouping rule

Evidence: [`bh-3jp2g` E6 and recommendation 3](../spikes/bh-3jp2g-xtdb-bitemporal-load.md).

- **Order is unchanged.** The index stays sorted by `(recorded_at, id)` with null `recorded_at`
  last ([§1](#1-archive-layout); the manifest schema pins `sorted_by` to `recorded_at,id`).
- **Grouping.** Consecutive index lines with **equal** `recorded_at` form one group, and one
  group is exactly one `SYSTEM_TIME` transaction. Ties across different entities are harmless
  (28,402 transactions for 59,387 ops in E6).
- **Order within a group is the loader's job.** The spike orders a group by
  `(recorded_at, entity causal order, event id)`. The index cannot carry that, because `id` is a
  hash and not causal. The loader reads each group's events through `part` and `line` and sorts
  them by `(bead, §6 ordering key)`. Statement order inside one transaction does not matter for
  writes with distinct `_valid_from` (S7), so this only affects same-instant writes, which
  [A5](#a5-same-instant-microsecond-split) handles. Ties in the §6 key stay deterministic but not
  causal. A producer that knows a causal order must express it in distinct timestamps.
- **No index change is needed.** The sort order and the one-line-per-event rule remain as in §1,
  so neither the manifest schema nor the `beadlog` index writer changes.

### A4. Null `recorded_at`

Evidence: [`bh-3jp2g` E4 (S6, S11) and recommendation 3](../spikes/bh-3jp2g-xtdb-bitemporal-load.md).

XTDB cannot store "unknown" recorded time: `SYSTEM_TIME = NULL` means wall-clock time, which ends
backfill (S6). So:

- Events with null `recorded_at` are the **tail** of the index (it sorts them last, §1). The
  loader stops the `SYSTEM_TIME` replay at the first null and loads the tail afterwards, in
  wall-clock transactions, reporting `recorded-time.null` (§8) for each. The spike suggested
  leaving such events out of the index; this ADR keeps them listed, since the tail is trivially
  detectable and the index contract (one line per event) stays unchanged.
- Every such write carries an explicit `_valid_to`: the `occurred_at` of the next known version
  of the same entity, or open-ended when there is none. Without it, a wall-clock write that is
  earlier in valid time **replaces the later versions** in current system time (S11, the clobber
  already seen in `bh-w56ts` E7.2). The bound keeps them.
- The alternative of declaring `recorded_at := occurred_at` with provenance `derived` is a
  producer-side choice, made by an extractor (as the spike did for snapshot-derived rows). The
  loader never invents a recorded time.

### A5. Same-instant microsecond split

Evidence: [`bh-3jp2g` S7 and recommendation 3](../spikes/bh-3jp2g-xtdb-bitemporal-load.md); operator
decision, 2026-10-31.

Within one transaction, two writes to the same entity (same table, same `_id`) with the same
`_valid_from` leave **only the last**; the earlier one is gone from every read, including
`FOR ALL SYSTEM_TIME` (S7). `bh` has 136 such cases. The spike offered two options, last-writer-wins
or a split, and the operator chose the **split**:

- Within a group, after the §6 ordering of [A3](#a3-indexrecordedjsonl-ordering-and-grouping-rule),
  number each entity's writes that share one `occurred_at` from `k = 0`. Write `0` stays in the
  group's transaction. Write `k ≥ 1` gets its **own transaction** at
  `SYSTEM_TIME = recorded_at + k µs` with
  `_valid_from = occurred_at + k µs`. (Reading "k-th" as zero-based is this amendment's
  clarification of the decision.)
- Split transactions follow the group's transaction and stay strictly increasing, so A2 holds.
  Microsecond values are accepted (S1e).
- **Precondition.** `recorded_at + k µs` must stay strictly below the next distinct
  `recorded_at` of the replay, otherwise the loader refuses before submitting. The spike's
  "cannot reach the next second" relies on `bd` timestamps having one-second resolution; another
  source may not.
- **Loss.** The stored times of a split write differ from the event's. The loader reports a loader
  code `x-xtdb.us-split` (the loss-report pattern allows `x-<name>`), disposition `approximated`,
  once for `/occurred_at` and once for `/recorded_at`, with `stored_as` holding the shifted
  value. The event id is unaffected because identity members are not rewritten in the archive.
- **No new field.** The split is derived from the index group and the events, so neither the
  index line nor the event envelope gains a marker.

### A6. Two-package XTDB loader and the `beadlog.store` capability

Evidence: [`bh-cntr0` recommendation 2](../spikes/bh-cntr0-xtdb-operational-fit.md); layout from
[`bh-w56ts`](../spikes/bh-w56ts-xtdb-bead-shape.md).

The XTDB loader is two packages, following
[`package-class-library-vs-plugin-adr.md`](package-class-library-vs-plugin-adr.md):

| Package | Class | Owns | Depends on |
|---|---|---|---|
| `packages/beadlog-xtdb` | Library; never imports `beadhive` | BeadLog events to the layout (b) tables and back as as-of reads; psycopg connection setup; transit writer and reader; single-type column coercion; version gate; dangling-edge report | `beadlog`, `psycopg[binary]>=3.2,<4`, an exactly pinned `transit-python2` (`==0.8.321` in the spike) or an in-package transit writer |
| `packages/beadhive-beadlog-xtdb` | Plugin (`plugin.json`) | Manifest, config schema, `doctor` and `status`, and binding the loader into `bh backfill` through the capability below | `beadhive`, `beadlog-xtdb` |

- Both are `Private :: Do Not Upload`. The public extra is `beadhive[xtdb]`. Root resolves the
  plugin lazily, after manifest selection, as `beadhive-tracker-github` does for `tracker.adapter`.
- **`beadlog.store`, API 1**, is a new plugin capability: the seam through which `bh backfill`
  obtains a BeadLog store loader. This settles the name that `bh-cntr0` left to this ADR.
  Namespace `plugins.beadlog-xtdb`, host-owned, `enabled` default **false**. The loader returned
  through it must pass the `beadlog.testing` loader-conformance kit.
- **Layout (b)** ([`bh-w56ts`](../spikes/bh-w56ts-xtdb-bead-shape.md)): tables `beads`
  (`_id` = bead id), `bead_labels` (`"<bead>|<label>"`), `bead_edges` (`"<from>|<type>|<to>"`),
  `bead_comments` (comment id). More than one group uses `"<group_id>:<bead_id>"`. Each document
  is one transit parameter; reads use `SET fallback_output_format='transit'`; timestamps go as
  typed instants; `_type` is renamed `bd_record_type`; every column keeps one type.
- **Operations** ([`bh-cntr0`](../spikes/bh-cntr0-xtdb-operational-fit.md)): a single node, off
  unless needed, loopback bind with `XTDB_PASSWORD`, `--memory 2g`, a real-disk data directory
  (`doctor` refuses `tmpfs` and `ramfs`), a GET-based healthcheck override, a pinned image digest,
  and version gate `>=2.2.0,<2.3`. The operator owns start, stop and upgrade.

### A7. Schema and package impact

- **JSON Schemas (`docs/schemas/beadlog/v0.1/`): no change.** The erasure kind is a free-form
  loader capability name (`loader.capabilities` items match `^[a-z][a-z0-9:._-]*$`), the split is
  derived rather than stored, and `x-xtdb.us-split` is a legal loss code. No field was needed.
- **`beadlog` package follow-up** (code in merged sub-epic `bh-ebopn`, deliberately not changed
  by this amendment): add `erase-logical` to `LoaderCapability`, and teach the conformance kit
  that a store declares at most one of `erase` and `erase-logical`. **Landed** in `packages/beadlog`:
  `LoaderCapability.ERASE_LOGICAL` exists and the `beadlog.testing` conformance kit enforces that
  rule, so the XTDB loader declares `erase-logical` as a real capability, not a plain string.

### A8. What this supersedes in §8 and §9

| Where | Was | Now |
|---|---|---|
| §8, store class 3, Tombstone and Typical loss codes | `ERASE`; `recorded-time.null` only | `ERASE` (logical, [A1](#a1-erase-is-logical-in-xtdb)); `recorded-time.null`, `erasure.incomplete`, and `x-xtdb.us-split` when it fires |
| §8, store class 3, Null `recorded_at` | one final transaction at load time | wall-clock transactions after the replay, each with an explicit `_valid_to` ([A4](#a4-null-recorded_at)) |
| §9 XTDB, bead `_id` and tables | `_id` = JCS of the bead ref; tables `beads`, `labels`, `edges`, `comments` | layout (b), [A6](#a6-two-package-xtdb-loader-and-the-beadlogstore-capability) |
| §9 XTDB, `create` / `set` / `phase` / `retire` | `UPDATE … FOR PORTION OF VALID_TIME …` | a full `beads` version by `INSERT … RECORDS` with `_valid_from = occurred_at`, because `UPDATE … FOR PORTION OF` creates no row before the first version (`bh-w56ts` E7.4); an out-of-order insert needs `_valid_to` (E7.2); setting a field to null uses `UPDATE`, never `PATCH` (E7.3) |
| §9 XTDB, `label_*` / `edge_*` / `comment` | JCS ids, tables `labels`, `edges`, `comments` | add = `INSERT` with the composite `_id`; remove = `DELETE … FOR PORTION OF VALID_TIME FROM t TO NULL`; comment = `INSERT` |
| §9 XTDB, `tombstone` | "removes across all valid and system time" | removes across all **reads** only; bytes remain ([A1](#a1-erase-is-logical-in-xtdb)) |
| §9 XTDB, `recorded_at` | replay with a null tail "at database time" | [A2](#a2-recorded-time-monotone-load-semantics) to [A5](#a5-same-instant-microsecond-split) |
| §9 XTDB, "Unverified, for the spike" | open questions | answered: a later `_valid_from` closes the earlier row (`bh-w56ts` E7.1); pgwire fidelity needs transit (`bh-w56ts` E3); load cost in `bh-3jp2g` E6 |

The `checkpoint` row of §9 (seeds an absent bead, otherwise ignored) is unchanged.

## References

### Standards and specifications

- RFC 8785 JSON Canonicalization Scheme (JCS): <https://www.rfc-editor.org/rfc/rfc8785>
- RFC 6902 JSON Patch: <https://www.rfc-editor.org/rfc/rfc6902>
- RFC 7396 JSON Merge Patch: <https://www.rfc-editor.org/rfc/rfc7396>
- RFC 3339 timestamps (added by this ADR): <https://www.rfc-editor.org/rfc/rfc3339>
- RFC 6901 JSON Pointer (added by this ADR): <https://www.rfc-editor.org/rfc/rfc6901>
- RFC 7493 I-JSON (added by this ADR): <https://www.rfc-editor.org/rfc/rfc7493>
- JSON Schema draft 2020-12 (added by this ADR): <https://json-schema.org/draft/2020-12/schema>
- CloudEvents 1.0.2: <https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md>
- SQL:2011 application-time vs system-versioned periods: <https://en.wikipedia.org/wiki/SQL:2011>
- BDP core events profile: <https://raw.githubusercontent.com/gastownhall/bdp/main/specs/profiles/core/events.md>
- git-bug operation model: <https://raw.githubusercontent.com/git-bug/git-bug/master/doc/design/data-model.md>
- XTDB v2 valid time and backfill system time: <https://docs.xtdb.com/about/time-in-xtdb>,
  <https://docs.xtdb.com/reference/main/sql/txs.html>

### Store-landscape research sources (reproduced in full)

Every external source from
[`beadlog-store-landscape-research.md`](beadlog-store-landscape-research.md) (spike
`bh-o1x4r`), with its marking: **fetched** = read during research; **cited** = referenced from
prior knowledge or a search result, not read.

#### Bi-temporal stores

- XTDB — Time in XTDB (fetched): <https://docs.xtdb.com/about/time-in-xtdb>
- XTDB — SQL transactions reference (fetched): <https://docs.xtdb.com/reference/main/sql/txs.html>
- XTDB — Key concepts (fetched): <https://docs.xtdb.com/concepts/key-concepts.html>
- XTDB — Types in XTDB (fetched): <https://docs.xtdb.com/about/types-in-xtdb.html>
- XTDB — Launching XTDB v2, 2025-06-12 (fetched): <https://xtdb.com/blog/launching-xtdb-v2>
- XTDB — SQL overview (fetched): <https://docs.xtdb.com/quickstart/sql-overview>
- XTDB — Updating the past, immutability walkthrough part 3 (search result): <https://docs.xtdb.com/tutorials/immutability-walkthrough/part-3>
- XTDB — Insert and query quickstart (search result): <https://docs.xtdb.com/quickstart/insert-and-query>
- XTDB — Taking out the O(n²) blog (search result, event-log storage): <https://xtdb.com/blog/taking-out-the-o-n-squared>
- XTDB — Building a bitemp index 1: taxonomy (search result): <https://www.xtdb.com/blog/building-a-bitemp-index-1-taxonomy>
- XTDB — Dev diary Sep 24 (search result): <https://xtdb.com/blog/dev-diary-sep-24>
- XTDB — GitHub repository, MPL-2.0 (cited): <https://github.com/xtdb/xtdb>
- XTDB — 404 at research time, not read: <https://docs.xtdb.com/quickstart/control-the-timeline>, <https://docs.xtdb.com/intro/data-model>
- HoneySQL XTDB support, RECORDS syntax (search result): <https://cljdoc.org/d/com.github.seancorfield/honeysql/2.7.1325/doc/getting-started/xtdb-support>
- Clojurians #xtdb log 2025-06-13, pgwire type reports (search result): <https://clojurians-log.clojureverse.org/xtdb/2025-06-13>
- Clojurians #xtdb log 2024-09-23 (search result): <https://clojurians-log.clojureverse.org/xtdb/2024-09-23>
- Datomic forum — transacting historical data (fetched): <https://forum.datomic.com/t/strange-behavior-when-transacting-historical-data/2485>
- SQL:2011 (fetched summary): <https://en.wikipedia.org/wiki/SQL:2011>
- Dolt documentation (cited, commit `--date` semantics unverified): <https://docs.dolthub.com/>

#### Bead protocol upstream

- BDP specs index (fetched): <https://github.com/gastownhall/bdp/blob/main/specs/README.md>
- BDP core events profile (fetched): <https://raw.githubusercontent.com/gastownhall/bdp/main/specs/profiles/core/events.md>
- Beads upstream (cited, searched for occurrence-time plans; none found): <https://github.com/steveyegge/beads>

#### Operation-log prior art

- git-bug data model (fetched): <https://raw.githubusercontent.com/git-bug/git-bug/master/doc/design/data-model.md>
- Automerge storage (cited, not fetched): <https://automerge.org/docs/under-the-hood/storage/>
- Fossil tickets (cited, not fetched): <https://fossil-scm.org/home/doc/trunk/www/tickets.wiki>

#### Envelope, delta and canonicalization standards

- CloudEvents 1.0.2 spec (fetched): <https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md>
- RFC 7396 JSON Merge Patch (fetched): <https://www.rfc-editor.org/rfc/rfc7396>
- RFC 6902 JSON Patch (fetched): <https://www.rfc-editor.org/rfc/rfc6902>
- RFC 8785 JSON Canonicalization Scheme (cited): <https://www.rfc-editor.org/rfc/rfc8785>

#### Packaging

- Frictionless Data Package (fetched): <https://specs.frictionlessdata.io/data-package/>
- EPUB 3.3 OCF ZIP container / mimetype (fetched): <https://www.w3.org/TR/epub-33/#sec-zip-container-mime>
- Python wheel RECORD (fetched): <https://packaging.python.org/en/latest/specifications/recording-installed-packages/>
- Reproducible Builds — archives (fetched): <https://reproducible-builds.org/docs/archives/>
- File-extension collision check for `.bdl` / `.bdlg` (fetched): <https://www.fileinfo.com/extension/bdl>

### In-repo sources

- [`docs/design/backfill-platform-adr.md`](backfill-platform-adr.md) — architecture and packaging
- [`docs/design/beadlog-store-landscape-research.md`](beadlog-store-landscape-research.md) —
  store survey and measurements
- [`docs/BEAD_GROUPS.md`](../BEAD_GROUPS.md) — time axes ("Every bead has a lifecycle, and
  history is its journal"), identity ("Identity beyond readable prefixes"), erasure vs retention,
  retirement, and the occurrence-time row of the upstream gap table
- `packages/beadhive-beads-client/spec/openapi.v0.yaml` — Beads v0 `EventRecord` (post-image
  journal)
- [`docs/design/package-class-library-vs-plugin-adr.md`](package-class-library-vs-plugin-adr.md)
  — library package class for `beadlog`
- `docs/schemas/beadlog/v0.1/` — the schemas and examples this ADR specifies
