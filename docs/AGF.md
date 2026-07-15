# Agentic Git Flow (AGF)

This repo authors `bh`, the **integration-plane driver** for AGF, and is driven by it.
Don't improvise raw `git` / `gh pr` for the lifecycle — drive beads through `bh work` and
load the role skill for the seat you're in. The basics, so you can start without re-reading.

## Naming: AGF, Beadflow, Beadhive

Three distinct layers, easy to conflate — keep them separate:

- **AGF (Agentic Git Flow)** — the **abstract, tracker-independent process** this doc
  describes: planes, seats, worktree-per-bead, review gates, serialized merges. It has no
  opinion about which issue tracker or CLI implements it.
- **Beadflow** — AGF **implemented on beads**: this repo's concrete process, unchanged
  behavior under a naming/framing layer, not a rewrite. When this doc says "AGF", it means the
  abstract process; when it says "this repo" or points at `bh`, it means Beadflow, the
  bead-backed implementation.
- **Beadhive** — the **umbrella brand/org** (`github.com/beadhive`) this repo (`beadhive/
  workspace`) ships under; `bh` is its CLI binary, published as the `beadhive` package. See
  [`docs/design/limn-naming-strategy-adr.md`](design/limn-naming-strategy-adr.md) for the full
  decision record and rationale.

## Tenets (the why)

- **Operational planes, kept separate.** *Control* governs and configures the factory
  (supervisor · director · custodian · controller). *Planning* turns ideas into molecules
  (planner). *Integration* executes them (dispatcher → developer → merger). Each operational plane
  has its own verb surface and seats; they hand off sequentially and never step into each other's
  role. **Assurance** is the exception — a cross-cutting gate layer (warden, security + policy
  only), not a sequential plane.
- **Integration vs release.** *Integration* is high-frequency and dirty: each bead gets a
  worktree off the integration tip, and lands on an **always-green** line. *Release* is a
  separate, deliberate, gated act. **Merging is not releasing.**
- **Lossless history.** Agents do the merging, so we keep audited history: merge `--no-ff` at
  the boundary, **never squash there**.
- **Tiered retention.** Squash only *local checkpoints* into a few clean conventional-commit
  digests *before* merge; the integration ledger is preserved forever.
- **Unit of work = a bead.** Worktree → implement → refine → check → submit → review → merge.

## Planning plane — upstream of the integration loop

Before a dispatcher assigns beads, the **planning plane** turns a raw idea into a
molecule: a gated, dependency-linked swarm the integration loop can execute.

```text
ideate → research → architecture → decompose → file molecule
```

This runs in a **human-interactive session** — not inside a worktree, not a dispatcher.
The `planner` skill is the cartographer; for *deep* tiers it spawns the `analyst`
sub-agent for codebase + web research before decomposing.

**The planning-seat contract.** A planning session outputs exactly two artifact types:
**beads** (molecule specs compiled by `bh plan file`) and **decision records**
(`docs/design/*.md`) — **never code**. The contract is normative and harness-independent:
any harness entering the planning seat loads it (the `planner` role skill carries it), so
the human never has to restate it at the start of a session. Decision record:
[design/planning-seat-ux-and-spike-loop.md](design/planning-seat-ux-and-spike-loop.md).

### The spike loop — settle feasibility before implementation beads exist

When architecture surfaces an unresolved GO/NO-GO question, the planner does not guess and
does not file speculative implementation beads. It files a small **spike molecule** instead
and re-enters planning when the verdict lands:

```text
ideate → design ─→ feasibility settled? ──yes──→ file implementation molecule → kickoff → dispatch
                        │ no (open GO/NO-GO question)
                        ▼
              file SPIKE molecule (spike beads + decision bead)
                        ▼
              integration plane executes spikes (normal dispatch)
                        ▼
              decision bead closes with verdict
                 GO ──→ replan the spike epic → implementation molecule
                 NO-GO → ADR in docs/design, close, done
```

