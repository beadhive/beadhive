# Project Instructions for AI Agents

## Agentic Git Flow (AGF)

This repo authors `bh` (Beadhive), the **integration-plane driver** for AGF, and is driven by
it. AGF is the abstract, tracker-independent process; **Beadflow** is that process implemented
on beads — what this repo's tool (`bh`) drives. Don't improvise raw `git` / `gh pr` for the
lifecycle — drive beads through `bh work` and load the role skill for your seat (`coordinator`
/ `developer` / `merger` / `work`).

See **[docs/AGF.md](docs/AGF.md)** for the tenets, the one-terminal loop, and which skill to
load when.

<!-- ws:agf:start (managed by `bh rig init` — edit outside these markers; `-f` refreshes) -->
## AGF — Agentic Git Flow

This repo is onboarded as a **`bh` rig** and develops via **AGF**: work is tracked in beads
and driven through `bh`, **not** raw `git` / `bd` / `gh`.

- **Is this repo set up for AGF?** → run `bh rig ready` (add `-v` for the line-item breakdown).
- **Lifecycle, roles, conventions:** see `.beads/PRIME.md` and `docs/AGF.md`.
- Drive beads with `bh work`; load the role skill for your seat (coordinator / developer / merger).
<!-- ws:agf:end -->
