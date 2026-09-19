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
| [`bh-gj0v9.2`](../spikes/bh-gj0v9.2-audit-trail-feedback-signal.md) | Outcome-learned routing is NO-GO. Planner labels are not ground truth (J-CPX card, §7.2) |
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
  customer data, but ZDR is enterprise-only. **Operator decision (2026-09-19): diffs and logs
  may be sent.** The code lands on a public repo anyway, and TypeSafe is trusted as a
  reputable vendor. Two guards stay: egress remains an explicit per-hive opt-in (§4.4), and
  **every** outbound payload passes a local secret sanitizer first, because a log or
  transcript can leak what the public repo never contains. The sanitizer and its
  gateway-level counterpart get their own spikes (§4.5).

---

## 3. Cost model — where savings exist and where they cannot

**Units under subscriptions.** The fleet runs on Claude and Codex subscriptions, so the real
currency is **quota (tokens) and wall-clock**, not invoices. The dollar figures below are
list-price proxies that make token volumes comparable across engines. The savings case is
quota recouped by moving decisions to Jev.

### 3.1 Price references

| Engine | Input \$/Mtok | Output \$/Mtok | Cache read \$/Mtok | 5-min cache write \$/Mtok |
|---|---|---|---|---|
| **Jev 1.13** (`jev-1.13.0`) | **0.042** | **0** | — | — |
| Claude Haiku 4.5 | 1.00 | 5.00 | 0.10 | 1.25 |
| Claude Sonnet 5 (AGF default seat tier) | 2.00 | 10.00 | 0.20 | 2.50 |
| Claude Opus 5 (escalation tier) | 5.00 | 25.00 | 0.50 | 6.25 |
| Claude Fable 5.1 (reference only; not available to this fleet) | 10.00 | 50.00 | 0.25 | 12.50 |

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
- `err_rate`: error rate *at that coverage* against the consensus labels (§7.2).
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
   DB. (ADR Amendment 1 draws the line: telemetry and the §4.6 evaluation corpus may persist
   locally because the loop never reads them.)
4. **Hooks are verbs, not generated scripts** (hooks-as-functionality ADR). Hooks call a `bh`
   verb and honor its exit code. `bh` never writes a hook body.
5. **Plugins bind ports; domain code never queries a registry** (plugin-kernel v1). Each decision
   family is a narrow runtime-checkable `Protocol`, bound at bootstrap.
6. **Best-effort by default.** A missing key, a 4xx/5xx, a timeout, or low confidence must
   **never block** a transition. The point falls back to its baseline engine, and the fallback
   provenance is recorded (the same shape as `ComplexityResult.fallback`).
7. **Nothing collected leaves the host unsanitized** (§4.5). This is the design's one
   *fail-closed* rule: if the sanitizer errors or times out, the payload is not sent and the
   decision abstains to its baseline. It closes the egress, never the transition.

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
| `shadow` | baseline | yes; both verdicts recorded | **benchmarking** (every spike runs here first) and the **replay corpus** for future Jev versions (§4.6) |
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
| Spike prototyping (question wording, labelled-set runs from a shell) | **`jev-cli`** (`jev run request.json`) | Fastest way to iterate on question JSON with no product code. Fits the spike bar |
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
`plugins.jev.egress: {diffs: bool, logs: bool}`. Per the operator decision in §2, both default
to `true` for this fleet's hives once the plugin is enabled, **after** the §4.5 sanitizer. Until
spike S2 is GO, both are forced to `false`, and points run on bead text alone. A private hive
can keep them off permanently.

### 4.5 Outbound data safety — one sanitizer for every inference call

The operator has allowed diffs and logs to be sent. What we *collect*, though, is not the public
repo: failure output dumps environments, transcripts quote tokens a tool printed, and bead text
sometimes carries pasted config. The exposure is not specific to Jev. Every inference call that
sends collected data carries it:

- Jev decisions.
- Teacher labelling (§7.2), which sends history to Claude and Codex.
- The shadow corpus (§4.6), which writes that data to disk.
- Harness traffic, if it is ever routed through a managed gateway.

So this is a **core** concern, not a Jev plugin feature.

- **`OutboundSanitizer` port in core:** `sanitize(payload, policy) -> Sanitized(clean, findings)`.
  Every outbound state builder calls it: Jev, the teacher harness runner, and shadow-corpus
  writes. `findings` hold detector, type, and span counts, **never values**. They go to OTEL
  like every other decision attribute.
- **Typed, stable placeholders:** `<secret:github_token#1>`, `<secret:high_entropy#2>`. The
  structure survives, so a judgment such as "the log says a credential is missing" can still
  be made on sanitized text.
- **Fail-closed** as §4.1 rule 7 describes, and never on the transition path.
- **Two layers, spiked independently.** S2 is the source filter: mandatory, local, CPU-only.
  S3 is a gateway guardrail: an optional second layer that adds central control.

