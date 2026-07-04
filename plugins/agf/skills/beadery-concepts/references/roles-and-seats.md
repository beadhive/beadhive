# Roles & seats

How the factory's org model works: what a role is versus a seat, the seven seats and their
duties, the Gas Town naming layer, and how a seat is launched.

## Role vs seat

- A **role** is the archetype — the definition of a job (its duties, its skill, the tools and
  model it can reach). It is abstract and reusable.
- A **seat** is a role *instance* bound to a running session: a role plus an **identity**
  (`coord/<name>` for a coordinator, `crew/<name>` for a developer), a **worktree**, and a
  **rig**. The same role can be seated many times concurrently — many developers, one per bead —
  each its own seat with its own identity and worktree.

## The seven seats

| Seat | Duty |
|---|---|
| **planner** | Turn a raw idea into a gated molecule (ideate → research → architecture → decompose → file). |
| **coordinator** | Dispatch ready beads to developers, watch review gates, re-dispatch, serialize merges. |
| **developer** | Take one assigned bead to a validated, reviewable state in its worktree, then submit. |
| **reviewer** | Walk an approved branch — read intent + change, run tests and a demo, resolve or bounce the gate. |
| **merger** | Serialize approved beads onto the always-green integration branch, `--no-ff`, preserving history. |
| **analyst** | Fire-and-forget read-only research sub-agent for the planner (codebase + web); never implements. |
| **superintendent** | Commission and configure rig sites across the workspace; report to Head Office. |

The **epic-coordinator** / **epic-coordinator-deep** are collapsed-dispatch variants of the
coordinator, not additional seats — see the collapsed-dispatch design in
[dispatch-and-scheduling.md](dispatch-and-scheduling.md).

## Gas Town names — five seats only

A subset of seats carries a Gas Town nickname. Only these five have one; the rest have none —
there is no Gas Town name for reviewer, superintendent, or analyst.

| Seat | Gas Town name |
|---|---|
| developer | polecat |
| coordinator | overseer |
| merger | the Refinery |
| planner | the cartographer |
| epic-coordinator | the pit crew |

## The `contributor` seat _(roadmap)_

The **`contributor`** is a dedicated, persistent, rig-scoped seat for the Contribution plane —
built on the read-only analyst research primitive but owning a target-repo dossier and driving
gated upstream PRs over external rigs. See the Contribution plane in
[agf-and-planes.md](agf-and-planes.md).

## Role modes — launching a seat as the main loop

Any AGF seat can run as the **main** Claude Code loop instead of as a task-spawned sub-agent.
Two equivalent entry points:

- `bdry role <seat>` — exports the role, then execs the seat's agent definition.
- `claude --agent agf:<seat>` — resolves the seat definition from the `agf` Claude Code plugin
  (a local `.claude/agents/<seat>.md` override outranks the plugin).

When a seat launches as a role mode, its definition body becomes the system prompt, its
frontmatter preloads the role skill (plus `work` for every seat except superintendent), and its
tools / model fields scope what the seat can reach.

## Coordinator vs epic-coordinator — dispatch vs implement

The distinction is **who does the coding**:

- A plain **coordinator** *dispatches only*: it never implements a bead itself. It fans each
  ready bead out to a developer sub-agent in that bead's own worktree, watches the gate, and
  serializes the merge.
- A collapsed **epic-coordinator** / **epic-coordinator-deep** *implements*: it works every ready
  bead of one epic sequentially in one shared worktree, merging the set once. The deep variant
  additionally holds an escape valve to kick a single risky bead back out to its own worktree.

Which seat runs is decided by seat-typed, depth-bounded dispatch — a leaf bead goes to a
developer, an epic to a coordinator, and a collapsed epic to an epic-coordinator variant chosen
by `work.dispatch.max_depth`. See [dispatch-and-scheduling.md](dispatch-and-scheduling.md).
