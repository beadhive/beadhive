---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: collect-and-bridge
  title: Collect sources and recover deterministic doc↔bead bridges
  performer: agent
  action:
    type: script
    script: scripts/reconcile.sh
    timeout_seconds: 120
  verify:
    type: script
    script: scripts/reconcile.sh
    success_exit: 0
  on_failure:
    strategy: abort
  effect: read-only
  estimated_duration_minutes: 3
  tags: [collect, bridge, deterministic]
---

# What this step does

Runs `scripts/reconcile.sh <rig-path>` (read-only). Pick the source by the rig's shape — you can
run both if the rig has both:

**Default — `docs/decisions/` + `docs/design/`** (match docs to EXISTING beads). Recovers each
doc's owning bead via two deterministic bridges, in order:

1. **Frontmatter back-ref** — a `Beads: <epic>, <id>` line in the doc itself. Exact.
2. **Git add-trailer** — the bead id in the subject of the commit that first added the doc
   (`git log --follow --diff-filter=A`). Exact.

Emits a TSV `status`, `doc`, `bead`, `bridge`. Statuses: `PRESENT-in-sync`,
`PRESENT-needs-stamp`, `DRIFTED-ref`, `DANGLING`, `UNMATCHED`.

**`--planning` — `.planning/phases/`** (extract a NEW structure from GSD frontmatter). For rigs
that tracked work in a GSD `.planning/` tree rather than beads:

3. **Structured frontmatter** — each `phases/NN-<name>/` → a proposed **epic**; each
   `NN-MM-PLAN.md` → a proposed **issue** under it, `closed` if a sibling `NN-MM-SUMMARY.md`
   exists; `depends_on:` → proposed **dep** edges (wired at apply).

Emits rows `NEW-epic` / `NEW-issue` (and `PRESENT-in-sync` on re-run, once a bead carries the
doc path as its `external_ref`). This source proposes new beads rather than matching existing
ones, so its whole output flows to the classify step as NEW candidates.

**`--docs <dir>` — an arbitrary markdown tree** (e.g. a prose `.planning/` that is *not* GSD
`phases/`: `decisions/`, `milestones/`, `plans/`, `research/`). Runs the exact same bridges
(0/1/2 + fuzzy shortlist) over every `*.md` under `<dir>` instead of `docs/`. Use it when a rig
kept its history as free-form prose with no frontmatter or add-trailer link — the tool will find
few deterministic matches and hand most docs to the classify step as fuzzy/NEW judgment. The
emitted beads carry `external_ref = <doc path>`, so bridge 0 makes a post-import re-run
`PRESENT-in-sync` (idempotent).

# Why this is first

The deterministic recovery is cheap, correct, and never guesses — so it shrinks the problem
before any judgment is spent. On a tidy rig it resolves the large majority of docs exactly and
hands the agent only the genuine residual.

# Success criteria

- The script exits 0 and prints a match table.
- Every row is one of the known statuses; `UNMATCHED` rows are expected and are the next step's input.

# What can go wrong

| Failure | Likely cause | Action |
|---|---|---|
| `no beads corpus` | not a bd-tracked rig | Abort; wrong target |
| Empty table | no `docs/decisions` or `docs/design` | Abort; nothing to reconcile from docs (consider `.planning`/commit-only path) |
| Wrong prefix in ids | multi-prefix corpus | Re-run with `--prefix <p>` |

# Stale JSONL (auto-export off)

The tool reads a fresh `bd export` snapshot, so its output is always correct even when the rig's
tracked `.beads/issues.jsonl` lags the live DB (the case when bd auto-export is not enabled). This
step never mutates that tracked file. If you *want* to bring it current in the same pass — to
de-stale the rig for file-only consumers (`br`) — that is a write, so gate it: confirm with the
human, then re-run with `--refresh-jsonl`. Permanent fix is rig-init auto-export.

# Notes

Read-only by default. No bead is created or modified; the tracked JSONL is touched only with the
explicit `--refresh-jsonl` opt-in.
