# Jev adoption tracks — config-only validation gates, a judgment envelope, and Bifrost port custody

> Status: **proposal** (2026-09-22). Bead: `bh-ljlhq`. Documentation only; no product code.
> Extends [`jev-decision-engine-spike-matrix.md`](jev-decision-engine-spike-matrix.md) (2026-09-19)
> and its section 9 amendment (2026-09-20, `bh-shlla.1`). It does not restate the matrix's
> rankings, primitives, or GO bars, and does not re-litigate any of them.

## Why this document exists

The matrix ranked twenty decision points and grouped them into seven molecules with one
sequence. Since it was written, three things changed that the matrix cannot see, and together
they invalidate its sequencing rather than its analysis:

1. **Section 9.6's hold on M0 has already expired.** It holds M0 "until `bh-ie41e.5` and
   `bh-wl4jh.4` close". Both closed on 2026-09-20. `bh-bnxm1` was filed to re-verify section 9
   when they did; its trigger has fired and it is still open.
2. **Every seam the matrix names is scheduled to move.** This is the finding that would freeze
   the whole programme if applied naively, and section 2 below shows why it must not be.
3. **A config-only gate surface exists that the matrix never considers.**
   `work.validate.<phase>` runs an arbitrary command at seven bead lifecycle transitions and
   decides each one by exit code, with no `bh` change of any kind.

A fourth fact constrains the matrix's own tracer bullet: `BifrostLocalClassifier` is a pinned
port of third-party Apache-2.0 code, so the "fix the keyword scorer" reading of `bh-da31q` is
wrong (section 5).

This document separates the gates, proposes three independently sequenced tracks, and ends with
the molecules to file.

---

## 1. Three gates, not one sequence

The matrix treats sequencing as a single ordered list of molecules. It is actually three
independent axes, and conflating them is what produced a plan in which nothing can start.

> **Forward references.** `repository-physical-organization-adr.md` (`bh-50gsn.2`) and
> `root-module-ownership.toml` (`bh-50gsn.1`) are cited throughout and are **not on `main`
> yet** — both live on `wt/batch/bh-50gsn`. Read them from that batch branch until it lands.
> The same applies to section 9 of the spike matrix, which is on `wt/bead/issue/bh-shlla.1`.

| Gate | What it actually blocks | Status on 2026-09-22 |
|---|---|---|
| **A — M-SEC / `OutboundSanitizer`** | Any point whose state is **logs, diffs or transcripts**, plus every Tier-T teacher pass over that data | **Not started.** Hard blocker on matrix ranks 1–6 and J-REV. Zero coupling to either workstream |
| **B — the Beads v1.3 typed read boundary** | Points whose state is **bead rows in bulk**: J-CTX, J-COL, J-DUP, J-NXT | Decisions closed 2026-09-20; implementation open (`bh-xkg4u`, `bh-ywew7`, `bh-yml5h`) |
| **C — the physical refactor (`bh-cgfj0`)** | Only points needing a **new port cut into a frozen root file** *and* live shadow traffic | Chain runs `bh-50gsn → bh-31p2s → bh-7oo93 → bh-ym56t → bh-4kmy0`; `bh-31p2s` additionally depends on `bh-yml5h` |

Two corrections to section 9 fall out, and belong to `bh-bnxm1`:

- **Strike the M0 hold in 9.6.** Its two stated reasons were the decisions closing, and S1's
  harness resting on a read path those decisions might replace. The first is resolved. The
  second never applied: **section 9.4 rule 1 forbids `--brief` in the label pipeline**, so S1's
  corpus extraction reads full payloads and was never coupled to the projection work.
- **Re-read 9.2's third bullet against the closed `bh-ie41e.5` verdict.** Section 9.1 recorded
  it as a forward reference to a drafted decision. It has since closed, and 9.2 should bind
  state builders to whatever that decision actually authorizes.

---

## 2. Seam ownership — why "wait for the refactor" is the wrong default

