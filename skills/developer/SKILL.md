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
