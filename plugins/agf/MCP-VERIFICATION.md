# MCP Plugin Verification Checklist

End-to-end verification that the bundled `ws` MCP server registers correctly after a
fresh plugin install. Run from a clean state (no prior plugin install for this user).

## Prerequisites

- `ws[otel]` installed: `uv tool install 'ws[otel]'` (fastmcp ships as a core dependency)
- Claude Code CLI available: `claude --version`
- A clone of the workspace repo on disk

## Step 1 — Register the marketplace and install the plugin

```sh
# From the workspace repo root:
claude plugin marketplace add .
claude plugin install agf@workspace --scope user
```

Expected: both commands exit 0 with no errors.

## Step 2 — Confirm `ws` appears in /mcp

In a Claude Code session, run:

```
/mcp
```

Expected checklist:

- [ ] `ws` appears in the MCP server list
- [ ] Status shows **connected** (not "failed" or "not connected")

If `ws` is absent or shows an error, run `ws doctor` to diagnose (see Step 4).

## Step 3 — Read a resource

In a Claude Code session or via the MCP inspector:

```
ws://work/ready
```

Expected:

- [ ] Resource returns JSON (may be an empty list if no ready beads exist)
- [ ] No connection error or tool-call failure

A structured JSON response confirms the server is reachable and fastmcp (a core
dependency) is present.

## Step 4 — Verify with ws doctor

```sh
ws doctor
```

Expected output under `# MCP`:

- [ ] `fastmcp: available`
- [ ] `plugin declares server: yes`

If `fastmcp: unavailable` appears, the install is broken — reinstall `ws` (fastmcp is a
core dependency):

```sh
uv tool install --force 'ws[otel]'
```

If `plugin declares server: no` appears, update the plugin:

```sh
claude plugin update agf@workspace
```

## Step 5 — Verify the disable path (optional toggle test)

Confirm the server is a true option, not a hard dependency:

```sh
# Disable via the plugin
claude plugin disable agf@workspace
```

Then in Claude Code `/mcp` — `ws` should no longer appear as connected.

Re-enable:

```sh
claude plugin enable agf@workspace
```

Alternatively, toggle the `ws` entry directly in the Claude Code `/mcp` panel (uses
`disabledMcpjsonServers` / `enabledMcpjsonServers` settings, scoped to this server only
rather than the entire plugin).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `ws` absent from `/mcp` | Plugin not installed, or a broken `ws` install (fastmcp missing) | Run `claude plugin install agf@workspace --scope user`; reinstall `ws` |
| `ws` shows "failed" | `ws-mcp` exits 1 (broken install — fastmcp missing) | Reinstall `ws[otel]`; check `ws doctor` |
| `plugin declares server: no` | Older plugin version without `.mcp.json` | `claude plugin update agf@workspace` |
| Resource read fails | Server started but import failed | `ws-mcp` manually to see error output |