Every file the matrix names as a seam is in the `legacy_implementation` root class of
`docs/design/root-module-ownership.toml`, owned by `bh-31p2s, bh-7oo93, and bh-ym56t`:

`complexity.py` · `work_next.py` · `localloop.py` · `seatrun.py` · `schedule.py` ·
`triage.py` · `plan.py` · `work_submission.py`

If seam stability were the gate, no Jev work of any kind could begin until `bh-ym56t` lands.
That conclusion is wrong, for two reasons the matrix already established but never connected:

1. **Spike prototype code is not product code.** Matrix section 7 puts it in
   `docs/spikes/artifacts/`. Spikes are refactor-invisible by construction. Only
   *implementation* attaches to a seam.
2. **The organization ADR closes the package root, not the repository.**
   `docs/design/repository-physical-organization-adr.md` Decision 2 forbids new
   **package-root** implementation files. Decision 1 points new code at
   `modules/<capability>/`, `kernel/<concern>/`, `adapters/<boundary>/`,
   `integrations/<external-product>/` and `bootstrap/`. `kernel/plugins/manifests/` already
   carries `repowise.json` and `observaloop.json`, so a plugin manifest has a home today that
   touches no frozen file.

**The operative rule:**

> Bind through an existing injected `Protocol` → go now. Cut a new seam into a frozen root file
> *and* require live shadow traffic → wait. Everything between those is an offline spike or a
> config change, and is not gated at all.

Exactly one decision point satisfies the first clause today: **J-CPX**, through
`complexity.ComplexityClassifier` (`src/beadhive/complexity.py:202`), a `@runtime_checkable`
Protocol with three injected consumers — `plan.py:115`,
`complexity_backfill.py:159` and `:408`.

---

## 3. Track B — config-only gates at the validation boundary

This track needs no `bh` change at all. It is the fastest available measurement of whether Jev
helps this fleet, and it is not mentioned anywhere in the matrix.

### 3.1 The gate surface

`config_work_settings.validate_cmd(cfg, entry, phase, main_gate)`
(`src/beadhive/config_work_settings.py:73`) resolves `work.validate.<phase>` (per-hive over
global), falling back to `work.validate_cmd` (default `just check`). With `main_gate`, a
`<phase>-main` override is preferred over `<phase>`. Seven named phases, each an arbitrary
command whose **exit code decides whether the bead transition proceeds**:

| Phase key | Transition it gates | Call site |
|---|---|---|
| *(none)* → `work.validate_cmd` | `bh work check` | `work_submission.py:40` |
| `submit` | `in_progress → submitted` | `work_submission.py:409`, `work_group.py:414` |
| `molecule` | molecule-branch validation | `work_merge.py:231`, `:269` |
| `merge`, `merge-main` | `approved → merged` | `work_merge.py:866` |
| `postland` | after landing | `work_merge.py:297` |
| `union` | combined/union validation | `work_merge.py:826` |
| `push-main` | pre-push main gate | `prepush.py:280` |

### 3.2 The execution contract

From `worktree_verify.py:948`:

```python
res = run(shlex.split(cmd), cwd=str(tmp), check=False, env=child_env, tee=log)
```

- **`shlex.split`, not a shell.** No `&&`, no pipes, no globs. The configured value must be one
  executable plus arguments — a `just` target or a script path.
- **`cwd` is a throwaway detached worktree.** `git worktree add --detach` at
  `worktree_verify.py:739`.
- **`env` is `os.environ`** minus `OTEL_*`, `BH_OBSERVALOOP_PROFILE`/`WS_OBSERVALOOP_PROFILE`
  (`otel.telemetry_neutral_env`, `otel.py:53`) and `FORCE_COLOR`/`CLICOLOR_FORCE`; plus
  `OTEL_SDK_DISABLED=true`, `NO_COLOR=1`, `BH_TEST_REPORT_DIR` (`test_report.export`,
  `test_report.py:107`), and — only when
  `work.validation_protocol = "beadhive-validation-result/v1"` —
  `BH_VALIDATION_RESULT_PATH` (`validation_records.py:30`, path built at `:477`).
  Everything else is preserved untouched, so **`TYPESAFE_API_KEY` reaches the child**.
