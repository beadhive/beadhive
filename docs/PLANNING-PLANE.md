# Planning plane — `bh plan` + `planner` (idea → gated molecule)

The planning plane is the **upstream stage** of Beadflow: a human-interactive session takes a raw
idea (feature / change / refactor) and drives

```text
ideate → research → architecture → decompose → file molecule
```

producing a beads **swarm** (epic + child issues + dependency DAG) that a dispatcher later
implements via `bh work`. It runs in a distinct, deliberate session — **not** inside a
worktree. The planner role is the cartographer; it does not implement, dispatch, or merge.

> Accuracy is the whole job. A wrong decomposition wastes every downstream implementation
> hour, so the design is accuracy-first and bi-directional (validate → preview → atomic file
> → round-trip verify).

See also [AGF.md](AGF.md) for the overall flow and [WORK.md](WORK.md) for the integration
plane that follows.

## Two gates, by design

Filing a molecule opens **two distinct approval gates**:

| Gate | Verb | What it does |
|---|---|---|
| Plan approval | `bh plan file <spec>` | Compiles the spec into beads (epic + children + deps + labels) and opens the kickoff gate (`kickoff=pending`). |
| Kickoff approval | `bh plan approve <epic>` | Resolves the kickoff gate and flips `kickoff=approved`; only now do the molecule's roots surface in `bd ready` for a dispatcher. |

Never collapse them. The first gates whether the decomposition is right; the second gates
whether the work should start now.

## Fidelity spectrum and intake triage

When the idea arrives the planner **auto-classifies** it into a fidelity tier and **asks
the human to confirm or override** before proceeding:

- **quick** — small fix / refactor (≈2–4 issues): chat → inline-synthesized spec →
  dry-run → file.
- **spec** — medium feature (≈5–15 issues): author/edit a YAML spec → `check` → preview
  → file.
- **deep** — large/cross-cutting epic: spawn `analyst` research sub-agents + architecture
  → spec → file.

All three converge on the same compiler and the same two gates; the tier only scales how
much research and structuring happen up front.

## Staged flow

```text
frame → triage (confirm) → research → architecture/decisions → decompose → check + preview
  → [PLAN APPROVAL] file → show (round-trip verify) → [KICKOFF APPROVAL] approve → dispatcher
```

Each stage is a human checkpoint with loop-back. Research uses existing tools (Explore,
GitHub search, context7, exa / deep-research) guided by the skill — no `bh` code.

## Adopt: seed a frame from a promoted report

`bh plan adopt <intake-bead>...` is the planner-side entry to the same flow, fed by the
**intake pipeline** (epic). When triage `promote`s a report (bug or feature
request, from any channel — cross-rig `report` / GitHub `github` / legacy `import`), it lands
as `intake:promoted`; `adopt` consumes that queue and seeds the opening **frame** of a molecule
spec from the report text. The planner then decomposes it into issues and files it like any
other spec — the two gates (plan-approval, kickoff) are unchanged.

Two things ride from the report onto the filed epic:

- **Provenance survival.** The system-of-record `source_system` + `external_ref` pair (native
  bd fields, e.g. `github` / `gh-9`) carries onto the epic, so a GitHub-sourced request stays
  traceable. Because `source_system` is settable only at bead birth, a provenance-carrying epic
  is **born via `bd import`** rather than `bd create`.
- **Originating link (correct direction).** On `bh plan file`, each origin report is linked as
  **child-of the epic** (`bd dep add <report> <epic> -t parent-child`) — the report **depends-on**
  the epic. The epic **owns** the report; the report is **never** a blocker of the epic (it can't
  wrongly gate the molecule on an open report) and it rides the epic to completion. A `blocks`
  edge is not usable here — bd forbids blocking edges between an epic and a task — so
  `parent-child` is the sanctioned direction.

`bh plan show <epic>` renders the originating report(s) in their own section (with channel +
provenance), so the round-trip proves what landed traces back to the request. Origin reports are
held out of the molecule's work-sibling set, so they never demand acceptance or a kickoff gate.

## Molecule spec format

A transient, diffable **YAML molecule spec** is the editable accuracy lever. After filing,
**beads is the source of truth**; the spec is absorbed scaffolding.

```yaml
epic:
  title: "Epic title"
  description: "intent + context"
  design: "architecture notes"
  adopts: [bd-123]              # originating report id(s) — set by `bh plan adopt` (optional)
  source_system: github        # native provenance carried onto the epic (optional)
  external_ref: gh-9           # e.g. gh-<n> — keeps a sourced request traceable (optional)

issues:
  - handle: a                   # local id, referenced by deps
    title: "Issue title"
    type: feature|task|bug|chore
    priority: 1
    description: "the why for this slice"
    acceptance: "done when …"   # REQUIRED (accuracy)
    design: "approach notes"
    size: m                     # closed dim
    model: opus|sonnet|haiku    # routing (closed dim)
    harness: claude             # routing (closed dim)
    component: runtime          # open dim
    batch: same-file            # batch:<group> — handle these as ONE parallel unit (open dim)
    deps: [b, c]                # local handles this depends on
```

### Batches (`batch:<group>`)

A `batch:<group>` marks issues the dispatcher should run as **one** parallel unit — one
worktree/agent, validated and merged once — instead of the default one-bead-per-worktree.
Declare a batch when issues **contend on the same file** (avoid repeated merge conflicts) or
**share expensive validation** (run it once after serial implementation). The field becomes a
`batch:<group>` label on every member bead, so membership survives filing. Authoring a batch
that the validator will accept:

- **Shared model** — members must not declare conflicting `model` tiers (omit `model` to inherit).
- **Within the cap** — at most `work.batch_max_size` (default 5) members per group.
- **Cohesive** — members must share a `component` **or** be contiguous (connected via `deps`)
  in the DAG; a scattered, unrelated set is rejected.

