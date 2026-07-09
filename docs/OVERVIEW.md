# bh — overview

`bh` is a single CLI for managing **beads** issue tracking across many repositories. Each
repo is its own beads database (a **rig**) with a stable prefix; `bh` onboards them, keeps
their labels consistent, runs `bd`/`git` across one or all of them, and aggregates every
rig into one cross-repo view — even rigs whose code isn't checked out.

It's a thin orchestrator: the heavy lifting is delegated to `bd` (beads), `git`,
`git-workspace`, `dolt`, and `docker`. `bh` encodes the conventions, the registry, the
validation, and the routing.

```text
$GIT_WORKSPACE (default: ~/workspace)   the canonical HQ launch directory
   └─ <provider>/<org>/<repo>/         each repo = a rig (embedded Dolt in .beads/)
        │  bd dolt push → refs/dolt/data on the repo's own git remote
        ▼
   ~/.ws/hub                             ← bh sync aggregates every rig (cloned by path, uncloned by cache)
                                           bh hq bd ready → actionable work across the whole workspace
```

## Mental model in one breath

A **rig** is a repo's beads DB. Its issues carry a short, stable **prefix** (`ag-infra-1`).
Repo identity that can change (provider, org) lives in **labels**, not the prefix. Issue
history is stored on the repo's **own git remote** under `refs/dolt/data` — no central
database to run. The **hub** (`~/.ws/hub/`) is the cross-rig aggregation built by `bh sync`.
A **Factory HQ** (`~/.ws/hq/`, if registered) is an evolved durable control-plane store that
subsumes the hub's role; when present, `bh hq …` and `bh sync` use it instead. The hub and
HQ share the same aggregation mechanism but run at different scales (personal hub vs. shared
Factory HQ). `git-workspace` (optional) tells `bh` what repos exist and unlocks fleet operations.

## Command map

| Command | What |
|---|---|
| `bh rig init` | onboard the current repo as a rig → [RIGS](RIGS.md) |
| `bh bd …` / `bh git …` | passthrough to beads/git, with `-a`/`-r` rig routing → [PASSTHROUGH](PASSTHROUGH.md) |
| `bh labels …` | validate / sync / report / docs the registry → [LABELS](LABELS.md) |
| `bh sync` / `bh hq …` | build & query the HQ aggregate (cross-rig) → [HUB](HUB.md) |
| `bh work …` | drive a bead assigned → merged → [WORK](WORK.md), [BEADS-SYNC](BEADS-SYNC.md) |
| `bh doctor` | status + diagnostics → [DIAGNOSTICS](DIAGNOSTICS.md) |
| `bh dolt …` | optional local Dolt server → [DOLT](DOLT.md) |
| `bh backup` / `bh config …` | JSONL export / config management → [CONFIGURATION](CONFIGURATION.md) |

## Documentation

- **[ONBOARDING](ONBOARDING.md)** — fresh Mac → configured AGF workspace (Phases 0–6 + skip-points).
- **[DESIGN](DESIGN.md)** — the model and the reasoning: rigs, prefixes, labels,
  identity-over-time, hosting, the hub. Start here for *why*.
- **[CONFIGURATION](CONFIGURATION.md)** — `~/.ws/`, `config.yaml` schema, env vars.
- **[CLI](CLI.md)** — command tree, help panels, the global `-a`/`-r` routing flags.
- **[RIGS](RIGS.md)** — onboarding, rig kinds, prefix & identity derivation, agent extras.
- **[LABELS](LABELS.md)** — the label taxonomy, dimensions, validation & enforcement.
- **[PASSTHROUGH](PASSTHROUGH.md)** — `bh bd` / `bh git` and rig routing.
- **[HUB](HUB.md)** — `bh sync` / `bh hq` and the cross-rig aggregate.
- **[WORK](WORK.md)** — `bh work`, the bead lifecycle driver (assigned → merged).
- **[BEADS-SYNC](BEADS-SYNC.md)** — distributing issue state to agents over Dolt git refs.
- **[INTEGRATIONS](INTEGRATIONS.md)** — the optional git-workspace integration.
- **[DIAGNOSTICS](DIAGNOSTICS.md)** — `bh doctor` (status + warnings).
- **[DOLT](DOLT.md)** — the optional local Dolt SQL server.

## Install

```sh
just bootstrap      # brew bundle + mise install + uv sync
just install        # uv tool install --force '.[otel]' → ~/.local/bin/bh
bh config init      # scaffold ~/.ws (config.yaml, docker-compose.yml, .env.example)
```

Python package `beadhive`; command `bh`; config home `~/.ws/`. See [CONFIGURATION](CONFIGURATION.md).
