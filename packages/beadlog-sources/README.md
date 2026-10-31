# beadlog-sources

BeadHive-free BeadLog producers, per the
[backfill platform ADR](../../docs/design/backfill-platform-adr.md) (sections 2 to 4). A
**library** package: depends only on [`beadlog`](../beadlog) through its public `__all__`, and
never imports `beadhive`.

This scaffold holds the contracts only (`beadlog_sources.contracts`):

- `SourceStrategy` and `ExtractRequest` — a producer of proposed events from one kind of local
  source, registered in the `beadlog.sources` entry-point group;
- `JudgmentPort` with `ChoiceQuery`, `ScoreQuery` and `Judgment` — the typed seam for fuzzy
  calls, shaped like Jev's Choice/Score primitives.

Source strategies, reconcile against a baseline (NEW / PRESENT / NOISE / DRIFT), the judgment
engines and the standalone `beadlog` CLI land in later beads.

Tests: `uv run --package beadlog-sources pytest packages/beadlog-sources/tests`.
