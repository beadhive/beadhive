# Operational-workflow substrate ADR — beadhive declines beads' `formula` / `wisp`

**Status:** accepted · **Date:** 2026-08-20 · **Supersedes:** nothing ·
**Generalises:** [loop-ownership-and-execution-memory-adr.md](loop-ownership-and-execution-memory-adr.md)
**Decision 3**, which rejected formula/wisp for the *dispatch loop* specifically. This record
extends that refusal to every operational workflow in the repo, on independently gathered
evidence, and additionally discharges that ADR's "Open prerequisite — the gate-instrumentation
gap".
**Related:** [setup-guide-adr.md](setup-guide-adr.md) (Guide as the adopted runbook layer),
[work-runtime-tiers-adr.md](work-runtime-tiers-adr.md) (beads-as-state Decision 1),
[gas-frameworks-comparison.md](gas-frameworks-comparison.md) (where the Wisp row is already
recorded as absent)

Filed by decision bead `bh-gj0v9.4`, joining spike molecule `bh-gj0v9` — *"does beadhive need
beads' formula/wisp at all, given Guide is already its adopted operational-workflow substrate?"*
Four spikes ran; all four artifacts are in `docs/spikes/` and are the evidence base for
everything below. This record is the synthesis, not a summary — read the spikes for the
measurements.

> **Scope guard.** This ADR settles one question: **what substrate beadhive uses to describe and
> track operational (non-code-change) work**, and specifically whether beads' `formula` / `cook`
> / `pour` / `wisp` family is part of that answer. It does **not** re-decide Guide's adoption
> (settled by `setup-guide-adr.md`), the runtime-tier seam, or the loop-ownership process model.
> It does **not** deprecate anything: beadhive has zero formula/wisp integration today, so
> "decline" means *keep not having it*, and there is no migration.

---

## Context

A long research pass in mid-2026 produced a framing that looked like a substrate choice: beads
ships `formula` / `cook` / `pour` / `wisp`, beadhive ships Guides, both describe multi-step
workflows, so pick one — or adopt formula for the parts Guide leaves open. The pass also produced
one fact that mostly dissolves the framing:

**A formula never executes anything.** Verified against `bd formula schema` (bd 1.1.0) across all
18 exported structs: a `Step` carries `id` / `title` / `description` / `notes` / `type` /
`priority` / `labels` / `metadata` / `depends_on` / `needs` / `waits_for` / `assignee` / `expand`
/ `expand_vars` / `condition` / `children` / `gate` / `loop` / `on_complete`. There is **no
`action`, `script`, or `command` field anywhere in the schema**, and no `rollback`, `compensate`,
`undo`, `retry` or `on_failure` field either. `cook` resolves a template into a proto; `pour` /
`wisp` materialise **bead records**. The doing always happens out of band.

That makes "reimplement `release.py` / `onboard.py` as a formula" a **category error**, not an
option with tradeoffs. The only shape beads supports is a hybrid — declarative formula for the
DAG, imperative code for the work — and the only question left open was whether that hybrid is
worth the double-bookkeeping. Four spikes were filed to answer it rather than guess, because
filing implementation beads against a premise this unstable is exactly the failure the spike loop
exists to prevent.

They ran independently and converged:

| Spike | Question | Verdict |
|---|---|---|
| [`bh-gj0v9.1`](../spikes/bh-gj0v9.1-formula-vs-python-hybrid.md) | Can a formula express `onboard.py`'s step DAG — is the hybrid worth it? | **NO-GO** |
| [`bh-gj0v9.2`](../spikes/bh-gj0v9.2-audit-trail-feedback-signal.md) | Is a scheduling / model-tier feedback loop worth building, and on what source? | **NO-GO** |
| [`bh-gj0v9.3`](../spikes/bh-gj0v9.3-guide-vs-formula-boundary.md) | Does formula/wisp fill Guide's missing retention story? | **NO-GO** |
| [`bh-gj0v9.6`](../spikes/bh-gj0v9.6-no-code-change-tasks.md) | Does wisp (or a new bead kind) fit a purposefully branch-free operational task? | **NO-GO** on both — **GO** on a third option |

