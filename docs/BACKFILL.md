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
| `beadlog-sources` | BeadHive-free producers: source strategies (the neutral `bd` reader, `git-commits`, `docs` and `planning`), reconcile, judges, the standalone `beadlog` CLI |
| `beadhive-backfill` | the envelope (`BackfillEngine`, `Plan`, audit and pre-state ports) and the hive-policy passes (`beadhive_backfill.complexity`) |
| root `beadhive` | the bd StoreLoader (`backfill_bd`), the `bh backfill` CLI and its catalog rows |

Root never holds engine logic; the published wheel vendors the three libraries' import packages
(they stay `Private :: Do Not Upload`).

## Verbs

```text
bh backfill complexity   [--hive H] [--dry-run | --apply] [--plan F] [--pre-state F] [--audit F] [--json]
bh backfill export       [--hive H] [--out F.bdlg] [--json]
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

### `bh backfill export`

Writes the hive's current state as a `.bdlg` archive: one `bd export --all` read turned into
events by the neutral bd strategy (`beadlog_sources.bd`), which re-emits the ids of events a
backfill loaded earlier. The archive declares one bead group whose id is a UUIDv5 over the hive's
`provider/org/repo` (the same on every export, so re-exports union-merge) and whose directory is
the hive prefix; the default file is `<prefix>.bdlg` in the current directory. Use it as the
baseline a `beadlog reconcile` diffs against.

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
changes), which a neutral `bh backfill apply` reports but does not apply; applying the appends
needs a pass that marks DRIFT applicable. `scripts/backfill_commit_linkage.py` and
`scripts/bead_commit_correlation.py` are deprecated shims over the same matcher.

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
`allow_shallow`. Stamps are DRIFT to reconcile (the bead exists), which the neutral `bh backfill
apply` does not apply; load them through the bd StoreLoader. Applying them and extracting again
reports every doc `PRESENT-in-sync` and proposes nothing.

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

## Pass matrix

| Pass | Verb | Reads | Writes | Plan | DRIFT | Postcondition |
|---|---|---|---|---|---|---|
| `complexity` | `bh backfill complexity` | `bd list --all --include-infra` | `bd label add/remove`, one label at a time | JSON (`complexity-label-backfill`, schema 1) | applied | one valid `complexity:<TIER>` per routable bead; `model:*` untouched |
| `reconcile` (neutral) | `bh backfill apply` | `bd export --all` | the bd StoreLoader (`bd import`, `bd update`, `bd close`, `bd dep`, `bd comments add`) | `.bdlg` from `beadlog reconcile` | reported only | second reconcile finds no NEW |

Scope: `complexity` governs epics, features, tasks, bugs and chores in `open`, `blocked`,
`in_progress`, `deferred` or `closed`; the merge-slot record (`gt:slot`) and every other bead are
counted by reason and never touched, so a change to them never invalidates a plan.

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
deletion and erasure in the bd loader, and any write (a pass previews unless `--apply`).
