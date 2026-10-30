# ADR: Backfill platform — BeadLog interchange, three library packages, neutral production vs hive import

- **Status:** Accepted (architecture and packaging). The BeadLog v0 wire details are decided in
  `bh-65jen`; the first bi-temporal store target is gated on spike molecule `bh-5wu0u`.
- **Date:** 2026-10-30
- **Decision owner:** Beadhive maintainers (operator-approved planning session)
- **Related work:** workstream `bh-pu6n6`; sub-epics `bh-ebopn` (BeadLog format and engine),
  `bh-8a7ts` (source strategies), `bh-b3xhd` (timeline and archival export), `bh-yvg41`
  (coverage and continuous backfill), `bh-5wu0u` (XTDB spike); `bh-skls-svo` (skills hive)
- **Supersedes:** `bh-9wpd` (`bh import`, parked since 2026-07), whose design is absorbed here;
  `bh-9wpd.8` is carried forward as deferred source strategies.
- **Amends:** [`docs/MODULES.md`](../MODULES.md) principle 9 and the landed `packages/*`
  section, each with a dated note pointing back here.
- **Research:** [`beadlog-store-landscape-research.md`](beadlog-store-landscape-research.md)
  (store survey, measurements, and every source consulted).

## Context

Four unrelated tools in this repo call themselves "backfill", and they share no code:

| Tool | Where | Safety envelope |
|---|---|---|
| `bh backfill-complexity` | `src/beadhive/complexity_backfill.py` | Full: hash-gated plan, corpus-drift refusal, pre-state export, `applying` audit with checkpoints, rollback, second dry run must be empty |
| Commit linkage backfill | `scripts/backfill_commit_linkage.py` + `scripts/bead_commit_correlation.py` | Idempotent append writer, but a script: `sys.path` hack, guessed prefix, per-bead non-atomic writes |
| `bh:backfill` skill | `beadhive/skills: skills/backfill/scripts/reconcile.sh` | Human confirm gate; 300 lines of bash that silently stopped after 37 of 120 docs on this repo (`bh-skls-svo`) |
| `bh plan repair` | `src/beadhive/plan.py` | Repairs molecule setup only; out of scope here |

Meanwhile, history is eroding. On hive `bh`, beads date from 2026-06-29, but Dolt commit history
starts 2026-07-25 and the bd `events` table starts 2026-08-07. Bead stores that accept past
events (valid time distinct from recorded time) are expected soon, and `docs/BEAD_GROUPS.md`
already requires imported and backfilled records to keep their occurrence time. Linkage baselines
measured 2026-10-30:

- closed code-bearing beads with recorded commits: 1520/2029 (74.9%);
- commits resolving to a bead: 3162/3778 (83.7%);
- docs linked to a bead: about 1/271.

## Decision

### 1. BeadLog is the interchange format and the plan format

BeadLog (`.bdlg`) is a ZIP holding `mimetype`, `MANIFEST.json`, schemas and size-partitioned
JSONL parts. Each part is an **op-log** of atomic events: `create`, field-level `set`,
`label_add`/`label_remove`, `edge_add`/`edge_remove`, `comment`, `phase`, `retire`, `delete`,
`tombstone`, and `checkpoint` (full state). Every event carries:

- `occurred_at` — valid time, when it happened;
- `recorded_at` — source transaction time;
- `extracted_at` — when it was extracted;
- source and evidence, provenance (`observed`/`derived`/`inferred`) and confidence.

Event ids are SHA-256 over RFC 8785 canonical JSON of the identity fields only, so re-extraction
gives the same id and two logs union-merge by id. Sets always change by add/remove; scalars
resolve last-writer-wins on `(occurred_at, recorded_at, id)`. A loader refuses unknown required
capabilities and reports every field it cannot keep. Recorded-time preservation is monotone-only
wherever it exists, so it is a declared capability (`recorded-time-monotone-load`) backed by an
optional global recorded-time index.

The same file is the backfill **plan**:

1. strategies emit a proposed `.bdlg`;
2. reconcile diffs it against a baseline `.bdlg` (the target exported as a BeadLog);
3. apply is a store loader.

New stores add a loader. Strategies never change.

### 2. Three library packages, with root keeping only BeadHive wiring

Per [`package-class-library-vs-plugin-adr.md`](package-class-library-vs-plugin-adr.md), these are
three **library** packages under `packages/*`, copied from `packages/_template`. They stay
`Private :: Do Not Upload` until the format settles after the XTDB verdict. None of them imports
`beadhive`.

| Package | Owns | Depends on |
|---|---|---|
| `beadlog` | Format: events, kinds, canonical ids, manifest, capabilities, JSON Schemas, **merge/union** and **diff/fold**, `.bdlg` archive I/O, `beadlog.testing` loader-conformance kit | stdlib only |
| `beadlog-sources` | BeadHive-free producers: source strategies (entry-point group `beadlog.sources`: git commits, docs bridges, GSD planning, neutral `bd` baseline via the bd binary; later Task Master, backlog.md, CHANGELOG); **reconcile** against a baseline (NEW/PRESENT/NOISE/DRIFT); `JudgmentPort` with deterministic and `bh-judge/1` command judges (`[haiku]` extra); standalone `beadlog` CLI | `beadlog` |
| `beadhive-backfill` | BeadHive-specific: the hive apply envelope lifted from `complexity_backfill` (plan hash, drift gate against a fresh baseline, pre-state export, audit, rollback, idempotency proof); hive-policy passes (complexity labels, identity labels and `origin:backfill`, AGF lifecycle conventions, BeadHive run journals) | `beadlog`, `beadlog-sources` |
| root `src/beadhive` | bd StoreLoader (identity labels via `bd.import_labeled`), `bh backfill` CLI and catalog rows, config, `doctor` / `hive ready` checks, post-merge hook | all three |

