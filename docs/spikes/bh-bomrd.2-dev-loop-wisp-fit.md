# Spike `bh-bomrd.2` — does formula/wisp add anything over the shipped OTEL self-check telemetry?

**Bead:** `bh-bomrd.2` · **Seat:** `dev/dev1` · **Type:** research-only (no product code)
**Joins:** `bh-bomrd` — does formula/wisp fit two narrow, already-mechanical pipelines?
**Sibling:** `bh-bomrd.1` (the release loop; it owns the live GC re-test)

> Not a re-litigation of `bh-gj0v9` ([ADR](../design/operational-workflow-substrate-adr.md),
> NO-GO on formula/wisp as a *general* operational-workflow substrate). That molecule scoped its
> four spikes to `onboard.py`'s plugin DAG, a scheduling feedback loop, Guide's retention gap, and
> no-code-change work items. None of them tested this pipeline, and none of them had the
> `/workflows/wisps` narrative page in evidence.

## Question

The developer loop — prepare code → lint-fix → `bh work check` → `bh work submit` — already has a
**shipped, merged** answer for per-attempt visibility: `bh-trgcd.2`'s OTEL self-check span
attributes. Does a formula/wisp shape add anything over it?

The reopener that makes this a fair question rather than a settled one: `bh-trgcd.2`'s design note
leans on CLAUDE.md's *"bead history is a permanent archive, never squash it"* rule to justify
keeping per-attempt iteration **out of the bead corpus**. But wisps claim exclusion from the
permanent audit trail — a *different* property. If a wisp never becomes permanent history at all,
the archive objection does not apply to it, and the comparison has to be decided on the merits
instead. Three sub-questions follow:

1. Is the wisp exclusion claim real, and does the distinction collapse in practice?
2. Does "operational loops" in the wisps docs mean a human's live retry loop, or something
   categorically different?
3. On the merits, then: what does a wisp/formula buy, and what does it cost?

## Method

1. Read the shipped alternative in full, in this repo's source, not from any brief:
   `src/beadhive/otel.py:618-651` (`set_self_check`), `src/beadhive/work.py:1865-1889`
   (`_mark_self_check`) and its call site at `:1831` inside the `check` verb, plus the
   surrounding `check` body (`:1793-1862`) for what else already runs per attempt.
   `docs/OBSERVABILITY.md:163-200` ("Developer self-check spans"). `git show --stat` on the
   merged commit for the real cost of the shipped thing.
2. Fetched and read <https://beads.gascity.com/workflows/wisps> directly — its actual wording,
   its worked examples, and its wisp-vs-pour table.