#### Spike S2 (`J-SEC-SRC`) — potential-secrets filter on collected data

- **Question:** can a **local, CPU-only** filter redact secrets from everything we collect
  (check and CI logs, diffs, session transcripts, bead text, env and config dumps)? Its recall
  must be high enough to permit egress, without over-redaction that breaks the judgments that
  consume the text.
- **Candidate layers, cheapest first.** The spike measures each layer alone and the stack:
  1. **Known-value matching.** Exact-match redaction of the secret values this host actually
     holds:
     - env vars whose names match credential patterns;
     - the sources `credentials.py` resolves;
     - `gh auth token`;
     - harness auth files;
     - `.env` files in the hive.

     Near-total recall for *our own* secrets at near-zero cost, but blind to everything else.
  2. **Pattern + entropy scanners run offline:** gitleaks, detect-secrets, and trufflehog with
     verification off (no network). These cover provider key formats, private-key blocks, JWTs,
     connection strings, and high-entropy strings in assignment context.
  3. **A local CPU classifier for the residue:** context-dependent cases that no pattern
     covers, such as a password in prose, internal hostnames, or customer data in a log.
     Candidates to benchmark include Presidio-style recognizers and a small token-classification
     model served on CPU (ONNX or similar).
- **Must not:** use Jev or any remote model to *find* secrets. That sends the secret.
- **False-positive traps** that must survive un-redacted, because over-redacting them breaks
  J-CHK and J-MRG: commit SHAs, bead ids, UUIDs, content hashes, dolt refs, base64 test
  fixtures, and dummy tokens under `tests/`.
- **Eval corpus:**
  - real history (transcripts, check logs), scanned locally;
  - **planted canaries**: fake secrets in every provider format injected into real logs;
  - locally generated synthetic secrets;
  - the trap set.

  The corpus and its labels never leave the host.
- **Measure:** recall per class (known-value canaries, provider formats, generic); the
  over-redaction rate on traps; **Jev agreement with and without sanitization** on the J-CHK
  and J-MRG sets, i.e. does redaction hurt the decision; CPU time per MB at p50 and p95.
- **GO bar:**
  - 100% recall on known-value canaries;
  - ≥ 99% recall on provider-format synthetics;
  - ≤ 1% over-redaction on traps;
  - no measurable drop in Jev agreement from redaction;
  - p95 ≤ 200 ms for a 32k-token payload on one core, which keeps it inside Jev's own latency
    envelope.

  A partial GO (layers 1–2, with layer 3 deferred) is an acceptable verdict.

#### Spike S3 (`J-SEC-GW`) — gateway guardrails and TypeSafe protocol support

- **Question:** should outbound inference route through a **Beadhive-managed gateway**
  (Bifrost, LiteLLM, or similar) that enforces secret guardrails centrally? And can such a
  gateway carry TypeSafe's protocol today?
- **What is known (2026-09-19, from `jev-cli` @ `980cb98`).** Jev is already served by two
  hosted gateways, each with its **own** protocol:
  - **Vercel AI Gateway** at `/v4/ai/evaluation-model`, with a translated shape: Noul becomes
    `boolean` with a `probability` field, and the model id moves into a header.
  - **OpenRouter** at `/api/alpha/decisions`, native shape, alpha.

  `jev-cli`'s `custom` provider targets any proxy that speaks native `/v1/systemone`. None of
  these is OpenAI-chat-compatible, so a generic OpenAI-compatible gateway can carry Jev only
  through a pass-through or custom-provider route. bh's routing config already names Bifrost
  and OpenAI-compatible endpoints for **chat** inference ([COMPLEXITY-ROUTING.md](../COMPLEXITY-ROUTING.md)).
- **Method.** For each candidate (Bifrost and LiteLLM, with Vercel and OpenRouter as
  references):
  1. **Protocol:** can it proxy native `/v1/systemone`? Does its guardrail hook see the JSON
     `state` body, or only chat `messages`?
  2. **Guardrails:** pre-call secret/PII detection with redact vs block, custom guardrail
     plugins, and audit logging that never logs values. The central question: can the gateway
     call **the S2 filter itself** as its guardrail, so there is one detector and not two?
  3. **Credential custody:** the gateway holds `TYPESAFE_API_KEY` and the model keys, so hosts
     do not.
  4. **Operational fit:** runs as a container or service beside the host daemon; the latency
     it adds; what happens when it is down (answer: abstain to baseline).
  5. **Reach beyond Jev:** teacher labelling and harness traffic (`claude`, `codex`) through
     the same gateway. Which harnesses accept a base-URL override under subscription auth?
- **GO bar:** a gateway that carries chat inference **and** TypeSafe traffic (natively or
  through a thin adapter we own), with a guardrail hook that runs the S2 filter, adding ≤ 50 ms
  at p95.