- **`rc` is the sole verdict.** An ingested report "can never upgrade it"
  (`worktree_verify.py` comment at the `BH_TEST_REPORT_DIR` export).
- **A structured verdict has a home already.** `validation_records.read_protocol` is consumed
  into the run record at `work_submission.py:159`.

### 3.3 What a validation child can and cannot see

| Available | Not available |
|---|---|
| The full tree at the validated sha | **The bead id** |
| Full git history — it is a real worktree | The bead's description / acceptance criteria |
| Any diff it computes itself | The phase it is running as |
| `BH_TEST_REPORT_DIR` (JUnit drop zone) | The branch name — **HEAD is detached** |
| `BH_VALIDATION_RESULT_PATH` (structured drop) | |

The bead id is not recoverable from commit messages: only merge commits carry
`bead bh-<id>`. Ordinary commits on a bead branch do not.

**Therefore the honest zero-change scope is diff-and-tree judgments, not bead-relative ones.**
J-SUB's per-criterion Nouls are out of reach until the bead reaches the child.

### 3.4 Proposed experiments

**B1 — diff-shaped Noul gate.** A `just` recipe over `git diff <base>..HEAD` asking J-REV's
critical-section flags as independent Nouls: touches a trust boundary (auth, credentials,
destructive path); adds behavior with no corresponding test change; leaves commented-out code
or a bare TODO. Multi-label, so Nouls rather than a Choice. Starts as warnings (exit 0).
Judges the tree only; needs no bead identity and no sanitizer for this hive, whose diffs are
public code.

**B2 — J-CHK in `advise`, as a wrapper.** A script configured at `work.validate.submit` that
runs the real validation command, and on failure classifies the pre-parsed failure blocks with
`jev-cli`, writes the verdict to `BH_VALIDATION_RESULT_PATH`, and **re-exits with the original
`rc`**. Non-invasive by construction: it can annotate the ledger, never flip red to green.

**B2 is an egress, and it is the exact one M-SEC exists to gate.** Check logs dump environments
and quote tokens that tools printed. Matrix section 4.4 forces log egress to `false` until S2
is GO. The narrow way through is **S2 layer 1 alone** — known-value matching over
credential-patterned env vars, the sources `credentials.py` resolves, `gh auth token`, harness
auth files, and `.env` files in the hive. The matrix itself calls this "near-total recall for
*our own* secrets at near-zero cost". It is a small script, not the full spike, and it is the
honest precondition for B2.

### 3.5 The one small `bh` change worth making

