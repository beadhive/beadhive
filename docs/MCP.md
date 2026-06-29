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

```sh
uv tool install 'ws[mcp]'   # or: pip install 'ws[mcp]'
```

## Run

```sh
ws mcp serve   # ws subcommand (stdio, blocking)
ws-mcp         # standalone console-script (same)
```

Both print a friendly error and exit 1 if the `[mcp]` extra is not installed.

## Exposed tools

These four tools are registered. Everything else stays CLI-only.

| Tool | Inputs | Output |
|---|---|---|
| `plan_check` | `spec: dict` | `{valid, problems}` |
| `plan_file` | `spec: dict`, `rig?: str`, `dry_run?: bool` | `{epic_id, issue_count, root_count}` or dry-run preview |
| `work_refine` | `bead: str`, mode (`squash_plan`/`autosquash`/`since`), `rig?`, `dry_run?` | `{subjects, branch, backup, log, …}` |
| `bd_create` | `issues: list[dict]`, `rig?: str` | `{created, count}` |

Core exceptions (`MoleculeError`, `PlanError`, `WorkError`) map to `ToolError` so the client
receives a clean, actionable message instead of a stack trace.

## Availability

```sh
ws doctor   # reports MCP availability under "# MCP"
```