- **Likely verdict:** GO for chat traffic now, and "Jev pending gateway support". In that case
  S2 at the source is the only layer on Jev traffic. That is why **S2 is mandatory and S3 is
  optional**: an S3 NO-GO does not block the programme, but an S2 NO-GO blocks log and diff
  egress.

### 4.6 Shadow mode as a training and replay corpus

Shadow mode benchmarks each point first. It is also the durable corpus for re-evaluating a
point when TypeSafe ships a new Jev version.

- **Recorded per shadow decision:**
  - point id, `question_set_version`, and pinned model id;
  - the **sanitized** state (post-S2 only);
  - Jev's raw answers (full probability distributions, not just the argmax);
  - the baseline engine's verdict and which engine acted;
  - timestamps;
  - an **outcome link**: the bead id plus the later event that confirmed or contested the
    decision (the §7.2 tier H filter, applied automatically).
- **Where:** `~/.beadhive/jev/shadow/<hive>/<point>/<yyyy-mm>.jsonl`, the same pattern as the
  retro skill's run directories. Local, opt-in, with a per-hive retention setting. Never pushed
  to the hive remote, never written into beads.
- **Compatible with the loop-ownership ADR, per its
  [Amendment 1](loop-ownership-and-execution-memory-adr.md#amendment-1--the-boundary-governs-primary-operating-data-not-telemetry-or-evaluation-data)**
  (operator decision, 2026-09-19). Decision 2's zero carve-out governs **primary operating
  data**. This corpus is **experiment/evaluation data**: write-only from the loop's side, never
  authoritative about lifecycle facts, local, and sanitized. Its test: delete the corpus, and
  every live decision is unchanged. Results flow back only through reviewed config (a pin, a
  threshold). Anything that *reads* the corpus at runtime would be operating data and would
  need its own amendment.
- **Labels accrue.** Contested outcomes and Jev-vs-baseline disagreements go to the §7.2
  teacher tier. Each point's corpus becomes its growing regression set.
- **Model-version replay.** When `GET /v1/models` shows a new version (`jev-preview` moves
  first), a replay re-asks the corpus's stored states and questions against the candidate
  model. It then compares agreement, calibration, and coverage-at-threshold **per label tier**
  against the pinned version, and proposes re-tuned thresholds. During the spikes the replay is
  an artifact script; in the plugin it becomes `bh decide replay`. Moving the pin is a reviewed
  config change, never alias drift.
- **Question-set replay:** a reworded question is evaluated the same way, as a new
  `question_set_version` over stored states. Question iteration stops needing fresh traffic.

---

## 5. Priority matrix

The fleet runs **both** modes: many supervised one-terminal sessions, where the human or the
dispatcher agent makes the call in its own session, plus the unattended `local` runtime, which
launches dedicated seats. The same decision point can save a whole seat in one mode and a single
thought in the other. So cost impact is scored **per mode**:

- **S:** cost impact (0–5) in supervised sessions.
- **U:** cost impact (0–5) in the unattended runtime.
- **Floor** = min(S, U): the impact that happens **regardless of mode**.

**Score** = 0.6·S + 0.4·U + quality/autonomy (0–3) − risk (0–3) + seam readiness (0–2). The 0.6
weight on S reflects the operator's supervised-heavy usage. Ties break on **Floor**, then on
max(S, U). The top three rows have Floor ≥ 3, so the biggest savings that land in either mode
sit at the top. **Sequence** is the order to file and run spikes. It differs from rank because
the labelling foundations and the tracer bullet de-risk everything after them.

| Rank | ID | Decision point (AGF transition) | Today: supervised / unattended | Primitive(s) | S | U | Floor | Qual | Risk | Seam | **Score** | Seq. |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | **J-CHK** | Validation / test failure triage (`bh work check`, merge re-validation red, pre-land) | Both: the implementing agent reads full logs; infra flakes burn whole turns or retries | **Choice** per failure block + **Noul** gates | 4 | 4 | **4** | 2 | 1 | 2 | **7.0** | 3 |
| 2 | **J-BNC** | Changes-requested routing + resume tier (`review=changes-requested` → ?) | S: dispatcher turn / U: loop resumes on the same tier | **Choice** (route) + **Score** (rework severity → tier) | 3 | 3 | **3** | 3 | 1 | 1 | **6.0** | 4 |
| 3 | **J-STOP** | Premature stop: is the session *really* finished, or should it continue (Stop/SubagentStop hook) | S: **human re-prompts** / U: seat idles to the wall-time cap | **Noul** set | 4 | 3 | **3** | 2 | 1 | 1 | **5.6** | 7 |
| 4 | **J-SUB** | Submit readiness: does the change meet each acceptance criterion (`claim → submit`) | Both: developer self-judges "done"; submit checks only history + validation | **Noul** per criterion + **Noul** scope-creep | 3 | 2 | **2** | 3 | 1 | 1 | **5.6** | 8 |
| 5 | **J-MRG** | Merge outcome: does the merge *action* need a merger seat at all; route a refused/failed merge | S: dispatcher reads the refusal in context / U: **dedicated merger seat** per merge | **Choice** (failure route) | 1 | 5 | **1** | 1 | 0 | 2 | **5.6** | 5 |
| 6 | **J-SEAT** | Seat outcome: blocked/incomplete cause → retry, escalate, replan, link blocker | S: dispatcher reads a sub-agent's report / U: `CAUSE_BLOCKED` waits for a **human** | **Choice** (cause) + **Noul** (claimed-done cross-check) | 1 | 4 | **1** | 3 | 1 | 1 | **5.2** | 6 |
| 7 | **J-CTX** | Context/pack selection at session launch (which skills, references, and files to load) | Both: loads by index; full skill roster in context | **Choice** over roster + **Noul** need-any; per-file relevance **Score** | 3 | 3 | **3** | 1 | 0 | 1 | **5.0** | 9 |
| 8 | **J-CPX** | Complexity tier (`bh plan check/file`, backfill) → model tier | Both: keyword scorer (`BifrostLocalClassifier`), deterministic | **Score** (4 ordered tiers) | 2 | 2 | **2** | 1 | 0 | **2** | **5.0** | **2** (tracer) |
| 9 | **J-PLN** | Plan lint: acceptance testability, overlapping slices, spike-shaped issues (`bh plan check`) | S: the planner session judges it / U: n/a (planning is supervised) | **Score** (testability) + **Noul** (overlap, GO/NO-GO shaped) | 1 | 1 | **1** | 3 | 0 | 1 | **5.0**\* | 11 |
| 10 | **J-COL** | Collapse vs fanout / batch cohesion (`work.dispatch.mode: auto`, `schedule`) | Both: `size:` budget + planner labels; same-file contention is planner-declared only | **Noul** pairwise (same files/component) + **Score** cohesion | 3 | 3 | **3** | 1 | 1 | 1 | **4.0** | 12 |
| 11 | **J-DUP** | Duplicate detection at intake (`find_dupes`, `bd find-duplicates --method ai`) | Both: Jaccard (misses paraphrase) or LLM per pair | **Choice** over shortlist + `none`, **Noul** gate | 2 | 2 | **2** | 1 | 0 | 1 | **4.0** | 13 |
| 12 | **J-REV** | Review-depth routing / cascade (which reviewer, how much context) | S: **human** reviews (Jev only focuses the packet) / U: **dedicated reviewer seat** at a fixed tier | **Score** (depth) + **Noul** per critical-section flag + per-criterion **Noul** | 2 | 4 | **2** | 2 | 2 | 1 | **3.8** | 10 |
| 13 | **J-INT** | Intake disposition, type, priority, hive (`accept / reject / reroute / promote`) | Both: a dispatcher turn per report | **Choice** ×3 + **Score** (priority) | 2 | 2 | **2** | 1 | 1 | 1 | **3.0** | 14 |
| 14 | **J-DEP** | Blocker criticality: which proposed blockers to link and as what edge | Both: agent/planner judgment; often unlinked or over-linked | **Choice** (edge type) + **Noul** (acceptance-depends) | 1 | 1 | **1** | 3 | 2 | 1 | **3.0** | 15 |
| 15 | **J-NXT** | What to work on next (tie-breaks inside `bh work ready` order) | Both: deterministic order; agent picks among ties | **Noul** conflict-with-in-flight + **Score** risk (composite) | 1 | 1 | **1** | 2 | 1 | 1 | **3.0** | 16 |
| 16 | **J-LOOP** | Loop-breaker: is this retry the *same* failure (stuck) or progress | U only: substring `_ACTION_SIGNATURES` over event text | **Noul** pairwise sameness + **Choice** over `REASONS` | 0 | 1 | **0** | 2 | 1 | 1 | **2.4** | 17 |
| 17 | **J-REF** | Refine plan: which digest each checkpoint commit squashes into | Both: agent authors `--plan` by hand | **Choice** per commit over candidate digests | 1 | 1 | **1** | 1 | 1 | 1 | **2.0** | 18 |
| 18 | **J-RET** | Retro: label session segments (planning/implementing/diagnosing/fixing) | Offline: agent labels ambiguous sessions by hand | **Choice** per segment (map-reduce) | 0 | 0 | **0** | 1 | 0 | 1 | **2.0**† | **1** (enabler) |
| 19 | **J-ESC** | `bh escalate` routing at HQ (which hive / upstream / env / docs) | Both: director agent reads the inbox | **Choice** | 1 | 1 | **1** | 1 | 0 | 0 | **2.0** | 19 |
| 20 | **J-RO** | Semantic fallback for read-only auto-approval (`approve-readonly.sh`) | S only: allowlist; unknown → human prompt | **Noul** (deny-direction only) | 1 | 1 | **1** | 0 | **3** | 0 | **−2.0** | 20 |

**What moved, and why.**

- **J-MRG fell from 1 to 5.** Its huge saving (a whole merger seat per merge) exists only in the
  unattended runtime. In a supervised session, the dispatcher runs `bh work merge` inline, and
  the only judgment is reading a refusal it already has in context.
- **J-SEAT fell from 3 to 6** for the same reason.
- **J-REV fell from 5 to 12.** Supervised review is a human, and Jev only trims the packet.
- **J-STOP rose from 7 to 3.** Supervised sessions are where the human re-prompt tax lives.
- **J-CTX rose from 8 to 7.** It pays on every session launch in either mode.
- **J-CHK and J-BNC stay at the top.** Their savings do not depend on the mode.

\* J-PLN reaches 5.0 on quality, not cost. The planner skill says "a wrong decomposition wastes
every downstream implementation hour", and the matrix cannot price that directly.
† J-RET saves little itself. It ranks first in **sequence** because its output feeds the
labelling pipeline (§7.2) and the decision-share measurement S0 needs.

---

## 6. Spike cards

Each card lists: **transition / formula analog** · **today** · **primitive and why** ·
**question sketch** · **state (and what code keeps)** · **gate policy** · **seam** ·
**baseline vs Jev cost** · **ground truth** · **GO bar**.

### 6.1 J-CHK — validation / test failure triage (rank 1)

- **Mode reach:** both. Logs are read by whoever implements, human-supervised or not.
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
- **Labels (§7.2):** tier H comes from red `check` runs in developer transcripts and OTEL
  self-check attributes (`bh-trgcd.2`). What the agent did next is the revealed label: it edited
  the touched code, reran without changes, or ran the formatter. The label is confirmed when the
  next check went green. Tier T labels every block. Tier S fills infra and flaky classes, which
  history under-represents.
- **GO bar:** `environment_infra` and `lint_format_only` precision ≥ 0.95 at their act
  thresholds. Mean resume-turn input tokens reduced ≥ 30% in shadow replay.

### 6.2 J-BNC — changes-requested routing and resume tier (rank 2)

- **Mode reach:** both. A dispatcher turn per bounce in supervised fanout; the resume branch in the
  unattended loop.
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
- **GO bar:** ≥ 90% route agreement with the consensus labels. The severity→tier policy must not
  raise
  second-bounce rate in shadow replay.

### 6.3 J-STOP — premature-stop detection (rank 3)

- **Mode reach:** both, **supervised-led.** A Claude Code `Stop` hook fires in interactive sessions
  exactly as in headless seats. Every avoided "you're not done, keep going" is a human round-trip
  plus a warm-context turn.
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

### 6.4 J-SUB — submit readiness against acceptance criteria (rank 4)

- **Mode reach:** both. In supervised mode a prevented bounce also saves the human reviewer's time.
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

### 6.5 J-MRG — merge outcome router (rank 5)

- **Mode reach:** unattended-led. The dedicated merger seat exists only in the `local` runtime.
  Supervised savings are limited to refusal routing.
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
- **Labels (§7.2):** tier H comes from every historical `bh work merge` refusal in bead history
  (`review` set-state reasons, merge-slot events), labelled with the route actually taken and
  confirmed when the next merge landed. Tier S fills rare routes such as
  `combined_red_resume`.
- **GO bar:** ≥ 95% route accuracy at ≥ 80% coverage, and zero dropped work in replay.
  Measured merger-seat spend (S0) ≥ 3% of fleet spend. Otherwise downgrade to "deterministic
  router, no Jev", which is still a saving and can be filed as a non-Jev bead.

### 6.6 J-SEAT — seat outcome / blocked-cause router (rank 6)

- **Mode reach:** unattended-led. Supervised: the dispatcher reads a developer sub-agent's report in
  context.
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
- **Labels (§7.2):** tier H comes from historical `SeatRun` envelopes that were blocked, handed
  off, or incomplete, paired with what happened next in bead history. Tier T is required here,
  because "what happened next" is often a human's workaround rather than the right route.
- **GO bar:** ≥ 90% cause accuracy at ≥ 70% coverage. In a 2-week shadow on a live hive, zero
  `transient_retry` acts that should have been `needs_human_decision`.

### 6.7 J-CTX — context and pack selection at launch (rank 7)

- **Mode reach:** both. Every session and sub-agent launch loads context.
- **Primitive:** follow the **skill-suggestion cookbook** exactly. Request 1: one **Choice** over
  the whole skill/reference roster (option text = descriptions) plus a **Noul** gate "this bead
  needs a specialised skill at all". Request 2: re-judge the top 3 with full text. Then inject a
  one-line `<skill_relevance>` hint. For files: **Score** relevance of repowise search hits to
  the bead (the reranking pattern).
- **Why it pays:** it runs on every seat launch. The saving is context tokens and wrong-skill
  loads. The cookbook measured wrong-skill loads falling from 16.8% to 7.3% on Haiku.
- **GO bar:** ≥ 20% fewer input tokens per seat at unchanged submit success in shadow A/B.

### 6.8 J-CPX — complexity tier (rank 8; **tracer bullet, sequence 2**)

- **Mode reach:** both. Classification runs at plan time, and the tier routes seats in either mode.
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
- **Guardrail:** `bh-gj0v9.2` §3. The existing `complexity:` labels are **never** reference
  labels, because the scorer under test produced them. Labels come from the teacher (tier T),
  cross-checked against outcome signals: the tier a bead's seat actually needed (escalations,
  bounces citing depth). See §7.2.
