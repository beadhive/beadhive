# Backfill

`bh backfill` brings a hive's beads in line with its history and policy through one safety
envelope. A **pass** proposes BeadLog events against a fresh read of the hive, the engine seals
them into a hash-gated **plan**, and **apply** writes the plan one bead at a time with a pre-state
export, a checkpointed audit, rollback and a proof that a second dry run proposes nothing.

The architecture and packaging are decided in
[`design/backfill-platform-adr.md`](design/backfill-platform-adr.md); the `.bdlg` format in
[`design/beadlog-format-adr.md`](design/beadlog-format-adr.md).

| Package | Owns |
|---|---|
| `beadlog` | the `.bdlg` format: events, ids, manifest, merge/fold/diff, archive I/O |
| `beadlog-sources` | BeadHive-free producers: source strategies (the neutral `bd` reader and the `bd-history` events-table conversion), reconcile, judges, the standalone `beadlog` CLI |
| `beadhive-backfill` | the envelope (`BackfillEngine`, `Plan`, audit and pre-state ports) and the hive-policy passes (`beadhive_backfill.complexity`, `beadhive_backfill.timeline`) |
| root `beadhive` | the bd StoreLoader (`backfill_bd`), the `bh backfill` CLI and its catalog rows |

Root never holds engine logic; the published wheel vendors the three libraries' import packages
(they stay `Private :: Do Not Upload`).

## Verbs

```text
bh backfill complexity   [--hive H] [--dry-run | --apply] [--plan F] [--pre-state F] [--audit F] [--json]
bh backfill timeline     [--hive H] [--dry-run | --apply] [--plan F] [--pre-state F] [--audit F] [--json]
bh backfill export       [--hive H] [--out F.bdlg] [--history] [--json]
bh backfill inspect      F.bdlg [--json]
bh backfill verify       F.bdlg [--json]
bh backfill apply        PLAN.bdlg [--hive H] [--dry-run] [--pre-state F] [--audit F] [--json]
bh backfill-complexity   (the flat alias of `bh backfill complexity`; published wire surface)
```

Nothing writes to a hive unless asked: a pass previews by default and writes only with
`--apply`, and `apply` writes only without `--dry-run`. `export`, `inspect` and `verify` never
write to a hive.

### Shared options

