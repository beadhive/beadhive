# Agentic Git Flow (AGF)

This repo authors `ws`, the **integration-plane driver** for AGF, and is driven by it.
Don't improvise raw `git` / `gh pr` for the lifecycle — drive beads through `ws work` and
load the role skill for the seat you're in. The basics, so you can start without re-reading.

## Tenets (the why)

- **Three operational planes, kept separate.** *Control* commissions and configures rig sites
  (superintendent). *Planning* turns ideas into molecules (planner). *Integration* executes them
  (coordinator → developer → merger). Each plane has its own verb surface and seat; they hand
  off sequentially and never step into each other's role.
- **Integration vs release.** *Integration* is high-frequency and dirty: each bead gets a
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

## Control plane — commissioning rigs across the workspace

Before planning or integration begins, the **control plane** stands up the rig sites:
a human-supervised session commissions repos (clone, init, register), configures them
(otel, feature flags, prefix), and reports to **Head Office** — the workspace registry
(`~/.ws/config.yaml` → `managed_repos`).

```text
discover → onboard → configure → verify → hand off
```

This runs in a **human-supervised session** — not inside a worktree, not alongside a
coordinator. The `superintendent` skill is the commissioning agent; it does not pair with
`ws work` (the one structural break from every other AGF role).

**Distinct paths, by design:**

- **Register-only** — `ws rig add <provider/org/repo>` stamps the registry with no cwd.
- **Local onboard** — `ws rig onboard <provider/org/repo>` inits an existing checkout.
- **Remote onboard** — `ws rig onboard ... --clone-url <url>` clones first (only when absent).
- **Configure** — `ws config set` / `ws config unset` (dotted path, validated, round-trip).

See [CONTROL-PLANE.md](CONTROL-PLANE.md) for the full 5-step loop, verb surface, and MCP
tools.

## Molecule integration branch (two-level)

