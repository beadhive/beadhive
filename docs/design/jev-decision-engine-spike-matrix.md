# Jev decision engines — where System One judgments can replace agent-loop micro-decisions

**Status:** Proposal (design + spike matrix). No product code. **Date:** 2026-09-19.
**Feeds:** one spike molecule per matrix row (planner spike loop), then a `jev` optional plugin
that activates every point whose spike verdict is **GO**, and only when the plugin is enabled and
a valid `TYPESAFE_API_KEY` is present.

> **What this is.** An inventory of the repeated *micro-decisions* AGF makes while a bead moves
> through `bh work`. Today an LLM agent turn or an entire agent seat makes many of them. Some
> could instead be a TypeSafe **System One** call to Jev, which returns typed Choice/Score/Noul
> answers with calibrated probabilities. This doc ranks them by expected impact on **total cost**.
> For each point it says which TypeSafe primitive fits and why, and how a spike would settle
> GO/NO-GO with measured evidence.
>
> **What this is not.** A claim that Jev replaces the implementing, reviewing, or planning
> *generation* work. Jev does not write code, prose, or explanations. It makes fast, narrow
> judgments over a supplied `state`. Every saving below comes from removing or shrinking an
> **agent turn or seat whose only job was to make one of those judgments**.

---

## 1. Sources read