**Merge belongs to the format.** Combining logs is part of what BeadLog means, so `beadlog`
owns it. Every producer and consumer then gets the same answer.

**Reconcile needs no store.** "Existing beads" is a baseline BeadLog, so classification is
`diff(proposed, baseline)`, and even bead-id resolution for commit matching uses the baseline's
id set.

### 3. Producing a BeadLog is neutral; importing one into a hive is BeadHive-specific

The skills split along the same line:

- a **neutral beadlog skill** builds a BeadLog from a repo's own history: run strategies,
  resolve the judged residual with a human, merge, verify. Its home (BeadHive skills repo or a
  separate neutral repo) is still to be decided.
- **`bh:backfill`** imports a BeadLog into a hive: export a baseline, reconcile, preview, apply
  through the envelope, verify idempotency.

### 4. Judgment is a typed port shaped for Jev

`choose` (pick one option) and `score` (ordered criteria) return `outcome | abstain`,
probabilities, confidence, option count, rationale and engine. This follows
[`jev-adoption-tracks-proposal.md`](jev-adoption-tracks-proposal.md) (lines 214–240).

- The deterministic lexical engine is the default.
- A versioned `bh-judge/1` JSON envelope goes to a configured subprocess (the
  `semantic_selection` precedent). The bundled Haiku command is opt-in (`enabled=false`) and
  falls back to deterministic on any failure.
- Moving to Jev later means pointing `command` at it; no code changes.
- Verdicts are recorded in the plan and are never asked again at apply time.

### 5. Timeline sources and store targets

- **Timeline sources.** Lifecycle history comes from one bulk read of the bd `events` table,
  first-parent `chore(merge)` commits and commit dates. Never loop `bd history` over the corpus:
  about 20 s per bead, and one version per store-wide commit.
- **bd target.** bd/Dolt is loader #1, with occurrence time kept in `metadata.bdlg.*`.
- **XTDB target.** XTDB v2 is the first bi-temporal target to spike: user-set valid time,
  monotone backfill of system time (v2.2+), native as-of reads, `ERASE` for tombstones,
  schemaless nested documents. On GO, its loader is its own package depending only on `beadlog`.
- A second store candidate gets evaluated only after the XTDB verdict.

### 6. Defaults

Every new behavior is configurable and off by default: the Haiku judge, coverage checks,
continuous backfill, and the Dolt-history gap fill.

## Why three packages now (rebutting MODULES.md principle 9)

Principle 9 defers distribution splits until isolation demonstrably helps. That case is made
here for reasons specific to this capability:

1. **External consumers by design.** BeadLog exists so *other* bead stores can write loaders.
   They should depend on a stdlib-only format package, not on BeadHive.
2. **Neutral producers.** Building a BeadLog from git, docs or planning trees is useful to any
   beads user. Keeping strategies BeadHive-free keeps that true and makes it testable.
3. **The cycle.** Root's `bd.py` sits in the 65-module import cycle. Packages that cannot import
   `beadhive` stay out of it by construction, and the import checker enforces that.
4. **Test closure.** Format and strategy changes run their own package tests, not the root
   partition.

## Alternatives considered

- **Internal module `src/beadhive/modules/backfill`.** This was the first draft. Extracting it
  later would be costly, and its strategies would be BeadHive-bound.
- **Two packages (format + engine).** Rejected because strategies would sit inside a BeadHive
  package and need BeadHive to build a BeadLog.
- **Fold strategies into `beadlog`.** Fewer boundaries, but a heavier format core that consumers
  must take on.
- **JSON Patch deltas.** Rejected: array-index operations on labels and dependencies are
  order-fragile and not idempotent. JSON Merge Patch cannot add one set element or clear a field.
- **Post-image journal** (bd v0 `EventRecord`, BDP postimage). Rejected: no deltas, no occurrence
  time, lossy diff.
- **In-process Anthropic SDK judge.** Rejected in favor of the command envelope, which makes Jev
  a configuration swap and adds no SDK dependency.
- **Snapshot replay via `bd history`.** Rejected as infeasible at corpus scale. Kept only as the
  opt-in, chunked `dolt_history_*` gap fill.

## Consequences

- Three new workspace members, which the import checker must classify. The `packages` attest key
  covers their tests.
- `bh backfill-complexity` stays as an alias of `bh backfill complexity`. The flat leaf is
  published wire surface.
- Strategies move out of `scripts/` and out of the skill's bash. Old entry points become
  deprecation shims.
- History capture improves before more erodes: `bh-4kec3` produces the first archival BeadLog of
  `bh`. It does not replace two-tier history retention (`bh-3vs6c`).
- A tracker-sync strategy (GitHub/Jira) remains out of scope; that is `bh sync`.
