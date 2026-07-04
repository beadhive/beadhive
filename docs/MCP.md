# MCP — `ws mcp serve` / `ws-mcp`

`ws` exposes a FastMCP server over stdio for MCP clients. It is an **optional extra** — the
core CLI works without it.

## CLI vs MCP

Use MCP tools only when structured I/O is the advantage — passing a typed spec in and getting
structured results back, with no temp-file marshalling or CLI-string scraping. Simple and bulk
commands offer no such advantage; they stay CLI-only.

**Token-efficiency policy:** the MCP tool surface is intentionally minimal. Agents running
simple commands (`ws sync`, `ws doctor`, `bd …`) should call the CLI directly. MCP tool
docstrings point to the CLI for everything not on the exposed list.

## Install

Install both `[mcp]` and `[otel]` together so the MCP server can also export OpenTelemetry
signals (see [OBSERVABILITY.md](OBSERVABILITY.md)). Without the `[mcp]` extra,
`ws mcp serve` and `ws-mcp` print a friendly error and exit 1; the observaloop integration
reports unavailable.

```sh
uv tool install 'ws[otel,mcp]'   # or: pip install 'ws[otel,mcp]'
```

`just install` (the development recipe) installs both extras automatically.

## Run

```sh
ws mcp serve   # ws subcommand (stdio, blocking)
ws-mcp         # standalone console-script (same)
```

Both print a friendly error and exit 1 if the `[mcp]` extra is not installed.

## Distributed via the agf plugin

The `ws` MCP server is bundled with the AGF Claude Code plugin and registered automatically
at user scope when the plugin is installed. No manual `claude mcp add` step is needed.

```sh
# install the plugin — registers the ws MCP server at user scope
claude plugin marketplace add <path-to-workspace>
claude plugin install agf@workspace --scope user

# confirm registration
/mcp   # ws should appear as "connected"
```

The server uses the `.mcp.json`-at-root convention (Claude Code auto-discovers it):

```json
{
  "mcpServers": {
    "ws": { "command": "ws", "args": ["mcp", "serve"] }
  }
}
```

The `ws[otel,mcp]` extra must be installed for the server to start; without it the
plugin declares the server but `ws mcp serve` will exit 1 with a friendly error.
`ws doctor` reports whether both the extra and the plugin declaration are in place.

### Enable / disable

| Method | Command / setting |
|---|---|
| Disable in `/mcp` | Toggle the `ws` entry off in the Claude Code MCP panel |
| Disable plugin | `claude plugin disable agf@workspace` (removes all plugin-provided servers) |
| Re-enable | `claude plugin enable agf@workspace` or toggle in `/mcp` |
| Fine-grained | `enabledMcpjsonServers` / `disabledMcpjsonServers` in Claude Code settings |

### CLI vs MCP — when to use each

Prefer the **CLI** for simple, bulk, or fire-and-forget operations — `ws sync`, `ws doctor`,
`bd …`, one-off rig commands. The CLI is always available, needs no server, and produces
human-readable output with no overhead.

Prefer **MCP** when structured I/O is the advantage: passing a typed spec in and getting a
typed result back (no temp-file marshalling, no CLI-string scraping), or reading live state
via MCP resources (`ws://work/ready`, `ws://doctor`, etc.) in a subscription loop.

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

### Control plane (superintendent)

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
single scalar read), `rig rm` (destructive unregister), `ws sync`, `ws doctor`. `rigs_status` is
the richer superset of `rigs_available` — use `rigs_available` when you only need the
`ws rig add` candidates.

Core exceptions (`MoleculeError`, `PlanError`, `WorkError`, and the config/rig failure modes)
map to `ToolError` so the client receives a clean, actionable message instead of a stack trace.

## Exposed resources

Resources expose read-only state over MCP's resource subscription model. All resources return
`application/json` and are **always fresh on read** — there is no caching layer.

| Resource | Description |
|---|---|
| `ws://probe/health` | Service health probe; confirms MCP registration. |
| `ws://config` | Resolved config dict (full workspace config state). |
| `ws://config/{key}` | Single config value by dotted key path. |
| `ws://doctor` | Structured workspace diagnostics (config/providers/orgs/rigs overview + inventory, disk_usage, fleet_health, worktrees, molecules, mcp, observability, warnings). |
| `ws://rigs/status` | Richer workspace status view: candidates (unregistered repos), collisions, violations, and all registered rigs. |
| `ws://rigs/available` | Discoverable-but-unregistered repos; diffs git-workspace's tracked repos against registered rigs. |
| `ws://rigs/survey` | Fleet onboarding table, one row per on-disk repo. |
| `ws://labels/validation` | Label validation findings: required_violations, per-issue problems, db_ok flag. |
| `ws://worktrees` | Worktree classification status for all managed rigs (SAFE/ACTIVE/DIRTY/REVIEW/UNMERGED/LANDED_REBASED/DETACHED/MERGED_ORPHAN/ABANDONED). |
| `ws://work/ready` | Ready (unblocked, dependency-ordered) beads for the current rig. |
| `ws://work/intake` | Untriaged intake inbox: rows (open intake beads) and dupes (mechanical duplicate pairs). |
| `ws://work/intake/dupes` | Duplicate-pair candidates for intake queue only; subset of mechanical-dedup pairs. |
| `ws://work/issue/{id}` | Single bead by id (template resource). |
| `ws://work/show/{id}` | Bead branch local history: base commit, max_commits limit, flagged commits for `base..branch`. |
| `ws://plans` | Swarm list for the current rig (molecule dashboard). |
| `ws://plan/{ref}` | Single molecule status by swarm ref. |
| `ws://hq/intake` | Fleet-wide untriaged intake inbox, aggregated across the hub. |
| `ws://work/schedule/{epic}` | Epic schedule plan: epic kickoff status and bead timing windows. |

### Freshness and subscription

Resources are **dynamic** and always fresh on read. A subscribed client does not cache; instead,
it re-reads when invalidated via `resources/updated` notification.

Mutating MCP tools emit `resources/updated` for the URIs they invalidate:

| Tool | Invalidates |
|---|---|
| `config_set` | `ws://config`, `ws://config/{key}` |
| `rig_add` / `rig_onboard` | `ws://rigs/status`, `ws://rigs/available`, `ws://rigs/survey` |
| `plan_file` | `ws://work/ready`, `ws://plans` |
| `bd_create` | `ws://work/ready`, `ws://work/intake` |

### CLI-change limitation and upgrade path

**Limitation:** notifications fire **only on MCP-driven mutations**. An out-of-process change —
editing `~/.ws/config.yaml` by hand, or running `ws rig add` / `bd create` from the CLI — mutates
the same state but does **not** emit a notification, so a subscribed client can go stale until it
re-reads.

**Upgrade path:** a future **mtime-watch** upgrade will watch the backing files (config,
beads DB) and emit `resources/updated` on any change regardless of who made it, eliminating the
CLI-change gap.

### Dual-exposed resources

The tools `rigs_status` and `rigs_available` are **kept as both tools and resources**:

- **As tools:** structured inputs and complex result shapes (needed for superintendents).
- **As resources:** polling clients and subscription patterns get the same payloads without tool
overhead.

Tool-only clients are unaffected; subscription clients prefer the resource interface.

## Availability

```sh
ws doctor   # reports MCP availability under "# MCP"
```
