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

## Tests

`uv run --inexact --package beadlog-xtdb pytest packages/beadlog-xtdb/tests` runs the unit tests,
which use fakes and need no XTDB. `tests/test_beadlog_xtdb_int.py` is integration-marked and is
skipped unless `BEADLOG_XTDB_TEST_DSN` names a node (a single-node `ghcr.io/xtdb/xtdb` 2.2.x
container bound to `127.0.0.1`, `--memory 2g`, data directory on real disk, pinned digest).
