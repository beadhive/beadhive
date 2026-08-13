# Dolt

Two different things share the word "server" in this project, and conflating them is the most
likely way to misread everything below:

| | **the store engine** | **the optional central server** |
|---|---|---|
| what it is | where a hive's beads actually live | a standalone Dolt SQL server you run yourself |
| who runs it | `bd`, automatically | you, via `bh dolt up` (compose) |
| do hives use it | **yes — every hive, always** | no; nothing points at it today |
| module | `store_locator.py` / `dolt_health.py` | `dolt.py` |
| section | [below](#the-store-engine) | [below](#the-optional-central-server) |

## The store engine

Every hive's issue data lives in a Dolt database. **Since 0.8.0, a newly onboarded hive lands
on `bd`'s own shared `dolt sql-server`** — one server per host, spawned and supervised by `bd`
itself, listening on `127.0.0.1:3308`, with each database under
`~/.beads/shared-server/dolt/<database>`. This is mode (a) of
[dolt-server-mode-adr.md](design/dolt-server-mode-adr.md), and it is the fleet's target mode,
not a per-hive opt-in.

There is nothing to start. `bd` auto-starts the shared server when a command needs it, and
`bh hive init` / `bh hq init` set the mode as part of onboarding — `dolt_mode: "server"` in the
hive's `.beads/metadata.json`, `dolt.shared-server: true` in its `.beads/config.yaml`.

**The older mode is embedded**: an in-process engine under the repo's own
`.beads/embeddeddolt/`, one exclusive file lock per store, no server and no liveness question.
Hives created before 0.8.0 are embedded and **stay embedded across an upgrade** — re-running
onboarding never moves one. Moving them is `bh hive migrate-storage`'s job; see
[UPGRADING.md](UPGRADING.md#07x--080--the-store-engine-moves-to-bds-shared-dolt-server).

Either way, distribution is unchanged and git-native: issue history travels as `refs/dolt/data`
on each repo's own git remote, and the cross-hive view is the [hub](HUB.md) on disk. Storage
mode is a *local* engine choice; it is not where your data is published.

### Which mode is this hive on

The persisted answer is a filesystem fact, never a live probe — `bh` reads
`.beads/metadata.json`, because `bd dolt status --json` is ambiguous by mode (it has been
measured reporting `"running": false` for a live server answering real queries).

```sh
bh doctor          # store engine section: mode per hive + one endpoint probe
bh hive ready      # same check, scoped to one hive
```

Server mode is the only mode that can be *down*, so those two report it: a real socket probe of
`127.0.0.1:3308` (override with `bd`'s own `BEADS_DOLT_SERVER_HOST` / `BEADS_DOLT_SERVER_PORT`),
plus an engine/metadata mismatch check — a hive whose config says shared-server while its
metadata still says embedded is drift worth knowing about, not a cosmetic disagreement. The
behavior-on-down decision is
[dolt-store-engine-liveness-adr.md](design/dolt-store-engine-liveness-adr.md).

### Why the move

Measured on one machine, 2026-08-03 — full method and caveats in the ADR and in
[bh-00cq](spikes/bh-00cq-external-dolt-sql-server.md):

| | embedded | shared |
|---|---|---|
| 5 readers + 1 writer, median of 3 | 3.456 s | **0.720 s** |
| engine-open + query, A/B on identical cloned data | 187 ms | **55 ms** |
| cold 306 MB clone from the production remote | hung past 240 s | 10 s |
| fleet disk (22 hives × `.beads/embeddeddolt`) | ~2.8 GB | reclaimed |

## The optional central server

A standalone Dolt SQL server you can run locally under compose (module: `dolt.py`). It is
**optional infra** — `bh` does not require it, and it is *not* what the section above describes.
`bh dolt up` does not serve your hives; `bd`'s shared server already does that.

### When you'd want it

Stand this up only for a **shared/central backend** (e.g. a homelab host several machines
connect to), a **backup Dolt remote** independent of the git mirror, or to host the hub on a
server. Pointing hives at *this* server — an external endpoint rather than the per-host shared
one — is mode (c) in the ADR: still unimplemented, tracked as `bh-z41i` ((c)-local) and
`bh-3mik` ((c)-remote), both downstream of mode (a).

### Commands

```sh
bh dolt up          # backend ensure-up → compose up -d → provision
bh dolt provision   # wait for the app user, then GRANT privileges (idempotent)
bh dolt down
bh dolt logs | ps | sql
```

- **`up`** starts the container runtime (per backend), brings up the compose service, then
  provisions. **`provision`** waits for the beads app user to accept connections (the Dolt
  image creates it *after* the server starts listening), then grants it privileges.
- Config: `~/.beadhive/docker-compose.yml` + `~/.beadhive/.env` (database defaults to `workspace`, app
  user `beads`). Scaffold with `bh config init`.

### Zombie servers, and which source of truth wins

A `dolt sql-server` whose datadir has been **unlinked underneath it** keeps listening and keeps
answering the MySQL handshake. Every liveness probe bh had was a probe of the *port*, so a zombie
reported `✓ reachable` — truthfully, and uselessly. One on `beadhive-factory` started 2026-08-05,
**survived a deliberate host wipe and reinstall**, and kept serving 127.0.0.1:3308 from a
directory that no longer existed.

`bh doctor`'s **Store Engine** section now inventories every running server:

```text
  dolt servers on this host: 6
    ✓ pid 1595647  [shared]  /home/bees/.beads/shared-server/dolt
    ✓ pid 1161710  [cache]   /home/bees/.beadhive/cache/github/briancripe/agentic-git-flow/.beads/dolt
    ✗ pid 8080     [shared]  /home/bees/.beads/shared-server/dolt  ← DATADIR IS GONE (zombie)
  reconciliation: dolt.backend=docker, shared-server dir present (/home/bees/.beads/shared-server)
    authoritative: the running process — …
```

Three things can disagree about the store engine, and bh now says **which one it believes**:

| source | states | authority |
|---|---|---|
| `dolt.backend` in config | an *intention* | lowest |
| the shared-server directory | what was *laid down* | middle |
| the running process | what is *happening* | **authoritative** |

The running process wins because it is the only one that can be serving queries right now — on
the host above, config said `docker` and the filesystem said "no shared-server directory" while a
native nix dolt was answering on 3308.

The `[cache]` row is a category nothing previously named: **bd starts one server per hydrated
cache hive as well as the shared one**, so "how many dolt servers should be running here" had no
stated answer at all, and eight live processes could not be sorted into fleet vs leak.

`bh host provision` fails its verify step while a zombie is running, so re-provisioning a wiped
host cannot silently adopt a dead datadir. It **detects and refuses**; it does not kill or
restart. `bd dolt stop` cannot stop the shared server (bd calls it *external* and refuses), so
the remedy is a SIGTERM to the pid `bh doctor` now shows you, then a restart from the `--config`
path it printed alongside.

### Pluggable container backend

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
