# Operational-workflow substrate ADR — beadhive declines beads' `formula` / `wisp`

**Status:** accepted · **Date:** 2026-08-20 · **Supersedes:** nothing ·
**Addendum 2026-08-20** ([`bh-bomrd.3`](#addendum--2026-08-20-bh-bomrd3-the-two-narrow-mechanical-pipelines)):
two narrow mechanical pipelines tested, both NO-GO; **no decision below is reversed**, but
Decision 2's *stated reasons* are corrected (A4) and Decision 5's gate-bead guarantee is
disambiguated (A3) — read the addendum before citing either.
**Addendum 2 2026-08-20** ([`bh-yber2.3`](#addendum-2--2026-08-20-bh-yber23-the-guardrailed-mitigations-and-the-measured-roi)):
the guardrailed/mitigated release loop tested and the ROI claim measured — mechanism **NO-GO**
on a *false green* (B1), ROI direction confirmed but magnitude overstated and not model-bound
(B4). No decision reversed; Decision 2's reopen bar gains a **fifth** condition and Decision 5 is
reinforced (B2). This is the **final** answer on wisp adoption (B7).
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

---

## Addendum — 2026-08-20 (`bh-bomrd.3`): the two narrow mechanical pipelines

**Status of this addendum:** accepted · **Extends, does not supersede, anything above.** No
decision in this ADR is reversed, softened, or re-scoped. Two of them are *re-grounded* — the
stated reasons change while the verdicts do not — and one measured fact makes a Decision-2 bullet
refutable in one command, so it is corrected here rather than left to be re-derived wrongly.

After reviewing the record above, the operator raised a narrower question the four `bh-gj0v9`
spikes never tested. Those spikes were scoped to `onboard.py`'s plugin-registry DAG, a scheduling
feedback loop, Guide's retention gap, and no-code-change work items — none of them examined an
**already-mechanical, mostly-linear pipeline**, which is the one shape beads' own narrative docs
(`/workflows/wisps`, not in evidence for any of the four) explicitly advertise for wisp. Molecule
`bh-bomrd` ran two independent spikes on the two such pipelines this repo actually has, with an
agent Guide (judgment/interaction) explicitly out of scope:

| Spike | Pipeline | Shipped alternative compared against | Verdict |
|---|---|---|---|
| [`bh-bomrd.2`](../spikes/bh-bomrd.2-dev-loop-wisp-fit.md) | dev loop: prepare → lint-fix → `bh work check` → `bh work submit` | `bh-trgcd.2`'s OTEL self-check span attributes | **NO-GO** |
| [`bh-bomrd.1`](../spikes/bh-bomrd.1-release-loop-wisp-fit.md) | release loop: `just attest` → `bump` → `release-preview` → `release` | `bh release preflight/attest/await/preview/recover` (`release.py`, 934 lines) | **NO-GO** |

They failed for **different reasons**, and neither reason is the one this ADR gives above. That is
the point of recording them.

### A1 — dev loop: NO-GO on measured merits, *not* on the archive rule

`bh-trgcd.2`'s design note (and `OBSERVABILITY.md:167`, `work.py:1870`) rests the case for keeping
per-attempt iteration out of the bead corpus on CLAUDE.md's *"bead history is an archive — never
squash it"* rule. A wisp claims exclusion from the audit trail, which is a **different** property,
so that justification does not by itself dispose of the wisp option. `bh-bomrd.2` tested the claim
and **withdrew the archive objection**:

> **A wisp writes no version history at all.** A wisp and a persistent control put through
> identical create → claim → close transitions in a scratch hive: the control returns two
> versioned entries, the wisp returns `No history found for issue dl-wisp-bvv`.

This is *stronger* than the `/workflows/wisps` page claims. The page describes federation-level
exclusion (`federation.exclude_types` defaults to `[wisp]`) — a **config default**, one key away
from being false. The measured behaviour is structural: the rows never reach a Dolt commit, so
there is nothing for a `federation.exclude_types` edit to leak. **A wisp-tracked check loop would
not violate the archive rule.** The rule is simply not the load-bearing argument, here or in
`bh-trgcd.2`.

Wisp still loses, on four independent grounds, three of them measured in that spike:

- **The one theoretical advantage does not exist.** With an *open* wisp live in the store,
  `bd list` printed *"No issues found."* and `bd ready` printed *"✨ No open issues"*; only
  `bd mol wisp list` — a query that already names it as a wisp — showed it.
- **Buying that visibility is buying the bug.** The only way onto the ready surface is
  `bd dep add <dev-bead> <wisp>`, after which the dev bead leaves the queue *"blocked by
  dependencies"* with the blocker absent from every list view — i.e. exactly `bh-gj0v9.6`'s
  configuration, where GC strips the `DEPENDS ON` edge with no tombstone. **The benefit and the
  bug are the same dependency edge.**
- **Inter-attempt edges carry less information than the hash already stamped.**
  `attempt₂ depends on attempt₁` encodes nothing a timestamp does not, and cannot distinguish
  "re-ran unchanged" from "re-ran after an edit" — which is the actual question.
  `bh.validation.tree` plus `tree.dirty` answers it in one hash and one boolean.
- **Cost asymmetry, measured.** `bd create --ephemeral` medians **0.47 s**; a create/claim/close
  triple is **~1.4 s of DB writes per check attempt, unconditionally** — there is no wisp
  equivalent of `is_active()`, because the whole point of the record is that it exists. The
  shipped path is 193 lines already merged, five in-process `set_attribute` calls, and **zero
  cost when otel is off** (the default). And with otel off, `.bh/testreport/<tree>/` already
  persists retry history keyed by tree.

**Formula is NO-GO here on inapplicability, not cost.** The pipeline's only interesting structure
is a retry loop whose length is discovered by running it, and `retry` / `rollback` / `on_failure`
appear in none of the 18 schema structs, while `LoopSpec.range` needs a literal count. There is no
version of this that is not two descriptions of one loop, one of which cannot express the loop.

**One reading correction this addendum makes explicit:** the `/workflows/wisps` page's own examples
— *"release checklists, health patrols, diagnostics"*, and the `--wisp-type` enum
(`heartbeat, ping, patrol, gc_report, recovery, error, escalation`) — are, without exception,
**unattended passes over a step list known before the run starts**. A developer's live retry loop
discovers its length by running. Those are categorically different shapes, and "operational loops"
on that page does not mean the second one.

### A2 — release loop: NO-GO on a *new*, worse bug, plus an architectural mismatch

This is the pipeline the docs advertise most directly (`bd formula schema Formula`: `phase` —
*"Patrol and **release** workflows should typically use 'vapor'"*; `pour` — *"Reserve pour=true for
critical, infrequent work (**e.g. releases**)"*). It was tested with a real four-step formula,
wisped and walked end to end twice in a scratch hive. **Two of this ADR's headline disqualifiers
turned out not to apply**, and the spike's own central hypothesis was confirmed — and then a bug
neither this ADR nor `bh-gj0v9.6` anticipated killed it anyway:

| Disqualifier above | Holds for a self-contained release run? |
|---|---|
| Invisible to the work queue (Decision 2, bullet 1) | **No** — `bd ready --mol <wisp-id>` lists the wisp's steps, including the gate |
| GC strips edges off *persistent* beads (Decision 2, bullet 3) | **No** — verified live: `Removed 0 dependency link(s)`, control bead's graph intact |
| Squash yields only a throwaway prose blob (`bh-gj0v9.1` ev. 10) | **No** — `bd mol squash` does clear the ephemeral flag; the digest is persistent and versioned |

What kills it instead is **strictly worse than the bug this ADR records, and reachable with no
persistent bead in the picture at all.** `bd mol wisp gc --closed --force` is **hive-wide and
step-granular, with no `--mol` scope flag**. Run against a mid-flight release (attest + bump
closed, 2/5) while one unrelated closed patrol wisp existed elsewhere in the hive — the everyday
reason an operator types the command the wisps page tells them to type regularly — it found 3
closed wisps (the patrol **and both completed steps of the running release**) and deleted all
three. `bd mol current` went from *"Progress: 2/5, attest done, bump done"* to **`Progress: 0/3`**
with no completed steps, and `release-preview` lost its `needs: bump` edge entirely.

The erased fact is the load-bearing one. [`attested-green-adr.md`](attested-green-adr.md):
*"the bump is the last safely reversible moment"* — so **"we already bumped, a local tag exists"**
is precisely the state such a record exists to hold, and it vanishes silently at the one moment it
would be consulted (a stopped release). This is not an oversight nobody considered:
`bd mol wisp gc --help` documents deliberate live-work protection for the `--age` path (GH#4394,
never reclaiming blocked/pinned/wip steps, aborting rather than risking live steps if the blocked
set cannot be read) — **`--closed` bypasses all of it**, because a *closed step of a running
molecule* is not in the protected set. `--exclude-type` is no mitigation: release steps are `task`.

The documented escape hatch fails too. The wisps page's best practice is *"Squash before you
delete"*; `bd mol squash` on that same still-open run deleted three open steps — including the
**unresolved releaser gate and `just release` itself** — and marked the root complete, with no
confirmation and no refusal. The one persistent artifact it leaves then asserts `Completed: 0/3`
about a run that genuinely attested and bumped, because the completed steps had already been GC'd
out from under it.

**And even with every one of those defects fixed upstream, the fit would still fail**, on grounds
that owe nothing to any bug: **the shipped release flow derives position from the world; a wisp
asserts it.** `recover` decides on `git ls-remote` *"against the actual remote, not a local
tracking ref"*; `preview` measures the ledger verdict, the remote tag, and PyPI; `preflight` reads
a verdict keyed on the **tree hash** with *"no flag that turns a refusal into a pass"*; `pending` /
`await` read a marker keyed on `tree_of(entry, sha)`, so a marker for a different tree is not a
marker. All four share an exit contract in which **`3` = COULD NOT MEASURE is never folded into
`1` = refused** — a contract that exists because the 0.11.5 incident was caused by a confident
wrong sentence. A wisp step is a hand-closed assertion verified against nothing: close `bump`, then
`git reset --hard`, and the wisp still reports bumped while the ledger, `ls-remote` and the marker
all report otherwise. Adding a **fifth, unmeasured, non-authoritative** record beside four measured
ones, immediately in front of a one-way door, is a net loss regardless of GC.

This **moves nothing in the Decision-4 table** — the release cut stays **"Neither"** — it changes
the reasons on that row.

### A3 — gate beads: Decision 5 is UNAFFECTED, and here is the distinction that protects it

`bh-bomrd.1` found that a **formula-materialised `type: gate` bead inside a wisp is ephemeral**:
`bd history rl-wisp-v3l` on the resolved human gate returned *"No history found"*, against a full
versioned record for a persistent control. It is unversioned, un-synced, and GC-eligible.

**This does not touch Decision 5.** Read the distinction carefully, because the two things share a
type name and nothing else:

| | `bd gate create --blocks <bead> …` | a `gate:` step materialised by `bd mol wisp <formula>` |
|---|---|---|
| Persistent | **yes** | no — ephemeral, in the `dolt_ignore`d wisps table |
| Versioned (`bd history`) | **yes** — both versions, timestamps, author | no — *"No history found"* |
| Git-synced / visible to other seats | **yes** | no — host-local |
| GC-eligible | **never** | yes, including mid-run (see A2) |

Decision 5 recommends **`bd gate create` directly**, never a formula-materialised gate, and every
property it relies on was verified against that path. Its recommendation stands exactly as
written, unqualified: a human / out-of-band op that gates code work is a **gate bead**; an
agent-performed op with a real outcome is an **ordinary bead whose deliverable is the evidence
record**. Nothing in either `bh-bomrd` spike weakens it.

The reason this is worth spelling out: someone reading Decision 5's *"gate beads are persistent,
versioned, git-synced, and never GC-eligible"* and then reaching for `gate:` inside a formula
because it is the same words would get none of those four properties. **Those are properties of
`bd gate create`, not of the `type: gate` bead shape.** For the release loop specifically, the
existing human gate — `environment: pypi-prod` on `release.yml`, with a real executor, real
identity, and an audit log GitHub retains — is strictly better than either, and moving it into a
wisp loses the executor *and* the record.

### A4 — corrections to Decision 2's stated reasons (the verdict is unchanged)

Three premises above are now measured more precisely. Each makes Decision 2 *better founded*, not
weaker, but two of them are refutable as currently worded and would send the next investigation
down a wrong path.

1. **"Invisible to the work queue" is scoped-query-false.** `bd ready --mol <wisp-id>` **does**
   list wisp steps. The closed spikes measured *bare* `bd ready` and generalised. The real barriers
   are **discovery** — the id appears on no list surface, and `bd mol wisp list` shows only *open*
   wisps, printing *"No wisps found"* the instant a run closes, before any GC — and **locality**
   (host-only, no second operator, no second machine). State it that way; the current wording loses
   an argument to one command.
2. **The GC bullet understates the hazard while overstating its precondition.** The
   persistent-bead form (`bh-gj0v9.6`) is a **special case**. The general form: `gc --closed`
   destroys the completed steps of an **in-flight** molecule and their dependency edges, with **no
   persistent bead involved**. Conversely, the persistent-edge form is *not* reachable in a
   self-contained run — verified live.
3. **`bd mol squash` does promote to a persistent, versioned digest**, correcting `bh-gj0v9.1`
   evidence 10 — but on an in-flight molecule it deletes open steps and auto-closes the root, and
   its summary can be flatly wrong about what completed.

**Decision 2's reopen bar is unchanged and, if anything, tightened.** It prices re-opening at
*"wisp gains git sync, `bd ready` visibility, and a GC that does not strip edges off persistent
beads."* The dev loop measured the opposite of git sync and the opposite of ready-surface
visibility, and would sit squarely on the third. The release loop adds a fourth condition specific
to it: a release step's completion would have to become something `bh` **verifies** against the
ledger/remote rather than accepts on a close — because A2's architectural objection survives every
bug fix.

### A5 — two `bd` defects escalated, one at elevated severity

Both were reproduced live in a scratch hive (bd 1.1.0 dev) under `bh-bomrd.1` and filed via
`bh escalate`. Neither changes a verdict above; both are worth upstream attention on their own.

- **`hq-9le` — P1, silent data loss.** `bd mol wisp gc --closed --force` reclaims **closed steps of
  an in-flight molecule**, deleting their dependency edges and resetting molecule progress,
  bypassing the live-work protection `gc --help` documents for the `--age` path (GH#4394). No
  `--mol` scope flag; `--exclude-type` does not cover it. **The elevated severity is deliberate,
  not routine CLI-ergonomics triage.** All three conditions hold at once: it destroys real state
  **silently** (the command reports success and a count, and the loss is only visible by re-running
  `bd mol current`); it is triggered by **routine housekeeping the vendor's own docs instruct you
  to run regularly**; and the victim is a **documented, sanctioned use case** (`phase: vapor`
  release/patrol runs — the shape `bd formula schema` and `/workflows/wisps` both nominate). It is
  reachable under entirely normal conditions — one unrelated closed wisp anywhere in the hive is
  enough — and is **not release-specific and not an edge case**; the release run is simply where it
  was caught.
- **`hq-qe9` — P2.** `bd mol squash <id>` on a molecule with **open** steps does not refuse or
  confirm: it deleted three open steps (including an unresolved human gate and the final
  one-way-door step) and auto-closed the root.

### A6 — consequence: nothing is warranted from either spike

**No implementation beads are warranted from `bh-bomrd`, and none should be filed.** Both spikes
returned NO-GO, so the epic's GO branch — a `/bh:replan` spike-verdict re-entry into an
implementation molecule — is not taken. Nothing above changes any decision in this ADR, and the
*"Explicitly not to be filed"* list stands as written; `bh-bomrd.2` reached *"per-attempt
instrumentation, wisp telemetry"* independently, from its own evidence, on a pipeline this ADR
never examined.

Build nothing, change nothing: `bh-trgcd.2` stands as merged, `release.py` and the release recipes
are not to be touched, and this addendum is the record that the narrower question was asked and
answered — so it is not re-opened from intuition the next time the wisps page is read.

Three **documentation-only** follow-ons are noted for a groom pass to absorb, each a
single-paragraph edit, none an implementation bead: fold A4's three corrections into Decision 2's
bullets; record in `OBSERVABILITY.md` (`:167`) and `work.py` (`:1870`) that the durable reasons the
self-check signal is not a bead write are the measured ones in A1, not the archive rule alone; and
carry A3's two-column gate table wherever Decision 5's gate-bead recommendation is cited.

---

## Addendum 2 — 2026-08-20 (`bh-yber2.3`): the guardrailed mitigations and the measured ROI

**Status of this addendum:** accepted · **Extends, does not supersede, anything above — including
the `bh-bomrd.3` addendum.** No decision is reversed. Decision 2's reopen bar gains a **fifth**
condition (B6), Decision 5 / A3 is **reinforced** with one measured caveat (B2), and one risk this
ADR and A2 had only argued abstractly is now recorded as **reproduced in the field** (B5).

After `bh-bomrd` closed, the operator raised two objections to its release-loop NO-GO that neither
`bh-gj0v9` nor `bh-bomrd` had tested: (1) the known defects could be handled as **guardrail-managed
risk** plus two **new design mitigations** — a real `bd gate create` gate bead instead of a
formula-materialised one, and selective replication of **measured facts** into a persistent bead's
metadata; and (2) the reconstruct-the-release-state agent loop the shipped verbs require was
claimed to cost real wall-clock and tokens (*"sometimes ~30s"*) against *"~3s"* for one structured
query — a claim **nobody had measured**. Molecule `bh-yber2` ran one spike on each.

| Spike | Question | Verdict |
|---|---|---|
| [`bh-yber2.1`](../spikes/bh-yber2.1-guardrailed-wisp-mechanism.md) | do the guardrail + the two new mitigations hold up mechanically? | **NO-GO** — on a *new*, more serious failure than either prior molecule found |
| [`bh-yber2.2`](../spikes/bh-yber2.2-agent-loop-cost-measurement.md) | what does the agent probe loop actually cost vs one structured query? | **direction confirmed, magnitude overstated** — 1.41× median, not ~10×, and it is not LLM cost |

Both point the same way, for independent reasons. Neither offsets the other, and the epic's GO
branch is not taken.

### B1 — mechanism: the gate that does not gate

The mitigation stack was: a **gateless** release wisp for the cheap mechanical steps, a **separate
persistent `bd gate create --type human --blocks <one-way-door step>`** as the sign-off, and
metadata replication of measured facts. Run live in a fresh scratch hive (bd 1.1.0 dev), three
runs plus three controls.

**The record half of the mitigation genuinely works.** A `bd gate create` gate placed over a wisp
step is a persistent, versioned, git-synced, GC-proof bead: `bd history` returns the full
open→closed transition with author, timestamp and sign-off reason where the *same command against
a wisp step* returns *"No history found"*; it never appears in `bd mol wisp list`; it is in
`bd export`; it survives `gc --closed --force`, and GC even rewrites its dangling reference to a
`[deleted:…]` **tombstone** — materially better than `bh-gj0v9.6`'s silent strip. **`bh-bomrd.1`'s
E9** (*"the releaser sign-off has no audit record"*) is closed locally, in `bd`, with no GitHub
dependency. That was the hypothesis, and it held.

**The enforcement half does not.** With that gate **open** on the one-way-door step and all three
predecessors closed, `bd mol current` reported the step **`[ready]`** and printed
`Start with: bd update … --claim`, and `bd ready --mol` listed it as startable — while `bd show` on
the very same step correctly listed the gate under `DEPENDS ON`. Reproduced in two independent
runs. Three controls in the same hive isolate the cause, and none of them is the query or the gate:

- the **same gate** correctly blocks a **persistent** bead (`bd ready` drops it);
- a **formula-materialised** gate correctly blocks the **identical** step (`[pending]`, and
  `Next ready:` routes to the gate);
- a plain `bd dep add` of an **ordinary persistent** blocker is ignored **identically**.

So the root cause is general: **molecule-scoped readiness honours only blockers that are themselves
inside the ephemeral set.** A non-ephemeral blocker of an ephemeral step is invisible to it,
whatever its type or provenance. (A bounded follow-up run during this decision pass pins that
scoping down further — the *unscoped* `bd ready --include-ephemeral` gets it right; see B6.)

**Why this is worse than the defect it was introduced to fix, not a lateral move.** E9 is a
*missing record*. This is a **false green** in front of an irreversible action: the tracking
surface does not omit the block, it affirmatively says the one-way door is clear and tells the
operator to walk through it, while the releaser sign-off is unresolved.
[`attested-green-adr.md`](attested-green-adr.md) exists because the 0.11.5 incident was caused by
a confident wrong sentence; this is that shape exactly. The mitigation therefore converts into a
strict trade rather than a win — **you buy the sign-off's audit trail with the sign-off's
enforcement**. The formula gate enforces and leaves no record; the real gate leaves a perfect
record and enforces nothing. There is no configuration in which a wisp step is both gated and
audited.

The accepted risk itself behaved exactly as the operator predicted and is **not** re-opened: a
full run walked to completion and *then* GC'd lost nothing (confirming `bh-bomrd.1` **E5**), and a
mid-flight `gc --closed --force` destroyed in-flight progress exactly as **E6** describes — the
guardrail is load-bearing, and it held. The NO-GO does not rest on it.

### B2 — salvage 1: `bd gate create` is a working, audited gate — Decision 5 is *reinforced*

Keep this finding separate from the "adopt wisp" question it was tested inside, because it is the
opposite of a negative result. Every property A3's table claims for `bd gate create` — persistent,
versioned, git-synced, never GC-eligible — was **re-verified here by direct measurement**, on a
harder case than A3 tested (a gate whose blocked bead is itself ephemeral and gets deleted out
from under it), and all four held, plus a tombstone A3 did not promise.

**Decision 5's recommendation — a human / out-of-band op that gates code work is a gate bead —
stands unqualified and is better evidenced than before.** What B1 disproves is the *combination*
with a wisp molecule, not the gate mechanism.

One caveat A3's table should now carry, and the only one:

> Those four properties **do not include being enforced**, once the bead being blocked is
> ephemeral. `bd gate create` gates a **persistent** bead correctly (measured). Over a wisp step it
> is a record only.

Decision 5's own scope has always been persistent beads, so this narrows nothing it claims. It is
written down so nobody re-derives "gate beads don't work" from B1.

### B3 — salvage 2: measured-fact replication is sound, and needs no wisp

The second mitigation — writing a **timestamped measurement** (`tag_sha`, the probe command run,
`remote_has_tag`, `measured_at`) rather than a bare `done` flag to a persistent bead's
`--metadata` at each checkpoint — is a plain `bd update --metadata`. Measured: it creates **no
node and no edge** (`bd dep tree` byte-identical before and after), each write **is** an archived
version, values **shallow-merge** so per-checkpoint keys accumulate, the whole map is exported,
and it survives every GC including the mid-flight one that destroys the molecule. It therefore
**cannot** reintroduce `bh-gj0v9.6`'s edge-stripping shape — confirmed live, not assumed, because
no edge is ever created. (The *gate*, by contrast, necessarily does create a persistent↔ephemeral
edge, and mildly does reintroduce it.)

**Critically, nothing in that result depends on a wisp existing.** The molecule contributed
nothing to it. If this pattern is the value, it is available **without adopting any part of the
wisp mechanism** — which is why it is recorded here as a standing, wisp-independent option rather
than dying with the NO-GO. Two rules if anyone takes it:

1. **One distinct key per checkpoint, never reuse a key.** `metadata` is projected into neither
   `bd history --json` nor `bd show --as-of` (both return `null` for it at every commit), so an
   overwritten value is **unrecoverable**. Distinct keys make the record effectively append-only.
2. **It stays a snapshot *beside* the measured verbs, never a substitute.** It narrows **E11** —
   a timestamped measurement is strictly more than an assertion — but does not close it: what is
   read back is *remembered*, not *re-measured*, and `remote_has_tag: false` will print forever
   regardless of what the remote now says.

**No bead is filed for it here** (spike-loop rule). It is a `/bh:replan` input if anyone wants it.

### B4 — ROI: the direction is real, the magnitude is not, and the cost is not the model

Measured with the harness's own reported `duration_ms` / `num_turns` / token / cost fields — not a
hand-rolled stopwatch — **n = 5 per condition, one scenario, one model, one host**. Both conditions
were asked the same question about the same genuine mid-flight release state (bumped, tagged
locally, not attested, nothing pushed).

| | median wall-clock | turns | output tokens | cost |
|---|---|---|---|---|
| **A** — probe loop over four `bh release` verbs | **12110 ms** (range 11621–32222) | 4 (one run: 7) | 615 | $0.0516 |
| **B** — one `bd mol current --json` | **8569 ms** (range 7575–9890) | **2, every run** | 374 | $0.0333 |
| **A ÷ B** | **1.41×** (worst A ÷ best B: 4.25×) | 2× | 1.64× | 1.55× |

**The direction holds on every metric and every pair** — no A run beat the slowest B run. **The
magnitude does not.** The *"~30s"* tail is real but atypical (one run in five: 32.2 s, 7 turns,
237k tokens); the *"~3s"* side **never happened at all** — B never came in under 7.6 s, because any
LLM-mediated answer costs ~7–8 s of model time whatever it is relaying. Only a *non-agentic* caller
— a script, a hook, a status line — collects the sub-2-second number the claim assumes.

**And the reproducible part of the gap is not reasoning cost.** With no LLM in the loop at all, the
four probes cost ~5.5 s of wall clock and the single query ~1.16 s — **~1.2–1.4 s of interpreter /
CLI startup per invocation**, with the real network measurements adding almost nothing. Subtracting
that leaves the *model-side* cost of the two conditions **indistinguishable at this N**. The
measured mean gap is `(4 − 1) × ~1.3 s of process startup`, not "LLM interpretation is expensive".
Note also that the "expensive" condition's output was **half the bytes** of the "cheap" one's JSON:
the cost is round-trips, not payload. What the structured query genuinely buys is **variance, not
mean** — 2 turns in 5/5 runs versus 4-or-7 at the agent's discretion, with nothing capping the
latter.

**The alternative that fell out of measuring it, named here and deliberately not filed.** Since the
gap is invocation count, it closes **without any second record**: a **`bh release status [--json]`**
verb running the *same four measurements* `preflight` / `pending` / `preview` / `recover` already
make, once, emitted structured. That collapses both terms at once — the extra process startups and
the extra turns — while staying **measured**: no asserted state, nothing that can silently disagree
with the remote, **E11 untouched**.

**This is a finding, not a bead.** Per the spike-loop rule this molecule files nothing; it is
recorded for whoever next replans this area to act on **if they choose to**. It is plain `bh` verb
work — no formula, no wisp, no new durable record — and is therefore unaffected by everything else
in this addendum.

### B5 — E11 stopped being hypothetical: the cheap answer named the *wrong* next action

The most consequential result across both spikes is **incidental to the cost measurement and more
important than it.**

All 5 probe-loop runs answered correctly: *attest the bumped tree, then await, then push* — which
is what the world state required. **All 5 structured-query runs answered wrong**, naming the
materialised `Gate: human` step, which has no `needs` edge and is therefore `ready` from
instantiation and wins the topological pick over `attest-bumped`. The reviewer confirmed this is
**not a test-setup artifact**: it is an inherited structural property of the base release formula,
present since `bh-bomrd.1`.

This ADR (A2) and `bh-bomrd.1` (**E11**) both argued *in the abstract* that **the shipped release
flow derives position from the world while a wisp asserts it**, and that adding a fifth,
unmeasured record beside four measured ones in front of a one-way door is a net loss. That argument
is now **reproduced, 5 for 5, unplanned, on a record this spike had hand-seeded to be otherwise
perfect and richer than the probe loop's** — it relayed the verified facts verbatim and precisely,
and still pointed at the wrong door.

Stated plainly, because it is the sentence to carry out of this molecule: **the 1.41× saving was
collected in exchange for a confidently wrong next action.** E11 is no longer a prediction. Note
that this is a *sibling* failure to B1's — B1 is a false green on *whether a step is blocked*, B5
is a false green on *which step is next* — and they arise from different mechanisms, which is why
neither is a mitigation for the other.

### B6 — `hq-mek` escalated, and Decision 2's reopen bar gains a fifth condition

**`hq-mek`** — filed via `bh escalate` from `bh-yber2.1`: *a persistent blocker of an ephemeral
wisp step is ignored by wisp-scoped readiness.* `bd gate create --type human --blocks <wisp-step>`
leaves the step reported `[ready]` by both `bd mol current` and `bd ready --mol` while the gate is
open, and a plain `bd dep add` persistent blocker behaves identically, while `bd show` correctly
lists both. It belongs at the severity **`hq-9le`** got (A5): it is **silent**, it is reachable
under **entirely normal use of two documented commands**, and it **fails toward "go" in front of an
irreversible operation**. No verdict here depends on it being fixed.

**Bounded follow-up during the decision pass — `hq-mek` is narrower than `bh-yber2.1` states, and
the correction is worth carrying upstream.** A ~5-minute check, **not a re-spike**, run in a fresh
scratch hive reproducing M4's exact shape (gateless release wisp, `bd gate create --type human`
on the one-way-door step, all predecessors closed). The spike only ever tried `bd mol current` and
`bd ready --mol`; two documented `bd ready` flags it never tried settle where the defect lives:

| invocation | one-way door, with the gate OPEN |
|---|---|
| `bd mol current <mol>` | **`[ready]`** + `Start with: bd update … --claim` — the M4 bug |
| `bd ready --mol <mol>` | **listed as ready** — the M4 bug |
| `bd ready --mol <mol> --include-ephemeral` | **listed as ready** — the M4 bug (the flag does not help here) |
| `bd ready --include-ephemeral` (**no `--mol`**) | **correctly withheld** — *"No ready work found (all issues have blocking dependencies)"* |
| `bd ready --include-ephemeral --explain` | **correct and specific**: *"Blocked (1 issues): … ← blocked by `yb-ovm`: Gate: releaser sign-off [open]"* |

Two controls confirm the withholding is a real block and not the flag simply excluding wisps:
resolving the gate makes `bd ready --include-ephemeral` list the same step immediately
(*"Ready: 1 issues with no active blockers"*), and re-blocking it with a plain `bd dep add` of an
**ordinary persistent non-gate** bead (M4c's shape) puts it back under `Blocked` with that bead
named — while `bd mol current` calls it `[ready]` in the same breath.

So: **the unscoped `GetReadyWork` path is blocker-aware for ephemeral steps and gets this right;
the molecule-scoped path (`--mol`, and `bd mol current`) does not.** `hq-mek` should be re-scoped
to the molecule-scoped readiness computation specifically — a materially smaller and more findable
defect than "wisp readiness is broken", and one with an available correct query. A second, minor
defect fell out: **`--explain` is silently ignored when `--mol` is present** (same output as
without it), which is why the wrong answer never announces itself.

**This does not move the verdict, and it is not a workaround worth relying on.** The correct query
is the one nobody reaches for: `bd mol current` is the molecule's *natural* tracking surface and
the one `bh-bomrd.1` **E3** singled out as a genuinely readable *"where did it stop"* report, and
it is the one that lies. Being safe only if you avoid the obvious command, on a surface with no
warning that it is unreliable, in front of an irreversible push, is the same false green B1
describes. The fifth reopen condition below stands as written, now with a precise target.

**Decision 2's reopen bar — the "Reopen this ADR only if" list above, as extended by A4 — takes a
fifth condition:**

> **Wisp-scoped readiness must honour non-ephemeral blockers.**

The bar now reads, in full: git sync; `bd ready` visibility; a GC that does not strip edges off
persistent beads; release-step completion becoming something `bh` **verifies** rather than accepts
on a close (A4); **and** wisp-scoped readiness honouring non-ephemeral blockers. **All of them, not
any one.** The fifth is not optional bookkeeping: without it, **no gate placed on a wisp step — of
any type, from any provenance — actually gates**, so any future proposal to put a wisp in front of
a one-way door is disqualified before its other merits are weighed.

**Note added 2026-08-20 (`bh-p66q6`) — the fifth condition is an obligation this repo's
passthrough-gating policy already carries, not a concession invented for wisp.** Framing only; it
changes nothing above.

**This repo already gates the raw `bd` surface for precisely this reason.** `bd_pass_enabled`
defaults **false** (`src/beadhive/config.py`, `bd_pass_enabled` / `git_pass_enabled`;
[`docs/PASSTHROUGH.md`](../PASSTHROUGH.md)) because — its own docstring — *"the raw bd surface is
gated so agents reach for the convention verbs (`bh work`, `bh plan`) instead of hand-driving
beads"*, and `otel.count_passthrough` (`src/beadhive/otel.py`) exists to **measure** the gate's hit
rate: *"the allowed/gated mix tracks how often a first-class verb is missing or undiscovered."*
Reaching for passthrough is not merely discouraged here — it is instrumented **as the signal that a
first-class verb is missing.**

**M4 is that signal firing.** An agent or operator who needs to know whether a molecule step or a
gate is actually ready has **no `bh`-native verb today**, so they reach past the gate for
`bd mol current` / `bd ready --mol` — the two surfaces this section has just measured as wrong
(`hq-mek`, as re-scoped by the bounded follow-up above, `hq-0w5`). That is the passthrough policy's
own predicted failure mode, landing on a case where the missing verb is not merely inconvenient but
**unsafe**. It is not a wisp-specific problem this investigation happened to trip over; wisp is only
where it surfaced.

**So the fifth condition is satisfiable two ways, and this record should not be read as naming only
the first:**

1. **An upstream `bd` fix** to the molecule-scoped readiness computation — the right long-term home,
   on a timeline this repo does not set.
2. **A `bh`-native verb wrapping correct molecule/gate readiness** — over the query this section has
   already confirmed correct (`bd ready --include-ephemeral --explain`, per `hq-0w5`), or a direct
   dependency-graph read via `bd show`, which was right in every control run above. This is work the
   passthrough-gating policy **already commits this repo to** — a missing convention verb is a thing
   to build whether or not wisp is ever adopted. **The operator has expressed a standing preference
   for this path.**

**What this note does not do.** It moves **no verdict**: wisp remains NO-GO, B7 is still the final
answer, and the fifth reopen condition stands exactly as written above — **unmet**. It commits to no
build and **no timeline**, and files **no bead**; a `bh`-native readiness verb is a `/bh:replan`
input like B3 and B4, not an obligation created here. All it corrects is the framing, so that when
such a verb is built it is understood as **completing an existing architectural commitment** rather
than a bespoke concession made to make wisp adoptable.

### B7 — consequence: nothing is warranted, nothing is filed, and this is the final answer

**No implementation beads are warranted from `bh-yber2`, and none are filed.** Both spikes returned
against adoption for independent reasons, so the epic's GO branch — a decision doc recommending a
scoped `/bh:replan` into an implementation molecule, with the guardrail policy promoted into
CLAUDE.md — is **not** taken. No product code changes. **CLAUDE.md is not amended**: the proposed
*"never run `bd mol wisp gc --closed` or `bd mol squash` while any wisp molecule is open"* line was
only ever needed to make wisp adoption survivable, and beadhive is not adopting wisp, so the rule
would guard a practice that does not exist. A6 stands as written, and so does the *"Explicitly not
to be filed"* list.

**The final answer, across three spike molecules** — `bh-gj0v9` (four spikes), `bh-bomrd` (two),
`bh-yber2` (two): **beadhive does not adopt `wisp` for any of the shapes investigated** — general
operational workflow, no-code-change work items, the dev check loop, the release loop, and now the
guardrailed-and-mitigated release loop with a real gate bead. Nine spikes, five shapes, three
independent failure classes (invisibility/locality, destructive GC, and assert-vs-measure), one
verdict. The next person who reads the `/workflows/wisps` page and has the idea should read this
addendum and **B5** before opening a bead; the question has been asked as thoroughly as it is worth
asking.

**What is actually alive from all of this investigation is narrower than the question, and none of
it needs formula or wisp:**

1. **Gate beads via `bd gate create`, for human / out-of-band ops that gate code work** — Decision
   5, unaffected and now better evidenced (B2). Already shipped `bd` behaviour; nothing to build.
2. **A possible future `bh release status [--json]`**, consolidating the four existing measurement
   verbs into one structured call (B4) — plain `bh` verb work, still measured, no new record. A
   finding for a future replan, **not filed here**.
3. **Optionally, measured-fact replication to a persistent bead's metadata** (B3) — one
   `bd update --metadata` per checkpoint, wisp-independent, safe, with the two rules stated.
   Also **not filed here**.

Build nothing, change nothing. This addendum is the record that the guardrailed version of the
question was asked, measured, and answered — so it is not re-opened from intuition.

---

## Addendum 3 — 2026-08-21 (`bh-a74aa.4`): the narrow release-loop safety bar clears

**Status of this addendum:** accepted · **Narrow GO to replan, not a general adoption reversal.**
Decision 2 remains NO-GO for `wisp` as a work-item or general operational-workflow substrate;
Decision 4's existing workflow rows remain unchanged. This addendum answers only whether a
release-shaped, host-local wisp may be used as a disposable execution projection when all durable
truth and all safety decisions pass through the three `bh` primitives delivered by `bh-a74aa`.

### C1 — decision: GO to a scoped implementation replan

The narrow guardrailed release-loop design now clears the five-condition reopen bar stated in A4
and B6. The result is **GO to `/bh:replan` an implementation molecule for the release-loop
tracking itself**, using the three primitives below. It is not approval to implement that loop in
this decision bead, and no implementation molecule is filed here.

The important qualification is that upstream wisp semantics did **not** change. Wisps remain
host-local, unversioned and absent from the persistent audit corpus. The narrow design clears the
bar by making the wisp a disposable projection rather than the canonical record: command-coupled
measured facts and sign-off live on persistent beads, while `bh` owns the only readiness and
destructive-cleanup surfaces the design may use. That substitution is specific to this static,
four-step release shape and does not satisfy Decision 2 for primary work items generally.

| Reopen condition | `bh-a74aa` evidence | Result for the narrow design |
|---|---|---|
| Git-synced durable state | `bh checkpoint run` appends a timestamped concrete measurement under a new key on a **persistent** control bead only after the real command exits 0; the persistent `bd gate create` sign-off is likewise versioned and synced | **Met by substitution.** Wisp rows still do not sync, but they carry no canonical release fact; completed checkpoints and sign-off do |
| Ready-surface visibility | `bh work readiness <molecule-id>` reports every member step through `dep tree` plus the unscoped `bd ready --include-ephemeral` paths, in human and JSON forms | **Met for a known release molecule.** The implementation molecule must own and retain the run id; bare `bd ready` remains the wrong interface |
| Cleanup cannot destroy live progress | the `bh bd` passthrough refuses `mol wisp gc --closed` and `mol squash` while **any** wisp molecule is non-closed hive-wide, names every open molecule, and fails closed when the safety query cannot be read | **Met at the bh boundary.** `BH_DEBUG=1` remains an explicit operator escape hatch; genuinely raw `bd` stays outside bh's control, as `PASSTHROUGH.md` already states |
| Completion is verified, not hand-asserted | `bh checkpoint run` runs the real command first, propagates any non-zero exit without metadata or step mutation, writes the persistent fact next, and only then closes the optional wisp step | **Met.** Key reuse is refused, the helper creates no dependency edge, and metadata failure cannot advance the wisp |
| Non-ephemeral blockers gate wisp steps | `bh work readiness` deliberately never calls `bd mol current` or `bd ready --mol`; its real-bd regression reproduces M4 and reports the one-way-door step blocked by the open persistent gate | **Met.** The false green in B1 is closed on the first-class bh surface |

The three implementations are separately reviewable and merged on the `bh-a74aa` integration
branch:

1. `bh-a74aa.1` — blocker-correct `bh work readiness`, including a real-bd M4 regression;
2. `bh-a74aa.2` — hive-wide destructive-cleanup guard with E6/E7 negative tests and safe-case
   regressions;
3. `bh-a74aa.3` — command → append-only persistent checkpoint → optional step-close ordering,
   with failure, key-reuse, concurrency and no-edge tests.

### C2 — what this GO does not claim

- **It does not make a wisp authoritative.** Read-time release truth still comes from the ledger,
  remote and package index. A checkpoint is a durable, timestamped snapshot beside those probes,
  never a replacement for re-measurement. B5's confidently wrong `next_step` remains the reason
  the future loop must not call `bd mol current` or treat wisp state as world state.
- **It does not adopt formula/wisp for the developer loop, Guide runs, onboarding, no-code work,
  dispatch, or general primary work.** All prior NO-GOs stand.
- **It does not claim bare-command enforcement.** The safety layer follows bh's existing
  passthrough model: first-class verbs are the supported path, the raw substrate remains an
  operator escape. The implementation plan must not introduce a direct raw-`bd` cleanup or
  molecule-readiness path.
- **It does not establish product ROI.** `bh-yber2.2` measured only a 1.41× median wall-clock
  difference at n=5 per condition, mostly attributable to process startup, and the cheaper query
  returned the wrong next action. Safety eligibility is now established; whether the tracking
  earns its maintenance cost remains an implementation-planning decision. The measured
  `bh release status [--json]` alternative remains in scope for that comparison.

### C3 — required next step, deliberately not performed here

Re-enter planning with `/bh:replan` and produce a fresh, scoped implementation molecule for the
release-loop tracker. That plan must use `bh work readiness` for all molecule/gate decisions,
route every real release command through `bh checkpoint run` with one never-reused key and a
timestamped concrete fact, keep the real sign-off as a persistent `bd gate create` gate, and
permit destructive cleanup only through the guarded `bh bd` path after all wisp molecules are
closed. It must also compare the resulting maintenance cost with the one-invocation, still-measured
`bh release status [--json]` alternative before committing to the wisp layer.

This decision bead files no implementation work. Its output is this bounded permission to replan.
