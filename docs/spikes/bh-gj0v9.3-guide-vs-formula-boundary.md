# Spike `bh-gj0v9.3` — Guide vs formula/wisp: does formula fill a niche Guide leaves open?

**Bead:** `bh-gj0v9.3` · **Seat:** `dev/dev1` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-gj0v9.4` — adopt formula/wisp, push beads-as-Guide-StateBackend,
or close the question with an ADR

## Question

Beadhive has already adopted, shipped and ADR'd Guide as its operational-runbook layer
(`src/beadhive/assets/guides/setup/`, `docs/design/setup-guide-adr.md`, epic `bh-0olv9`).
**Given that, does beads' `formula`/`wisp` fill any remaining niche — specifically Guide's
missing retention/GC story — that is worth adopting formula for?**

Critically NOT asking: whether formula can execute a workflow (the epic already settled it
cannot — a `Step` has no `action` field), nor whether Guide is good (it is shipped product,
not a candidate). The only live GO candidate is **retention**: Guide's run log is append-only
and permanent, and wisp (`gc`/`squash`/`burn`/`promote`) is the shape of the missing piece.

## Method

1. Read the canonical Guide spec at its source of truth — `agentguides/agentguides:SPEC.md`
   (v0.1, 738 lines) — §12 state/audit model, §13 `StateBackend`, §19 deferred work; grepped
   the whole file for `retention|prune|delete|expire|garbage|permanent`.
2. Read the **reference runtime** `agentguides/runtime` @ `4461bc3`, version **0.5.12** —
   `src/agentguides/state/` (ABC + markdown + beads backends), `src/agentguides/cli/state.py`,
   `docs/DESIGN.md`, `docs/cli/web.md`, and `.planning/decisions/008-beads-mode-set.md`.
   This is where the spike's premise broke (Evidence 1).
3. Read beadhive's own shipped Guides: `src/beadhive/assets/guides/setup/` (GUIDE.md, SKILL.md,
   12 `steps/`, nested `guides/rescue/`), `src/beadhive/setup_guide.py`,
   `claude-plugin/beadhive/skills/backfill/GUIDE.md`, and
   `beadhive/infra:guides/github-app-tier-provision/`.
4. **Measured** run-log growth: `find ~/.guide` on this machine, plus `find -printf '%s'` over
   every real `runs/<run-id>.md` in the runtime's own examples corpus (n=28) for per-run size.
5. Exercised the wisp surface live: `bd mol wisp --help`, `bd mol wisp gc --help`,
   `bd promote --help`, `bd mol wisp list` against this hive (`bd` 1.1.0-dev).

## Evidence

1. **The spike's central premise is stale: the beads `StateBackend` is not parked, it shipped.**
   The bead and epic both state "Guide's own SPEC.md names beads as the obvious future
   StateBackend but parks it (ADR 008); no beads code path exists in the Guide runtime." The
   spec text still says that — `agentguides/SPEC.md:567` ("v0.1 ships Markdown; Beads is
   modeled but parked (see ADR 008)") and `SPEC.md:721` — but the **runtime is three minor
   versions past it**. `runtime/.planning/decisions/008-beads-mode-set.md:3` reads
   `**Status:** Accepted`, and `beads.local-cli` **shipped in v0.2**
   (`.planning/milestones/v0.2.md:27`, `- [x] M2 — BeadsBackend(mode=local-cli)`). The code is
   live at `runtime/src/agentguides/state/beads/` — 878 lines across `backend.py` (601),
   `_local_cli.py` (174), `_client.py` (89) — with the web layer serving it as a first-class
   backend (`docs/cli/web.md:17-18,36-49`) and `guide review` emitting proposals as beads
   (`docs/cli/review.md:61,215`). **There is nothing left to "push upstream": it is upstream.**
2. **Shipping the beads backend did not bring retention with it.** The `StateBackend` ABC has
   ten abstract methods (`runtime/src/agentguides/state/base.py:46-105`): `start_run`,
   `load_run`, `append_event`, `update_step`, `list_runs`, `list_all_runs`, `set_status`,
   `mark_prereqs_checked`, `current` (+ concrete `read_events`/`load_runs`). **None deletes,
   prunes, expires or archives anything.** Same at the CLI: `guide state` exposes 14 verbs
   (`runtime/src/agentguides/cli/state.py`) — `start-run`, `append-event`, `update-step`,
   `load-run`, `list-runs`, `list-all-runs`, `set-status`, `mark-prereqs-checked`, `current`,
   `read-step`, `read-events`, `export-events`, `load-proposal`, `list-proposals` — again no
   delete. Grepping `retention|prune|delete|expire|garbage` across `SPEC.md` returns **one**
   hit, at `SPEC.md:352`, and it is about retry exhaustion, not storage. `SPEC.md:465,494`
   make the permanence explicit: "Markdown body (append-only audit log)" / "The Markdown body
   is append-only." So Evidence 1 does not close the retention gap — the gap is real and
   survives the backend that was supposed to be its answer.
3. **…but the gap is measured at effectively zero.** On this machine, after the setup Guide has
   shipped: `ls ~/.guide*` → *No such file or directory*; `find ~/.guide -path '*runs*' -name
   '*.md'` → **0 files, 0 bytes, growth rate 0 B/day**. Not a sampling artifact — the default
   state path is `~/.guide/state` (`runtime/src/agentguides/config.py:55`) and nothing has ever
   written there. For per-run size, the runtime's own examples corpus holds 28 real completed
   run logs (`examples/guides/*/state/*/runs/*.md`, `examples/books/…`):

   | metric | bytes |
   |---|---|
   | n | 28 |
   | total | 53,196 (52 KB) |
   | mean | 1,900 |
   | median | 1,412 |
   | min | 742 |
   | max | 4,327 |

   A worst-case run log is **4.2 KB**. Beadhive's Guides run at O(machines) and O(hives)
   cardinality — the setup Guide once per machine, backfill once per hive onboard,
   `github-app-tier-provision` once per tier — not per-day. At the observed mean, **1 GB is
   ~550,000 runs**. There is no accumulation problem to solve.
4. **Wisp is the wrong primitive for a Guide run, by wisp's own documentation.** `bd mol wisp
   --help` (bd 1.1.0): "wisp (vapor): Ephemeral work that auto-cleans up … **Any operational
   workflow without audit value**" versus "pour (liquid): Persistent work that needs audit
   trail." A Guide run is *nothing but* audit value — 7 of beadhive's 12 shipped setup steps
   verify by `agent_judgment` (see Evidence 6), whose whole point is the recorded reasoning.
   Also disqualifying for shipped product: wisps are "stored locally but **NOT synced via
   git**." A user's setup-Guide run would be unrecoverable and unshareable.
5. **The wisp lifecycle is unexercised and internally inconsistent.** `bd mol wisp list` in this
   hive → `No wisps found` (matches the epic's finding that `wisps`/`wisp_events` are empty
   everywhere here). The two help texts disagree on where a wisp even lives: `bd mol wisp
   --help` says "Wisps are issues with `Ephemeral=true` **in the main database**", while `bd
   promote --help` says it "copies the issue **from the wisps table (dolt_ignored)**". Betting a
   shipped product's state layer on a lifecycle that is both unrun and self-contradictory in its
   own CLI help is an unpriced risk.
6. **Guide and beads Steps are different layers, and beadhive's shipped Guide proves it
   quantitatively.** Across the 12 files in `src/beadhive/assets/guides/setup/steps/`:
   **7 steps verify by `agent_judgment`, 5 by `script`; all 12 declare `on_failure:`; all 12
   declare `interactions:`.** A concrete step
   (`steps/030-install-bh.md:8-49`) carries `action.type: prompt`, `verify.type: script` with
   `script: scripts/verify-bh-version.sh`, `interactions:`, and `on_failure:` with per-exit-code
   clauses and strategies. A beads `Step` has none of those fields (`bd formula schema Step`, per
   the epic). The layers are complementary, not competing — and the complement runs one way only:
   Guide can *use* beads for storage (Evidence 1); beads cannot express what a Guide step does.
7. **Beadhive does not own the retention decision even if it wanted to.** `SPEC.md:567`: "The
   backend is chosen by the **runtime/harness + operator**, never by the Guide — a Guide is
   backend-agnostic and carries no backend declaration." Beadhive's shipped `SKILL.md` complies:
   its `metadata.guide` block is `entry: GUIDE.md` and nothing else (no `state_backend`), and
   beadhive has **zero dependency on the agentguides runtime** (`pyproject.toml` has no
   `agentguides`/`guide` entry; the Guide ships as inert markdown assets). Worse for the premise,
   beadhive's own fallback path stores nothing at all: `bh setup guide`'s wizard
   (`src/beadhive/setup_guide.py`, 461 lines) walks `steps/` interactively and persists **no run
   state whatsoever** — its only writes are the export of the Guide files themselves to
   `~/.beadhive/guides/setup` (`setup_guide.py:127`). Unbounded run-log growth on a user's
   machine is not beadhive's problem to own, because beadhive neither creates nor configures
   that log.
8. **Decision 4's coupling risk, costed.** `docs/design/setup-guide-adr.md:125-131` binds
   beadhive to the `agentguides.io/schemas/0.1/*` family, and `:177-179` names the risk: "These
   schemas are pre-1.0 and may move; the mitigation is that all three are declared per-file by
   `$schema` comment, so a version bump is a mechanical edit and a re-validation rather than a
   rewrite." That mitigation holds **only because beadhive's coupling is to a declarative schema
   it ships as data**. Depending instead on a *backend interface* (a Python ABC that has already
   grown from the spec's 6 published methods at `SPEC.md:559-564` to 10 in the runtime at
   `base.py:46-105`, i.e. a **+67% drift** while the spec text stayed at v0.1) would convert a
   sed-able `$schema` bump into a code dependency on an unversioned, still-moving surface —
   for a component beadhive does not install and cannot select (Evidence 7).
9. **Corpus-collision hazard, noted for the record.** A beads-backed run is created as
   `issue_type="epic"` with `external_ref=guide_id` (`runtime/src/agentguides/state/beads/
   backend.py:167`) and steps as child `issue_type="task"` beads (`backend.py:325`). If an
   operator ever pointed `GUIDE_STATE_BACKEND_PATH` at a beadhive hive rather than a separate
   corpus, every Guide run would surface as an epic in `bh work ready` and the dispatch plane.
   The fix is a sentence of documentation (keep the Guide corpus at its own `state_path`), not a
   molecule.

## Boundary table

| Workflow | Substrate | Structural reason |
|---|---|---|
| **Setup Guide** (`src/beadhive/assets/guides/setup/`, 12 steps + nested `guides/rescue/`) | **Guide, alone** | Needs judgment + verification + retry: 7/12 steps verify by `agent_judgment`, all 12 have `on_failure` and `interactions` (Evidence 6). And beads is *literally unavailable* — the Guide runs on a machine with no `bh`, no hive and no `bd`; step `080-first-hive` is where a bead store first exists. A beads-backed work item cannot track the workflow that creates the bead store. |
| **`github-app-tier-provision`** (`beadhive/infra:guides/…`, 7 steps) | **Guide, alone** | Two `performer: human` prerequisites (`GUIDE.md:22,31`) and an execution trace over credential/tier operations. Beads adds nothing: the run is single-session and needs no place in the dependency graph. |
| **`backfill`** (`claude-plugin/beadhive/skills/backfill/`, 5 steps) | **Both — cleanly, already** | The interesting case, and it is already solved without formula. The Guide is the *execution trace* (classify → propose → human confirm → apply); its **product** is durable beads. Two sources of truth do not arise because they answer different questions: the run log records *how the reconcile reasoned*, the filed beads are *what exists*. Note `GUIDE.md:12-14` — "A re-run of the reconcile proposes zero changes" — the run is idempotent, so nothing is lost by not tracking the run itself as work. |
| **Release cut** (`just bump` + `.github/workflows/release.yml`) | **Neither** | Already fully mechanized in CI, and its human gate is already an approval gate: `release.yml:20`, `environment: pypi-prod  # approval gate`. Formula's `gate:` primitive would re-model in beads a gate GitHub already enforces, with no executor behind it. |
| **`onboard.py`** (`src/beadhive/onboard.py`, 1,562 lines) | **Neither — it is imperative code** | A two-phase DAG executor whose nodes hold real `action` callables and read-only `Check` predicates, batch-failing preflight before any mutation. A formula `Step` has no `action`. Its own docstring (`onboard.py:14`) says it is "onboarding-specific by design — NOT a generic workflow engine." Re-expressing it as a formula would produce a work-item shadow of code that still has to run, i.e. exactly the double-bookkeeping the epic asked about. |

**The both-substrates rule** (from the `backfill` row): compose them by **layer, not by mirror**
— a Guide run may *produce* beads, and (via the shipped beads backend) may *be stored in* a bead
corpus, but a Guide run must never be *shadowed by* a hand-maintained parallel bead. The run log
owns "how it went"; beads own "what work exists." Mirroring is the only shape that creates two
sources of truth, and no beadhive workflow needs it.

## Verdict — **NO-GO**

**No formula/wisp adoption is warranted.** Two independent blockers, either sufficient:

- **The niche is already filled, upstream, by shipped code.** The one GO candidate was
  beads-as-Guide-StateBackend, and it is not a parked ADR to push on — `beads.local-cli` shipped
  in agentguides runtime v0.2 and is live at v0.5.12 (Evidence 1). Beadhive would be funding a
  contribution that landed three minor versions ago.
- **The retention gap it was meant to close measures zero, and wisp would not close it anyway.**
  0 bytes of run log on this machine, 1.9 KB mean per real run, O(machines) run cardinality
  (Evidence 3) — ~550,000 runs to reach 1 GB. And wisp explicitly targets workflows "without
  audit value" and is not git-synced (Evidence 4), which is the opposite of what a Guide run is;
  its lifecycle is also unexercised and self-contradictory in its own help text (Evidence 5).

Beyond retention, formula adds **nothing** to beadhive's five real workflows: three want Guide's
executable/judgment shape that a `Step` cannot express (Evidence 6), the release cut's gate is
already a GitHub environment approval (`release.yml:20`), and `onboard.py` is imperative code a
non-executing formula could only shadow. Even the residual "Guide has no delete" fact
(Evidence 2) is not beadhive's to fix: beadhive neither installs the runtime nor selects the
backend (Evidence 7).

## Recommendation

1. **Close the operational-substrate question with an ADR** in `docs/design/` (the epic's
   third option), carried by decision bead `bh-gj0v9.4`. It should record: Guide is beadhive's
   operational-runbook layer; beads are its work-item layer; a workflow needing both composes by
   layer (Guide run *produces* beads, per `backfill`) and never by mirror; formula/wisp is
   declined with the sizing above so the question does not get re-litigated from intuition.
2. **Correct the stale premise in the epic before `bh-gj0v9.4` synthesizes.** Epic `bh-gj0v9`
   finding 5 ("parks it (ADR 008); no beads code path exists in the Guide runtime") is false as
   of runtime v0.2; the spec text at `SPEC.md:567` is what is stale, not the runtime. Any verdict
   that reads finding 5 literally will over-value the beads-StateBackend option.
3. **If retention is ever actually wanted, it is a one-liner, not an engine.**
   `find ~/.guide/state -path '*/runs/*.md' -mtime +N -delete`, or an upstream `guide state
   prune` RFC against the append-only decision. Revisit only if a real corpus is observed past
   ~100 MB (≈55,000 runs at the measured mean). Filing that today would be building a GC for a
   directory that does not exist.
4. **Two sentences of documentation, no beads needed.** (a) If anyone configures a beads-backed
   Guide corpus, point `state_path` at its own store, never at a hive — run beads are created as
   `issue_type=epic` and would otherwise surface in `bh work ready` (Evidence 9). (b) Keep
   beadhive's coupling to agentguides at the `$schema` layer that Decision 4's mitigation
   actually covers; do not take a code dependency on the runtime's `StateBackend` ABC while the
   published spec interface and the implemented one differ by 4 methods (Evidence 8).
