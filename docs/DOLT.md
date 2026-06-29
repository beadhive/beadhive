# Dolt server (optional)

A standalone Dolt SQL server you can run locally (module: `dolt.py`). It is **optional infra**
— `ws` does not require it, and rigs are not wired to it by default.

## When you'd want it

You don't need it for normal use: rigs are embedded Dolt under each repo's `.beads/`, issue
data is distributed via `refs/dolt/data` on git remotes, and the cross-rig view is the
[hub](HUB.md) on disk. Stand this up only for a **shared/central backend** (e.g. a homelab
host multiple machines connect to), a **backup Dolt remote** independent of the git mirror, or
to host the hub on a server. (Pointing rigs *at* this server is not yet wired — it's
scaffolding for that future role. See [DESIGN](DESIGN.md#hosting-on-the-repos-own-git-remote).)

## Commands

```sh
ws dolt up          # backend ensure-up → compose up -d → provision
ws dolt provision   # wait for the app user, then GRANT privileges (idempotent)
ws dolt down
ws dolt logs | ps | sql
```

- **`up`** starts the container runtime (per backend), brings up the compose service, then
  provisions. **`provision`** waits for the beads app user to accept connections (the Dolt
  image creates it *after* the server starts listening), then grants it privileges.
- Config: `~/.ws/docker-compose.yml` + `~/.ws/.env` (database defaults to `workspace`, app
  user `beads`). Scaffold with `ws config init`.

## Pluggable container backend

Chosen by `dolt.backend` in `config.yaml` — a thin dispatch, not a plugin framework:

| backend | pre-step before compose | runtime |
|---|---|---|
| `colima` | `colima start` if not running (mac VM) | docker |
| `docker` | none (native daemon assumed) | docker |
| `podman` | `podman machine start` | podman |
| `none` | none (server managed elsewhere) | docker |

The compose command is auto-detected (`docker compose`, else `docker-compose`; `podman
compose` for podman) and overridable via `dolt.compose`. Adding a backend is a few lines in
`dolt.py` — no new file.
