# Planning-seat UX + spike loop — design ADR

> Status: **decided.** This is the decision record for making the planning plane a
> one-keystroke seat in Claude Code and for wiring the spike loop
> (plan → spike → verdict → replan → implementation molecule) into AGF as a first-class
> *convention* (not a schema change). It captures the chosen UX, the spike-loop shape, the
> alternatives rejected, and the rationale. Execution is tracked by two molecules — one in
> this rig (docs) and one in the `bh-cp` rig (`beadhive/claude-plugin`); this doc only
> records *what* was decided and *why*.

## Problem

Two gaps, one symptom ("I keep having to re-specify that the output is beads, not code"):

1. **No entry point.** The planner seat exists (`bh:planner` skill + agent, `bh plan`
   check/file/approve/verify, two gates) but the claude-plugin ships **zero slash
   commands**, so every ideation session starts in Claude Code's default plan mode — which
   is code-shaped — and the human must restate the seat contract each time.
2. **The spike loop is convention-only and unwired.**
   [`docs/spikes/fekf-10-resumable-agent.md`](../spikes/fekf-10-resumable-agent.md)
   established the spike format (Question → Method → Evidence → Verdict GO/NO-GO →
   Recommendation), but nothing connects spike → decision → re-entry into planning, and
   nothing covers mid-execution replanning when a blocker invalidates part of a molecule.

## Decision

