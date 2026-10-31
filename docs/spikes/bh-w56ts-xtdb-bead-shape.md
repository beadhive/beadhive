# Spike `bh-w56ts` — Can XTDB v2 hold every bead type schemalessly, and in which table layout?

**Bead:** `bh-w56ts` · **Seat:** `dev/xtdb-shape` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-psu1b` (adopt XTDB v2 as the first bi-temporal BeadLog loader?), and
directly on `bh-3jp2g` (bi-temporal load of bh history), which should reuse the layout, `_id`
rule and INSERT/query patterns in [Recommendation](#recommendation). Parent: epic `bh-5wu0u`.
Context: [backfill platform ADR](../design/backfill-platform-adr.md),
[BeadLog store landscape](../design/beadlog-store-landscape-research.md) (design implication 8).

## Question

Can XTDB 2.2.0 store the real shape of every bead type in a `bh bd export` (all `issue_type`
values) as schemaless documents written from Python over pgwire, and read them back field-equal?
Which table layout should a BeadLog loader use: **(a)** labels and dependencies nested in the bead
document, or **(b)** labels as `(bead, label)` rows and edges as `(from, type, to)` rows, each with
its own valid period?

Sub-questions: pgwire type fidelity from Python; column type widening on heterogeneous fields;
`_id` choice; dangling edge detection without foreign keys; whether `PATCH` can clear a field; and
whether an `INSERT` with a later `_valid_from` closes the earlier row.

**Not asked:** loading the full bi-temporal history with `SYSTEM_TIME` backfill and as-of
reproduction (`bh-3jp2g`), operational footprint at scale (`bh-cntr0`), and the BeadLog wire
format (`bh-65jen`). This spike touches valid time only as far as the layout choice needs it.

## Method

**Server.** `ghcr.io/xtdb/xtdb:2.2.0` (image `c6c1962`), started with the image's own
`config.yaml`: pgwire on 5432, `log: !Local /var/lib/xtdb/log`, `storage: !Local
/var/lib/xtdb/buffers`. Data directory bind-mounted on real disk (ext4 `/data`, not tmpfs):

```sh
docker run -d --name xtdb-spike-bh-w56ts --memory 3g --memory-swap 3g --user "$(id -u):$(id -g)" \
  -p 127.0.0.1:15432:5432 \
  -v /data/bees/beadhive/scratch/xtdb-spike/bh-w56ts/xtdb:/var/lib/xtdb \
  ghcr.io/xtdb/xtdb:2.2.0
