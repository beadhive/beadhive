# AGF & the operational planes

**Beadery is the factory that runs AGF on beads.** AGF (Agentic Git-Flow) is the methodology —
abstract and tracker-independent; Beadery is the concrete factory that executes it, with `bdry`
as its command and beads as its unit of work. This file states that framing once and then walks
the tenets and the planes.

## The five tenets

1. **Three operational planes, kept separate.** *Control* commissions and configures rig sites.
   *Planning* turns ideas into molecules. *Integration* executes them. Each plane has its own
   verb surface and seat; they hand off sequentially and never step into each other's role.
2. **Integration vs release.** *Integration* is high-frequency and dirty: each bead gets a
   worktree off the integration tip and lands on an **always-green** line. *Release* is a
   separate, deliberate, gated act. Merging is not releasing.
3. **Lossless history.** Agents do the merging, so history is audited: merge `--no-ff` at the
   boundary and **never squash there**.
4. **Tiered retention.** Squash only *local checkpoints* into a few clean conventional-commit
   digests *before* merge; the integration ledger is preserved forever.
5. **Unit of work = a bead.** Worktree → implement → refine → check → submit → review → merge.

## The operational planes

Each plane runs a distinct session with its own seat and verb surface, and hands off to the next.

### Control plane — commissioning rigs

The **superintendent** stands up and configures rig sites, then hands off. Loop:

```text
discover → onboard → configure → verify → hand off
```

A human-supervised session (not inside a worktree, not alongside a coordinator). It commissions
repos (clone, init, register), configures them (`bdry config set`), and reports to **Head
Office** — the workspace registry at `~/.ws/config.yaml → managed_repos`. Verb surface:
`bdry rig` / `bdry config` / `bdry sync` / `bdry labels`. This is the one seat that does **not**
pair with the `work` skill. See [storage-model.md](storage-model.md) for Head Office and rig
kinds.

### Planning plane — idea → gated molecule

The **planner** (the cartographer) turns a raw idea into a molecule a coordinator can execute.
Loop:

```text
ideate → research → architecture → decompose → file molecule
```

A human-interactive session. For *deep* tiers the planner spawns the **analyst** sub-agent for
codebase and web research before decomposing. Filing a molecule opens **two distinct gates**,
never collapsed:

- **Plan approval** — `bdry plan file <spec>` compiles the spec into beads (epic + children +
  deps + labels) and opens the kickoff gate (`kickoff=pending`). Gates whether the decomposition
  is right.
- **Kickoff approval** — `bdry plan approve <epic>` resolves the gate and flips
  `kickoff=approved`; only now do the molecule's root beads surface in `bdry work ready` for a
  coordinator. Gates whether the work should start now.

### Integration plane — execute the molecule

The **coordinator → developer → merger** chain executes a kicked-off molecule. The coordinator
(overseer) finds ready beads, assigns and provisions worktrees, launches developer (polecat)
sub-agents, watches review gates, and serializes merges through the merger (the Refinery):
**parallel devs, serial merge.** Each bead lands `--no-ff` on the always-green integration line.
See [bead-lifecycle.md](bead-lifecycle.md) for the verb table and
[dispatch-and-scheduling.md](dispatch-and-scheduling.md) for how ready work becomes agents.

### Release plane _(roadmap)_

A deliberate, gated act separate from integration: version determination plus a Commitizen (`cz`)
release gate turns an always-green integration line into a cut release. Merging is not releasing;
the release plane is where releasing happens.

### Contribution plane _(roadmap)_

A sibling to Integration that operates over **external rigs** — our virtualized view of a repo
outside the factory boundary that we do not control and generally cannot push to. It is **always
fork-and-PR**: a dedicated **`contributor`** seat (built on the read-only analyst primitive) owns
a repo **dossier** — the target's CONTRIBUTING rules, PR-template and DCO requirements, mined
historical conventions, and AI-PR posture — whose conventions trump ours on any conflict. An
automated **provenance scrub** hard-blocks factory metadata from entering a PR, and a
human-only, non-agent-resolvable **`bdry work pr`** publication gate clears an exceptionally high
quality bar before anything is published upstream.

## The one-terminal loop

The whole integration plane runs from a **single Claude Code terminal**: a coordinator finds
ready beads, assigns and provisions worktrees, launches developer sub-agents (model per bead),
watches the review gates, and serializes merges via the merger. Parallel devs, serial merge — one
terminal driving the molecule end to end.