- **GO bar:** exact-tier agreement with the consensus set ≥ the keyword scorer's + 15 pts, and
  within-one-tier agreement ≥ 95%.

### 6.9 J-PLN — plan lint (rank 9; quality-first)

- **Mode reach:** supervised. Planning is a human-interactive seat.
- **Primitive:** a **Score** of acceptance testability per issue
  (`["no observable outcome", "observable but unbounded", "concrete, checkable outcome"]`). A
  **Noul** per issue pair for scope overlap. A **Noul** per issue: "this issue is an unresolved
  feasibility question" (should be a spike, per the planner's spike loop).
- **Seam:** `bh plan check` warnings (`advise` only). The planner keeps authority.

### 6.10 J-COL — collapse vs fanout (rank 10)

- **Primitive:** **Noul per sibling pair**: "`a` and `b` will edit the same file or function",
  from descriptions, designs, and repowise-resolved paths. Plus a **Score** for cohesion.
  Code keeps the `size:` budget arithmetic, the batch caps, and the model-tier guards.
- **Why it pays:** every avoided fanout developer saves a ~92.7k-token seat floor, and
  same-file contention the planner missed becomes merge conflicts (→ J-MRG/J-BNC churn).
- **GO bar:** ≥ 0.8 precision on "same file" against realised diffs from landed molecules.

