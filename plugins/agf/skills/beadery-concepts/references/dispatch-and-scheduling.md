# Dispatch & scheduling

How ready work becomes agents: the dispatch modes, their control knobs, when to choose each, and
how the scheduler groups beads.

## The default: one bead, one worktree, one developer, one merge

The default AGF unit is **one bead → one worktree → one developer → one merge** — parallel devs,
serial merge. Separate worktrees buy parallel wall-time, and each bead lands on its own clean
conventional history. This is the right call whenever beads are independent.

## The three dispatch modes

`work.dispatch.mode` decides how the **root dispatcher** turns a ready epic into agents:

- **fanout** (default) — leaves the per-bead / per-group developer fan-out unchanged: each ready
  bead (or scheduled group) gets its own developer sub-agent in its own worktree, run in
  parallel. The fanout dispatcher holds no Edit/Write — it delegates, never implements.
- **collapsed** — dispatches **one** collapsed `dispatcher` `Task` (`dispatcher @ batch`, the seat
  that replaces the retired `epic-coordinator`) that works **every** ready bead of the epic
  sequentially in one shared `wt/batch/<epic>` worktree on one shared batch branch, merged **once**
  at the end. It bypasses the scheduler's grouping guards (the operator is vouching for cohesion)
  and requires a **fully un-batched** epic — a partially planner-batched epic fails loudly at claim
  rather than silently mixing batch groups.
- **auto** — decides per epic: it collapses only when the `size:`-weighted total stays within
  `auto_budget` **and** the set is single model tier / single review gate; otherwise it fans out.

## Control knobs — `work.dispatch.*`

Each key resolves per-rig override > global > default. Every value is **advisory**: dispatch
config decides grouping and seat only; it never claims or merges anything.

| Key | Default | Values | Effect |
|---|---|---|---|
| `work.dispatch.mode` | `fanout` | `fanout` \| `collapsed` \| `auto` | How to dispatch a ready epic; unknown values fall back to `fanout`. |
| `work.dispatch.max_depth` | `2` | `0` \| `1` \| `2` | Which collapsed seat runs and whether it has an escape valve; out-of-range clamps to `2`. |
| `work.dispatch.max_beads_per_session` | `8` | int | Cap on beads one collapsed session holds before it splits into chunked sessions. |
| `work.dispatch.auto_budget` | `8` | int | `size:`-weighted budget `auto` can absorb before it prefers fanout. |
| `work.dispatch.review_mode` | `self` | `self` \| `fresh` | Who resolves a dispatched bead's review gate. |
| `work.batch_max_size` | `5` | int | Max members in a scheduler batch group (the size cap guard). |

```sh
bdry config set work.dispatch.mode collapsed        # force-collapse ready epics
bdry config set work.dispatch.max_depth 1           # collapsed seat with no escape valve
bdry config set work.dispatch.auto_budget 12        # let auto absorb a bigger epic
bdry config set work.dispatch.review_mode fresh     # independent reviewer per bead (depth 2)
```

### `max_depth` — which dispatcher variant, and the escape valve

The collapsed worker is **one seat, `dispatcher` (`disp/`)** — the retired `epic-coordinator` /
`epic-coordinator-deep` / `foreman` names all fold into *dispatcher @ batch (collapsed)*. Two
capability ceilings distinguish the variants, each a hard `tools:`-grant presence/absence, not a
prose convention:

- **`implement`** (Edit/Write) — on for every collapsed dispatcher (it inlines the developer work),
  off for a fanout dispatcher (it only delegates).
- **`sub-dispatch`** (Task) — the escape valve; a collapsed dispatcher may hold **≤1** (kick exactly
  one bead out to a developer).

`max_depth` picks the collapsed variant and how far dispatch nests:

- **0** — the current session does the work itself, no `Task` (no `sub-dispatch`) — only coherent
  for a human already on the developer seat.
- **1** — one `Task` to a collapsed **dispatcher @ batch** (`implement` on, `sub-dispatch` off):
  works every ready bead sequentially in the shared batch worktree, merged batch-end. With no
  `Task` ceiling there is no escape valve — a bead needing isolation is out of scope.
