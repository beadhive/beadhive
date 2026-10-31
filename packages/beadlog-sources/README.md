# beadlog-sources

BeadHive-free BeadLog producers, per the
[backfill platform ADR](../../docs/design/backfill-platform-adr.md) (sections 2 to 4). A
**library** package: depends only on [`beadlog`](../beadlog) through its public `__all__`, and
never imports `beadhive`.

- **Contracts** (`beadlog_sources.contracts`): `SourceStrategy` and `ExtractRequest` (producers
  registered in the `beadlog.sources` entry-point group); `JudgmentPort` with `ChoiceQuery`,
  `ScoreQuery`, `Judgment` and `Fallback`, shaped like Jev's Choice/Score primitives.
- **`bd` baseline strategy** (`beadlog_sources.bd`, entry point `bd`): one `bd -C <root> export
  --all` read (never a `bd history` loop) turned into current-state events per issue row — a
  `create` snapshot, a `phase` when not open, `label_add` / `edge_add` per label and dependency,
  a `comment` per comment — with `occurred_at` from `created_at` / `closed_at` / `started_at` /
  dependency and comment `created_at` and `recorded_at` from `updated_at`. Events a bd loader
  stored in `metadata.bdlg.<facet>` (`facet_key`, `envelope`) are re-emitted with their original
  ids when their recomputed id matches and they agree with the row; native events cover the
  rest, so the fold always equals the row. Floats in bd metadata travel as decimal strings.
- **`bd-history` strategy** (`beadlog_sources.bd_timeline`, entry point `bd-history`): the bd
  `events` table (read by `beadlog_sources.bd_history`) as BeadLog history events — `create`,
  `phase`, `set`, `label_add/remove`, `edge_add/remove`, `comment` — `observed`, with stable
  `bd:events/<row uuid>` sources and `occurred_at` kept distinct from `recorded_at`. A bead with
  no `created` row gets an `inferred` low-confidence create from its issues row; revert artifacts
  are suppressed; and `consistent_history` drops anything that would change the baseline's folded
  current state, so `fold(baseline + history) == fold(baseline)`. `history_annotation` builds the
  `bdlg.history` manifest annotation (per-source counts, earliest recorded date, pre-retention
  gap). No hive conventions live here: they are `beadhive-backfill`'s timeline pass.
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
