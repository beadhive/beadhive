# ADR: Backfill for merge-heavy and forked repos — merge units, git boundary, attachment, selector

- **Status:** Revised 2026-11-14 with the operator's rulings on the open decisions (D3 and D4 on by
  default; D8 cap exemption, `related` deferral, `--select` grammar and the D1 mixed outcome as
  recommended); awaiting operator approval at the review gate. Every
  feature bead of the molecule is blocked on it.
- **Date:** 2026-11-14
- **Decision owner:** Beadhive maintainers (operator approves; not delegated)
- **Related work:** molecule `bh-g0845` (0.38.0); this design bead `bh-5e4r1`; feature beads
  `bh-19q4u` (#159), `bh-8h2k8` (#154 part 1, called "154a" in the molecule), `bh-yg3ae`
  (#154 part 2), `bh-dviao` (#157), `bh-espkv` (#162). Reports: GitHub #154, #157, #159, #162;
  related `bh-t6j8i` (docs-pass titles).
- **Amends:** [`backfill-history-pipeline-adr.md`](backfill-history-pipeline-adr.md) (B3 merge
  rule, B4 attach tiers, B5 identify steps, the `bdlg.collation/1` vocabulary) and
  [`backfill-rerun-control-adr.md`](backfill-rerun-control-adr.md) (Q3 decisions: a batched
  selector write; Q4 reader excludes: a git-reader boundary). Both carry a matching pointer.
- **Grounded in:** main `fdaa02dd` (0.37.1). File and line references below are to that tree;
  paths under `packages/beadlog-sources/src/beadlog_sources/` are abbreviated `bls/`.

## Context

Field previews of 0.36.0, 0.37.0 and 0.37.1 over eleven real repositories (GitHub issues
154, 157, 159 and 162) show five failure shapes in merge-heavy repositories and forks:

1. **Merge units dominate and read badly.** Each free first-parent merge becomes a unit titled by
   the merge subject ("Merge pull request #7 from org/fix/render-crash"); 35-69 per repo, 553 of
   869 proposed beads in the worst. `include_types` is never consulted for merges
   (`bls/cluster.py:313-332`: `rule_merge` ignores it, `rule_commit` reads it), so coverage
   depends on how a PR happened to be merged. Branch-sync merges ("Merge branch 'main' into
   next") were 127 of 169 units in one repo.
2. **A residual or NOISE merge swallows its commits.** The commits a merge brings in are its
   artifacts, not units; if identify leaves the merge residual (91 of 93 on a 0.37.1 run, weak
   candidates scoring 1-5%), the feat commits inside appear nowhere in the plan.
3. **A fork reads its upstream.** `since=<sha>` is the only boundary, pinned by hand, and tags
   ignore the range (130 upstream release tags plus 10 `backup/*` tags read as release evidence).
4. **A doc piles onto the wrong unit.** One reorganising merge collected about 100 of about 120
   doc attachments; a `docs:`-added ADR attached to an unrelated commit.
5. **Decisions are one unit at a time.** Families of 100+ same-shape units need one command per
   unit, or turning off a whole rule.

Operator rules that bind every option (recorded on `bh-g0845`): everything new is opt-in and
default-preserving (unset means today's output, byte-identical goldens), except where the operator ruled
otherwise (D3 and D4, below), configurable per hive
through `backfill.hives.<prefix>.*`; any default-visible change needs an explicit decision here
and an UPGRADING entry; never fabricate timeline events; never derive bead-id prefixes
(`bh-ear6v`); never rewrite matched beads; bead history is never compacted or flattened.

Where this ADR calls something **default-visible** it means: a run with no new option set
produces different bytes (collation, plan, view, created beads) or different created beads than
0.37.1. The default-visible effect of every decision is stated in its own subsection and
tabulated in the summary.

## Summary of recommendations

| # | Decision | Recommendation | Default-visible effect |
|---|---|---|---|
| D1 | Merge-unit identity (#154 R5/R6) | **C, plus A as opt-in.** Always-on hidden-commit reporting (D5); opt-in `identify.merge_fallback = commits`; opt-in `identify.merge_identity = commits` (a merge is NEW when none of its commits has a candidate). B (promote the PR) is out. Mixed outcome defined below. | None beyond D5's reporting. |
| D2 | Merge type derivation | Branch prefix, then the most significant conventional type of the brought-in commits, then untyped. Optional `Unit.type`, set only when `collate.merge_types` is set. `merge_types` filters merges; `include_types` stays commit-only; unset means all merges and no type recorded. | None. |
| D3 | Branch-sync noise | A `Unit.noise` flag set by subject shape (no PR ref); identify turns it into a SKIP-class verdict at a new step, unit visible in `view unit`; a noise merge releases its commits to the commit rule. **On by default** (`collate.noise` defaults to `["branch-sync"]`; `[]` restores 0.37 output). | **Yes:** sync-merge units are flagged and project nothing; their commits go to the commit rule; identify verdicts and goldens change. UPGRADING. |
| D4 | Merge titles | Order PR title, merge body first line, first included commit subject, readable branch name; the review rule already covers the first. **On by default** (`collate.merge_titles` defaults to `derived`; `subject` restores 0.37 titles). The body line needs the reader attr `body_head`, recorded by default. | **Yes:** merge-unit titles change, identify's lexical verdicts shift, merge-artifact evidence gains `body_head`, goldens regenerate. UPGRADING. |
| D5 | Hidden-commit reporting | **Always on, additive.** New `hidden_commits` keys in the plan summary and the `beadhive.backfill.view/1` unit view, emitted only when non-zero. | Reports of runs that hid commits gain keys; runs that hid nothing are byte-identical. Created beads unchanged. |
| D6 | Selector for `decide` (#162) | `--select` (repeatable) `field op value`, `=` and `~`, AND only, fixed field list, reads the run's `collation.json` printing path and age, `--dry-run`, default `--max` cap with `--yes`, guard rails, one batched write, selector in each reason. Standing rules deferred. | None (new flag). |
| D7 | Git boundary (#159) | `exclude_revs` (resolved each run, `^sha`, unresolvable = error naming it), composed with cursor and since; resolved shas into `parse_key` and `run.json`; `tag_scope = all\|range` (default `all`), `tag_include`/`tag_exclude` globs; new `since_date`; `since` unchanged, not deprecated; the all-hex date workaround moves behind the strategy rather than being deleted. | None. |
| D8 | Doc attachment (#157) | `attach.max_per_unit` (default unset), `attach.rename_guard` (default off), opt-in `own-add` tier outside the default list, opt-in candidate ranking with the runner-up recorded. **`related` links are deferred out of 0.38.0.** | None. |
| D9 | Collation schema | **One additive `bdlg.collation/1` change** introduced by `bh-8h2k8` and extended by later beads. New members are emitted only when set; loaders accept the old exact shape and the old shape plus the optional members. No version bump. A pre-0.38 reader rejects a collation that uses the new vocabulary. | None for runs that opt out of D3 and D4 and set no other new option; otherwise the D3/D4 output changes. |

Net effect on defaults (operator ruling 2026-11-14): **two defaults change in 0.38.0, D3 and
D4, and this is a default-visible change.** For an unchanged configuration a 0.38.0 run (a)
flags branch-sync merges as noise, so they project no bead, (b) titles merge units from the
derived order instead of the merge subject, which shifts identify's lexical verdicts for merge
units (a merge that was PRESENT or residual by its subject tokens may now decide differently),
(c) records `body_head` on merge artifacts in the evidence, and therefore re-reads evidence once
(`parse_key` changes), and (d) adds D5's report keys on runs that hid commits. Goldens that hold
merge units regenerate. Every other decision stays opt-in and default-preserving. The opt-out
is `collate.noise = []` and `collate.merge_titles = subject`, which together restore 0.37.1
output byte for byte (a test in `bh-8h2k8` proves it). `bh-8h2k8` adds the
"## 0.37.1 → 0.38.0" UPGRADING entry giving the why, the effect and the opt-out.

## D1 -- Merge-unit identity (#154 R5, R6)

### Evidence

- A merge becomes a unit only when it is a free first-parent merge: `mainline_merges` excludes
  merges inside another merge (`bls/cluster.py:208`), and `rule_merge` (`:313-324`) creates a
  unit when any of its second-parent commits is free, sweeping those commits as artifacts, else
  claims the merge for the single unit that owns all its commits.
- Identify (B5) then sees one unit, keyed by the merge sha and titled "Merge pull request ...".
  Step 3 (`no-candidate`) sees only that title's tokens; a candidate that shares a token with
  the merge subject ("pull", "request", the branch words) keeps it out of step 3 and into the
  lexical step and then residual (`bls/identify.py:726-853`, the residual append at `:840`).
  The commits inside were never offered to identify.
- Nothing downstream reports them. `unit_view` counts decisions (`src/beadhive/backfill_views.py:
  104-125`) and shows an `artifacts` count; the commits are neither units, children nor links.
- A residual unit projects nothing (`bls/project.py:49`), so its commits drop out of the plan.

### Options

- **A. Commit-based identity.** A merge is identified through its commits: NEW when none of
  them matches a candidate; held otherwise.
- **B. Promote the PR.** Use the GitHub PR (`review` category) as the unit and identify it by
  its number/title. Needs the opt-in `github` source; does nothing for merges without a PR.
- **C. Report and fall back.** Never hide commits (report them), and on request emit them as
  their own commit-rule units when their merge ends residual or NOISE.
- **D. Do nothing.** Leave the merge rule and its identity as is.

### Recommendation: C in 0.38.0, with A as an opt-in

1. **Always-on reporting (D5)** is a correctness fix: the plan must not silently omit work.
2. **Opt-in fallback**, `backfill.identify.merge_fallback = none | commits` (default `none`):
   when a merge unit ends residual, NOISE or SKIP, replace it by the commit-rule units of its
   commits that `include_types` admits, then identify those units in the same run.
   *Constraint for `bh-yg3ae`:* residual is only known after B5, which runs after B3, so the
   fallback is a deterministic second B3+B5 pass over exactly those merges, not a B3 option.
   The replacement is by key, so a second run reaches the same units (the commit keys are the
   keys the commit rule would make).
3. **Opt-in commit-based identity**, `backfill.identify.merge_identity = unit | commits`
   (default `unit`, today's behaviour). Under `commits`, a merge unit that would go to the
   lexical step is first checked commit by commit with the step-3 test (does the baseline hold a
   bead or epic sharing a title token with that commit's subject?).
   - **All commits have no candidate:** the merge is NEW (`derived`, review `required`, like
     step 3), whatever candidate its own title matched.
   - **Mixed (some commits have a candidate, some do not):** the unit keeps the existing flow
     (lexical, judge, threshold, residual) unchanged, and the report names, per merge, which
     commits had a candidate and which did not. A mixed merge is a human decision: the commits
     that matched suggest the PR is already represented, and promoting it NEW would duplicate.
   - **All commits have a candidate:** unchanged flow, as for mixed.
   - A merge with no commits of its own (empty `merge_children`) is unchanged.
4. **B is out** of 0.38.0: it needs a source most repos do not enable, and the `review` rule
   already gives that behaviour where it is on (`bls/cluster.py:17-19`, `:296-302`).

### Trade-offs

- A keeps the one-bead-per-PR shape (R6) and fixes R5 without an extra source; its cost is a
  second candidate lookup per merge and the mixed case staying human.
- The fallback gives up the PR grouping for those merges, which is why it is opt-in and a
  per-merge consequence of a verdict, not a global rule switch.
- Both depend on D5 being visible first; the order in the molecule (reporting before opt-ins)
  is deliberate.

### Default-visible effect

None beyond D5's additive keys. `merge_fallback` and `merge_identity` default to today's flow.

## D2 -- Merge type derivation (#154 R2)

### Evidence

- `Unit` has no type (`bls/collate.py:482-511`); `include_types` is an option of the commit rule
  only (`bls/cluster.py:326-332`).
- The reader gives each commit `cc_type` (`bls/git_reader.py:228`); a merge's own subject is not
  conventional, so its `cc_type` is `None`.
- `ClusterOptions.from_json` refuses any member set other than `OPTION_MEMBERS`
  (`bls/cluster.py:155-160`), and `Unit.from_json` any set other than `_UNIT_MEMBERS`
  (`bls/collate.py:539-555`); D9 covers the vocabulary.

### Options

- **A. Branch prefix only.** Fast; misses `Merge pull request #7 from org/patch-1`.
- **B. Brought-in commits only.** Always available; a squash-style mixed PR gets an arbitrary
  type.
- **C. Prefix, then commits, then untyped.** The operator's recommended order.
- **D. Apply `include_types` to merges.** Simple, but it changes today's output for every
  repository that sets `include_types` (default-visible) and cannot express "fix commits but
  feat merges".

### Recommendation: C, with a separate `merge_types`

- **Derivation order.** (1) The branch prefix: the first path segment of the merged branch
  (`feat/x`, `fix/x`), taken from the subject shapes `Merge pull request #N from org/<branch>`
  and `Merge branch '<branch>'`, when it is a conventional type or one of a small fixed alias
  set (`feature` -> `feat`, `bugfix`/`hotfix` -> `fix`). (2) Else the most significant
  conventional `cc_type` among the merge's own commits, using a fixed significance order
  (`feat`, `fix`, `perf`, `refactor`, `revert`, `build`, `ci`, `docs`, `test`, `style`,
  `chore`; a commit with a breaking change counts as `feat`). (3) Else untyped (`None`).
  The branch name is parsed from the subject only; no refs are read (the reader has none).
- **New optional field** `Unit.type`, recorded only for merge-rule units, and only when
  `collate.merge_types` is set. The value `*` records the type without filtering (the same
  convention as `include_types`' `*`, `bls/cluster.py:ANY_TYPE`), which is what the selector (D6)
  and `view unit` need when the operator wants to look before filtering.
- **`merge_types`** filters merge units: a merge whose type is not listed (`untyped` names the
  untyped case) is not made a unit by the merge rule, so its commits go to the commit rule and
  are admitted by `include_types` as plain commits. **Unset: all merges, no type derived, no
  `type` member** (today's output).
- **`include_types` stays commit-only**, so no existing configuration changes meaning.

### Trade-offs

- A filtered-out merge releases its commits rather than dropping them: a `fix/` merge that
  carries one `feat` commit still yields that feat commit as a unit, which matches how the same
  work as plain commits behaves.
- The significance order is a policy; it is fixed here (not configurable) to keep the type
  deterministic and testable, and can become an option later without changing the default.

### Default-visible effect

None from `merge_types` itself: unset it leaves cluster options, units and collation as 0.37 (apart
from D3/D4's defaults; a test in
`bh-8h2k8` proves it).

## D3 -- Branch-sync noise: mechanism and default (#154 R3)

### Evidence

- A branch-sync merge ("Merge branch 'main' into next", "Merge remote-tracking branch ...")
  becomes a unit like any merge (`bls/cluster.py:313-324`).
- NOISE in B5 means "covered by a named epic" and needs a covering bead
  (`bls/identify.py:9-11`, `:637-638`); a branch sync has none. The class that fits "nothing is
  projected, not residual" is `SKIP` (`bls/identify.py:124`, `bls/project.py:590-591`), which
  today only an operator decision produces, and which counts in `view` only when present
  (`src/beadhive/backfill_views.py:116-118`, the byte-identical-when-absent precedent).
- The reader does not know refs, so a reachability rule ("second parent already reachable from
  another long-lived branch") cannot be applied in B3 (out of scope, below).

### Options

- **A. Flag, then SKIP at identify (recommended).** Cluster sets a noise flag by subject shape;
  identify emits a derived SKIP verdict at a new step; the unit stays in the collation and in
  `view unit`.
- **B. Drop at cluster.** Never make the unit. Invisible; the operator cannot see or override it.
- **C. Treat as a type.** A merge type `sync` filtered by `merge_types`. Reuses D2, but needs the
  operator to list every wanted type and hides sync merges only under that option.
- **D. Reachability rule.** Deferred; needs ref knowledge.

### Recommendation: A, on by default in 0.38.0 (operator ruling)

- **Detection (subject shape, no PR ref).** The subject matches `Merge branch '<x>'`,
  `Merge branch '<x>' into <y>`, or `Merge remote-tracking branch '<x>'` (case-sensitive,
  anchored), and the merge has no review ref (no `Merge pull request`, no `#N` that resolves to
  a review artifact). A PR merge is never flagged.
- **Field.** Optional `Unit.noise`: the shape name (`branch-sync`), recorded only when set.
- **Verdict.** A new identify step `branch-sync` (verdict `class` `SKIP`, provenance `derived`,
  review `none`, rationale naming the shape), placed after the operator-decision step 0 so an
  operator `link` or `noise` decision still wins. It is not the operator `skip` decision and does
  not use the decision bead.
- **No swallowed commits.** A noise-flagged merge claims only the merge commit, not its
  second-parent commits; those stay free for the commit rule (the commits a sync brings in are
  another branch's work, and swallowing them is the exact failure of D1/D5).
- **Default (operator ruling 2026-11-14): on.** `backfill.collate.noise` defaults to
  `["branch-sync"]`; `collate.noise = []` is the opt-out and restores 0.37 output. My original
  recommendation was opt-in with a flip in 0.39.0 (the unit removal cannot be undone for beads
  created from previous behaviour, and the subject shapes are heuristics); the operator chose the
  reporter's default now. The mitigations that stay: the unit remains visible in `view unit`,
  an operator `link`/`noise` decision still wins (step 0), a PR merge is never flagged, and the
  swept commits are released rather than hidden. `bh-8h2k8` must state in UPGRADING why (127 of
  169 units in one repo were sync merges), the effect (those units project nothing; their
  commits become commit-rule units when `include_types` admits them) and the opt-out.

### Trade-offs

- Keeping the unit visible costs one row per sync merge in `view unit` but keeps the plan
  auditable and lets D6 select `noise=branch-sync`.
- A new identify step is new vocabulary (D9); the alternative of reusing step `decision` would
  mislabel a rule-derived verdict as an operator's.

### Default-visible effect

**Yes.** Sync-merge units no longer create beads on a default run; verdicts, plan and goldens change
for repositories with such merges. Opt out with `collate.noise = []`. UPGRADING entry required.

## D4 -- Merge titles and default (#154 R1)

### Evidence

- A merge unit's title is the anchor's title, i.e. the merge subject (`bls/cluster.py:248-249`).
- The `review` rule, when the opt-in `github` source is on, already titles a unit from the PR and
  sweeps the merge into it (`bls/cluster.py:17-19`, `:296-302`); the merge rule only sees merges
  with no review, so the first source in the reporter's order is already covered.
- The reader keeps the merge subject (`title=subject`, `bls/git_reader.py:236-238`) but not the
  body: the body lines feed `refs` and trailers only (`:209-218`), so the first body line is not
  in `attrs`.
- Titles feed identify's lexical matching (B5 step 4), so a title change moves verdicts; the unit
  key (the merge sha) does not change, so beads already created stay PRESENT through
  `external_ref` (step 1) and are never rewritten.

### Options

- **A. Default on now (operator ruling).** Matches the reporter's wish; changes created titles and
  identify verdicts for every existing merge-heavy hive (default-visible; golden regeneration).
- **B. Opt-in `collate.merge_titles = derived`.** My original recommendation.
- **C. Fixed to subject forever.** Leaves the 553-of-869 problem to retitling by hand.

### Recommendation: A, with the opt-out `subject`

- **Order**, per merge unit: (1) the PR title (review rule, already in place); (2) the first line
  of the merge body, when non-empty and not itself a "Merge ..." line (GitHub puts the PR title
  there); (3) the subject of the first included commit (the oldest commit the merge brings in);
  (4) the branch name made readable: drop a conventional-type prefix segment, replace `-` and
  `_` with spaces, capitalise the first letter (`fix/render-crash` -> "Render crash"); else the
  merge subject (today's title).
- **Reader support.** The merge body's first line must reach the collation, so the reader
  records a merge-only `attrs` member `body_head` (the first non-empty body line, else `None`)
  under the reader option `merge_body`. The pipeline sets `merge_body` whenever
  `collate.merge_titles = derived`, which is now the default, so **evidence is no longer
  byte-identical by default: merge artifacts gain `body_head`**. The claim holds only under the
  opt-out (`merge_titles = subject`: the option is not set and the attr is absent). `merge_body`
  is a reader option in `parse_key`, so the first 0.38.0 run re-reads evidence once and a
  flip re-reads again. Commits that are not merges are unchanged.
- **Default (operator ruling 2026-11-14): `derived`.** `collate.merge_titles = subject` restores
  0.37 titles. Titles feed identify's lexical step, so verdicts for merge units can shift; the
  unit key (the merge sha) is unchanged, so beads created earlier stay PRESENT through
  `external_ref` (step 1) and are never retitled. `bh-8h2k8` states why, effect and opt-out in
  the UPGRADING entry.

### Trade-offs

- On by default means every existing merge-heavy hive sees changed titles and shifted lexical
  verdicts on upgrade (the cost of the ruling), in exchange for the reporter's repos needing no
  config; `subject` is the escape hatch.
- `Merge branch ...` subjects have empty bodies, so they fall through to the first commit.

### Default-visible effect

**Yes.** Created merge-unit titles, lexical identify verdicts, merge evidence (`body_head`) and
goldens change on a default run. Opt out with `merge_titles = subject`. UPGRADING entry required.

## D5 -- Hidden-commit reporting (#154 R4)

### Evidence

As in D1: a merge unit that ends residual, NOISE or SKIP projects nothing
(`bls/project.py:49-52`, `:590-593`) and its swept commits appear in no plan, view or count.

### Options

- **A. Report only.** Additive keys; the operator sees what is hidden.
- **B. Always fall back to commit units.** Changes created output by default.
- **C. Report always, fall back opt-in (recommended).**

### Recommendation: C

- **What is hidden.** A commit that belongs to a unit (its `artifacts`) of kind `issue` or
  `epic` whose final decision is residual, NOISE or SKIP (an operator skip included). A commit
  attached to a PRESENT or NEW unit is not hidden. For a merge-rule unit this is its
  second-parent commits; no other unit rule sweeps commits for a unit that projects nothing,
  except `review`, which is counted the same way.
- **Where.** The plan summary gains `hidden_commits`: `{"count": N, "units": M}`; the unit view
  (`beadhive.backfill.view/1`) gains a top-level `hidden_commits` object (count, and per unit its
  key, decision and commit shas), and each affected unit row gains `hidden_commits: [shas]`.
  **Emitted only when non-zero**, the existing pattern of `skip` in the counts
  (`src/beadhive/backfill_views.py:116-118`; `bls/project.py:49-52`).
- **Fallback** is D1's opt-in `merge_fallback`; with it on, the same commits are units and the
  hidden count for them is zero.

### Trade-offs

- It changes the text of reports for the runs it describes; that is the point (the operator
  could not see the omission before), so it is stated here as the one accepted default-visible
  change. No created bead, label, title or timeline event changes.

### Default-visible effect

New keys in the plan summary and the unit view on runs with hidden commits (the reporter's
91-of-93 run is the example); byte-identical when none are hidden. UPGRADING lists it.

## D6 -- Selector for `bh backfill decide` (#162)

### Evidence

- `decide` takes one unit key (`src/beadhive/backfill_cli.py:1378-1476`).
- `record_decision` (`src/beadhive/backfill_decisions.py:95-147`) exports and folds the whole
  baseline on every call (`:111-112`) and loads once per unit (`:131`); N units cost N exports
  and N writes.
- `decide` reads no collation; `view` loads one from a run directory
  (`src/beadhive/backfill_views.py:40-47`, `collation_path` accepts the directory or the file).
- A decision is one closed bead per unit with a deterministic id (`decision_id(prefix, group,
  key)`), consulted as B5 step 0 (`bls/identify.py:9-13`); the reason is already a field.

### Options

- **A. A shell loop over `view unit --json`.** No code; N writes; the intent is not recorded.
- **B. A small selector (recommended).**
- **C. A general query language (JSONPath, an expression DSL).** More power than a decision
  tool should accept, and a larger review surface.
- **D. Standing rules applied to later runs.** Deferred (below).

### Recommendation: B

- **Grammar.** `bh backfill decide --select <field><op><value> ... skip|noise|link [<bead>]`;
  `--select` is repeatable and the clauses are ANDed; no OR, no negation in 0.38.0. `op` is `=`
  (exact) or `~` (regular expression, `re.search`, no flags). A value may be quoted with `'` or
  `"` (`title~'^Merge branch'`). A pattern longer than 200 characters is a usage error (the cap
  bounds regex cost over short titles and keys).
- **Fields (fixed).** `rule`, `kind`, `area`, `title`, `key`, `decision` (the unit's verdict
  class), `status` (B6 derived status), and, once D2/D3 land, `type` and `noise`. An unknown
  field, a bad regex, or a field the collation lacks (`type` on a collation made without
  `merge_types`) is a **usage error** naming the missing option, never a silent zero match.
  For the enumerated fields (`rule`, `kind`, `decision`, `status`) an `=` value outside the
  vocabulary is a usage error (catches typos).
- **Source.** Units come from the run's `collation.json` (`--run-dir`, default the latest run);
  the command prints the collation's path and age before doing anything; a missing collation
  names `bh backfill collate`. The single-key form is byte-identical to today, and a key plus
  `--select` is a usage error.
- **Determinism.** Matches are sorted by unit key; equal inputs select equal sets; zero matches
  writes nothing and exits success.
- **Guard rails.** `attach` units are excluded and reported (their identity is an existing bead);
  a unit that already has a live bead (verdict PRESENT or an `external_ref` match in the
  baseline) is excluded and reported, so a selector cannot write a decision that contradicts an
  existing bead. Excluded units are listed with the reason in `--dry-run` and in the result.
- **Safety.** `--dry-run` lists the matched units and the exact decision ids and fields and
  writes nothing (a test asserts it). More matches than `--max` (default 200,
  `backfill.decisions.select_max`) refuses unless `--yes` is given.
- **Batched write.** One baseline export and one load for N decisions: a new
  `record_decisions` builds all events from one fold and one `loader.load`. Each decision stays
  its own bead, so re-runs and `view decisions` work as now; `decided_at` still comes from
  `decision_clock`.
- **Reason.** Each decision's reason is `select: <the selector clauses as typed>` followed by
  the operator's `--reason`, if any.
- **Standing rules deferred** to a later ADR amendment: they are a second store of intent next
  to the decision beads, need precedence and expiry rules, and are not needed to remove the
  reporter's workaround.

### Trade-offs

- Repeatable `--select` avoids writing an expression parser for AND (and keeps shell quoting
  simple); the bead text's `--select EXPR` is satisfied by one clause.
- The cap trades convenience for a deliberate step; 200 is above the 127-unit family seen in
  one repo and below the 680 across four.

### Default-visible effect

None: a new flag; the existing form is unchanged.

## D7 -- Git boundary and tags (#159)

### Evidence

- `read_git` (`bls/git_reader.py:444-494`): `base` is the cursor, else `since` when it is a hex
  sha (`_SINCE_SHA`, `:74`), read as `base..rev`; otherwise `--since=<since>`. The cursor wins
  over `since` (`:459`).
- Tags: one `for-each-ref` over every tag, independent of range (`:470-481`), chained into
  `previous`/`commit_range` over the full list (`:485-492`).
- Options are free-form strings (`bls/contracts.py:2267-2274` shows the date workaround; reader
  options are `dict[str, str]`).
- `parse_key` hashes `HEAD`, the dirty status and the options, not any resolved ref
  (`src/beadhive/backfill_history.py:181-190`): if `exclude_revs=origin/main` and `origin/main`
  moves while `HEAD` does not, the key is unchanged and stale evidence is reused.
- An all-hex date (`20260901`) is handed over with a leading space so the reader's sha test
  fails (`bls/contracts.py:2267-2274`, `bls/commits.py:167-168`, `:218-221`).

### Options

- **A. `exclude_revs` (recommended).** A csv of refs, resolved every run, passed as `^<sha>`.
- **B. Per-hive `since` overlay refreshed by hand/CI.** The reporter's workaround; drifts.
- **C. `merge-base` auto-detection against a named upstream.** Implicit; wrong for fast-forwarded
  or rebased forks, and needs a remote name.

### Recommendation: A

- **`exclude_revs`.** A csv of refs. Each is resolved on every run with
  `git rev-parse --verify <ref>^{commit}` and passed to `git log` as `^<sha>`. A ref that does
  not resolve is a `GitReadError` naming it (never a silent skip: that would read the whole
  upstream). It composes with the cursor and `since` (`base..rev ^excl`): the cursor never
  rewinds when upstream moves, and moving upstream only ever removes commits from the range.
- **Not persisted as `since`.** The resolved shas are recorded in `run.json` and hashed into
  `parse_key` (so an upstream-only change re-reads; an unchanged upstream reuses evidence). They
  are never written into the config.
- **Tags.** `tag_scope = all | range`, default `all` (today). Under `range` a tag is read only
  when the commit it points at (peeled for annotated tags) is among the commits read. The filter
  runs **before** the `previous` chain, so a downstream tag's `commit_range` starts at the first
  in-scope tag; the first in-scope tag has `commit_range = None`, as the first tag does today.
  `tag_include` / `tag_exclude` are csv globs (`fnmatch`, case-sensitive) on the tag name; a tag
  must match an include (when set) and no exclude; **exclude wins**. Both apply under either
  scope. The tag filters and scope are in `parse_key` through the options.
- **`since_date`.** An explicit date option passed as `--since=<date>`. Setting it together with
  `since` is a usage error naming both. **`since` is unchanged and not deprecated** (a
  deprecation warning would be default-visible and the sha form is the common one).
- **The date workaround.** The leading-space trick is removed from the contract, not from the
  user-facing behaviour: the `commits` strategy (the old pass) translates an all-hex `--since`
  that is not a commit into `since_date` internally, so a configuration with `since=20260901`
  keeps working. A direct `read_git` call with `since=20260901` is a sha candidate by design; use
  `since_date`. The git-commits strategy goldens must stay green (`bh-19q4u` acceptance).
- Per hive through `backfill.hives.<prefix>.sources.git.options` (the 0.37.0 overlay).

### Risk: a fork that rebases onto upstream changes unit keys

Unit keys are commit shas (`git:commit/<sha>`). A downstream fork that rebases its own commits
onto a moved upstream gets new shas, hence new keys: identify step 1 (`external_ref`) no longer
matches the beads created earlier, and the units come back as NEW or (through lexical matching)
residual, i.e. duplicates for review. `exclude_revs` does not cause this and cannot fix it.
Mitigation in 0.38.0 is documentation only (BACKFILL.md, the history-readers section): rebase
rewrites identity; backfill before rebasing, or record `skip`/`link` decisions (D6) for the
rewritten units. A patch-id key is a possible later design and is out of scope.

### Trade-offs

- `parse_key` must resolve each excluded ref (one `rev-parse` per ref) before the read, to key
  the evidence on what the boundary currently is; that is cheap next to the read.
- Range-scoped tags lose the pre-range `commit_range` start; that is intended for forks (the
  upstream tag is the noise) and is opt-in.

### Default-visible effect

None: all four options are unset by default; `tag_scope` defaults to `all`. Default option sets
give byte-identical goldens.

## D8 -- Doc attachment (#157)

### Evidence

- Tiers `explicit`, `same-change`, `shared-path`, `lexical` (`bls/attach.py:42-46`;
  `bls/collate.py:112-125`); `AttachOptions` carries `tiers`, `lexical_percent`,
  `lexical_min_tokens` and `standalone` (`bls/attach.py:83-94`).
- `same_change` needs the doc's add commit to be claimed by a unit holding code
  (`bls/attach.py:222-244`): a `docs:` add commit under `include_types: [feat]` is in no unit, so
  the tier misses. `shared_path` has no cap and chooses by evidence count then key
  (`:246-261`); a moved doc counts as added by the moving commit (`doc_add` is the oldest change
  touching the path, `bls/collate.py:224`), so a reorganising merge collects docs.
- The reader reads names with `--name-status -M` (`bls/git_reader.py:457`) but `_paths` drops the
  status letter and keeps both sides of a rename (`:185-198`): rename status is **not** kept
  today, so a rename guard needs reader support. Merges are not diffed (`paths` is empty for a
  merge), so a merge cannot itself be "mostly renames"; the rename is on a commit inside it.
- An unattached doc becomes a standalone doc-unit (`:310-318`). There is no `related` link:
  `Unit` has no such field and the projector emits only parent-child and blocks edges
  (`bls/project.py:19-30`).
- The reader records no line counts (name-status only), so "edit size" can only be the number of
  paths a commit touches.

### Options

- **A. Cap only.** Simple; does not fix the wrong-unit case.
- **B. Cap + rename guard + own-add tier + ranking, all opt-in (recommended).**
- **C. Turn off `shared-path`.** Loses good matches (the reporter's own finding).
- **D. Add `docs` to `include_types`.** One bead per doc edit.

### Recommendation: B

- **`attach.max_per_unit`** (default unset = no cap). When set, a unit takes at most that many
  attachments from the guessing tiers (`shared-path`, `lexical`); overflow is decided by
  evidence count then key, deterministically; an overflowing doc goes to the next tier, then
  stands alone. `explicit` is exempt (the doc names the unit). **`same-change` is also exempt**
  (the doc was added in the unit's own change: direct evidence, and a feature that genuinely
  adds five docs must not shed them). This narrows the bead text ("explicit exempt"); **decided
  by the operator, 2026-11-14**.
- **`attach.rename_guard`** (default off). Requires the reader to keep rename status: reader
  option `keep_renames` (default off, set by the pipeline when `rename_guard` is on, part of
  `parse_key`) adds a commit `attrs` member `renamed` (the destination paths of R entries) only
  when the option is on, so default evidence is byte-identical. With the guard on, a doc whose
  add commit holds it in `renamed` is not attached by `same-change`, `shared-path` or `own-add`
  through that commit (it is a move, not an authoring); it falls to the next tier or stands
  alone. A per-doc rename test replaces the bead's "mostly-rename merge ratio": merges carry no
  paths, and the per-doc test needs no threshold.
- **`own-add` tier** (reason `own-add`), outside the default `tiers` list. When the operator
  adds it (the list order is the precedence; the intended order is after `same-change` and
  before `shared-path`), a doc whose first-add commit exists but is not claimed by any unit is
  given a doc-unit of its own with evidence = that add commit, instead of attaching by path
  proximity. The attachment passes the existing invariant (`unit == doc`, evidence non-empty,
  reason not `standalone`; `bls/collate.py:584`).
- **Candidate ranking**, `attach.rank = evidence | proximity` (default `evidence`, today's
  choice). `proximity` orders candidates by: absolute time distance from the doc's add commit
  to the unit's anchor, then subject-token overlap (descending), then the number of paths
  touched (the available edit size, descending), then key. The runner-up (key and the rank
  numbers) is recorded as an optional `runner_up` member of the attachment and shown in
  `view unit`. Emitted only when `rank = proximity`.
- **`related` links are deferred out of 0.38.0.** A link from the own-add doc bead to the
  implementing unit needs a new `Unit` field (`related`), a projector edge kind and a BeadLog
  edge, i.e. a second schema change on the bead-event side that the pipeline ADR treats as
  stable (the BeadLog format ADR is unchanged by it). The own-add attachment still records the
  candidate implementing unit's anchor artifact in its evidence when one is found, so a later
  bead can emit the link without re-collating. The reporter's "link to the feat unit" is the
  follow-up.
- The per-folder doc epic is out of scope.

### Trade-offs

- Ranking by time distance uses commit dates the pipeline already holds; without line counts it
  cannot reproduce the "+16 lines vs +3 lines" detail of the report, but time and title overlap
  cover that example (matching feat minutes later versus an unrelated one ten hours later).
- Exempting `same-change` from the cap weakens the cap in the one scenario (a reorganising
  merge) where a move is treated as an add; the rename guard is the answer there, and the two
  are meant to be used together.

### Default-visible effect

None: `max_per_unit` unset, `rename_guard` off, `own-add` not in the default tiers, `rank`
defaults to `evidence`; the default option set keeps the goldens byte-identical.

## D9 -- Collation schema

### Evidence

- `bdlg.collation/1` is strict at every level: `Unit.from_json` refuses any member set but
  `_UNIT_MEMBERS` (`bls/collate.py:539-555`), `Attachment.from_json` likewise (`:599-607`),
  `ClusterOptions.from_json` refuses any set but `OPTION_MEMBERS` (`bls/cluster.py:155-160`),
  the same for the attach, identify and status options (`bls/collate.py:762-791`), the verdict
  `step`/`class` and attach `reason` vocabularies are closed (`bls/identify.py:114-128`;
  `bls/collate.py:119-125`), and the packaged schema sets `additionalProperties: false`
  throughout (`bls/schemas/bdlg-collation-v1.schema.json`).
- The 0.37.0 precedent: adding step 0 (`decision`) and class `SKIP` meant a reader older than
  0.37.0 rejects a collation holding them, and a run with no decisions writes exactly what it
  did before (`docs/BACKFILL.md:623-629`).
- `Collation.to_json` always writes every option member (`bls/collate.py:650-665`), so a new
  option member emitted unconditionally would change every collation's bytes.

### New vocabulary this molecule needs

`Unit.type` and `Unit.noise` (D2, D3); the options `cluster.merge_types`, `cluster.merge_titles`,
`cluster.noise`, `identify.merge_identity`, `identify.merge_fallback`, `attach.max_per_unit`,
`attach.rename_guard`, `attach.rank` (D1-D4, D8); the identify step `branch-sync` (D3); the attach
reason `own-add` and the optional `runner_up` member (D8); reader option and attr members
(`merge_body`/`body_head`, `keep_renames`/`renamed`) in the evidence corpus, whose attrs are also
strict (D4, D8).

### Options

- **A. One additive change to `bdlg.collation/1` (recommended).**
- **B. Bump to `bdlg.collation/2`.** A clean break, but every 0.37 collation on disk stops
  reading, both loaders must coexist, and default runs would change bytes.
- **C. One change per feature bead.** Several partial schemas in one release; the strict loaders
  are touched five times.

### Recommendation: A

- **Shared, introduced once.** `bh-8h2k8` (the first bead that needs it) introduces the
  mechanism: `to_json` emits a new member **only when it differs from its default** (`type`,
  `noise`, `runner_up`; and each new option member only when set), and `from_json` accepts the
  old exact member set and the old set plus any subset of the new optional members. The
  packaged JSON Schema lists the new members as optional properties and the new enum values.
  Later beads (`bh-yg3ae`, `bh-dviao`, `bh-espkv` where relevant) extend the same lists; none
  changes the schema id.
- **Byte-identical when opted out.** A run that opts out of D3 and D4 (`noise = []`,
  `merge_titles = subject`) and sets no other new option writes exactly the 0.37.1 collation:
  same members, same option objects. With the D3/D4 defaults a run uses new vocabulary where
  a sync merge exists (`noise`, step `branch-sync`) and so is not readable by a
  pre-0.38 reader. A test per bead proves it, and a 0.37
  collation file still reads.
- **Older readers.** A reader older than 0.38.0 rejects a collation that uses any new
  vocabulary (now including a default 0.38 run over a repository with branch-sync merges)
  with `CollationFormatError`, exactly as 0.37.0's did for `decision`/`SKIP`; it
  reads a collation that uses none. Collations are host-local run artifacts read by the same
  install that wrote them; the note goes into BACKFILL.md and UPGRADING (as a rule of thumb:
  do not read a 0.38 run directory with an older `bh`).
- The evidence (`bdlg.evidence/1`) additions are reader-option gated (D4, D8). `keep_renames`
  is off by default; `merge_body` is on by default through D4's default, so default merge
  evidence gains `body_head` (see D4); opting out restores it.
- The BeadLog events and `plan.bdlg` are not changed by this molecule (no `related` edge; D8).

### Trade-offs

- Conditional serialization makes `to_json` slightly less uniform; the alternative (always emit)
  would break every golden for a feature most hives will not enable.
- A strict loader remains: unknown members from a future version are still refused, by design.

### Default-visible effect

None for runs that set no new option.

## Order and lanes

One lane, one developer, serial (the operator's molecule-dispatch rule), because the beads share
`cluster.py` and `collate.py` and the one schema change:

1. `bh-5e4r1` -- this ADR (operator approves; its decisions are copied into each child).
2. `bh-19q4u` -- #159 git boundary (D7). Touches only the reader, `commits.py` and `parse_key`;
   independent of the collation schema, so it goes first.
3. `bh-8h2k8` -- #154 part 1: merge titles, types, `merge_types`, branch-sync noise (D2-D4) and
   the shared collation-schema change (D9).
4. `bh-yg3ae` -- #154 part 2: hidden-commit reporting, opt-in fallback, opt-in commit-based
   identity (D1, D5). Blocked on `bh-8h2k8`.
5. `bh-dviao` -- #157 attachment (D8). After #154, whose titles feed the lexical tier and whose
   merge claims feed same-change.
6. `bh-espkv` -- #162 selector (D6). Last, so the selector can use `type` and `noise`.

Release 0.38.0: `feat` commits for features, `fix` for D5's reporting; one
"## 0.37.1 → 0.38.0" UPGRADING section, written by the feature beads (not this bead).

## Deferred, and out of scope

- The **branch-sync reachability rule** (a merge whose second parent is reachable from another
  long-lived branch): needs ref knowledge the reader lacks.
- **Standing rules** for `decide` (D6).
- The **per-folder doc epic** from #157, and `related` links (D8).
- **#161 / #160** (the later references molecule) and `bh-t6j8i` item 1 (docs-pass titles;
  scheduled after this molecule so goldens regenerate once). `bh-t6j8i` item 2 (`since_date`) is
  done by `bh-19q4u`.
- **B (promote the PR)** for merge identity and a **patch-id unit key** for forks that rebase.

## Decisions ruled by the operator (2026-11-14)

The open questions of the first revision were ruled on 2026-11-14:

1. **D3 and D4 defaults: on by default in 0.38.0**, not deferred. `collate.noise` defaults to
   `["branch-sync"]` and `collate.merge_titles` to `derived`; the opt-outs are `collate.noise = []`
   and `collate.merge_titles = subject`. This is a default-visible change: identify verdicts for
   merge units shift and goldens regenerate. `bh-8h2k8` adds the "## 0.37.1 → 0.38.0" UPGRADING
   entry (why, effect, opt-out). D4's `body_head` evidence attr is recorded by default.
2. **D8 cap exemption:** `explicit` and `same-change` attachments are both exempt from
   `max_per_unit`. Decided as recommended.
3. **D8 `related` links:** deferred out of 0.38.0. Decided as recommended.
4. **D6 grammar:** repeatable `--select` clauses, ANDed. Decided as recommended.
5. **D1 mixed outcome:** a mixed merge keeps the existing flow and is reported. Decided as
   recommended.

## Consequences

- Every feature bead cites the decision it implements, and the operator's approved text is copied
  into it before it starts.
- Default changes in 0.38.0 are D3, D4 (operator ruling) and D5's additive report keys, each with an
  UPGRADING entry and an opt-out; everything else is opt-in, and each bead carries a
  default-preserving golden test.
- The pipeline ADR's B3 (merge rule), B4 (attach tiers) and B5 (identify steps) and the re-run
  control ADR's Q3 and Q4 are amended as above; both carry a pointer to this document.