| Source | What it settled for this doc |
|---|---|
| TypeSafe docs: [primitives](https://docs.typesafe.ai/primitives), [use-case map](https://docs.typesafe.ai/concepts/use-case-map), [patterns](https://docs.typesafe.ai/patterns), [Python SDK](https://docs.typesafe.ai/sdk/python), plus confidence, models, API, fan-out, composite scoring, confidence-routing, intent-routing, `jev-1.13` jaggedness, and the SDE-cascade, skill-suggestion, and parallel-questions cookbooks | Primitive semantics, pricing, limits, failure modes, composition patterns |
| [`tumf/jev-cli`](https://github.com/tumf/jev-cli) @ `980cb98` (v0.6.2) | CLI + stdio MCP wrapper. Fit and limits in §4.3 |
| `typesafe-sdk` on PyPI (0.7.0) | `requires_python >=3.10`, deps `httpx2`, `pydantic`, `tenacity` (built-in retry/backoff) |
| bh plugin skills: `work`, `developer`, `reviewer`, `merger`, `dispatcher` (+ `scheduling.md`, `collapsed-mode.md`), `planner`, `triage`, `retro` | Where AGF currently asks an agent to decide |
| `src/beadhive/`: `complexity.py`, `work_next.py`, `localloop.py`, `seatrun.py`, `schedule.py`, `triage.py`, `plan.py`, `work_submission.py`, `kernel/plugins/*`, `kernel/lifecycle/contracts.py` | Existing seams, closed vocabularies, and the purity boundary |
| [`bh-yber2.2`](../spikes/bh-yber2.2-agent-loop-cost-measurement.md) | **Measured** agent-loop cost baseline reused in §3 |
| [`bh-gj0v9.1`](../spikes/bh-gj0v9.1-formula-vs-python-hybrid.md), [`bh-gj0v9.3`](../spikes/bh-gj0v9.3-guide-vs-formula-boundary.md), [`bh-bomrd.2`](../spikes/bh-bomrd.2-dev-loop-wisp-fit.md) | Formula/wisp is NO-GO, so decision seams must not depend on formulas |
| [`bh-gj0v9.2`](../spikes/bh-gj0v9.2-audit-trail-feedback-signal.md) | Outcome-learned routing is NO-GO. Planner labels are not ground truth (§6.9, §7.1) |
| [`loop-ownership-and-execution-memory-adr.md`](loop-ownership-and-execution-memory-adr.md), [`hooks-as-functionality-adr.md`](hooks-as-functionality-adr.md), [`plugin-kernel-v1-adr.md`](plugin-kernel-v1-adr.md) / [`PLUGIN-AUTHORING.md`](../PLUGIN-AUTHORING.md) | Constraints the integration must respect (§4) |

---

## 2. The model in one screen

| Primitive | Answers | Returns | Use it in AGF when… |
|---|---|---|---|
| **Choice** | Which one of these options? | `choice`, `probabilities`, `confidence` | The answer selects **one code path** from a closed set: route a bounce, pick a disposition, name a failure cause. Always include a `none/other` option when the set may not cover the input |
| **Score** | Which level on an ordered scale? | `score` (can fall between levels), `probabilities`, `confidence` | The answer is a **position on an ordered rubric** we can threshold: complexity tier, review depth, priority, severity. Level text must describe concrete situations |
| **Noul** | Is this statement true? | `noul` ∈ [0,1], no separate confidence | A **clean yes/no condition** where several may hold at once (one Noul per label): "criterion *i* is met", "these two beads edit the same file", "this pair is a duplicate". 0.5 means undecided, not medium |

Operating facts that drive the design (from the docs, `jev-1.13`):

- **Price:** \$0.042 per 1M **input** tokens. **Output is free.** A 4k-token decision costs
  **≈\$0.00017**. A 30k-token decision costs ≈\$0.0013.
- **Limits:** 64k tokens per request, of which `state` plus the longest question may use 32k.
  1,200 req/min and 250k tok/s (currently adjusted dynamically).
- **Speculative fan-out:** questions in one request run in parallel against one `state`. Extra
  questions are nearly free. The parallel-questions cookbook measured batching as **~12× cheaper
  and ~10× faster** than one question per call. **Every point below batches all its questions
  into one request.**
- **Known limits of `jev-1.13`:** it reads instructions literally. It is weak at arithmetic,
  counting, and date comparison. Accuracy drops as irrelevant `state` grows, and adversarial
  content can move answers. Separate questions are not guaranteed to agree with each other, and
  it cannot generate text. Design consequence: **code keeps everything computable** (path
  intersections, counts, DAG reachability, timestamps) and **filters the `state`** before asking.
- **Pin the model:** thresholds are tuned against `jev-1.13.0`, not `jev-latest`. The alias
  moves on release and silently shifts calibrated thresholds.
- **Data egress:** `state` (bead text, diffs, logs) leaves the host. Jev is not trained on
  customer data, but ZDR is enterprise-only. Opt-in per hive is mandatory (§4.4).

---

## 3. Cost model — where savings exist and where they cannot

### 3.1 Price references

| Engine | Input \$/Mtok | Output \$/Mtok | Cache read \$/Mtok | 5-min cache write \$/Mtok |
|---|---|---|---|---|
| **Jev 1.13** (`jev-1.13.0`) | **0.042** | **0** | — | — |
| Claude Haiku 4.5 | 1.00 | 5.00 | 0.10 | 1.25 |
| Claude Sonnet 5 (AGF default seat tier) | 2.00 | 10.00 | 0.20 | 2.50 |
| Claude Opus 5 (escalation tier) | 5.00 | 25.00 | 0.50 | 6.25 |
| Claude Fable 5.1 (operator-invoked only) | 10.00 | 50.00 | 0.25 | 12.50 |

Claude rates are first-party list prices as of 2026-09. Jev's rate is from the TypeSafe models
page, reviewed 2026-09-17.

### 3.2 Per-decision cost by *decision locus*

The saving depends mostly on **where the decision is made today**, not on per-token price.
`bh-yber2.2` measured it: a cold headless seat processes a **~92.7k-token floor** before doing
anything. A warm, in-context decision pays only for its marginal turn.

| Decision locus (today) | Baseline cost per decision | Jev cost (4–8k-token state) | Ratio | Source |
|---|---|---|---|---|
| **Dedicated seat** (a whole agent launched to make the call, e.g. merger or blocked-cause triage) | Sonnet 5: **\$0.033–0.052** median warm, **\$0.125–0.167** cold; **8.6–12.1 s** median, **32 s** tail | \$0.00017–0.00034; ≈0.2–1 s | **~100–1000×** | `bh-yber2.2` E1 (N=5 per condition, one scenario — direction and magnitude only) |
| **Dedicated turn in a warm session** (dispatcher reads a gate or report and decides) | Sonnet 5 at 60k warm context: 60k×\$0.20/M + ~500 out×\$10/M ≈ **\$0.017**. Opus 5: ≈ **\$0.043** | \$0.00017–0.00034 | **~50–250×** | Arithmetic from §3.1. Spike S0 measures the real distribution |
| **In-context marginal thought** (an implementing agent decides mid-task; no extra turn) | ≈ output tokens only (~\$0.001–0.005) | \$0.00017–0.00034 | **~3–15×**, and usually *no* turn is saved | — |
| **Deterministic code today** (substring match, keyword scorer, Jaccard) | ≈ \$0 | \$0.00017 | **cost goes up** | Justified only by a quality gain that avoids downstream retries |

### 3.3 The Amdahl ceiling — measure it before claiming totals

A bead's dominant spend is the **implementation session**, which Jev cannot replace. Total
savings are therefore bounded by **the share of fleet spend that goes to decision-only turns and
seats**. Nobody has measured that share. **Spike S0 (§7) measures it first.** If
decision-locus spend is, say, 8% of the total, the best possible direct saving is about 8%,
however cheap Jev is. The rest of the value then has to come from **avoided downstream work**:
fewer bounces, retries, orphaned seats, and wrong-tier launches, plus less context loaded per
seat. The matrix scores both kinds.

### 3.4 Net-savings formula each spike must fill in

For a decision point with frequency *f* (decisions per 100 beads):

```text
net_saving_per_100_beads = f × [ coverage × (C_base − C_jev)
                                 − coverage × err_rate × C_wrong
                                 − (1 − coverage) × C_jev ]          # abstains still paid for Jev
                           + Δquality                               # avoided bounces/retries/seat-runs × their cost
```

- `coverage`: fraction auto-decided at the chosen confidence threshold. The rest abstains to the
  baseline engine.
- `err_rate`: error rate *at that coverage* against the golden set.
- `C_wrong`: cost of acting on a wrong answer. It is point-specific and often much larger than
  `C_base`; a wrong `blocks` edge stalls a molecule.
- `Δquality`: measured or projected downstream change, priced with S0's seat-run cost
  distribution.

---

## 4. Integration architecture (constraints first)

### 4.1 Non-negotiables inherited from standing decisions

1. **Formula-independent.** Formula/wisp was declined three times (`bh-gj0v9.1`, `bh-gj0v9.3`,
   `bh-bomrd.2`). Decision points attach at **bead state-transition guards** and **shell/Claude
   Code hooks**. The same verb is what a future formula `check` step would call, so nothing here
   waits on formulas.
2. **`work_next.decide()` stays pure and low-judgment (R4).** Jev never runs inside the 12-row
   table. Where a row consumes a semantic fact (such as the loop-breaker's failure signature),
   the **impure edge** computes that fact and passes it in as data, exactly as `bd ready` rows
   are passed in today.
3. **Zero execution-memory carve-out** (loop-ownership ADR Decision 2). Jev verdicts are not
   runtime state. When a verdict drives a transition, its evidence is recorded in the
   `bd set-state … --reason` text, which is already the durable audit trail. Everything else goes
   to OTEL span attributes, following the `bh-trgcd.2` precedent. No verdict cache, no sidecar
   DB.
4. **Hooks are verbs, not generated scripts** (hooks-as-functionality ADR). Hooks call a `bh`
   verb and honor its exit code. `bh` never writes a hook body.
5. **Plugins bind ports; domain code never queries a registry** (plugin-kernel v1). Each decision
   family is a narrow runtime-checkable `Protocol`, bound at bootstrap.
6. **Best-effort by default.** A missing key, a 4xx/5xx, a timeout, or low confidence must
   **never block** a transition. The point falls back to its baseline engine, and the fallback
   provenance is recorded (the same shape as `ComplexityResult.fallback`).

### 4.2 Shape

```text
            ┌──────────────────────── core (beadhive) ─────────────────────────┐
 caller ──▶ │  DecisionPort[P] (Protocol per family)                          │
 (work.py,  │     .decide(subject) -> Verdict[P]                              │
  localloop │                                                                 │
  triage,   │  BaselineEngine[P]  = today's behaviour                         │
  plan,     │     deterministic rule │ keyword scorer │ ABSTAIN("defer to agent")
  hooks)    │                                                                 │
            │  Verdict: outcome ∈ closed set P.outcomes │ ABSTAIN │ UNAVAILABLE │
            │           probabilities, confidence, engine, model="jev-1.13.0",│
            │           question_set_version, input_tokens, cost_usd,         │
            │           latency_ms, evidence{qid→raw answer}, fallback?       │
            └────────────────────────────────────▲───────────────────────────┘
                                                 │ bound at bootstrap iff
                                                 │ plugin enabled ∧ key valid ∧ point is GO
            ┌─────────────── optional plugin `jev` (beadhive[jev]) ──────────┴──┐
            │  beadhive_jev.client   — thin wrapper over typesafe_sdk          │
            │     ask(state, questions) (always one batched request)           │
            │     noul_gate(p, hi, lo) -> YES|NO|ABSTAIN                       │
            │     choice_or_abstain(ans, floor, per_option_floors)            │
            │     score_tier(ans, levels, floor)                              │
            │  beadhive_jev.questions — versioned question catalogue per point │
            │     (instructions/criteria text hashed → question_set_version)   │
            │  beadhive_jev.state     — per-point state builders + filters     │
            │  JevEngine[P] implements DecisionPort[P]                         │
            └──────────────────────────────────────────────────────────────────┘
```

**Modes per point** (`plugins.jev.points.<point-id>.mode`, default `off`):

| Mode | Engine that *acts* | Jev runs? | Purpose |
|---|---|---|---|
| `off` | baseline | no | default |
| `shadow` | baseline | yes; both verdicts logged | **benchmarking** (every spike runs here first) |
| `advise` | the agent, which receives Jev's verdict as a one-line hint | yes | cuts agent turns while keeping the agent's judgment (the skill-suggestion cookbook pattern) |
| `act` | code acts on a Jev verdict above threshold; `ABSTAIN` falls to the baseline | yes | removes the turn or seat entirely |

A point may be set to `advise` or `act` only if its spike verdict is **GO**. The plugin carries
a static `GO_POINTS` table, updated only when a spike's decision bead closes.

**The verb for hooks and formula checks:**

```sh
bh decide <point-id> --bead <id> [--input @file|-] [--json]
# exit 0 = proceed / decided   1 = hold (do not transition)
#      2 = route (outcome in JSON on stdout)   3 = could not decide (abstain/unavailable → use baseline)
```

Exit 3 deliberately matches `release.py`'s `3 = COULD NOT MEASURE`. Callers include:
Claude Code `Stop`/`SubagentStop`/`PreToolUse` hooks in the bh plugin, lefthook jobs, the `local`
runtime's impure edge, and, if formulas are ever adopted, a formula `check` step.

### 4.3 SDK vs `jev-cli` vs `jev-mcp`

| Use | Client | Why |
|---|---|---|
| Every in-process decision port (`work.py`, `localloop.py`, `triage.py`, `plan.py`, `bh decide`) | **`typesafe-sdk` via our wrapper** | Python ≥3.10 fits bh's ≥3.11 floor. Typed answers. Async client for the loop. Built-in retry (`tenacity`) honors `retry-after`. No subprocess: `bh-yber2.2` E4 measured **1.2–1.4 s of CLI startup per call**, which is several times Jev's own latency |
| Spike prototyping (question wording, golden-set runs from a shell) | **`jev-cli`** (`jev run request.json`) | Fastest way to iterate on question JSON with no product code. Fits the spike bar |
| Interactive seats that decide in-context (planner, dispatcher, human operator) | **`jev-mcp`**, optional | Lets an agent *delegate* a narrow judgment instead of reasoning it out. Useful only in `advise`-style flows |
| Production hooks | **not** `jev-cli`: hooks call `bh decide`, which uses the SDK | `jev-cli` is **unofficial and alpha** (v0.6.2, first archived change 2026-09-18). It requires **Python ≥3.13**, has **no retry** (a 429 exits with code 4), and uses a 60 s stdlib `urllib` timeout. It is fine as a tool but not as a dependency of a safety-relevant transition |

Packaging: `beadhive[jev]` extra (`typesafe-sdk>=0.7,<0.8`). `httpx2` is a new transitive
package alongside bh's existing `httpx`. It is a separate distribution with no import conflict,
but the dependency-taxonomy ADR requires it to be recorded.

### 4.4 Plugin manifest sketch (activation = enabled ∧ valid key ∧ GO)

```json
{
  "manifest_version": 1, "plugin_id": "jev", "plugin_version": "0.1.0",
  "capabilities": {"provides": [
    {"id": "decision.complexity", "api_version": 1},
    {"id": "decision.merge-outcome", "api_version": 1},
    {"id": "decision.check-failure", "api_version": 1}
  ]},
  "configuration": {"namespace": "plugins.jev", "schema_artifact": "urn:beadhive:wire-schema:plugin-config:jev:1"},
  "lifecycle": {"subscriptions": [
    {"event": "host.readiness", "id": "jev.probe-key", "criticality": "best-effort",
     "idempotency": "not-applicable", "order": 100, "timeout_seconds": 10,
     "retry": {"backoff_seconds": 0, "max_attempts": 1}, "compensation": {"action_id": null, "mode": "none"}}
  ]},
  "security": {
    "credentials": [{"id": "typesafe-api", "purpose": "System One decisions", "required": true,
                     "sources": [{"kind": "environment-variable", "name": "TYPESAFE_API_KEY"}]}],
    "executables": [],
    "permissions": [{"id": "network.egress.typesafe", "purpose": "send decision state to api.typesafe.ai", "required": true}]
  }
}
```

`jev.probe-key` sends one minimal request, equivalent to `jev auth test`. On failure, every port
stays bound to its baseline, and `bh doctor` reports why. Config also carries
`plugins.jev.egress: {diffs: bool, logs: bool}` so a hive can allow bead text while withholding
code.

---

## 5. Priority matrix

**Impact score** = cost impact (0–5) + quality/autonomy impact (0–3) − risk (0–3) + seam
readiness (0–2). Rows are sorted by that score. **Sequence** is the recommended order for filing
and running spikes. It differs from rank because S0 and the tracer bullet de-risk everything
after them.

| Rank | ID | Decision point (AGF transition) | Today's engine / locus | Freq. | Primitive(s) | Cost | Qual | Risk | Seam | **Score** | Seq. |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **J-MRG** | Merge outcome: does the merge *action* need a merger seat at all; route a refused/failed merge | Unattended `merge` → dedicated **merger seat** (LLM) | every bead + every molecule | **Choice** (failure route) | 5 | 1 | 0 | 2 | **8** | 3 |
| 2 | **J-CHK** | Validation / test failure triage (`bh work check`, merge re-validation red, pre-land) | Developer agent reads full logs; infra flakes burn whole turns | every red check | **Choice** per failure block + **Noul** gates | 4 | 2 | 1 | 2 | **7** | 4 |
| 3 | **J-SEAT** | Seat outcome: blocked/incomplete cause → retry, escalate, replan, link blocker | `CAUSE_BLOCKED` = "judgment; do not retry" → a **human** reads the summary | every non-done seat run | **Choice** (cause) + **Noul** (claimed-done cross-check) | 4 | 3 | 1 | 1 | **7** | 5 |
| 4 | **J-BNC** | Changes-requested routing + resume tier (`review=changes-requested` → ?) | **Dedicated dispatcher turn** + default resume on the same tier | every bounce (~7% of reviews) | **Choice** (route) + **Score** (rework severity → tier) | 3 | 3 | 1 | 1 | **6** | 6 |
| 5 | **J-REV** | Review-depth routing / cascade (which reviewer, how much context) | **Dedicated reviewer seat** at a fixed tier with the whole packet | every review | **Score** (depth) + **Noul** per critical-section flag + per-criterion **Noul** | 4 | 2 | 2 | 1 | **5** | 8 |
| 6 | **J-SUB** | Submit readiness: does the change meet each acceptance criterion (`claim → submit`) | Developer self-judges "done"; submit checks only history + validation | every submit | **Noul** per criterion + **Noul** scope-creep | 2 | 3 | 1 | 1 | **5** | 7 |
| 7 | **J-STOP** | Premature stop: is the seat *really* finished, or should it continue (Stop/SubagentStop hook) | Human re-prompts; unattended seat idles until the wall-time cap | every seat stop | **Noul** set | 3 | 2 | 1 | 1 | **5** | 9 |
| 8 | **J-CTX** | Context/pack selection at seat launch (which skills, references, and files to load) | Agent loads by index; full skill roster in context | every seat launch | **Choice** over roster + **Noul** need-any; per-file relevance **Score** | 3 | 1 | 0 | 1 | **5** | 10 |
| 9 | **J-CPX** | Complexity tier (`bh plan check/file`, backfill) → model tier | Keyword scorer (`BifrostLocalClassifier`), deterministic | every planned bead | **Score** (4 ordered tiers) | 2 | 1 | 0 | **2** | **5** | **2** (tracer) |
| 10 | **J-PLN** | Plan lint: acceptance testability, overlapping slices, spike-shaped issues (`bh plan check`) | Structural checks only; semantic quality left to the planner agent | per plan | **Score** (testability) + **Noul** (overlap, GO/NO-GO shaped) | 1 | 3 | 0 | 1 | **5**\* | 16 |
| 11 | **J-COL** | Collapse vs fanout / batch cohesion (`work.dispatch.mode: auto`, `schedule`) | `size:` budget + planner labels; same-file contention is planner-declared only | per molecule | **Noul** pairwise (same files/component) + **Score** cohesion | 3 | 1 | 1 | 1 | **4** | 11 |
| 12 | **J-DUP** | Duplicate detection at intake (`find_dupes`, `bd find-duplicates --method ai`) | Jaccard (misses paraphrase) or LLM per pair | every intake item | **Choice** over shortlist + `none`, **Noul** gate | 2 | 1 | 0 | 1 | **4** | 12 |
| 13 | **J-INT** | Intake disposition, type, priority, hive (`accept / reject / reroute / promote`) | **Dedicated dispatcher turn** per report | every report | **Choice** ×3 + **Score** (priority) | 2 | 1 | 1 | 1 | **3** | 13 |
| 14 | **J-DEP** | Blocker criticality: which proposed blockers to link and as what edge | Agent/planner judgment; often unlinked or over-linked | per proposed blocker | **Choice** (edge type) + **Noul** (acceptance-depends) | 1 | 3 | 2 | 1 | **3** | 14 |
| 15 | **J-NXT** | What to work on next (tie-breaks inside `bh work ready` order) | Deterministic order; agent picks among ties | per dispatch pass | **Noul** conflict-with-in-flight + **Score** risk (composite) | 1 | 2 | 1 | 1 | **3** | 15 |
| 16 | **J-LOOP** | Loop-breaker: is this retry the *same* failure (stuck) or progress | Substring `_ACTION_SIGNATURES` over event text | per repeated action | **Noul** pairwise sameness + **Choice** over `REASONS` | 1 | 2 | 1 | 1 | **3** | 17 |
| 17 | **J-REF** | Refine plan: which digest each checkpoint commit squashes into | Agent authors `--plan` by hand | per noisy submit | **Choice** per commit over candidate digests | 1 | 1 | 1 | 1 | **2** | 18 |
| 18 | **J-RET** | Retro: label session segments (planning/implementing/diagnosing/fixing) | Agent labels ambiguous sessions by hand | offline, per retro | **Choice** per segment (map-reduce) | 0 | 1 | 0 | 1 | **2**† | **1** (enabler) |
| 19 | **J-ESC** | `bh escalate` routing at HQ (which hive / upstream / env / docs) | Director agent reads the inbox | per escalation | **Choice** | 1 | 1 | 0 | 0 | **2** | 19 |
| 20 | **J-RO** | Semantic fallback for read-only auto-approval (`approve-readonly.sh`) | Allowlist; unknown → human prompt | per unknown Bash call | **Noul** (deny-direction only) | 1 | 0 | **3** | 0 | **−2** | 20 |

\* J-PLN reaches 5 on quality, not cost. The planner skill says "a wrong decomposition wastes
every downstream implementation hour", and the matrix cannot price that directly. Among the
score-5 rows it sorts last because its cost score is the lowest.
† J-RET saves little itself. It ranks first in **sequence** because it builds the labelled
corpus and the decision-share measurement that S0 needs (§7).

---

## 6. Spike cards

Each card lists: **transition / formula analog** · **today** · **primitive and why** ·
**question sketch** · **state (and what code keeps)** · **gate policy** · **seam** ·
**baseline vs Jev cost** · **ground truth** · **GO bar**.

### 6.1 J-MRG — merge outcome router (rank 1)

- **Transition:** `approved → merged` (`bh work merge`, `finish`). In formula terms, a guard
  before the `merge` step plus the check after it.
- **Today:** `localloop.ROLE_FOR_ACTION["merge"] = "merger"`, so the unattended loop launches a
  **whole merger seat** for every merge. What that seat *executes* is deterministic:
  `bh work merge` holds the slot, verifies history, merges `--no-ff`, closes, and releases. The
  seat's only judgment is **on refusal or failure**. The merger skill lists the cases: noisy
  history, merge conflict, and combined-state red.
- **Primitive: Choice.** The routes are mutually exclusive and each maps to one code path, so
  Choice is correct: the TypeSafe docs recommend "the one whose answer your code can act on
  directly". Options:
  `refine_history` (noisy/non-conventional history → developer `refine`),
  `rebase_resume` (conflict or stale base → `review=changes-requested` + resume),
  `combined_red_resume` (merged clean, integration tip red with siblings → auto-bounce path),
  `infra_retry` (slot contention, dolt lock, network, docker permissions → retry with backoff),
  `escalate_human` (shared pushed branch needs a forward fix, or unrecognised),
  `other`.
- **Code first:** most refusals already carry a closed reason or exit code. The spike's first
  question is **how much of this needs a model at all**. Jev is only for free-text residue:
  conflict hunks and validation output.
- **State:** `refusal_reason`, `exit_code`, `stderr_tail` (filtered to the last error block),
  `conflicted_paths[]`, and `bead.title`. Usually under 3k tokens.
- **Gate:** `act` at confidence ≥ 0.8 for `infra_retry`, `refine_history`, and `rebase_resume`.
  `escalate_human` is always allowed. Anything below the floor abstains to escalate. Nothing is
  ever dropped (merger rule).
- **Seam:** new `MergeOutcomeRouter` port called from the `local` runtime's `_act` for `merge`.
  The successful path runs **no seat**.
- **Cost:** baseline is one merger seat per merge (≥ \$0.033–0.17, 8–30 s). Jev costs ≈ \$0.0001,
  and only on failures. **This is the largest direct saving in the matrix**, because the
  successful path's cost goes to about zero.
- **Ground truth:** replay every historical `bh work merge` refusal from bead history
  (`review` set-state reasons, merge-slot events) and hand-label the correct route. N ≥ 60.
- **GO bar:** ≥ 95% route accuracy at ≥ 80% coverage, and zero dropped work in replay.
  Measured merger-seat spend (S0) ≥ 3% of fleet spend. Otherwise downgrade to "deterministic
  router, no Jev", which is still a saving and can be filed as a non-Jev bead.

### 6.2 J-CHK — validation / test failure triage (rank 2)

- **Transition:** `check` red, `submit` validation red, merge re-validation red (conservative
  mode). This is the "check step" of any future formula.
- **Today:** the implementing agent ingests the whole log, often thousands of lines, and decides
  whether the failure is its fault. Infra failures (dolt sql-server lock, docker group, network,
  selective-CI closure) cost a full reasoning turn or a whole retry.
- **Primitive: one Choice per failure block, plus Nouls.** Code pre-parses failures into
  candidate blocks (pytest node ids, tracebacks, ruff/mypy lines). This is the "select instead
  of generate" pattern. For each block, a Choice picks the cause:
  `caused_by_this_change`, `pre_existing_on_base`, `flaky_or_timing`, `environment_infra`,
  `interaction_with_sibling`, `lint_format_only`, `other`. Only one cause is actionable per
  block, so Choice fits. **Nouls** handle the independent flags: "the failure message names a
  missing credential or permission", "the output shows the test was killed by a timeout".
- **Code keeps:** whether the traceback's files intersect the diff (set arithmetic, not Jev),
  whether the same test fails on the base commit (run it), and retry counts.
- **Routes:** `lint_format_only` → run the formatter, no LLM. `environment_infra` → backoff and
  retry once, then `bh escalate`. `flaky` → one rerun. `caused_by_this_change` → hand the
  developer **only that block**, which is a context-token saving on the resume turn.
  `interaction_with_sibling` → the combined-red path.
- **Gate:** `advise` first, with the verdict and the filtered block injected into the
  developer's next turn. `act` only for `lint_format_only` and `environment_infra` at ≥ 0.85.
- **Seam:** `CheckFailureTriager` port in `work_submission.impl_check`. The same port serves
  `bh decide check-failure` for lefthook and CI.
- **Cost:** baseline is one developer turn over a large log. At 20–60k extra input tokens on
  Sonnet 5 that is roughly \$0.04–0.12, and a full seat when an infra flake forces a retry. Jev is
  ≈ \$0.0002–0.0008 (N blocks batched).
- **Ground truth:** historical red `check` runs from OTEL self-check span attributes
  (`bh-trgcd.2`) and seat transcripts, hand-labelled. N ≥ 100 blocks.
- **GO bar:** `environment_infra` and `lint_format_only` precision ≥ 0.95 at their act
  thresholds. Mean resume-turn input tokens reduced ≥ 30% in shadow replay.

### 6.3 J-SEAT — seat outcome / blocked-cause router (rank 3)

- **Transition:** seat harvest (`LocalLoop._harvest` → `record_cause`).
- **Today:** `seatrun.classify_run` is deterministic over `RoleOutcome.status`. `BLOCKED` maps
  to `CAUSE_BLOCKED` ("judgment; do not retry"). A human or an up-chain agent must then read
  `summary`/`next_action`. `DONE` is trusted even when the summary says the tests were not run.
- **Primitive: Choice + Noul.** Choice over the blocked cause (mutually exclusive next owner):
  `needs_human_decision`, `spec_ambiguous_replan`, `missing_dependency` (→ J-DEP),
  `tool_or_bh_bug` (→ `bh escalate`), `environment_credentials`, `transient_retry`, `other`.
  Nouls cross-check a claimed completion. "The summary states that validation or tests were run
  and passed" (low on a `DONE` → hold before submit). "The summary says the work is incomplete
  or deferred."
- **State:** `{status, summary, next_action, bead.title, bead.acceptance}` (small).
- **Gate:** `act` for `transient_retry` and `tool_or_bh_bug` at ≥ 0.8. Others `advise` into the
  escalation record. The contradiction Noul (`DONE` but p(tests run) < 0.2) is `act → hold`.
- **Seam:** a `SeatOutcomeJudge` port consulted after `classify_run` at the impure edge. The
  verdict becomes the `record_cause` reason text, so `work_next` stays pure.
- **Cost:** baseline is human attention, or a dedicated dispatcher turn per blocked run. The
  quality win is **unattended recovery**: fewer stalled beads waiting on a human.
- **Ground truth:** historical `SeatRun` envelopes whose status was blocked, handoff, or
  incomplete, paired with what actually happened next in bead history. Hand-label; don't trust
  outcomes blindly.
- **GO bar:** ≥ 90% cause accuracy at ≥ 70% coverage. In a 2-week shadow on a live hive, zero
  `transient_retry` acts that should have been `needs_human_decision`.

### 6.4 J-BNC — changes-requested routing and resume tier (rank 4)

- **Transition:** `review=changes-requested → resume | replan | split | escalate`.
- **Today:** the dispatcher runs a turn per gate and relaunches `resume` on the **same** tier.
- **Primitive: Choice for the route** (`resume_fix_in_place`, `replan_spec_wrong`,
  `split_followup` (the ask exceeds the bead's scope), `rebase_only`, `refine_history_only`,
  `escalate_human`), because the routes exclude each other. **Score for rework severity**,
  because the answer is ordered and code maps it to a model tier:
  (0) cosmetic or naming only · (1) localized logic fix in touched files ·
  (2) new behaviour or tests required · (3) design is wrong; approach must change.
  Score, not Choice, because the levels are ordered and thresholding them is the point. Level 3
  also implies the `replan` route, which code checks for consistency and abstains on
  disagreement.
- **Guardrail:** do **not** learn tier from outcomes. `bh-gj0v9.2` showed that is backwards. The
  Score reads the reviewer's text for this bounce only.
- **Seam:** `BounceRouter` port at the dispatcher's watch-gates step (`bh work ready --gated`),
  and in the unattended loop's `resume` branch.
- **GO bar:** ≥ 90% route agreement with the golden set. The severity→tier policy must not raise
  second-bounce rate in shadow replay.

### 6.5 J-REV — review-depth routing (cascade, not replacement) (rank 5)

- **Transition:** `submitted → approved` (the reviewer seat). Human gates stay human.
- **Today:** a reviewer seat at a fixed tier reads the full review packet.
- **Primitive:** **Score** for review depth, where code maps each level to a reviewer tier and
  flags:
  (0) mechanical: rename, formatting, docs-only · (1) ordinary change within one component ·
  (2) touches a trust boundary, concurrency, persistence, or money paths ·
  (3) cross-cutting or architectural.
  Plus **one Noul per critical-section flag** (auth/credentials, error handling on a
  destructive path, parser/serialization, migration, concurrency). The flags are multi-label, so
  they are Nouls, not a Choice. Plus **one Noul per acceptance criterion**, shared with J-SUB.
- **This is the SDE-cascade shape.** A cheap judge decides how much expensive reasoning to
  spend. Low depth with all criteria covered at high p gets a Haiku/Sonnet reviewer with a
  **focused packet** (flagged hunks only). High depth or any flag gets an Opus reviewer. Jev
  never approves.
- **State hygiene:** Jev's 32k limit and context-rot behavior mean diffs must be pre-filtered.
  One request per hunk group, then an aggregate. That is a *legitimate* two-request dependency:
  the first answer selects the second request's state.
- **GO bar:** no increase in escaped defects (post-merge fix beads citing the reviewed bead) in
  a shadow comparison. Reviewer spend down ≥ 30%. If escaped-defect data is too sparse to
  measure, **NO-GO for `act`** and keep `advise` (packet focusing only).

### 6.6 J-SUB — submit readiness against acceptance criteria (rank 6)

- **Transition:** `in_progress → submitted` (`bh work submit`).
- **Primitive: one Noul per acceptance criterion**, plus a scope-creep Noul ("the diff changes
  behaviour not described by any acceptance criterion or the bead description"). Criteria are
  independent and several can fail at once. A Choice would force "the one unmet criterion" and
  mis-model the problem. Nouls are also absolute, so all of them can be low, as they should be.
- **Composition (code):** `ready = all(p_i ≥ τ_hi) and p_scope < τ_lo`. Any `p_i ≤ τ_lo` holds
  with that criterion named. Anything in between abstains.
- **State:** `{criterion_i, bead.description, diff_stat, filtered_hunks_for_i}`. Code selects the
  hunks by path and symbol mentions. Literal reading means each criterion's instruction must be
  exact.
- **Hook:** the developer seat's `Stop`/`SubagentStop` hook calls `bh decide submit-readiness`.
  Exit 1 feeds the named unmet criteria back as the continue reason. `bh work submit` can also
  call it before opening the gate, as a warning in `advise` mode.
- **Honest ceiling:** the measured review bounce rate is 7.1% (`bh-gj0v9.2`). Preventing a
  bounce saves a review plus a resume cycle, so this is a **quality-first** point with moderate
  cost impact.
- **GO bar:** catches ≥ 50% of historical bounces whose reason cites an unmet acceptance
  criterion, with a false-hold rate ≤ 5% on beads that were approved first time.

### 6.7 J-STOP — premature-stop detection (rank 7)

- **Transition:** a seat ending its turn. This is the "dispatch loop until the long-lived agent's
  content is ready" point.
- **Primitive:** a **Noul set** over the final assistant message and the recent tool log:
  "claims completion without evidence that validation ran", "asks a question the role skill
  says the seat must decide itself", "stops on a recoverable tool error". Plus facts computed in
  code (ready beads remain, the gate is still open).
- **Gate:** `act` blocks the stop with a reason, but **at most N continues per seat** (a hard
  cap in code). This prevents a Jev-driven infinite loop.
- **Seam:** Claude Code `Stop`/`SubagentStop` hook in the bh plugin → `bh decide seat-stop`.
- **GO bar:** ≥ 80% precision on "should have continued" in labelled transcripts (J-RET corpus),
  and a measured drop in human re-prompts.

### 6.8 J-CTX — context and pack selection at launch (rank 8)

- **Primitive:** follow the **skill-suggestion cookbook** exactly. Request 1: one **Choice** over
  the whole skill/reference roster (option text = descriptions) plus a **Noul** gate "this bead
  needs a specialised skill at all". Request 2: re-judge the top 3 with full text. Then inject a
  one-line `<skill_relevance>` hint. For files: **Score** relevance of repowise search hits to
  the bead (the reranking pattern).
- **Why it pays:** it runs on every seat launch. The saving is context tokens and wrong-skill
  loads. The cookbook measured wrong-skill loads falling from 16.8% to 7.3% on Haiku.
- **GO bar:** ≥ 20% fewer input tokens per seat at unchanged submit success in shadow A/B.

### 6.9 J-CPX — complexity tier (rank 9; **tracer bullet, sequence 2**)

- **Why first among the ports:** the seam already exists. `complexity.ComplexityClassifier` is a
  `@runtime_checkable Protocol`, and `plan.py` and `complexity_backfill.py` accept an injected
  classifier. The doc calls the keyword scorer a "best-effort compatibility bridge" with planned
  replacements "behind the classifier interface". It proves the plugin binding, key probe,
  fallback provenance, pinned model, and shadow logging at **zero behavioral risk**.
- **Primitive: Score.** The four tiers are ordered (`SIMPLE < MEDIUM < COMPLEX < REASONING`) and
  thresholded, which is Score's definition. Level text must describe concrete situations, for
  example `"mechanical edit fully specified by the description; no design choice"` …
  `"requires novel design or multi-step reasoning across components; ambiguity remains"`.
  `ComplexityResult.score` = `answer.score / 3` (already normalized 0..1). Confidence below the
  floor maps to `FallbackProvenance(MEDIUM, "jev low confidence")`. **UNKNOWN is not a Score
  level.** Map it through the confidence floor.
- **Guardrail:** `bh-gj0v9.2` §3. The golden set is **human-labelled**, not the existing
  `complexity:` labels. Those labels come from the scorer under test.
- **GO bar:** exact-tier agreement with the golden set ≥ the keyword scorer's + 15 pts, and
  within-one-tier agreement ≥ 95%.

### 6.10 J-COL — collapse vs fanout (rank 11)

- **Primitive:** **Noul per sibling pair**: "`a` and `b` will edit the same file or function",
  from descriptions, designs, and repowise-resolved paths. Plus a **Score** for cohesion.
  Code keeps the `size:` budget arithmetic, the batch caps, and the model-tier guards.
- **Why it pays:** every avoided fanout developer saves a ~92.7k-token seat floor, and
  same-file contention the planner missed becomes merge conflicts (→ J-MRG/J-BNC churn).
- **GO bar:** ≥ 0.8 precision on "same file" against realised diffs from landed molecules.

### 6.11 J-DUP — duplicate detection (rank 12)

- **Primitive:** code prefilters with Jaccard at a *low* threshold. Then one request: **Choice**
  over the shortlist of existing beads **plus `none`**, and a **Noul** "this report describes
  the same underlying work as an existing bead". This is the cookbook's Choice-picks /
  Noul-gates split, because Choice is relative and would always name *something*. It replaces
  `--method ai`'s per-pair LLM calls with one call.
- **GO bar:** recall ≥ mechanical + 25 pts at precision ≥ 0.9 on the labelled intake history.

### 6.12 J-INT — intake disposition (rank 13)

- **Primitive:** one fan-out request. **Choice** disposition (`accept/reject/reroute/promote`).
  **Choice** type (`bug/feature/task/chore`). **Choice** target hive, with options drawn from
  `managed_repos` descriptions plus `this_hive`. **Score** priority on P0–P4 levels written as
  concrete impact statements. The priority is a Score and not a Choice because it is ordered.
- **Gate:** `advise`. The dispatcher confirms. `act` only for `accept` with type and priority at
  high confidence.

### 6.13 J-DEP — blocker criticality (rank 14)

- **Primitive:** **Choice** over the edge type for each proposed (bead, candidate-blocker) pair:
  `blocks` (A cannot be completed or validated until B lands), `related` (discovered-from /
  relates-to), `unrelated`. Plus a **Noul** that restates the `blocks` condition literally: "At
  least one of `bead.acceptance` cannot be demonstrated without `candidate` being done." The
  Noul is the literal-reading guard. Only act on `blocks` when both agree.
- **Code keeps:** cycle detection, the ancestry check, and the rule that a new `blocks` edge on
  an in-flight molecule needs a human (it changes `bd ready`).
- **Gate:** `act` for `related` (low harm). `blocks` stays `advise`.
- **Why rank is low despite high quality:** `C_wrong` is large. A spurious `blocks` edge stalls a
  molecule.

### 6.14 J-NXT — what to work on next (rank 15)

- **Primitive:** **composite scoring**. Per ready bead: a **Noul** "conflicts with in-flight bead
  *k*" (pairwise, against claimed beads) and a **Score** of risk/uncertainty. Weights live in
  code. DAG unblocking value is **computed**, not asked.
- **Constraint:** only **tie-breaks** within `bh work ready`'s dependency order. It never
  reorders across it. R4 stands.

### 6.15 J-PLN — plan lint (rank 10; quality-first)

- **Primitive:** a **Score** of acceptance testability per issue
  (`["no observable outcome", "observable but unbounded", "concrete, checkable outcome"]`). A
  **Noul** per issue pair for scope overlap. A **Noul** per issue: "this issue is an unresolved
  feasibility question" (should be a spike, per the planner's spike loop).
- **Seam:** `bh plan check` warnings (`advise` only). The planner keeps authority.

### 6.16 J-LOOP, J-REF, J-RET, J-ESC, J-RO (ranks 16–20)

- **J-LOOP.** **Noul**: "attempt *n*'s failure is the same underlying failure as attempt *n−1*".
  Computed at the edge and passed into `work_next` as data. The table stays pure. It fixes the
  known under-count (`attempt_count` is a lower bound) in the safe direction only: it may
  escalate *earlier* on genuine repeats, never later.
- **J-REF.** **Choice** per checkpoint commit over the candidate digests, where code proposes
  digests from path clusters. The subject line stays generated by a human or LLM. Jev cannot
  generate.
- **J-RET.** **Choice** per transcript segment over the retro skill's activity set. This is
  map-reduce over `~/.claude/projects/*.jsonl`. **Sequence 1**, because it produces the
  decision-locus cost attribution for S0 and the labelled transcripts for J-STOP.
- **J-ESC.** **Choice** over hives and `{bh-core, bd-upstream, environment, docs/user-error}`.
  Low volume.
- **J-RO.** **Recommended NO-GO for allow-direction.** Jev's jaggedness list includes
  adversarial content, and an auto-*allow* on a shell command is a security boundary. If
  spiked, Jev may only **add** friction (flag a risky command the allowlist would pass), never
  remove it.

---

## 7. Spike molecules — structure and sequence

Per the planner's spike loop, filing is **two molecules, never speculative beads**. Each point is
filed as a spike bead (`type: task`, `tag:spike`) that produces
`docs/spikes/<bead-id>-<slug>.md` using the standard template (Question / Method / Evidence /
Verdict / Recommendation). Prototype code goes under `docs/spikes/artifacts/`, **not product
code**. One decision bead (`tag:decision`) per molecule depends on all of that molecule's spikes.
On **GO**: `/bh:replan` files the implementation molecule for the `jev` plugin and flips the
point into `GO_POINTS`. On **NO-GO**: record the reason in the ADR and move on.

Suggested molecules:

| Molecule | Spikes | Why grouped |
|---|---|---|
| **M0 — Foundations** | **S0** decision-locus spend share (from J-RET corpus + `SeatRun.cost_usd` + OTEL); **S1** golden-set + shadow harness (`jev-cli` driven, artifact code); **J-RET** | Every other GO bar needs S0's cost distribution and S1's harness |
| **M1 — Tracer** | **J-CPX** | Existing Protocol seam proves plugin binding, key probe, provenance, pinning |
| **M2 — Integration-plane routers** | **J-MRG, J-CHK, J-SEAT, J-BNC** | Highest direct savings. All are Choice routers over closed sets at the unattended loop's impure edge |
| **M3 — Gates and cascades** | **J-SUB, J-REV, J-STOP** | Share the per-criterion Noul question set and the hunk-filtering state builder |
| **M4 — Context and scheduling** | **J-CTX, J-COL, J-NXT** | Launch-time and schedule-time. Share repowise-backed relevance state |
| **M5 — Planning and intake** | **J-DUP, J-INT, J-DEP, J-PLN, J-ESC** | Planner/dispatcher-plane, mostly `advise` |
| **M6 — Safety review** | **J-LOOP, J-REF, J-RO** | Small or risky. J-RO is expected NO-GO and recorded so it is not re-litigated |

### 7.1 Shared method every spike follows

1. **Question design with `jev-cli`.** Write `request.json` per point and iterate the wording on
   10–20 hand-picked cases. Record the final question set and its hash.
2. **Golden set.** Label N cases by hand (N is on each card). Sources: `bd history --json`,
   `review` set-state reasons, gate events, `SeatRun` envelopes, OTEL self-check attributes, and
   retro transcripts. **Never use existing planner labels or outcomes as ground truth for
   judgments they were produced by** (`bh-gj0v9.2` §3).
3. **Measure:** accuracy and a coverage/error curve across thresholds; calibration (reliability
   diagram, 10 bins); Jev `usage.input_tokens` → \$; p50/p95 latency; the baseline's cost for
   the same cases (agent `usage` from transcripts, priced with cache rates).
4. **Fill in §3.4.** Report net savings per 100 beads, with S0's distribution for `C_base` and a
   point-specific `C_wrong`.
5. **Verdict** against the card's GO bar. State the limitation up front, as `bh-yber2.2` does:
   a small N is a direction, not a multiplier.

### 7.2 What the `jev` plugin implementation molecule contains (after the first GO)

1. `beadhive_jev` wrapper (client, gates, question catalogue, state builders), `beadhive[jev]`
   extra, and a manifest with the key probe.
2. The `DecisionPort` Protocol family + `Verdict` envelope in core, with baseline engines that
   reproduce today's behavior byte for byte.
3. `bh decide <point>` verb with the exit contract in §4.2, plus `bh doctor` reporting per-point
   mode, GO status, and fallback reasons.
4. One port binding per GO point, shipped in `shadow`. Promotion to `advise`/`act` is a config
   change, gated on the point being in `GO_POINTS`.
5. OTEL attributes per decision: point, engine, outcome, confidence, `jev.model`,
   `question_set_version`, input_tokens, cost_usd, latency_ms, fallback. No new durable store.

---

## 8. Open questions for the operator

1. **Egress policy.** Can diffs and logs go to `api.typesafe.ai` for this fleet, or only bead
   text? This decides whether J-REV, J-SUB, and J-CHK are spikeable as written or must run on
   metadata alone.
2. **Unattended vs supervised first.** Rank 1–4 savings materialise mostly under the `local`
   runtime (dedicated seats). If the fleet is still mostly supervised one-terminal sessions,
   J-CTX and J-STOP move up.
3. **Budget for labelling.** The GO bars assume roughly 50–150 hand-labelled cases per point.
   M0 can pre-label with a strong model for human confirmation, but confirmation is still human
   time.