### 6.11 J-DUP — duplicate detection (rank 11)

- **Primitive:** code prefilters with Jaccard at a *low* threshold. Then one request: **Choice**
  over the shortlist of existing beads **plus `none`**, and a **Noul** "this report describes
  the same underlying work as an existing bead". This is the cookbook's Choice-picks /
  Noul-gates split, because Choice is relative and would always name *something*. It replaces
  `--method ai`'s per-pair LLM calls with one call.
- **GO bar:** recall ≥ mechanical + 25 pts at precision ≥ 0.9 on the labelled intake history.

### 6.12 J-REV — review-depth routing (cascade, not replacement) (rank 12)

- **Mode reach:** unattended-led. Supervised review is a human. Jev can still focus the packet
  (flagged hunks, unmet criteria) to shorten the walkthrough.
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

### 6.13 J-INT — intake disposition (rank 13)

- **Primitive:** one fan-out request. **Choice** disposition (`accept/reject/reroute/promote`).
  **Choice** type (`bug/feature/task/chore`). **Choice** target hive, with options drawn from
  `managed_repos` descriptions plus `this_hive`. **Score** priority on P0–P4 levels written as
  concrete impact statements. The priority is a Score and not a Choice because it is ordered.
- **Gate:** `advise`. The dispatcher confirms. `act` only for `accept` with type and priority at
  high confidence.

