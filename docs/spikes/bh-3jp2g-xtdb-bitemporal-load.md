# Spike `bh-3jp2g` — Does XTDB 2.2.0 load bh history bitemporally and reproduce bd as of a cut?

**Bead:** `bh-3jp2g` · **Seat:** `dev/xtdb-bitemporal` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-psu1b` (adopt XTDB v2 as the first bi-temporal BeadLog loader?).
Parent: epic `bh-5wu0u`. Builds on the merged shape spike
[`bh-w56ts`](bh-w56ts-xtdb-bead-shape.md) (layout (b), `_id` rule, write/read patterns) and the
store landscape spike [`bh-o1x4r`](bh-o1x4r-beadlog-store-landscape.md). Context:
[backfill platform ADR](../design/backfill-platform-adr.md),
[BeadLog store landscape](../design/beadlog-store-landscape-research.md).

## Question

Can XTDB 2.2.0 hold the **whole** bh bead history bitemporally? Valid time would come from each
event's `occurred_at`. System time would come from its `recorded_at`, through the v2.2
`SYSTEM_TIME` backfill. And do `FOR VALID_TIME AS OF` reads then reproduce the state bd itself
recorded? Settling it needs five answers:

1. As-of reconstruction at three historical cuts matches bd for 50 or more beads, with every
   difference explained.
2. How the monotone `SYSTEM_TIME` rule behaves on ties, nulls and out-of-order input. From that,
   a grouping rule for BeadLog's optional `index/recorded.jsonl`.
3. Load time, storage size and peak memory for the full history.
4. Whether `ERASE` removes a bead from every valid-time **and** system-time read.
5. How the bd `events` rows map to `_valid_from` and to recorded time.

**Not asked:** the document shape and `_id` rule (`bh-w56ts`), long-running operational
footprint (`bh-cntr0`, which reads this spike's load metrics), the BeadLog wire format
(`bh-65jen`), and a production loader. The scripts here are throwaway.

## Method

**Server.** `ghcr.io/xtdb/xtdb:2.2.0`, with the image's own config: pgwire on 5432 and local log
and storage under `/var/lib/xtdb`. The data dir is on real disk (ext4 `/data`, not tmpfs).
`SYSTEM_TIME` backfill needs a database with no later transaction, so every experiment and the
full load started from an empty data dir. Only one container ran at a time.

```sh
docker run -d --name xtdb-spike-bh-3jp2g --memory 3g --memory-swap 3g --user "$(id -u):$(id -g)" \
  -p 127.0.0.1:15433:5432 \
  -v /data/bees/beadhive/scratch/xtdb-spike/bh-3jp2g/xtdb:/var/lib/xtdb ghcr.io/xtdb/xtdb:2.2.0
