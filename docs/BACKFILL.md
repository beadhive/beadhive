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
| `beadlog-sources` | BeadHive-free producers: source strategies (the neutral `bd` reader, the `bd-history` events-table conversion, `git-commits`, `docs` and `planning`), reconcile, judges, the standalone `beadlog` CLI |
| `beadhive-backfill` | the envelope (`BackfillEngine`, `Plan`, audit and pre-state ports) and the hive-policy passes (`beadhive_backfill.complexity`, `beadhive_backfill.timeline`, `beadhive_backfill.source_passes`) and the link-coverage metrics (`beadhive_backfill.coverage`) |
| root `beadhive` | the bd StoreLoader (`backfill_bd`), the `bh backfill` CLI and its catalog rows |
| `beadlog-xtdb` | BeadHive-free XTDB loader foundation (not vendored into root; the opt-in plugin binds it): pgwire connection, layout (b) row builders, an in-package transit codec, single-type column coercion, the `>=2.2.0,<2.3` version gate and the dangling-edge report |

Root never holds engine logic; the published wheel vendors the three libraries' import packages
(they stay `Private :: Do Not Upload`).

## Verbs

```text
bh backfill complexity   [--hive H] [--dry-run | --apply] [--plan F] [--pre-state F] [--audit F] [--json]
bh backfill timeline     [--hive H] [--dry-run | --apply] [--since-cursor] [--plan F] [--pre-state F] [--audit F] [--json]
bh backfill commits      [--hive H] [--dry-run | --apply] [--since-cursor] [--plan F] [--pre-state F] [--audit F] [--option K=V]... [--json]
bh backfill docs         (the same options as commits, without --since-cursor)
bh backfill planning     (the same options as commits, without --since-cursor)
bh backfill coverage     [--hive H] [--refresh] [--json]
bh backfill export       [--hive H] [--out F.bdlg] [--history] [--store bd|xtdb] [--json]
bh backfill inspect      F.bdlg [--json]
bh backfill verify       F.bdlg [--json]
bh backfill apply        PLAN.bdlg [--hive H] [--dry-run] [--pre-state F] [--audit F] [--store bd|xtdb] [--json]
bh backfill-complexity   (the flat alias of `bh backfill complexity`; published wire surface)
```

