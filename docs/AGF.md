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

## Planning plane — upstream of the integration loop

Before a coordinator assigns beads, the **planning plane** turns a raw idea into a
molecule: a gated, dependency-linked swarm the integration loop can execute.

```text
ideate → research → architecture → decompose → file molecule
```

This runs in a **human-interactive session** — not inside a worktree, not a coordinator.
The `planner` skill is the cartographer; for *deep* tiers it spawns the `analyst`
sub-agent for codebase + web research before decomposing.

**Two gates, by design:**

- **Plan approval** — `ws plan file <spec>` compiles the spec into beads and opens the
  kickoff gate.
- **Kickoff approval** — `ws plan approve <epic>` resolves the gate; only now do the
  molecule's root beads surface in `bd ready` for a coordinator.

**Fidelity spectrum** — auto-classified at intake, confirmed with the human:

- *quick* — small fix / refactor (≈2–4 issues): inline spec, dry-run, file.
- *spec* — medium feature (≈5–15 issues): YAML spec authored and checked.
- *deep* — cross-cutting epic: `analyst` sub-agents research first.

See [PLANNING-PLANE.md](PLANNING-PLANE.md) for the full design, spec format, and verb
surface.

## Molecule integration branch (two-level)

Each kicked-off molecule gets its own integration branch (`mol/<epic>`), created by
`ws plan approve`. Bead merges land into `mol/<epic>`; only when the molecule is whole does
`ws work merge <epic> --molecule` validate the assembled branch and land it on the
always-green integration line as **one `--no-ff` bubble**. This keeps `main` untouched
and always-green until an entire molecule is ready — two levels: bead merges inside the
molecule bubble, molecule bubble on `main`. A bead with no `mol/<epic>` branch still
targets `main` directly (backward-compatible).

See [PLANNING-PLANE.md](PLANNING-PLANE.md) for how kickoff creates the branch and
[WORK.md](WORK.md) for the full `--molecule` verb mechanics.

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