- **2** — a collapsed **dispatcher @ batch + escape** (`implement` on, `sub-dispatch:1`), the
  default: the same collapsed loop, but it also holds one `Task` — the one genuine escape valve.
  Most beads stay collapsed; for **one specific** risky or conflicting bead it kicks that bead out
  to its own isolated `wt/bead/issue/<id>` worktree driven by a developer sub-agent, while the
  siblings stay collapsed. The kicked-out bead is quarantined (its commits never touch the shared
  batch branch) and lands **last**, against an already-updated container.

### `review_mode` — who resolves the gate

- **self** (default) — the collapsed dispatcher is its own review authority and self-resolves
  each bead's gate in the same collapsed session (no second `Task`), legitimate because the
  collapsed session runs under a live human watching it.
- **fresh** — a separate reviewer `Task` with independent, fresh context resolves each bead's
  gate. Spawning that `Task` requires depth 2; a depth-1 + `fresh` pairing is a dispatcher
  misconfiguration to surface, not a silent self-review.

A third mode, `paired`, is deliberately not implemented; selecting it normalizes to `fresh` and
emits a warning rather than silently no-op'ing, so the bead still gets an independent reviewer.

## When to choose which

- **Fan out** when beads are **independent** — you win parallel wall-time and per-bead failure
  isolation, and each bead lands on its own clean history.
- **Collapse** when parallelism buys nothing or validation dominates: a **linear chain** with no
  mid-point testable unit cannot be parallelized anyway, so one worktree/validate/merge is
  strictly cheaper than N sequential ones; and **expensive validation** amortizes to one run.
- **Do not collapse** when beads are heterogeneous (different components, model tiers, or review
  gates) or large — a collapsed/batched set fails **as a unit**, so keep the blast radius small.

## The scheduler — `bdry work schedule <epic>`

`bdry work schedule <epic>` computes the dispatch plan for a molecule's open beads. Group
formation:

- **Child epics → nested dispatchers.** A child epic is itself a molecule, so it is partitioned
  out first and dispatched to its **own** nested dispatcher seat (`dispatcher @ epic-container`) —
  never batched or collapsed with leaf issues (nesting bounded by `max_depth`).
- **Planner batches** — a shared `batch:<group>` label the planner declared (already validated at
  plan time) is honored as one grouped agent when it has ≥2 members.
- **Auto-detected private linear chains** — a run of beads connected by *private* `blocks` edges
  (no fan-in / fan-out): a chain cannot be parallelized, so batching is strictly cheaper.
- **Else singleton** — everything left over is a singleton, the default parallel one-per-worktree.

### The four guards

Auto-detected chains are re-validated against the same guards before batching; any guard failure
drops the candidate back to singletons, because a batch fails **as a unit**:

| Guard | Rule |
|---|---|
| **Cohesion** | Members share a `component` or are contiguous in the dep DAG (implicit for a private-edge chain). |
| **Size cap** | At most `work.batch_max_size` (default 5) members — keeps the merge bubble reviewable and bisectable. |
| **Single model tier** | One model per group; conflicting `model:` labels are refused (members can omit `model` to inherit). |
| **No mixed review gates** | Members share a review gate; mixed `gate:` overrides are refused so one approval covers the bubble. |

**Blast-radius reasoning:** the size cap and cohesion guards exist because a batch bounces
together — no partial landing. Keep groups small and cohesive so a single failure never strands a
large, scattered set.

## Parallel vs sequential — the cost trade-off

| | Batch / collapse | Singleton (default) |
|---|---|---|
| **Merges / validates** | 1 for N beads | N (one per bead) |
| **Wall-time** | Serial (one agent implements in order) | Parallel (N agents concurrently) |
| **Failure blast radius** | Whole group bounces on any failure | Only the failing bead bounces |
| **Bisect granularity** | Per-bead commits preserved inside the `--no-ff` bubble | Per-bead branch |
| **Review scope** | One gate covers all members | One gate per bead |

Batch wins when wall-time parallelism does not matter (a linear chain cannot be parallelized) or
when validation cost dominates (expensive setup amortized once). Stay with singletons when beads
are independent and cheap to validate — parallel wall-time is then the dominant win and per-bead
isolation makes failures cheap.
