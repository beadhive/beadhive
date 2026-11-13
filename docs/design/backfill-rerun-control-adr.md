# ADR: Backfill re-run control — per-hive config, doc status mode, ledger decisions, reader excludes

- **Status:** Proposed (awaiting operator approval of this ADR's review gate; Q1 and Q2 carry
  explicit operator recommendations).
- **Date:** 2026-11-13
- **Decision owner:** Beadhive maintainers (operator approves; not delegated)
- **Related work:** molecule `bh-1vkff` (0.37.0); this design bead `bh-meuhp`; implementation
  children `bh-pieo5` (reader excludes), `bh-g71fi` (per-hive config), `bh-2sicb` (per-unit
  decisions), `bh-f4t6r` (#153 status mode) and `bh-uysl1` (docs-only runs), the last two one
  branch (`batch:status`). Reports: GitHub #153, #155, #156; related `bh-q7f9f`
  (`config get` layer display).
- **Amends:** [`backfill-history-pipeline-adr.md`](backfill-history-pipeline-adr.md): the rules
  "NEW creates closed beads" (Phase C), "a design record's `declared_status` never sets a status"
  (B6) and the per-hive `areas` intent (B3) change as set out in Q1-Q3; that ADR carries a
  matching pointer.
- **Grounded in:** main `0624f478` (0.36.x). Line references below are to that tree.

## Context

0.36.0 shipped `bh backfill history`. Field feedback from an operator re-running it on a real
hive shows the re-run loop lacks control in four places:

1. **#156** -- every backfill read takes the single host-level `backfill` block, so
   `collate.areas` ("per-hive area overrides") is not per hive.
2. **#153 / `bh-uysl1`** -- a created doc bead is always closed (by its add commit), whatever the
   doc says; and a docs-only run (no git reader) creates **open** beads, which look like live
   work.
3. **#155 (part 2)** -- an operator cannot record "skip this unit", "this is noise" or "this is
   that bead"; deleting an unwanted backfilled bead just gets the unit proposed again.
4. **#155 (part 1)** -- the docs and planning readers take positive globs only, so templates and
   archives cannot be excluded.

Operator decisions already made (2026-11-13, recorded on `bh-1vkff`; not re-opened here):
scope is #155 + #153 + #156 + `bh-uysl1`; per-unit decisions live in the **bead store** (the
hive's ledger), not a repo file or host config; #153 is accepted as opt-in, and a default change
is to be explored and brought to the operator.

**Constraints that bind every option:** every new behaviour is configurable and defaults to
today's output unless this ADR, operator-approved, changes a default; prefixes are never derived
or suggested (`bh-ear6v`); bead history is never compacted or flattened -- tombstones and
decision records are additive data; golden and equivalence tests stay green or change only with
a documented output change.

## Summary of recommendations

| # | Question | Recommendation |
|---|---|---|
| Q1 | Where per-hive backfill settings live | **(a)** a host-config overlay `backfill.hives.<prefix>.*`, merged over the host block; `judge` stays host-only. |
| Q2 | Created doc bead status | Add opt-in `backfill.collate.status.doc_default = closed\|open\|from-doc` (default **`closed`**, today's output). In `create=propose`, never mint an open bead without evidence: a doc unit with no closing moment is **skipped and reported** unless the mode says open. Do **not** change the default in 0.37.0. |
| Q3 | Ledger shape of per-unit decisions | A **dedicated closed decision bead per unit key** (type `decision`, `source:decision`), written by `bh backfill decide skip\|noise\|link`; identify consults it as a new step 0. A deleted bead becomes a tombstone only through an explicit `--deleted <id>` path or the opt-in `backfill.decisions.on_delete`; default is today's behaviour. |
| Q4 | Reader exclude syntax | A new `exclude` option (comma list of globs, same glob language as `globs`) on both readers; **no negation in v1** (a leading `!` is rejected so negation can be added compatibly). Exclude wins over explicit `files`, with a warning. |

## Q1 -- Where per-hive backfill settings live (#156)

### Evidence

- Every read site takes the host block whole: `backfill_cli.py:1012` (plan rebuild) and
  `:1045` (history run) validate `cfg.get("backfill")`; `backfill_continuous.py:85` reads
  `backfill.continuous`; the other readers are `backfill_cli.py:378,492,640,653,1320,1611`,
  `backfill_coverage.py:41`, `work_merge.py:1565` and `cli.py:1884`.
- `backfill` is a **host-owned** prefix (`modules/config/application/partition.py:56`, with the
  reason: the judge runs a host command that may spend API credit, so "a fleet push can never
  switch it on"). `managed_repos`, the registry that holds each hive's entry, is **fleet-owned**
  (`partition.py:87`).
- `bh config set/unset/get` take `--scope fleet|host` only (`cli.py:3984-4046`); there is no
  per-hive scope.
- `collate.areas` is described as "Per-hive area overrides" (`contracts.py:2400-2403`) and the
  pipeline ADR intended per-hive overrides (B3 item 6, "per-hive override map"), but it is a
  host key.
- Precedent for per-hive over global: `config.layered` (`config.py:109`) and
  `config_work_settings.py:43-45,486-509` (`work.*` per-hive entry over the global section) --
  but `work` is a fleet prefix, the opposite trust position to `backfill`.

### Options

**(a) Hive-keyed overlay in host config**: `backfill.hives.<hive>.<same shape as backfill>`.
Host-owned by the existing `backfill` prefix, so no new partition rule is needed and a fleet
push cannot reach it.

**(b) The hive's registry entry** (`managed_repos[<hive>].backfill`). Matches the `work`
precedent and keeps a hive's settings next to its identity. But `managed_repos` is fleet-owned:
a fleet push could then set `collate.create = propose` (writes beads), widen `sources`, or --
unless a per-key allowlist is bolted on -- reach the judge. Partitioning one entry by key is a
new mechanism, and the fleet/host split exists precisely to avoid that.

**(c) A repo-local file** (e.g. `.bh/backfill.toml`). Travels with the clone, reviewable in a
PR. But then repository content controls a host behaviour that writes to the bead store and may
spend money: anyone who can land a commit can change what backfill creates. It also fails for
hives whose repo you do not own and for runs on a checkout at an old commit.

### Recommendation: (a)

- **Key** is the hive's registered `prefix` (explicit, dot-free, already required by create mode
  -- `backfill_create.py:57-66`; never derived). A hive with no registered prefix cannot have an
  overlay; the error says to set the registry prefix first. (A dotted triplet key would collide
  with the dotted `config set` path syntax; this is the one sub-choice to confirm.)
- **Merge rule:** for the hive, `effective = host block <- overlay`, resolved by one function
  used at **every** read site above. Mappings merge key by key (so `collate.areas` and a reader's
  `options` keep the host's entries unless the hive overrides that key); lists and scalars
  replace. A hive with no overlay yields the host block unchanged, byte for byte -- today's
  behaviour.
- **Trust:** the overlay is host config, so a fleet push cannot write it
  (`partition.py` already refuses it). Inside the overlay, `judge` (command, key wiring, call
  caps) is **rejected at validation**: one credit-spending host command stays one host-wide
  setting. Allowing a per-hive judge later is additive. A test covers both: a fleet-scope
  write to `backfill.hives.*` is refused, and an overlay `judge` key fails validation.
- **`config set`/`get`:** `bh config set backfill.hives.<prefix>.collate.areas '{...}' --json`
  works with the existing dotted-path writer once `BackfillConfig` models `hives:
  dict[str, BackfillHiveOverlay]` (the overlay model is the same sections minus `judge`).
  `bh config get backfill.collate.areas --hive <prefix>` (new option, read-only) returns the
  effective value and names the layer on stderr (`host` or `backfill.hives.<prefix>`). This is
  the same mechanism `bh-q7f9f` asks for `work.*`; implement it once, generally, and let
  `bh-g71fi` land the backfill part without waiting for `bh-q7f9f`.
- **Continuous backfill** (`backfill_continuous.py:85`, `work_merge.py:1565`, `cli.py:1884`)
  already knows the hive it triggers for; it resolves the effective block for that hive.

### Trade-offs

Per-hive settings live in the host's config file, not with the hive: they do not travel with a
clone and each host configures them (same as every other `backfill` setting today). Accepted:
that is the price of keeping credit-spending and bead-writing behaviour out of fleet and repo
reach. Hives with unstable prefix registration lose overlays until registered.

## Q2 -- Status of created doc beads (#153 and `bh-uysl1`)

### Evidence (how a doc unit's status is decided today)

- B6 `own()` decides status from evidence only: a plan item by its done marker, a merged review
  or committed change, a release, a closed tracker item, **an add commit (`basis "doc"`,
  `lifecycle.py:530-531`)**, else open from a review or tracker item (`:532-535`). The doc's
  own `declared_status` is read for conflicts only (module doc, `lifecycle.py:9-13`;
  `:613-650`).
- A design record with an add commit contributes a closing moment `(6, add)`
  (`lifecycle.py:503-507`); with no add commit only its declared date creates it (`:508-509`).
  The add commit comes from git change artifacts (`_add_moment`, `lifecycle.py:452-455`).
- With **no evidence at all** (a docs-only run: no git reader, so no add commit) `ev.status` is
  `None` and the unit falls through to `Lifecycle(key, "open", "none", created)`
  (`lifecycle.py:564-566`). In `create = propose` that is an **open created bead** -- the
  `bh-uysl1` bug.
- Declared statuses normalise onto `DECLARED_STATUSES`: `proposed, draft, accepted, delivered,
  superseded, rejected, deprecated` (`evidence.py:59-67`; synonyms `doc_metadata.py:43-66`).
  "Design only" is a separate raw-phrase check against `StatusOptions.design_only`
  (`lifecycle.py:626`).
- The docs reader does not parse checklists; unchecked roadmap items come from the planning
  reader as plan items (`done_marker` false -> open, `lifecycle.py:521-523`).
- Conflicts never change status (`lifecycle.py` module doc), they fire in narrow cases
  (draft+code landed, design-only+feat, accepted after the last release with no code).
- The B6 options are recorded in the collation (`backfill_cli.py:1020-1025`,
  `StatusOptions.to_json`) and replayed, so any new B6 option must round-trip there, and the
  config model is `BackfillStatusConfig` (`contracts.py:2346`).
- Created beads are meant to be closed historical records (pipeline ADR Phase C); "open created
  beads look like live work" (`bh-uysl1`) -- open beads show in `bd ready` and to dispatchers.
- Created beads are never rewritten: PRESENT only appends/stamps, never `phase`
  (`project.py` module doc), so a mode change affects **newly created beads only**.

### Options

**Setting.**

- **S0** keep: always closed by evidence. (Today; fails #153.)
- **S1** opt-in key `backfill.collate.status.doc_default = closed | open | from-doc` (the
  reporter's sketch), default `closed`.
- **S2** the same key, default `from-doc`.

**`from-doc` mapping** (which declared statuses become open):

- **M1** `draft`, `proposed` -> open; everything else, and undeclared, closed.
- **M2** M1 plus "design only" phrase hits (the existing `design_only` list) -> open.
- **M3** M2 plus `accepted` when no code is attached (the reporter's "accepted-no-code").

**Docs-only runs** (`bh-uysl1`'s options 1-3): (1) skip and report a unit with no closing moment
in `create = propose`; (2) warn at plan time and require `--yes`; (3) make git mandatory with
docs under `propose`.

### Recommendation

1. **S1, default `closed`; do not change the default in 0.37.0.** The key lives in
   `StatusOptions` / `BackfillStatusConfig` beside the other B6 options (so it is recorded in the
   collation and replays). Values:
   - `closed` -- today.
   - `open` -- a unit whose status rests only on a doc (`basis` `doc` or `none`) is open.
   - `from-doc` -- such a unit is open when a doc attached to it declares a status in
     `status.doc_open` (default **`["draft", "proposed"]`**, i.e. M1) or a raw status matching
     `design_only` (M2, always on in this mode); closed otherwise.
   Add `status.doc_open` as a list so an operator can add `accepted` (M3) without a code
   change. `accepted` is **not** in the default list: an accepted ADR is a finished decision
   record, and the existing `doc-accepted-no-code` conflict already flags the suspicious case.
2. **Evidence outranks the declaration.** `from-doc` applies only when the unit has no
   evidential closure (no merged/committed change, release, closed tracker item, plan done
   marker). A draft doc whose change also landed code stays closed and the existing
   `doc-draft-code-landed` conflict still fires. `declared_status` therefore gains a narrow
   role -- it can choose open for evidence-free doc units under an opt-in mode -- and still
   never overrides evidence. This amends the pipeline ADR's "never sets a status" sentence.
3. **Unchecked checklist items in docs: out of scope.** The docs reader has no checklist model;
   an operator who wants roadmap items to be open names that file in the planning reader
   (`files`), as the maintainers already advised on #153. Documented in `docs/BACKFILL.md`.
4. **Docs-only runs: option 1, mode-aware.** In `create = propose`, a unit with no closing
   moment is **not created**; it appears in `bh backfill inspect` and the plan summary as
   `skipped: no closing moment (add commit unavailable; include the git reader)`. Exceptions
   are explicit, never silent: mode `open`, and mode `from-doc` for a doc whose declared status
   maps open, both of which *intend* an open bead. Option 2 (`--yes`) turns a plan-time fact
   into a prompt and breaks unattended runs; option 3 forbids legitimate docs-only link runs
   (`create = off`). In `create = off` nothing is created, so nothing changes.
   This is a deliberate **output change** (a bug fix): today's docs-only `propose` run mints
   open beads. A test covers it; the default `git,docs,planning` run is byte-identical.
5. **Report-only is the safe floor.** Conflicts are reported exactly as today in every mode.

### The default-change alternative (S2) -- for the operator

Making `from-doc` the default would make a draft or proposed ADR come in open, which is the more
truthful reading of "draft". Against it: it changes the output of every future run on every hive
(goldens, `bd ready` contents, dispatcher-visible open work from a bulk backfill of a large doc
corpus), it contradicts the documented "backfill creates closed historical records" principle
and the `bh-uysl1` rationale, and the mapping (is an accepted-but-unbuilt ADR open?) has no
field evidence yet. Because the key exists, flipping the default later is a one-line change plus
a documented `UPGRADING` entry, so deferring costs nothing. **Recommend S1 now; revisit the
default after a release of use.** The ADR is the place for the operator to choose S2 instead; if
chosen, `bh-f4t6r` updates the goldens and `docs/UPGRADING.md` documents the change.

### Trade-offs

Opt-in means the reporter's hive must set one key (per hive, via Q1) to get the behaviour.
Skip-and-report in docs-only `propose` changes today's (buggy) output and makes an unattended
docs-only run produce fewer beads than before; the report line explains why.

## Q3 -- Ledger shape of per-unit operator decisions (#155 part 2)

### Evidence

- A re-run recognises a created bead by `external_ref` (B5 step 1, `identify.py:606-620`; the
  projector sets `external_ref = unit key`, `project.py` module doc "NEW"; ids are minted
  deterministically, `_sources.mint_bead_id`, `_sources.py:184`). A bead carries **one**
  `external_ref` (`bd update --external-ref`, a single string).
- A **deleted bead drops out of the baseline** (`identify.py:497-498`: `not state.exists or
  state.deleted or state.erased` is skipped), and `parse_export` drops `tombstone`-status rows
  (`bd.py:553`). So its unit is NEW again.
- **Measured on `bd 1.3.0`:** after `bd delete <id> --force`, `bd export --all` and
  `bd list --all` no longer contain the issue and `bd show` says "no trace left in the live
  database"; only `bd history <id>` (needs the id) still returns it. A deleted bead therefore
  **cannot be turned into a skip tombstone from the live store alone** -- the evidence that it
  ever existed must be recorded before deletion, or recovered by a targeted `bd history`. (A
  closed bead stays in the baseline and is never re-proposed -- the maintainers' stop-gap on
  #155.)
- NOISE is offered only against a baseline **epic** (`identify.py:30-32,664-671`;
  `reconcile.py:13,421,472` requires a covering-epic event) and in practice only the judge picks
  it. Earlier judge answers are reused (`backfill_history.py:18-22,295-298`) but are keyed by
  query hash, not unit, and an operator cannot author one.
- Verdict records have a closed vocabulary: `STEPS` (`identify.py:101-108`), `CLASSES`
  (`:113`), validated strictly on load (`check_verdict`, `:387-416`); the projector's decisions
  are `new/present/noise/residual` (`project.py:152-154`).
- Run directories are host-local state (`backfill_history.py:85-86`), not the ledger.
- BeadLog `delete` and `tombstone` events exist (`EventKind`, `_model.py`), and a
  bd loader keeps per-facet envelopes in `metadata.bdlg` (`bd.py` module doc) -- but bd itself
  cannot keep `delete`/`tombstone` (`facet_key` returns `None`), so those are not the way to
  store an *operator decision* in a bd ledger.

### Options for the record

- **R1 -- metadata/labels on the target bead** (`metadata.backfill.decisions`). No place for
  `skip` (there is no target), lost if the target is deleted, and a link decision would mutate a
  bead that may be hand-made.
- **R2 -- a dedicated decision bead per unit key (recommended).** An ordinary, closed bead of
  bd's built-in `decision` type.
- **R3 -- `bd remember` memories.** Keyed values in the store, but exported as non-issue rows
  that `parse_export` skips (`bd.py:541-546`), so the BeadLog baseline cannot see them.
- **R4 -- host config or a repo file.** Excluded by the operator's decision.

### Recommendation: R2, consulted as B5 step 0

**Record.** One bead per decided unit key, created by the verb below:

- id: `mint_bead_id(<hive prefix>, <group>, "decision:" + <unit key>)` -- deterministic, so
  repeating a decision is idempotent; the prefix is the explicit registry prefix, never derived.
- type `decision`, status **closed**, title `Backfill decision: <kind> <unit key>`.
- labels: the creation labels of backfill (`origin:backfill` plus the identity triplet, as
  `backfill_create.py:71-90`) and `source:decision` (the open provenance facet,
  `backfill_bd.py:974-977`). Being `origin:backfill` it is work-neutral (`plan.py:448`).
- `external_ref = "bfdec:" + <unit key>` -- a different namespace from any unit key, so B5
  step 1 can never mistake it for the unit's own bead.
- metadata: `backfill.decision` = `skip|noise|link`; `backfill.unit` = the unit key;
  `backfill.target` = the bead id for `link` (and the covering epic for `noise`);
  `backfill.reason`, `backfill.decided_by`, `backfill.decided_at`.
- Re-deciding the same unit **updates the same bead's metadata**; bd's history keeps the earlier
  decision. Removing a decision is `bd close`/re-decide, never delete-and-forget.

**Why this shape.** It is additive data in the ledger the operator chose; it is a normal issue
row, so it survives `bd export`/import and every BeadLog loader with no new store feature; it
needs no `compact` or `flatten` and no history rewrite; and `link` does not need the target to
gain a second `external_ref` (a bead carries one) -- the decision bead *is* the link record,
and the target is untouched. The unit key is the stable source key (`doc:<path>`,
`gh:pr/<n>`, area keys), already the idempotency key.

**Verb.** `bh backfill decide <unit-key> skip|noise|link [<bead>] [--reason R] [--hive H]
[--dry-run]` and `bh backfill decide --deleted <bead-id> skip` (below).
`--dry-run` prints the exact decision bead (id, labels, metadata) and writes nothing; the real
run goes through the same guarded bd write path as other backfill writes. `noise` requires a
covering epic (`<bead>` must be an epic, matching `reconcile.py:13`); `skip` takes no target;
`link` requires an existing, live bead. `bh backfill inspect`/`view` shows recorded decisions
(a `decisions` view and a per-unit "decided by operator" mark).

**How identify consults them.** `_Baseline` (`identify.py:491-516`) splits decision beads out
by `source:decision` (so they never enter lexical candidates or `refs`) into a `decisions`
index keyed by unit key. In `identify_units`, **before** step 1 (`identify.py:606`), a unit
with a decision gets a verdict at a new step `decision` (added to `STEPS`; provenance
`derived`, review `none`):

- `link` -> class PRESENT against the target (projects only the usual appends/stamps; the
  `external_ref` stamp rule is unchanged: stamped only when the bead has none).
- `noise` -> class NOISE with the covering epic (reconcile-compatible).
- `skip` -> a verdict that projects **nothing** and is **not** residual: add the decision
  `skip` beside `new/present/noise/residual` (`project.py:152-154`); it appears in views as
  `skipped (operator)`, and never as a unit awaiting review.
A decision whose `link`/`noise` target no longer exists is **reported and not applied** (the
unit falls through to normal identification), never silently honoured. Adding the step and the
`skip` decision extends the strict collation vocabulary (a `bdlg.collation/1` extension: older
readers reject a new collation); the schema/doc note ships with `bh-2sicb`.

**Deleted backfilled bead as skip tombstone.** Because a hard delete leaves no trace in the live
store (evidence above), two routes, both producing a normal decision bead:

1. **Explicit, always available:** `bh backfill decide --deleted <bead-id> skip` recovers the
   bead's `external_ref` with one targeted `bd history <bead-id>` read (not a history scan) and
   records the skip. Documented as the replacement for "delete it".
2. **Opt-in detection:** `backfill.decisions.on_delete = ignore | report | skip`, default
   **`ignore`** (today: a deleted bead's unit is proposed again). `report` and `skip` compare
   the units last applied (the run directory's previous plan -- ids are deterministic from the
   unit key) with the current baseline; a unit whose minted bead is gone is "deleted since last
   apply": `report` lists it in the plan summary, `skip` also records the decision bead through
   the normal apply. Detection is host-local (it needs the previous run directory), the
   resulting record is ledger-resident and then protects every host.

### Trade-offs

A decision bead is one more (closed) bead per decision in the hive: visible in `bd list --all`,
countable, and it will show in broad queries unless filtered by `source:decision`. Accepted: the
alternative stores either lose skips (R1), hide them from BeadLog (R3) or put them outside the
ledger (R4). Skipping a unit permanently is also less discoverable than a closed bead, so
`inspect` lists decisions and `bd` can list them by label. The default for `on_delete` stays
`ignore`: auto-tombstoning on a heuristic comparison is the kind of behaviour that should be
switched on, not surprise an operator.

## Q4 -- Exclude-pattern syntax for the docs and planning readers (#155 part 1)

### Evidence

- Docs reader `_discover` (`docs_reader.py:~125-165`) collects files from layout presets
  (`LAYOUT_PRESETS`), then each `globs` entry through `Path.glob` (`:160-162`), then explicit
  `files` (`:163-169`); an explicit file with no enabled detecting format is an error.
- Planning `RoadmapFormat.discover` (`planning_reader.py:479-492`): explicit `files`, then
  `globs`; only when **neither** is given does it fall back to the conventional `ROADMAP_FILES`
  (`:69-`), which is why naming a checklist file replaces the default roadmap lookup (#153).
- Reader options are a flat `dict[str, str]` of comma lists (`csv(...)`;
  `BackfillSourceConfig.options`, `contracts.py:2270`), so a new option is a string.
- Python floor is 3.11 (`pyproject.toml:6`): no `PurePath.full_match`; `**` is handled by
  `Path.glob` for *discovery*, so exclusion needs its own `**`-aware matcher.

### Options

- **E1 -- an `exclude` list of globs**, applied after discovery. Same glob language as `globs`.
- **E2 -- gitignore-style ordered `patterns`** with `!` negation (last match wins).
- **E3 -- both**: `exclude` now, `!`-negation later.

### Recommendation: E1 now, E3's door left open

- New option `exclude` on **both** readers, a comma list, matched against the repo-relative
  posix path. Glob language = that of `globs` (`*`, `?`, `[...]`, `**`; no brace expansion); a
  trailing `/` means the directory's whole subtree (`docs/archive/` = `docs/archive/**`). A
  pattern with no `/` matches a file name at any depth (`*-template.md`), the one gitignore
  convention operators universally expect.
- **Applied last**, to the union of layout presets, `globs`, conventional names (the planning
  default roadmap lookup) and `files`.
- **Explicit `files` vs `exclude`:** an explicitly named file that an exclude also matches is
  **excluded with a warning** naming both. The exclude is the narrower, later statement
  (operators add it to silence something), and failing loudly would break a run for a conflict
  the operator can see in the warning; the alternative ("explicit wins") would make an exclude
  silently ineffective for the one file the operator is most likely to be annoyed by.
- **Negation reserved:** a pattern starting with `!` is rejected with a clear error now, so
  adding gitignore-style re-inclusion later is backward compatible. Re-inclusion is already
  expressible by narrowing `globs`.
- Default empty -> today's output unchanged. Per-hive excludes use Q1:
  `backfill.hives.<prefix>.sources.docs.options.exclude`. The reader header records the option
  (`header_options`), so the evidence-reuse check (sources, options and `HEAD` unchanged,
  `backfill_history.py` "Resume") invalidates on a change.

### Trade-offs

No negation in v1: an operator who wants "everything under `docs/` except `docs/archive/` but
keep `docs/archive/adr/`" must write `globs` accordingly. Gained: a small, predictable matcher
and a csv-compatible option. The custom `**` matcher is the main implementation cost; it must be
shared by both readers (one helper in `_sources.py`).

## Consequences and cross-references

- `backfill-history-pipeline-adr.md` gets an "Amendments" pointer: Phase C "NEW creates closed
  beads" and B6 "`declared_status` never sets a status" are amended by Q2; B5 gains step 0
  (decisions) by Q3; the "per-hive override map" of B3 is realised by Q1.
- Docs the children update: `docs/BACKFILL.md`, `docs/CONFIGURATION.md`, one
  `## 0.36.x -> 0.37.0` section in `docs/UPGRADING.md`, `CHANGELOG`. The `lifecycle.py` and
  `identify.py` module docs change with their code.
- Operator-visible output changes in 0.37.0 if this ADR is approved as recommended: **one** --
  a docs-only `create = propose` run skips evidence-free doc units instead of creating them open
  (Q2 item 4). Everything else is opt-in.

## Child acceptance refinements

These are the refinement comments to post on each child once the operator approves this ADR.
They are recorded here, not posted yet.

**`bh-pieo5` (reader excludes):**
> ADR bh-meuhp Q4 refinement: option name `exclude` on both readers (csv of globs, repo-relative
> posix paths; `**`, `*`, `?`, `[..]`; trailing `/` = subtree; a pattern without `/` matches a
> file name at any depth). Applied last over layouts, `globs`, conventional names (planning
> default roadmap lookup) and `files`. An explicit file matched by `exclude` is excluded with a
> warning naming file and pattern. A pattern starting with `!` is a validation error (reserved
> for later negation). One shared matcher in `_sources.py` (Python 3.11: no `full_match`). The
> option is part of `header_options`, so evidence reuse invalidates on change. Tests: both
> readers, default empty = unchanged goldens, explicit-vs-exclude warning, `!` rejected.

**`bh-g71fi` (per-hive config):**
> ADR bh-meuhp Q1 refinement: overlay lives in HOST config at `backfill.hives.<prefix>.*`
> (key = registry `prefix`; no prefix -> clear error, never derived). Model `BackfillHiveOverlay`
> = `BackfillConfig` minus `judge`. One resolver `effective_backfill(cfg, hive)` used at every
> read site (`backfill_cli.py:378,492,640,653,1012,1045,1320,1611`, `backfill_continuous.py:85`,
> `backfill_coverage.py:41`, `work_merge.py:1565`, `cli.py:1884`); mappings merge key by key,
> lists/scalars replace; no overlay = host block unchanged. Tests: fleet-scope write to
> `backfill.hives.*` refused; overlay `judge` rejected; `collate.areas` per hive; a hive with no
> overlay byte-identical. `bh config get <key> --hive <prefix>` prints the effective value and
> the layer; `config set backfill.hives.<prefix>...` works through the existing writer. Do not
> block on `bh-q7f9f`; implement the layer display so it can be reused for `work.*`.

**`bh-2sicb` (per-unit decisions):**
> ADR bh-meuhp Q3 refinement: a decision is a closed `decision`-type bead per unit key (id
> `mint_bead_id(prefix, group, "decision:"+key)`, labels `origin:backfill` + identity triplet +
> `source:decision`, `external_ref=bfdec:<key>`, metadata `backfill.decision/unit/target/reason/
> decided_by/decided_at`). Verb `bh backfill decide <unit-key> skip|noise|link [<bead>]
> [--reason] [--hive] [--dry-run]` and `decide --deleted <bead-id> skip` (recovers the
> `external_ref` with one `bd history <id>`). identify gets step 0 `decision` before
> `identify.py:606`, decision beads are excluded from the baseline candidates/refs; link ->
> PRESENT, noise -> NOISE (requires an epic), skip -> new projector decision `skip` (nothing
> projected, not residual). Stale link/noise targets are reported, not applied. Setting
> `backfill.decisions.on_delete = ignore|report|skip`, default `ignore`; detection compares the
> previous run's plan with the baseline. Tests: skip/noise/link round trip, `--dry-run` writes
> nothing, export/import survival, delete -> `decide --deleted` -> re-run not re-proposed,
> `on_delete` modes, stale target. Note the collation vocabulary extension (new step/decision).
> No compact/flatten, no history rewrite. Depends on `bh-pieo5` (as filed) and Q1 for the
> `decisions` setting.

**`bh-f4t6r` (status from the doc):**
> ADR bh-meuhp Q2 refinement: key `backfill.collate.status.doc_default = closed|open|from-doc`
> (default `closed` = today, unless the operator approved a default change) plus
> `status.doc_open` (default `["draft","proposed"]`; `from-doc` also treats a raw status matching
> `design_only` as open). Add to `StatusOptions` (round-trips through the collation record) and
> `BackfillStatusConfig`. Applies only to a unit with no evidential closure (`basis` `doc` or
> `none`); evidence still wins and `doc-draft-code-landed` etc. still fire. Doc checklists are
> out of scope (planning reader). Tests: git-history run and docs-only run in each mode,
> including the reporter's three example docs; no mode changes an existing bead.

**`bh-uysl1` (docs-only runs):**
> ADR bh-meuhp Q2 item 4 refinement: in `create = propose`, a unit with no closing moment
> (`lifecycle.py:564-566`) is not created: it is reported as `skipped: no closing moment` in the
> plan summary and `inspect`, in modes `closed`; in `open`, and in `from-doc` for a doc whose
> declared status maps open, creating the open bead is intended. `create = off` unchanged.
> Default `git,docs,planning` output byte-identical. Test: docs-only propose creates no open
> bead silently in each mode.
