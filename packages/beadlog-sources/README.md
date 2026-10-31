# beadlog-sources

BeadHive-free BeadLog producers, per the
[backfill platform ADR](../../docs/design/backfill-platform-adr.md) (sections 2 to 4). A
**library** package: depends only on [`beadlog`](../beadlog) through its public `__all__`, and
never imports `beadhive`.

- **Contracts** (`beadlog_sources.contracts`): `SourceStrategy` and `ExtractRequest` (producers
  registered in the `beadlog.sources` entry-point group); `JudgmentPort` with `ChoiceQuery`,
  `ScoreQuery`, `Judgment` and `Fallback`, shaped like Jev's Choice/Score primitives.
- **Judges** (`beadlog_sources.judges`): `DeterministicJudge` (lexical; one-hot at 1.0 or
  abstain) and `CommandJudge`, which speaks the versioned `bh-judge/1` JSON envelope
  ([schema](src/beadlog_sources/schemas/bh-judge-v1.schema.json), copied to
  `docs/schemas/bh-judge-v1.schema.json`) to a subprocess and falls back to deterministic on a
  timeout, a non-zero exit, or malformed or wrong-version output, recording the fallback.
  `JudgeSettings` mirrors root bh's `backfill.judge` section; every default is off. Choice
  confidence is `clamp((n * p_max - 1) / (n - 1), 0, 1)`.
- **Reconcile** (`beadlog_sources.reconcile`): `reconcile(proposed, baseline, judge)` classifies
  each proposed bead NEW / PRESENT / NOISE / DRIFT on top of `beadlog.diff` and `write_plan`
  writes the plan `.bdlg`. The manifest `plan` section holds the hashes and judge identity; the
  per-bead classes and every verdict (engine, model, confidence, rationale, fallback) are in
  manifest annotation `bdlg.reconcile`, and `prior_verdicts` reuses them so a recorded verdict is
  never asked again.
- **`beadlog` CLI** (`beadlog_sources.cli`, stdlib `argparse`): `merge`, `reconcile`, `inspect`,
  `verify`, and one `extract <name>` subcommand per installed `beadlog.sources` strategy. Judge
  settings come from `--judge-*` flags, then `BEADLOG_JUDGE_*` env vars.
- **`bh-judge-haiku`** (`beadlog_sources.haiku`): the opt-in reference `bh-judge/1` command over
  the Anthropic Messages API. Install the `haiku` extra (`beadlog-sources[haiku]`, which adds
  `httpx`); the key comes from `ANTHROPIC_API_KEY` only and is never logged or recorded.

Tests: `uv run --package beadlog-sources pytest packages/beadlog-sources/tests`.