```

**Client.** Python 3.14.6, psycopg 3.3.6 (binary), transit-python2 0.8.321, via
`uv run --no-project --with 'psycopg[binary]' --with transit-python2` (no repo dependency changes).

**Data (read-only, production-derived).** `bh-export.jsonl`: 9,088 beads from `bh bd export`.
`bh-events.json`: 36,716 rows of the bd `events` table. The production bd store was not queried.

Field census of the export: 10 `issue_type` values (table in E1); 35 top-level keys, each with a
single JSON type across the corpus; `_type` is `"issue"` on every row (it is the export's record
discriminator, not an `issue_type`); `metadata` is a struct whose only key is `git.commits` (a
string holding encoded JSON); `dependencies[]` and `comments[]` are lists of structs with a
`created_at` each; 27,347 timestamps, all `YYYY-MM-DDTHH:MM:SSZ`; longest `description` 19,583
characters; 5,190 beads contain non-ASCII text; no `null` values, empty strings or empty lists.

**Scripts** (throwaway, committed under [`scripts/bh-w56ts/`](scripts/bh-w56ts/); outputs in
[`scripts/bh-w56ts/output/`](scripts/bh-w56ts/output/)):

| Script | Does |
|---|---|
| `common.py` | Connection setup (transit param dumper, `str` dumper), document builders for both layouts, transit decoder |
| `load.py` | Loads layout (a) `beads_a`; layout (b) `beads_b`, `bead_labels`, `bead_edges`, `bead_comments`; and `bd_events` |
| `roundtrip.py` | Reads every row back and diffs field-by-field against the export, per `issue_type` |
| `fidelity.py` | Type-fidelity matrix: one table per case, value stored top-level and nested, read in both output modes |
| `widening.py` | Mixed-type columns: resulting column type, what Python reads, how predicates behave |
| `temporal.py` | Per-element valid-period queries, layout (a) equivalent, INSERT/PATCH/UPDATE semantics, dangling edges |

Layout (b) label and edge periods come from the bd `events` table where it has evidence
(`label_added`/`label_removed`, `dependency_added`/`dependency_removed`). Otherwise they start at
the snapshot: `dependencies[].created_at` for edges, and the bead's `created_at` for labels with
no add event. Events only start on 2026-08-07.

## Evidence

### E1. Every bead type loads and reads back field-equal

From `output/roundtrip.md`. **A/transit** is layout (a) read with
`SET fallback_output_format='transit'`. **B** is layout (b): the bead document joined back with
its current label, edge and comment rows.

| issue_type | beads | (a) transit: field-equal | (a) default JSON output: field-equal | (b) reassembled: field-equal |
|---|---|---|---|---|
| event | 3176 | 3176 | 3126 | 3176 |
| gate | 2241 | 2241 | 2241 | 2241 |
| task | 1217 | 1217 | 43 | 1217 |
| feature | 792 | 792 | 12 | 792 |
| bug | 750 | 750 | 35 | 750 |
| epic | 386 | 386 | 3 | 386 |
| molecule | 314 | 314 | 311 | 314 |
| chore | 188 | 188 | 3 | 188 |
| spike | 13 | 13 | 0 | 13 |
| decision | 11 | 11 | 0 | 11 |
| **total** | **9088** | **9088** | **5774** | **9088** |

The `bd_events` table (36,716 rows) also reads back with no divergences, after accounting for the
52,369 `null` fields that read back as absent (E3.3).

These results hold under three loader-side transforms, which a loader must apply and a loss report
must declare:

1. **`_type` is renamed to `bd_record_type`.** XTDB rejects the whole document with `Cannot put
   documents with columns: [_type]`, because `_`-prefixed top-level keys are reserved. The rename
   is lossless.
2. **Timestamps are sent as typed instants** (E3.1). Sent as plain JSON strings they stay `text`,
   and a column that mixes both breaks range queries (E4).
3. **Strings starting with `~`, `^` or `` ` `` are escaped with a leading `~`** (transit-json
   rule). The `text_tilde` case round-trips once escaped.

**Divergences.** There are none in (a) read as transit, or in (b). With the default JSON output
format, (a) diverges on 3,314 beads, and every divergence comes from how XTDB renders nested values
as JSON, not from what it stores:

| Field | Beads | Cause |
|---|---|---|
| `labels` | 3260 | A set is rendered as a JSON list in XTDB's internal order (`["org:beadhive", "component:intake", …]`), not the export's sorted order. Membership is equal. |
| `dependencies` | 138 | A nested timestamp is rendered as a Java ISO string that **drops `:00` seconds**: `2026-10-22T00:06:00Z` comes back as `2026-10-22T00:06Z`. It also arrives as `str`, not a timestamp. |
| `comments` | 6 | Same nested-timestamp rendering as `dependencies`. |

Snapshot/events consistency for layout (b), from `output/load.md`: 0 label anomalies. Edges had 32
`dependency_removed` events with no matching add event in the window, so their edge type is
unknown and they were skipped, plus 8 add/remove sequences that disagree with the snapshot. 14
`events.issue_id` values are not in the export (deleted beads).

### E2. Layout (a) nested vs (b) split: a per-element valid-period query

Layout (b), real data (`output/temporal.md` lines 7-95). One query returns each label's own
periods for a bead. Here `bh-q0lol.5` went through 14 review bounce cycles:

```sql
SELECT label, _valid_from, _valid_to FROM bead_labels FOR ALL VALID_TIME
WHERE bead_id = %s ORDER BY label, _valid_from
-- ('review:changes-requested', '2026-09-02T23:55:55Z', '2026-09-03T00:10:48Z')
-- ('review:changes-requested', '2026-09-03T00:16:50Z', '2026-09-03T00:27:27Z')   ... 13 periods
-- ('review:pending',           '2026-09-02T23:49:59Z', '2026-09-02T23:55:55Z')   ... 14 periods
-- ('size:l',                   '2026-08-24T20:56:02Z', None)
```

Further queries in the same output, each a single statement:

- Labels as of a past instant, passing a Python `datetime` as the `FOR VALID_TIME AS OF %s`
  parameter. The result includes `review:pending`; the current labels do not.