**The split-verdict case was checked, not assumed away.** `bh-gj0v9.2` was designed to be able to
land GO while the formula questions landed NO-GO, and the decision bead was explicitly warned not
to let a formula NO-GO silently kill it. It did not: `.2` returned NO-GO on **its own** evidence
(a flat 7.1%-vs-7.1% bounce rate and an underpowered, confounded training set), on a question that
never mentions formula. And `bh-gj0v9.6` *did* split internally — two NO-GOs and a GO — and that
GO is carried through as Decision 5 below rather than being averaged into the molecule's NO.

---

## Decision 1 — beadhive does not adopt `formula` as a workflow substrate

**No beadhive workflow will be re-expressed as a `.formula.json`, and no bead should be filed to
convert one.** Named and declined explicitly: `onboard.py`, `doctor.py`, `release.py`, the `local`
patrol loop, and the release cut.

The blocker is not expressiveness — it is **ordering**. Every runtime-dependent construct in a
real beadhive workflow must be resolved in Python *before* `cook`, which means the Python already
knows the full step membership and order at the moment it would hand them to the formula. The
formula is therefore never the source of the DAG; it is a downstream copy of a decision the code
has already made. `bh-gj0v9.1` measured this against `onboard.py`'s eight membership predicates:
the six scalar installer flags and the tri-state `hub_sync` transcribe cleanly, but
`registry.find_entry(...) is None or c.force` and the per-plugin `_p.enabled(...)` are compound
calls the single-term `condition` grammar rejects outright, and `unclean_applies` depends on
`ctx.cloned` — state produced *mid-run*, after a step that a pour-time constant cannot wait for.

Three further limits, all confirmed live rather than read off docs:

- **Runtime fanout is not expressible.** `LoopSpec.range` documents variable substitution and does
  not implement it (`invalid range "1..{{plugin_count}}"`); only a literal range expands, which is
  precisely the number the plugin registry makes dynamic. `on_complete.for_each` reads
  `output.<field>` from a step — a bead has no output field, and closing a step produced no
  fanout. `expand` / `ExpandRule` name their targets statically.
- **There is no branch-on-probe and no rollback.** `BranchRule` is a fork-join for parallelism
  (all branches run), `Gate` is an async wait, and `AdviceStep`'s `args` / `output` maps are
  opaque strings, not commands. `onboard.py`'s `_act_bd_init` branches three ways on live
  filesystem/remote probes and `_run_bd_mint` runs a compensating cleanup on failure. The schema
  has no field for either.
- **The copy would be unguarded.** `tests/test_onboard_dag.py` is 813 lines asserting execution
  order, per-flag gating, gate-before-`bd-init`, `--skip-check` downgrades and dry-run
  mutation-freedom — every assertion against `build_steps()`. A parallel formula would restate ~19
  nodes and ~30 edges with **no test that fails when the two disagree**, and because its condition
  set is a lossy projection of the Python predicates, exact equivalence is not checkable even in
  principle.

The two negative cases bound the claim from the other side: `doctor.py` has **zero ordering
constraints** between its 20 sections, so a formula's entire contribution (`depends_on` / `needs`)
would be an empty set plus a second copy of the list; and the `local` patrol loop is an unbounded
`while True` whose fanout is `bd ready`'s result each tick, which has no finite materialisation —
`LoopSpec` itself requires `max` whenever `until` is set, "to prevent unbounded loops". So
`onboard.py` — the strongest candidate in the repo, and the only Python already written as a
declarative DAG — is the high-water mark, and it does not clear the bar.

---

## Decision 2 — beadhive does not adopt `wisp` as a work-item substrate

