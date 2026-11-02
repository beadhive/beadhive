# beadlog-xtdb

The BeadHive-free foundation of the XTDB BeadLog loader, per
[format ADR amendment A6](../../docs/design/beadlog-format-adr.md#a6-two-package-xtdb-loader-and-the-beadlogstore-capability).
A **library** package: it depends on [`beadlog`](../beadlog) (through its public `__all__`) and
`psycopg[binary]>=3.2,<4`, never imports `beadhive`, and is `Private :: Do Not Upload`. The
`beadlog.store` plugin that binds a loader into `bh backfill` is a separate package; the op
mapping, the monotone initial load and the as-of reader are built on this one.

Evidence for every rule below is in the spikes:
[`bh-w56ts`](../../docs/spikes/bh-w56ts-xtdb-bead-shape.md) (layout, transit, types),
[`bh-3jp2g`](../../docs/spikes/bh-3jp2g-xtdb-bitemporal-load.md) (bitemporal load) and
[`bh-cntr0`](../../docs/spikes/bh-cntr0-xtdb-operational-fit.md) (operations, version range).

- **Connection** (`connect`, `configure_connection`): `str` goes as a typed `text` (XTDB refuses
  OID-0 parameters in DML), `int` as `int8` (psycopg's `int2` would create a `smallint` column), a
  document as one `TransitParam` (OID 16384, `INSERT INTO <table> RECORDS %s, ...` via
  `insert_records`), and reads run `SET fallback_output_format='transit'` so nested values keep
  their types. A `TransitLoader` decodes OID 16384 columns to Python values.
- **Transit** (`encode`, `decode`, `Instant`): an in-package codec for exactly the subset a bead
  document uses, so there is no `transit-python2` dependency. Writes are transit verbose JSON:
  sets as `~#set`, timestamps as `~#time/instant` (microseconds; the plain `~t` tag keeps only
  milliseconds), and every string starting with `~`, `^` or a backtick escaped with a leading
  `~`. The reader handles XTDB's non-verbose dialect with cache references, returns nested
  timestamps (`~#time/zoned-date-time`, zero seconds omitted) as aware UTC `datetime`, sets as
  `frozenset`, and keeps unknown tags as `Tagged`. Its tests pin text captured from XTDB 2.2.0.
- **Layout (b)** (`Layout`, `state_rows`, `bead_row`, `label_row`, `edge_row`, `comment_row`,
  `record_rows`, `decode_bead_row`): tables `beads` (`_id` is the bead id), `bead_labels`
  (`<bead>|<label>`), `bead_edges` (`<from>|<type>|<to>`) and `bead_comments` (the comment's
  event id); a load over several groups uses `<group_id>:<bead_id>` everywhere. `beads` holds
  `group_id`, `bead_id`, `issue_type`, `phase`, one column per BeadLog field and a `metadata`
  struct of the `metadata.<key>` fields (keys verbatim). A top-level `_type` is renamed
  `bd_record_type`, any other `_` key is refused, `null` is omitted (XTDB cannot store it), and
  timestamps are typed instants. Builders take `valid_from` / `valid_to` as given: choosing them
  (and the order of writes) is the loader's job. `record_rows` builds the same rows from a
  `bd export` record, as the spike did.
- **Type discipline** (`ColumnTypes`): XTDB widens a column to a union the first time a row brings
  another type, permanently, and range predicates on it then fail on the server. Known bd columns
  (`priority`, `due_at`, ...) are fixed; any other column is pinned by its first value; a later
  value is coerced to that type or refused with `ColumnTypeError` (never stringified).
- **Operations** (`check_version`, `require_supported_version`, `find_dangling_edges`): the
  loader is validated against XTDB `>=2.2.0,<2.3` and refuses anything else with a message saying
  what to do. XTDB has no foreign keys, so `find_dangling_edges` runs the anti-join over
  `bead_edges` and `beads` and splits `external:` targets from unresolved ones; the loader reports
  them and does not refuse the load.
- **As-of reader** (`read_state`, `read_history`, `write_baseline`): the inverse of the loader.
  `read_state(conn, extracted_at=..., valid_at=..., system_at=...)` reads the four tables
  `FOR SYSTEM_TIME AS OF` / `FOR VALID_TIME AS OF` (the current belief when no cut is given) and
  emits events that fold to that state: a `create` per bead plus the `phase` / `retire` /
  `delete` its document implies, and a `label_add`, `edge_add` or `comment` per held row.
  `read_history(conn, extracted_at=...)` reads `FOR ALL SYSTEM_TIME FOR ALL VALID_TIME`, keeps the
  store's belief (now, or as of `system_at`) and diffs each bead's document versions into
  `create` / `set` / `phase` / `retire` / `delete`, and each label or edge period into an add and,
  once ended, a remove. Every event is `provenance: observed`, `source.system: xtdb`,
  `occurred_at` = `_valid_from`, `recorded_at` = `_system_from` (a removal's is the
  `_system_to` of the `[_valid_to, inf)` rectangle it closed; a time XTDB does not hold stays
  `null`). `extracted_at` is a parameter, so the same store reads to the same ids and bytes.
  `checkpoints=True` adds a derived `checkpoint` per bead; `groups=[...]` keeps only those
  groups. `write_baseline(path, result)` writes a `.bdlg` that passes `beadlog verify` and
  serves as the `baseline` of `beadlog_sources.reconcile`. A `create` and the `phase` / `retire`
  / `delete` of the same version share an instant and a facet, so the reader moves the latter
  one microsecond later (`annotations.xtdb` keeps the stored time). Whatever a read cannot give
  back is in `ReadResult.notes`: a re-read comment has a new event id (the stored one is in
  `annotations.xtdb.comment_id`), a comment without a body, a field BeadLog cannot hold, an
  edge's `metadata`, and rows whose bead is not in the read. A state read has no history, so
  its `create` is dated at the version in force at the cut.

## Loader

`XtdbStoreLoader(connection, mode="auto", clock=...)` has the `beadlog` StoreLoader shape
(`name`, `version`, `capabilities`, `export()`, `load(events, *, dry_run)`) and passes the
`beadlog.testing` loader-conformance kit against a real XTDB 2.2.x. `load_archive(loader, path)`
loads a `.bdlg` replaying its `index/recorded.jsonl` order. `plan_load` is the pure planner behind
both: every guard and every loss is decided before anything is written, so a dry run reports
exactly what the apply reports. Design: epic `bh-wj8d7` and
[format ADR amendment A1-A5](../../docs/design/beadlog-format-adr.md#amendment-2026-10-31-xtdb-spike-findings).

- **Op mapping.** Each bead's rows equal the BeadLog fold of its loaded events. `create`, `set`,
  `phase`, `retire`, `delete` (a version with `deleted: true`) and a seeding `checkpoint` write a
  full `beads` version by `INSERT ... RECORDS` with `_valid_from = occurred_at`; a field set to
  null is absent from that version, which replaces the whole row (never `PATCH`). `label_add` /
  `edge_add` insert the composite `_id`; `label_remove` / `edge_remove` are `DELETE ... FOR
  PORTION OF VALID_TIME FROM t TO NULL`; `comment` inserts; `tombstone` is `ERASE`. An event that
  lands earlier in valid time than versions already written rewrites only the versions it
  changes, each with an explicit `_valid_to`, so nothing later is clobbered.
- **Monotone initial load** (capability `recorded-time-monotone-load`). Only into a fresh
  database: refused before any write when a loader table holds a row or `xt.txs` has a
  transaction at or after the first `recorded_at`. One `BEGIN READ WRITE WITH (SYSTEM_TIME =
  TIMESTAMP '<recorded_at>')` per distinct `recorded_at`, SYNC `COMMIT`, times checked strictly
  increasing client-side; a `recorded_at` later than the (injectable) clock is refused.
- **Same-instant split.** Within a group, each entity's writes sharing one `occurred_at` are
  numbered from `k = 0`; write `k >= 1` gets its own transaction at `recorded_at + k` µs with
  `_valid_from = occurred_at + k` µs (`x-xtdb.us-split`, for `/occurred_at` and `/recorded_at`).
  A split that would reach the next `recorded_at` is refused. Checkpoints: an assertion
  checkpoint writes no row, so it never counts toward `k`; a seeding checkpoint writes at its
  `covers_through` position, before every change of its bead, so it is always `k = 0`.
- **Wall-clock phase last.** Null-`recorded_at` events load after the replay in wall-clock
  transactions with explicit `_valid_to` (`recorded-time.null`), then every `ERASE`
  (`erasure.incomplete`; `erase-logical`: the content leaves every read, its bytes may stay in
  the log until the database is rebuilt from a purged BeadLog).
- **Incremental mode** (`auto` on a non-fresh target): wall-clock transactions,
  `recorded-time.reassigned`; events already loaded are skipped, so a reload is idempotent.
- **Abort.** A failed statement or `COMMIT`, or an aborted transaction found in `xt.txs`
  afterwards, raises `LoadAborted`: discard the database and reload. Never a retry in place,
  never a report claiming success.
- **Journal.** Every loaded event is also kept whole, with its original id, in table
  `bead_events` (`event` JSON, `valid_at`): `export()` reads it back (format ADR section 4) and an
  incremental load starts from it. The as-of reader needs only the four layout (b) tables.
- **Loss report.** Validates against `docs/schemas/beadlog/v0.1/loss-report.schema.json`. Loader
  codes: `x-xtdb.us-split`, `x-xtdb.sub-microsecond` (times finer than XTDB's µs are truncated
  for the store and kept as written in the journal), `x-xtdb.column-coerced` /
  `x-xtdb.column-refused` (one type per column), `x-xtdb.erased`, `x-xtdb.id-unrepresentable`;
  core codes include `valid-time.missing`, `precision.lost`, `field.unsupported` and
  `edge.dangling-target`.

## Tests

`uv run --inexact --package beadlog-xtdb pytest packages/beadlog-xtdb/tests` runs the unit tests,
which use fakes and need no XTDB (the loader's cover every guard with an injected clock).
`tests/test_beadlog_xtdb_int.py`, `tests/test_beadlog_xtdb_reader_int.py` (which writes a fixture log
with plain statements and checks that the reader folds to the same state at three valid-time cuts
and at system-time cuts) and `tests/test_beadlog_xtdb_loader_int.py` (the conformance kit, a fixture
log read back on both time axes, the refusals and an injected abort) are integration-marked and
skipped unless `BEADLOG_XTDB_TEST_DSN` names a node (a single-node `ghcr.io/xtdb/xtdb` 2.2.x
container bound to `127.0.0.1`, `--memory 2g`, data directory on real disk, pinned digest). The
loader tests attach a fresh database per store (`ATTACH DATABASE`).
