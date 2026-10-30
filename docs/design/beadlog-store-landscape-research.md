# BeadLog store landscape — which bead stores can load bi-temporal history

> **Status:** planning research, 2026-10-30 · **Decision record:**
> [`backfill-platform-adr.md`](backfill-platform-adr.md) · **Feeds:** BeadLog v0 format ADR
> (`bh-65jen`) and the XTDB first-target spike (`bh-5wu0u`; open verifications are closed by
> `bh-o1x4r`) · **Evidence levels** are marked per store.

## Question

BeadLog (`.bdlg`) is a portable archive of bead history: an op-log of atomic events (create,
field `set`, label/edge add/remove, comment, phase, retire, delete, tombstone, checkpoint), each
carrying `occurred_at` (valid time), `recorded_at` (source transaction time) and `extracted_at`.
Which stores can load it faithfully, how must each degrade, and which should be the first
bi-temporal target we spike?

## Method

- Measured the current bd/Dolt store on the `bh` hive (history surfaces, cost, retention).
- Read in-repo specs: Beads v0 OpenAPI `EventRecord`, `docs/BEAD_GROUPS.md` gap table.
- Read the upstream BDP core events profile.
- Read official XTDB v2 documentation (independently, after an earlier pass relied on an
  outdated claim about system time).
- Datomic, SQL:2011, git-bug, Automerge/Fossil from public docs or general knowledge — marked.

XTDB entered this research as the planner's suggestion (not an operator or repo requirement).

## Stores

### bd on Dolt — current source and first loader (measured)

| Surface | Holds | Cost / limits | Use |
|---|---|---|---|
| `bd history <id> --json` (`dolt_history_issues`) | Issue-row snapshots | ~20 s/bead; one entry per store-wide Dolt commit (bh-1b0rc.3: 32,751 entries, 6 distinct states, 50 MB); no labels/deps/metadata | Opt-in chunked gap fill only |
| `events` table | created, claimed, updated, closed, reopened, label/dependency add/remove, commented, lease_reclaimed + actor | One SQL call ~13 s, 36k rows; starts 2026-08-07; replica-local writes | Primary timeline source |
| `dolt_log` | Commit times | Starts 2026-07-25 | Recorded-time evidence |
| `bd_events_journal` | Durable journal | Off; 7-day/100k floor; per replica | Unusable |
| `provenance_events` | bead→sha/PR/work-id | Empty; append-only, idempotent | Future commit-link sink |
| `bd export` / `bd import` | Current state / upsert by id or external_ref | ~11 s; raw import skips identity labels (`import_labeled` adds them) | Reader snapshot / loader create path |

Time model: commit time only. Loader degrades occurred_at to `metadata.bdlg.*`; native
`created_at`/`closed_at` only on create/close (pending verification that `bd import` honors them).
History before 2026-07-25 is already lost; compaction/flatten would lose more.

### Beads v0 service API `EventRecord` (read in spec)

Post-image journal; ops create/update/close/delete/dep_add/dep_remove/comment; `seq` gapless but
replica-local; `ts` explicitly not monotone in `seq`; no occurrence time. Kinds map ~1:1 to
BeadLog; ids cannot be cross-store. Role: future reader source.

### BDP upstream core events profile (read in spec)

Envelope: source-local `id`, `source`, `time` (recording time, not a total order), `origin`
(write|observation), `revision`/`previousRevision`, `representation` (postimage|delta);
operations create/update/delete Bead and Link; advertised retention. **No occurrence time, no
historical import, nothing planned upstream.** Role: likeliest future loader; BeadLog aligns
loosely (`provenance`↔`origin`, checkpoints↔post-images) without depending on field names;
occurred_at would degrade to `annotations` with a loss report.

### XTDB v2 (official docs; planner-introduced candidate)

- Stable since 2025-06-12; MPL-2.0; minor releases through at least v2.2.
- Temporal columns `_valid_from`/`_valid_to`, `_system_from`/`_system_to` (closed-open periods).
- **Valid time is user-set:** on INSERT via `_valid_from`/`_valid_to`; on UPDATE/DELETE/PATCH via
  `FOR PORTION OF VALID_TIME FROM … TO …` (not via `SET`).