**No `bh` verb, config key or code path will materialise or consume wisps as primary work.** Two
spikes reached this independently, and `loop-ownership-and-execution-memory-adr.md` Decision 3 had
already reached it for the dispatch loop.

The disqualifiers are structural, not gaps waiting to be filled:

- **A wisp is invisible to the work queue.** With 8 open wisp issues live in a scratch hive,
  `bd ready` printed *"No open issues"* and `bd list` printed *"No issues found"*. The whole reuse
  argument for materialising an operational run as beads is "beads IS the state" — but a wisp is
  not on the ready surface, so `bh work ready` and every dispatch loop are blind to it. It is a
  private side-table that happens to live in the issues table.
- **A wisp can block real work, invisibly.** `bd dep add <persistent> <wisp>` succeeds, after
  which `bd ready` reports everything blocked with the blocker absent from every list view.
- **GC of a *completed* wisp silently rewrites a persistent bead's dependency graph.** This is the
  finding that ends the discussion for infra-apply-shaped work. In a live experiment: close the
  wisp, run `bd mol wisp gc --closed --force`, and the persistent bead it was blocking comes back
  with **no `DEPENDS ON` section at all** — no tombstone, no record of what gated it. What GC
  destroys is not the ephemeral op's own record but the *permanent* bead's history of having been
  gated by it. And "closed" is exactly the state a completed op spends its life in;
  `gc --closed` purges all closed wisps regardless of age.
- **Wisps are local-only.** *"stored locally but NOT synced via git"* — invisible to every other
  seat and machine. `publish_export.py:47` already names `--all` permanently forbidden for
  reaching the ephemeral wisps table.
- **bd's own routing doc sends these cases elsewhere.** `bd mol wisp --help`: wisp is for *"any
  operational workflow **without audit value**"*; `pour` is for *"anything worth preserving in git
  history."* An infra apply, a Guide run, a release — who did what, to which environment, when —
  is audit value by definition.
- **The lifecycle is unexercised and self-contradictory.** `bd mol wisp list` returns empty in
  every hive here; `bd mol wisp --help` says wisps are *"issues with Ephemeral=true in the main
  database"* while `bd promote --help` says it copies *"from the wisps table (dolt_ignored)"*.

The one wisp use case anyone in this hive has named as a fit still stands and is still unbuilt: a
**per-pass dispatch trace** — burn on a clean pass, squash into a digest on escalation. That is a
housekeeping signal, which is what the `--wisp-type` enum (`heartbeat, ping, patrol, gc_report,
recovery, error, escalation`) has said all along. Nothing in this ADR forbids it; it is simply not
a work-item substrate.

---

## Decision 3 — no scheduling / model-tier feedback loop, on any source

**Do not build a loop that learns routing (model tier, seat) from past outcomes. Do not re-open it
on "we just need more data."** The blocker is value, not availability.

The cheapest member of the query family — *does the tier predict whether a bead bounced at
review?* — is answerable today in one `SELECT` over durable, git-synced columns, and the answer is
flat: **7.1% bounce for `model:sonnet` (169 beads), 7.1% for `model:opus` (112)**, with median
time-to-close 50.5m vs 55.0m — the *cheaper* tier marginally faster. There is nothing to route on.

There also could not have been, and the data is worse than empty:

- **Underpowered by ~1.5 years.** Detecting a 2× change in a 7.1% rate at α=0.05 / 80% power needs
  626 beads per arm (1252 total). The tiered training set is **126**, growing at ≈2.4/day ⇒ ~520
  days — of a process that must stay stationary while `bd`/`bh` change weekly and the planner's
  own tiering heuristic is under active revision.
- **Confounded in the dangerous direction.** Tier is not a randomised treatment; it is the
  planner's difficulty judgment (this very epic's design field argues every child to opus on
  exactly those grounds). Harder beads got opus, easier got sonnet, both landed at 7.1% — the
  signature of a **manual policy that is already working**. A correlational learner cannot see the
  difficulty term; it sees "tier does not predict failure" and recommends demoting everything to
  the cheapest tier, inverting the policy that produced the flat rates. The failure mode is not
  "learns nothing", it is "learns the opposite, with a number attached."
