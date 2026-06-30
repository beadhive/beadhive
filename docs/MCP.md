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

## Availability

```sh
ws doctor   # reports MCP availability under "# MCP"
```
