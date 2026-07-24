# Beads sync — distributing issue state to agents (design)

> Status: **design / intent.** It records how `bh` is meant to move beads issue state across
> the hub, each hive, and distributed agents using embedded Dolt and git-native refs — **no
> server required**. Parts are built (see *What exists vs gaps*); the role choreography is the
> target. The branch/worktree side of the same lifecycle is [WORK](WORK.md); the cross-hive
> read cache is [HUB](HUB.md); the optional shared server is [DOLT](DOLT.md).

## The bet: state travels on git refs, not a server

Beads stores issues in **Dolt**, which is versioned like git — commits and refs, pushed and
pulled between copies. Each hive's authoritative issue history lives on **its own git remote**
under `refs/dolt/data` ([OVERVIEW](OVERVIEW.md)). So moving issue state between machines is
just a Dolt push/pull of those refs — the **same transport** as the code branch handoff
(`wt/bead/<id>`). Two git-native channels move in parallel: the **branch** carries the change,
the **Dolt ref** carries the bead's state. No central database has to be online for an agent
to get its work or report progress.

This is why the [DOLT](DOLT.md) shared server is optional and, by default, unused: hives are
embedded Dolt under each repo's `.beads/`, and distribution is git-native.

## Three vantage points

```mermaid
flowchart TB
  subgraph remotes["Hive git remotes (authoritative — refs/dolt/data)"]
    RA[(hive A)]; RB[(hive B)]; RC[(hive C)]
  end
  subgraph bh["bh host — local embedded cache (no server)"]
    HUB[("~/.ws/hub: aggregate of ALL registered hives")]
  end
  RA -->|bh sync: dolt pull| HUB
  RB -->|bh sync: dolt pull| HUB
  RC -->|bh sync: dolt pull| HUB
  subgraph dev["developer worktree (local sandbox OR remote host)"]
    DW[("embedded Dolt for ONE hive + wt/bead/&lt;id&gt; branch")]
  end
  RA <-->|"dolt pull (claim) / push (submit)"| DW
  COORD{{dispatcher}} -->|"assign → dolt push state"| RA
  COORD -->|provision + trigger| dev
```

### 1. `bh` (hq) — a read cache over every hive

`bh sync` pulls each registered hive's `refs/dolt/data` into one **local embedded Dolt DB** at
`~/.ws/hub` (cloned hives by path; uncloned by a blobless minimal-clone cache). `bh hq bd
ready` then answers "what's actionable anywhere?" across the whole workspace without a server
and without every repo checked out. This is built today — see [HUB](HUB.md). The HQ aggregate
is a **cache**: authoritative state stays on each hive's remote.

### 2. `developer` — one hive, one bead, anywhere

A developer agent (a local sandbox folder elsewhere on the box, or a wholly separate remote
host) does **not** need the hub or the central server. It pulls **its own copy of the one
hive's Dolt remote**, giving it that hive's issues, then works the assigned bead in its
worktree. On a remote host the worktree can't share a local object store, so the hive is
cloned and the `bead/<id>` branch + `refs/dolt/data` are the only things that cross — exactly
the handoff medium [WORK](WORK.md) and the `bh work` impl spec describe.

### 3. `dispatcher` — assign here, run there

The dispatcher owns the cross-boundary transfer:

1. **Assign + publish state.** `bh work assign <id> --to dev/<name>` stamps the assignee in
   beads, then **pushes that state to the hive's remote** (`bd dolt push`). The assignment is
   now durable on the ref, not just in the dispatcher's local DB.
2. **Provision the worktree wherever** — local sandbox or remote host.
3. **Trigger the developer**, which **pulls the hive's Dolt remote** (sees the assignment) and
   `bh work claim`s the bead as its own actor (→ `in_progress`).

State crossed the boundary entirely through git refs: the dispatcher never reaches into the
developer's machine to mutate a database — it pushes a ref, the developer pulls it.

## The choreography (assign → claim → submit → merge)

```mermaid
sequenceDiagram
  participant C as Dispatcher
  participant R as Hive remote (refs/dolt/data + branches)
  participant D as Developer (sandbox/host)
  participant M as Merger
  C->>R: bh work assign --to dev/x  +  bd dolt push (assignee, status=open)
  C->>D: provision worktree + trigger
  D->>R: bd dolt pull (sees assignment)
  D->>D: bh work claim (→ in_progress, identity+signing)  +  push state
  D->>D: implement in wt/bead/<id>, self-refine
  D->>R: bh work submit (push bead/<id> branch + review:pending + gate)
  Note over C,D: review runs async (bd gate)
  C->>R: bd ready --gated → outcome
  alt changes-requested
    C->>D: trigger bh work resume (pull feedback, re-submit)
  else approved
    M->>R: merge-slot acquire → --no-ff merge → close → push state
  end
```