- **Every candidate source fails on its own terms** (see Correction 2 below for `events`);
  `dolt_diff_issues` is **26.8% phantom rows** (3077 of 11461), 1772 of them injected by two `bd`
  schema migrations — and `bd` is at migration 62 and ships more each release, so this recurs
  permanently at corpus scale; and a *new* execution record would add rows **and Dolt commits** to
  a store already at ~68× amplification (6.9 MB logical → 470 MB archive, 8622 commits in 26 days)
  whose only compaction path is forbidden until `bh-3vs6c` lands.

One methodological finding is worth more than the verdict and is recorded here so it is not
re-learned: **`AVG()` reverses the conclusion in two independent places.** Mean time-to-close reads
`sonnet 1097.1m` vs `opus 59.2m` — an apparent 18× gap that is entirely one bead left open ~17
days. And the "this gate historically takes 40 minutes" premise behind the whole audit-trail idea
is likewise a mean artifact: the median gate of *every* class closes in **~5 minutes**. Any future
consumer of these columns must use percentiles. The one real signal — a 10× p90 split, 44.6m for
review vs 462m for kickoff — is fully predictable from the gate's own `reason` string, already
present at creation time. There is nothing to learn, only something to read.

---

## Decision 4 — the layer boundary: Guide owns the run, beads own the work

**Guide is beadhive's operational-runbook layer. Beads are its work-item layer. A workflow needing
both composes by layer, never by mirror.**

They are structurally different, and beadhive's own shipped Guide proves it quantitatively: across
the 12 steps in `src/beadhive/assets/guides/setup/steps/`, **7 verify by `agent_judgment` and 5 by
`script`; all 12 declare `on_failure:`; all 12 declare `interactions:`.** A beads `Step` has none
of those fields. The complement runs one way only — a Guide can *use* beads for storage; beads
cannot express what a Guide step does.

The composition rule, taken from the one workflow that already needs both (`backfill`): a Guide
run may **produce** beads, and may **be stored in** a bead corpus, but a Guide run must never be
**shadowed by** a hand-maintained parallel bead. The run log owns *"how it went"*; beads own *"what
work exists."* Mirroring is the only shape that manufactures two sources of truth, and no beadhive
workflow needs it.

Applied to every real workflow in the repo:

| Workflow | Substrate | Why |
|---|---|---|
| Setup Guide (12 steps + nested `guides/rescue/`) | **Guide, alone** | Judgment + verification + retry; and beads is *literally unavailable* — the Guide runs on a machine with no `bh`, no hive, no `bd`. Step `080-first-hive` is where a bead store first exists. |
| `github-app-tier-provision` (7 steps) | **Guide, alone** | Two `performer: human` prerequisites; single-session, needs no place in the dependency graph. |
| `backfill` (5 steps) | **Both, by layer** | Guide is the execution trace; its *product* is durable beads. Idempotent re-run proposes zero changes, so nothing is lost by not tracking the run as work. |
| Release cut (`just bump` + `release.yml`) | **Neither** | Already mechanised in CI; its human gate is already `environment: pypi-prod` — the `gate: gh:run` shape formula models, with a real executor behind it. |
| `onboard.py` (1,562 lines) | **Neither — it is imperative code** | Two-phase DAG executor with real `action` callables; its own docstring says "onboarding-specific by design — NOT a generic workflow engine." |

