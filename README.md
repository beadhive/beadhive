# ws

`ws` is a single CLI for managing **beads** issue tracking across many repositories. Each
repo is its own beads database (a **rig**) with a short, stable prefix; `ws` onboards them,
keeps their labels consistent, runs `bd`/`git` across one or all of them, and aggregates
every rig into one cross-repo view — even rigs whose code isn't checked out.

It's a thin orchestrator over `bd`, `git`, `git-workspace`, `dolt`, and `docker`: `ws`
encodes the conventions, the registry, validation, and routing. Config and runtime state live
under `~/.ws/`; **no issue data lives here** — each rig's issues live in its own Dolt DB under
`refs/dolt/data` on that repo's own git remote.

This repo is the CLI's source (Python package `ws`, command `ws`).

## Install

```sh
just bootstrap      # brew bundle + mise install + uv sync   (once per machine)
just install        # uv tool install . → ~/.local/bin/ws
ws config init      # scaffold ~/.ws (config.yaml, docker-compose.yml, .env.example)
```

Then edit `~/.ws/config.yaml` (and, only if you use the optional Dolt server, copy
`~/.ws/.env.example` → `~/.ws/.env`). The toolchain is pinned in `.mise.toml` + `Brewfile`.

## Docs

Everything else — the design and reasoning, configuration, the full command surface, and each
component — starts at **[`docs/OVERVIEW.md`](docs/OVERVIEW.md)**.

## Develop

```sh
just lint    # ruff check
just fmt     # ruff format
just test    # pytest
just build   # uv build
```