Export `BH_BEAD`, `BH_PHASE` and `BH_BRANCH` into `child_env` beside the existing
`test_report.export(...)` call. Additive, matches the `BH_TEST_REPORT_DIR` precedent ("bh never
invokes a runner — it names a directory and reads what appears"), and it is what turns the gate
from tree-blind into bead-aware, making **J-SUB a config-only gate**.

It is independently justifiable and its trigger is not a spike verdict, so it is filed as a
standalone bead rather than inside a molecule — the reasoning `bh-bnxm1` records for itself.

---

## 4. Track A — a judgment envelope for closed-vocabulary decisions

**The idea:** express today's deterministic decisions in System One answer shape, so that gate
policy is written and tested against engines we fully control, and a later engine swap changes
the engine — not the call sites, the vocabularies, or the tests.

### 4.1 Three quarters of this already exists

| Existing type | Primitive shape | Already has | Missing |
|---|---|---|---|
| `work_next.Decision` (`work_next.py:220`) | **Choice** — `ROWS`, `ACTIONS` (11), `REASONS` (6), all closed and asserted at construction | closed vocabulary, stable `as_dict()` | distribution, confidence, abstain, engine provenance |
| `seatrun.Classification` (`seatrun.py:189`), `RunOutcome` (`:178`) | **Choice** — `DONE`/`BLOCKED`/`HANDOFF`/`INCOMPLETE`; "the single verdict every tier is supposed to consult" | closed `StrEnum`, `detail` | same |
| `complexity.ComplexityResult` (`complexity.py:171`) | **Score** — four ordered tiers, `score` normalized 0..1, `TierBoundaries` as explicit thresholds | `FallbackProvenance` (`:156`) is ABSTAIN in all but name; `source` and `version` already discriminate backends | distribution, confidence |
| `work_next._ACTION_SIGNATURES` (`work_next.py:123`) | **Noul** — substring match for "is this the same failure" | — | a substring match standing in for a probability |

`ComplexityResult` is the template: it already carries `tier | None` for UNKNOWN, a normalized
score, `source`, `version`, and provenance for why a fallback fired.

### 4.2 The envelope

```python
@dataclass(frozen=True)
class Judgment:
    outcome: str | None                  # member of the family's closed set; None = ABSTAIN
    probabilities: Mapping[str, float]   # deterministic engine -> one-hot
    confidence: float                    # deterministic engine -> 1.0
    option_count: int                    # see 4.3
    engine: str                          # "rule:work_next.v1" | "beadhive/bifrost-compatible-local" | "jev-1.13.0"
    evidence: Mapping[str, object]
    fallback: FallbackProvenance | None
```

A deterministic implementation returns a one-hot distribution at confidence `1.0`, so no
behavior changes. What is gained now is that **`bh`'s decisions become uniformly auditable and
abstain-capable**, and the gate-policy functions get built and tested against fixtures.

### 4.3 Two asymmetries to build in from the start

Confirmed against the live TypeSafe API reference (`POST https://api.typesafe.ai/v1/systemone`):

- **A Noul answer is `{type, noul}` — there is no `confidence` field.** A uniform
  `confidence >= floor` gate is meaningless for Noul-shaped families; they need a two-sided
  band (`hi`/`lo` → YES / NO / ABSTAIN), where 0.5 means undecided rather than medium.
- **Choice confidence depends on the option count.** The documented derivation is
  `clamp((n * p_max - 1) / (n - 1), 0, 1)`. Since matrix section 2 mandates adding a
  `none`/`other` option to every Choice, **adding that option shifts the confidence scale**.
  Thresholds are therefore per-family and never shared, and `option_count` must be recorded
  next to `confidence`. A reworded question set that changes option count invalidates its
  tuning, so `question_set_version` must cover criteria, not only instruction text.

For reference, the other shape constraints the envelope must tolerate: Choice `criteria` is a
map of option to description (max 255 options); Score `criteria` is an ordered array of 2–10
level descriptions and its `score` may land *between* levels, returning a `legend`; Noul
`criteria` is an optional `{true, false}` clarification.

### 4.4 Constraint preserved

R4 stands. No `Judgment` producer runs inside `work_next.decide()`. The impure edge computes the
semantic fact and passes it in as data, exactly as `bd ready` rows are passed today.
`_ACTION_SIGNATURES` is the clean first case: a substring match replaced by a Noul-shaped input
computed at the edge.

---

## 5. Track C — Bifrost port custody and a second backend

### 5.1 The constraint

[`docs/upstream/bifrost-complexity-scorer.md`](../upstream/bifrost-complexity-scorer.md) pins
`BifrostLocalClassifier` (`complexity.py:519`) to `maximhq/bifrost@c1b84fdc5a85`, Apache-2.0,
Copyright 2025 H3 Labs Inc. `LOCAL_SCORER_SOURCE = "beadhive/bifrost-compatible-local"` and
`LOCAL_SCORER_VERSION = "1.0.0+bifrost.c1b84fdc5a85"` (`complexity.py:31-32`) change **only**
when the pinned semantics or research basis change.

The compatibility target is explicit: weights 30/25/25/−5/10; saturation at 3 code, 2 reasoning,
3 technical, 2 simple; the three-segment word-count curve; thresholds 0.15/0.35/0.60; the
strong-reasoning override; and UNKNOWN when no lexical signal matches.

### 5.2 What this does to `bh-da31q`

`bh-da31q` measured that once the code dimension saturates at 3 distinct hits, its contribution
pins at 0.300, and with reasoning and technical at zero the only term still moving is the
length-driven word-count score weighted 0.10 — putting the total in `[0.30, 0.40]` with the
MEDIUM/COMPLEX boundary at 0.35 dead centre, for 24.4% of routable beads.

**The measurement is sound and the mechanism is real. The bead is filed against the wrong
thing.** Those caps and that weight are *in the compatibility target*. Changing them in place
would silently fork the port and void the attribution claim.

`bh-da31q` should be re-characterized from "bug in `BifrostLocalClassifier`" to "measured
evidence that the Bifrost-compatible tier band underserves Beadhive bead text", and become the
motivating evidence for a second backend.

### 5.3 What the upstream note already authorizes

Nothing here is new architecture. The note's "Replacement options" already state:

> The `ComplexityClassifier` protocol and backend-neutral `ComplexityResult` are the replacement
> boundary.

with option 2 being "ship a compiled or intentionally forked helper behind the same Python
protocol", and the entry conditions spelled out: **new source/version values, parity tests for
this corpus, and an explicit decision about UNKNOWN and required fallback behavior.**

### 5.4 Proposal

- **C1 — pin-drift gate.** Check that the version string, weights, caps, curve, thresholds and
  override still match the pinned upstream note, and that Apache-2.0 attribution and
  modification marking survive. **This obligation exists today and nothing enforces it.** It
  depends on nothing else in this document.
- **C2 — dual-backend selection.** `BifrostLocalClassifier` stays, frozen, at its version
  string. A second backend is a *sibling* behind `ComplexityClassifier`, selected by config,
  discriminated by the `source`/`version` fields that already exist on `ComplexityResult`.
  `DEFAULT_CLASSIFIER` (`complexity.py:607`) stays Bifrost until a decision says otherwise.
- **C3 — one comparison harness, two purposes.** A corpus runner scoring the same beads through
  both backends is simultaneously J-CPX's measurement and C1's parity evidence. Build it once.
- **C4 — re-characterize `bh-da31q`** per 5.2.

### 5.5 A note on the J-CPX GO bar

The matrix sets J-CPX's bar at "exact-tier agreement ... >= the keyword scorer's + 15 pts". Given
5.2, that bar may be met for the wrong reason. The spike must report agreement against the
consensus set **and** against the 24.4% length-decided band separately, or the verdict flatters
the new backend.

---

## 6. Replan — molecules, sequencing, and what not to file

### 6.1 Reconciliation with in-flight work

| Existing bead | Action |
|---|---|
| `bh-shlla.1` | **Land it.** In progress with an expired lease; section 9 is stranded on `wt/bead/issue/bh-shlla.1`. Everything here cites it |
| `bh-bnxm1` | **Run it.** Trigger fired 2026-09-20; apply the two corrections in section 1 |
| `bh-da31q` | Re-characterize per 5.2; becomes M-CPX's evidence bead |
| `bh-cgfj0` chain | Track A *implementation* sequences with `bh-7oo93`, which owns the capability packages holding `work_next.py`, `seatrun.py`, `complexity.py` and `plan.py`. The Track A *contract decision* lands now, as documentation |
| `bh-xkg4u`, `bh-ywew7`, `bh-yml5h` | **No coupling.** No track here reads bead rows in bulk |
| M-SEC | Still blocks B2. B1, all of Track A, and all of Track C are clear |

### 6.2 Molecules to file

**M-GATE — `spike(jev): config-only decision gates at the validation boundary`**

| Bead | Note |
|---|---|
| Spike: diff-shaped Noul gate as a `just` recipe (B1) | Measure signal vs noise against landed diffs |
| Spike: known-value secret filter, S2 layer 1 only | Precondition for B2; a genuine down-payment on M-SEC |
| Spike: J-CHK advise wrapper writing `BH_VALIDATION_RESULT_PATH` (B2) | Depends on the filter spike |
| Decision (`tag:decision`) | Adopt / narrow / reject config-only gates, and which phases carry which |

**Standalone, outside any molecule:**
`feat(validation): export BH_BEAD / BH_PHASE / BH_BRANCH into the validation child env`

**M-CPX — `feat(complexity): Bifrost port custody and a second classifier backend`**

| Bead | Note |
|---|---|
| `feat: pin-drift gate for the Bifrost-compatible scorer` (C1) | Depends on nothing; land it first |
| `feat: dual-backend selection behind ComplexityClassifier` (C2) | Bifrost stays frozen and maintained |
| Spike: comparison harness and J-CPX measurement (C3) | Per-tier reporting required by 5.5 |
| Decision (`tag:decision`) | Acceptance criteria are the upstream note's three stated entry conditions |

**M-JUDGE — `docs(design): ADR for a judgment envelope over bh's closed decision vocabularies`**

Documentation only, mirroring how `bh-shlla` scoped itself. Decides the envelope, the
Noul/Choice threshold asymmetry, the ABSTAIN rule, and **where it lands** under the
organization ADR. Its placement should be ratified inside `bh-50gsn.5`, which is already
chartered to reconcile architecture documentation, rather than decided unilaterally.
Implementation leaves file into `bh-7oo93` at replan time.

### 6.3 Sequencing

```text
NOW, no gate
  bh-shlla.1 (land section 9) · bh-bnxm1 (re-verify) · bh-da31q (re-characterize)
  M-CPX C1   pin-drift gate            <- smallest; unblocks nothing but pays immediately
  M-GATE     B1 diff gate · S2-layer-1 known-value filter
  M-JUDGE    ADR (docs only); placement ratified via bh-50gsn.5

THEN
  M-GATE     B2 J-CHK wrapper          (after the filter spike)
  M-CPX      C2 dual backend · C3 harness · decision
  standalone BH_BEAD env change        -> J-SUB becomes a config-only gate

AFTER bh-7oo93
  M-JUDGE    implementation leaves: Decision / Classification / ComplexityResult onto the envelope
```

### 6.4 What not to file

- **No standalone `beadhive-jev` distribution wrapping "SDK or CLI".** The SDK and `jev-cli`
  are two packagings of the same `POST /v1/systemone`; an abstraction over them is a seam that
  buys nothing. Matrix section 4.3's choice of client per use stands as a choice, not an
  interface.
- **No `DecisionPort` product code yet.** M-JUDGE decides the shape first, and the files it
  would touch are frozen by section 2.
- **No change to `BifrostLocalClassifier`'s algorithm, ever**, without moving the pin and the
  version string together.

---

## 7. Open questions for the operator

1. Does the **S2 layer-1 known-value filter** belong in M-GATE as B2's precondition, or as the
   opening bead of a real M-SEC molecule that M-GATE then depends on? This document leans
   M-SEC, since it is needed there regardless.
2. Does **M-JUDGE's ADR** go in as its own bead, or as an amendment inside `bh-50gsn.5`?
3. Is the `BH_BEAD` / `BH_PHASE` / `BH_BRANCH` env change wanted on its own merits, independent
   of whether any Jev gate is ever adopted?

---

## 8. Implementation log (2026-09-22)

Recorded here because one of the decisions below **reverses a seam this document implied**, and
a proposal that quietly disagrees with what shipped is worse than no proposal.

### 8.1 What was filed

| Molecule / bead | Scope |
|---|---|
| `bh-o2mjz` (M-SEC) | Outbound data safety: corpus, layer-1 known-value filter, offline scanners, residue classifier, redaction-vs-judgment agreement, gateway, decision |
| `bh-j83gm` (M-GATE) | Config-only gates at the validation boundary; `bh-j83gm.2` depends on `bh-o2mjz.2`, so the J-CHK wrapper cannot run before the sanitizer exists |
| `bh-hlwwo` (M-CPX) | Comparison harness, J-CPX measurement, second-backend decision |
| `bh-7tq78` | Bifrost pin-drift gate — independent of every Jev verdict |
| `bh-9igpb` | Export `BH_BEAD` / `BH_PHASE` / `BH_BRANCH` into the validation child env |
| `bh-068zo` | Emit JUnit XML into `BH_TEST_REPORT_DIR` |
| `bh-xg1r9` | Trivial-change policy short-circuit (below) |

M-JUDGE was folded into `bh-50gsn.5` rather than filed separately, per §7 question 2.

### 8.2 The seam correction

`bh-xg1r9` was filed as a composing **`ImpactBackend`** and that was wrong. `apply_fail_closed`
is **proof-carrying**: a key reaches `unaffected_keys` only with an owner for every changed path,
no global-input hit, units present, membership in `proven_keys`, and no affected unit. A
documentation triage has none of that evidence, so a backend would have had to fabricate exactly
the proof the fail-closed rules exist to demand — and `selective_validation` feeds
`unaffected_keys` into `carry_key_verdict`, recording a green the key never earned.

What shipped instead is a **policy short-circuit above the resolver**, narrowing `active_keys` the
way `AttestKeyConfig.enabled` already behaves: *"deliberately absent: they neither run, block,
satisfy the aggregate, nor produce carryable proof."* `all_keys_green` therefore still refuses to
call the revision fully attested. Impact resolution, `apply_fail_closed`, Pants and the receipt
contract are untouched.

**The general rule this is an instance of:** *what did this change touch* is an impact question
answered from evidence; *what do we choose to test* is a policy question answered from
configuration. Expressing the second as the first is how a policy decision gets laundered into
the audit trail as proof.

### 8.3 Two judges, because misconfiguration is the live risk

A skip requires the configured path globs **and** an optional semantic triage
(`yes` / `no` / `unknown`) to agree. Neither can skip alone: globs that are too **broad** are
vetoed by a triage answering `no`; globs that are too **narrow** never fire, and the record says
so. `unknown` is first-class — a triage that cannot tell, raises, or returns garbage is never a
veto and never a skip.

Patterns are `fnmatch`, not pathlib globs: `*` crosses `/` and `**` means nothing, so `docs/*`
also admits `docs/schemas/x.json`. That is pinned by a test as the likeliest misconfiguration.

### 8.4 Measurements this work produced

| | |
|---|---|
| Attest key medians (1,642 runs) | `stateful` 348s · `integration` 292s · `demos` 153s · `arch-contracts` 61s · `package` 45s · `docs` 9s · `unit` 4s = **~913s**, run sequentially |
| Green/red split (1,592 runs) | **69.8% green**, 30.2% red |
| Red-log corpus | 380 usable, median **836,806** est tokens, max 4.4M |
| Live `review` call | 0.354s, 7,609 input tokens, **$0.00032** — against 8.6–12.1s and $0.017–0.052 for the seat it replaces |
| JUnit drop zone | **0 XML files across 1,591 runs**; `BH_TEST_REPORT_DIR` referenced nowhere in the repo |

### 8.5 The finding that outranks the rest

A red `just check` log's **root cause did not survive to the decision**. pytest prints exception
detail mid-run and its concise summary last, so a tail-based window carried the `FAILED`/`ERROR`
list while `could not create numbered dir` (40 occurrences) and `bh-hermetic` (144) were both
absent. The model correctly reported low confidence on evidence it never received.

The fix is not a better window. It is `bh-068zo`: bh already exports `BH_TEST_REPORT_DIR` and
already parses JUnit into per-case results, and this repo has simply never opted in. Roughly
2–4k structured tokens would carry the causes that 12k tokens of log tail did not.
