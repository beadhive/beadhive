---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/install.schema.json
install:
  id: beadhive
  summary: Beadhive — the `bh` CLI, the integration-plane driver for Agentic Git Flow (AGF) and cross-repo beads issue tracking.
  methods:
    # Alternatives — pick ONE that fits the user's OS / package manager.
    # Recommended primary: install the `beadhive` package from PyPI so `bh` lands on PATH.
    - kind: package
      manager: uv
      os: [macos, linux]
      command: uv tool install 'beadhive[otel]'
    - kind: package
      manager: homebrew
      os: [macos, linux]
      command: brew install beadhive/tap/beadhive
    - kind: package
      manager: pipx
      command: pipx install 'beadhive[otel]'
    - kind: package
      manager: pip
      command: pip install 'beadhive[otel]'
  verify: bh --version
  # Already installed? Report installed-vs-available and offer the upgrade (with consent).
  upgrade: ask
  configure:
    # Sequential — run in order, asking permission at each step.
    # 1. Scaffold ~/.beadhive (config.yaml + templates). Required, one-time per machine.
    - kind: script
      command: bh config init
    # 2. Claude Code only — register the bh MCP server at user scope
    #    (shells out to: claude mcp add bh --scope user -- bh mcp serve).
    - kind: script
      command: bh mcp install
    # 3. Optional, Claude Code only — the bh claude-plugin (AGF seat defs, role skills).
    - kind: plugin
      harness: claude-code
      ref: bh@beadhive
---

# Install Beadhive

> This is the repo's real `INSTALL.md`, following the
> [INSTALL.md convention](https://github.com/agentguides/agentguides). Paste its
> link into any agent to install Beadhive. A convention-aware agent reads the
> `install:` frontmatter above; every other agent — and you — can follow the
> prose below and reach the same result with the same permission prompts.

Beadhive is the `bh` CLI (Python package `beadhive`). Installing it is two
things — a **package install** (puts `bh` on your PATH) and a one-time
**configure** step (`bh config init`), plus optional Claude Code wiring.
Whoever is installing (agent or human) should **ask before running each
command**.

## 1. Install `bh` (pick ONE)

These are alternatives. Choose the one that matches your setup; you only need one.

- **`uv` (recommended, macOS/Linux):**

  ```sh
  uv tool install 'beadhive[otel]'   # puts `bh` on PATH (~/.local/bin)
  ```

- **Homebrew:**

  ```sh
  brew install beadhive/tap/beadhive
  ```

- **`pipx` / `pip`:**

  ```sh
  pipx install 'beadhive[otel]'      # or: pip install 'beadhive[otel]'
  ```

The `[otel]` extra enables OpenTelemetry signals out of the box; drop it if you
don't want them. The MCP server ships in the core install.

## 2. Verify

```sh
bh --version
```

This should print a version. If it does not, `bh` is not on your PATH —
`uv tool` and `pipx` install to `~/.local/bin`; add it to your shell profile:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

## 3. Configure

Run these in order.

1. **Scaffold the config home** (required, one-time per machine). Writes
   `config.yaml` and templates into `~/.beadhive/`:

   ```sh
   bh config init
   ```

2. **Claude Code only — wire the MCP server** at user scope, so planning, work,
   hive, and config tools are available in every session:

   ```sh
   bh mcp install     # runs: claude mcp add bh --scope user -- bh mcp serve
   ```

3. **Optional, Claude Code only — the `bh` claude-plugin.** Vends the AGF seat
   agent defs and role skills (dispatcher / developer / merger / …):

   ```sh
   claude plugin marketplace add beadhive/claude-plugin
   claude plugin install bh@beadhive
   ```

That's it. `bh` is installed, verified, and configured. Next steps:
[`docs/ONBOARDING.md`](docs/ONBOARDING.md) walks from here to a fully
configured AGF workspace with registered hives; [`README.md`](README.md) has the
overview and docs map.
