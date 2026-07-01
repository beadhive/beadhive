# Beads + ws work — repo conventions

This repo tracks work with **bd** (beads) and integrates it with git through **`ws`**. Two
rules before anything else:

- **Drive the lifecycle with `ws work`, not raw `bd` / `git`.** `ws work` takes a bead from
  assigned → merged and applies this repo's config defaults (identity, commit signing,
  validation, review gate) for you.
- **Read beads with the first-class verbs** — `ws work ready|issue|list` (dependency-ordered,
  byte/JSON-stable output), not raw `bd` queries.
- **File epics/molecules with `ws plan file`, never hand-create them with `ws bd create`.** The
  planner compiler builds the full envelope a coordinator needs to dispatch — the
  `provider:`/`org:`/`repo:` triplet + dimension labels, the bd swarm, and a per-root kickoff
  gate — which a hand-rolled `ws bd create` epic lacks.
- **`ws bd` is a gated last-resort fallback**, off by default (`passthrough.bd_enabled`): it
  exits non-zero with a steering message until you set `WS_BD_PASS_ENABLED=1` (or `WS_DEBUG=1`).
  Reach for it only for one-off issue surgery the convention verbs above don't cover.
- **Raw `git` is only for local work** — the actual change *inside* a worktree. Never use
  raw `git` / `bd` / `gh` to drive the lifecycle (claim, submit, merge), or you bypass the
  defaults the tooling sets up for you.

Beads is **issues only** — knowledge/memory lives in the project's own system (no
`bd remember`).

## Load the skill for your role

Each role skill states its duties and the verbs it uses; all of them build on the shared
**`work`** skill (the `ws work` verb reference).

| Role | Gas Town | Skill | Duty |
|---|---|---|---|
| Developer | polecat | `developer` | take one assigned bead to a reviewable state |
| Coordinator | overseer | `coordinator` | dispatch beads to developers, watch gates, re-dispatch |
| Merger | refinery | `merger` | serialize merges to the integration branch, preserve history |

## Conventions

- Every issue's home is the `provider:`/`org:`/`repo:` triplet — `ws plan file` injects it when
  it compiles a molecule; `ws labels validate` checks it. Dependencies are declared in the
  molecule spec (`deps:`) and filed by `ws plan file`, not hand-added.
- `ws plan verify <epic>` is the planner's done-gate: it checks a filed molecule against the
  planning-plane conventions (bd swarm, per-root kickoff gate, triplet + closed-dimension
  labels) — the same check `ws work start`/`assign`/`claim` run before dispatch.
- `ws work` reads per-rig defaults from config — load the `work` skill for details.
- **Future follow-up:** cross-rig `ws hub` interchange (`ws plan` / `ws work --rig <id>`) is not
  wired yet.
- Run `bd prime` after compaction or in a new session to reload this context.