| Axis | Chosen |
|---|---|
| Seat contract | A planning session outputs exactly two artifact types: **beads** (molecule specs compiled by `bh plan file`) and **decision records** (`docs/design/*.md`). Never code. Stated normatively in AGF.md; any harness entering the seat loads the contract, the human never restates it. |
| Entry UX (Claude Code) | **Three slash commands** in the claude-plugin — `/bh:plan <idea>`, `/bh:replan <epic>`, `/bh:groom` — each opening with the seat-contract banner and loading the `bh:planner` skill **inline in the main thread** (planning is human-interactive; a Task subagent can't converse). |
| Persistent seat mode | A `planning-seat` **output style** shipped by the plugin pins the contract for a whole session. Implementation must verify plugins can vend output styles; fallback: command markdown as the persistent contract + a PreToolUse hook that soft-warns on `Edit`/`Write` outside `docs/`/spec paths. |
| Spike shape | **Two-molecule loop.** File a small spike molecule (spike beads + one decision bead). On GO, `/bh:replan <spike-epic>` files the implementation molecule informed by the spike docs. On NO-GO, record the ADR and close. No speculative implementation beads that a NO-GO would orphan. |
| Re-entry verb | **`replan`** — the single verb for ANY mid-flight plan alteration. Spike verdicts are one trigger; a blocker, review bounce, or mid-execution discovery re-enters planning through the same door. |
| Spike support level | **Convention only.** `tag:spike` / `tag:decision` labels + `docs/spikes/TEMPLATE.md` + skill/doc updates. Zero changes to `plan.py` / `molecule.py` / the bead type set. |

## The pipeline as a state machine

```text
ideate → design ─→ feasibility settled? ──yes──→ file implementation molecule → kickoff → dispatch
                        │ no (open GO/NO-GO question)
                        ▼
              file SPIKE molecule (spike beads + decision bead)
                        ▼
              integration plane executes spikes (normal dispatch)
                        ▼
              decision bead closes with verdict
                 GO ──→ /bh:replan <spike-epic> → implementation molecule
                 NO-GO → ADR in docs/design, close, done
```

### Spike-loop conventions

- **Spike bead**: `type: task`, label `tag:spike`, acceptance =
  "`docs/spikes/<bead-id>-<slug>.md` exists with Question / Method / Evidence /
  Verdict (GO|NO-GO) / Recommendation sections; no product code."
- **Decision bead**: label `tag:decision`, `deps:` on all spike beads in the molecule. Its
  description instructs: read the spike docs; on GO run `/bh:replan <epic>`; on NO-GO
  record the ADR and close with reason. The close reason carries the verdict.
- **Spike epic**: also labeled `tag:spike`, so `bh plan status` distinguishes spike
  molecules at a glance.
- **Re-entry linkage**: the implementation epic's description/`external_ref` links back to
  the spike epic (provenance, mirroring the intake-adopt pattern).

### Command modes

| Command | Mode |
|---|---|
| `/bh:plan <idea>` | New molecule. Fidelity triage gains a **spike branch**: if architecture surfaces an unresolved GO/NO-GO question, propose a spike molecule instead of guessing. |
| `/bh:replan <epic>` | Re-enter planning for an existing molecule when new evidence invalidates or completes part of it. Two triggers, one verb: **(a)** spike verdict landed — read spike epic + `docs/spikes/` artifacts, carry the verdict into architecture, decompose the implementation molecule; **(b)** mid-execution blocker/discovery/decision — amend the live molecule: supersede/close invalidated beads, re-dep, file follow-on beads. Always: gather the triggering evidence first, restate what changed, then alter beads. |
| `/bh:groom` | Backlog-wide fixup: reconcile existing beads with new discussion/decisions/ADRs — `bd update`/`supersede`/`close`/re-dep. Distinct from replan: groom is hygiene across the backlog with no single triggering epic; replan is scoped to one molecule with a triggering event. |

## Alternatives considered and rejected

### Single gated epic for spikes (spike + implementation beads in one molecule)

**Rejected.** Filing implementation beads before the spike proves them right creates
speculative beads a NO-GO orphans (mass-close with reasons, polluted history). The
two-molecule loop keeps every filed bead honest: nothing exists in the tracker that the
current evidence doesn't support. The `bd gate` machinery this would have reused is the
same machinery kickoff gates already exercise, so nothing is lost.

### First-class CLI support (`spike` bead type, `bh plan spike`/`replan` verbs, spec-schema fields)

**Rejected for now (YAGNI).** The existing spec YAML + labels + gates already express the
loop; the fekf-10 spike ran fine on pure convention. Encoding it in `plan.py`/`molecule.py`
before the workflow shape is proven under use buys typing safety at the cost of real code
churn. Upgrade path: if the convention holds after a few spike cycles, promote `tag:spike`
handling into `bh plan verify` / a `bh plan replan` verb.

### `plan-resume` / `revise` / `pivot` as the re-entry verb

**Rejected.** `plan-resume` scoped the verb to spikes only, but mid-execution blockers need
the same re-entry door; `revise`/`pivot` don't name the artifact being changed. `replan`
names the artifact (the plan/molecule), reads as one verb, and stays in the `plan` family.

### One `/bh:plan` command with argument-driven modes

**Rejected in favor of separate commands per mode.** Distinct commands are discoverable in
the command palette, keep each command file short and single-purpose, and let `/bh:groom`
(no epic argument) and `/bh:replan <epic>` (required argument) have honest signatures.

### Planner as a Task subagent for these sessions

**Rejected.** Planning is human-interactive by design (every stage is a checkpoint with
loop-back); a subagent can't converse with the human mid-flow. The `planner` *agent*
definition remains for programmatic contexts (e.g. a director routing intake), but the
slash commands load the skill inline in the main thread.

## Consequences

- Execution is two molecules: **`bh` rig** — AGF.md/PLANNING-PLANE.md spike-loop + seat
  contract docs, `docs/spikes/TEMPLATE.md`; **`bh-cp` rig** — planner SKILL.md mode
  sections, three command files, the `planning-seat` output style (with support
  verification + hook fallback), plugin version bump/release.
- The seat contract is harness-independent: the AGF.md statement is the normative source;
  the Claude Code commands are one binding of it. Other harnesses (codex) bind the same
  contract their own way.
- The fekf-10 spike doc is retroactively the canonical example of the template.
