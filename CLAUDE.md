# Project Instructions for AI Agents

## Agentic Git Flow (AGF)

This repo authors `bh` (Beadhive), the **integration-plane driver** for AGF, and is driven by
it. AGF is the abstract, tracker-independent process; **Beadflow** is that process implemented
on beads — what this repo's tool (`bh`) drives. Don't improvise raw `git` / `gh pr` for the
lifecycle — drive beads through `bh work` and load the role skill for your seat (`coordinator`
/ `developer` / `merger` / `work`).

See **[docs/AGF.md](docs/AGF.md)** for the tenets, the one-terminal loop, and which skill to
load when.

## Bead history is an archive — never squash it

**Do not run `bd compact` or `bd flatten` on any hive.** Not with a retention window, not
"just the old commits", not to reclaim disk. If you believe a hive needs either, stop and ask
the operator.

Bead version history is the *only* record of when each lifecycle stage happened — claim,
close, reopen — reconstructable by diffing consecutive versions from `bd history <id> --json`.
Both commands destroy that permanently: `compact` squashes everything past its retention
window, and `flatten` squashes all of it, **prunes remote-tracking refs, and runs Dolt GC**, so
the next push replaces the remote's copy too. There is no archive tier to fall back on yet.

This is not hypothetical. `bh-art` was created 2026-06-29 and its reachable history begins
2026-07-25 — about a month is already gone, and nothing surfaced that loss at the time.

A two-tier design (permanent archive on the remote + a compacted working copy for the open
frontier) is filed as `bh-3vs6c`. Until it lands, both commands are one-way.

<!-- bh:agf:start (managed by `bh hive init` — edit outside these markers; `-f` refreshes) -->
## AGF — Agentic Git Flow

This repo is onboarded as a **`bh` hive** and develops via **AGF**: work is tracked in beads
and driven through `bh`, **not** raw `git` / `bd` / `gh`.

- **Is this repo set up for AGF?** → run `bh hive ready` (add `-v` for the line-item breakdown).
- **Lifecycle, roles, conventions:** see `docs/AGF.md` and the bh plugin's role skills.
- Drive beads with `bh work`; load the role skill for your seat (coordinator / developer / merger).
<!-- bh:agf:end -->
