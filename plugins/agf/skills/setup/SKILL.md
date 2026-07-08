---
name: setup
description: >-
  Phase 0-4 onboarding driver: walks a fresh Mac + Claude Code user from zero to a configured
  AGF workspace. Owns the PRE-ws bootstrap (Homebrew, uv, then the ws binary), wires the MCP
  server at user scope, runs 'ws setup check' to gate post-ws deps, and hands off to
  'ws config init' and the setup-git-workspace sub-skill. Every step probes before acting —
  safe to re-run. Use when setting up AGF on a new machine or resuming a partial setup.
  Named 'setup' (not onboard-machine) to avoid confusion with rig onboarding.
---

# setup — fresh Mac → AGF workspace

You are guiding a user from a freshly imaged Mac (with Claude Code already running) to a fully
configured AGF workspace. Drive each phase interactively, pausing at each skip-point so the
user can confirm their starting situation. Re-runs are safe — probe before you act.

## Skip-point map — join at the right rung

Ask the user which phase applies to their current state before you begin:

| Skip when... | Jump to |
|---|---|
| `brew` already installed | Phase 1b (install uv) |
| `brew` and `uv` both installed | Phase 2 (install ws) |
| `ws` installed but MCP not wired | Phase 2b (wire MCP) |
| `ws` installed and MCP wired | Phase 3 (run ws setup check) |
| `ws setup check` already green | Phase 4 (ws config init) |
| config already initialised | Phase 5 (git-workspace walkthrough) |

If the user is unsure, start at Phase 0 and let the probes decide.

---

## Phase 0 — confirm the agf plugin is installed

The `setup` skill you are reading is bundled inside the `agf` Claude Code plugin. If you are
already reading this, the plugin is installed. Nothing to do — move to Phase 1.

If a user asks how to get here from absolute zero, the one-time bootstrap is:

```sh
claude plugin marketplace add briancripe/workspace
claude plugin install agf@workspace
```

Restart Claude Code, then invoke `/setup` or load the `setup` skill.

---

## Phase 1a — install Homebrew (pre-ws)

**Probe first:**

```sh
command -v brew
```

If `brew` is found, skip to Phase 1b.

If missing, install Homebrew (macOS only — this skill targets macOS):

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

After the installer exits, follow any shell-profile instructions it prints (typically
`eval "$(/opt/homebrew/bin/brew shellenv)"` on Apple Silicon or
`eval "$(/usr/local/bin/brew shellenv)"` on Intel). Verify:

```sh
brew --version
```

---

## Phase 1b — install uv (pre-ws)

**Probe first:**

```sh
command -v uv
```

If `uv` is found, skip to Phase 2.

If missing, install uv (the Python toolchain manager ws uses):

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Follow any shell-profile instructions the installer prints. Open a new shell or source your
profile, then verify:

```sh
uv --version
```

---

## Phase 2 — install ws (pre-ws)

**Probe first:**

```sh
command -v ws
```

If `ws` is found, skip to Phase 2b.

If missing, install the ws binary with the `otel` extra so OpenTelemetry signals work out of
the box (the MCP server ships in the core install — fastmcp is a core dependency):

```sh
uv tool install 'ws[otel]'
```

Verify the install:

```sh
ws --version
```

If `ws` is not on `PATH` after install, `uv tool` places binaries in `~/.local/bin`. Add
it to your shell profile:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

---

## Phase 2b — wire the MCP server at user scope

The ws MCP server exposes planning, work, rig, and config tools to every Claude Code session
across all rigs once it is registered at user scope.

**Probe first:**

```sh
claude mcp list | grep -q '^ws '
```

If the `ws` entry is found (exit 0), skip to Phase 3.

If missing, register it:

```sh
claude mcp add ws --scope user -- ws mcp serve
```

Verify the entry appears:

```sh
claude mcp list
```

You should see `ws` in the output. The MCP server is now available to all future Claude Code
sessions without any per-rig configuration.

---