- Corpus-wide label durations, using `(_valid_to - _valid_from) AS held` and
  `ORDER BY held DESC`. The longest `review:pending` hold was `PT45H27S` on `bh-jksq.1`.
- Beads holding `review:pending` at 2026-10-01T00:00Z: 155.
- Removed edges with closed periods, for example `bh-c6dk.5 blocks bh-bh2h.2.2` from
  `2026-08-10T15:26:49Z` to `2026-08-10T15:59:57Z`.

Layout (a), the same question on a versioned bead document (lines 96-110). Each label change
**and** an unrelated title change writes a whole new document version: description, labels,
dependencies and comments. So the period of a label is fragmented across versions:

```sql
SELECT l.label, MIN(v._valid_from) AS vf, MAX(COALESCE(v._valid_to, TIMESTAMP '9999-12-31T00:00:00Z')) AS vt,
       COUNT(*) AS fragments
FROM temporal_a FOR ALL VALID_TIME AS v, UNNEST(v.labels) AS l(label) GROUP BY l.label
-- ('review:pending', '2026-08-02T00:00:00Z', '2026-08-04T00:00:00Z', 2)  -- 2 fragments for 1 period
```

`MIN`/`MAX` is only correct when a label was held once. A bead like `bh-q0lol.5`, which re-adds a
label 14 times, needs a gaps-and-islands window query over every version of the bead. On this
corpus, layout (a) would write about 19.2k extra whole-document versions: 15,546 + 2,332 label
events plus 1,239 + 88 dependency events, at an average document of about 1.8 KB. Layout (b)
writes about 19.2k small rows instead.

### E3. pgwire type fidelity from Python (psycopg 3.3.6)

From `output/fidelity.md`. Each value is written through a transit `RECORDS` parameter, then read
back both as a top-level column and nested in a struct. "OK" means equal to the value written.

| Case | Column type | top, JSON output | nested, JSON output | top, transit output | nested, transit output |
|---|---|---|---|---|---|
| text | text | str OK | str OK | str OK | str OK |
| 1 MiB text | text | str OK | str OK | str OK | str OK |
| unicode (em dash, CJK, astral emoji, combining mark, Hebrew) | text | str OK | str OK | str OK | str OK |
| text containing NUL (`a\x00b`) | text | str OK | — | — | — |
| `~`-leading string (escaped) | text | str OK | str OK | str OK | str OK |
| int / int 2^62 | bigint | int OK | int OK | int OK | int OK |
| float | double precision | float OK | float OK | float OK | float OK |
| bool | boolean | bool OK | bool OK | bool OK | OK (transit `Boolean` singleton) |
| timestamp, transit `~t` | timestamptz | **ms only** (µs truncated) | **str** | **ms only** | ms only |
| timestamp, transit `~#time/instant` | timestamptz | datetime OK (µs kept) | **str** `…19:25:00.123456Z` | datetime OK | OK |
| timestamp as a plain JSON string | text | str (not a timestamp) | str | str | str |
| list of str | text[] (ARRAY) | list OK | list OK | list OK | OK |
| empty list | json | `[]` OK | `[]` OK | OK | OK |
| mixed list `[1, "a", true]` | json | list OK | list OK | OK | OK |
| set of str / empty set | json | **list** (order arbitrary) | **list** | frozenset OK | frozenset OK |
| struct (key `git.commits`) / 3-deep struct / empty struct | json | dict OK | dict OK | OK | OK |
| list of structs holding a timestamp | json | **timestamp → str, `:00` dropped** | same | OK | OK |
| JSON-ish metadata as string-encoded JSON | text | str OK | str OK | str OK | str OK |
| uuid (`~u…`) | uuid | UUID OK | **str** | UUID OK | UUID OK |
| keyword (`~:blocks`) | USER-DEFINED | str | str | str | Keyword |
| interval (`_valid_to - _valid_from`) | duration | **str** `PT45H27S` (OID 11112 unknown to psycopg) | — | — | — |

Further findings from the same runs:

1. **Nested values lose types under the default output.** With the default JSON fallback output,
   nested values (structs, lists of structs, sets, and union-typed columns) come back as plain
   JSON. Nested timestamps arrive as strings, sets as lists, and nested UUIDs as strings. After
   `SET fallback_output_format='transit'`, those columns arrive as transit text (OID 16384) that
   decodes to typed values. Typed top-level columns (text, bigint, timestamptz, uuid, text[]) come
   back natively in either mode.
