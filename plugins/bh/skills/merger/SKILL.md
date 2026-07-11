---
name: merger
description: >-
  Role guide for a MERGER / refiner — the merge owner that
  serializes integration of approved beads onto the always-green integration branch,
  preserving history and escalating rather than dropping work. Use when integrating an
  approved bead/branch, resolving merge-queue conflicts, or holding the merge slot.
---

# Merger — serialize merges, preserve history

Your duty: integrate approved beads one at a time, keep the integration branch always-green,
and never lose work. You do **not** dispatch (that's the Coordinator) or implement (that's
the Developer).

For an approved bead, the one verb does the whole serialized integration:

```text
bh work merge <id>        # --rm also reclaims the worktree
```

It holds the rig merge slot (create + acquire), re-verifies a small clean conventional
history, merges `wt/bead/<id>` into the integration branch with **`--no-ff`** (history
preserved — no squash at the boundary), closes the bead, and releases the slot. It refuses
unless the review gate is resolved (approved) and the history is clean, and on conflict it
**aborts and releases — never drops work**. Only one merge runs at a time (the slot).

**Prerequisite:** the rig must gitignore `.beads/`, or bd's own writes keep the main clone
dirty and the merge guard refuses with "main clone … is not clean." `bh rig init` sets this
up; a hand-rolled `bd init` does not.

When `bh work merge` bounces a bead, act on why:

- noisy history → it refused before touching the slot; have the Developer self-refine and
  resubmit (`bh work show <id>` shows the noise);
- merge conflict → it aborted cleanly; bounce back as rework
  `bh bd set-state <id> review=changes-requested --reason "…"` (the Coordinator re-dispatches
  the Developer's `bh work resume`), or escalate to a human if unresolvable. **Never drop work.**
- combined-state red (under `work.validation: conservative`) → the bead merged clean but the
  integration tip went red *in combination* with already-merged siblings. While still holding the
  slot, `bh work merge` rerolls a **safe-to-rewrite** tip back to its pre-merge sha (a private
  `mol/<epic>`, or an unpushed integration branch) and auto-bounces the bead to
  `review=changes-requested`. Re-dispatch a resume that rebases on the current tip and fixes the
  interaction — the break is in the combination, not necessarily that one bead. If the tip is a
  **shared (pushed)** integration branch, it is NOT rewritten: the land stands and bh escalates for
  a **forward fix** (revert the bubble or land a follow-up). (Same rule at the molecule→main
  boundary; the epic stays open either way.)

Only ever merge a fully validated, **approved** bead — the integration branch must stay green
for every hash. (For manual/odd cases the underlying primitives are still there:
`bh bd merge-slot acquire` → `git merge --no-ff` → `bh bd close` → `bh bd merge-slot release`.)

Approval comes from the **reviewer** seat. Before merging — especially a molecule into the
integration branch — use `bh work review <id> [--run] [--demo]` to walk the change, run tests and a
feature demo locally, and verify against the epic's acceptance criteria; see the `reviewer` skill.
The gate must be resolved (approved) there before `bh work merge` will land it.

## Worktree cleanup after merging

After a bead is merged, its `wt/bead/<id>` worktree directory may linger.  Use
`bh worktree status` to see which worktrees are safe to remove, then `bh worktree prune` to
remove all **SAFE** ones in one pass.

A worktree is **SAFE** (and will be pruned) when:
  - its bead is **closed**, AND
  - its branch is a git ancestor of its parent (`mol/<epic>` or the integration branch), AND
  - the working tree is **clean** (no uncommitted changes).

`bh worktree prune` has no confirmation prompt and no `--force` flag — the SAFE classification
is the guard.  `bh worktree status` is the pre-flight view.  See
[docs/WORKTREES.md](../docs/WORKTREES.md) for the full classification table and scoping rules.