Nothing writes to a hive unless asked: a pass previews by default and writes only with
`--apply`, and `apply` writes only without `--dry-run`. `export`, `inspect`, `verify` and
`coverage` never write to a hive.

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
| `--store S` | `apply` and `export` only: the BeadLog store, `bd` (the default; the hive's bd store) or `xtdb` (see [Other stores](#other-stores---store)) |

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
- With `backfill.timeline.gapfill.enabled` the beads that have no `created` row also get
  snapshot-level events from `dolt_history_issues`; see [Gap fill](#gap-fill). Off by default:
  without it no history scan runs and the annotation has no `gapfill` member.

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

The whole sequence, from the baseline export to the apply, is under [End to end](#end-to-end).

### Other stores (`--store`)

`apply` and `export` take `--store`. Without it, or with `--store bd`, they are exactly the bd
verbs above (same request, output, JSON keys, exit codes, recovery artifact and follow-ups).
Any other store comes from a plugin providing the `beadlog.store` capability, composed lazily
only when it is installed, enabled and selected; otherwise the verb exits 1 saying what to do
(install `beadhive[xtdb]`, `bh config set plugins.beadlog-xtdb.enabled true`, run
`bh plugin beadlog-xtdb doctor`). Today that is `xtdb`, the opt-in
[`beadhive-beadlog-xtdb`](../packages/beadhive-beadlog-xtdb/README.md) plugin over the
`beadlog-xtdb` loader, configured under [`plugins.beadlog-xtdb`](CONFIGURATION.md); the operator
runs the node ([`bh-cntr0`](spikes/bh-cntr0-xtdb-operational-fit.md)).

- `apply PLAN --store xtdb` runs the same [safety envelope](#safety-envelope) against the
  store: the corpus hash is checked against the store's own history (so a plan written against
  the bd hive does not apply to a store holding something else; a plan for a fresh store is a
  `beadlog reconcile` against an empty baseline), the whole-plan dry run gives the loader's loss
  report, the recovery artifact is a `.bdlg` of the store's pre-apply history
  (`<plan>.pre-state.bdlg` by default), and every bead is written, audited and verified as with
  bd. On a fresh database the first written unit takes the loader's monotone initial load
  (recorded time preserved) and later units load incrementally; each loss report says which.
  The bd-only follow-ups (the incremental cursor, the coverage cache) are not touched.
- `export --store xtdb` writes the store's current state for the hive's bead group, read by the
  as-of reader (default file `<prefix>.xtdb.bdlg`); the manifest's `annotations["beadhive.store"]`
  names the store and target and counts the reader's notes (what it could not carry back).
  `--history` reads the bd events table and is refused with a non-bd store.

The standalone `beadlog` CLI is the `beadlog-sources` distribution's script (in a checkout,
`uv run beadlog`); the installed `bh` carries the libraries but not that script or its
`beadlog.sources` entry points.

### `beadlog extract git-commits`

The neutral `git-commits` strategy (`beadlog_sources.commits`) links commits to beads: it scans
each commit message (subject and body) for ids, keeps only tokens that resolve against the
baseline's own beads (`bh-infra` and the like are dropped), and proposes one `set` of
`metadata.git.commits` per bead that **appends** the new shas, oldest first, to the baseline's
list. Existing entries keep their order and are never removed; once the shas are in the baseline,
extracting again proposes nothing. A bead whose stored value is not a JSON array of strings is
reported as DRIFT (in the command output and the archive annotation `bdlg.git-commits`) and gets
no event, so it is never overwritten. A shallow clone is refused (`git fetch --unshallow`).

```bash
beadlog extract git-commits --root . --group <group id> --group-dir <prefix> \
  --extracted-at 2026-10-31T00:00:00Z --baseline baseline.bdlg \
  --option rev=main --option since=<sha|date> -o proposed.bdlg
```

Options: `rev` (default `HEAD`), `since` (a commit, or a date for `git log --since`),
`allow_shallow=true`. Reconcile classes an append as DRIFT (the bead exists and its `git.commits`
changes); `bh backfill commits` applies it (see
[the hive passes](#bh-backfill-commits-docs-and-planning-passes-3-to-5)).

### `beadlog extract docs`

The neutral `docs` strategy (`beadlog_sources.docs`) links decision and design docs to beads, the
port of `reconcile.sh`'s docs mode (`bh-skls-svo`: it silently stopped at 37 of 120 docs; this
one processes every doc, reads git once and makes no per-doc store call). Per doc, deterministic
before fuzzy: a bead already carrying the doc as `external_ref` is `PRESENT-in-sync`; else the
first `Beads:` line of the doc, then the bead id in the commit that first added it, names the
bead (`PRESENT-needs-stamp`: a `set` of `external_ref`; `DRIFTED-ref`: the bead points elsewhere;
`DANGLING`: no such bead; the last two are reported, never written). What is left is
`UNMATCHED`: a token-overlap shortlist (`top_k`, default 3) over every baseline bead is put to the
judge as one choice (`present:N` / `noise:N` / `new` / `abstain`) and each verdict, confidence
included, is recorded in the archive annotation `bdlg.docs`. **The judge is off by default**
(`--judge-enabled true` turns it on, same `--judge-*` flags as `beadlog reconcile`): then every
unmatched doc is an abstain residual, listed as `RESIDUAL <doc>` for a human to confirm.

```bash
beadlog extract docs --root . --group <group id> --group-dir <prefix> \
  --extracted-at 2026-10-31T00:00:00Z --baseline baseline.bdlg \
  --option docs=docs/decisions,docs/design -o proposed.bdlg
```

Options: `docs` (directories, recursive; default `docs/decisions` and `docs/design`, their `*.md`
only), `top_k`, `id_prefix`, `new_type`, `labels` (extra labels for a judged-NEW bead, such as the
hive identity triplet), `prior` (an earlier extract whose verdicts are reused, never re-asked),
`allow_shallow`. Once the stamps are in the baseline, extracting again reports every doc
`PRESENT-in-sync` and proposes nothing. The status table and the residual workflow are under
[Reconcile statuses](#reconcile-statuses) and [Judged residuals](#judged-residuals).

### `beadlog extract planning`

The neutral `planning` strategy (`beadlog_sources.planning`) turns a GSD `.planning` tree into
beads in one reviewed plan: `phases/NN-<name>/` becomes an epic, each `NN-MM-PLAN.md` an issue with
a `parent-child` edge to its phase, `depends_on` (inline `[..]` or a block list) `blocks` edges, a
plan with an `NN-MM-SUMMARY.md` is closed (a phase when all its plans are), and `occurred_at` is the
git add date of the plan or summary. `external_ref` is the doc path, so extracting again after the
apply proposes nothing (a summary that appears later proposes only its close). Created beads carry
`origin:backfill` and `source:planning`; `bh label validate` also wants one `complexity:<TIER>` on
every routable bead, which is hive policy: name it through `--option labels=complexity:MEDIUM` or run
`bh backfill complexity` afterwards. Options: `planning` (the tree, default `.planning`),
`id_prefix` (required with an empty baseline), `labels`, `issue_type`, `allow_shallow`.

A plan whose loader reports `erasure.incomplete` is refused; delete and bead-scope tombstones
are off in the bd loader and are reported as dropped.

## Coverage

`bh backfill coverage` measures how well a hive's work is linked, so that backfill becomes a drift
detector and not a one-time chore. Three ratios, the baselines of the
[ADR](design/backfill-platform-adr.md#context) measured the same way:

| Ratio (JSON key) | Numerator | Denominator |
|---|---|---|
| `closed_beads_with_commits` | closed code-bearing beads that record a commit in `metadata.git.commits` | closed (or retired) beads of a `code_types` type |
| `commits_resolving_to_bead` | commits whose message (subject and body) names a bead of the hive | every commit of the integration branch |
| `docs_linked_to_bead` | docs under `doc_roots` that some bead carries as its `external_ref` | every `*.md` under `doc_roots` |

The code-bearing types are `backfill.coverage.code_types` (default `bug`, `chore`, `feature`,
`task`); `event`, `gate` and `molecule` records are infrastructure and never enter the first ratio,
whatever is configured. Commits are matched with the same two-step matcher as `git-commits` (a loose
namespace-anchored token, kept only when it resolves in the hive's own id set), so `bh-infra` is
not a link. A ratio over an empty denominator is `null`, never 0 or 1. A shallow clone is refused,
as in `git-commits`: truncated history would understate the commit ratio.

```bash
bh backfill coverage --refresh   # one bd export, one git log, one doc walk; rewrites the cache
bh backfill coverage             # the cached state, checked against the hive's current inputs
bh backfill coverage --json      # the same as JSON (schema: `packages/beadhive-backfill/src/beadhive_backfill/schemas/coverage-v1.schema.json`)
```

**Cached state.** The ratios are cached as a versioned JSON file in the bh state dir,
`<bh home>/state/backfill/<hive key>/coverage.json`: `computed_at`, an `inputs_hash`, the settings,
the ratios and the top unlinked examples of each (most recently closed beads, newest commits, doc
paths). The `inputs_hash` is a SHA-256 over exactly what the ratios depend on: the settings, each
bead's type, closedness, recorded commits and `external_ref`, each commit's sha and message, and
the doc paths. A plain `coverage` reads the inputs once to compare, and reports the cache as one of:

| `status` | Meaning |
|---|---|
| `fresh` | the cache matches the hive's current inputs |
| `stale` | the cache was computed from different inputs: its figures are shown marked **STALE** (text) or with `status: stale` (JSON) and are never presented as current; run `--refresh` |
| `missing` | no cache yet; run `--refresh` |
| `invalid` | the file is corrupt or of a version this build does not read; `--refresh` replaces it |

The cache is rewritten atomically by `coverage --refresh`, and, when `backfill.coverage.enabled`
is true (off by default), after every applied pass: `complexity`, `timeline` and the neutral
`apply`. A failed refresh only warns on stderr; the apply has already landed and is audited.

**Advisory surfaces.** With `backfill.coverage.enabled` (off by default) `bh doctor` (a
`link_coverage` section, one row per hive) and `bh hive ready` (one optional `link coverage` line)
show the cached ratios. They read the cache file once with `check_cache(path)` and no inputs: no bd
export, no `git log`, no doc walk, so they stay cheap, and with the check disabled they do not open
the cache at all. A ratio below `backfill.coverage.warn_below` (default `0.60`, judged per ratio;
an empty ratio never warns) renders a warning and is marked `!`; it never fails `hive ready`, whose
coverage line is optional. States:

| State | Shown when | Rendered |
|---|---|---|
| ok / warn | the cache is readable and was measured with the configured `code_types` and `doc_roots` | the three ratios, `!` on those below the threshold |
| never computed | there is no cache file | a hint to run `bh backfill coverage --refresh` |
| stale | the file is unreadable, corrupt or of a version this build does not read, or it was measured with other `code_types` / `doc_roots` than configured | no figures (they would not be current) and the same hint |

`stale` here is narrower than the `stale` of `bh backfill coverage`: whether the beads, commits or
docs changed since the cache was written is exactly what the cheap read cannot know, so a cache
that merely aged shows its ratios with their `[cached <time>]` stamp. Run `coverage` to compare the
inputs. The verdict logic is `beadhive_backfill.coverage_advisory.evaluate`.

**For later passes.** The seam is one call. A new applied pass ends with
`backfill_cli.refresh_coverage_after_apply(hive, quiet=as_json)`, or, from a module that
`backfill_cli` imports (as `complexity_backfill` is), appends to its own `APPLIED_HOOKS` list. A
caller that has the inputs without a full export (an incremental run) builds a
`beadhive_backfill.coverage.CoverageReport` with `compute_coverage` and persists it with
`write_coverage_cache`; a cheap advisory check reads the cache with `check_cache(path)` and no
inputs, and gets `unverified` instead of a freshness claim.

## Reconcile statuses

`beadlog reconcile` classes every proposed bead as NEW, PRESENT, NOISE or DRIFT against the
baseline. The skill's `reconcile.sh` printed five statuses per doc; the `docs` strategy keeps the
same five names (they appear in its report) and maps each to an engine class and event:

| `reconcile.sh` status | Meaning | Engine class | Event |
|---|---|---|---|
| `PRESENT-in-sync` | a bead already carries the doc as `external_ref` | PRESENT | none |
| `PRESENT-needs-stamp` | the doc names a bead that has no `external_ref` | DRIFT | `set` of `external_ref` |
| `DRIFTED-ref` | the named bead points at a different doc | DRIFT | none; reported, never overwritten |
| `DANGLING` | the named bead is not in the baseline | DRIFT | none; reported |
| `UNMATCHED` | no bridge resolved the doc | NEW, PRESENT or NOISE once judged | `create` for NEW, `set` of `external_ref` for PRESENT, none for NOISE; an abstain is a residual |

The planning rows of `reconcile.sh --planning` map the same way: `NEW-epic` and `NEW-issue` are
NEW (a `create`, its `parent-child` and `blocks` edges, and a close when the plan has a summary),
and `PRESENT-in-sync` is PRESENT. For `git-commits` there is no per-doc status: a bead whose
`git.commits` gains shas is DRIFT, and a stored value that is not a JSON array of strings is
reported as DRIFT with no event.

## Judged residuals

Only the `docs` pass has a residual: the docs left after the deterministic bridges (reverse
`external_ref`, the `Beads:` line, the bead id in the commit that first added the doc).

1. **The judge is off by default.** Every `UNMATCHED` doc is then an abstain residual. The extract
   prints one `RESIDUAL <doc>` line each, and the archive annotation `bdlg.docs` lists them. A
   doc with no shortlisted candidate at all is NEW without a question.
2. **A human confirms the residual list.** Read each doc, then either stamp the bead you choose
   (`bd update <id> --external-ref <doc>`, or a `Beads:` line in the doc) and extract again, or
   leave it for a later pass. Nothing is written for a residual.
3. **Opt in to the judge** with `--judge-enabled true` (and the `--judge-command`,
   `--judge-model`, `--judge-timeout-seconds`, `--judge-max-calls-per-run` and
   `--judge-auto-accept` flags, or `BEADLOG_JUDGE_*` environment). Each doc is one choice among
   `present:N`, `noise:N`, `new` and `abstain` over the token-overlap shortlist. An answer
   outside the offered options becomes an abstain.
4. **Verdicts are recorded and reused.** Every verdict, with its confidence, engine and model, is
   stored in the archive, and `--option prior=<earlier extract>` (for `reconcile`, `--prior
   <earlier plan>`) reuses a recorded verdict by its query hash, so nothing is asked twice.
   `beadlog reconcile` marks a judged entry as needing review unless its confidence reaches the
   `--judge-auto-accept` threshold for its family; `bh backfill inspect plan.bdlg` shows the count.

## End to end

The flow is export, extract, reconcile, inspect, apply. The strategy name is `git-commits`,
`docs` or `planning`.

```bash
bh backfill export --out baseline.bdlg
bh backfill inspect baseline.bdlg            # note the group id and directory
beadlog extract <strategy> --root . --group <group id> --group-dir <prefix> \
  --extracted-at 2026-10-31T00:00:00Z --baseline baseline.bdlg -o proposed.bdlg
beadlog reconcile proposed.bdlg baseline.bdlg -o plan.bdlg
bh backfill inspect plan.bdlg                # classes, review count, verdicts
bh backfill apply plan.bdlg --dry-run        # the loader's loss report
bh backfill apply plan.bdlg
```

Run the extract from the repository the history lives in, with the baseline exported from the
hive you are going to apply to. After the apply, export again and extract again: the second
extract must propose nothing (the postcondition column of the matrix).

### `bh backfill commits`, `docs` and `planning` (passes #3 to #5)

Reconcile classes most of what these strategies propose as DRIFT, because the bead already exists:
the `git-commits` appends, the `docs` `external_ref` stamps and a close that arrives after the
bead was created. The neutral `bh backfill apply` applies NEW only. Each of these hive passes wraps
its strategy, declares `drift_applicable`, and so applies DRIFT and NEW alike through the full
envelope (plan hash, drift gate, pre-state, applying audit, verification, rollback). They read the
repository at the hive checkout and the bd store through the StoreLoader; no `beadlog` CLI,
baseline export or reconcile step is needed.

```bash
bh backfill commits --plan commits.bdlg                  # preview: writes nothing
bh backfill commits --apply --plan commits.bdlg          # apply the saved plan
bh backfill docs --option docs=docs/decisions,docs/design --plan docs.bdlg
bh backfill planning --option labels=complexity:MEDIUM --plan planning.bdlg
bh backfill planning --apply --plan planning.bdlg
```

| Pass | Proposes | DRIFT (applied) | NEW (applied) | `--option` keys |
|---|---|---|---|---|
| `commits` | `set` of `metadata.git.commits` appending shas | the append to a bead that exists | none | `rev`, `since`, `allow_shallow`, `git` |
| `docs` | `set` of `external_ref`; a judged NEW doc | the stamp on a bead that exists | `create` plus `origin:backfill` and `source:docs` for a judged NEW doc | `docs`, `top_k`, `id_prefix`, `new_type`, `labels`, `allow_shallow`, `git` |
| `planning` | `create` of epics and issues, edges, closes | a close that arrives after the bead exists | the epics, issues and edges of the tree | `planning`, `id_prefix`, `labels`, `issue_type`, `allow_shallow`, `git` |

`--option K=V` is repeatable and takes the same keys as `beadlog extract` (an unknown key is
refused). The options are recorded in the plan, and `--apply` rebuilds the pass from the plan, so
it takes no `--option`. `commits` pins `rev` to the commit it names at plan time, so the apply and
its proof read the planned history even when the branch moved in between. The plan fails to apply
if the hive changed after planning (the drift gate), and if the pass settings differ from the
ones recorded.

The judge is off by default. Without `backfill.judge.enabled` the `docs` pass hands the strategy no
judge: every unmatched doc stays an abstain residual, printed as `RESIDUAL <doc>: undecided; needs
human confirm`, and nothing is created for it. With the judge configured, each verdict is recorded
in the plan and the apply replays the recorded verdicts only. Strategy-level drift (a bead that
points at another doc, a malformed `git.commits`) is printed as `DRIFT <bead>: <reason>; not
overwritten` and never written.

Created `planning` beads carry `origin:backfill` and `source:planning`; name a routing tier with
`--option labels=complexity:<TIER>` or run `bh backfill complexity` afterwards. Idempotent: once a
plan is applied the next preview reports `0 beads would change`, and the engine proves that on
the fresh store before it reports the apply as done. An applied pass also refreshes the coverage
cache when `backfill.coverage.enabled`. A plan from these passes also applies through
`bh backfill apply PLAN.bdlg`.

## Incremental and continuous backfill

Linkage and timeline stay current with small runs instead of full re-scans.

**Cursors.** Each incremental pass keeps a cursor: the first-parent head it last read to
(`land_sha`) and, for `timeline`, the newest bd `events` row it saw (`events_at`, `events_row_id`).
They live in one small versioned JSON file per hive, beside the coverage cache
(`<state>/backfill/<hive>/cursors.json`), with a digest. A cursor moves only when an **applied**
incremental plan names it (the entry records the `plan_hash` and the `audit` of the run it came
from); a dry run never moves one. The `events` table's row id is a UUID and its `created_at` has
second resolution, so "after the cursor" is `created_at >= events_at` (inclusive: a row from the
cursor's own second is seen again, which is harmless because every proposal is content-addressed).

**`bh backfill commits --since-cursor`** and **`bh backfill timeline --since-cursor`** (dry-run
options; the saved plan records its range) read only what moved after the cursor;
`backfill.continuous` runs the same incremental passes. What each pass reads when it runs incrementally:

| Pass | What moved | How it is read |
|---|---|---|
| `commits` | the commits after `land_sha` | `git log <land_sha>..<rev>` (the plan pins `since` and `rev`) |
| `timeline` | beads with an `events` row at or after `events_at`, and the targets of the lands after `land_sha` (a batch land: every bead carrying its `batch:` label) | a `WHERE created_at >= ...` read, then the `events` rows of just those beads |

A moved bead's facts come from the same sources and rules as a full run, so a run proposes
**exactly the events a full run would add for those beads, and nothing else**. The plan report
gains an `incremental` section (`mode`, the `scope`, the `cursor_in`, the `cursor_next` and any
`warnings`); a run without `--since-cursor` is unchanged. What a cursor cannot see: a commit that
names a bead created after it, and run journals (host-local files): a periodic full run catches
both.

A cursor is a hint, never an authority. With no cursor the first `--since-cursor` run is a full
run (and, once applied, records the cursor). A cursor file that cannot be trusted (not JSON,
another schema, a failed digest, a malformed entry), or a cursor whose commit left the branch
history, is a warning on stderr and a full run.

**Continuous backfill** (`backfill.continuous`, off by default) runs the same passes after
`bh work merge` / `finish` lands on the integration branch (trigger `post-merge`) and after
`bh hive sync` pulls a hive (trigger `sync`). It is best effort: a failing loader, a corrupt cursor
or any pass error is one warning on stderr and the merge or sync carries on. It runs after the
merge slot is released and is bounded:

- `timeline` is scoped to the merged beads plus whatever moved since the cursor; `commits` reads
  from the cursor, or, with none, from the integration tip before the merge. A hook never starts a
  full run: with nothing to bound it, it says so and waits for one `--since-cursor` run.
- With no cursor the hook is scoped to the merged beads and records none, so it cannot skip commits
  an earlier run never read; with a cursor it follows and moves it. Both `commits` and `timeline`
  earn a cursor from `--since-cursor`; until a `timeline` cursor exists, a `timeline` hook stays
  scoped to the merged beads.
- Each run is the ordinary plan/apply envelope (plan hash, drift gate, applying audit,
  verification, rollback). Its recovery export holds only the beads it changes (a `.bdlg`, never a
  corpus archive) and its audit and recovery files replace the previous run's
  (`<state>/backfill/<hive>/continuous/`); the bd rows are read through a per-run read cache.
- When anything was applied the coverage cache is refreshed through the same seam as the other
  passes (when `backfill.coverage.enabled`).

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

## Gap fill

The events table cannot say anything about the ~2,500 of ~9,100 `bh` beads that predate it
(created before 2026-08-07) or were imported without a `created` row: they carry only an
inferred, low-confidence create. Dolt versions every `issues` row at every store commit, so their
*snapshots* can be recovered from `dolt_history_issues`. This is the opt-in, bounded gap fill
(`beadlog_sources.bd_gapfill`; [backfill-platform-adr.md](design/backfill-platform-adr.md)
section 6, "Dolt-history gap fill"). **It is slow (about an hour for the whole `bh` gap) and off
by default.**

```yaml
backfill:
  timeline:
    gapfill:
      enabled: false      # the default; true makes `export --history` run the gap fill
      batch_size: 25      # beads per history scan
      timeout_seconds: 180  # per query; a query past it stops the run (resumable)
```

The section is host-owned like the rest of `backfill` (a fleet push cannot switch it on). No CLI
flag was added: `bh backfill export --history` reads the config. See
[CONFIGURATION.md](CONFIGURATION.md#backfill-timeline-gapfill).

**What it reads.** Only `SELECT`s, through the injected command seam
(`bd -C <root> sql ...`), each with the configured timeout, restricted to `id IN (...)` batches of
`batch_size` beads, and only for beads with no `created` row in the events table; a bead that has
one is not even queried. Per batch:

1. a streaming **scan** of `dolt_history_issues` returning, per commit, the bead id, the commit
   hash, the commit date and a short server-side digest of exactly the columns BeadLog models
   (title, description, design, acceptance criteria, notes, priority, assignee, owner, external
   ref, due/defer times, estimate, spec id, type, status, close reason; never `updated_at` and
   the other volatile columns). The rows are ordered here by `(commit_date, commit_hash)`,
   **never DAG order** (commit dates are not monotone) and consecutive equal digests collapse.
2. a **fetch** of the full columns of only the commits where the digest changed, as `issues AS
   OF '<commit>'` point lookups, collapsed again on the projected values. A 30,000-version bead
   with four distinct states yields four states and four point lookups.

A bead with a single state is done after the scan. `dolt_history_labels`,
`dolt_history_dependencies` and `metadata` are **not** read (see the table: labels alone return
2.5x the rows of issues for the same walk), so a gap-filled bead has no label, edge or metadata
history before the events table; the annotation says so.

**Events.** For distinct states S1..Sk (k >= 2): a `set` per modeled field and a `phase` for the
status wherever a value changes between consecutive states, and the value of S1 for every field
that changes later. `occurred_at` is not known better than the commit date, so
`occurred_at == recorded_at ==` the dolt commit date, `provenance` is `derived`, confidence is
0.7 for a change between two snapshots and 0.3 for the first snapshot (the value existed *by*
that commit; when it was set is not recorded), and the evidence names the commit. The source is
`dolt` / `dolt:history/<commit>#<bead>`, so re-extraction gives identical event ids, and the
fold guard of the history export drops anything that would change a bead's current state. The
store's history only begins at its first commit (2026-07-25 on `bh`), so a bead that was never
edited since then yields no events at all (about 61 of 100 sampled).

**Resumable.** The collapsed states are saved after every batch to `<out>.gapfill.json` (beside
the archive, written atomically, removed when the archive is written). A timeout or interrupt
exits with `gap fill stopped: ... progress is saved in <path>, re-run to resume`; re-running the
same command continues at the next batch and produces the same events with no duplicates. A
cursor from a different bead list or batch size is ignored.

**Measured cost** (production `bh` store, 37,200 store commits, 9,145 issues, 2,529 of them
without a `created` row; read-only, one query at a time; the sample is ids 1000-1099 of the 2,529
sorted ids; seconds are wall clock for the scan plus the fetch):

| Batch size | Seconds | Scan rows | Digest states | Distinct states |
|---:|---:|---:|---:|---:|
| 1 | 11.9 | 9,248 | 15 | 15 |
| 5 | 15.0 | 157,514 | 20 | 17 |
| 10 | 18.3 | 337,143 | 29 | 23 |
| 25 | 34.8 | 860,381 | 54 | 37 |
| 50 | 70.1 | 1,754,249 | 109 | 82 |
| 100 | 141.1 | 3,551,982 | 204 | 143 |

A scan costs about 9 s fixed (every store commit is walked, whatever the batch) plus about 1.3 s
per bead; the fetch is 2-3 s per 40 changed commits. The full run of 2,529 beads is therefore
about **59 minutes at any batch size of 25 or more** (101 x 34.8 s, 51 x 70.1 s, 26 x 141.1 s),
and hours below that (batch 1 would be ~8 h). It returns about 34,000 rows per bead, so memory
grows with `batch_size` (the default 25 holds ~0.9 M small tuples). Roughly four in ten sampled
beads changed, with ~1.4 distinct states each, so the whole gap yields on the order of a thousand
changed beads and a few thousand events.

What measuring ruled out, kept so nobody retries it: `bd history <id>` is ~20 s per bead;
`bd sql` drops a query whose first result byte takes more than ~10 s (`i/o timeout`;
`dolt.pool-read-timeout`, `BEADS_DOLT_POOL_READ_TIMEOUT` and `BD_` equivalents had no effect), so
`GROUP BY`, window-function and `ORDER BY` shapes failed from 5 beads up (the 10-bead `GROUP BY`
died at 11.7 s) and get worse as the store grows; `content_hash` is unchanged by a status change
(`bh-9wpd.1` went open to closed under one `content_hash` on every commit), so the scan digests
the modeled columns itself; `commit_hash IN (...)` and `dolt_diff_issues` give no shortcut (still
the full walk); `EXPLAIN` is not supported through `bd sql --json`.

## First archival export (2026-10-31)

The first archival BeadLog of the `bh` hive was produced on 2026-10-31 with
`bh backfill export --history`. The gap fill was **not** included (operator decision, 2026-10-31);
it can be appended later as a separate archive.

**Storage.** The archive is kept in a private host directory, never in a git repo:
`/data/bees/beadhive/archives/bh/bh.history.2026-10-31.bdlg` (directory 0700, file 0600), sha256
`052ae24a84538219501484c7d08b706c0dacf4cfa6a2ce2fe65613fb012a0cbd`, 11,270,240 bytes. Archives
contain private bead content and must never be committed or pushed; only the path and the hash
are referenced. This is insurance, not a replacement for two-tier history retention (`bh-3vs6c`).

**Manifest.** 88,290 events across 9,147 beads.

- By kind: create 18,293, label_add 35,142, phase 13,813, edge_add 11,578, set 6,370,
  label_remove 2,356, comment 678, edge_remove 60.
- By source: `bd:events` 38,440, `bd:issues` 47,322, `bd:inferred` 2,528.
- Earliest recorded history is 2026-08-07T07:25:10Z (the beads date from 2026-06-29, the store
  commits from 2026-07-25).
- The consistency guard suppressed 770 history events (682 of them transient `metadata.holder`
  leases).

**Round-trip proof.** The full archive was loaded into an empty embedded scratch store (never
production) and re-exported. The load took 1,941 s; 77,497 events were re-exported.

- 44,108 archive event ids came back unchanged.
- 31,626 were dropped as `x-bd.superseded` (bd keeps only the latest state per facet).
- 12,556 were degraded and not returned under their id; 12,551 of them are `create` events
  re-minted natively (`recorded-time.reassigned`).
- Zero ids are missing without explanation: every missing id has a loss-report entry.
- 33,389 native extras (9,147 creates, 24,241 label_add, 1 phase).

Known limitations: create-event ids do not round-trip through the bd loader (bug `bh-pfrag`), and
a full-hive load needs a longer import timeout than the stock 120 s (bug `bh-f8lcb`).

**Reproduce.** `bh backfill export --history` writes the archive and `bh backfill verify` checks
it. Create a scratch store with an embedded `bd init` (never the production hive), load the
archive into it with `bh backfill apply`, then run `bh backfill export --history` against the
scratch store and compare the two event-id sets: every id missing from the re-export must have a
loss-report entry.

## Pass matrix

| Pass | Verb | Reads | Writes | Plan | DRIFT | Postcondition |
|---|---|---|---|---|---|---|
| `complexity` | `bh backfill complexity` | `bd list --all --include-infra` | `bd label add/remove`, one label at a time | JSON (`complexity-label-backfill`, schema 1) | applied | one valid `complexity:<TIER>` per routable bead; `model:*` untouched |
| `timeline` | `bh backfill timeline` | the bd `events` table, `git log --first-parent`, commit dates, run journals | `bd update` of `metadata.lifecycle.*` (and `metadata.bdlg`) only, one bead at a time | `.bdlg` (`timeline`, schema 1) | applied | every lifecycle key well-formed with its confidence; a second dry run proposes nothing |
| `reconcile` (neutral) | `bh backfill apply` | `bd export --all` | the bd StoreLoader (`bd import`, `bd update`, `bd close`, `bd dep`, `bd comments add`) | `.bdlg` from `beadlog reconcile` | reported only | second reconcile finds no NEW |
| `commits` (`git-commits`) | `bh backfill commits` (neutral: `beadlog extract git-commits`) | `git log` (`rev`, `since`) and the bd store's bead ids | one `set` of `metadata.git.commits` per bead, appending shas | `.bdlg` (`commits`, schema 1) | appends are DRIFT; applied | a second plan proposes nothing |
| `docs` | `bh backfill docs` (neutral: `beadlog extract docs`) | the doc dirs, the first-add commit of each doc and the bd store | `set` of `external_ref` (DRIFT); `create` plus `origin:backfill` and `source:docs` for a judged NEW | `.bdlg` (`docs`, schema 1) | stamps are DRIFT; applied | a second plan finds every doc `PRESENT-in-sync` |
| `planning` | `bh backfill planning` (neutral: `beadlog extract planning`) | the `.planning` tree and the git add date of each plan and summary | `create` of epics and issues, `parent-child` and `blocks` edges, closes | `.bdlg` (`planning`, schema 1) | a later close is DRIFT; applied | a second plan proposes nothing |

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

## Retired entry points

The strategies replace three older entry points:

- `scripts/backfill_commit_linkage.py` and `scripts/bead_commit_correlation.py` are deprecation
  shims. Each prints a warning on stderr that names `beadlog extract git-commits` (the first
  also names `beadlog reconcile` and `bh backfill apply`). They are removed in the
  release after next. The shim of `backfill_commit_linkage.py` still writes through
  `beadhive.git_linkage`, which the neutral flow does not.
- `scripts/reconcile.sh` of the `bh:backfill` skill is retired by the skills hive, where it
  lives. The follow-up is `bh-skls-nzz`: rewrite `bh:backfill` as the BeadLog-import layer and
  retire `reconcile.sh`.

## The `bh:backfill` skill

With the strategies in `beadlog-sources`, the `bh:backfill` skill becomes the BeadLog-import
layer: it takes a BeadLog and brings it into a hive through this CLI (export a baseline,
reconcile, preview with `bh backfill inspect` and `apply --dry-run`, apply through the safety
envelope, and prove idempotency with a second extract). Building a BeadLog from a repository's
history is the producing half, which the strategies above do on their own, with no hive. The
neutral skill that builds a BeadLog from a repository's history lives in `beadhive/skills` as its
own BeadHive-free skill directory, tracked as `bh-skls-8vr`.

## Defaults

Every new behavior is off until asked for: the Haiku judge (`backfill.judge.enabled=false`),
the dolt_history gap fill (`backfill.timeline.gapfill.enabled=false`), the coverage cache refresh
after an applied pass (`backfill.coverage.enabled=false`), continuous backfill after merges and
syncs (`backfill.continuous.enabled=false`), deletion and erasure in
the bd loader, `export --history` (the plain export is the default), and any write (a pass,
`timeline` included, previews unless `--apply`).
