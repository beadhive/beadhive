---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/guide.schema.json
guide:
  id: backfill
  version: "0.1.0"
  summary: |
    Reconstruct a rig's history as beads from its own local sources — reconcile-first, so an
    existing bead corpus is de-duplicated rather than clobbered. Recover each source doc's bead
    via deterministic bridges (frontmatter back-ref, then the id in the commit that added the
    doc); leave the residual to agent judgment; emit a diff; apply only on human confirm.
  goal_state: |
    Every source doc that maps to work is linked to exactly one bead: pre-existing beads gain an
    external_ref pointing at their source; genuinely missing history is filed as new (closed if
    history says done); nothing is duplicated. A re-run of the reconcile proposes zero changes.
  prerequisites:
    - id: bead-rig
      performer: agent
      description: Target is a bd-tracked rig — a `.beads/issues.jsonl` corpus exists.
    - id: local-sources
      performer: agent
      description: The rig has local sources to mine — `docs/decisions/`, `docs/design/`, and/or `.planning/`.
    - id: apply-authority
      performer: human
      description: A human is present to review the proposal and authorize (or decline) the write.
  external_resources:
    - title: Reconcile tool
      path: scripts/reconcile.sh
    - title: Bridge roadmap
      path: references/bridges.md
  rollback_strategy: none
  end_states:
    - id: reconciled
      description: "Proposal applied; every doc-backed bead linked and any NEW gaps filed; idempotency re-run is clean."
      score: 1.0
    - id: nothing-to-do
      description: "Rig was already fully linked; reconcile proposed no changes."
      score: 1.0
    - id: proposal-only
      description: "Human reviewed the diff and declined to apply; the proposal is the delivered artifact."
      score: 0.5
  estimated_duration_minutes: 20
  tags: [agf, beads, backfill, reconcile, provenance]
  requires:
    tools:
      - "git@>=2"
      - "jq@>=1.6"
      - "bd"
---

# When to use this Guide

You are onboarding or catching up a bd-tracked rig that has **real history** — commits, ADRs,
design docs, planning corpora — and you want that history represented as beads. The rig may
already have a **partial** bead corpus (work tracked natively from some point on); this Guide
exists precisely to avoid duplicating it.

# Also handles (not a reason to avoid it)

- **Empty-corpus rigs** (no beads yet) — use this Guide as-is. Reconcile against an empty corpus
  *is* import: the same steps run, the matching just finds nothing, so every artifact classifies
  NEW. No steps are skipped and no separate guide is needed — empty is the degenerate case.
  Idempotency is unchanged (`external_ref` / bridge 0), so a re-run is still a no-op. The only
  difference is the apply mechanism (see step `apply`): stamping existing beads vs. bulk-creating
  new ones.

# When NOT to use this Guide

- **Pulling from an external tracker** (GitHub/Jira/Linear issues) — that's tracker sync, a
  different path. This Guide mines *local* sources only.
- **Live forward work** — this is retrospective reconstruction, not the normal develop loop.

# Decision criteria during execution

- **Deterministic before fuzzy.** `scripts/reconcile.sh` recovers links it is *certain* of
  (frontmatter back-ref, then git add-trailer) and refuses to guess. Whatever it marks
  `UNMATCHED` is the agent's judgment residual — match by title/content only if confident, else
  treat as NEW.
- **NEW vs noise.** A commit or doc becomes a bead only if it represents work worth tracking. If
  a version/phase epic already covers the range at a coarser grain, link to the epic — do not
  shatter it into per-commit beads.
- **PRESENT beads are native, not backfilled.** They get the `external_ref` link only. Only
  genuinely NEW beads carry `origin:backfill` + `source:<kind>`.
- **Never write without the human seeing the diff.** The emit-proposal step is a hard confirm
  gate; apply is a separate step.

# What you need at hand

- The path to the target rig's working tree.
- Ten minutes to eyeball the proposal — the match table is short; the judgment residual is where
  your attention goes.
