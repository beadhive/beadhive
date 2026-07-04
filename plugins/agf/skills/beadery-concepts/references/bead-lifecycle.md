# Bead lifecycle

The unit of work and how it moves from assigned to merged. All lifecycle commands are
`bdry work …` — raw `git` is only for the change *inside* a worktree, never the lifecycle around
it.

## Bead — the unit of work

A **bead** is a single issue: the atomic unit of work the factory schedules, implements, and
merges. Beads live in the rig's beads DB (managed via `bd`/`beads` under the hood) and are driven
through their lifecycle by `bdry work`, which composes `bd`, worktrees, and per-agent identity
and applies the rig's config defaults (identity, signing, validation, review gate) so you do not
pass them by hand.

## Molecule, swarm, workstream

- A **molecule** (the AGF term) is an epic plus its child issues plus their dependency DAG — a
  gated, dependency-linked unit the integration loop executes. **Swarm** is the beads-primitive
  term for the same structure.
- A **workstream** is an epic-of-epics: an `issue_type=epic` bead whose children are themselves
  epics. There is no new type — the tier is just the position in the dotted id.

## Container branches & the integration_base climb

Every bead — leaf or container — has exactly one branch under the unified namespace
**`wt/bead/<type>/<id>`** (`<type>` ∈ `epic` | `issue`; stable, no time/hash tail):

- A **leaf** lives at `wt/bead/issue/<id>`.
- A **container** (an epic, at any tier) lives at `wt/bead/epic/<id>` and *is* both the
  coordinator's seat worktree and the integration line its children fork from and land on.

A coordinator opens a container with `bdry work start <epic> --as coord/<name>`, which provisions
the seat worktree on `wt/bead/epic/<epic>` (forked off its integration base) and takes the epic
seat. Child beads fork off the container, so bead B sees bead A's already-merged work.

**Integration target = the `integration_base` climb.** A bead's fork/land target is resolved by
walking the dotted `<parent>.<n>` id chain to the **nearest started container ancestor**
(`wt/bead/epic/<parent>`), falling back to the rig integration branch (`main`) at the dotless
root. So a leaf lands on its epic, an epic lands on its workstream, and a workstream lands on
`main` — **one recursive rule**: `bdry work finish <container>` lands `wt/bead/epic/<container>`
up one level and tears the seat down.

## The lifecycle verbs

```text
brief → assign → claim → (work) → show → refine → check → submit → [review/approve] → resume → (merge/finish)
```

| Verb | What it does |
|---|---|
| `bdry work brief <id>` | Print the bead's requirements/goals + the validation command. Read-only. |
| `bdry work assign <id> --to <name>` | Orchestrator: stamp assignee + provision the worktree (epic → `coord/<name>`, else `crew/<name>`). |
| `bdry work claim <id> [--as <name>]` | Worker ack: re-attach/provision the worktree with identity + signing, then mark in-progress. |
| `bdry work show <id> [--view V]` | Render the bead branch's local history to judge noise before submit. Read-only. |
| `bdry work refine <id>` | Squash local checkpoint noise into clean conventional digests behind a backup branch + byte-identical gate. |
| `bdry work check <id>` | Run the rig's validation against the worktree; propagate its exit code. |
| `bdry work submit <id>` | Verify clean history, validate from a clean checkout, set `review:pending`, open the review gate. Handoff — not "done". |
| `bdry work resume <id>` | After changes-requested: re-attach a fresh worktree, print the feedback, re-assert the claim. |
| `bdry work abandon <id>` | Release the claim and record the abandon (the recovery path). |
| `bdry work merge <id>` | **Merger-owned.** Land the bead into its container as one `--no-ff` bubble. |
| `bdry work finish <epic>` | **Merger-owned.** Land an assembled container up one level as one `--no-ff` bubble, close it, tear down the seat. |

**Merge is a separate role.** The merger owns `merge` / `finish`, gated by a **merge-slot** so
lands serialize, always `--no-ff`, and **never squashed** at the integration boundary
(tiered retention — the squashing happens worker-side in `refine`, before submit). `abandon` is
the recovery path when a bead cannot be salvaged.

## Review gates

At `submit` the bead's review gate opens; its type is set by the rig's config (`review_gate`):

- **human** — a person resolves the gate (approve or changes-requested).
- **timer** — the gate auto-resolves after a configured interval.
- **gh:run** — a GitHub CI run must go green; `submit` pushes the branch so CI sees it.
- **gh:pr** — a GitHub pull request gates the merge; `submit` pushes the branch.

A gate that returns `changes-requested` bounces the bead back to `resume`; a bead cannot merge
until its gate is resolved and not changes-requested.