2. **Plain psycopg parameters fail in DML.** psycopg sends `str` with OID 0, and XTDB refuses
   untyped parameters in DML: `Missing types for args - client must specify types for all non-null
   params in DML statements`. Registering `StrDumper` for `str` fixes it. Native per-field
   parameters (`RECORDS {_id: %s, ts: %s, …}`) also work: a `datetime` keeps µs, a `list[str]`
   arrives as text[], and `Jsonb` as a struct. But psycopg sends small ints as **int2**, which
   creates a `smallint` column (E4). A whole document as one transit parameter
   (`RECORDS %s, %s, …`) avoids hand-built SQL per document shape.
3. **Null and absent are indistinguishable.** A key sent as `null` is not stored: no column is
   created (`user columns: ['keep', 'n']`), and a `null` key inside a struct is dropped
   (`{a: null, b: 1}` reads back as `{b: 1}`). A loader cannot round-trip "explicitly null" as
   distinct from "absent". The bh export has no nulls; the events table has 52,369, and they read
   back as absent.
4. **Map keys are kept verbatim.** That covers `git.commits`, `Mixed-Case Key`, `kebab-key`,
   `a$b`, `a/b` and `_under` inside structs. Top-level column names keep their case too
   (`issueType`), though unquoted SQL identifiers fold to lower case.

### E4. Column type widening on heterogeneous fields

From `output/widening.md`. XTDB widens a column to a union, reported as `json`, the first time a
row brings a different type. That is permanent for the table, and it changes how **every** row
reads:

| Mix | Resulting type | Effect on reads and queries |
|---|---|---|
| timestamptz row + text row | json | JSON output: the timestamp row now reads as `str` `2026-10-30T19:25Z`. `WHERE ts < TIMESTAMP '…'` fails with a server `ClassCastException` (`java.lang.Long cannot be cast to … ByteBuffer`). `ORDER BY ts` fails with `compare not applicable to types instant and utf8`. `ts = '2026-10-30T19:25:00Z'` matches both rows. |
| bigint + text (`2` vs `"P2"`) | json | `WHERE p < 3` fails with the same `ClassCastException`. `WHERE p = 'P2'` works. |
| int2 (psycopg default) + int8 | bigint | Numeric widening is clean. `p > 1` works. |
| text[] + list[int] + text | json | `UNNEST` still works on the list rows and skips the scalar row. Transit output wraps scalars as `["~#'", …]`. |
| struct{a:int} + struct{a:str} | json | `(m).a` returns mixed `int`/`str`. `WHERE (m).a = 1` works. |
| **real**: `events.new_value` with its embedded JSON decoded | json | 13,862 str, 7,197 struct and 15,657 absent. `(new_value).status = 'in_progress'` matches 1,936 rows. `new_value = ''` matches 7,736. |

In the bh export itself, every key has one JSON type, so nothing widens when the loader encodes
each field's type consistently. Heterogeneity across bead types shows up only as **sparse
columns** (`await_type` only on gates, `mol_type` only on molecules), which XTDB stores as absent.

### E5. `_id` choice

- A struct `_id` (`{group: 'bh', id: 'bh-1'}`) is **refused**: `Invalid ID type:
  java.util.LinkedHashMap`. The bead's `{group, id}` pair cannot be the `_id` directly.
- A missing `_id` is refused (`missing '_id'`). String, uuid (`~u…`) and keyword (`~:bh/bh-1`) ids
  are accepted, and so is a qualified string such as `bh/bh-1`.
- Bead ids are unique in the export: 9,088 distinct ids, all with prefix `bh`, 4,586 of them
  dotted children. Lookup by `_id` took 33 ms in a Python round trip.
- `ATTACH DATABASE bh_other` followed by connecting with `dbname=bh_other` gives an isolated `_id`
  namespace: `bh-1` written there was not visible from `dbname=xtdb`. Where an attached database
  is stored when no storage is configured, and how durable it is, was **not verified**.

### E6. Dangling edge targets without foreign keys

XTDB enforces no foreign keys, so the loader writes any edge, and a query finds the dangling ones.
An anti-join on layout (b) at the current valid time (`output/temporal.md` lines 163-173):