- **System time is database-maintained, with one exception (v2.2+):** a transaction may declare
  `SYSTEM_TIME = <ts>` "for an initial backfill", which must be strictly later than the latest
  completed transaction or the transaction aborts.
- History reads: `FOR VALID_TIME AS OF / BETWEEN`, `FOR ALL VALID_TIME`, same for `SYSTEM_TIME`,
  combinable.
- Built-in WITHOUT OVERLAPS on the bitemporal rectangles; UPDATE closes prior periods
  (documented). INSERT with a later `_valid_from` closing an earlier row is inferred, unverified.
- `ERASE FROM … WHERE` removes across all valid and system time (GDPR use case).
- `PATCH` is an upsert merge where absent/null keys keep old values — cannot clear a field.
- **Documents:** schemaless tables created on first insert; rows are documents inserted with
  `INSERT INTO t RECORDS {…}`; first-class nested types (structs, maps, lists, sets); column types
  inferred and widened; every row needs `_id`; no foreign keys; no uniqueness beyond `_id`.
- Postgres wire protocol access. Community reports (2025) of type-fidelity gaps: nested results
  returned as JSON, array/temporal type round-trip issues — needs spike verification.

### Datomic-style (one forum thread)

`:db/txInstant` can be backdated only if not earlier than the latest transaction — history loads
are bulk loads into a fresh database. Same monotone constraint as XTDB's backfill system time.

### SQL:2011 temporal tables (standard; general knowledge)

Application-time periods (user-supplied) vs system-versioned tables (engine-assigned). The
reference model: occurred_at → application time; recorded_at → system time (usually provenance
only).

### Non-database prior art

git-bug (design doc): entity id = hash of first operation, operation ids = content hashes,
Lamport ordering, wall clock display-only — source of BeadLog's content-addressed ids and optional
`lamport`/`prev`. Automerge, Fossil tickets (general knowledge): content-addressed append-only
change logs merge cleanly.

## Matrix

| Store | Evidence | Valid ("happened") time | Recorded time on load | Bulk history read | Role |
|---|---|---|---|---|---|
| bd / Dolt | measured | ✗ (metadata fallback) | store-assigned | `events` table only | Source + loader #1 |
| Beads v0 API | spec | ✗ | replica-local seq/ts | post-image journal | Future reader |
| BDP | spec | ✗ (unplanned) | store-assigned; retention advertised | changefeed | Likeliest future loader |
| XTDB v2 | official docs | ✓ user-set | ✓ monotone-only (v2.2+ backfill) | native FOR ALL VALID/SYSTEM_TIME | First bi-temporal target candidate |
| Datomic-style | forum thread | application-level | ✗ (monotone bulk load only) | built-in | Manifest flag lesson |
| SQL:2011 stores | standard | ✓ application time | ✗ system time | store-dependent | Reference model |
| git-bug | design doc | ✗ (Lamport + wall clock) | n/a | git | Id/ordering model |

## Design implications for BeadLog v0

1. Three time fields — no store has all three axes natively.
2. Content-addressed event ids — every store's native ids/seqs are source- or replica-local.
3. Atomic op-log with set add/remove — matches bd `events`, BDP link ops, and per-element
   valid periods in a bi-temporal store.
4. Checkpoints — snapshot-oriented loaders (bd, BDP post-images) want a starting state.
5. Capabilities + loss report — stores differ in what they can honor; degradation is declared.
6. `recorded-time-monotone-load` + optional `index/recorded.jsonl` — recorded-time preservation
   is monotone-only wherever it exists, so loaders need a global recorded order and the archive
   must say when preservation is only possible into an empty store. Corollary: an archive loaded
   *after* newer transactions exist keeps valid time but loses recorded-time preservation.
7. Tombstones — erasure must reach archives (BEAD_GROUPS); maps cleanly to XTDB `ERASE`.
8. Document shape — store the bead as a nested document but split labels and edges into their
   own rows/tables in stores where nested values version as a whole, so each element keeps its
   own valid period.

## Open verifications (spike scope)

- Does `bd import` honor supplied `created_at`/`closed_at`?
- Dolt commit `--date` semantics (believed to be commit metadata, not valid time).
- XTDB: does INSERT with a later `_valid_from` close the earlier row? What is the documented
  `system_time_start`-style legacy import option, if any, vs `SYSTEM_TIME` backfill?
