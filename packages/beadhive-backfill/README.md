# beadhive-backfill

BeadHive-specific backfill, per the
[backfill platform ADR](../../docs/design/backfill-platform-adr.md) (section 2). A **library**
package: depends only on [`beadlog`](../beadlog) and [`beadlog-sources`](../beadlog-sources),
each through its public `__all__`, and never imports `beadhive`. Root wires the bd store ports
and the `bh backfill` CLI on top of it.

## The apply envelope

`BackfillEngine` is the transaction skeleton lifted from `complexity_backfill`, with every hive
or bd specific arriving through a port:

- `plan(pass, reader, judge)` reads a fresh baseline, lets the pass propose events, classifies
  them with `beadlog_sources.reconcile` (NEW / PRESENT / NOISE / DRIFT) and seals a `Plan`: a
  `.bdlg` whose manifest `plan` section carries the pass, its schema version, the corpus hash
  (the pass's projection of the baseline) and the plan hash. Pass and reconcile verdicts are
  recorded in the plan.
- `apply(plan, reader, loader, audit, pre_state=...)` verifies the plan hash, refuses corpus
  drift before any export or write, assesses losses with a loader dry run
  (`erasure.incomplete` needs acknowledgement), exports the pre-state and persists the
  `applying` audit before the first write, writes one bead per unit with
  `progress` / `recovery` checkpoints, then proves the result: the projection equals the
  expected post-state, the pass postcondition holds, and a second dry run proposes nothing
  using the plan's recorded judgments only (no judge is invoked). Any failure or interruption
  replays the loader inverses of the touched units in reverse and is audited.
- DRIFT proposals are reported but applied only when the pass sets `drift_applicable`.
- Inverses follow the format ADR's per-kind table: a `create` is undone by a `retire` with
  reason `backfill-rollback`, never a delete; `comment` and `tombstone` have none and are
  audited as rollback residue.

## Contracts

- `HivePass` (name, schema version, invalidating facets, `drift_applicable`, `propose`) with
  the optional hooks `ScopedPass.exclusion`, `VerifyingPass.verify` and `ReportingPass.report`;
- `StoreReader`, `StoreLoader` and `PreStateExporter`, the store ports;
- `AuditSink` with `AuditEntry`, `AuditProgress`, `AuditRecovery` and `AuditState`.

Store-neutral adapters: `JsonFileAuditSink` (the audit file beside the plan) and
`ArchivePreStateExporter` (the pre-state as a `.bdlg`).

## Passes

- `beadhive_backfill.complexity` — pass #1: exactly one canonical `complexity:<TIER>` label per
  routable bead, `model:*` labels never touched. `ComplexityPass` is the engine pass; the tier
  vocabulary and classifier arrive through the `ComplexityRules` port (root implements it over
  `beadhive.complexity`). The module also owns the published JSON plan/audit contract of
  `bh backfill complexity` / `bh backfill-complexity`: `build_plan`, `verify_plan` and
  `apply_plan`, which runs the JSON plan through `BackfillEngine` with row-store adapters. See
  [`docs/BACKFILL.md`](../../docs/BACKFILL.md).

- `beadhive_backfill.timeline` — pass #2: the AGF lifecycle conventions over the history readers
  (`merges`, `run_journals` and the neutral bd events table in `beadlog_sources`). `TimelinePass`
  proposes only `metadata.lifecycle.*` `set` events (`claimed_at`, `committed_at`,
  `last_committed_at`, `submitted_at`, `reviewed_at`, `merged_at`, `reopened_at`, `reopen_count`,
  `run_started_at`, `run_ended_at`), each with a `<key>_confidence` tier, `occurred_at` kept
  distinct from `recorded_at` (a land's committer date vs the bd `closed` event). The four bulk
  reads arrive through the `TimelineReaders` port (`PathReaders` over a checkout); a full read
  costs a fixed number of calls, never one per bead. `Timeline.of(bead)` returns a bead's whole
  timeline, history events included.

Identity-label passes land in a later bead.

Tests: `uv run --package beadhive-backfill pytest packages/beadhive-backfill/tests`.