```sql
SELECT e.from_id, e.type, e.to_id FROM bead_edges AS e
WHERE NOT EXISTS (SELECT 1 FROM beads_b AS b WHERE b._id = e.to_id)
```

It returns exactly the **7** dangling targets found by the Python census of the export. Four point
outside the hive (`external:infra:gateway-read-development-deployed-v1`,
`external:beadhive-ui:gateway-read-demo-catalog-v1`). Three are malformed ids of the form
`discovered-from:bh-ku9n9.1`, where the type was spelled into the target. Layout (a) needs
`UNNEST(b.dependencies) AS d(dep)` plus `(d.dep).depends_on_id` in the anti-join and returns the
same 7 rows. There were no edges or label rows with a missing source bead.

### E7. Temporal write semantics (short answers for `bh-3jp2g`)

From `output/temporal.md` lines 111-162:

1. **An INSERT with a later `_valid_from` closes the earlier row: yes.** v1 from 08-01, then v2
   from 08-05, reads back as `v1 [08-01, 08-05)` and `v2 [08-05, ∞)`.
2. **An out-of-order INSERT clobbers later versions.** An INSERT with an earlier `_valid_from` and
   no `_valid_to` replaces every later version: inserting v0 from 07-01 after them leaves only
   `v0 [07-01, ∞)`. v1 and v2 survive only in system time. With an explicit `_valid_to` (07-01 to
   08-01), all three periods are kept. History loads must write each entity in valid-time order,
   or bound each period with `_valid_to`.
3. **`PATCH` cannot clear a field.** `PATCH INTO t RECORDS {_id: 'p', assignee: null, status:
   'closed'}` updates `status` but leaves `assignee = 'dev/x'`. `UPDATE t SET assignee = NULL`
   does clear it.
4. **`UPDATE … FOR PORTION OF VALID_TIME` does not create a row.** Run over a range before the
   row's first `_valid_from`, it is a no-op. Use INSERT with `_valid_from`/`_valid_to` to write
   history.

### E8. Footprint (single load; `bh-cntr0` owns the full measurement)

| Measure | Value |
|---|---|
| Container memory, idle after start | 431 MiB (cap 3 GiB) |
| Container memory, peak during load (sampled every 2 s) | about 907 MiB |
| Container memory after every experiment | 1.09 GiB |
| Load time, 9,088 bead docs (a), 500 per transaction | 7.96 s |
| Load time, 9,088 bead docs (b) | 5.04 s |
| Load time, 32,757 split rows (21,760 labels, 10,321 edges, 676 comments) | 12.68 s |
| Load time, 36,716 `bd_events` rows, 1,000 per transaction | 23.31 s |
| Data dir after load (inputs: 16.3 MB export + 32.9 MB events) | 112 MB |
| Data dir after every experiment (log 137 MB, buffers 143 MB) | 280 MB |
| `SELECT * FROM beads_a`, 9,088 rows, JSON / transit output | 0.92 s / 1.10 s |

## Verdict — **GO**

XTDB 2.2.0 stores all 10 bead types in the export without any schema declaration and reads all
9,088 back field-equal in either layout, under three declared, lossless transforms (`_type`
rename, typed instants, transit string escaping). What makes it work is reading with
`SET fallback_output_format='transit'` and writing whole documents as transit parameters.
Layout (b) answers per-label and per-edge valid-period questions in a single query on real data,
where layout (a) cannot. The risks are all things the loader controls: the default JSON output
mangles nested values, null and absent are indistinguishable, widened columns break range
predicates, and there are no foreign keys. None of them blocks the loader.

## Recommendation

**Use layout (b).** It follows design implication 8 of the store research. Reuse it verbatim in
`bh-3jp2g`:

| Table | `_id` | Columns | Valid period |
|---|---|---|---|
| `beads` | bead id string (`bh-q0lol.5`) | every export field except `labels`, `dependencies` and `comments`; `_type` renamed `bd_record_type`; timestamps as instants; `metadata` as a struct with keys verbatim | per bead version (BeadLog `create`/`set` events) |
| `bead_labels` | `"<bead_id>\|<label>"` | `bead_id`, `label` | one row per held period (`label_add` to `label_remove`) |
| `bead_edges` | `"<from_id>\|<type>\|<to_id>"` | `from_id`, `type`, `to_id`, `created_by`, `created_at`, `metadata` | one row per held period (`edge_add` to `edge_remove`) |
| `bead_comments` | comment id (UUID string) | `bead_id`, `author`, `text`, `created_at` | from `created_at`, open-ended |