Each kicked-off molecule gets its own integration branch (`mol/<epic>`), opened on the
**integration** plane: a coordinator runs `ws work start <epic> --as coord/<name>` to open
`mol/<epic>` off the integration branch and take the epic seat. Planning stays separate —
`ws plan approve` only readies the epic's beads in `bd ready`; it no longer creates the branch
(the planes never step into each other's role). Child beads assigned afterward fork off
`mol/<epic>` (opened lazily on first `assign`/`claim` if `start` was skipped), so bead B sees
bead A's already-merged work; `ws work merge <bead>` lands each into `mol/<epic>`. When the
molecule is whole, `ws work finish <epic>` (alias of `ws work merge <epic> --molecule`)
validates the assembled branch and lands it on the always-green integration line as **one
`--no-ff` bubble** — two levels: bead merges inside the molecule bubble, molecule bubble on
`main`. A bead with no `mol/<epic>` branch still targets `main` directly (backward-compatible).

Dispatch is seat-typed: an **epic** may only be assigned to / started by a **coordinator**
(`coord/<name>`), any other bead only by a **developer** (`crew/<name>`).

See [WORK.md](WORK.md) for the full `start` / `finish` / `--molecule` verb mechanics.

## Batch groups (the exception to one-bead-per-worktree)

The default is one bead → one worktree → one developer → one merge. A **batch** runs several
beads in one shared worktree, merged once. It wins when a linear chain has no mid-point
testable unit, or when validation is expensive enough to amortize once. **Do not batch when**
beads are independent (you lose parallel wall-time), heterogeneous (different components, model
tiers, or review gates), or large (over 5 — a batch fails as a unit, so keep the blast radius
small). The four guards (cohesion, size cap, single model tier, no mixed review gates) enforce
this automatically; any guard failure falls back to singletons. See
[WORK.md — Batch groups](WORK.md#batch-groups--when-not-to-batch) for the full guards,
blast-radius reasoning, and cost trade-off table.

## The loop (one Claude Code terminal)

A **coordinator** finds ready beads, assigns + provisions worktrees, launches **developer**
sub-agents (model per bead), watches review gates, and serializes merges via the **merger**.
Parallel devs, serial merge.

## Role modes — launching a seat as the main loop

Any AGF seat can run as the **main** Claude Code loop instead of as a task-spawned
sub-agent. Two equivalent entry points:

- `ws role <seat>` — thin sugar: exports `WS_ROLE` then execs `claude --agent <seat>`.
- `claude --agent <seat>` — reads the seat def from `.claude/agents/<seat>.md` directly.

When a seat launches as a role mode the def's **body** becomes the system prompt, its
**`skills:` frontmatter** preloads the role skill (plus `work` for every seat except
superintendent), and its **`tools:` / `model:`** fields scope what the seat can reach.
The TUI statusline renders `⬡ <org>/<repo> · <seat>` showing the active seat and rig.

The seven seats: `planner`, `coordinator`, `developer`, `reviewer`, `merger`, `analyst`,
`superintendent`.

`ws rig init --claude` (and `ws rig onboard --claude`) injects the agent defs into
`.claude/agents/` during rig onboarding — see [RIGS.md](RIGS.md).

### Delegation depth spectrum — how far dispatch nests

When the root coordinator collapses an epic (`work.dispatch.mode` `collapsed`/`auto`),
`work.dispatch.max_depth` (`0` | `1` | `2`, default **`2`**) picks *how far* it may nest
sub-agent dispatch. The three depths are a spectrum from "no Task at all" to "one collapsed
session with a single escape hatch":

- **Depth 0 — the current session does the work itself.** No `Task` is spawned; whoever is
  already on the seat implements the beads in-place. This is only coherent for a **human
  already on the developer seat** driving the work by hand — there is no sub-agent to delegate
  to. An agent root coordinator can't do useful work at depth 0.
- **Depth 1 — one `Task` to `epic-coordinator`.** The root coordinator dispatches **ONE**
  `Task` to the collapsed `epic-coordinator` seat, which works **every** ready bead of the
  epic sequentially in **one shared `wt/batch/<epic>` worktree** on one shared batch branch,
  then merges the whole set batch-end. That seat holds Edit/Write but **no `Task`** — a hard
  harness ceiling from its fixed `tools:` grant, not a prose convention — so it can never
  nest further. There is no escape valve at depth 1: a bead that needs isolation is simply
  out of scope.
- **Depth 2 — `epic-coordinator-deep`, the implicit default today.** Same collapsed loop as
  depth 1, but this seat **also holds `Task`** — the one genuine escape valve. Most beads stay
  collapsed on the shared batch branch; for **one specific** genuinely risky or conflicting
  bead, the deep seat kicks it back out to its own isolated `wt/bead/<id>` worktree driven by
  a **developer** sub-agent (one `Task`, passing that bead's `model:`) while the siblings stay
  collapsed.

**Escape-valve mechanics (depth 2 only).** The kicked-out bead is quarantined and lands last:

- Its work **never commits onto the shared batch branch** — it lives only on its own isolated
  `wt/bead/<id>` branch.
- It lands **last**, via the normal per-bead merge path, against an **already-updated**
  `mol/<epic>`: the collapsed siblings `merge --group` into `mol/<epic>` first, *then* the
  isolated bead merges against that updated `mol/<epic>`, then `ws work finish <epic>`.

Use the valve sparingly — it reintroduces the per-worktree overhead that collapse exists to
avoid. The dispatch-config keys that drive collapse (`work.dispatch.*`) and the
planner-hints-vs-override precedence are documented in
[CONFIGURATION.md — work.dispatch](CONFIGURATION.md#workdispatch--collapsed-dispatch).

## Progressive disclosure — load what the seat needs

- `Skill: superintendent` — control-plane seat: discover → onboard → configure → verify →
  hand off. Does **not** pair with `ws work`.
- `Skill: coordinator` — dispatch loop (overseer): ready → assign → fan-out devs → gate → merge.
- `Skill: developer` — implement one assigned bead in a worktree → submit (claim `--as <crew>`).
- `Skill: merger` — serialize approved beads, `ws work merge`, `--no-ff`, never drop work.
- `Skill: work` — `ws work` verb reference.

Each seat above (except `Skill: work`) is also launchable as a **role mode** —
`ws role <seat>` / `claude --agent <seat>`; see the **Role modes** section above.

See also `ws work --help` and [WORK.md](WORK.md) for the full lifecycle and verb mechanics.
