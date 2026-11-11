# beadlog-sources

BeadHive-free BeadLog producers, per the
[backfill platform ADR](../../docs/design/backfill-platform-adr.md) (sections 2 to 4). A
**library** package: depends only on [`beadlog`](../beadlog) through its public `__all__`, and
never imports `beadhive`.

- **Contracts** (`beadlog_sources.contracts`): `SourceStrategy` and `ExtractRequest` (producers
  registered in the `beadlog.sources` entry-point group); `JudgmentPort` with `ChoiceQuery`,
  `ScoreQuery`, `Judgment` and `Fallback`, shaped like Jev's Choice/Score primitives.
- **Evidence IR** (`beadlog_sources.evidence`): source records as frozen `Artifact`s carrying
  un-interpreted `Ref`s, typed by the closed `Category` set (`change`, `review`,
  `tracker-item`, `design-record`, `plan-item`, `release-marker`), each with a required-attrs
  shape validated at construction (`ArtifactShapeError` names the key, category and attr). An
  `EvidenceCorpus` is written as `bdlg.evidence/1` JSONL: one header per reader (name, version,
  options, cursor), then records sorted by key, every line RFC 8785 canonical JSON and every
  record id content-addressed, so a round trip is byte-identical
  ([schema](src/beadlog_sources/schemas/bdlg-evidence-v1.schema.json), copied to
  `docs/schemas/beadlog/bdlg-evidence-v1.schema.json`). Not part of the BeadLog format. The
  fixtures kit `beadlog_sources.testing.evidence` builds one artifact of any category per call.
- **Collate B1+B2** (`beadlog_sources.collate`): `collate(corpus, baseline, directory, *, local)`
  builds the B1 `Indexes` (sha, merge children, tag range, PR commits, path to changes, doc add
  commit, key) and resolves every `Ref` into a `ResolvedRef`: bead tokens local (the baseline's
  ids) then foreign (`LedgerDirectory`, exact membership, never a prefix) else unresolved;
  sha/pr/issue/path/tag to artifacts. Output `bdlg.collation/1`
  ([schema](src/beadlog_sources/schemas/bdlg-collation-v1.schema.json), copied to
  `docs/schemas/beadlog/`), canonical bytes; `verdicts`/`conflicts`/`residual` are reserved
  for B5 and B6.
- **Collate B3** (`beadlog_sources.cluster`, run by `collate(..., options=ClusterOptions())`):
  ordered claim rules, the first claim wins: plan item, explicit local bead id (an `attach`
  unit), PR, first-parent merge, then one unit per leftover commit whose type is in
  `include_types` (default `feat`). `review`/`merge`/`commit` units are grouped into area epics
  by path-prefix override, else majority CC scope, else top-level directory, else release
  window, with the `areas` override map. Each `Unit` keeps its key (the later `external_ref`),
  rule and claimed artifact keys; release markers never make units.
- **`bd` baseline strategy** (`beadlog_sources.bd`, entry point `bd`): one `bd -C <root> export
  --all` read (never a `bd history` loop) turned into current-state events per issue row — a
  `create` snapshot, a `phase` when not open, `label_add` / `edge_add` per label and dependency,
  a `comment` per comment — with `occurred_at` from `created_at` / `closed_at` / `started_at` /
  dependency and comment `created_at` and `recorded_at` from `updated_at`. Events a bd loader
  stored in `metadata.bdlg.<facet>` (`facet_key`, `envelope`) are re-emitted with their original
  ids when their recomputed id matches and they agree with the row; native events cover the
  rest, so the fold always equals the row. Floats in bd metadata travel as decimal strings.
- **`git-commits` strategy** (`beadlog_sources.commits`, entry point `git-commits`): the two-step
  bead-id matcher (loose candidates over each commit message, resolved against the baseline's own
  beads) proposing one append-only `set` of `metadata.git.commits` per bead; a malformed stored
  value is reported as drift and never overwritten, and a shallow clone is refused.
- **`docs` strategy** (`beadlog_sources.docs`, entry point `docs`): decision and design docs
  bridged to beads (existing `external_ref`, frontmatter `Beads:`, the bead id in the first-add
  commit), then a full-corpus lexical shortlist and a judged residual; proposes `external_ref`
  stamps and, for a judged NEW, a `create`. With no judge (the default) every unmatched doc is an
  abstain residual. `ExtractRequest.judge` carries the judge.
- **`planning` strategy** (`beadlog_sources.planning`, entry point `planning`): a GSD `.planning`
  tree as epics, issues, `parent-child` and `blocks` edges and closes, dated from git, idempotent
  on `external_ref`; created beads carry `origin:backfill` and `source:planning`.
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
