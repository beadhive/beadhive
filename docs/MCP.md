# MCP — `bh mcp serve` / `bh-mcp`

`bh` exposes a FastMCP server over stdio for MCP clients. It is an **optional extra** — the
core CLI works without it.

## CLI vs MCP

Use MCP tools only when structured I/O is the advantage — passing a typed spec in and getting
structured results back, with no temp-file marshalling or CLI-string scraping. Simple and bulk
commands offer no such advantage; they stay CLI-only.

**Token-efficiency policy:** the MCP tool surface is intentionally minimal. Agents running
simple commands (`bh sync`, `bh doctor`, `bd …`) should call the CLI directly. MCP tool
docstrings point to the CLI for everything not on the exposed list.

## Install

FastMCP ships as a core dependency of `bh`, so the MCP server is available from a plain
install. Add the `[otel]` extra so the server can also export OpenTelemetry signals (see
[OBSERVABILITY.md](OBSERVABILITY.md)). Only a broken install (fastmcp somehow missing)
makes `bh mcp serve` and `bh-mcp` print a friendly error and exit 1; the observaloop
integration then reports unavailable.

```sh
uv tool install 'beadhive[otel]'   # or: pip install 'beadhive[otel]'
```

`just install` (the development recipe) installs the `[otel]` extra automatically.

## Run

```sh
bh mcp serve   # bh subcommand (stdio, blocking)
bh-mcp         # standalone console-script (same)
```

Both print a friendly error and exit 1 only if fastmcp is missing (a broken install).

## Distributed via the agf plugin

The `bh` MCP server is bundled with the AGF Claude Code plugin and registered automatically
at user scope when the plugin is installed. No manual `claude mcp add` step is needed.

```sh
# install the plugin — registers the bh MCP server at user scope
claude plugin marketplace add <path-to-workspace>
claude plugin install agf@workspace --scope user

# confirm registration
/mcp   # bh should appear as "connected"
```

The server uses the `.mcp.json`-at-root convention (Claude Code auto-discovers it):

```json
{
  "mcpServers": {
    "bh": { "command": "bh-mcp", "args": [] }
  }
}
```

The plugin launches the server via the `bh-mcp` console-script entry-point rather than
`bh mcp serve`.  The distinction matters: `bh mcp serve` is gated behind the
`bh setup check` cache and exits 1 before the MCP handshake when that cache is
absent or stale (producing an opaque `-32000` in the client).  `bh-mcp` has no such
gate — it answers `initialize` cleanly regardless of cache state, and fails gracefully
with exit 1 + a reinstall hint only when fastmcp is missing (a broken install).

Because fastmcp is a core dependency, a normal install starts the server; only a broken
install (fastmcp missing) makes `bh-mcp` exit 1 with a friendly error while the plugin
still declares the server. `bh doctor` reports whether fastmcp and the plugin declaration
are in place.

### Enable / disable

| Method | Command / setting |
|---|---|
| Disable in `/mcp` | Toggle the `bh` entry off in the Claude Code MCP panel |
| Disable plugin | `claude plugin disable agf@workspace` (removes all plugin-provided servers) |
| Re-enable | `claude plugin enable agf@workspace` or toggle in `/mcp` |
| Fine-grained | `enabledMcpjsonServers` / `disabledMcpjsonServers` in Claude Code settings |

### CLI vs MCP — when to use each

Prefer the **CLI** for simple, bulk, or fire-and-forget operations — `bh sync`, `bh doctor`,
`bd …`, one-off rig commands. The CLI is always available, needs no server, and produces
human-readable output with no overhead.

Prefer **MCP** when structured I/O is the advantage: passing a typed spec in and getting a
typed result back (no temp-file marshalling, no CLI-string scraping), or reading live state
via MCP resources (`beadhive://work/ready`, `beadhive://doctor`, etc.) in a subscription loop.

The token-efficiency policy above still applies: the MCP surface is intentionally minimal.
Most operations stay CLI-only; only the tools listed below justify the MCP path.

## Exposed tools

These tools are registered. Everything else stays CLI-only.

### Planning / work plane

| Tool | Inputs | Output |
|---|---|---|
| `plan_check` | `spec: dict` | `{valid, problems}` |
| `plan_file` | `spec: dict`, `rig?: str`, `dry_run?: bool` | `{epic_id, issue_count, root_count}` or dry-run preview |
| `work_refine` | `bead: str`, mode (`squash_plan`/`autosquash`/`since`), `rig?`, `dry_run?` | `{subjects, branch, backup, log, …}` |
| `bd_create` | `issues: list[dict]`, `rig?: str` | `{created, count}` |

### Control plane (custodian)

| Tool | Inputs | Output |
|---|---|---|
| `config_set` | `key: str`, `value`, `type?: str` (`"json"`/`"string"` coercion hint) | `{ok, problems, old, new}` |
| `rig_add` | `provider`, `org`, `repo`, `prefix?`, `kind?`, `upstream?` | `{prefix, kind, registered}` |
| `rig_onboard` | `provider`, `org`, `repo`, `clone_url?`, `prime?`, `claude?`, `skills?`, `observaloop?` | `{cloned, registered, prefix, synced, warnings[]}` |
| `rigs_status` | _(none)_ | `{candidates[], collisions[], violations[], rigs[]}` |
| `rigs_available` | _(none)_ | `{candidates[], registered[]}` |