The one GO candidate this decision had to dispose of was **beads-as-Guide-`StateBackend`**, on the
epic's belief that it was parked upstream and beadhive should push it. That belief was false — see
Correction 1. And the retention gap it was meant to close **measures zero**: `~/.guide` does not
exist on this machine (0 files, 0 bytes, 0 B/day) and the runtime's own corpus of 28 real run logs
averages **1.9 KB** (max 4.3 KB), against O(machines) / O(hives) run cardinality — ~550,000 runs
to reach 1 GB. Beadhive does not own that decision anyway: the backend is chosen by the
runtime/harness + operator, beadhive has **zero dependency on the agentguides runtime** (the Guide
ships as inert markdown assets), and `bh setup guide`'s own fallback wizard persists no run state
at all.

If retention is ever genuinely wanted it is `find ~/.guide/state -path '*/runs/*.md' -mtime +N
-delete`, or an upstream RFC against the append-only decision — not an engine, and not a
substrate.

---

## Decision 5 — the no-code-change question is already answered: **gate beads**

This is the molecule's positive outcome, and the only thing anyone should act on.

`bh-87ktb` made `bh work review --run/--demo` refuse when the resolved branch has zero commits
over its base. That is correct and stays. But it also blocks work that is *purposefully* code-free
— the operator's example being "apply the tofu infra changes" — and raised the question of whether
`bh` needs a new concept to wrap such tasks.

**It does not. The concept exists and is called a `type: gate` bead.** Verified end-to-end:
`bd gate create --blocks <bead> --type human --reason "ops: apply the tofu infra changes"` blocks
the dependent bead in `bd ready`; `bd gate resolve <id> --reason "<evidence>"` releases it. No
branch, no worktree, no commit, at any point. Gate beads are persistent, versioned (`bd history`
returns both versions with timestamps and author), dependency-blocking, git-synced, and **never
GC-eligible** — the exact properties a wisp lacks. This spike was itself blocked by one.

Two shapes, both shipping today, nothing to build:

1. **Human / out-of-band op that gates code work** → a **gate bead**, resolved with the evidence in
   the reason (plan hash, run URL, state serial).
2. **Agent-performed op with a real outcome** → an **ordinary bead** whose deliverable is the
   *evidence record* (e.g. `docs/ops/<date>-<env>-apply.md` carrying the plan hash and apply
   output). It then has ≥1 commit and every existing guard passes untouched — and `bh-87ktb`'s
   refusal stops being friction and becomes the mechanism that **forces the audit trail to exist**.

**A formal no-code-change bead *kind* is declined** for the same reason wisp is: cost without
coverage. It is not one flag on `claim`. `claim_authority.py` writes `bh-claim.json` into the
worktree's own git-dir, so "skip worktree provisioning" also deletes the store that seat
verification and the fencing-token epoch guard both read — a no-worktree kind needs a **second
claim-authority backend before it can be claimed at all**. On top of that, the zero-commit refusal
is four checks across three verbs (`submit`'s `_history_ok` and its clean-checkout/`head_sha`
trio, `review`'s refusal, `merge`'s `_guard_bead_clean_history` plus a `--no-ff` with nothing to
merge), and the review gate is keyed on a sha that would have to be substituted. That is a
parallel lifecycle, for a case two existing mechanisms already cover with zero new code.

---

## Corrections to the record

The epic's own framing carried three errors load-bearing enough to name here rather than fix
silently. A future reader who takes the epic's findings literally will re-derive the wrong
answer.

**Correction 1 — Guide's beads `StateBackend` is NOT parked; it shipped.** Epic `bh-gj0v9`
finding 5 states *"Guide's own SPEC.md names beads as the obvious future StateBackend but parks it
(ADR 008); no beads code path exists in the Guide runtime."* **False.** The agentguides runtime's
`.planning/decisions/008-beads-mode-set.md` reads `Status: Accepted`, `BeadsBackend(mode=local-cli)`
shipped in **v0.2**, and the runtime is at **v0.5.12** with 878 lines live under
`src/agentguides/state/beads/` (`backend.py` 601, `_local_cli.py` 174, `_client.py` 89), served by
the web layer as a first-class backend and by `guide review` emitting proposals as beads. What is
stale is the **SPEC.md text**, not the runtime.

