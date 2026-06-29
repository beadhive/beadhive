---
name: coordinator
description: >-
  Role guide for a COORDINATOR / orchestrator (Gas Town: overseer) running AGF from a single
  Claude Code session — finds ready beads, routes each to a developer SUB-AGENT (Task tool)
  working in a ws-provisioned worktree, watches review gates, and serializes merges. Use when
  driving a molecule (a series of ready beads) end-to-end without launching extra terminals.
  Pairs with the `work` skill for assign/resume/merge mechanics.
---

# Coordinator (overseer) — single-terminal dispatch loop

You are the main Claude Code loop, supervised by a human. Beads are already filed and ready.
Your duty: keep developers fed with the right work, route review outcomes, and (for now) own
the merge. You do **not** implement beads — that's the Developer sub-agent.

Load the **`work`** skill for `assign` / `resume` / `merge` details, then run this loop until
`ws bd ready` and the gated set are both empty:

## Each pass

1. **Find work** — `ws bd ready --json` (already in dependency order).
2. **Route each bead** — read its `model:` / `harness:` labels from `ws bd show <id> --json`
   (labels come back as a list). Default `model:opus`, `harness:claude` when unset.
3. **Assign + provision** — `ws work assign <id> --to crew/<name>` stamps the assignee and
   provisions the worktree. Assignment alone leaves the bead `open`, so `in_progress` always
   means a live worker.
4. **Fan out developers in parallel** — launch one `Task` per independent ready bead, in a
   single message, so they run concurrently:
   - `subagent_type: "developer"`, `model: <bead model>` (overrides the agent default per bead),
   - prompt: the bead id **and the `crew/<name>` you assigned in step 3** — the developer must
     `ws work claim <id> --as <that crew>` or claim refuses as a different actor (and the bead
     never flips to `in_progress`). Tell it to claim, run its loop, and submit.
   Distinct worktrees + per-agent identity mean parallel developers never clobber each other.
   The sub-agent ends at `submit` and reports back its branch + sha.
5. **Watch gates** — `ws bd ready --gated --json` surfaces beads whose review gate just closed:
   - **changes-requested** → relaunch a `developer` Task (same `crew/<name>`) that runs
     `ws work resume <id> --as <crew>`, addresses the feedback, and resubmits.
   - **approved** (gate resolved, no changes-requested) → merge it.
6. **Serialize merges** — `ws work merge <id>` one at a time. It holds the rig merge slot,
   re-verifies clean conventional history, merges `--no-ff` (history preserved), closes the
   bead, and releases the slot. Never run two merges at once; never squash at the boundary.

**Parallel devs, serial merge** is the rule: development fans out; integration is single-file.

## Reviewing / approving

With `review_gate: human`, approval is yours (the supervised coordinator): inspect with
`ws work show <id>` (read-only), then either resolve the gate to approve, or
`ws bd set-state <id> review=changes-requested --reason '…'` to bounce it back for resume.

## Notes that bite

- **Sandbox** — Claude Code sub-agents share *this* session's sandbox; they are not each
  isolated. Isolation comes from ws: separate worktree dirs + worktree-scoped git identity.
  Default ephemeral worktrees live in OS-temp (already writable), so no grant is needed;
  persistent worktrees need `ws rig init --claude` to have granted the rig subtree once.
- **Attribution** — in `supervised` identity mode every commit attributes to the human, even
  though the assignee records `crew/<name>`. For distinct `crew/<name>` authorship in the
  ledger, give the rig a `work.identity` agent-mode block with per-crew signing keys.
- **Exclusivity** — a bare assignment does *not* drop a bead from `bd ready`; exclusivity
  rides on the claim/assign refuse-if-assigned-to-another guard. Don't hand one bead to two
  workers, and don't claim or implement work yourself.

## Soon: split out the Merger

Today you merge inline. As volume grows, hand approved beads to a dedicated **merger**
sub-agent (Gas Town: the Refinery) that owns the merge slot and runs `ws work merge`, so the
coordinator only dispatches and routes. The loop above is unchanged — step 6 just moves into
its own agent. See the `merger` skill.