Why not (a): sets and edges version as a whole with the document. Every label flip rewrites about
1.8 KB, unrelated field changes fragment label periods, and per-element history needs
gaps-and-islands over versions (E2). `|` is a safe separator: no bead id or label in the corpus
contains it. The deterministic composite `_id` makes a re-add a new period on the same entity
rather than a new entity.

**`_id` rule.** Use the bead id string. A struct `_id` is impossible (E5). For more than one bead
group, either put each group in its own XTDB database (verified isolation, durability unverified)
or use a qualified string `"<group_id>:<bead_id>"`, with `group_id`/`bead_id` columns for lookup.
Edge rows must then use the same form for `to_id`. UUIDv5 ids are possible but unreadable, so
defer them.

**Write pattern** (from `common.py`; works as tested):

```python
conn = psycopg.connect(DSN, autocommit=False)
conn.adapters.register_dumper(Transit, TransitDumper)   # oid 16384, json.dumps(value)
conn.adapters.register_dumper(str, StrDumper)           # XTDB refuses OID-0 params in DML
with conn.transaction():                                 # one XTDB tx per batch (500 docs)
    conn.execute("INSERT INTO beads RECORDS " + ", ".join(["%s"] * len(docs)),
                 [Transit(d) for d in docs])
# doc = {"_id": "bh-x", "title": "~~escaped if it starts with ~", "priority": 2,
#        "created_at": {"~#time/instant": "2026-10-30T19:25:37Z"},          # not "~t" (ms only)
#        "metadata": {"git.commits": "[\"…\"]"},
#        "_valid_from": {"~#time/instant": "…"}, "_valid_to": {"~#time/instant": "…"}}
# label row: {"_id": "bh-x|review:pending", "bead_id": "bh-x", "label": "review:pending",
#             "_valid_from": …, "_valid_to": …}
```

**Read pattern:**

```python
conn.execute("SET fallback_output_format='transit'")      # typed nested values; decode OID 16384
# current labels / labels as of t / every period of one label:
"SELECT label FROM bead_labels WHERE bead_id = %s"
"SELECT label FROM bead_labels FOR VALID_TIME AS OF %s WHERE bead_id = %s"   # %s = datetime
"SELECT label, _valid_from, _valid_to FROM bead_labels FOR ALL VALID_TIME WHERE bead_id = %s"
# nested field access: (col).field — e.g. (metadata)."git.commits"
# dangling targets:
"SELECT e.from_id, e.type, e.to_id FROM bead_edges e "
"WHERE NOT EXISTS (SELECT 1 FROM beads b WHERE b._id = e.to_id)"
```

**Loader contract.** It goes into the XTDB loader package and the BeadLog loss report.

1. Keep every column a single type. Coerce per field from the BeadLog schema, and never send the
   same field as both a timestamp and a string. Widening is permanent and breaks range predicates
   and `ORDER BY` (E4).
2. Rename or refuse `_`-prefixed top-level keys, and declare it in the loss report.
3. Treat `null` as absent: XTDB cannot store "explicitly null". To clear a field, write a full
   document version or use `UPDATE … SET f = NULL`. Never use `PATCH` (E7.3).
4. Check dangling edges after load with the anti-join above. Report them rather than refusing:
   the corpus has 7 legitimate ones (external targets and malformed `discovered-from:` ids).
5. For `bh-3jp2g`: write each entity's versions in valid-time order, or set `_valid_to`
   explicitly. An unbounded out-of-order INSERT erases later versions from current valid time
   (E7.2). Label and edge periods built from the bd `events` table need a snapshot fallback before
   2026-08-07, and type resolution for `dependency_removed` (32 unresolved here).
6. Pin psycopg parameter types: `StrDumper` for `str`, and transit or explicit int8 for integers,
   to avoid `smallint` columns. Python clients must read with `fallback_output_format='transit'`
   and decode it. transit-python2 works, but returns its own `Boolean` and `frozendict` types.

**Open, unverified here:** where an attached database's storage lives when it is not configured
(E5); and the server `ClassCastException` on range predicates over widened columns, which looks
like an XTDB 2.2.0 defect. Avoid it by contract 1 rather than relying on a fix.
