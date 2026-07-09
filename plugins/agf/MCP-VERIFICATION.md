# MCP Plugin Verification Checklist

End-to-end verification that the bundled `bh` MCP server registers correctly after a
fresh plugin install. Run from a clean state (no prior plugin install for this user).

## Prerequisites

- `beadhive[otel]` installed: `uv tool install 'beadhive[otel]'` (fastmcp ships as a core dependency)
- Claude Code CLI available: `claude --version`
- A clone of the workspace repo on disk

## Step 1 — Register the marketplace and install the plugin

```sh
# From the workspace repo root:
claude plugin marketplace add .
claude plugin install agf@workspace --scope user
```

Expected: both commands exit 0 with no errors.

## Step 2 — Confirm `bh` appears in /mcp

In a Claude Code session, run:

```
/mcp
```

Expected checklist:

- [ ] `bh` appears in the MCP server list
- [ ] Status shows **connected** (not "failed" or "not connected")

If `bh` is absent or shows an error, run `bh doctor` to diagnose (see Step 4).

## Step 3 — Read a resource

In a Claude Code session or via the MCP inspector:

```
beadhive://work/ready
```

Expected:

- [ ] Resource returns JSON (may be an empty list if no ready beads exist)
- [ ] No connection error or tool-call failure

A structured JSON response confirms the server is reachable and fastmcp (a core
dependency) is present.

## Step 4 — Verify with bh doctor

```sh
bh doctor
```

Expected output under `# MCP`:

- [ ] `fastmcp: available`
- [ ] `plugin declares server: yes`

If `fastmcp: unavailable` appears, the install is broken — reinstall `bh` (fastmcp is a
core dependency):

```sh
uv tool install --force 'beadhive[otel]'
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

Then in Claude Code `/mcp` — `bh` should no longer appear as connected.

Re-enable:

```sh
claude plugin enable agf@workspace
```

Alternatively, toggle the `bh` entry directly in the Claude Code `/mcp` panel (uses
`disabledMcpjsonServers` / `enabledMcpjsonServers` settings, scoped to this server only
rather than the entire plugin).

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `bh` absent from `/mcp` | Plugin not installed, or a broken `bh` install (fastmcp missing) | Run `claude plugin install agf@workspace --scope user`; reinstall `bh` |
| `bh` shows "failed" | `bh-mcp` exits 1 (broken install — fastmcp missing) | Reinstall `beadhive[otel]`; check `bh doctor` |
| `plugin declares server: no` | Older plugin version without `.mcp.json` | `claude plugin update agf@workspace` |
| Resource read fails | Server started but import failed | `bh-mcp` manually to see error output |