`config_set` is **delta-apply**: one dotted key per call (a value-level write, not a
whole-config schema). A `value` that is already a list/map, or a string passed with
`type: "json"`, takes the `--json` round-trip path so structured config keys survive; a plain
string gets the CLI's `true|false`→bool / digits→int coercion (use `type: "string"` to force a
literal). Validation problems come back as `ok: false` + `problems` (writing nothing) rather
than as an error — the structured advantage over the CLI.

**Intentionally CLI-only** (no structured-I/O advantage, or destructive): `config get` (a
single scalar read), `rig rm` (destructive unregister), `bh sync`, `bh doctor`. `rigs_status` is
the richer superset of `rigs_available` — use `rigs_available` when you only need the
`bh rig add` candidates.

Core exceptions (`MoleculeError`, `PlanError`, `WorkError`, and the config/rig failure modes)
map to `ToolError` so the client receives a clean, actionable message instead of a stack trace.

## Exposed resources

Resources expose read-only state over MCP's resource subscription model. All resources return
`application/json` and are **always fresh on read** — there is no caching layer.

| Resource | Description |
|---|---|
| `beadhive://probe/health` | Service health probe; confirms MCP registration. |
| `beadhive://config` | Resolved config dict (full workspace config state). |
| `beadhive://config/{key}` | Single config value by dotted key path. |
| `beadhive://doctor` | Structured workspace diagnostics (config/providers/orgs/rigs overview + inventory, disk_usage, fleet_health, worktrees, molecules, mcp, observability, warnings). |
| `beadhive://rigs/status` | Richer workspace status view: candidates (unregistered repos), collisions, violations, and all registered rigs. |
| `beadhive://rigs/available` | Discoverable-but-unregistered repos; diffs git-workspace's tracked repos against registered rigs. |
| `beadhive://rigs/survey` | Fleet onboarding table, one row per on-disk repo. |
| `beadhive://labels/validation` | Label validation findings: required_violations, per-issue problems, db_ok flag. |
| `beadhive://worktrees` | Worktree classification status for all managed rigs (SAFE/ACTIVE/DIRTY/REVIEW/UNMERGED/LANDED_REBASED/DETACHED/MERGED_ORPHAN/ABANDONED). |
| `beadhive://work/ready` | Ready (unblocked, dependency-ordered) beads for the current rig. |
| `beadhive://work/intake` | Untriaged intake inbox: rows (open intake beads) and dupes (mechanical duplicate pairs). |
| `beadhive://work/intake/dupes` | Duplicate-pair candidates for intake queue only; subset of mechanical-dedup pairs. |
| `beadhive://work/issue/{id}` | Single bead by id (template resource). |
| `beadhive://work/show/{id}` | Bead branch local history: base commit, max_commits limit, flagged commits for `base..branch`. |
| `beadhive://plans` | Swarm list for the current rig (molecule dashboard). |
| `beadhive://plan/{ref}` | Single molecule status by swarm ref. |
| `beadhive://hq/intake` | Fleet-wide untriaged intake inbox, aggregated across the hub. |
| `beadhive://work/schedule/{epic}` | Epic schedule plan: epic kickoff status and bead timing windows. |

### Freshness and subscription

Resources are **dynamic** and always fresh on read. A subscribed client does not cache; instead,
it re-reads when invalidated via `resources/updated` notification.

Mutating MCP tools emit `resources/updated` for the URIs they invalidate:

| Tool | Invalidates |
|---|---|
| `config_set` | `beadhive://config`, `beadhive://config/{key}` |
| `rig_add` / `rig_onboard` | `beadhive://rigs/status`, `beadhive://rigs/available`, `beadhive://rigs/survey` |
| `plan_file` | `beadhive://work/ready`, `beadhive://plans` |
| `bd_create` | `beadhive://work/ready`, `beadhive://work/intake` |

### CLI-change limitation and upgrade path

**Limitation:** notifications fire **only on MCP-driven mutations**. An out-of-process change —
editing `~/.ws/config.yaml` by hand, or running `bh rig add` / `bd create` from the CLI — mutates
the same state but does **not** emit a notification, so a subscribed client can go stale until it
re-reads.

**Upgrade path:** a future **mtime-watch** upgrade will watch the backing files (config,
beads DB) and emit `resources/updated` on any change regardless of who made it, eliminating the
CLI-change gap.

### Dual-exposed resources

The tools `rigs_status` and `rigs_available` are **kept as both tools and resources**:

- **As tools:** structured inputs and complex result shapes (needed for custodians).
- **As resources:** polling clients and subscription patterns get the same payloads without tool
overhead.

Tool-only clients are unaffected; subscription clients prefer the resource interface.

## Availability

```sh
bh doctor   # reports MCP availability under "# MCP"
```
