# Jev as composition — shape criteria, candidate map, and three PRD families

> Status: **landscape survey** (2026-09-22). Documentation only; no product code, no verdict on
> any candidate. Companion to
> [`jev-decision-engine-spike-matrix.md`](jev-decision-engine-spike-matrix.md) (2026-09-19) and
> [`jev-adoption-tracks-proposal.md`](jev-adoption-tracks-proposal.md) (2026-09-22). It does not
> restate their rankings, primitives or GO bars, and does not re-litigate any of them.

## Why this document exists

The spike matrix and the adoption tracks both adopt Jev as a **judgment engine** — twenty
decision points where a System One call replaces one micro-decision, plus a `Judgment` envelope
and config-only validation gates. That is one half of the idea.

The other half is unclaimed here: **composition**. In the graph-driven agent design that
motivated this survey ([merijjeyn/jive](https://github.com/merijjeyn/jive)), the savings do not
come from each judgment being cheap. They come from the planner emitting **one declarative DAG
instead of N tool-call turns**, with judgment calls as one node type inside it and shell
execution as the other. The composition layer is what bounds the number of times a reasoning
model has to wake up at all.

This document does not propose adopting that design. It answers a narrower question that has to
be settled first: **what kinds of Beadhive features does composition actually fit**, so that
PRDs can be scoped against the conceptual architecture rather than against an analogy.

## 1. Seven shape criteria

A candidate is a good fit for jev-as-composition when it satisfies several of these. All seven
are derived from measured evidence already in this tree, not from the upstream design's claims.

| | Criterion | Why it matters | Evidence |
|---|---|---|---|
| **A** | Fan-out over a collection known only at runtime | `LoopSpec.range` variable substitution is documented and non-functional; `on_complete.for_each` is inert. The #1 gap | `bh-gj0v9.1` E7 |
| **B** | A retry / until loop of unknown length | "Precisely the construct the schema cannot express" | `bh-bomrd.2` E9 |
| **C** | Branch points not expressible as comparisons | If every branch is `==`, the answer is a script, not Jev. **The discriminator** | §2 below |
| **D** | Serial CLI invocations | ~1.2–1.4 s interpreter startup each; the entire measured mean gap was `(4 − 1) × startup` | `bh-yber2.2` E4 |
| **E** | A safe fallback direction | Abstain must land on today's behavior, or the confidently-wrong answer ships | `bh-yber2.2` E5 |
| **F** | Payload is metadata, not content | Paths and rows need no sanitizer; logs, diffs and transcripts are gated behind M-SEC | matrix §4.4, §4.5 |
| **G** | Recurrence | Frequency × saving is the whole ROI; measure the ceiling before claiming totals | matrix §3.3 |

**C is the discriminator, and it cuts both ways.** Criteria A, B and D are satisfied by a plain
graph runtime with no Jev in it at all. Jev earns its place only where a branch is a judgment.
Several candidates below want the *graph* and do not want Jev; naming that explicitly is the
main practical result of this survey.

### 1.1 Why the formula/wisp NO-GO is evidence *for* this, not against it

[`operational-workflow-substrate-adr.md`](operational-workflow-substrate-adr.md) declines beads'
`formula` / `wisp` as a workflow substrate. That verdict stands and nothing here reopens it. But
its reasoning is worth reading forward rather than only backward:

| Formula lacks (measured) | A runtime-evaluated graph has |
|---|---|
| Any execution primitive — no `action` / `script` / `command` field in any of 18 structs | Executable nodes; the entire point |
| Retry / loop-until-green — no `retry`, `rollback`, `compensate`, `undo`, `on_failure` | Bounded repetition, `until` evaluated post-iteration, `max` required, explicit `exhausted` outcome |
| Runtime fanout (`LoopSpec.range` broken, `for_each` inert, `expand` static) | Per-item expansion over a discovered collection, with a concurrency limit |
| Runtime-evaluated conditions — `condition` is a pour-time constant | Activation conditions evaluated once dependencies settle, against loop-carried state |
| Compound predicates — single-term grammar, `&&` / `\|\|` rejected outright | Comparisons, membership, existence, and boolean combinations |
| Branch-on-probe — `BranchRule` is fork-join, `Gate` is an async wait | Recovery branches; merge nodes with `allowFailedDependencies` |

The ADR's own diagnosis is that the blocker "is not expressiveness — it is **ordering**": every
runtime-dependent construct must be resolved before `cook`, so the formula is never the source of
the DAG, only a downstream copy. A runtime-evaluated graph is precisely the fix for an ordering
problem. `bh-gj0v9.1` recommendation 5 also refused to generalise its own NO-GO — condition
filtering with automatic dangling-edge repair *works*; it was worth nothing there because
membership was runtime-computed.

### 1.2 Three primitives that already shipped

Composition over write verbs needs idempotency and gate evaluation. Both exist, merged on the
`bh-a74aa` integration branch (ADR Addendum 3):

- **`bh checkpoint run`** — command → new-key persistent checkpoint → optional step-close,
  serialised by a host-wide `flock` keyed by hive, bead and checkpoint key across precheck,
  command, postcheck and metadata write. Command failure leaves metadata untouched; key reuse is
  refused; it creates no dependency edge. This is an idempotency-keyed, locked node executor.
- **`bh work readiness <molecule-id>`** — blocker-correct readiness that deliberately never calls
  `bd mol current`, with a real-bd regression reproducing the false-green.
- **Measured-fact replication** — `bd update --metadata` on a *persistent* bead: versioned per
  write, shallow-merged, exported, untouched by GC, and **needs no wisp**. One distinct key per
  measurement, never reused; a snapshot *beside* the measured verbs, never a substitute.

### 1.3 The failure mode every candidate inherits

`bh-yber2.2` E5 / ADR B5: the cheap structured query was faster **and named the wrong next
action**, because a materialised gate bead had no `needs` and won the topological pick over the
step the world actually required. The record printed `Next ready: just release — ONE-WAY DOOR`
while the sign-off was unresolved.

> A record that confidently says "clear" when it is not is precisely the `0.11.5`-incident
> failure shape.

Generalised: **a graph's loop-carried state is not world state.** Any node gating an irreversible
action re-measures. Criterion E exists to bound this.

## 2. The candidate map

Eleven candidates, grouped by the plane they sit in. Criteria letters per candidate are claims to
be tested, not findings.

### Validation boundary

**1. Attest-key selection** · A C E F G · *seam already cut*

Fan-out over the configured attest keys, a Noul per key, a hard partition check at the join.
`selective_validation.semantic_selection()` shells out to `work.attest.semantic.command` with
`--base` / `--head` and expects the `jevwrap/select/1` envelope back; the configured default
command is the literal string `jevwrap select`, with nothing behind it. Every error, timeout,
malformed answer, non-partition or empty selection falls through to the full route, and skipped
keys are marked "no proof carried". Payload is file paths, not contents — no sanitizer needed for
any hive. Moves **wall-clock**: the motivating docs-only change ran `just check-all` twice at 8+
minutes each, against 6.749 s for the `docs` key alone.

**2. Check-failure triage → retry** · A B C D G · *blocked on M-SEC*

The seat's inner loop. Extract failure blocks deterministically, classify each (root cause /
cascade / flake / infra), retry-until-green with a bound, hand the planner a ranked minimal
summary instead of the raw log. Moves **turns and tokens** — the largest single context dump in
the developer loop. Requires S2 layer 1 (known-value secret filter) first: check logs dump
environments and quote tokens that tools printed.

### Integration plane (the seat)

**3. Seat lifecycle envelope** · B C E · *needs a graph runtime*

Claim → provision → implement → check → submit is already a fixed DAG that the model re-derives
per bead from the role skill. A saved graph parameterised by bead id, with judgment only at the
branch points. Moves **turns and consistency**. Carries the highest §1.3 exposure of anything
here, so it wants the most guardrails and should not go first.

**4. Merge / landing router** · C E · *seam exists (`work.validate.merge`)*

J-MRG, conflict handling, the union tier. Low fan-out, essentially one judgment plus a branch.
**More judgment than composition** — belongs in the existing Track A/B work rather than in a
graph proposal.

### Control plane (the fleet)

**5. Fleet-wide probe / aggregate** · A D E G · *no Jev content*

N hives × M probes, each paying CLI startup. Pure foreach + merge with zero judgment in it. Moves
**wall-clock and turns**, and it is the cheapest item on this list to build. Worth naming
precisely *because* it is a strong composition fit and a zero Jev fit — the clearest single piece
of evidence that the graph and the judgment engine are separable investments.

**6. Dispatch: collapse-vs-fanout and complexity routing** · A C G · *bindable today*

`bd ready` rows → foreach → classify → assign. J-COL and J-CPX already sit here, and J-CPX is the
one decision point the adoption tracks say can bind through an existing injected `Protocol`
(`complexity.ComplexityClassifier`) with no frozen-file surgery. Moves **routing quality** — the
measured 24.4% of routable beads landing in `[0.30, 0.40]` with the MEDIUM/COMPLEX boundary at
0.35 dead centre — not turns.

### Planning plane

**7. Intake triage and dedup** · A C E F G · *unblocked, offline*

Foreach over intake rows, duplicate detection and disposition (J-DUP, J-INT). Batch, not a gate,
so there is no latency pressure. Bead titles and descriptions are the payload: a step up in
sensitivity from paths, well short of logs. Moves **operator time** — a recurring dimension that
does not appear in the usual wall-clock / token / turn list.

**8. Plan lint / molecule verification** · A C E · *`plan_check` already structured*

Foreach over a proposed molecule's beads → acceptance-criteria quality judgment → aggregate.
J-PLN, filed as quality-first rather than cost-first. Moves **plan quality**; near-zero cost
saving. A good early Jev candidate precisely because nothing breaks when it abstains.

### Cross-cutting

**9. Artifact refs + bounded previews** · D G · *mechanism validated*

Not a graph — the substrate every graph needs. Full results on disk, a bounded preview plus a
stable path into the model's context. §1.2's measured-fact replication validated the durable
half; `BH_TEST_REPORT_DIR` / `BH_VALIDATION_RESULT_PATH` are the existing "name a directory and
read what appears" precedent. Moves **context bytes**. Zero Jev content, and a prerequisite for
candidates 1–3 rather than a competitor to them.

**10. Read-only query graph** · A D E G · *no Jev content*

The generalisation of `bh-yber2.2` recommendation 2: one invocation, N measurements, structured
result, no asserted state. It is tracked by Family I (`bh-r5g1b`): `bh-r5g1b.1` is the measured
`bh release status` aggregate control, and `bh-r5g1b.4` compares a second aggregate with a general
graph on the fleet-wide probe. The earlier **4 turns → 2** result is one scenario (n=5 per
condition); its durable finding is lower turn variance, not a universal cost multiplier.

The concrete Beads workflow — ready now, upcoming blockers/layers, and parallel scheduling without
`bv` — has its own no-bv packet spike (`bh-ioz51`). That spike compares the current `bh`/`bd` read
sequence with one bounded context response and decides whether a purpose-built aggregate or
compact MCP resource is justified. Its evidence feeds the Family I graph decision, but it does not
inherit the earlier 4-to-2 result. Jev-based bead-prose selection is a separate J-CTX extension
(`bh-bgbiy`) and remains outside Family I.

**11. Taskground-style eval harness** · — · *measurement, not composition*

The only way to tell whether any of the above worked: held-out verifiers outside the workspace,
identical instructions across agent profiles, per-agent metrics derived from saved events.
`bh-yber2.2` is already a one-off instance and its recommendation 3 asks for exactly this
generalisation — ≥3 scenarios with warm context, to isolate the marginal cost its ~92k
system-prompt floor hides.

## 3. Three PRD families

Reading down the criteria columns, the candidates cluster into three families that want
*different* investments. That separation is the actionable result.

| Family | Candidates | Criteria | Wants | Moves |
|---|---|---|---|---|
| **I — fan-out and aggregate** | 5, 10, 9, and the enrich half of 1 | A + D | A graph runtime and the canonical operation catalog. **No Jev at all** | wall-clock, turns, context bytes |
| **II — bounded retry loops** | 2, 3 | B + C | The graph runtime *and* Jev; the loop's exit and branch conditions are judgments | turns, tokens |
| **III — selection and gating** | 1, 6, 7, 8, 4 | C dominant, A secondary | Jev with a thin graph around it — a fan-out and a validated join, not a general runtime | decision quality, wall-clock (1), operator time (7) |

Three consequences:

1. **Family I is an execution-plane capability; Family III is a policy capability.** They are
   independently sequenceable and neither blocks the other.
2. **The existing Jev programme is entirely Family III.** The spike matrix's twenty decision
   points, the `Judgment` envelope, and the config-only gates all live there. What is unclaimed
   is Family I.
3. **Family II is the intersection**, which is why it looks the most attractive and is the
   furthest out. Its PRD cannot be written honestly until one of the other two exists and M-SEC
   has a down-payment.

### 3.1 What each PRD is about

- **Family I** — "a graph contract over the canonical operation catalog." Barely mentions Jev.
  Its hard questions are node typing against the catalog, artifact refs, replay semantics for
  effectful verbs, and reconciliation with
  [`loop-ownership-and-execution-memory-adr.md`](loop-ownership-and-execution-memory-adr.md)
  Decision 2, whose execution-memory boundary is **zero** — a graph run record is execution
  memory, and Amendment 1's telemetry carve-out does not obviously cover it.
- **Family II** — bounded retry with judgment-driven exit. Gated on M-SEC and on Family I's
  runtime. Highest payoff, highest §1.3 exposure.
- **Family III** — largely M-JUDGE plus the tracks already drafted, extended with the fan-out and
  validated-join shape that candidates 1, 7 and 8 share.

## 4. What this document does not say

- It does not adopt the upstream graph design, or any part of it.
- It does not rank the candidates, set a GO bar, or file implementation work.
- It does not reopen the formula/wisp NO-GO, the spike matrix's rankings, or the adoption
  tracks' sequencing.
- It does not claim a measured saving for any candidate. Criterion G exists because the
  addressable ceiling must be measured before any total is claimed.