This is a **two-molecule loop** by design: nothing exists in the tracker that the current
evidence doesn't support — a NO-GO orphans no implementation beads. **Replan** is the single
re-entry door for *any* mid-flight plan alteration: a spike verdict is one trigger; a
mid-execution blocker or discovery re-enters planning through the same door. The loop is
**convention-only** — labels (`tag:spike` / `tag:decision`), the spike artifact template
[spikes/TEMPLATE.md](spikes/TEMPLATE.md), and the planner conventions; **no new `bh` verbs,
no new bead types**. See [PLANNING-PLANE.md — Spike loop](PLANNING-PLANE.md#spike-loop) for
the bead conventions.

**Two gates, by design:**

- **Plan approval** — `bh plan file <spec>` compiles the spec into beads and opens the
  kickoff gate.
- **Kickoff approval** — `bh plan approve <epic>` resolves the gate; only now do the
  molecule's root beads surface in `bd ready` for a dispatcher.

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
dispatcher. The control plane splits into four seats over four blast radii — **supervisor**
(`super/`, governs the factory + policy), **director** (`dir/`, intake + fleet work routing),
**custodian** (`cust/`, config + secrets + provisioning + cleanup), and **controller** (`ctrl/`,
factory telemetry) — with the **custodian** doing the mechanical commissioning above. The Head
Office registry is partitioned across them (supervisor writes policy, director writes fleet
membership, custodian writes rig config, controller reads); a small/single-rig factory collapses
them into the **supervisor**. Control-plane seats do not pair with `bh work` (the one structural
break from every other AGF role). See [CONTROL-PLANE.md](CONTROL-PLANE.md) for the full split.

**Distinct paths, by design:**

- **Register-only** — `bh rig add <provider/org/repo>` stamps the registry with no cwd.
- **Local onboard** — `bh rig onboard <provider/org/repo>` inits an existing checkout.
- **Remote onboard** — `bh rig onboard ... --clone-url <url>` clones first (only when absent).
- **Configure** — `bh config set` / `bh config unset` (dotted path, validated, round-trip).

### Onboarding preflight gate

`bh rig onboard` / `bh rig init` model onboarding as a small DAG of steps, each with **preflight
checks**. Every statically-evaluable check runs **up front, as a batch**: if any fails, onboarding
prints *all* failures and exits **before any mutation** — it never starts `bd init` (which commits
its scaffolding onto whatever branch HEAD points at) against a tree it shouldn't. The gate sits
ahead of that commit, so `bd init` only ever lands on a clean, default branch. A fresh clone is
clean by construction, so the working-tree checks are marked N/A for it.

Check ids (surfaced by `--dry-run`, targetable by `--skip-check`):

| id | overridable | fires when |
|---|---|---|
| `valid-triplet` | no | the argument isn't `provider/org/repo` |
| `clone-url-present` | no | target is absent and no `--clone-url` was given |
| `clone-url-reachable` | yes | reserved (reachability probe deferred; never blocks today) |
| `parent-writable` | no | the parent dir can't be written (can't clone into it) |
| `under-git-workspace` | no | the target isn't a git repo under `$GIT_WORKSPACE` |
| `not-excluded` | no | the repo is excluded by the registry |
| `fork-needs-yes` | no | the repo is a fork and `--yes` wasn't passed (beads is off by default) |
| `prefix-policy` | no | the prefix violates a required-org policy |
| `dirty-tree` | **yes** | the existing working tree has uncommitted changes |
| `on-default-branch` | **yes** | HEAD is on a non-default branch (or detached) |

`dirty-tree` and `on-default-branch` apply only to an **existing folder we did NOT just clone**.

- **`--dry-run`** (both `init` and `onboard`) prints the full preflight plan — every check id and
  the steps that would run — and mutates nothing.
- **`--skip-check <id>[,<id>]`** downgrades an **overridable** check failure (today `dirty-tree`,
  `on-default-branch`) from a hard failure to a `⚠` warning and proceeds. Non-overridable checks
  (excluded, prefix-policy, …) can never be skipped. `--force` / `--yes` keep their existing
  meanings (re-register / opt into a fork); they are not `--skip-check`.

> Upstream follow-up (not in `bh`): a `bd init --no-commit` flag. The bh preflight can't stop
> `bd`'s commit, but this gate guarantees it lands on a clean default branch.

