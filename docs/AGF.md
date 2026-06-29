# Agentic Git Flow (AGF)

This repo authors `ws`, the **integration-plane driver** for AGF, and is driven by it.
Don't improvise raw `git` / `gh pr` for the lifecycle — drive beads through `ws work` and
load the role skill for the seat you're in. The basics, so you can start without re-reading.

## Tenets (the why)

- **Two planes, kept separate.** *Integration* is high-frequency and dirty: each bead gets a
  worktree off the integration tip, and lands on an **always-green** line. *Release* is a
  separate, deliberate, gated act. **Merging is not releasing.**
- **Lossless history.** Agents do the merging, so we keep audited history: merge `--no-ff` at
  the boundary, **never squash there**.
- **Tiered retention.** Squash only *local checkpoints* into a few clean conventional-commit
  digests *before* merge; the integration ledger is preserved forever.
- **Unit of work = a bead.** Worktree → implement → refine → check → submit → review → merge.

## The loop (one Claude Code terminal)

A **coordinator** finds ready beads, assigns + provisions worktrees, launches **developer**
sub-agents (model per bead), watches review gates, and serializes merges via the **merger**.
Parallel devs, serial merge.

## Progressive disclosure — load what the seat needs

- `Skill: coordinator` — dispatch loop (overseer): ready → assign → fan-out devs → gate → merge.
- `Skill: developer` — implement one assigned bead in a worktree → submit (claim `--as <crew>`).
- `Skill: merger` — serialize approved beads, `ws work merge`, `--no-ff`, never drop work.
- `Skill: work` — `ws work` verb reference.

See also `ws work --help` and [WORK.md](WORK.md) for the full lifecycle and verb mechanics.