Every arrow to/from **R** is a Dolt push/pull of `refs/dolt/data` (state) or a git push/pull
of the `wt/bead/<id>` branch (code). Nothing requires a live server; an offline agent simply
syncs when it reconnects.

> **Not the same as a fleet host switch.** This section is about one developer/one bead moving
> to another host, mid-assignment. Relocating an operator's **entire fleet** (every registered
> hive's branches + Dolt state) to a different physical machine is a separate flow — see
> [CONTROL-PLANE.md — Relocating the fleet to another host](CONTROL-PLANE.md#relocating-the-fleet-to-another-host-pack-up-before-host-switch).

## Local vs remote developer

| | Local sandbox (same box) | Remote host |
|---|---|---|
| Code | linked `git worktree` (shared object store) or a clone | full clone of the hive |
| Bead state | shares the hive's embedded Dolt, or its own pull | own `bd dolt pull` of `refs/dolt/data` |
| Identity/signing | worktree-scoped git config ([WORK](WORK.md)) | same, but the signing key must be **injected**, not a local path |
| Handoff medium | branch + Dolt ref (object store may be shared) | branch + Dolt ref **only** |

The remote case is the strict superset: design for "only the branch and the ref cross" and the
local case falls out for free.

## What exists vs gaps

**Built today:**

- Embedded Dolt per hive under `.beads/`; authoritative history on each hive's git remote at
  `refs/dolt/data` (`bd dolt push` / `pull`).
- `bh sync` → the local hub cache aggregating every registered hive ([HUB](HUB.md)).
- `bh -a bd dolt pull` to refresh cloned hives; minimal-clone bootstrap for uncloned ones.
- `bh work` lifecycle verbs over the local bead DB ([WORK](WORK.md)).
- **State push/pull wired into `bh work`** (bh-dw3e.6, through `Engine.push_state`/
  `pull_state`): `assign`/`submit` `bd dolt push` the new state; `claim`/`resume` `bd dolt pull`
  first, so a developer on another host actually sees the assignment. A push/pull failure never
  blocks the verb — the local DB mutation these verbs exist for has already happened by the
  time it runs — and a solo/no-remote hive is a silent no-op, not a warning.
- **Federation ops on the Engine seam** (`Engine.federation_status`/`sync_state`, bh-wty3.1):
  read-only per-peer sync status (`bd federation status --json` — reachability, ahead/behind,
  conflicts; a real network fetch, so callers own when to pay it) and bidirectional sync
  (`bd federation sync`, optionally `--peer`/`--strategy ours|theirs`; with conflicts and no
  strategy bd pauses and the outcome reports the conflicted tables). Both parse defensively
  and never coerce a failure or unreachable peer into looking in-sync.

**Gaps (the net-new this design asks for):**

- **Per-hive developer bootstrap.** A one-shot "give this sandbox/host just hive X's beads" (the
  developer-side analogue of `bh sync`, scoped to one hive).
- **Remote triggering + key injection.** Launching the developer on a remote host and
  injecting its signing key (local key *paths* are meaningless there) — spec'd in
  the `bh work` impl spec, not built.
- **Conflict-free state merge.** When the dispatcher and a developer both push bead state,
  Dolt merges the refs; the rules for the bead row (assignee/status) need to be pinned.

## Open questions

- Does the developer pull the **hive remote** directly, or a dispatcher-curated ref? Direct is
  simpler and serverless; curated lets the dispatcher gate what's visible.
- Where does the **hub** fit for a dispatcher — is dispatch driven from the hub cache
  (cross-hive) then pushed per-hive, or always per-hive?
- One embedded Dolt per developer host shared across that host's worktrees, vs one per
  worktree — trade isolation against pull cost.

See also: [WORK](WORK.md) (lifecycle verbs), [HUB](HUB.md) (cross-hive cache),
[DOLT](DOLT.md) (optional server), [OVERVIEW](OVERVIEW.md) (the no-server model),
[BEAD-BACKENDS](BEAD-BACKENDS.md) (bd vs br vs bw storage models) and
[design/bead-backend-abstraction.md](design/bead-backend-abstraction.md) (pluggable engines).
