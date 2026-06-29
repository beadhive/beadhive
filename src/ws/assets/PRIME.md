# Beads + ws work — repo conventions

This repo tracks work with **bd** (beads) and integrates it with git through **`ws`**. Two
rules before anything else:

- **Drive the lifecycle with `ws work`, not raw `bd` / `git`.** `ws work` takes a bead from
  assigned → merged and applies this repo's config defaults (identity, commit signing,
  validation, review gate) for you. Use **`ws bd`** (passthrough) for issue management
  (create / query / dependencies / close) — it auto-applies the `provider:`/`org:`/`repo:`
  triplet on create.
- **Raw `git` is only for local work** — the actual change *inside* a worktree. Never use
  raw `git` / `bd` / `gh` to drive the lifecycle (claim, submit, merge), or you bypass the
  defaults the tooling sets up for you.

Beads is **issues only** — knowledge/memory lives in the project's own system (no
`bd remember`).

## Load the skill for your role

Each role skill states its duties and the verbs it uses; all of them build on the shared
**`work`** skill (the `ws work` verb reference).

| Role | Gas Town | Skill | Duty |
|---|---|---|---|
| Developer | polecat | `developer` | take one assigned bead to a reviewable state |
| Coordinator | overseer | `coordinator` | dispatch beads to developers, watch gates, re-dispatch |
| Merger | refinery | `merger` | serialize merges to the integration branch, preserve history |

## Conventions

- Every issue's home is the `provider:`/`org:`/`repo:` triplet (auto-applied by
  `ws bd create`); `ws labels validate` checks it. Dependencies:
  `ws bd dep add <child> <parent>`.
- `ws work` reads per-rig defaults from config — load the `work` skill for details.
- Run `bd prime` after compaction or in a new session to reload this context.