Why the correction matters: finding 5 was the *entire* GO candidate on the Guide side. Read
literally it says "there is an accepted-but-unbuilt upstream contribution beadhive should fund" —
which would have sent an implementation molecule to build something that landed three minor
versions ago. The correct statement is: **there is nothing to push upstream, because it is
upstream.** Note also that shipping it did **not** bring retention with it (the ABC has no delete /
prune / expire method, and `guide state`'s 14 CLI verbs have none either), so the candidate fails
twice over — it is both already done and not the answer to the gap it was nominated for.

*Sub-correction, for accuracy of the spike record:* `bh-gj0v9.3` Evidence 2 and 8 say the
`StateBackend` ABC has **ten** abstract methods and that this is "+67% drift" from the spec's six.
It has **nine** (`start_run`, `load_run`, `append_event`, `update_step`, `list_runs`,
`list_all_runs`, `set_status`, `mark_prereqs_checked`, `current`, plus concrete
`read_events`/`load_runs`). The drift is 6 → 9, **+50%**. The conclusion is unchanged: do not take
a code dependency on an unversioned interface that has moved by half again while the published
spec text stood still. Keep beadhive's coupling to agentguides at the `$schema` layer that
`setup-guide-adr.md` Decision 4's mitigation actually covers.

**Correction 2 — the durable event log the epic assumed does not exist.** Epic finding 7 premises
much of the audit-trail question on *"an `events` table with 9k+ rows live in this hive."* The
table is in **`dolt_ignore`**: every one of its rows shows `to_commit = WORKING` in
`dolt_diff_events` — never committed, therefore never pushed, therefore **host-local**, absent
from every other clone and outside every backup that covers the corpus. `dolt_status` is clean
*because* the table is ignored. It is also **unstable across readings**: the epic recorded 9k+
rows, it now holds 6025 — a table that shrank while the corpus it describes grew from 2321 to 3300
issues — and a bead the epic recorded as having zero event rows now has two.

This inverts a derived conclusion elsewhere in the repo. The "gate-instrumentation gap" (406 of
680 gates with no `created` event) is **not** a defect: every one of the 406 predates the local
table's 13-day horizon, while the corpus is 53 days deep. And the `KNOWN LIMIT` paragraph in
`src/beadhive/work_next.py` plus the open prerequisite in
`loop-ownership-and-execution-memory-adr.md` both cite those numbers as grounds to treat
`attempt_count` as a lower bound — but **no derived count in this codebase reads the `events`
table**. `attempt_count` and `dispatch_cause_count` both consume `_flow_events`, which reads
`issue_type='event'` **beads** written by `bd set-state`: ordinary `issues` rows, Dolt-committed,
git-synced, 916 of them reaching back to 2026-07-12. The prerequisite is **discharged by re-aiming
it, not by repairing anything**, and the loop-ownership ADR can move to accepted outright.

A genuine defect *was* found in the same pass and is filed separately as **`bh-tc44a`**:
`plan.py`'s `_create_kickoff_gate` / `_create_release_hold_gate` discard `bd gate create`'s return
code, unlike the sibling checked path in `work_logic.ensure_review_gate` — the checked path is
173/173 instrumented, the unchecked one 95/103. It is a planning-plane correctness issue, entirely
separate from this ADR's verdict. Already filed; do not re-file.

**Correction 3 — two sizing figures the epic carried are wrong and should not be cited again.**
The "~6000 rows/day" write-volume premise is off by ~10×: the durable corpus grows **60–150
rows/day** (lifetime mean ≈62). Bloat is real but lives in the other dimension — **commit count**,
because every bead write is its own Dolt commit (8622 commits in 26 days, 330–725/day). And
`bd sql` **works** in this hive now (it runs a Dolt sql-server on port 3308); the epic's "`bd sql`
does not work at all in embedded mode" is stale. That changed what was *reachable* for the spikes,
not any verdict.

---

## What this ADR does *not* say

Guarding against over-generalisation, because the NO-GO is narrow and the mechanisms it declines
are not broken:

- **It does not say formula's DAG filtering is broken. It works.** `condition` filtering with
  automatic **dangling-edge repair** is a real, correct primitive: filtering 19 steps down to 13
  dropped `clone` and five un-flagged installers, and `identity` — whose only edge was to the
  now-absent `clone` — came out `READY`, not deadlocked. That matches `onboard.py`'s own
  `_topo_order` semantics exactly, for free. The tri-state `{{hub_sync}} != false` also
  round-tripped correctly. It is worth nothing *here* only because onboard's membership is
  runtime-computed. **If some future beadhive workflow ever has a step set genuinely determined by
  static flags at plan time, this is the piece that would carry it.**
- **It does not forbid wisp as a housekeeping trace.** Decision 2's carve-out stands: a per-pass
  dispatch trace (burn clean / squash on escalation) is the one named fit, unbuilt.
- **It does not soften `bh-87ktb`.** Forcing an operational task to leave a committed artefact is a
  feature. Decision 5 routes around it rather than through it.
- **It does not claim the model-tier signal will never exist.** It claims the *loop* is not worth
  building on today's evidence, and prices re-opening on volume alone at ~520 days — with the
  confounding of Decision 3 unfixed by any amount of additional non-randomised data.

---

## Consequences

**Nothing is built by this ADR.** Beadhive has zero formula/wisp integration; this record keeps it
that way and states why, so the question is not re-litigated from intuition.

Named as **future work, deliberately not filed from the decision bead** (implementation beads are
filed through the planner, never from a spike verdict):

1. **Teach `bh work approve` an `ops:` gate kind**, alongside the existing `security:` and
   `release-hold:` cases — one entry in `_gate_kind`'s table and one `_approve_*_gate` helper in
   the shape that already exists. Today an ops gate must be resolved with raw `bd gate resolve`,
   because `approve` refuses non-review gates. This is the **only real gap** Decision 5 leaves;
   it is small, additive, and touches no part of claim / submit / merge.
2. **Documentation corrections, no code:** amend the `KNOWN LIMIT` paragraph in
   `src/beadhive/work_next.py` and the "Open prerequisite" section of
   `loop-ownership-and-execution-memory-adr.md` per Correction 2; and record the `events` table's
   durability class in `docs/OBSERVABILITY.md` — *"the `events` table is in `dolt_ignore`:
   host-local, never committed, never pushed; the durable audit record is `issue_type='event'`
   beads"* — so the next investigation does not start where this one did.
3. **If run telemetry from `onboard.py` is ever wanted**, take it from `OnboardPlan`
   (`steps_run`, `checks`, `skipped_checks`, `cloned`, `reconfigure`) through the existing
   `jsonout` envelope. One structured line is strictly more data than a wisp squash digest, costs
   no second DAG description, and is already covered by the 813 lines of existing tests.

**Explicitly not to be filed:** a `bead_intents`-style table, per-attempt instrumentation, wisp
telemetry, an OTEL bead-id join, any new durable execution record, a formula transcription of any
beadhive workflow, a no-code-change bead kind, or an upstream contribution of the beads
`StateBackend`.

**Reopen this ADR only if** one of its measured premises moves: a beadhive workflow appears whose
step membership is genuinely static at plan time (Decision 1); wisp gains git sync, `bd ready`
visibility, and a GC that does not strip edges off persistent beads (Decision 2); a real
randomised routing experiment produces a tier effect (Decision 3); or an observed Guide run-log
corpus passes ~100 MB (Decision 4). Volume alone is not a reason.

**Two upstream `bd` defects** were found while gathering this evidence and escalated: `bd mol wisp
<formula>` reports *"not found as formula or proto"* when the real failure is a formula validation
error, and `LoopSpec.range` variable substitution is documented but non-functional. Neither
changes a verdict; both are worth reporting upstream regardless.
