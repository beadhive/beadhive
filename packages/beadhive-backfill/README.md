# beadhive-backfill

BeadHive-specific backfill, per the
[backfill platform ADR](../../docs/design/backfill-platform-adr.md) (section 2). A **library**
package: depends only on [`beadlog`](../beadlog) and [`beadlog-sources`](../beadlog-sources),
each through its public `__all__`, and never imports `beadhive`. Root wires the bd store loader
and the `bh backfill` CLI on top of it.

This scaffold holds the contracts only (`beadhive_backfill.contracts`):

- `HivePass` — a hive-policy pass that proposes events against a baseline;
- `StoreLoader` — maps events onto a store, reads it back as a baseline, reports losses;
- `AuditSink` with `AuditEntry`, `AuditProgress`, `AuditRecovery` and `AuditState` — the durable
  apply audit generalized from `complexity_backfill`.

The apply envelope (plan hash, drift gate, pre-state export, audit, rollback, idempotency
proof) and the policy passes land in later beads.

Tests: `uv run --package beadhive-backfill pytest packages/beadhive-backfill/tests`.
