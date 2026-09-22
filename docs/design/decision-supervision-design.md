# Decision supervision — an optional second opinion over deterministic branching

> Status: **proposal** (2026-09-22). Bead: `bh-eq116`. Documentation only; no product code.
> Generalizes the one-off pattern landed by `bh-xg1r9`.

## 1. The observation

Many points in `bh` run a *clearly deterministic calculation* that decides branching, or the
breadth of validation, and those decisions cost real wall-clock and token spend. The
calculations are not wrong in principle. What goes wrong in practice is that **we misconfigure
the inputs, or misconfigure the logic that turns inputs into a result** — and nothing notices,
because a deterministic calculation is confidently wrong in silence.

Three measured instances, all from this hive:

| Instance | The silent wrongness |
|---|---|
| `bh-da31q` | `BifrostLocalClassifier` tiers on **description length** once code keywords saturate; **24.4%** of routable beads sit in the length-decided band. A faithful implementation producing routing nobody would defend |
| `work.attest.impact.on_unresolved: fallback` | A one-file docs change resolved to the full required key set (**~913s** median). The operator bypassed validation by hand rather than pay it |
| `bh-xg1r9`'s own globs | `fnmatch`'s `*` crosses `/`, so `docs/*` silently admits `docs/schemas/x.json` |
| `bh-xg1r9`'s dogfood | `integration_base` returned the literal `wt/bead/epic`, yielding a 12-file diff. The policy declined correctly — on a base nobody had checked |

The last one is the thesis in miniature: **the logic was right and the input was wrong, and only
running it revealed that.**

## 2. What this is, and is not

**Is:** a supervisor that reviews a decision the code already made.

**Is not:** the spike matrix's `DecisionPort`, which *replaces* an agent's micro-decision. This
never originates a decision. The deterministic path always exists and always runs; the
supervisor can only confirm or override its result.

The claim is **not** that Jev knows better than the calculation. It is that a second,
**differently derived** opinion catches configuration drift that a single source cannot. Two
judges disagreeing is information; one judge is just an answer.

## 3. Two question shapes

**Shape A — judge (input only).** *"Given these signals, what should the result be?"* Produces an
independent answer to compare against the calculation's.

**Shape B — audit (input and result).** *"How confident are you that this result makes sense
given the input?"* This is the more valuable shape and has no analogue in the existing matrix:
it audits a decision rather than recomputing it, so it is cheap, it needs no parallel
implementation of the logic, and it degrades gracefully when the model has no strong view.

Shape B is the default. Shape A is for points where the result alone carries too little signal
to audit.

## 4. Verdicts and gating

Three-way: **`yes` / `no` / `unknown`**, matching the vocabulary this codebase already uses for
an honest non-answer (`ComplexityResult`'s `UNKNOWN`, exit 75 in `selective_validation`).
`unknown` is a first-class answer, not an error — a fall-through filter must be able to say "I
cannot tell" rather than guess.

Constraints that shape the gate, all measured:

- **State plus the longest question is capped at 32k tokens**, and accuracy degrades as
  irrelevant state grows. "Inputs small enough" is a real admission gate.
- **A Noul answer carries no `confidence` field.** Noul-shaped supervision needs a two-sided
  band (`hi`/`lo`), never a confidence floor. 0.5 means undecided, not medium.
- **Choice confidence depends on option count** — `clamp((n·p_max − 1)/(n − 1), 0, 1)` — so a
  threshold tuned for one option set does not transfer to another. Record `n` with the verdict.
- **Pin the model.** An alias moves on release and silently shifts a calibrated threshold.

## 5. Modes, per point

| Mode | Acts on | Jev runs |
|---|---|---|
| `off` | the calculation | no |
| `shadow` | the calculation | yes; both recorded |
| `advise` | the calculation, with the verdict surfaced | yes |
| `act` | the supervisor, above threshold | yes |

## 6. Override direction is per point

Some decisions may be overridden **only in the conservative direction** — more validation, more
friction, never less. This mirrors the matrix's J-RO rule that Jev may add friction but never
remove it.

A point's card must state which it is. `bh-xg1r9`'s policy is an example of the asymmetric case:
running an unneeded key costs up to 348 seconds, skipping a needed one ships a regression, so
only the conservative override is permitted.

## 7. Fail-open, always

Any error, timeout, missing key, low confidence, budget overflow, malformed answer, or
unrecognised verdict uses **the calculation**. No exception. A supervisor that can fail a
decision is worse than no supervisor, and this is not a safety boundary being protected — the
calculation is already the trusted path.

Proven in practice: `bh-xg1r9`'s semantic adviser hit a missing API key during its first real
run and printed `semantic key selection unavailable: ... — running normal route`.

## 8. Admission criteria for a decision point

A point qualifies only if **all** hold:

1. the inputs fit the state budget after filtering;
2. the decision is cheap enough that a supervisor round-trip does not dominate it (~0.2–1s);
3. the result is drawn from a **closed set** or an ordered scale, so a verdict is comparable;
4. a wrong override is recoverable, or the override direction is restricted to conservative.

## 9. Telemetry is a first-class output

Every verdict emits: point id, engine, model, question-set version, the calculation's result,
the supervisor's verdict, agreement, confidence, option count, whether an override was taken,
input tokens, latency.

**The purpose is to change the logic or the settings, not to paper over them.** A supervisor
quietly correcting a broken calculation forever is a worse outcome than one that makes the
breakage visible. Persistent disagreement at a point is a bug report about that point.

## 10. Candidate points in `bh` today

| Point | Result type | Why it qualifies |
|---|---|---|
| `complexity.classify` → tier | Score, 4 ordered tiers | `bh-da31q`: measurably misconfigured today |
| `selective_validation` key selection | set of closed key names | `bh-xg1r9` already does this by hand |
| `seatrun.classify_run` → outcome | Choice, `RunOutcome` StrEnum | closed set, small input |
| `work_next.decide` → action | Choice, 11 `ACTIONS` | R4: supervise at the impure edge, never inside the table |
| `work_next._ACTION_SIGNATURES` | Noul | a substring match standing in for a probability |
| `schedule` collapse vs fanout | Choice | `size:` budget plus planner labels |
| `release_order` | ordering | advisory already |

## 11. Placement

A cross-capability mechanism with its own invariant points at `kernel/<concern>/` under the
repository-physical-organization ADR. **This is the same question `bh-50gsn.5` already carries
for the judgment envelope** — reconcile with it rather than deciding twice. The envelope
(`Judgment`: outcome, probabilities, confidence, option_count, engine, evidence, fallback) is
the natural carrier for a supervisor verdict, so the two should land together or not at all.

## 12. Open questions

1. Does supervision compose with the judgment envelope as one kernel concern, or two?
2. Does `shadow` mode need a durable corpus, or is OTEL enough? The loop-ownership ADR's
   Amendment 1 permits local evaluation data the loop never reads.
3. Is Shape B sufficient alone for a first implementation, deferring Shape A entirely?