### 6.14 J-DEP — blocker criticality (rank 14)

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

### 6.15 J-NXT — what to work on next (rank 15)

- **Primitive:** **composite scoring**. Per ready bead: a **Noul** "conflicts with in-flight bead
  *k*" (pairwise, against claimed beads) and a **Score** of risk/uncertainty. Weights live in
  code. DAG unblocking value is **computed**, not asked.
- **Constraint:** only **tie-breaks** within `bh work ready`'s dependency order. It never
  reorders across it. R4 stands.

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
| **M-SEC — Outbound data safety** | **S2** source secret filter (mandatory); **S3** gateway guardrails + TypeSafe protocol support (optional) | Gates every egress of collected data, including teacher labelling. Runs first, alongside M0 |
| **M0 — Foundations** | **S0** decision-locus spend share (from J-RET corpus + `SeatRun.cost_usd` + OTEL); **S1** label pipeline (§7.2) + shadow harness (`jev-cli` driven, artifact code); **J-RET** | Every other GO bar needs S0's cost distribution, S1's labels, and S1's harness. S1's teacher and Jev passes over *real* history wait on S2. Local extraction and synthetic-only work can start at once |
| **M1 — Tracer** | **J-CPX** | Existing Protocol seam proves plugin binding, key probe, provenance, pinning |
| **M2 — Integration-plane routers** | **J-CHK, J-BNC, J-MRG, J-SEAT** | Highest direct savings; the first two pay in both modes. All are Choice routers over closed sets, at the loop's impure edge or the dispatcher's gate step |
| **M3 — Gates and cascades** | **J-STOP, J-SUB, J-REV** | Share the per-criterion Noul question set and the hunk-filtering state builder |
| **M4 — Context and scheduling** | **J-CTX, J-COL, J-NXT** | Launch-time and schedule-time. Share repowise-backed relevance state |
| **M5 — Planning and intake** | **J-DUP, J-INT, J-DEP, J-PLN, J-ESC** | Planner/dispatcher-plane, mostly `advise` |
| **M6 — Safety review** | **J-LOOP, J-REF, J-RO** | Small or risky. J-RO is expected NO-GO and recorded so it is not re-litigated |