- XTDB pgwire type fidelity from Python (nested structs, lists/sets, timestamps).
- XTDB load cost and footprint at bh scale (~9k beads, 36k+ events) on the factory host.

## Recommendation

Spike **XTDB v2 as the first bi-temporal BeadLog target**. It is the only surveyed store that
natively honors both axes (user-set valid time; recorded time via monotone backfill), reads
history natively at any valid/system cut, maps tombstone to `ERASE`, accepts heterogeneous bead
types as schemaless nested documents, and is MPL-2.0 and stable. Risks to settle: JVM service
footprint, pgwire type fidelity, the monotone constraint for incremental loads, and the absence
of foreign keys/uniqueness beyond `_id` (loader-side checks).

## Sources

Every external source consulted during the 2026-10-30 planning research. **Fetched** = page
read during research; **cited** = referenced from prior knowledge or a search result, not read.

### Bi-temporal stores

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

### Bead protocol upstream

- BDP specs index (fetched): <https://github.com/gastownhall/bdp/blob/main/specs/README.md>
- BDP core events profile (fetched): <https://raw.githubusercontent.com/gastownhall/bdp/main/specs/profiles/core/events.md>
- Beads upstream (cited, searched for occurrence-time plans; none found): <https://github.com/steveyegge/beads>

### Operation-log prior art

- git-bug data model (fetched): <https://raw.githubusercontent.com/git-bug/git-bug/master/doc/design/data-model.md>
- Automerge storage (cited, not fetched): <https://automerge.org/docs/under-the-hood/storage/>
- Fossil tickets (cited, not fetched): <https://fossil-scm.org/home/doc/trunk/www/tickets.wiki>

### Envelope, delta and canonicalization standards

- CloudEvents 1.0.2 spec (fetched): <https://github.com/cloudevents/spec/blob/v1.0.2/cloudevents/spec.md>
- RFC 7396 JSON Merge Patch (fetched): <https://www.rfc-editor.org/rfc/rfc7396>
- RFC 6902 JSON Patch (fetched): <https://www.rfc-editor.org/rfc/rfc6902>
- RFC 8785 JSON Canonicalization Scheme (cited): <https://www.rfc-editor.org/rfc/rfc8785>

### Packaging

- Frictionless Data Package (fetched): <https://specs.frictionlessdata.io/data-package/>
- EPUB 3.3 OCF ZIP container / mimetype (fetched): <https://www.w3.org/TR/epub-33/#sec-zip-container-mime>
- Python wheel RECORD (fetched): <https://packaging.python.org/en/latest/specifications/recording-installed-packages/>
- Reproducible Builds — archives (fetched): <https://reproducible-builds.org/docs/archives/>
- File-extension collision check for `.bdl` / `.bdlg` (fetched): <https://www.fileinfo.com/extension/bdl>

### In-repo sources

- `packages/beadhive-beads-client/spec/openapi.v0.yaml:7825-7930` — Beads v0 `EventRecord`
- `docs/BEAD_GROUPS.md` — time axes (:178-181), identity (:190-214), erasure vs retention
  (:132-136), retirement (:174-176), upstream gap table (:610-670)
- `.beads/issues.jsonl` — bead record shape (field census)
- `docs/design/bead-commit-linkage-contract.md`, `docs/design/bead-mention-correlation-yield-adr.md`,
  `docs/design/run-journal-correlation-contract.md`, `docs/design/jev-adoption-tracks-proposal.md`
  (:214-240), `docs/design/jev-decision-engine-spike-matrix.md`
- `src/beadhive/complexity_backfill.py`, `scripts/backfill_commit_linkage.py`,
  `scripts/bead_commit_correlation.py`, `src/beadhive/git_linkage.py`, `src/beadhive/bd.py`,
  `src/beadhive/hive_ready.py`, `src/beadhive/doctor.py`, `src/beadhive/work_merge.py:1304-1333`
- `beadhive/skills: skills/backfill/` — reconcile.sh, GUIDE.md, references/bridges.md
- Measurements on hive `bh` (2026-10-30): `bd history --json`, `bd history --events`, `events`
  table census, `dolt_log` range, `bh bd export` coverage counts, `git log --first-parent`.
