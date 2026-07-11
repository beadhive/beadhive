---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: classify
  title: Resolve the judgment residual — fuzzy match, NEW vs noise, drift
  requires: [collect-and-bridge]
  performer: agent
  action:
    type: manual
  verify:
    type: agent_judgment
  on_failure:
    strategy: ask
  effect: read-only
  estimated_duration_minutes: 6
  tags: [classify, judgment]
---

# What this step does

Takes the previous step's `UNMATCHED` / `DRIFTED-ref` rows — the residual the tool refused to
guess — and resolves each with judgment the tool cannot supply. Each `UNMATCHED` row is followed
by up to three `CANDIDATE` rows (the fuzzy shortlist, ranked by title-token overlap): the tool has
already narrowed 200-odd beads to a handful, but the pick is yours. Confirm a candidate only if
the doc and the bead are genuinely the same work — a high overlap score is a hint, not proof
(recall that an exact bridge once matched a doc whose title shared almost nothing with its bead).
For every residual doc, decide:

- **PRESENT (fuzzy)** — one of the candidates (or another bead you know) is the same work and you
  are confident. Add it to the stamp list (treat like `PRESENT-needs-stamp`). Record which
  candidate and why. If no candidate fits, do not force one — it is NEW.
- **NEW** — no existing bead represents this work. It becomes a bead in the proposal, created
  **closed** if history says the work is done, carrying `origin:backfill` + `source:<kind>`.
- **NOISE** — the work is already covered by a coarser bead (a version/phase epic). Link to that
  epic; do not create a new bead.
- **DRIFT** — a matched bead disagrees with its source. Note the delta as a proposed update
  (usually the bead gains detail). Reverse-drift (bead richer than the doc, e.g. a decision with
  no ADR) is a *doc* gap — flag it, do not mutate the bead.

Also scan commit history for significant delivered work with **no** doc and no covering bead —
those are NEW candidates too.

# Verification (agent_judgment)

Before declaring done, record in the run log, per residual item: the verdict, and the evidence
(the title matched, the epic that covers it, or why it is genuinely new). A verdict without
recorded reasoning is not acceptable — this is the step where a wrong call creates a duplicate or
buries real history.

# Why judgment, not script

Fuzzy similarity and "does this deserve a bead" are exactly the calls a deterministic tool gets
wrong. Keeping them here — explicit, logged, human-auditable — is what makes the tool safe to
trust for everything else.

# Notes

Read-only. Output is a classified list feeding the proposal; still nothing written.
