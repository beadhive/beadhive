# bh — overview

`bh` is a single CLI for managing **beads** issue tracking across many repositories. Each
repo is its own beads database (a **hive**) with a stable prefix; `bh` onboards them, keeps
their labels consistent, runs `bd`/`git` across one or all of them, and aggregates every
hive into one cross-repo view — even hives whose code isn't checked out.

It's a thin orchestrator: the heavy lifting is delegated to `bd` (beads), `git`,
`git-workspace`, `dolt`, and `docker`. `bh` encodes the conventions, the registry, the
validation, and the routing.

```text
$GIT_WORKSPACE (default: ~/workspace)   the canonical HQ launch directory
   └─ <provider>/<org>/<repo>/         each repo = a hive (its own Dolt DB → DOLT.md)
        │  bd dolt push → refs/dolt/data on the repo's own git remote
        ▼
   ~/.beadhive/hub                       ← bh sync aggregates every hive (cloned by path, uncloned by cache)
                                           bh hq bd ready → actionable work across the whole workspace
```

## Mental model in one breath

A **hive** is a repo's beads DB. Its issues carry a short, stable **prefix** (`ag-infra-1`).
Repo identity that can change (provider, org) lives in **labels**, not the prefix. Issue
history is stored on the repo's **own git remote** under `refs/dolt/data` — no central
database to run. The **hub** (`~/.beadhive/hub/`) is the cross-hive aggregation built by `bh sync`.
A **Factory HQ** (`~/.beadhive/hq/`, if registered) is an evolved durable control-plane store that
subsumes the hub's role; when present, `bh hq …` and `bh sync` use it instead. The hub and
HQ share the same aggregation mechanism but run at different scales (personal hub vs. shared
Factory HQ). `git-workspace` (optional) tells `bh` what repos exist and unlocks fleet operations.

## Command map

| Command | What |
|---|---|
| `bh hive init` | onboard the current repo as a hive → [HIVES](HIVES.md) |
| `bh bd …` / `bh git …` | passthrough to beads/git, with `-a`/`-r` hive routing → [PASSTHROUGH](PASSTHROUGH.md) |
| `bh label …` | validate / sync / report / docs the registry → [LABELS](LABELS.md) |
| `bh sync` / `bh hq …` | build & query the HQ aggregate (cross-hive) → [HUB](HUB.md) |
| `bh work …` | drive a bead assigned → merged → [WORK](WORK.md), [BEADS-SYNC](BEADS-SYNC.md) |
| `bh doctor` | status + diagnostics → [DIAGNOSTICS](DIAGNOSTICS.md) |
| `bh dolt …` | the *optional* central Dolt server (not the store engine) → [DOLT](DOLT.md) |
| `bh backup export\|usage\|reclaim` / `bh config …` | JSONL export mirror + backup-root disk usage/retention / config management → [CONFIGURATION](CONFIGURATION.md) |

## Documentation

Four of these answer "how do I get from here to there", and each owns exactly one axis —
**route** (which install), **phase** (where you start), **migration** (moving off an old
version or route) and **depth** (how far you take it). They link across axes rather than
summarising each other; [ADOPTION](ADOPTION.md#see-also) carries the table.

- **[INSTALL](../INSTALL.md)** — *route*: getting `bh` onto this machine — managed path, PyPI,
  Docker.
- **[ONBOARDING](ONBOARDING.md)** — *phase*: fresh Mac → configured Beadflow workspace
  (Phases 0–6 + skip-points).
- **[ADOPTION](ADOPTION.md)** — *depth*: the four rungs — one laptop, HQ remote, managed
  toolchain, a Linux executor — what each buys and what staying put costs.
- **[DESIGN](DESIGN.md)** — the model and the reasoning: hives, prefixes, labels,
  identity-over-time, hosting, the hub. Start here for *why*.
- **[CONFIGURATION](CONFIGURATION.md)** — `~/.beadhive/`, `config.yaml` schema, env vars.
- **[CLI](CLI.md)** — command tree, help panels, the global `-a`/`-r` routing flags.
- **[HIVES](HIVES.md)** — onboarding, hive kinds, prefix & identity derivation, agent extras.
- **[LABELS](LABELS.md)** — the label taxonomy, dimensions, validation & enforcement.
- **[PASSTHROUGH](PASSTHROUGH.md)** — `bh bd` / `bh git` and hive routing.
- **[HUB](HUB.md)** — `bh sync` / `bh hq` and the cross-hive aggregate.
- **[WORK](WORK.md)** — `bh work`, the bead lifecycle driver (assigned → merged).
- **[BEADS-SYNC](BEADS-SYNC.md)** — distributing issue state to agents over Dolt git refs.
- **[INTEGRATIONS](INTEGRATIONS.md)** — the optional git-workspace integration.
- **[DIAGNOSTICS](DIAGNOSTICS.md)** — `bh doctor` (status + warnings).
- **[DOLT](DOLT.md)** — the store engine hives run on (bd's shared server since 0.8.0), and the
  separate, optional local Dolt SQL server.
- **[CONTAINER](CONTAINER.md)** — Beadhive in a box: the four volumes, the two image targets,
  credentials, and the host-handoff runbook.
- **[UPGRADING](UPGRADING.md)** — *migration*: narrative upgrade notes for releases that change
  on-disk state or the CLI surface (start here after a version bump;
  [CHANGELOG.md](../CHANGELOG.md) has the mechanical per-commit list).

## Install

```sh
just bootstrap      # brew bundle + mise install + uv sync
just install        # build + install this checkout '.[otel]' → ~/.local/bin/bh
bh config init      # scaffold ~/.beadhive (config.yaml, docker-compose.yml, .env.example)
```

`just install` stamps the build with a PEP 440 local segment, so `bh --version`
reports `0.11.5+local.g790ef0d` (`.dirty` when the checkout had uncommitted
changes) rather than the released number it would otherwise be indistinguishable
from. PyPI forbids local segments, so such a build can never be published.

Python package `beadhive`; command `bh`; config home `~/.beadhive/`. See [CONFIGURATION](CONFIGURATION.md).