### 7.1 Shared method every spike follows

1. **Question design with `jev-cli`.** Write `request.json` per point and iterate the wording on
   10–20 hand-picked cases. Record the final question set and its hash.
2. **Labels.** Build the point's labelled set with the pipeline in §7.2: historical, teacher,
   and synthetic tiers, with a human adjudicating only the disagreements. Split it into **dev**
   (question iteration) and **test** (frozen before the first iteration, never seen while
   wording questions).
3. **Measure (on test, per label tier):** accuracy and a coverage/error curve across thresholds;
   calibration (reliability
   diagram, 10 bins); Jev `usage.input_tokens` → \$; p50/p95 latency; the baseline's cost for
   the same cases (agent `usage` from transcripts, priced with cache rates).
4. **Fill in §3.4.** Report net savings per 100 beads, with S0's distribution for `C_base` and a
   point-specific `C_wrong`.
5. **Verdict** against the card's GO bar. State the limitation up front, as `bh-yber2.2` does:
   a small N is a direction, not a multiplier.

### 7.2 Labels without a hand-labelling budget

The operator confirmed that 50–150 hand-labelled cases per point is not affordable. The
replacement uses three machine sources and spends human time only where they disagree.

**Tier H — historical revealed choices ("assume what we did was good").** This hive's own
history is a large record of decisions that were made and then lived with. At the time of
writing it holds:

- 3,334 closed beads, including 1,056 closed work items (933 with acceptance criteria).
- 2,008 lifecycle event beads, including ~1.5k review events and ~680 mentioning
  changes-requested.
- 659 bead/molecule merges on `main` since June.
- 309 session transcripts (249 MB) under `~/.claude/projects`.

Each point has an extractor that turns a historical moment into `(state as it was, choice that
was made)`. For example: a bounce reason paired with what the dispatcher did next; a red check
paired with the developer's next action; an intake item paired with its disposition; a
proposed blocker paired with whether a dep was added. Extractors live in
`docs/spikes/artifacts/` as spike code, and use `bh work list/issue --json`, `bd history --json`,
`git log`, and transcript parsing.

"Assume it was good" is tightened by an **outcome filter**. A historical label is kept only when
the downstream record does not contradict it:

| Label status | Condition |
|---|---|
| **Kept** | The next event confirms the choice: a resume that was approved next time, a merge that stayed, a check that went green after the fix, an accepted intake item that closed as done |
| **Contested** (sent to tier T) | The same failure recurred, the bead bounced again for the same reason, the merge was reverted (3 reverts in history), or the bead was reopened |

What tier H does and does not measure:

- **Its target is the right one for this job.** It measures whether Jev agrees with the choices
  AGF's agents and humans made that held up. That is exactly what "replace the agent's
  micro-decision" needs.
- **It is not ground truth about counterfactuals.** This is imitation, not outcome learning,
  which `bh-gj0v9.2` §3 showed is backwards. A label that was never tested, such as a route
  nobody tried, is not negative evidence.
- **Rare classes are thin.** Infra failures, spurious `blocks` edges, and some blocked causes
  are under-represented. Tier S fills them.

**Tier T — teacher labels from high-reasoning models, across two vendors.** Every tier H case,
plus every case tier H cannot label, is labelled independently by **two teachers from different
vendors**:

- **Claude Opus 5** at `xhigh` effort.
- **Codex Sol** (`gpt-5.6-sol`), the default Codex teacher.

Each receives the same `state` Jev will see, plus a rubric compiled from the role skill text and
the card's option definitions. Each returns a label and a short rationale as structured output.
Two model families agreeing is much stronger evidence than one model agreeing with itself, and
this directly weakens the agent-agreement bias listed below.

- **Codex Astra** is the adjudicator, used only in **exceptional** cases. Those are cases where
  the two teachers disagree **and** the case is safety-relevant (J-RO, `blocks` edges in J-DEP)
  or sits at an `act`-mode threshold.
- **Codex Terra** may label **simple** classes, where the rubric is near-mechanical: J-RET
  segment labels, J-CHK `lint_format_only`, and J-DUP exact-restatement pairs. It may also run
  the cheap independent validation pass on tier S cases for those classes.