3. **Live experiment** in a throwaway hive (`bd init --prefix dl`, session scratchpad, never this
   repo's hive; `bd version 1.1.0 (dev)`): created a wisp, claimed it, closed it, and compared
   `bd history` against a persistent control bead put through the same transitions. Then tested
   wisp visibility in `bd list` / `bd ready`, wired a persistent bead to depend on an open wisp,
   and timed `bd create --ephemeral`.
4. Took as settled and did not re-derive: `bh-gj0v9.1` (formula's schema limits) and `bh-gj0v9.6`
   (the wisp-GC edge-stripping reproduction). Did **not** re-run a live GC reproduction —
   `bh-bomrd.1` owns that, and per E10 below it would not have changed this verdict.

## Evidence

### A. What `bh-trgcd.2` actually ships

- **E1. Five span attributes on a span that already exists — no new span, no new record.**
  `set_self_check` (`otel.py:618-651`) stamps `bh.work.phase="check"`,
  `bh.validation.result=pass|fail`, `bh.validation.tree.dirty`, and — when non-empty —
  `bh.validation.tree` and `bh.seat`, onto the **already-open `work.check` verb span**. Combined
  with the `bh.bead` / `bh.epic` that `set_bead` stamps on every traced verb, the span is
  self-sufficient: attempts-before-green aggregates from the span stream with no join against,
  and no write to, bead data.

- **E2. Two guards make the off-path literally free.** `_mark_self_check` returns immediately
  unless `otel.is_active()` (`work.py:1880`), and `set_self_check` returns unless `_initialized`
  *and* the current span `is_recording()` (`otel.py:640-644`). `otel.is_active()` is just
  `return _initialized` (`otel.py:531-534`) and `config.otel_enabled` defaults to **`False`**
  (`config.py:1452-1455`). The docstring is explicit about why the gate sits at the caller and
  not only inside the emitter: the two reads `_mark_self_check` performs — a `git rev-parse` for
  the tree and a claim-record read — *"are not free … the off-path must stay zero-cost"*.

- **E3. Both reads reuse state the verb already has.** The seat comes from the claim record
  `claim`/`resume` already wrote in this worktree — *"the same source `_resolve_submit_actor`
  trusts, so no `bd` read is added either"*. The tree is `validation_ledger.tree_of`, the
  **verdict ledger's own content key** (`work.py:1887`). No subprocess is spawned that the verb
  would not otherwise spawn, and no `bd` call is made at all.

- **E4. `tree` + `dirty` encode something a bead field would not.** Because `tree` is a content
  key, several attempts sharing one `bh.validation.tree` with `tree.dirty=false` mean *the same
  content was re-checked without a change* — re-running rather than fixing
  (`OBSERVABILITY.md:198-200`). `dirty=true` says `tree` names HEAD and **not** what actually ran,
  so the key never silently lies about the one case being measured (iterating on uncommitted
  edits). This is the highest-information part of the signal and it is one boolean plus one hash.

- **E5. The cost, measured.** `git show --stat` on the merged `bh-trgcd.2`: **193 insertions, 0
  deletions, 5 files** — 36 lines in `otel.py`, 28 in `work.py`, 39 of docs, and 90 lines of test
  across `test_otel.py` / `test_work.py`. Already reviewed, already merged to `main`, in this same
  session.

- **E6. The otel-off case is not uncovered, either.** The `check` body already tees the run's gate
  log into `.bh/testreport/<tree>/`, the durable per-tree triage store (`work.py:1810-1825`),
  whose module docstring states its purpose in exactly these terms: *"`bh-ku9n9.8`'s flake signal
  **IS** retry history across runs at the same tree"* (`triage_store.py:12-13`). So retry history
  keyed by tree is **already persisted locally, independent of otel**, by shipped code. OTEL adds
  the cross-seat aggregate view on top; it is not the only record.

### B. Q1 — the exclusion claim is real, and *more* so than the docs say

- **E7. A wisp writes no version history at all.** This is the one place the live run contradicted
  my prior. In the scratch hive, a wisp and a persistent bead were put through identical
  transitions (create → claim → close). The persistent control has a full versioned record; the
  wisp has none:

  ```console
  $ bd history dl-0yd            # persistent control
  📜 History for dl-0yd (2 entries)
  se93o118  2026-08-20 18:25:43  ✓ dl-0yd: persistent control [P2 - closed]
  ck974eoo  2026-08-20 18:25:37  ○ dl-0yd: persistent control [P2 - open]

  $ bd history dl-wisp-bvv       # wisp, same three transitions
  No history found for issue dl-wisp-bvv
  ```

  Consistent with the wisps table being `dolt_ignore`d (`bd promote --help`, quoted in the ADR's
  Decision 2). The `/workflows/wisps` page only claims federation-level exclusion — *"excluded
  from federation push by default (`federation.exclude_types` defaults to `[wisp]`)"* — which
  would be a mere **config default**, one key away from being false. The measured behaviour is
  stronger and structural: the rows never reach a Dolt commit, so there is nothing for a
  `federation.exclude_types` edit to leak.

- **E8. So the distinction is real, and the archive objection is withdrawn.** CLAUDE.md's rule
  protects what `bd history <id>` returns by diffing consecutive versions. A wisp produces zero
  such versions (E7). A wisp-tracked check loop therefore would **not** violate the archive rule,
  and `bh-trgcd.2`'s stated justification does not by itself dispose of the wisp option. The two
  properties do not collapse — "kept out of the corpus by not being a bead" and "a bead that never
  enters the archive" reach the same place by different routes. **This spike's verdict does not
  rest on the archive rule.** Everything below is decided on the merits.

### C. Q2 — "operational loops" on the page is a different animal

- **E9. Every example the page gives is a run-shaped, statically-enumerable checklist.** Its own
  words: *"Operational workflows — **release checklists, health patrols, diagnostics** — create
  beads that are worthless the moment they close."* The wisp-vs-pour table's use-case cell reads
  *"release runs, operational loops, health checks."* Best practices: *"Wisps for operational
  loops — **patrols, release runs, diagnostics**."* The `--wisp-type` enum agrees:
  `heartbeat, ping, patrol, gc_report, recovery, error, escalation`. Not one example is a
  developer's live retry loop; every one is an unattended or scheduled pass over a **known step
  list**, instantiated from a proto (`bd mol wisp <proto-id>`) whose steps exist before the run
  starts.

  Two structural mismatches follow, and they are not stylistic:

  - **The unit is wrong.** The page's wisp unit is *"real beads you work through normally"* — work
    items. A check attempt is not a work item; it is an **observation about** one. The work item
    already exists, is persistent, is claimed, and is on the ready queue: the dev bead. Wisping the
    attempts double-books the same work — the bead says "implement X", the wisps say "attempt 1,
    attempt 2, attempt 3" — with the wisp side carrying nothing the bead does not already own
    except the verdict, which is E1's five attributes.
  - **The step count is not knowable at instantiation.** A patrol's steps are enumerable before the
    run; a retry loop's are discovered by running it. `bh-gj0v9.1` settled that formula has no
    representation for this: no `retry`, `rollback`, `compensate`, `undo` or `on_failure` field in
    **any of the 18 exported schema structs** (its evidence 8), and `LoopSpec.range` variable
    substitution documented but non-functional so the count *"must be baked into the formula
    file"* (its evidence 7). A retry loop is precisely the construct the schema cannot express, so
    the "formula" half of formula/wisp is not merely unhelpful here — it is inapplicable.

### D. Q3 — the named upsides do not survive contact

- **E10. "`bd ready` / `bd list` visibility into an in-progress check loop" is false — wisps are
  the one bead kind with no queue visibility.** Reproduced independently of `bh-gj0v9.6`, with an
  **open** wisp live in the store:

  ```console
  $ bd list                     # open wisp dl-wisp-htq exists
  No issues found.
  $ bd ready
  ✨ No open issues
  $ bd mol wisp list
  Wisps (1):   dl-wisp-htq  open  P2  task  check attempt 2 (open)
  ```

  It is visible only to a query that already names it as a wisp. So the single benefit that would
  distinguish a wisp from a span is the one thing a wisp specifically does not provide.

- **E11. The only way to buy that visibility is to recreate the `bh-gj0v9.6` bug shape.** To make
  a check loop show up on the ready surface you must attach it to something persistent — i.e.
  `bd dep add <dev-bead> <wisp>`. That works, and it makes matters worse, not better:

  ```console
  $ bd dep add dl-h3z dl-wisp-htq
  ✓ Added dependency: dl-h3z depends on dl-wisp-htq (blocks)
  $ bd ready
  ✨ No ready work found (all issues have blocking dependencies)
  ```

  The dev bead leaves the ready queue, blocked by a blocker `bd list` and `bd ready` cannot show.
  This answers the epic's open question for **this** pipeline: the edge-stripping bug is moot only
  while nothing persistent depends on the check wisp — and nothing would, *only* if you forgo the
  one upside that motivated the wisp in the first place. Pursue the upside and you are in exactly
  `bh-gj0v9.6`'s configuration, where `bd mol wisp gc --closed --force` strips the `DEPENDS ON`
  edge off the persistent bead with no tombstone. The benefit and the bug are the same edge. No
  live GC re-run was needed to reach that: the disjunction is exhaustive either way, which is why
  the live GC test is left to `bh-bomrd.1`.

- **E12. "Real dependency edges between attempts" is information-free here.** Check attempts are
  strictly sequential retries of one command against one tree. `attempt₂ depends on attempt₁`
  encodes nothing a timestamp does not already encode, and it is a *worse* encoding than E4's
  content key: the edge cannot distinguish "re-ran unchanged" from "re-ran after an edit", which
  is the actual question a developer-iteration signal has to answer. To carry that, a wisp bead
  would have to stuff the tree hash into a title or description string.

- **E13. Per-attempt cost, measured.** `bd create --ephemeral --wisp-type patrol` medians
  **0.47 s** in the scratch hive (0.472 / 0.462 / 0.492 s over three runs) — a subprocess and a
  DB write. A create/claim/close triple per attempt is ~1.4 s of pure bookkeeping added to every
  `bh work check`, **unconditionally** — there is no wisp equivalent of E2's `is_active()` gate,
  because the whole point of the record is that it exists. Against that, the shipped path costs
  five in-process `span.set_attribute` calls, and *zero* when otel is off. And the wisp's output
  is local-only, never synced (E7 is the mechanism), and burnable — so on the otel-off case it is
  a strictly worse local store than the per-tree triage store that already ships (E6).

- **E14. The asymmetry is the decisive fact.** The shipped answer is 193 lines, already reviewed
  and merged (E5). Any wisp/formula alternative is **new code with no existing caller**: wisp
  creation wired into the `check` verb, GC discipline so the store does not grow without bound,
  and — for the visibility that motivates it — dependency-edge management in the one configuration
  with a known live data-loss bug (E11). `bh` has **zero** formula/wisp integration today
  (`bh-gj0v9.6` E6), so none of this is an increment on an existing seam. To justify it, a wisp
  would have to deliver something OTEL does not. It delivers strictly less: no queue visibility
  (E10), no content key (E12), no cross-seat aggregation (E7), at ~1.4 s per attempt (E13).

## Verdict

| Option | Verdict |
|---|---|
| **Formula** for the dev check/submit loop | **NO-GO** |
| **Wisp** for per-attempt check tracking (replace or supplement `bh-trgcd.2`) | **NO-GO** |
| Keep the shipped OTEL self-check attributes as-is | **GO** (already shipped; no change) |

**Formula — NO-GO, on inapplicability rather than cost.** The pipeline's only interesting
structure is a retry loop, and retry is absent from all 18 schema structs while the loop primitive
that might have stood in for it requires a literal count (E9). A formula also never executes
anything (`bh-gj0v9.1`, settled), so the four steps would still be run by `bh` exactly as today.
There is no version of this that is not two descriptions of one loop, one of which cannot express
the loop.

**Wisp — NO-GO, and *not* for the archive reason.** The reopener is legitimate and was confirmed
stronger than its own documentation claims: a wisp genuinely produces no version history, so
CLAUDE.md's archive rule does not bar it (E7, E8). Judged on the merits it still loses on four
independent grounds, three of them measured in this spike: the queue visibility that is its only
theoretical advantage does not exist (E10); buying it requires the exact dependency-edge
configuration with a known silent-data-loss bug (E11); the inter-attempt edges it would add carry
less information than the one hash already stamped (E12, E4); and it costs ~1.4 s of unconditional
DB writes per attempt against a zero-cost-when-off path (E13, E2).

**Nothing here moves the ADR's own reopen triggers.** Decision 2 prices re-opening at *"wisp gains
git sync, `bd ready` visibility, and a GC that does not strip edges off persistent beads."* E7
measures the opposite of git sync, E10 measures the opposite of `bd ready` visibility, and E11
shows this pipeline would sit squarely on the third. Independently, the ADR's *"explicitly not to
be filed"* list already names *"per-attempt instrumentation, wisp telemetry"* — this spike reached
the same place from its own evidence, on the one pipeline that ADR never examined.

## Recommendation

**Build nothing. Change nothing.** `bh-trgcd.2` stands as merged; this spike is the record of the
narrower question having been asked against it and answered, so it is not re-opened from intuition
the next time the wisps page is read.

Two documentation-only follow-ons, **not** filed as implementation beads from here (implementation
beads are filed through the planner, never from a spike verdict) — both are single-paragraph edits
that a groom pass can absorb:

1. **Correct the record on why the self-check signal is not a bead write.** `OBSERVABILITY.md:167`
   and `work.py:1870` both rest the case on the archive rule alone. E7/E8 show that argument does
   not dispose of the wisp option — the durable reasons are E10/E12/E13 (no queue visibility, no
   content key, unconditional per-attempt cost). The conclusion is unchanged and the code is
   correct; only the stated justification is thinner than it reads.
2. **Record the measured wisp fact** — *a wisp produces no `bd history` versions at all, the
   wisps table being `dolt_ignore`d; the `/workflows/wisps` page understates this as a federation
   default* — next to the ADR's Decision 2 bullets, so the next investigation starts after E7
   rather than re-running it. This does not change Decision 2's outcome; it makes one of its
   premises stronger and one weaker, and both should be on the record.

**For `bh-bomrd`:** this spike contributes a NO-GO on the developer-loop half, reached on the
merits with the archive objection explicitly set aside. It hands `bh-bomrd.1` one transferable
finding — **the wisp-GC edge-stripping bug is reachable exactly when the wisp is made visible to
the ready queue, because visibility and the bug are the same dependency edge** (E11) — which is
the shape its live GC test should be aimed at. Note also that the release loop differs on E9 in a
way that may matter there and does not here: `just attest → bump → release-preview → release` is a
statically enumerable checklist with a known step count, which is the one shape the wisps page
actually sanctions. That spike should not inherit this NO-GO.