```

**Client.** Python 3.14, psycopg 3 (binary) and transit-python2, run as
`uv run --no-project --with 'psycopg[binary]' --with transit-python2`. No repo dependency
changes. The scripts reuse `bh-w56ts`'s `common.py`: transit `RECORDS` parameters, `StrDumper`,
string escaping, and `_type` renamed to `bd_record_type`.

**Data (read-only, production-derived).** `bh-export.jsonl` holds 9,088 beads from `bh bd export`
(current state only). `bh-events.json` holds 36,716 rows of the bd `events` table, from
2026-08-07T07:25:10Z to 2026-10-30T21:32:38Z. The production bd store was not queried.

**Scripts** are committed under [`scripts/bh-3jp2g/`](scripts/bh-3jp2g/), with captured stdout
in [`scripts/bh-3jp2g/output/`](scripts/bh-3jp2g/output/). The two shell helpers are kept verbatim
in [`shell.md`](scripts/bh-3jp2g/shell.md), because the native impact map does not own `.sh` files
under `docs/spikes/`.

| Script | Does |
|---|---|
| `xtdb.sh` | Fresh container up (refuses a non-empty data dir) / down (removes the container and its data dir) |
| `xt.py` | Points `bh-w56ts/common.py` at port 15433; `q()` query printer |
| `monotone.py` | Parts 1–4, each on a fresh node: `SYSTEM_TIME` syntax, ties, retries, out-of-order input, non-time aborts, null/DEFAULT, attached databases, several versions in one tx, DELETE defaults, a bitemporal correction, `COMMIT ASYNC`, null-recorded rows, future system time |
| `history.py` | Pure Python, no XTDB. Turns export + events into the op stream (one entry per distinct `recorded_at`). Separately replays bead state at the cuts and extracts bd pre-image witnesses |
| `load.py` | Loads the op stream: one `BEGIN READ WRITE WITH (SYSTEM_TIME = …)` transaction per distinct `recorded_at`, strictly increasing. Writes `load-metrics.json` |
| `sampler.sh` | Background sampler: `docker stats` memory and `du -sb` of the data dir, about every 3 s |
| `verify.py` | V1 as-of at the cuts vs the replay and vs bd witnesses; V2 current state vs export; V3 system-time reads |
| `erase.py` | `ERASE` of one bead, checked under 14 temporal reads per table; other beads re-verified |

**Event → row mapping (layout (b)).** One row write per event, in recorded order:

| bd `events.event_type` (rows) | XTDB write | `_valid_from` | System time |
|---|---|---|---|
| `created` (6,562) | the bead's first `beads` version (the seed) | event `created_at` (≤ export `created_at`, often 1 s earlier) | event `created_at` |
| `updated` (4,876), `claimed` (1,258), `status_changed` (1,405), `closed` (3,356), `reopened` (18), `lease_reclaimed` (20) | a new full `beads` version (pre-image re-anchored, then the delta applied) | event `created_at` | event `created_at` |
| `label_added` (15,546) | `INSERT INTO bead_labels` `{_id: "<bead>\|<label>"}` | event `created_at` | event `created_at` |
| `label_removed` (2,332) | `DELETE FROM bead_labels FOR PORTION OF VALID_TIME FROM t TO NULL WHERE _id = …` | (period ends at t) | event `created_at` |
| `dependency_added` (1,239) | `INSERT INTO bead_edges` `{_id: "<from>\|<type>\|<to>"}` | event `created_at` | event `created_at` |
| `dependency_removed` (88) | `DELETE FROM bead_edges FOR PORTION OF VALID_TIME FROM t TO NULL` for each held `(from, *, to)` | (period ends at t) | event `created_at` |
| `commented` (16) | `INSERT INTO bead_comments` with `source: 'event'` | event `created_at` | event `created_at` |
| none (state with no event: beads created before 2026-08-07, labels/edges/closes the events table never mentions) | snapshot-derived seed, label, edge or synthesized-close row | bead/edge `created_at` or export `closed_at` | **same as `_valid_from`** (declared inference, E1) |

The bd `events` table records changes live: there is one timestamp per row, and it is written
when bd commits the change. So for every event row **occurred_at = recorded_at =
`events.created_at`**. The table has no separate recorded column. Every loaded row therefore
starts with equal valid and system time. The two axes separate only where a later recorded
change ends an earlier belief (E5).

**Ground truth.** There are three comparisons, and only the second is independent of how the
loader reads events:

1. **Replay.** `history.py` folds each bead's events in plain Python (dicts and sets, no periods)
   and evaluates the state directly at each cut. The loader turns the same event semantics into
   INSERT/DELETE rows. A match therefore shows that XTDB's period mechanics return exactly the
   version a direct fold picks: INSERT closing the prior row, `DELETE FOR PORTION OF`,
   same-second versions in one tx, and `FOR VALID_TIME AS OF`.
2. **bd's own snapshots.** `updated`, `claimed` and `status_changed` rows carry a full JSON
   pre-image of the bead in `old_value`. A pre-image after a cut witnesses the state at the cut,
   provided no event of the same class lies in between and nothing else happened in that
   second. Scalar fields (status, assignee, priority, title, issue_type) and labels are checked
   separately.
3. **Export.** XTDB's current state is compared with `bh-export.jsonl`.

The three cuts are 2026-08-21, 2026-09-18 and 2026-10-16, at 00:00Z. A fourth cut, 2026-07-20,
falls before the events window and shows what is recoverable there. The fixed **sample** is 60
beads (`random.Random("bh-3jp2g")`): 20 created before the window that have events, and 40
created inside it with three or more scalar events. Every comparison also runs over all 9,088
beads.

## Evidence

### E1. What the bd `events` table makes recoverable

From `output/history.md` and a census of `bh-events.json`.

**Coverage.**

- The events table has 12 event types and runs from 2026-08-07T07:25:10Z. 14 rows belong to 3
  beads that are absent from the export (deleted) and are skipped, leaving 36,702 rows loaded.
- 2,075 beads (23%) were created before the window, back to 2026-06-29, and have no `created`
  event.
- Another 454 beads created inside the window also lack one. 235 of those have no events at all,
  for example the molecule `bh-iaxxv` and the tasks `bh-ie41e.1`–`.5`. Some bd create paths do
  not write `created`.
- 6,559 beads have a `created` event. Its time equals the export's `created_at` for 3,487 of them
  and is exactly **1 s earlier** for 3,072. `closed` events show the same skew against
  `closed_at`, so bd seems to truncate event times and round row times.

**Payloads** (what each row carries):

| Event | `old_value` | `new_value` / `comment` | Recoverable |
|---|---|---|---|
| `updated` | full JSON pre-image of the bead (4,559); empty (317) | JSON of the changed fields only; empty (317) | full state before **and** after. For the 317 payload-less rows, only that something changed; 309 are resolved from the next pre-image |
| `claimed` | full pre-image (1,258) | `{"assignee", "status"}` | full before/after |
| `status_changed` | full pre-image (1,376); the text `deferred` (29) | changed fields (`status`, often `metadata`); the text `open` (29) | full before/after, or the status transition |
| `closed` | empty (3,355) | close reason text (or empty) | that it closed and why; not the prior status |
| `reopened` | empty (15) / pre-image (3) | reason text / JSON | that it reopened |
| `lease_reclaimed` | the previous assignee | empty | the assignee change only |
| `label_added` / `label_removed` | null, or the label (newer rows) | `Added label: x` in `comment`, or the label in `new_value` | the set delta, exactly |
| `dependency_added` | empty | `Added dependency: <from> <type> <to>` | the edge with its type |
| `dependency_removed` | empty | `Removed dependency on <to>`, **with no type** | the target only; the type must come from an earlier add (28 removals unresolvable) |
| `commented` | null | comment text (16; reopen reasons, not the 676 export comments) | the text and time; no comment id |
| `created` | empty | empty | the creation instant only; initial field values come from the next pre-image or the export |

**The deltas and pre-images agree.** `history.py` folds every bead forward. At each event that
carries a pre-image, it compares its running state with bd's snapshot before re-anchoring on it.
Over 7,196 such checks on status, assignee, priority, title, issue_type, closed_at,
close_reason, notes, description, design and acceptance_criteria, only 4 fields differed:

- 1 status on the `bh-merge-slot` pseudo-bead, which bd's pre-image shows `open` again after a
  `claimed`, with no event in between;
- 3 notes values (`bh-b5v4y`, `bh-5sizy`, `bh-n17d`) that bd's next pre-image shows longer than
  the last event set them. These are notes edits that wrote no event.

So for scalar fields the events table is a near-complete record of change inside its window.
That held after three corrections:

1. **Same-second ordering.** The file orders ties by event id (16,462 adjacent ties, all sorted
   by id), not causally. There are 136 groups of scalar events on one bead in one second. The
   loader picks the permutation that no later pre-image contradicts, which reorders 74 of them.
2. **Payload-less `updated` rows** take their post-state from the next pre-image (309). An
   example is the assignment of `bh-x9hco` at 15:00:50, which only the following `claimed`
   pre-image reveals.
3. **`closed_at`** takes bd's own value, from the next pre-image or the export, rather than the
   event time, which is 1 s off (3,341 closes).

**State that the events never mention**, loaded as derived rows:

- **Closes.** 4,183 closes are synthesized from the export's `closed_at`. 1,392 are before the
  window. 2,791 are inside it: 2,680 `event`-type beads that bd creates already closed, and 111
  others with no `closed` event (43 gates, 22 tasks, 20 features, 12 bugs, …).
- **Labels.** 6,225 label rows exist only in the snapshot. They are assumed held from bead
  creation, which is the `bh-w56ts` rule; it gave 0 label anomalies here too.
- **Edges.** 9,102 edges exist only in the snapshot, valid from the dependency's `created_at`.
- **Losses.** 28 `dependency_removed` events name a target the loader never saw as an edge: it
  was added before the window, or added without an event. Its type is unknown, so the edge is not
  represented at all. 8 beads keep an edge the export no longer has (E3).

**Before the window** only these are recoverable: bead creation, final close (`closed_at`),
dependency creation and comment times. Intermediate status, assignee, label history and field
edits are not. Dolt history, which starts 2026-07-25 (`bh-o1x4r`), could fill part of that gap,
but was not used.

### E2. As-of reconstruction at three cuts matches the replay and bd's own snapshots

From `output/verify.md` (V1), read against the full load. Each cut issues three statements, one
per table, `SELECT … FROM <table> FOR VALID_TIME AS OF %s`. Each round of three takes 0.4–0.5 s.

| Cut (valid time) | Beads existing | All 9,088 beads vs replay | 60-bead sample vs replay | bd pre-image witnesses vs XTDB |
|---|---|---|---|---|
| 2026-08-21T00:00Z | 3,381 (7,593 labels, 3,499 edges) | **9,088 / 9,088 equal** | **60 / 60** | **101 / 101** (101 scalar, 5 label) |
| 2026-09-18T00:00Z | 5,763 (12,588 labels, 6,261 edges) | **9,088 / 9,088** | **60 / 60** | **91 / 91** (91 scalar, 65 label) |
| 2026-10-16T00:00Z | 8,419 (18,378 labels, 9,586 edges) | **9,088 / 9,088** | **60 / 60** | **47 / 47** (46 scalar, 31 label) |
| 2026-07-20T00:00Z (before the window) | 621 (1,621 labels, 647 edges) | 9,088 / 9,088 | 60 / 60 | none exist (no events) |

**What "equal" covers.**

- Against the replay: status, assignee, priority, title, issue_type, closed_at, close_reason,
  hashes of notes, description, design and acceptance_criteria, the label set, and the outgoing
  edge set. Existence counts too: a bead not yet created must be absent from XTDB.
- Against the witnesses: status, assignee, priority, title, issue_type and labels, exactly as bd
  serialized them in the pre-image.

**There are no differences to explain at the cuts.** The first verification run, against the dry
load, showed 13 witness mismatches. All were `priority`, and all were a bug in the spike's own
witness extraction: it used `pre.get(k) or None`, which turned priority 0 into None. After the
fix every witness matched. The replay agrees with the witnesses too, so the event semantics in
E1 reproduce bd's snapshots at these instants.

**What the pre-window cut means.** Before 2026-08-07 XTDB returns the snapshot-derived seed:

- the bead exists from its `created_at`;
- it is `open` until its `closed_at`;
- it carries the labels and edges it has today (edges from their own `created_at`).

That equals the replay by construction. No bd evidence exists to check it against. Status
changes, assignees and label churn before the window are unrecoverable (E1).

### E3. Final state vs the `bh-export.jsonl` current state

From `output/verify.md` (V2), with no temporal clause, so the read is as of now. XTDB matches the
replay's end state for **9,088 / 9,088** beads, and the export for **9,076 / 9,088**. Each of the
12 differences is a change the events table does not carry:

| Beads | Field | Cause |
|---|---|---|
| 8 | edges | XTDB still holds a `parent-child` edge that the export no longer has: `bh-mnpkc`, `bh-mw38f`, `bh-tfko9`, `bh-uf986`, `bh-3q5m9`, `bh-sj9lo`, `bh-420ye`, `bh-i3nc3`. A `dependency_added` exists, but no `dependency_removed`. bd re-parented or removed these without writing an event. These are the 8 anomalies `bh-w56ts` also found |
| 2 | notes | `bh-jfbll.10` (export 6,179 chars vs the last event's 4,424) and `bh-h2pd3`. The notes grew after the last `updated` event that carries notes. Three more cases like this appear as pre-image drift in E1 |
| 1 | assignee | `bh-65jen`. Its last event is a payload-less `updated` (21:30:50Z) with no later pre-image, so the assignment's content is unknown. It is one of the 8 ignored payload-less rows |
| 1 | status | `bh-gf10j`. A `claimed` event at 21:32:38Z, the very last event in the file, sets `in_progress`. The export says `open`, so it was taken a few seconds before the events dump |

`closed_at` matches everywhere because the loader takes bd's own value (E1, correction 3).
Without that correction the dry run showed 1,580 beads whose `closed_at` differed from the
export's. Every example printed was 1 s early.

### E4. Monotone `SYSTEM_TIME` in 2.2.0: syntax, ties, nulls, out-of-order input

From `output/monotone.md`. Each part ran on its own fresh node.

**Syntax (part 1, S1).** 2.2.0 accepts all of the following, and each row's `_system_from` equals
the requested instant:

```sql
BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-01T00:00:00Z')   -- S1a
BEGIN READ WRITE, SYSTEM_TIME = TIMESTAMP '2020-01-02T00:00:00Z'         -- S1b
BEGIN; SET TRANSACTION READ WRITE, SYSTEM_TIME = TIMESTAMP '…'           -- S1c
BEGIN READ WRITE WITH (SYSTEM_TIME = '2020-01-04T00:00:00Z')             -- S1d, string literal
… SYSTEM_TIME = TIMESTAMP '2020-01-05T00:00:00.000001Z'                  -- S1e, µs kept
BEGIN READ WRITE WITH (SYSTEM_TIME = %s)    -- S1f, psycopg datetime bind parameter
```

`SETTING SYSTEM_TIME = … INSERT …` is **not** valid: it fails with `Errors parsing SQL statement`
(S1g). `SETTING` applies only to queries, as `SETTING DEFAULT SYSTEM_TIME AS OF …, DEFAULT
VALID_TIME AS OF … SELECT …` (S9). The loader used S1a.

**Reads (part 2, S9).** The script loads a backdated correction: `price=10` valid from 01-12,
recorded at 01-12, then `price=9` valid over `[01-11, 01-13)`, recorded at 01-14. These reads
behave as documented:

| Read | Result |
|---|---|
| `FOR SYSTEM_TIME AS OF 01-13 FOR VALID_TIME AS OF 01-12` | 10 (what was believed before the correction) |
| `FOR SYSTEM_TIME AS OF 01-15 FOR VALID_TIME AS OF 01-12` | 9 (after it) |
| `FOR SYSTEM_TIME AS OF 01-13 FOR VALID_TIME AS OF 01-11` | no row (valid time not yet known) |
| `FOR SYSTEM_TIME BETWEEN 01-12 AND 01-14 FOR ALL VALID_TIME` | 3 rows; inclusive end, so it includes the correction |
| `FOR SYSTEM_TIME FROM 01-12 TO 01-14 FOR ALL VALID_TIME` | 1 row; exclusive end |
| `SETTING DEFAULT SYSTEM_TIME AS OF …, DEFAULT VALID_TIME AS OF …` | same as the per-table clauses |
| `FOR SYSTEM_TIME AS OF %s FOR VALID_TIME AS OF %s` with datetime parameters | works |

**The monotone rule, measured:**

| Case | What 2.2.0 does | Evidence |
|---|---|---|
| **Tie**: same `SYSTEM_TIME` as the latest completed tx | The whole tx aborts at `COMMIT` with `specified system-time must be later than current tx`; none of its rows are written | S2 |
| **Out of order**: earlier than the latest | The same abort | S4a |
| **The aborted tx's own system time** | It is recorded in `xt.txs` with `committed=false` at **wall-clock time** (2026-10-30T22:58:51Z), not at its requested time and not 1 µs past the latest. It counts as the latest completed tx | S2–S4 |
| **Does the load continue after a time abort?** | **No.** Every later backfill tx, including retries at +1 µs and +2 µs and the next in-order instant, aborts the same way, because the latest completed tx is now wall clock. Backfill is over for that database | S3b–S5d |
| **A non-time abort** (`ASSERT` fails) | Aborts at its **requested** system time, and the next in-order tx commits. The load can continue, minus that tx's data | S12 |
| A statement rejected when the tx is submitted (record without `_id`) | The client gets the error and **no** `xt.txs` row is written. Nothing is consumed | S5a |
| **Null**: `SYSTEM_TIME = NULL`, or `SET TRANSACTION …, SYSTEM_TIME = DEFAULT` | Accepted. The tx takes wall-clock time, which also **ends backfill** (S6c aborts afterwards) | S6 |
| Any plain wall-clock tx | Ends backfill, the same way | S6c, S11d |
| **Future** `SYSTEM_TIME` (now + 1 day) | Accepted, but its rows and its `xt.txs` row are **invisible to default reads** until the wall clock passes it. The next plain tx is pushed to the future instant + 1 µs and is invisible too | S11e, S11f, part 4 |
| `COMMIT ASYNC` | Returns success to the client even when the tx aborts; the abort shows only in `xt.txs` | S10 |
| A second database (`ATTACH DATABASE bf2`) on the same node | It has its own clock: a 2020 backfill commits there after the main database has wall-clock txs | S13 |

The main-branch docs say an aborted transaction "still takes a system-time of its own, 1µs past
the latest completed one, so a retry has to advance on that". That held only when the latest
was in the **future** (part 4: the aborted txs took `…28.000002Z` and `…28.000003Z`). During a
backfill, where the latest is in the past, 2.2.0 gave the aborted tx the current wall-clock
time. So the rule observed is `max(wall clock, latest + 1 µs)`.

**Within one tx (part 2, S7, S8).**

- Several versions of one entity with **distinct** `_valid_from` in one tx all keep their own
  periods: v1 [01-01, 01-03) and v2 [01-03, 01-05).
- Two writes with the **same** `_valid_from` in one tx: the last one wins, and the earlier one
  is **gone from every read, including `FOR ALL SYSTEM_TIME`**. v3 vanished and v4 remained.
- Under `SYSTEM_TIME = T`, an INSERT without `_valid_from` defaults its valid time to `T`. A plain
  `DELETE` (no `FOR PORTION OF`) ends the row at `T` (S8b). "Now" is the backfilled system time.
- `DELETE … FOR PORTION OF VALID_TIME FROM t TO NULL` gives an open-ended delete (S8c).
- A `_valid_from` later than the tx's system time is stored as is (S8d).

**Null-recorded rows loaded last (part 2, S11).** Two rows got versions v1 and v2 at backfilled
system times. A wall-clock tx then added an earlier seed v0.

- With `_valid_to` set to v1's start, v0 slotted in before v1, and v1 and v2 were kept.
- **Without `_valid_to`, v0 replaced v1 and v2 in current system time** (`m`: only
  `v0 [01-15, ∞)`). This repeats the `bh-w56ts` E7.2 clobber, now under system-time backfill.

### E5. Recorded time survived the load: system-time reads on the real data

From `output/verify.md` (V3) and `output/load.md`.

- **Recorded time is exact.**
  - `xt.txs` holds 28,402 committed transactions from 2026-06-29T03:43:46Z to
    2026-10-30T21:32:38Z, which are the first and last `recorded_at`.
  - `beads` has 22,501 distinct `_system_from` values, `bead_labels` 6,761, `bead_edges` 7,830.
  - The current version of every bead has `_system_from = _valid_from`; 0 rows differ.
- **The default system-time axis.** At each cut, `FOR SYSTEM_TIME AS OF <cut>`, which leaves
  valid time at its default, returned the same state as `FOR VALID_TIME AS OF <cut>` for every
  bead: 3,381 / 3,381, 5,763 / 5,763 and 8,419 / 8,419. So did pinning both axes. Each read takes
  0.2–0.5 s. The database at system time T believes exactly what bd's events said by T, which is
  what loading in recorded order should give.
- **The axes separate where they should.** `bh-e0kzf` held `review:pending` over
  `[00:43:27Z, 00:44:32Z)` on 2026-10-30.

  | `FOR SYSTEM_TIME AS OF` | `FOR VALID_TIME AS OF` | Label present? |
  |---|---|---|
  | 00:44:31Z | 00:44:33Z | **yes**: before the removal was recorded, it was believed to hold indefinitely |
  | 00:44:33Z | 00:44:33Z | no |
  | 00:44:31Z | 00:44:31Z | yes |

  Its bitemporal rows show it: `[00:43:27, 00:44:32)` is current from system time 00:43:27, and
  `[00:44:32, ∞)` is closed in system time at 00:44:32.
- **`FOR SYSTEM_TIME BETWEEN '2026-09-02' AND '2026-09-03' FOR ALL VALID_TIME`** on `bh-q0lol.5`
  lists each version with its source event id. It shows a seed, then the assignment at 19:32:52,
  then the claim at 22:39:35, and so on, each superseded in system time when the next was
  recorded.
- **Volume.** The bitemporal rows (`FOR ALL SYSTEM_TIME FOR ALL VALID_TIME`) are 39,030 `beads`
  (9,088 entities), 24,092 `bead_labels` (21,241), 10,409 `bead_edges` (10,327) and 691
  `bead_comments`.
- **Latency.** A point lookup of one bead `FOR VALID_TIME AS OF` a cut takes about 20 ms per
  Python round trip.

### E6. Load time, storage and memory for the full history

From `output/load.md` and `load-metrics.json` (written outside the repo for `bh-cntr0`, with 125
samples about every 3 s). It covers one load of the whole op stream into a fresh node: 9,088
beads, 36,702 event rows and the snapshot-derived rows, in layout (b), in valid-time **and**
recorded-time order.

| Measure | Value |
|---|---|
| Input | `bh-export.jsonl` 16.3 MB + `bh-events.json` 32.9 MB |
| Ops loaded | 59,387: inserts 24,195 `beads`, 21,760 `bead_labels`, 10,341 `bead_edges`, 691 `bead_comments`; deletes 2,332 labels, 68 edges |
| Transactions | **28,402**, one per distinct `recorded_at`, from 2026-06-29T03:43:46Z to 2026-10-30T21:32:38Z. Median 1 op per tx (18,829 single-op txs), p99 11, max 113 |
| Aborted | **0** (`xt.txs`: 28,402 rows, all `committed = true`) |
| Load time (SYNC commit, one client connection) | **359.6 s**, 79 tx/s. The dry run took 363.1 s. A sibling spike was loading on the same host at the same time (load average about 6) |
| Data dir after load | **370 MB** (369,728,267 B). All of it is the transaction **log**; `buffers` (block storage) was still empty, so nothing had been flushed to blocks yet |
| Container memory, idle before load | 417 MiB (cap 3 GiB) |
| Peak during load | **552 MiB**. Memory grew steadily with the live index (466 MiB at 22 s, 505 MiB at 133 s, 548 MiB at 320 s) |
| End of load | 552 MiB |
| Post-load idle | 551 MiB for the first 8 min. A **block flush** at 23:29:12Z, about 8 min after the load, moved data from the log to `buffers` (data dir 370 MB → 466 MB; memory spiked to 663 MiB for about 6 s). After that: **581 MiB** |

The log grows at about 13 KB per transaction. `bh-w56ts` loaded more data in 500- and
1,000-document batches (both layouts plus the raw events table) and its data dir was 112 MB.
Here most transactions hold a single small op, so the per-transaction overhead dominates. One
tx per distinct `recorded_at` is the price of preserving recorded time at one-second resolution.

### E7. `ERASE` removes a bead from every valid-time and system-time read

From `output/erase.md`. The target is `bh-q0lol.5`, the bead with 14 review bounce cycles. One
wall-clock transaction ran `ERASE FROM <table> WHERE <owner column> = 'bh-q0lol.5'` on `beads`
(`_id`), `bead_labels` (`bead_id`), `bead_edges` (`from_id`) and `bead_comments` (`bead_id`).
The ERASE ran after the full load, so it was allowed to end backfill.

| Read (beads / labels / edges / comments for the target) | Before | After |
|---|---|---|
| default (now) | 1 / 6 / 17 / 0 | **0 / 0 / 0 / 0** |
| `FOR VALID_TIME AS OF` 09-18 and 10-16 | 1 / 6 / 17 / 0 each | **0** |
| `FOR SYSTEM_TIME AS OF` 09-18 and 10-16, alone and with `FOR VALID_TIME AS OF` the same cut | 1 / 6 / 17 / 0 each | **0** |
| any read as of 08-21 (before the bead existed) | 0 | 0 |
| `FOR ALL VALID_TIME` | 18 / 33 / 17 / 0 | **0** |
| `FOR ALL SYSTEM_TIME` | 18 / 33 / 17 / 0 | **0** |
| `FOR ALL SYSTEM_TIME FOR ALL VALID_TIME` | 35 / 60 / 17 / 0 | **0** |
| `FOR SYSTEM_TIME BETWEEN 2026-06-01 AND 2026-12-01 FOR ALL VALID_TIME` | 35 / 60 / 17 / 0 | **0** |
| `SELECT COUNT(*) FROM beads FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE title = <its title>` | — | **0** |

**The other beads are intact.**

- Their bitemporal row counts are unchanged in all four tables (38,995 / 24,032 / 10,392 / 691).
- All 9,087 other beads still equal the replay at all three cuts.

**Two consequences for the loader.**

1. **Inbound edges stay.** 31 `bead_edges` rows owned by other beads point **to** the erased bead
   and are now dangling references. ERASE did exactly what its predicate said.
2. **Erased bytes stay on disk for now.** The bytes were still on disk right after the ERASE, 150 s
   later and again at teardown. `grep` found the erased title in the transaction log (`log/LOG`,
   `log/REPLICA_LOG`) and in three block files under `buffers/…/public$beads/`. ERASE is a
   **logical** erase across every read. Physical removal waits on compaction, and possibly on
   log retention. Neither was observed within this spike's window.

## Verdict — **GO**

XTDB 2.2.0 carried the whole bh history on both axes: 28,402 backfilled transactions and 0
aborts. `FOR VALID_TIME AS OF` reproduced the plain-Python replay **exactly** for all 9,088 beads
at all three cuts, the 60-bead sample included. It agreed with bd's own pre-image snapshots
wherever bd left one (E2). Its current state differs from the export only where the events table
itself is silent or 1 s imprecise (E3). Recorded time survived: `FOR SYSTEM_TIME AS OF` equals
`FOR VALID_TIME AS OF` at every cut, and the two axes separate exactly where a change was
recorded (E5). The full load took 6 minutes, peaked at 552 MiB and left 370 MB on disk (E6).
`ERASE` removed one bead from every valid-time and system-time read and left the others intact
(E7).

What makes it work is user-set `_valid_from` plus the v2.2 `SYSTEM_TIME` backfill, one
transaction per distinct `recorded_at`. The condition attached to the GO: **recorded-time
preservation is a one-shot, all-or-nothing property of a fresh database.** In 2.2.0 a single
tie, out-of-order, null or wall-clock transaction ends backfill for good (E4). The loader must
guarantee strict monotonicity itself. It cannot recover from a mistake in place.

## Recommendation

**Adopt XTDB v2 as the first bi-temporal BeadLog target (`bh-psu1b`).** Carry these into the
loader package and the BeadLog spec.

1. **Loader shape.** Reuse layout (b) and the `bh-w56ts` write/read patterns. Turn the BeadLog
   op-log into an op stream:
   - `create`/`set` (and `checkpoint`) become a full `beads` version via `INSERT … RECORDS`,
     with `_valid_from = occurred_at`.
   - `label_add`/`edge_add` become `INSERT` with the composite `_id`.
   - `label_remove`/`edge_remove` become `DELETE … FOR PORTION OF VALID_TIME FROM t TO NULL
     WHERE _id = …`.
   - `comment` becomes `INSERT`.
   - `tombstone` becomes `ERASE`.

   Commit SYNC. Use `COMMIT ASYNC` only if the loader then checks `xt.txs` before it reports
   success.
2. **`recorded-time-monotone-load` means an initial load into a fresh database.** A fresh node
   works, and so does `ATTACH DATABASE` (S13; its storage durability is still unverified). The
   loader must:
   - check client-side that every transaction's `recorded_at` is **strictly** greater than the
     previous one;
   - refuse `recorded_at` later than the load's wall clock (a future system time hides rows,
     S11e);
   - refuse to start if the target has any transaction at or after the first `recorded_at`.

   On any abort, discard the database and reload. Do not retry in place. Every load after the
   initial one uses plain wall-clock transactions: system time is the load time, valid time is
   still `occurred_at`. The loss report declares that recorded time was not preserved for those
   events.
3. **Grouping rule for `index/recorded.jsonl`.**
   - Order the index by `(recorded_at, entity causal order, event id)`.
   - Group events with **equal** `recorded_at` into one transaction with `SYSTEM_TIME =
     recorded_at`. Ties across different entities are harmless (E6: 28,402 txs for 59,387 ops).
   - When one entity has more than one write at the **same** `occurred_at` inside a group, the
     store keeps only the last write and drops the earlier ones even from `FOR ALL SYSTEM_TIME`
     (S7). bh has 136 such cases, all at one-second resolution. Either declare last-writer-wins
     per `(entity, occurred_at)`, as this spike did, or split the group. To split, give the k-th
     write of that entity its own transaction at `recorded_at + k µs`, with `occurred_at + k µs`.
     µs bumps are accepted (S1e) and cannot reach the next second.
   - **Null `recorded_at`.** XTDB cannot store "unknown": `NULL` means wall clock, which ends
     backfill (S6). Leave such events out of the index and load them **after** the backfill, in
     wall-clock transactions. Bound every one of them with an explicit `_valid_to`, the next known
     version's `occurred_at`, or it erases later versions (S11). The other option is to declare
     `recorded_at := occurred_at` with provenance `derived`. That is what this spike did for the
     snapshot-derived rows, so they could interleave with the event rows.
   - **Out-of-order or tied input** aborts the whole transaction and ends backfill. Later
     transactions do not load (S2–S4). Reject it before submission.
4. **A bd `events` → BeadLog extractor** (`beadlog-sources`) should encode E1:
   - Map `events.created_at` to both `occurred_at` and `recorded_at`.
   - Emit the full pre-image in `old_value` as a `checkpoint` before the event.
   - Resolve payload-less `updated` rows from the next pre-image.
   - Order same-second events by pre-image consistency, not by the file.
   - Take `closed_at` from bd's value rather than from the 1-s-truncated event time.
   - Synthesize `closed` from `closed_at` when no event covers it, marked `derived`.
   - Resolve the `dependency_removed` type from earlier adds and report the unresolvable ones.
   - Before 2026-08-07, emit only snapshot-derived events.
5. **`ERASE` policy for `tombstone`.** Decide whether a tombstone also erases **inbound** edge rows
   owned by other beads; this spike left them as dangling references (E7). For a real GDPR-style
   erase, also confirm with XTDB, or measure in `bh-cntr0`, when the erased bytes leave the log
   and the block files: compaction and log retention (E7). Until then, a tombstone gives a
   guarantee about reads only, not about bytes on disk.
6. **Open, not verified here:**
   - Restart cost and steady state. Right after the load, the whole history sat only in the log
     and the in-memory live index. A block flush followed about 8 minutes later (E6). `bh-cntr0`
     owns this.
   - Durability of attached-database storage.
   - The image's built-in Docker healthcheck reports `unhealthy` permanently, even on a healthy
     idle node. It runs `wget -q --spider http://localhost:8080/healthz/alive`, which sends a HEAD
     request, and healthz answers `405 Method Not Allowed` (Jetty 12.1.7). A plain GET returns
     `Alive.` A deployment should override the healthcheck with a GET. Hand this to `bh-cntr0`.
   - The difference between 2.2.0 and the main-branch docs on the system time of an aborted tx
     (E4). Re-check on upgrade.
