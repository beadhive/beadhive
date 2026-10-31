# Spike `bh-o1x4r` — which stores can load BeadLog, and what do `bd import` and Dolt `--date` honor?

**Bead:** `bh-o1x4r` · **Seat:** `dev/xtdb-landscape` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-psu1b` — adopt XTDB v2 as the first bi-temporal BeadLog loader?
(epic `bh-5wu0u`; decision record [`backfill-platform-adr.md`](../design/backfill-platform-adr.md))

> Builds on, and does not repeat, the committed planning research
> [`beadlog-store-landscape-research.md`](../design/beadlog-store-landscape-research.md)
> (the per-store survey, matrix and BeadLog v0 implications). That document's *Open
> verifications* section was updated in place with the results summarised here; read it for
> the full per-fact write-up.

## Question

Given the surveyed stores, which are **loader candidates** for BeadLog v0 and at what evidence
level, once the two verifications that need no XTDB are settled:

1. Does `bd import` honor a supplied `created_at` / `closed_at` (and `updated_at`), and what
   does it do to `events` rows on create vs update of an existing id?
2. What are Dolt `commit --date` semantics, and can they backdate `bd` writes?

Critically NOT asking: any XTDB behavior (later-`_valid_from` closing, pgwire type fidelity,
load cost). Those are owned by `bh-w56ts`, `bh-3jp2g` and `bh-cntr0`, and no XTDB container was
touched. Also not asking whether to build the loader (that is `bh-psu1b`).

## Method

All writes went to **throwaway embedded bd stores** created under
`/data/bees/beadhive/scratch/xtdb-spike/bh-o1x4r/` (real disk, not `/tmp`), each its own
`git init` + `bd init --prefix <p> --non-interactive`. Before the first write `bd where` and
`bd info` confirmed `.beads` and the Dolt database resolved inside that directory. No `bh bd`
passthrough, no hive, no production store, and no `bd compact` / `bd flatten` / `dolt gc` was
used. Tools: `bd 1.3.0 (f45b249ce)`, `dolt 2.3.5`.

1. **Imports on a fresh store (`s1`, prefix `spk`).** Four hand-written JSONL batches
   (`a1`..`a4`) with historic (2019-2020) `created_at` / `updated_at` / `started_at` /
   `closed_at`, run with `--dry-run` first and then for real, covering: create with
   timestamps, create without, update with a newer `updated_at`, stale row, same-second tie,
   and `--allow-stale` restore. After each step, state was read with `dolt sql` run from the
   store's `.beads/embeddeddolt/spk` directory (embedded mode has no `bd sql`: "not yet
   supported in embedded mode").
2. **Lifecycle contrast.** Ordinary `bd update` and `bd close` on an imported id, to compare
   the `events` rows they write against the import.
3. **Dolt `--date`.** In the same store, with bd idle: three `dolt commit --date` commits
   (2019, then 2031, then 2012 with a `-05:00` offset), then `dolt_log`,
   `dolt_history_issues` and `bd history <id> --json`. Then `bd update` run with
   `DOLT_COMMITTER_DATE`, `DOLT_AUTHOR_DATE`, `GIT_COMMITTER_DATE`, `GIT_AUTHOR_DATE` set, and
   `bd dolt commit --help` / `bd vc commit --help` read for a date flag.
4. **Production-scale round trip (`s2`, prefix `bh`).** The read-only reference export
   `data/bh-export.jsonl` (9,088 issues, all four timestamp fields populated for
   9,088 / 9,088 / 1,650 / 7,521 issues) was imported into a throwaway store to see whether the
   lifecycle timestamps survive a bulk create at scale and what the import costs. Comparison
   script: [`scripts/bh-o1x4r/compare_ts.py`](scripts/bh-o1x4r/compare_ts.py). The result is
   reported in Evidence item 8.
5. Teardown: both scratch stores were deleted at the end (`rm -rf` of the scratch directory
   created for this bead).

`bd import --help` flags cited (bd 1.3.0): `--dry-run`, `--dedup`, `--allow-stale`,
`-i/--input`, `--json`; stdin via `-`. The help text documents timestamp preservation, the
strictly-newer `updated_at` rule, `stale_skipped_ids` / `tie_kept_local_ids` /
`updated_issues`, and chunked transactions.

## Evidence

Legend: **measured** = observed on the throwaway stores; **docs** = from `--help` / tool
documentation only.

1. **Create honors all four timestamps (measured).** Rows imported with
   `created_at=2020-01-02T03:04:05Z`, `updated_at=...-03`, and for a closed row
   `started_at` / `closed_at` landed in `issues` unchanged to the second. A row with none got
   the import wall clock. Matches the help text ("preserved when present").
2. **Update never rewrites `created_at` (measured).** A second batch supplying
   `created_at=2019-01-01` for an existing 2020 id left the stored 2020 value; the same held
   under `--allow-stale`. `status`, `priority`, `title` and `closed_at` did follow the row.
3. **Update acceptance is governed by `updated_at` (measured, matches docs).** Strictly newer
   incoming `updated_at` wins and is listed under `updated_issues`; older is skipped
   (`stale_skipped_ids`); equal keeps local (`tie_kept_local_ids`, title unchanged);
   `--allow-stale` overwrote and moved `updated_at` *backwards* (2020-06 to 2015-01) and
   cleared `closed_at` when the row reopened. With `updated_at` omitted, it is stamped with
   wall clock.
4. **Events on create vs update (measured).** Each newly created id got exactly one
   `created` event, stamped with the **import wall clock** (e.g. 2026-10-30 21:42:44 for rows
   whose `created_at` was 2020) and the importing actor. Update-by-import wrote **no** event.
   By contrast `bd update` and `bd close` wrote `updated` and `closed` events at wall clock.
   Imports therefore cannot reconstruct or backdate the events timeline.
5. **`events` is not versioned (measured).** `dolt_ignore` lists `events`, `bd_events_journal`,
   `leases`, `wisps*` and others. Ignored tables sit outside Dolt commits by design (docs), so
   the events timeline is replica-local and not recoverable from `dolt_history_*`.
6. **`--dry-run` is side-effect free (measured).** It printed `Would import 3 issues (3 new, 0
   updated, 0 unchanged)` and, for the update batch, `2 updated ... (1 stale skipped)`; the
   Dolt log shows one commit per real import and none for dry runs. Per-id detail needs
   `--json` on a real run (`created` counts rows written, not new ids: an update-only batch
   reported `created: 2, updated: 2`).
7. **Dolt `--date` is commit metadata only (measured).**
   - `dolt commit --date` (forms `YYYY-MM-DD`, `YYYY-MM-DDTHH:MM:SS[Z|±hh:mm]`) set the commit
     date; the committed table values were identical, with `updated_at` wall-clock stamped by
     the column default.
   - It surfaces in `dolt log`, `dolt_log.date`, `dolt_history_issues.commit_date` and
     `bd history <id> --json` (`CommitDate`).
   - It is not validated and not monotone: commits dated 2019, then 2031 (future), then 2012
     were all accepted. `dolt_log` / `bd history` list in DAG order, not date order.
   - bd cannot be driven to use it: `bd dolt commit` / `bd vc commit` take only
     `-m` / `--stdin`; the four `*_COMMITTER_DATE` / `*_AUTHOR_DATE` env vars were ignored by
     `bd update` (commit carried wall clock). Only the `dolt` CLI on the embedded directory,
     with bd stopped, can write a dated commit, and bd then reads such commits fine.
8. **Production-scale import (measured).** Timestamps survive a 9,088-issue bulk create
   exactly, events are re-synthesized, and the import is slow and heavy; see the result block
   below this list.
9. **Other stores' evidence levels are unchanged by this spike (docs/specs).** XTDB v2 official
   docs; Beads v0 `EventRecord` and BDP core events profile read in spec; Datomic one forum
   thread; SQL:2011 standard; git-bug design doc. Details and the matrix live in the research
   doc and are not repeated here.

### Production-scale round trip (Evidence 8, measured)

Importing the 16 MB, 9,088-issue reference export into an empty embedded store:

- **Timestamps survived exactly.** After `bd export` of the result, `created_at`
  (9,088 / 9,088), `updated_at` (9,088 / 9,088), `started_at` (1,650 / 1,650) and `closed_at`
  (7,521 / 7,521) were identical to the source, to the second, with 0 issues missing.
- **Events were synthesized, not restored.** The new store held 9,088 `created` events
  stamped 2026-10-30 21:43:56 to 22:19:04 (the import window) plus 19,428 `label_added` events
  from the imported labels, versus the source hive's 36,716 events of 11+ kinds going back to
  2026-08-07. None of the source events came across.
- **Cost is high on a loaded host.** The import took 38 min 53 s wall (77 Dolt commits, i.e.
  chunked transactions) and the store grew to 2.2 GB on disk, against ~11-13 s for the
  matching `bd export` (12.8 s measured). The host was shared with other agents, so treat the
  duration as an upper-order figure rather than a benchmark; it does show that `bd import` is
  not a cheap loader at bh scale and that its commit/journal footprint dwarfs the source.

## Verdict — **GO**

**GO** on the question this spike owns: both verifications are settled and neither blocks the
XTDB spike. The result narrows what a bd loader can do, which is the shape the research
already assumed.

Loader candidates and the evidence behind each:

| Store | Role | Evidence level | Standing |
|---|---|---|---|
| bd on Dolt (`bd import`) | Loader create path for *new* ids; also the current source | **measured** (this spike, bd 1.3.0) | Candidate, degraded: native `created_at` / `updated_at` / `started_at` / `closed_at` on create only; no valid-time edits, no event backdating, no `created_at` correction; recorded time is wall-clock import commit time |
| Dolt commit `--date` | Not a loader; a recorded-time side channel | **measured** | Rejected as a loader mechanism: bypasses bd, non-monotone, unvalidated, mutates an archive store |
| XTDB v2 | First bi-temporal target | **official docs**, no run yet | Candidate; the XTDB-specific unknowns stay open under `bh-w56ts`, `bh-3jp2g`, `bh-cntr0` |
| BDP upstream events profile | Future reader / likeliest future loader | **spec read**, nothing run | Not a candidate today (no occurrence time, no historical import upstream) |
| Beads v0 service API `EventRecord` | Future reader source | **spec read** | Not a loader (journal read model) |
| Datomic-style | Manifest-only reference | **forum thread** | Not pursued; same monotone constraint as XTDB backfill |
| SQL:2011 temporal tables | Reference model | **standard** | Modelling reference, not a store |
| git-bug / Automerge / Fossil | Id and ordering prior art | **design doc / general knowledge** | Not stores |

## Recommendation

1. **Proceed to the XTDB spike** (`bh-w56ts`, `bh-3jp2g`, `bh-cntr0`) and then the adopt
   decision `bh-psu1b`. Nothing here blocks XTDB; it remains the only surveyed candidate that
   natively honors both time axes.
2. **For the bd loader, design to the measured limits.** Use `bd import` to create new ids
   with native `created_at` / `closed_at` / `updated_at`; carry every other `occurred_at`
   (field edits, label/edge history, comments) in `metadata.bdlg.*` with a declared loss
   report. Do not rely on `bd import` to fix `created_at` of an existing id or to produce
   historical `events` rows. Use `--dry-run` for the plan and `--json` for the result, and
   key re-runs on `updated_at` ordering (strictly-newer wins; ties keep local).
3. **Do not use Dolt `--date` as a loader mechanism.** Treat Dolt commit time as recorded-time
   evidence only, sorted by `commit_date` rather than relied on in log order.
4. **Protect the source.** Because `events` is outside Dolt versioning and history before
   2026-07-25 is already gone, the extractor should snapshot `events` and `bd export` before
   any retention action. Never `bd compact`, `bd flatten`, or `dolt gc` a hive (see
   `CLAUDE.md`).
