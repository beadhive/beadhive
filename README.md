# bh

`bh` is a single CLI for managing **beads** issue tracking across many repositories. Each
repo is its own beads database (a **rig**) with a short, stable prefix; `bh` onboards them,
keeps their labels consistent, runs `bd`/`git` across one or all of them, and aggregates
every rig into one cross-repo view — even rigs whose code isn't checked out.

It's a thin orchestrator over `bd`, `git`, `git-workspace`, `dolt`, and `docker`: `bh`
encodes the conventions, the registry, validation, and routing. Config and runtime state live
under `~/.ws/`; **no issue data lives here** — each rig's issues live in its own Dolt DB under
`refs/dolt/data` on that repo's own git remote.

`bh` is the **Beadhive** umbrella's workspace CLI — the integration-plane driver for **AGF**
(Agentic Git Flow), the abstract, tracker-independent process. **Beadflow** is that process
implemented on beads: this repo's concrete implementation, unchanged behavior under a naming
layer. See [docs/AGF.md](docs/AGF.md) for the process and
[docs/design/limn-naming-strategy-adr.md](docs/design/limn-naming-strategy-adr.md) for the
naming decision record.

This repo is the CLI's source (Python package `beadhive`, command `bh`).

## Install

```sh
just bootstrap      # brew bundle + mise install + uv sync   (once per machine)
just install        # uv tool install --force '.[otel]' → ~/.local/bin/bh
bh config init      # scaffold ~/.ws (config.yaml, docker-compose.yml, .env.example)
```

Then edit `~/.ws/config.yaml` (and, only if you use the optional Dolt server, copy
`~/.ws/.env.example` → `~/.ws/.env`). The toolchain is pinned in `.mise.toml` + `Brewfile`.

## Docs

New to bh? Start at **[`docs/ONBOARDING.md`](docs/ONBOARDING.md)** — the end-to-end guide
from fresh Mac to a configured AGF workspace with registered rigs.

Everything else — the design and reasoning, configuration, the full command surface, and each
component — starts at **[`docs/OVERVIEW.md`](docs/OVERVIEW.md)**.

## Develop

```sh
just lint    # ruff check
just fmt     # ruff format
just test    # pytest
just build   # uv build
```
