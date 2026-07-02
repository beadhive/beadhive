---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: emit-proposal
  title: Emit the reconcile diff and gate on human review
  requires: [classify]
  performer: agent
  action:
    type: manual
  verify:
    type: human_confirm
  interactions:
    - id: review-proposal
      when: after
      kind: confirm
      prompt: |
        The reconcile proposal is ready. It lists, for review:
          • PRESENT — existing beads to gain an external_ref link (no new bead, no
            origin:backfill; these are native).
          • NEW — beads to be created (closed if history says done), with
            origin:backfill + source:<kind>.
          • DRIFT / doc-gap — flagged, not auto-changed.

        Nothing has been written yet. Approve applying this proposal?
      required: true
  on_failure:
    strategy: ask
  effect: read-only
  estimated_duration_minutes: 4
  tags: [suggest, gate]
---

# What this step does

Renders the classified set into one reviewable diff — the SUGGEST half of the loop. Three
sections:

- **Stamps (PRESENT):** `bead → external_ref = <doc path>`, one row each. Deterministic + fuzzy
  matches combined, bridge noted.
- **New beads (NEW):** proposed `title`, `type`, `status`, `--external-ref`, and the
  `origin:backfill` + `source:<kind>` labels.
- **Flags (DRIFT / reverse-drift / doc-gap):** noted for the human, no action taken.

Then it **stops** on a human confirm.

# Why this is a hard gate

This is the single point where the run crosses from read to write. The v0.1 `human_confirm`
verification makes the gate a first-class part of the walk, not a convention — the run cannot
proceed to apply without a recorded human decision.

# End states reachable from here

- Human approves → continue to `apply`.
- Human declines → the walk terminates at **`proposal-only`** (the diff is the delivered
  artifact; still a clean, scored exit — a decision to not apply is recorded, not hidden).

# Notes

Read-only. The proposal is emitted to the run log / a scratch file for the human to inspect; no
bead is touched.
