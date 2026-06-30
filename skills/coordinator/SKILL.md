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
2. **Schedule: batch or singleton** — before assigning, decide *how to group* the molecule's
   work (see **Scheduling** below). `ws work schedule <epic>` prints the plan: each **group**
   (a planner `batch:<group>` or an auto-detected linear chain) runs as ONE grouped agent; the
   rest are **singletons** that fan out for parallel wall-time. Default stays one-per-worktree.
3. **Route each bead/group** — read its `model:` / `harness:` labels from `ws bd show <id> --json`
   (labels come back as a list). Default `model:opus`, `harness:claude` when unset. A group shares
   one tier (the scheduler guards that).
4. **Assign + provision** — `ws work assign <id> --to crew/<name>` stamps the assignee and
   provisions the worktree. Assignment alone leaves the bead `open`, so `in_progress` always
   means a live worker. For a group, the developer claims the shared batch worktree with
   `ws work claim --group <ids> --as crew/<name>` (8v8.2 mechanics).
5. **Fan out developers in parallel** — launch one `Task` per independent ready bead **or group**,
   in a single message, so they run concurrently:
   - `subagent_type: "developer"`, `model: <bead model>` (overrides the agent default per bead),
   - prompt: the bead id (or group ids) **and the `crew/<name>` you assigned in step 4** — the
     developer must `ws work claim <id> --as <that crew>` or claim refuses as a different actor
     (and the bead never flips to `in_progress`). Tell it to claim, run its loop, and submit.
   Distinct worktrees + per-agent identity mean parallel developers never clobber each other.
   The sub-agent ends at `submit` and reports back its branch + sha.
6. **Watch gates** — `ws bd ready --gated --json` surfaces beads whose review gate just closed:
   - **changes-requested** → relaunch a `developer` Task (same `crew/<name>`) that runs
     `ws work resume <id> --as <crew>`, addresses the feedback, and resubmits.
   - **approved** (gate resolved, no changes-requested) → merge it.
7. **Serialize merges** — `ws work merge <id>` (or `--group <ids>` for a batch) one at a time. It
   holds the rig merge slot, re-verifies clean conventional history, merges `--no-ff` (history
   preserved), closes the bead(s), and releases the slot. Never run two merges at once; never
   squash at the boundary.

**Parallel devs, serial merge** is the rule: development fans out; integration is single-file.

**Validation mode** (`work.validation`, default `relaxed`) tunes re-test aggressiveness per
molecule run: `conservative` re-validates the integration tip after *every* merge (catches which
serial merge broke the combination immediately, at the cost of an extra validation per bead +
post-land) and is worth it for wide same-file batches; `loose` trusts the per-bead submits and
skips the pre-land re-test (fastest, for well-factored independent work). On a re-validation red,
a safe-to-rewrite tip (private `mol/<epic>` or an unpushed branch) is rolled back and the unit
bounced; a shared (pushed) integration branch is left standing and escalated for a forward fix
(never rewritten). A landed molecule whose `main` moved underneath it is always re-validated
(staleness backstop), even in `relaxed`. See `docs/WORK.md` § Validation modes.

## Scheduling — batch vs singleton (the cost model)

The default unit is **one bead → one worktree → one developer → one merge**, and that is the
right call whenever beads are independent: distinct worktrees give you parallel wall-time and
each lands on its own clean conventional history. **Batch only when batching is genuinely
cheaper** — a *work group* runs several beads in ONE `wt/batch/<group>` worktree by one agent,
validated and merged **once** as a single `--no-ff` bubble (per-bead commits preserved inside, so
it stays lossless / bisectable; 8v8.1 is the data model, 8v8.2 the verbs).

Batching wins when a **trigger** applies AND the group stays **cohesive**:

- **Linear chain, no mid-point unit** — beads that build on each other with no testable/reviewable
  checkpoint until the end. A chain can't be parallelized anyway, so per-bead merges only add
  meaningless intermediate states; one bubble is strictly cheaper.
- **Same-file contention** — DAG-parallel beads all editing one file would collide on repeated
  separate merges. The planner declares these as a `batch:<group>` (it knows them at decompose
  time).
- **Expensive validation** — when integration-test setup costs more per session than implementing
  several cohesive beads serially and validating **once** at the end.

Otherwise keep singletons — independent + cheap-to-validate beads benefit from parallel wall-time.

**`ws work schedule <epic>`** computes this for you (read-only; `--json` for machine use). It:

1. **Honors planner batches** — any `batch:<group>` the planner declared (already cohesion- /
   size- / model-validated at plan time) with ≥2 members becomes one grouped agent.
2. **Auto-detects pure linear chains** — a run of beads connected by *private* `blocks` edges
   (no fan-in / fan-out), which nobody validated at plan time, so the scheduler re-applies the
   guards below before batching it.

Everything else is a singleton. Dispatch one developer `Task` per group / singleton.

### Guards (why a candidate is NOT batched)

- **Cohesion** — members must hang together (same component, or contiguous in the dep DAG). A
  grab-bag batch fails as a unit and is hard to review. (A private-edge chain is contiguous by
  construction; planner batches are checked at plan time.)
- **Size cap** — at most `work.batch_max_size` (default 5) members, so the bubble stays reviewable
  and bisectable. An overlong chain falls back to singletons.
- **Single model tier** — a group runs as one unit on one model; mixed `model:` tiers are refused.
- **No mixed review gates** — members must share a review gate; a chain mixing `gate:` overrides is
  refused (so one approval covers the whole bubble).

A candidate that trips any guard is dispatched as singletons instead — the cost model never forces
an incohesive or oversized batch. **Blast radius:** a batch fails (and bounces on changes-requested)
as a whole, so keep groups small and cohesive; that is the price of fewer merges/validations.

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
coordinator only dispatches and routes. The loop above is unchanged — step 7 just moves into
its own agent. See the `merger` skill.
