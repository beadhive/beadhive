---
name: beadhive-concepts
description: >-
  Getting-Started concept guide for Beadhive — the software factory that runs Agentic Git-Flow
  (AGF) on beads. Use to answer "what is a rig / HQ / hub / Head Office / role / seat / AGF /
  molecule / dispatch mode?", "explain Beadhive / AGF", "how does the bead lifecycle work", or
  "how does dispatch (collapse vs fanout vs auto) work" — a thin router into a references/
  bundle so you can look up one concept without pulling the whole corpus into context.
---

# Beadhive — concepts (progressive-disclosure router)

Beadhive is a software-factory implementation that operates using Agentic Git-Flow (AGF)
methodology. AGF is the methodology — abstract and tracker-independent; Beadhive is the factory
that runs it on **beads**. The command is `bh`.

The factory turns ideas into merged code through a fixed pipeline of specialized seats: a
**planner** decomposes an idea into a molecule of beads, a **dispatcher** dispatches each bead
to a **developer** in its own worktree, a **reviewer** walks the result, and a **merger**
serializes it onto an always-green integration line — all driven through `bh work`, never raw
`git` / `gh`. Every repo is a self-contained **rig** (its own beads DB); **Factory HQ**
aggregates every rig into one cross-repo view.

## Mental model in one breath

A **rig** is a repo's beads DB. Its issues carry a short, stable **prefix** (`ag-infra-1`).
Repo identity that can change (provider, org) lives in **labels**, not the prefix. Issue
history is stored on the repo's **own git remote** under `refs/dolt/data` — no central database
to run. **Factory HQ** (`~/.ws/hq/`, `bh hq …`) aggregates all rigs for cross-repo queries;
the hub aggregation mechanism powers it internally.

```text
~/workspace/<provider>/<org>/<repo>/   each repo = a rig (embedded Dolt in .beads/)
        │  bh bd dolt push → refs/dolt/data on the repo's own git remote
        ▼
   ~/.ws/hq    ← bh sync aggregates every rig (cloned by path, uncloned by cache)
                 bh hq bd ready → actionable work across the whole workspace
```

## Where to look

Every concept term has a one-line definition in the glossary; each definition points to the
cluster file that covers it in depth. Start at the glossary for any term; jump straight to a
cluster file when you know the area.

| Route to | Covers |
|---|---|
| **any term → [references/glossary.md](references/glossary.md)** | Alphabetical one-line definition of every concept, each pointing to its cluster file. The routing entrypoint. |
| [references/storage-model.md](references/storage-model.md) | Rigs, prefixes, the `provider:`/`org:`/`repo:` triplet, Dolt `refs/dolt/data` storage, the `.beads` stance, pluggable backends _(roadmap)_, and the Factory HQ / Head Office / hub distinction. |
| [references/agf-and-planes.md](references/agf-and-planes.md) | AGF, its five tenets, and the operational planes — control, planning, integration, plus release and contribution _(roadmap)_ — with each plane's loop, seat, and verbs. |
| [references/roles-and-seats.md](references/roles-and-seats.md) | Role vs seat, the seven seats and their duties, the Gas Town naming layer, and how a seat is launched as a role mode. |
| [references/bead-lifecycle.md](references/bead-lifecycle.md) | Bead, molecule, workstream, container branches, the `bh work` verb table (assigned → merged), and review gates. |
| [references/dispatch-and-scheduling.md](references/dispatch-and-scheduling.md) | The three dispatch modes (fanout / collapsed / auto), the `work.dispatch.*` control knobs, and how the scheduler groups beads. |

All commands are written `bh …`. This router stays thin — the depth lives in the cluster
files under `references/`.
