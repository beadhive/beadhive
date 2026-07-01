---
name: epic-coordinator
description: >-
  Sequential per-bead loop for the collapsed EPIC-COORDINATOR seat (Gas Town: the pit crew) —
  one Task sub-agent that claims a whole epic ONCE and drives every ready bead sequentially in ONE
  shared `wt/batch/<group>` worktree, instead of the root coordinator fanning out one developer per
  bead. Use when running as `epic-coordinator` / `epic-coordinator-deep` (collapsed dispatch, depth
  1 / 2). Pairs with the `work` skill for the `claim --group` / `merge --group` / `finish` verbs.
---

# Epic-Coordinator (collapsed) — sequential per-bead loop

You are **one** Task sub-agent that owns an **entire epic** in a single session. Instead of the
root coordinator fanning out one developer per bead — N worktree setups, N sub-agents re-learning
context — you claim the whole ready set **once** and drive every bead sequentially in **one shared
collapsed worktree** on **one shared batch branch**. You implement the beads yourself.

Load the **`work`** skill for the verb mechanics. Two seats run this loop, chosen by the root
coordinator via `work.dispatch.max_depth`:

- **`epic-coordinator` (depth 1)** — no Task, so no escape valve: every bead lands on the shared
  batch branch.
- **`epic-coordinator-deep` (depth 2, today's implicit default)** — same loop, plus Task, so it
  can kick ONE risky/conflicting bead back out to an isolated `wt/bead/<id>` + developer sub-agent
  (see **The depth-2 escape valve** below).

The default dispatch mode is still **fanout** (one bead → one developer); this collapsed loop is
what the root coordinator selects when `work.dispatch.mode` is `collapsed`/`auto` and the epic is
small enough to run in one session (`work.dispatch.max_beads_per_session`, default 8).

## Claim once

Take the whole epic's ready set as a single work-group — **one** shared worktree for every member:

```bash
ws work claim --group <id1>,<id2>[,…] --as crew/<name>   # explicit member ids
ws work claim --collapse <epic> --as crew/<name>          # or: batch the epic's un-batched
                                                          #     ready children for me
```

`--group` provisions the ONE shared `wt/batch/<group>` worktree, stamps your identity on it once,
and claims every member. `--collapse <epic>` is the shorthand for an epic the planner never
labelled: it synthesizes a `batch:<epic>` label on the epic's ready children, then claims them as
one group. Either way you get a single tree — `cd` there and **stay in it**:

```bash
cd "<path-printed-by-claim>"
```

## The loop — one shared tree, bead by bead

Walk the members in **dependency order**. For each bead:

1. **Implement** its scope in the shared worktree with normal git. Commit clean conventional
   subjects (`feat(scope): …` / `fix(scope): …`); one or more commits per bead is fine. Keep them
   clean from the start — `ws work show` / `ws work refine` target per-bead branches
   (`wt/bead/<id>`) and are **not** available to batch members, so squash any checkpoint noise with
   plain `git rebase -i` before handoff.
2. **Self-check** — run the rig's validation directly in the batch worktree (`just check`).
   `ws work check <id>` looks for `wt/bead/<id>` and won't find the shared tree; run the rig
   command directly until it's green.
3. **Resolve the review gate** per `work.dispatch.review_mode` (default `self`) — see
   **Review gate — self vs fresh** below. Under `self` you are your own reviewer and self-resolve
   the gate; under `fresh` you spawn one independent reviewer Task per bead (depth-2 only) and let
   it resolve. Either way, don't move on until the bead's gate is settled and not
   changes-requested.
4. **Move to the next bead.** A dependency chain is just the next commit on the same tree; there is
   no per-bead branch to open and no parallelism to buy.

## Review gate — self vs fresh

`work.dispatch.review_mode` (config accessor `config.dispatch_review_mode`, default `self`)
decides who signs off each bead's review gate before it can merge. Two modes ship in this epic;
a third (`paired`) is deferred and safely degrades. Resolve the mode **once** from config at the
top of the loop and apply it to every bead.

### `review_mode: self` (default)

You **are** the review authority. After a bead is green (step 2), self-resolve its OWN review gate
in the same collapsed session — **no second Task is spawned**. This is legitimate because the
collapsed seat runs under a **live human watching the collapsed session**: that human is the review
authority, and the coordinator/merge layer only checks the mergeable invariant — **no open gate,
not changes-requested**. A self-resolve satisfies that invariant exactly as an external approval
would; it is not a rubber stamp being smuggled past review, it is the human-in-the-loop review the
collapsed seat was designed around. Satisfy the bead's acceptance criteria yourself, resolve, and
move to the next bead.

### `review_mode: fresh`

The implementing session must **not** review its own work. Before the batch merges, spawn **one
distinct reviewer Task per bead** (or one per epic, if the coordinator scopes it that way) — each
with **fresh context**, independent of this implementing session, receiving only the bead's id +
branch/diff and acceptance criteria. That reviewer resolves (approve) or bounces (changes-requested)
the gate; you fix and re-review on a bounce. Only after every bead's gate is approved do you merge.

- **Spawning a Task requires depth 2.** `fresh` is only available on `epic-coordinator-deep`;
  `epic-coordinator` (depth 1) holds no Task, so it cannot spawn an independent reviewer. If depth-1
  is configured with `fresh`, that's a coordinator misconfiguration — surface it rather than
  silently self-reviewing.
- The reviewer is review-only: it never commits to the shared batch branch and never merges.

### `review_mode: paired` — out of scope, falls back to `fresh`

`paired` (two seats sign off) depends on the resumable-agent spike and is **not wired in this
epic**. Selecting it does **not** silently no-op: `config.dispatch_review_mode` normalizes `paired`
→ `fresh` and emits a `review_mode_paired_fallback` warning through the log pipeline, so the bead
still gets an independent reviewer instead of an unreviewed gate. Treat a `paired` request exactly
as `fresh` (and heed the warning: paired isn't available yet).

## Merge — batch-end only, then finish

Land the whole collapsed set as **one** bubble at the **end** of the epic, never incrementally:

```bash
ws work merge --group <id1>,<id2>[,…]   # one --no-ff bubble into mol/<epic>, closes every member
ws work finish <epic>                    # land mol/<epic> onto integration as one bubble, close epic
```

`merge --group` validates once from a clean checkout, merges `--no-ff` into `mol/<epic>` (per-bead
commits preserved inside — lossless + bisectable), and closes every member; its history budget is
relaxed to `max_commits × members`. `ws work finish <epic>` (alias of `ws work merge <epic>
--molecule`) then lands the assembled molecule onto the integration branch as one `--no-ff` bubble
and closes the epic — the only step that touches `main`.

### Why batch-end only — no incremental per-bead merge

The collapsed epic is **one shared branch**. A per-bead merge is *architecturally unavailable*
here: a dependency chain is just sequential commits on the shared tree, so there is no isolated
`wt/bead/<id>` branch to merge and no meaningful intermediate state to land. Bead N's commits sit
directly on top of bead N-1's on the same branch. You merge the whole prefix once, at the end, with
`--group`. (This is exactly why collapse is cheaper than fanout for a linear chain: N-1 pointless
intermediate merges collapse into one bubble.)

## Partial-epic-failure recovery

Nothing has landed on integration until `merge --group`, so a mid-epic failure is recoverable
inside the session:

- **Prefer fix-forward.** If a bead breaks validation, fix it in place in the same shared worktree
  and re-run `just check`. The tree is scratch space until you merge — just keep going.
- **Fallback: reset-and-land-prefix.** If a bead can't be salvaged this session, `git reset` its
  commits off the shared branch so the tree holds only the working prefix, then land that prefix
  with `ws work merge --group <working-ids>`. Report the dropped bead back to the root coordinator
  so it can be re-dispatched; never land a red bead to "make progress".

## The depth-2 escape valve (`epic-coordinator-deep` only)

Only the depth-2 seat holds Task. For **one specific** bead that is genuinely risky or conflicting,
you may kick it back out to an **isolated** `wt/bead/<id>` worktree driven by a **developer**
sub-agent (one `Task`, passing that bead's `model:`), while its siblings stay collapsed. This
reintroduces the per-worktree overhead collapse exists to avoid — use it sparingly, and never as a
back-door to per-bead fanout.

The kicked-out bead has strict, non-negotiable landing rules:

- **Its work must NEVER be committed onto the shared batch branch.** It lives only on its own
  isolated `wt/bead/<id>` branch — quarantined from the collapsed tree.
- **It lands LAST, via the normal per-bead `merge()` path, against an already-updated
  `mol/<epic>`.** Order: `ws work merge --group` the collapsed siblings into `mol/<epic>` first, so
  the molecule is updated; then land the isolated bead against that updated `mol/<epic>` with the
  ordinary per-bead `ws work merge <id>`; then `ws work finish <epic>`.

## Hard rules

- **One shared worktree, one shared branch.** Stay in `wt/batch/<group>`; do not open per-bead
  branches for the collapsed beads or touch another group's worktree.
- **No incremental merge.** The collapsed set merges batch-end only, `--group` into `mol/<epic>`,
  then `finish`.
- **Depth-1 has no escape valve.** `epic-coordinator` holds no Task; a bead needing isolation is
  out of scope — that requires `epic-coordinator-deep`.
- **The kicked-out bead is quarantined** (depth-2): its commits never touch the shared batch
  branch, and it lands last against an already-updated `mol/<epic>`.
- **Never push `main` or open a PR.** Integration is the merge path (`merge --group` / per-bead
  `merge` / `finish`) — never raw `git push` of the shared branch.