| Option | Meaning |
|---|---|
| `--hive H` | target hive (default: the hive owning the current directory) |
| `--dry-run` | preview only (the default for a pass; for `apply`, report the loader's losses) |
| `--plan F` | where a pass writes its plan on dry-run; required, and read back, with `--apply` |
| `--apply` | apply a previously saved `--plan` |
| `--pre-state F` | the recovery export, written before the first write (default: beside the plan) |
| `--audit F` | the machine audit JSON (default: beside the plan) |
| `--json` | machine output: the plan, audit, summary or loss report |

### `bh backfill complexity` (pass #1)

Converges every routable bead on exactly one canonical `complexity:<TIER>` label
([`COMPLEXITY-ROUTING.md`](COMPLEXITY-ROUTING.md)). `bh backfill-complexity` is the same command:
both read the same corpus and produce byte-identical plans, audits and output. The plan and audit
keep their published JSON shape (`schema_version: 1`, `migration: complexity-label-backfill`).

```bash
bh backfill complexity --dry-run --plan complexity-plan.json --json
bh backfill complexity --apply --plan complexity-plan.json \
  --pre-state complexity-pre-state.jsonl --audit complexity-audit.json
```

The preview reads `bd list --all --include-infra`, hashes every input that affects scope or
classification, and reports counts by tier, status, type, exclusion reason and existing label,
the UNKNOWN→MEDIUM fallbacks, duplicate or invalid labels, preserved `model:` hints and
model-versus-score disagreements. Apply runs the saved plan through the engine: the planned
target labels are recorded decisions and are never re-classified.

### `bh backfill timeline` (pass #2)

Derives each bead's lifecycle facts from the history readers and writes them as
`metadata.lifecycle.*` keys, which bd lacks. A dry run (the default) reads every source once for
the whole hive and, with `--plan F`, saves the plan as a `.bdlg`; `--apply --plan F` runs it
through the safety envelope below. **Nothing else is written:** apply issues one `bd update` per
bead that changes `metadata.lifecycle.*` and the loader's own `metadata.bdlg` occurrence-time
bookkeeping, never a field, label, status or edge. A second run proposes zero changes (the
envelope's idempotency proof): the applied events are stored in `metadata.bdlg`, the next
baseline re-emits them with their ids, and the pass re-derives the same ids from the same
sources.

```bash
bh backfill timeline --dry-run --plan timeline.bdlg      # preview: counts by fact and confidence
bh backfill inspect timeline.bdlg                        # classes, hashes, the pass report
bh backfill timeline --apply --plan timeline.bdlg        # pre-state, audit, second dry run
```

A timeline plan also applies through `bh backfill apply timeline.bdlg`.

**Lifecycle keys.** Every key `K` has a companion `K_confidence` (`high`, `medium` or `low`), so
the confidence is kept beside the value in bd, which holds current state only.

| Key | Source | Provenance | Confidence |
|---|---|---|---|
| `claimed_at` | the first bd `claimed` event | observed | high |
| `committed_at`, `last_committed_at` | author date of the first and last sha in `metadata.git.commits` (one `git log` for all shas) | derived | medium |
| `submitted_at` | the first `review:pending` label added | observed | high |
| `reviewed_at` | the first review outcome after it: `review:approved` or `review:changes-requested` added, or `review:pending` removed | observed | high for `review:approved`, else medium |
| `merged_at` | committer date of the first-parent `chore(merge): bead\|molecule\|batch` land commit (the latest land when there are several) | derived | high; medium for a legacy subject or a `batch <group>` land matched through the bead's `batch:<group>` label |
| `reopened_at`, `reopen_count` | bd `reopened` events only | observed, derived | high |
| `run_started_at`, `run_ended_at` | run journals (`process.spawned` or `run.created`; last `process.harvested`) when present | observed | medium |

**Two times, never one.** Each event the pass proposes keeps `occurred_at` (when it happened)
apart from `recorded_at` (when the source recorded it). bd records an event as it happens, so
for bd facts both are the event row's time. A git fact happened at the author date and was
recorded at the committer date. A land happened at its commit's committer date and bd *recorded*
the merge when it closed the bead, so `merged_at` has `occurred_at` = the land commit and
`recorded_at` = the first bd `closed` event at or after it (null when none follows). The ordering
`merged occurred_at < closed recorded_at` is therefore data, visible in the plan.

**Revert artifacts are not history.** Closes and reopens come from bd events only: a status flip
that appears only in snapshots (repeated exports, Dolt history) is never a reopen. Rows that
repeat verbatim collapse, and a phase change undone within the same second cancels. Refresh
merges (`chore(merge): refresh ...`) and `Merge branch 'main' into ...` are never lands.

### `bh backfill export`

Writes the hive's current state as a `.bdlg` archive: one `bd export --all` read turned into
events by the neutral bd strategy (`beadlog_sources.bd`), which re-emits the ids of events a
backfill loaded earlier. The archive declares one bead group whose id is a UUIDv5 over the hive's
`provider/org/repo` (the same on every export, so re-exports union-merge) and whose directory is
the hive prefix; the default file is `<prefix>.bdlg` in the current directory. Use it as the
baseline a `beadlog reconcile` diffs against.

**`--history`** (off by default; without it the output, flags, JSON keys and exit codes are
exactly as above) makes the archival archive: the current-state events **plus** every history
event of the bd `events` table, in one `.bdlg` (default file `<prefix>.history.bdlg`). It costs
two store reads (`SELECT` on `events`, and the store's first commit date) on top of the export.

- The events table becomes `create`, `phase`, `set`, `label_add/remove`, `edge_add/remove` and
  `comment` events (`beadlog_sources.bd_timeline`), `observed`, with the stable source
  `bd` / `bd:events/<row uuid>`, so re-exporting gives the same ids. Their ids differ from the
  current-state events' (`bd:issues/...`), and the group id is the same, so the two exports
  union-merge.
- A bead with no `created` row gets an `inferred` create built from its issues row
  (`bd:inferred/<id>#create`, `recorded_at` null, confidence 0.3 when the bead predates the
  table, 0.5 when it was imported without a created event). A `created` row whose time is later
  than the issues row's `created_at` keeps the row's time as `occurred_at` and its own as
  `recorded_at`, and says so in the evidence. (On this store that never happens: every `created`
  row is within a second *before* the issues row and never after it.)
- **The fold of the union equals the fold of the current state.** History is the past of the
  export, so any history event that would change a bead's folded current state is dropped and
  counted (a transient lease holder, a comment since deleted, a value the table never saw
  change), and so is every row of a bead the export does not hold.
- The manifest `annotations["bdlg.history"]` reports: `sources` (event count per source family:
  `bd:issues`, `bd:events`, `bd:inferred`), `events_table_rows`, `history_events`,
  `earliest_recorded` (the first row of the events table) and `latest_recorded`, what was
  `skipped_rows`, `suppressed` (revert artifacts and the dropped events above, by reason),
  `not_modeled`, and the `gap`: the first bead creation, the store's first commit and the first
  recorded event, computed from the data, with a note on the pre-retention gap. (On `bh`:
  beads from 2026-06-29, store commits from 2026-07-25, the events table from 2026-08-07.)
- `bh backfill inspect` prints the same numbers; `bh backfill verify` checks the archive like
  any other. Checkpoints are not written (the archive is complete without them).

Lifecycle facts (merged, submitted, reviewed, ...) are hive conventions, not events-table rows:
they enter the archive from the current-state side once `bh backfill timeline --apply` has
stored them in `metadata.bdlg`.

### `bh backfill inspect`

Prints an archive's manifest: format version, generator, groups, capabilities and the event
counts by kind. For a plan it adds the pass, schema version, corpus and plan hashes, the judge
identity, the reconcile classes (NEW / PRESENT / NOISE / DRIFT), how many entries need review,
the engine totals and a summary of the recorded verdicts by engine and outcome.

### `bh backfill verify`

Verifies every member's size and SHA-256, part order, the manifest counts, duplicate ids and the
index, and recomputes every event id. For a plan it also verifies the plan hash, so an edited plan
fails here before anyone tries to apply it.

### `bh backfill apply`

Applies a `.bdlg` plan to the hive: one written by the engine for a registered pass, or a neutral
plan from the standalone `beadlog reconcile` (whose NEW proposals are applicable). The plan must
declare exactly one bead group; the bd loader loads that group into the hive. `--dry-run` runs
the loader's assessment only and prints its loss report (every field bd cannot keep).

```bash
bh backfill export --out baseline.bdlg
bh backfill inspect baseline.bdlg            # note the group id and directory
beadlog extract <strategy> --root . --group <group id> --group-dir <prefix> \
  --extracted-at 2026-10-31T00:00:00Z --baseline baseline.bdlg -o proposed.bdlg
beadlog reconcile proposed.bdlg baseline.bdlg -o plan.bdlg
bh backfill inspect plan.bdlg
bh backfill apply plan.bdlg --dry-run
bh backfill apply plan.bdlg
```

The standalone `beadlog` CLI is the `beadlog-sources` distribution's script (in a checkout,
`uv run beadlog`); the installed `bh` carries the libraries but not that script or its
`beadlog.sources` entry points.

A plan whose loader reports `erasure.incomplete` is refused; delete and bead-scope tombstones
are off in the bd loader and are reported as dropped.

## History readers

Bulk, read-only readers over every lifecycle source, feeding `bh backfill timeline` and
`bh backfill export --history`. They return plain typed records (not BeadLog events). Each costs a
fixed number of calls for the whole hive (never per bead, never `bd history`, which takes ~20 s a
bead and omits labels, edges and metadata); the command runner is injected, so tests assert the
count with a counting fake.

| Reader | Package | Source | Calls |
|---|---|---|---|
| `beadlog_sources.bd_history.read_history` | `beadlog-sources` | the bd `events` table: `bd -C <root> sql --json 'SELECT ... FROM events'` (SELECT only; timeout default 300 s, the real call takes ~13 s for ~36k rows) | 1 |
| `beadlog_sources.git_dates.commit_dates` | `beadlog-sources` | author and committer date of many shas: `git log --ignore-missing --no-walk=unsorted` | 1 (chunks of 4000 shas) |
| `beadhive_backfill.merges.read_land_merges` | `beadhive-backfill` | `git log --first-parent <ref>`: `chore(merge): bead\|molecule\|batch <target>` lands | 1 |
| `beadhive_backfill.run_journals.read_run_journals` | `beadhive-backfill` | `<home>/run-journals/<hive>/*.jsonl` (the caller resolves the directory) | 0 store calls; a missing directory is an empty result |

The `events` rows have mixed shapes (measured on the production store): `claimed`, `updated`,
`status_changed` and `reopened` carry JSON in `old_value` (the whole prior issue) and `new_value`
(the changed fields); `closed` and `reopened` put the reason in `new_value` as text;
`status_changed` by the defer-wake daemon is plain text (`deferred` to `open`); labels are
`Added label: X` / `Removed label: X` in `comment` (or the bare label in `new_value` /
`old_value`); `dependency_added` is `Added dependency: <issue> <type> <target>` and
`dependency_removed` is `Removed dependency on <target>` (no type); `commented` has only
`comment`; `lease_reclaimed` has the reclaimed-from assignee in `old_value`; `created` has no
payload. The table `id` is a UUID, not a sequence: events sharing a second order by
`(created_at, id)`, which is deterministic, not causal.

Defensive by construction: a malformed row is skipped and counted by reason in `skipped`, an
unknown event type is counted in `unknown_types`, an unparseable log line or journal line is
counted, and a trailing journal line without its newline is treated as in flight. Only a failed or
unparseable *query* raises. A land commit's committer date is the bead's occurred `merged_at`;
refresh merges (`chore(merge): refresh wt/bead/epic/<id> from main`) and `Merge branch 'main' into
...` are never lands, and the three pre-convention July 2026 subjects are returned with
`legacy=True`. A `batch` land names its group (`batch group-a`), not its member beads.

## Pass matrix

| Pass | Verb | Reads | Writes | Plan | DRIFT | Postcondition |
|---|---|---|---|---|---|---|
| `complexity` | `bh backfill complexity` | `bd list --all --include-infra` | `bd label add/remove`, one label at a time | JSON (`complexity-label-backfill`, schema 1) | applied | one valid `complexity:<TIER>` per routable bead; `model:*` untouched |
| `timeline` | `bh backfill timeline` | the bd `events` table, `git log --first-parent`, commit dates, run journals | `bd update` of `metadata.lifecycle.*` (and `metadata.bdlg`) only, one bead at a time | `.bdlg` (`timeline`, schema 1) | applied | every lifecycle key well-formed with its confidence; a second dry run proposes nothing |
| `reconcile` (neutral) | `bh backfill apply` | `bd export --all` | the bd StoreLoader (`bd import`, `bd update`, `bd close`, `bd dep`, `bd comments add`) | `.bdlg` from `beadlog reconcile` | reported only | second reconcile finds no NEW |

Scope: `timeline` governs every bead that exists in the hive and proposes only the lifecycle keys
it has a source for (a bead with no facts is untouched). `complexity` governs epics, features,
tasks, bugs and chores in `open`, `blocked`, `in_progress`, `deferred` or `closed`; the merge-slot
record (`gt:slot`) and every other bead are counted by reason and never touched, so a change to
them never invalidates a plan.

## Safety envelope

Every apply, whatever the pass, does this in order:

1. **Plan hash.** An edited or corrupt plan is refused.
2. **Drift gate.** A fresh read of the hive must project to the planned corpus hash; otherwise
   apply refuses before exporting or writing anything ("corpus changed after planning").
3. **Loss assessment.** The loader's dry run reports what it cannot keep.
4. **Pre-state.** The recovery export (`bd export --all` JSONL) is written before the first write.
5. **Applying audit.** The audit records `state=applying` before the first write, then checkpoints
   the attempted, completed and uncertain bead around every write.
6. **Verification.** The whole projection must equal the expected post-state (drift inside the
   write window fails), the pass postcondition must hold, and a second dry run that uses only the
   plan's recorded judgments must propose nothing.
7. **Rollback.** On any failure or interruption every touched bead is restored by replaying the
   loader inverses in reverse; the audit ends `rolled_back`, or `rollback_failed` with the writes
   that could not be undone. Undoing a create retires the bead; it never deletes.

## Recovery

Keep the plan, the pre-state and the audit together until the audit says `state=applied`.

- **`applied`**: done. Check `second_dry_run_changes=0` (and, for complexity,
  `post_apply_complexity_errors=[]` and `model_labels_preserved=true`).
- **`rolled_back`**: nothing to recover. Read `error`, fix the cause, run a fresh dry run and
  plan; do not reuse the old plan if the corpus changed.
- **`rollback_failed`**: stop writers to the hive. The audit lists every write that could not be
  undone (`rollback_failures`, plus `rollback_residual` for engine plans). Restore those beads from
  the pre-state export, then run the dry run again.
- **`applying`** (the process died outside its rollback handler): stop writers. The uncertain bead
  is `recovery.uncertain_bead` (JSON plans) or `recovery.uncertain` (engine plans); `completed`
  lists the beads already written and `next_index` the next unit. Restore the touched beads from
  the pre-state export (`bd import` of the affected rows, or re-applying their labels from it),
  then rerun the same dry run. Do not manufacture a new plan until the original corpus is
  reconciled.

Never run `bd compact`, `bd flatten` or Dolt GC as part of a recovery: bead history is the only
record of when each lifecycle stage happened.

## Defaults

Every new behavior is off until asked for: the Haiku judge (`backfill.judge.enabled=false`),
deletion and erasure in the bd loader, `export --history` (the plain export is the default), and
any write (a pass, `timeline` included, previews unless `--apply`).