`bh plan show <spec|epic>` re-renders the molecule from either the spec file (pre-file
view) or the filed epic (post-file round-trip view), so you can confirm what landed matches
intent.

## Validation rules

`bh plan check <spec>` (and inline in `bh plan file`) enforces:

- **Epic present** with a non-empty title.
- **Every issue** has a unique handle, a title, and `acceptance` (the accuracy bar).
- **Deps closed-set**: every handle referenced in `deps` exists in the spec.
- **DAG / acyclic**: no dependency cycles (iterative DFS, 3-colour marking).
- **No orphan deps**: dangling references are flagged immediately.
- **Closed-label dimensions**: `model`, `harness`, `component`, `size` values that map to a
  closed dimension in the rig's config must be in that dimension's allowed set.
- **Batches** (`batch:<group>`): each declared group must share a model tier, hold no more than
  `work.batch_max_size` members, and be cohesive (same `component` or contiguous in the DAG).

## Filing mechanism

`bh plan file` compiles the spec into beads through these steps:

1. **Load + validate** the YAML spec (same rules as `check`).
2. **Topological sort** — order issues so each `--deps` references an already-created real
   id (deps before dependents, stable Kahn sort).
3. **Create epic** — `bd create --type=epic` with description/design; the provider/org/repo
   identity triplet is injected automatically.
4. **Create each child** — `bd create --parent <epic> --acceptance ... --design ... -l <labels>
   --deps <ids>` in topo order. Labels carry the dimension fields plus the identity triplet.
5. **Build swarm** — `bd swarm create <epic>` wires the DAG.
6. **Open kickoff gate** — `bd gate create --type=human --blocks <root>` for each root issue
   (issues with no deps); set `kickoff=pending` on the epic.

`--dry-run` renders a preview without calling `bd` at all, making it side-effect-free.
`--save <path>` writes the normalised spec for audit after filing.

### Why not `bd create --graph`?

The `--graph <json>` call creates epic + children + deps + labels atomically but **silently
drops `acceptance` and `design`** (warns "unknown field(s)"). Acceptance is the molecule's
required accuracy field, so the graph path would lose it. The per-issue path is used
instead; it carries every field and runs in dependency order.

## Kickoff gate and state

At file time the planner:

- Opens a `bd gate --type=human` **blocking each root issue** (so `bd ready` surfaces no
  work until the gate is resolved).
- Sets `kickoff=pending` on the epic (visible in `bd swarm status` and `bh plan status`).

`bh plan approve <epic>` resolves every open kickoff gate for that epic and flips
`kickoff=approved`. Only after approval do the molecule's root issues appear in `bd ready` for a
dispatcher to pick up. This is **pure planning** — it does *not* create the `mol/<epic>` branch;
opening that is an **integration-plane** step, so the planes never step into each other's role.

On the integration plane the dispatcher runs `bh work start <epic>` to open `mol/<epic>` off the
rig integration branch (or it opens lazily on the first `bh work assign`/`claim` of a child). Bead
worktrees for this molecule fork off `mol/<epic>` (not `main`), so intra-molecule dependencies
compose — each bead sees the work already merged by its predecessors. The dispatcher merges each
bead into `mol/<epic>` via `bh work merge <bead>`.

When all beads are merged the dispatcher runs `bh work finish <epic>` (alias of
`bh work merge <epic> --molecule`) to validate the assembled branch and land it on the integration
branch as one `--no-ff` bubble — the molecule's bead merges live inside that bubble, `main` stays
always-green until the whole molecule is ready. See [WORK.md](WORK.md) for the full verb mechanics
and backward-compatibility note.

## Command surface

| Verb | Does |
|---|---|
| `bh plan check <spec>` | Standalone validation: prints `✓ valid` (exit 0) or each problem (exit non-zero). |
| `bh plan file <spec> [--dry-run] [--save <path>]` | Validate → create epic + children + swarm + kickoff gate. `--dry-run` previews; `--save` writes the normalised spec. |
| `bh plan show <spec\|epic>` | Render the molecule from a spec file (pre-file) or a filed epic (round-trip verify). |
| `bh plan approve <epic>` | Resolve kickoff gates + set `kickoff=approved`; refuses unless `kickoff=pending`. |
| `bh plan status [<epic>]` | List all swarms with progress + kickoff column, or detail one. |

## Skills and agents

- **`Skill: planner`** (`skills/planner/SKILL.md`) — the human-interactive role: framing,
  triage + fidelity spectrum, the staged flow, when to escalate to research, how to author
  the spec, and the two gates. Pairs with `work` / `dispatcher` (downstream).
- **`analyst` sub-agent** (`.claude/agents/analyst.md`) — fire-and-forget research sub-agent
  the planner spawns on deeper tiers (codebase + web/docs research → structured findings).

## Durability escape hatches

Beads stores issues efficiently; large prose artifacts deserve deliberate handling.

### (a) Export to richer docs

`bh plan export` (future) — render a filed molecule into human-visible documents (design
doc, ticket dump, etc.) for audit or handoff outside the issue tracker. Not yet implemented;
beads is the source of truth in the meantime.

### (b) FK to external durable stores ("LFS-for-beads")

When an epic's prose — architecture diagrams, research dumps, large acceptance appendices —
outgrows what an issue field holds cleanly, **beads stays the index** while the bulky
artifact lives elsewhere:

- `--spec-id <id>` — reference an external spec by id.
- `--external-ref <url>` — link to any external resource (design doc, Notion page, ADR).
- `--metadata @file.json` — attach an arbitrary JSON blob (research output, config snapshots).

These fields are the FK; beads is still the source of truth for the molecule structure and
state. Implement only the field-plumbing now; richer export is the growth path.
