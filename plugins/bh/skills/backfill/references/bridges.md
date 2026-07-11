# Bridge roadmap

A **bridge** is how the reconcile recovers the link between a source artifact and its bead —
either matching an existing bead or extracting a proposed new one. Bridges are tried
**deterministic first, fuzzy last**: a bridge that resolves exactly is always preferred over one
that guesses, because a wrong auto-match silently creates a duplicate or buries real history.

## Implemented

| # | Bridge | Source | How it resolves |
|---|---|---|---|
| 1 | Frontmatter back-ref | any doc | a `Beads: <epic>, <id>` line in the doc → exact id |
| 2 | Git add-trailer | any doc | bead id in the subject of the commit that first added the doc (`git log --follow --diff-filter=A`) → exact id |
| 3 | GSD `.planning/` | GSD phase tree | `phases/NN-<name>/` → epic; `NN-MM-PLAN.md` → issue (closed if a sibling `*-SUMMARY.md` exists); `depends_on:` → dep edges |
| 11 | Fuzzy shortlist | any prose | title-token overlap → a ranked candidate list the agent chooses from; **never auto-links** |

Bridges 1–3 are exact and run in the tool. Bridge 11 narrows the residual but leaves the decision
to the agent — it is the safety valve for docs no structured bridge can resolve, not a matcher.

## Candidate bridges — build on demand, not now

Deliberately **not** built. Each is exact and worth adding **the day a target rig actually uses
that framework** — building parsers for frameworks no rig uses is the speculative generality this
Guide's whole reconcile-first stance exists to avoid. When you do build one, confirm the
framework's real frontmatter keys against its own spec at build time rather than from memory.

| # | Framework | Deterministic map | Notes |
|---|---|---|---|
| 4 | **MADR** (Markdown ADRs) | `docs/decisions/NNNN-*.md`; frontmatter `status`→open/closed, `date`, `deciders`; `Superseded-by`→dep edge | Bridges 1/2 are already a naive MADR reader — this is making them *status-aware*, a small delta |
| 5 | **Nygard ADR** (adr-tools) | `doc(s)/adr/NNNN-*.md`; no frontmatter — fixed `## Status` heading → state; `Supersedes: [N]`/`Amended by` text refs → edges | Heading-parse instead of frontmatter |
| 6 | **Task Master** | `.taskmaster/tasks.json` — `id`/`title`/`status`/`dependencies`/`subtasks` | Strongest possible bridge: already a dep graph, maps 1:1 to beads with zero guessing |
| 7 | **backlog.md** | markdown task files with `id`/`status`/`labels` frontmatter | |
| 8 | **Spec Kit** | `specs/NNN/{spec,plan,tasks}.md` tri-file per feature | feature→epic, tasks→issues |
| 9 | **Keep a Changelog** | `CHANGELOG.md` `## [x.y.z]` sections | → release/version beads |

## Out of scope

- **`Closes #N` / `Fixes #N` → tracker issue.** This links to an external tracker (GitHub/Jira),
  which is *sync*, a different path from local-source backfill. Leave it to the sync tooling.
- **Semantic / embedding search.** Bridge 11 is intentionally lexical. Reach for embeddings only
  if a real prose corpus defeats bag-of-words overlap — and even then, it still only *narrows*;
  the agent still decides.
