---
name: developer
description: >-
  Role guide for a DEVELOPER (Gas Town: polecat) — an agent assigned a single bead to
  implement and take to a reviewable state. Use when you've been assigned or claimed a bead
  and are about to start coding in a ws-managed repo, or when you would otherwise reach for
  `git clone` / `git checkout -b` / `gh pr create` to begin a task. Pairs with the `work`
  skill for the `ws work` verb mechanics.
---

# Developer (polecat) — take one bead to reviewable

Your duty: turn one assigned bead into a small, validated, reviewable change. You do **not**
dispatch work (that's the Coordinator) or merge it (that's the Merger).

Load the **`work`** skill for verb details, then:

1. `ws work brief <id>` — understand the requirements and the printed validation command.
2. `ws work claim <id>` — your ack; it gives you a worktree with identity + signing already
   stamped. **Don't `git clone` / `checkout -b`** — the branch is already `wt/bead/<id>`.
   `cd "$(ws worktree path --bead <id>)"`.
3. Implement with normal git **inside the worktree** (commit freely — it's scratch space).
   Tip: `git commit --fixup=<target>` as you go.
4. **Self-refine** before handoff: `ws work show <id>` to see the noise, then
   `ws work refine <id> --autosquash` (or `--plan`/`--since`) to squash checkpoints into a
   few clean conventional digests. It's a safe rewrite (backup branch + byte-identical gate),
   so `submit`'s history guard passes.
5. `ws work check <id>` — run validation; fix until green.
6. `ws work submit <id>` — hand off to async review. **Submit is not "done"**; your branch
   is the durable handoff, so don't rely on the worktree directory surviving.
7. `ws work resume <id>` — if review returns changes-requested; address it and re-submit.

Rules: stay inside the worktree; never push `main`, open a PR, or run the merge.

## Hitting a tool bug — bottom rung

If you hit a `ws` / `bd` / tool bug while working, fire a one-liner to HQ and keep going:

```
ws escalate '<what happened> with <tool>'
```

Fire-and-forget — do not stop to route or investigate. HQ queues it as `origin:escalation`;
the superintendent picks it up from `ws hq intake` and decides where it lands. Your job is the
bead, not the bug.

## Batch (work-group) path

When the coordinator assigns a `batch:<group>` of beads to you as a unit, use this opt-in
path. The default single-bead flow above is unchanged and is always the default.

**1. Claim the group** — one shared `wt/batch/<group>` worktree for every member:

```
ws work claim --group <id1>,<id2>[,...] --as crew/<name>
```

The command prints the worktree path and the group name. `cd` there immediately:

```
cd "<path-printed-by-claim>"
```

**2. Implement serially** — for each member in order, edit that bead's scope then commit
with a clean conventional subject:

```
git add -p
git commit -m "feat(scope): what and why"
```

One or more conventional commits per bead is fine. Keep them clean from the start —
`ws work show` and `ws work refine` target per-bead branches (`wt/bead/<id>`) and are not
available for batch members. Checkpoint noise must be squashed with plain `git rebase -i`
before handoff.

**3. Validate once** — run the rig's validation command directly in the batch worktree:

```
just check
```

`ws work check <id>` looks for `wt/bead/<id>` and won't find the batch worktree; run the
rig command directly until it's green.

**4. Merge the group** — land the batch as one bubble and close all members:

```
ws work merge --group <id1>,<id2>[,...]
```

`merge --group` validates once from a clean checkout, merges `--no-ff` into the molecule
base (per-bead commits preserved inside, lossless + bisectable), and closes every member.
The history budget is relaxed to `max_commits × members`.

**Batch rules:** stay in the shared worktree (`wt/batch/<group>`). Do not run
`ws work submit <id>` on batch members — it expects `wt/bead/<id>` which doesn't exist in
batch mode. Never open per-bead branches or touch another group's worktree.