See [CONTROL-PLANE.md](CONTROL-PLANE.md) for the full 5-step loop, verb surface, and MCP
tools.

## Container branches + the integration_base climb (N-tier)

Every bead — leaf or container — has exactly one branch under the unified namespace
**`wt/bead/<type>/<id>`** (`<type>` ∈ `epic` | `issue`; stable, no time/hash tail). A leaf lives at
`wt/bead/issue/<id>`; a **container** (an epic, at any tier) lives at `wt/bead/epic/<id>` and IS both
the dispatcher's seat worktree and the integration line its children fork from and land on. (The
old bespoke `mol/<epic>` prefix is **retired** — folded into this one convention.)

A kicked-off molecule's container branch is opened on the **integration** plane: a dispatcher runs
`bh work start <epic> --as disp/<name>`, which provisions its **seat worktree** on
`wt/bead/epic/<epic>` (forked off its `integration_base`) and takes the epic seat — the same
`worktree.ensure()` op as a developer seat, differing only in the `<type>` segment + identity.
Planning stays separate — `bh plan approve` only readies the epic's beads in `bd ready`; it no
longer creates the branch. Child beads assigned afterward fork off the container (opened lazily on
first `assign`/`claim` if `start` was skipped), so bead B sees bead A's already-merged work;
`bh work merge <bead>` lands each into the container.

**Integration target = the `integration_base` climb.** A bead's fork/land target is resolved by
walking the dotted `<parent>.<n>` id chain to the **nearest started container ancestor**
(`wt/bead/epic/<parent>`), falling back to the rig integration branch (`main`) at the dotless root —
a pure-git exact-ref climb that skips leaf ancestors. So a leaf lands on its epic; and, because a
**workstream** is just an `issue_type=epic` bead whose children are epics (no new type; the tier is
the position in the dotted id), an epic lands on its workstream and a workstream lands on `main` —
**one recursive rule** (`bh work finish <container>` lands `wt/bead/epic/<container>` up one level,
then tears the seat down). The staleness / rollback / `safe_to_rewrite` safety generalizes up the
chain with no new code: an intermediate local/unpushed container rolls back losslessly; only the
final `→ main` land is fixed forward. A dotless bead with no container still targets `main` directly.

Dispatch is seat-typed and recursive: an **epic** (any tier) may only be assigned to / started by a
**dispatcher** (`disp/<name>`), any other bead only by a **developer** (`dev/<name>`); a child
epic is dispatched to a **nested dispatcher** (`dispatcher @ epic-container`, the dispatcher seat
reused recursively) that self-lands onto the parent container, bounded by `work.dispatch.max_depth`.

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

A **dispatcher** finds ready beads, assigns + provisions worktrees, launches **developer**
sub-agents (model per bead), watches review gates, and serializes merges via the **merger**.
Parallel devs, serial merge.

## Role modes — launching a seat as the main loop

Any AGF seat can run as the **main** Claude Code loop instead of as a task-spawned
sub-agent. Two equivalent entry points:

- `bh role <seat>` — thin sugar: exports `WS_ROLE` then execs `claude --agent <agf:seat>` (or
  `claude --agent <seat>` when a local `.claude/agents/<seat>.md` override exists).
- `claude --agent agf:<seat>` — resolves the seat def from the `agf` Claude Code plugin.
- `claude --agent <seat>` — reads the seat def from a local `.claude/agents/<seat>.md`
  override file (the local file outranks the plugin).

When a seat launches as a role mode the def's **body** becomes the system prompt, its
**`skills:` frontmatter** preloads the role skill (plus `work` for every seat that drives a bead
lifecycle — the control-plane seats do not), and its **`tools:` / `model:`** fields scope what the
seat can reach.
The TUI statusline renders `⬡ <org>/<repo> · <seat>` showing the active seat and rig.

The seats, by plane: Control — `supervisor`, `director`, `custodian`, `controller`; Planning —
`planner`, `analyst`; Integration — `dispatcher`, `developer`, `reviewer`, `merger`; Assurance —
`warden` (with `verifier` kept as a lens). Roadmap seats: `releaser`, `contributor`, `operator`.