## Phase 3 — run ws setup check

**This is the handoff point.** From here, `ws setup check` owns all further dependency
validation — do not re-probe individual tools in this skill.

```sh
ws setup check
```

This command probes the post-ws dependencies (git-workspace, gh, bd, dolt, colima) and
caches the result in `~/.ws/setup-state.json`. It prints a status line for each tool and
exits 0 only when all are found.

**If any tool is missing:** `ws setup check` names the missing tools. Install them as
directed, then re-run `ws setup check` until it exits green. The exact install path for each
tool (brew formula, gh auth, dolt binary, colima) is covered in the ws docs — surface the
relevant section to the user if they are stuck. Do not replicate the probe logic here.

**Re-runs are safe:** re-running `ws setup check` refreshes the cache regardless of prior
state.

When `ws setup check` exits 0 (all green), move to Phase 4.

---

## Phase 4 — initialise ws config

**Probe first:**

```sh
ws setup show
```

If setup is reported complete and the user already has `~/.ws/config.yaml`, this phase may
already be done. Ask the user; if the config exists and looks correct, skip to Phase 5.

Otherwise, move into `$GIT_WORKSPACE` (the workspace root where all repos live, defaulting
to `~/workspace`):

```sh
cd "${GIT_WORKSPACE:-$HOME/workspace}"
```

If the directory does not exist, create it:

```sh
mkdir -p "${GIT_WORKSPACE:-$HOME/workspace}"
cd "${GIT_WORKSPACE:-$HOME/workspace}"
```

Run config init to write the starter config files:

```sh
ws config init
```

This writes `~/.ws/config.yaml`, `~/.ws/docker-compose.yml`, and the OTel compose variant.
Files that already exist are skipped (`ws config init` is idempotent — existing files are
never overwritten unless `--force` is passed).

When the command completes, open `~/.ws/config.yaml` with the user and walk through the
key fields:

- `orgs:` block — add any GitHub orgs or providers the user works with
- `work.identity.name` — their crew identity for AGF sessions
- `claude.source` — `plugin` (default) keeps seat agents in the installed plugin; `copy`
  writes them into each rig for offline use

Tell the user to copy `~/.ws/.env.example` to `~/.ws/.env` and fill in any API tokens or
secrets it references.

---

## Phase 5 — git-workspace walkthrough

**Probe first:**

```sh
command -v git-workspace
```

If `git-workspace` is already configured and the user has repos cloned under `$GIT_WORKSPACE`,
confirm with the user whether they need the walkthrough, then skip ahead to rig onboarding
if they are already set up.

Otherwise, load the **`setup-git-workspace`** sub-skill to guide the user through:

- What `$GIT_WORKSPACE` and the `<provider>/<org>/<repo>` layout mean
- How `workspace.toml` and provider tokens work
- Installing git-workspace if absent
- Three sub-branches:
  - Already configured with repos — safe `git workspace import` with backups before any
    `git workspace update`
  - Has cloned repos but no git-workspace config — guided import
  - Nothing yet — guided install → providers → first clone

Invoke the sub-skill by name:

```
/setup-git-workspace
```

or ask the user to load it:

> Load the `agf:setup-git-workspace` skill to continue.

---

## Idempotency guarantee

Every step in this skill probes before acting. Running the skill a second time on a machine
where setup is already complete produces only "already present / skipping" confirmations —
nothing is modified. The one exception is `ws setup check`, which always re-probes and
refreshes the cache; that is intentional (the cache reflects the current state of the tools).

## What this skill does NOT own

- **Post-ws dependency installation** — `ws setup check` surfaces what is missing; follow
  its output rather than re-implementing the probe table here.
- **Rig onboarding** — once the user has a configured workspace and git-workspace is set up,
  rig onboarding is driven by `ws rig onboard` and the rig-specific onboarding flow, not by
  this skill.
- **Claude Code project settings** — per-rig `.claude/settings.json` is written by
  `ws rig init --claude`, not here.