- **How it runs:** headless, through the harnesses bh already drives (`claude -p` and
  `codex exec`, per `deps.py`), on subscription quota. No per-case API billing, so no cost
  estimate is carried here. The quota spent is recouped once decisions move to Jev.
- **Every teacher input goes through the §4.5 sanitizer first.** Teacher labelling is outbound
  inference like any other.

Because the production baseline *is* an agent, the teachers are the right reference for "can
Jev stand in for the agent". Their `usage` envelopes also give the baseline-token half of §3.4
on the same cases.

- **Circularity guard:** teachers never see Jev's answer. Teacher rationales may inform
  *question wording* only through the dev split, never the test split.

**Tier S — synthetic cases generated to order.** A teacher (Opus 5 or Sol) writes realistic
cases **per class**, seeded with 3–5 real tier H exemplars of that class, with emphasis on:

- rare classes;
- boundary pairs, where one detail flips the label;
- adversarial content, such as a log line that argues for its own classification (a documented
  `jev-1.13` weak spot).

Every generated case carries its intended label by construction. It survives only if an
**independent** labelling pass reproduces that label. The pass runs in a fresh context with no
generation prompt, and **by the other vendor's model**, or by Terra for simple classes. Each
point then follows two rules:

- Tier S is **at most 40% of the test split**.
- Metrics are **always reported per tier**. Synthetic cases are usually cleaner than real
  history, so a pooled number would flatter Jev.

**Consensus and the human's remaining job.** A case's reference label, and its label strength,
come from agreement across tiers:

| Agreement | Treatment |
|---|---|
| H and both teachers agree, or S survives cross-vendor validation | **Consensus**: used as a reference label |
| The teachers disagree with each other or with H, and the case is ordinary | The two-teacher majority with H decides. Used, flagged as adjudicated, reported separately |
| A split on a safety-relevant or `act`-threshold case | **Astra** adjudicates. If Astra is uncertain, the case goes to the **human queue** |

The human queue is expected at **≈ 10–25 cases per point**, a skim-and-confirm pass rather than
labelling from scratch. It is the only human labelling the programme needs.

**Shadow mode keeps producing labels** (§4.6). Once a point ships in `shadow`, every production
decision where Jev and the acting engine disagree, or where the outcome link contests the
decision, goes through the same tier-T labelling. The labelled set grows from real traffic, and
the offline tiers matter less over time.

**Validity threats every spike must state.**

- **Agent-agreement bias:** tier T and the baseline are both agents, so a shared blind spot looks
  like correctness. Two vendors reduce this but do not remove it.
- **Survivorship in tier H:** only the decisions that were made are visible.
- **Distribution shift in tier S.**
- **Leakage** from the dev split into the question wording.

Per-tier reporting is the mitigation for all four.

### 7.3 What the `jev` plugin implementation molecule contains (after the first GO)

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

## 8. Operator decisions (2026-09-19) and what they changed

1. **Egress: diffs and logs may go to TypeSafe.** The code is headed for a public repo, and the
   vendor is trusted. J-REV, J-SUB, and J-CHK are spikeable as written. Egress stays an explicit
   per-hive opt-in, and log state builders still strip credentials (§2, §4.4).
2. **Supervised sessions are common.** Cost impact is now scored per mode, weighted 0.6
   supervised / 0.4 unattended, with mode-independent impact (Floor) as the tie-break. That
   keeps the savings that land in both modes at the top: J-CHK, J-BNC, J-STOP (§5).
3. **No budget for 50–150 hand labels per point.** Labels now come from historical revealed
   choices with an outcome filter, a high-reasoning teacher, and validated synthetic cases.
   Human time is limited to ≈ 10–25 adjudications per point (§7.2).
4. **Secret safety is its own programme.** A core `OutboundSanitizer` sits in front of **every**
   inference call that carries collected data. It is spiked as a local CPU source filter (S2,
   mandatory; blocks log and diff egress until GO) and as gateway-level guardrails, including
   which gateways can carry TypeSafe's protocol (S3, optional) (§4.5).
5. **Teachers are Claude and Codex, not Fable.** Opus 5 and Codex Sol label everything
   independently. Codex Astra adjudicates only exceptional splits, and Codex Terra may handle
   simple classes. Runs use subscription quota, so labelling cost is not estimated (§7.2).
6. **Shadow mode is also the replay corpus.** It records sanitized states, full answer
   distributions, and outcome links, so every new Jev version and every question rewording can
   be replayed against real history before a pin moves (§4.6).
7. **The corpus is compatible with the loop-ownership ADR.** Amendment 1 to that ADR, filed in
   this bead, limits Decision 2's zero carve-out to **primary operating data**. Telemetry and
   experiment/evaluation data kept for operator-side analysis may persist locally, provided the
   loop never reads them and results return only through reviewed config.