`bh rig init --claude` (and `bh rig onboard --claude`) installs the `agf` Claude Code plugin
(default plugin mode) or copies agent defs into `.claude/agents/` (copy mode) — see
[RIGS.md](RIGS.md). Rigs no longer commit seat agent files or `skills/` dirs in plugin mode;
those are vended by the plugin installed at onboard time.

### Delegation depth spectrum — how far dispatch nests

When the root dispatcher collapses an epic (`work.dispatch.mode` `collapsed`/`auto`),
`work.dispatch.max_depth` (`0` | `1` | `2`, default **`2`**) picks *how far* it may nest
sub-agent dispatch. The collapsed worker is a **`dispatcher @ batch`** variant (the seat that
replaces the retired `epic-coordinator`); `implement` (Edit/Write) and `sub-dispatch` (Task) are
hard `tools:`-grant ceilings. The three depths are a spectrum from "no Task at all" to "one
collapsed session with a single escape hatch":

- **Depth 0 — the current session does the work itself.** No `Task` is spawned; whoever is
  already on the seat implements the beads in-place. This is only coherent for a **human
  already on the developer seat** driving the work by hand — there is no sub-agent to delegate
  to. An agent root dispatcher can't do useful work at depth 0.
- **Depth 1 — one `Task` to a collapsed `dispatcher @ batch`.** The root dispatcher dispatches
  **ONE** `Task` to the collapsed dispatcher seat, which works **every** ready bead of the
  epic sequentially in **one shared `wt/batch/<epic>` worktree** on one shared batch branch,
  then merges the whole set batch-end. That seat holds `implement` (Edit/Write) but **no
  `sub-dispatch` (Task)** — a hard harness ceiling from its fixed `tools:` grant, not a prose
  convention — so it can never nest further. There is no escape valve at depth 1: a bead that
  needs isolation is simply out of scope.
- **Depth 2 — a collapsed `dispatcher @ batch` with `sub-dispatch:1`, the implicit default
  today.** Same collapsed loop as depth 1, but this variant **also holds one `Task`** — the one
  genuine escape valve. Most beads stay collapsed on the shared batch branch; for **one specific**
  genuinely risky or conflicting bead, the deep variant kicks it back out to its own isolated
  `wt/bead/issue/<id>` worktree driven by a **developer** sub-agent (one `Task`, passing that
  bead's `model:`) while the siblings stay collapsed.

**Escape-valve mechanics (depth 2 only).** The kicked-out bead is quarantined and lands last:

- Its work **never commits onto the shared batch branch** — it lives only on its own isolated
  `wt/bead/issue/<id>` branch.
- It lands **last**, via the normal per-bead merge path, against an **already-updated** container
  `wt/bead/epic/<epic>`: the collapsed siblings `merge --group` into the container first, *then* the
  isolated bead merges against that updated container, then `bh work finish <epic>`.

Use the valve sparingly — it reintroduces the per-worktree overhead that collapse exists to
avoid. The dispatch-config keys that drive collapse (`work.dispatch.*`) and the
planner-hints-vs-override precedence are documented in
[CONFIGURATION.md — work.dispatch](CONFIGURATION.md#workdispatch--collapsed-dispatch).

## Progressive disclosure — load what the seat needs

- `Skill: supervisor` / `director` / `custodian` / `controller` — the control-plane seats:
  govern, route the fleet, commission (discover → onboard → configure → verify → hand off), and
  observe. Do **not** pair with `bh work`.
- `Skill: dispatcher` — dispatch loop (overseer): ready → assign → fan-out devs → gate → merge;
  collapsed mode inlines the implementation on a shared batch branch.
- `Skill: developer` — implement one assigned bead in a worktree → submit (claim `--as <dev>`).
- `Skill: merger` — serialize approved beads, `bh work merge`, `--no-ff`, never drop work.
- `Skill: work` — `bh work` verb reference.

Each seat above (except `Skill: work`) is also launchable as a **role mode** —
`bh role <seat>` / `claude --agent <seat>`; see the **Role modes** section above.

See also `bh work --help` and [WORK.md](WORK.md) for the full lifecycle and verb mechanics.
