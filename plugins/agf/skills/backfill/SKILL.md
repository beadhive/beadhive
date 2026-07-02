---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/skill-guide-extension.schema.json
name: backfill
description: >-
  Reconstruct a rig's history as beads from its own local sources (git log, decision/design
  docs, planning frontmatter) WITHOUT duplicating beads that already exist. Activate when
  onboarding or catching up a repo that has real history — and possibly a partial bead corpus —
  and you want that history represented as beads. Runs propose → reconcile → classify → suggest
  and stops for human confirmation; never blind-imports.
license: MIT
compatibility: >-
  Assumes a bd-tracked rig (a `.beads/issues.jsonl` corpus) and local sources under
  `docs/decisions/`, `docs/design/`, and/or `.planning/`. Deterministic bridge recovery is
  handled by the bundled `scripts/reconcile.sh` (git + jq + bd, no other deps); fuzzy matching
  and NEW-vs-noise calls stay with the agent.
allowed-tools: Bash Read Edit Grep AskUserQuestion
metadata:
  type: guide
  guide:
    entry: GUIDE.md
---

# Backfill (agent-assisted, reconcile-first)

This Skill is a **Guide**. A Guide-aware harness loads `GUIDE.md` to begin a run; a plain-Skill
harness should read `GUIDE.md` for framing, then walk `steps/` in order.

Core rule: **a backfill is a reconcile, not an import.** Every run proposes candidate beads from
the rig's own sources, matches them against the beads already present, and writes only the gap —
so a re-run is idempotent and an already-well-tracked rig degenerates to provenance-stamping
rather than duplicate creation.

The deterministic half (recover the doc↔bead link, join against the corpus, classify) is done by
`scripts/reconcile.sh`. The judgment half (fuzzy fallback, is-this-NEW-or-noise, drift) stays with
the agent. Nothing is written until a human sees the diff.
