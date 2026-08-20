# Spike `bh-yber2.2` — what does LLM-mediated release-state reconstruction cost, vs one query?

**Bead:** `bh-yber2.2` · **Seat:** `dev/dev1` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-yber2.3` — *"do the guardrailed mitigations and measured ROI change
the release-loop verdict?"*

> **READ THIS FIRST — the limitation, not buried at the bottom.** This is a **small-N,
> single-scenario, single-machine** measurement: **N = 5 dispatches per condition, 10 total**,
> one release scenario, one model (`claude-sonnet-5`), one host, one network. It is **not** a
> benchmark and yields **no universal multiplier**. What it establishes is the **direction** of
> the gap and its **rough magnitude on this scenario**, plus — the more useful result — the
> **shape of the variance**. Any number below re-quoted as "the" cost of an agent loop is being
> misused.
>
> Sibling findings taken as settled and **not** re-derived (per the bead's mandatory reading):
> `bh-bomrd.1` **E3** — `bd mol current` is a genuinely readable "where did it stop" report; and
> **E11** — every verb in [`release.py`](../../src/beadhive/release.py) *re-measures* rather than
> remembers (`preflight` / `pending` / `await` / `recover` / `preview`, under the
> `0 = proven / 1 = refused / 2 = half-done / 3 = COULD NOT MEASURE` exit contract), while a wisp
> step is a hand-closed assertion. **This spike measures COST ONLY.** It does not re-open whether
> the cheap record can be trusted — E11 already answered that, and §Evidence E5 below is a live
> instance of exactly the divergence E11 predicted.

## Question

The operator's ROI claim, stated in the `bh-yber2` epic and never measured by anyone:
reconstructing *"where does this release stand"* today makes an outer-loop agent probe the
existing measurement verbs and interpret their text output — claimed to cost real wall-clock
(*"sometimes ~30s"*) and real tokens — where a single deterministic structured query
(`bd mol current --json`) is claimed to cost far less of both (*"~3s + minimal tokens"*).

Two questions, in order:

1. **Direction.** Is the LLM-mediated probe loop measurably more expensive at all — in wall-clock
   and in tokens?
2. **Magnitude.** Is it the ~10× the "~30s vs ~3s" framing implies, or something smaller?

## Method

### The instrument, and one deviation from the bead's letter

The bead specifies the Agent/Task tool's built-in usage reporting (`subagent_tokens`,
`duration_ms`) rather than a hand-rolled timer. **This seat has no Task tool** (its tool set is
Bash / Read / Edit / Write / Skill), so the equivalent instrument one level down was used: the
**same harness in headless mode**, `claude -p --output-format json`, whose result envelope
reports `duration_ms`, `duration_api_ms`, `num_turns`, `total_cost_usd`, and a full `usage`
block. Nothing here is a stopwatch this spike wrote: **every number in the Evidence tables is a
field the harness emitted**, except the four raw-command timings in E4, which are `date +%s%3N`
around a shell command with no LLM in it at all.

Every dispatch used identical flags — `--model sonnet --allowedTools Bash`, cwd a neutral empty
directory (`…/scratch/neutral`, no `CLAUDE.md` on its path, so the harness baseline is byte-for-
byte the same for both conditions), the target directories reached by `cd` inside the prompt.

### The scenario — one release, stopped mid-flight

A clone of this repo at `…/scratch/relscratch`, driven into a genuine post-bump/pre-attest state:

* the real hive's validation ledger copied in, so the **pre-bump** tree (`2b130073d341`) is
  really attested green under `just check-all`;
* a **bump commit** `ed51cfcb89a2` (0.13.0 → 0.14.0, `pyproject.toml` + `CHANGELOG.md`) and a
  **local tag `v0.14.0`**, pushed nowhere.

That is the exact hole `release.py`'s header names: *"`cz bump` … ⇒ A NEW TREE, WITH NO
ATTESTATION."* The true answer to the question is therefore **"attest the bumped tree
(`bh release attest` / `just attest`), then `await`, then push"**, and the verified facts are the
pre-bump green, the tag's absence from origin, and 0.14.0's absence from PyPI.

The clone resolves as a managed hive through an isolated `BH_HOME` copy (fleet + host config
copied to `…/scratch/bhhome`, one `managed_repos` entry added for it with
`work.validate.push-main: just check-all`). **The operator's real config was not touched.**

The structured surface is a separate scratch hive at `…/scratch/molhive`: `bd init` +
`bd mol wisp mol-release-run --var version=0.14.0`, using `bh-bomrd.1`'s formula artifact
([`bh-bomrd.1-mol-release-run.formula.json`](bh-bomrd.1-mol-release-run.formula.json)) plus **one
added step** — `attest-bumped` (`needs: [bump]`, and `release-preview` re-pointed at it) — so the
wisp mirrors the flow `release.py` actually runs rather than `bh-bomrd.1`'s 4-step abbreviation.
Molecule `rl-wisp-71m`, with `attest` and `bump` closed carrying the measured facts as close
reasons ⇒ `Progress: 2/6`.

### The two conditions

Both were given the **same question** — *"What is the next action for this release, and what has
already been verified?"* — and the same answer format (*"at most 6 lines"*).

| | Allowed to run | cwd of the work |
|---|---|---|
| **A** (probe loop) | ONLY `bh release preflight` / `pending` / `preview` / `recover --tag` — no `git`, no `bd`, no file reads, no molecule query | `…/scratch/relscratch` |
| **B** (single query) | Exactly one command: `bd mol current rl-wisp-71m --json` | `…/scratch/molhive` |

**Fairness caveat, stated plainly.** B's record was seeded *by this spike* from the same measured
facts A had to go and measure. So the comparison prices the **cost of reconstruction**, and
deliberately gives condition B the benefit of a record that is already correct and already
populated — a benefit E11 says is not free in the real world.

## Evidence

### E1. Every dispatch, verbatim from the harness result envelope

`total_tok` = `input + output + cache_creation + cache_read` (what the model processed);
`cache_cr` / `cache_rd` are shown separately because they move with *cache warmth across runs*,
not with the condition — the runs were dispatched in order A1, B1, A2, B2, A3, B3, A4, B4, A5, B5.

| run | duration_ms | api_ms | turns | output_tok | cache_cr | cache_rd | total_tok | cost_usd |
|---|---|---|---|---|---|---|---|---|
| A1 | 12110 | 7948 | 4 | 615 | 22808 | 69219 | 92646 | 0.1669 |
| A2 | 11658 | 7251 | 4 | 609 | 2240 | 89772 | 92625 | 0.0495 |
| A3 | **32222** | 22829 | **7** | **1968** | 3299 | 232092 | **237369** | 0.1190 |
| A4 | 11621 | 7497 | 4 | 607 | 2252 | 89772 | 92635 | 0.0496 |
| A5 | 12616 | 8437 | 4 | 725 | 2295 | 89772 | 92796 | 0.0516 |
| B1 | 8649 | 6519 | 2 | 370 | 16139 | 76222 | 92735 | 0.1253 |
| B2 | 8196 | 6226 | 2 | 381 | 2921 | 89440 | 92746 | 0.0501 |
| B3 | 8569 | 6641 | 2 | 375 | 0 | 92361 | 92740 | 0.0333 |
| B4 | 9890 | 8219 | 2 | 362 | 0 | 92361 | 92727 | 0.0332 |
| B5 | 7575 | 5935 | 2 | 374 | 0 | 92361 | 92739 | 0.0333 |

| median (n=5) | duration_ms | turns | output_tok | total_tok | cost_usd |
|---|---|---|---|---|---|
| **A** probe loop | **12110** (range 11621–32222) | 4 (one run: 7) | 615 | 92646 | 0.0516 |
| **B** single query | **8569** (range 7575–9890) | **2, every run** | 374 | 92739 | 0.0333 |
| **A ÷ B** | **1.41×** (worst A ÷ best B: **4.25×**) | **2×** | **1.64×** | 1.00× | **1.55×** |

### E2. The direction holds; the magnitude is nowhere near 10× — except in the tail

Median wall-clock **1.41×**, median output tokens **1.64×**, median cost **1.55×**, turns
**exactly 2×**. Small, but it is the same sign on every metric and on every one of the five
pairs — no A run was faster than the slowest B run.

The tail is where the operator's number lives. **A3 took 32.2 s** — the agent chose to run more
probes and reason harder about `pending`'s bare exit-1 — against a B range that never left
**7.6–9.9 s**. That single run is a 4.25× gap and a 2.6× total-token gap, and it is a
near-perfect match for the operator's remembered *"sometimes ~30s"*.

So the honest framing is **not** "the loop costs 1.4×". It is: **A's cost is agent-chosen and
therefore unbounded; B's is structural and flat.** B ran exactly 2 turns in 5/5 runs (one tool
call, one answer) — there is no path by which it spends more. A ran 4 turns in 4/5 and 7 in the
fifth, and nothing in the design caps that.

### E3. `total_tok` is flat at ~92.7k for both — and that is an artifact, not a finding

Nine of ten runs processed ~92.6–92.8k tokens regardless of condition, because a cold headless
harness carries a ~92k system-prompt/tool-definition floor that dwarfs this task. Only A3, whose
extra turns re-read that floor, breaks out (237k). **Do not read the flat column as "the
conditions cost the same tokens."** The floor is a constant this measurement adds; the
condition-attributable token difference is the one the floor hides — **turns (4 vs 2) and output
tokens (615 vs 374)** — and in a real outer-loop agent, whose context is already warm, that
marginal difference is the whole cost.

### E4. Most of the measured wall-clock gap is CLI process startup, not LLM reasoning

With no LLM involved at all (three consecutive shell runs each):

| | wall clock | stdout bytes |
|---|---|---|
| all four probes (`preflight`, `pending`, `preview`, `recover --tag`) | 6038 / 5501 / 5510 ms | 1714 |
| `bd mol current rl-wisp-71m --json` | 1157 / 1163 / 1155 ms | 3341 |

Per-probe: `preflight` 1755 ms, `pending` 1312 ms, `preview` 1429 ms, `recover` 1569 ms — i.e.
**~1.2–1.4 s of interpreter/CLI startup each**, with `preview`'s three real network measurements
(ledger + `git ls-remote` + a PyPI request) adding almost nothing on this host's fast link.

Subtract that from E1's medians: A spends ~5.5 s in tools and ~6.6 s in the model; B spends
~1.2 s in tools and ~7.4 s in the model. **The model-side cost of the two conditions is
indistinguishable at this N.** The entire reproducible wall-clock gap is
`(4 − 1) × CLI startup`. Two consequences worth carrying forward:

* on the operator's *slow/remote* connection the gap widens, since the network-touching probes
  are invoked 4× rather than 1×;
* and the same gap is closable **without any new record** — see Recommendation.

Note also that the "expensive" condition's probe output is **half the bytes** of the "cheap"
condition's JSON (1714 vs 3341). The cost is in **round-trips and reasoning**, not payload size.

### E5. The cheap answer was cheaper *and different* — a live instance of E11

Not a cost finding, but it was measured, so it is reported. All 5 A runs answered correctly:
*"next action: `just attest` / `bh release attest HEAD` — the bump is refused because no fresh
green verdict exists for HEAD; verified: `v0.14.0` is not on origin (still fully reversible),
`beadhive 0.14.0` is not on PyPI."*

All 5 B runs answered from the wisp's `next_step` field: *"next action: `rl-wisp-jzt` — Gate:
human."* The materialised gate bead has no `needs`, so it is `ready` from instantiation and wins
the topological pick over `attest-bumped`, which is the step the *world* requires next. B's
"already verified" list was richer and more precise (it relayed the close reasons verbatim:
tree `2b130073d341` green at `2026-08-20T19:20:46Z`, bump `ed51cfcb89a2`, tag `v0.14.0`) — and
that is exactly because this spike *put* those measured facts there.

Read narrowly: **the 1.4×/1.55× saving bought an answer that named the wrong next action**, on a
record that was hand-seeded to be otherwise perfect. E11 predicted precisely this class of
divergence; nothing here re-opens it.

## Verdict — the direction holds, the magnitude does not

**The operator's claim is directionally CONFIRMED and quantitatively OVERSTATED for the typical
case, while being accurate about the tail.**

| The claim | Measured here |
|---|---|
| The probe loop costs real wall-clock, *"sometimes ~30s"* | **Yes** — median **12.1 s**, but one run in five hit **32.2 s** |
| The probe loop costs real tokens | **Yes, modestly** — **1.64×** output tokens, **2×** turns, **1.55×** cost at the median |
| A single structured query costs *"~3s + minimal tokens"* | **No — 8.6 s median**, never below 7.6 s. The *command* is 1.16 s; the ~7.4 s is the LLM turn around it, which the claim omits |
| Therefore ~10× | **No — 1.4× at the median.** 4.25× at the extremes |

Two corrections a future reader should take from this rather than the headline ratio:

1. **The "~3s" side of the claim is the part that is wrong.** Any LLM-mediated answer, even a
   one-command relay, costs ~7–8 s of model time here. Only a *non-agentic* caller — a script,
   a hook, a status line — collects the 1.16 s number.
2. **What the structured query actually buys is variance, not mean.** 2 turns every time versus
   4-or-7-at-the-agent's-discretion is the durable difference; the median gap is small enough to
   be swamped by ordinary noise.

And per E4, the measured mean gap is not "LLM interpretation is expensive" at all — it is
**4 CLI startups instead of 1**.

## Recommendation

1. **Carry this to `bh-yber2.3` as: the ROI claim does not, on its own, justify a second
   release-state record.** A 1.4× median (4.25× tail) saving is real but small, it is mostly
   process startup, and E5 shows it was collected while giving the wrong next action. Combined
   with `bh-bomrd.1`'s standing **NO-GO** on E11 grounds, the measured ROI is not the
   counterweight the epic hoped for — it does not move the ADR's Decision-4 row.
2. **The lazy fix that captures nearly all of the measured gap, with no new record: one probe
   that answers in one invocation.** E4 attributes the gap to `(4 − 1) × ~1.3 s` of CLI startup
   and E1 attributes the token gap to 4 turns instead of 2. A `bh release status [--json]` — the
   same four measurements `preflight` / `pending` / `preview` / `recover` already make, run once
   and emitted structured — collapses both terms at once, and it stays *measured*: no asserted
   state, no second thing that can silently disagree with the remote, E11 untouched. That is a
   small, scoped, product-shaped bead, and it is where the ROI in this claim actually lives.
3. **Do not re-quote the ratios above without the N.** Five dispatches per condition, one
   scenario, one model, one host. If a decision needs a firmer number, the cheap next experiment
   is the same harness across ≥3 scenarios (clean tree / half-done push / `COULD NOT MEASURE`)
   with warm context, which would isolate the marginal cost the ~92k floor hides here.

> Scratch artifacts (`…/scratch/relscratch`, `…/scratch/molhive`, `…/scratch/bhhome`) were
> removed after the runs; the raw harness JSON envelopes lived in the session scratchpad, and
> every field quoted above is transcribed from them.
