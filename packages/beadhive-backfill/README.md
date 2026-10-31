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
`ArchivePreStateExporter` (the pre-state as a `.bdlg`). Policy passes (complexity, identity
labels, lifecycle conventions) land in later beads.

Tests: `uv run --package beadhive-backfill pytest packages/beadhive-backfill/tests`.
