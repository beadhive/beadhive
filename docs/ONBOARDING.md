# Onboarding — fresh Mac to configured Beadflow workspace

This guide walks you from a freshly imaged Mac (with Claude Code already running) to a fully
configured Beadflow workspace: `bh` installed, MCP server wired, config initialised, repos registered
as rigs, and a dispatcher ready to drive beads.

The [`setup` skill][setup-skill] is the agent-native driver for this
journey — it runs each step interactively, probes before acting, and is safe to re-run. This
document is the reference narrative; the skill is the guided experience.

---

## Find your entry point

Four labeled starting situations. Each has an entry point (where to begin) and a skip-point
(where the path rejoins the main story). If you are not sure, start at Phase 0.

| Situation | Starting state | Entry point | Skip-point |
|---|---|---|---|
| **(a) Fresh Mac** | Nothing installed beyond Claude Code | [Phase 0](#phase-0--get-the-setup-skill) | No skip — run all phases |
| **(b) Repos not under git-workspace** | Repos cloned locally; no git-workspace config | [Phase 0](#phase-0--get-the-setup-skill) to check bh state, then [Phase 5 Sub-branch B](#sub-branch-b-repos-cloned-but-not-using-git-workspace) | Once bh + config are set up, land at [Phase 5B](#sub-branch-b-repos-cloned-but-not-using-git-workspace) |
| **(c) git-workspace already good** | git-workspace configured, repos cloned under `$GIT_WORKSPACE` | [Phase 2](#phase-2--install-bh) if `bh` not installed; [Phase 3](#phase-3--validate-post-bh-dependencies) if already installed | [Phase 6a](#phase-6a--survey-candidate-rigs) |
| **(d) GitLab-only / no gh** | GitLab, Gitea, or local repos only; no GitHub account | Enter at your brew/uv/bh state (Phase 0–2); skip `gh` in Phase 3 | [GitLab-only path](#gitlab-only--no-github-path) |

Finer-grained skip-points within each situation:

| Skip when... | Jump to |
|---|---|
| `brew` already installed | [Phase 1b](#phase-1b--install-uv) |
| `brew` and `uv` both installed | [Phase 2](#phase-2--install-bh) |
| `bh` installed, MCP not yet wired | [Phase 2b](#phase-2b--wire-the-mcp-server-at-user-scope) |
| `bh` installed and MCP wired | [Phase 3](#phase-3--validate-post-bh-dependencies) |
| All deps validated (`bh setup check` green) | [Phase 4](#phase-4--initialise-bh-config) |
| `~/.ws/config.yaml` already exists | [Phase 5](#phase-5--git-workspace-walkthrough) |
| git-workspace configured, repos cloned | [Phase 6a](#phase-6a--survey-candidate-rigs) |
| Rigs already registered | [Phase 6c](#phase-6c--verify-and-hand-off) |

---

## Phase 0 — Get the setup skill

**Before `bh` exists**, the only agent capability available is what the `agf` Claude Code
plugin provides. If you are reading this inside a Claude session that already knows the
`setup` skill, the plugin is installed — move to Phase 1a.

If you need to install the plugin from absolute zero, run these two commands once in any
Claude Code terminal, then restart Claude Code:

```sh
claude plugin marketplace add beadhive/claude-plugin
claude plugin install bh@beadhive
```

After restarting, invoke the setup skill:

```text
/setup
```

or ask Claude to load `agf:setup`. The skill walks you through Phases 1–6 interactively.
This document is the reference behind each step.

---

## Phase 1a — Install Homebrew

**Probe first:**

```sh
command -v brew
```

If `brew` is found, skip to [Phase 1b](#phase-1b--install-uv).

If missing, install Homebrew (macOS only — this guide targets macOS):

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

Follow any shell-profile instructions the installer prints. On Apple Silicon this is typically:

```sh
eval "$(/opt/homebrew/bin/brew shellenv)"
```

On Intel Macs:

```sh
eval "$(/usr/local/bin/brew shellenv)"
```

Add the `eval` line to your shell profile (`~/.zshrc` or `~/.bash_profile`) so `brew` is on
`PATH` in every future session. Verify:

```sh
brew --version
```

### Phase 1 prerequisite table

The full tool set required for the Beadflow workspace — reconciled against the repo's `Brewfile`
and `.mise.toml`. The two paths are:

- **User path** — install `bh` from a package registry (no repo clone needed); install
  post-`bh` deps via `bh setup check` output.
- **Developer path** — clone this repo and run `just bootstrap` (installs everything below).

| Tool | Version | Source | User path | Developer path | Purpose |
|---|---|---|---|---|---|
| `brew` | system | installer | Phase 1a | Phase 1a | system package manager |
| `uv` | latest | `.mise.toml` | Phase 1b | `just bootstrap` | Python toolchain manager; installs `bh` |
| `git-workspace` | system | `brew install git-workspace` | Phase 5 | Phase 5 | repo layout + fleet management |
| `gh` | 2.95.0 | `.mise.toml` / `brew install gh` | Phase 3 (conditional) | `just bootstrap` | GitHub CLI; required for GitHub provider only |
| `bd` (beads) | system | `Brewfile`: `brew "beads"` | Phase 3 | `just bootstrap` | beads issue tracker engine |
| `dolt` | system | `Brewfile`: `brew "dolt"` | Phase 3 | `just bootstrap` | Dolt backend for beads |
| `colima` | system | `Brewfile`: `brew "colima"` | Phase 3 | `just bootstrap` | Docker daemon/VM (beads+Dolt runtime) |
| `mise` | system | `Brewfile`: `brew "mise"` | not needed | `just bootstrap` | tool-version manager (provides developer tools) |
| `python` | 3.12 | `.mise.toml` | not needed | `just bootstrap` | bh runtime |
| `just` | 1.54.0 | `.mise.toml` | not needed | `just bootstrap` | task runner (`just check`, `just lint`, …) |
| `docker-cli` | 29.6.1 | `.mise.toml` | not needed | `just bootstrap` | Docker CLI (dev tooling) |
| `docker-compose` | 5.2.0 | `.mise.toml` | not needed | `just bootstrap` | Compose (dev tooling) |
| `node` | lts | `.mise.toml` | not needed | `just bootstrap` | markdown linter runtime |
| `markdownlint-cli2` | latest | `.mise.toml` (npm) | not needed | `just bootstrap` | docs linting (`just lint-md`) |

**Developer bootstrap shortcut** — if you are contributing to `bh` (not just using it), clone
this repo and run:

```sh
just bootstrap   # brew bundle + mise install + uv sync
just install     # uv tool install . → ~/.local/bin/bh
```

`just bootstrap` installs every `Brewfile` brew formula and every `.mise.toml` tool in one
shot. The user path installs only what `bh` needs at runtime.

---

## Phase 1b — Install uv

**Probe first:**

```sh
command -v uv
```

If `uv` is found, skip to [Phase 2](#phase-2--install-bh).

If missing, install uv (the Python toolchain manager `bh` uses):

```sh
curl -LsSf https://astral.sh/uv/install.sh | sh
```

Follow any shell-profile instructions the installer prints. Open a new shell or source your
profile, then verify:

```sh
uv --version
```

### Note: `gh` is optional

`gh` (the GitHub CLI) is required only when you use a **GitHub provider**. If you work
exclusively with GitLab, Gitea, or local repos, skip `gh` installation — you can add it
later if you register a GitHub provider. The dependency validation in Phase 3 notes which
deps are conditional.

---

## Phase 2 — Install bh

**Probe first:**

```sh
command -v bh
```

If `bh` is found, skip to [Phase 2b](#phase-2b--wire-the-mcp-server-at-user-scope).

If missing, install the `bh` binary (from the `beadhive` package) with the `otel` extra so
OpenTelemetry signals work out of the box (the MCP server ships in the core install — fastmcp
is a core dependency):

```sh
uv tool install 'beadhive[otel]'
```

Verify:

```sh
bh --version
```

If `bh` is not found after install, `uv tool` places binaries in `~/.local/bin`. Add it to
your shell profile:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

---

## Phase 2b — Wire the MCP server at user scope

The `bh` MCP server exposes planning, work, rig, and config tools to every Claude Code session
across all rigs once it is registered at **user scope** (one-time setup; no per-rig wiring).

**Probe first:**

```sh
claude mcp list | grep -q '^bh '
```

If the `bh` entry is found (exit 0), skip to [Phase 3](#phase-3--validate-post-bh-dependencies).

If missing, use the convenience verb:

```sh
bh mcp install
```

This shells out to `claude mcp add bh --scope user -- bh mcp serve`. You can also run the
underlying command directly if you prefer:

```sh
claude mcp add bh --scope user -- bh mcp serve
```

Verify:

```sh
claude mcp list
```

You should see `bh` in the output. The MCP server is now available to all future Claude Code
sessions. In a fresh Claude session, `bh doctor` shows the MCP section as connected.

---

## Phase 3 — Validate post-`bh` dependencies

Run from any directory:

```sh
bh setup check        # probe all post-bh deps; cache result in ~/.ws/setup-state.json
bh setup show         # report cached status (read-only; does not re-probe)
```

`bh setup check` probes each tool in the table below, exits 0 only when all required deps
pass, and writes a cache to `~/.ws/setup-state.json`. Every `bh` verb except `setup`,
`config init`, `doctor`, `--version`, and `--help` is gated on a passing cache — running
`bh <verb>` on a fresh install tells you to run `bh setup check` first. Re-running at any
time refreshes the cache.

If any tool is missing, `bh setup check` names it. Install the missing tools per the table
below and re-run until it exits green.

The env var `WS_SKIP_SETUP_CHECK=1` bypasses the gate for debugging.

### Post-`bh` prerequisite table

| Tool | Probe | Install | Purpose | Gate required? |
|---|---|---|---|---|
| git-workspace | `command -v git-workspace` | `brew install git-workspace` | clone/layout management | Yes |
| `gh` | `command -v gh` | `brew install gh` + `gh auth login` | GitHub CLI (fork classification, API) | Yes (all setups; see note) |
| `bd` (beads) | `command -v bd` | `brew install beads` (Brewfile) | issue tracker engine | Yes |
| dolt | `command -v dolt` | `brew install dolt` (Brewfile) | Dolt beads backend | Yes |
| colima | `command -v colima` | `brew install colima` (Brewfile) | container runtime | Yes |

**Notes:**

- `beads`, `dolt`, and `colima` are in the repo's `Brewfile` (`brew "beads"`, `brew "dolt"`,
  `brew "colima"`). `gh` is pinned in `.mise.toml` at `gh = "2.95.0"`. `git-workspace` is an
  external tool not in the Brewfile.
- `gh` is probed unconditionally — ALL five tools must be found for `setup==true`. Making
  `gh` conditional on the configured provider is a planned improvement. If you are on
  GitLab or Gitea only, install `gh` to pass the gate but skip GitHub-specific config.
- `dolt` and `colima` are required by the `bd` + Dolt backend (the only backend today). When
  alternative backends land, they will become conditional — see [Future sections](#future-sections).

---

## Phase 4 — Initialise bh config

**Probe first:**

```sh
bh doctor
```

If `~/.ws/config.yaml` already exists and the doctor output looks correct, skip to
[Phase 5](#phase-5--git-workspace-walkthrough).

Move into `$GIT_WORKSPACE` (the workspace root where all repos live; defaults to
`~/workspace`). Create it if it does not exist:

```sh
mkdir -p "${GIT_WORKSPACE:-$HOME/workspace}"
cd "${GIT_WORKSPACE:-$HOME/workspace}"
```

`$GIT_WORKSPACE` is the canonical HQ launch directory. The `setup` skill sets it to
`~/workspace` if unset. When you open a Claude session from this directory, the dispatcher
and related roles discover your rigs automatically.

Then scaffold the starter config files:

```sh
bh config init
```

This writes `~/.ws/config.yaml`, `~/.ws/docker-compose.yml`, and `.env.example` from bundled
templates. **Existing files are never overwritten** (`bh config init` is idempotent; pass
`--force` to overwrite intentionally).

### Key fields to tune

Open `~/.ws/config.yaml` and review:

| Field | What to set |
|---|---|
| `providers:` | List of git hosts you use (`github`, `gitlab`, `gitea`). Can be omitted if git-workspace integration is enabled — it reads providers from `workspace.toml`. |
| `orgs:` | Add your GitHub/GitLab orgs with a short `code:` and `policy:`. Orgs not listed fall back to `sanitize(name)[:2]` + `personal`. |
| `work.identity.name` | Your seat identity for Beadflow sessions (e.g. `dev/dev1`). |
| `claude.source` | `plugin` (default) installs seat agents via the `agf` plugin; `copy` writes them directly into each rig (legacy / airgap). |

Use `bh config set` to edit values without opening the file:

```sh
bh config set git_workspace.enabled true
bh config set work.identity.name "dev/yourname"
```

Copy `.env.example` to `.env` and fill in any tokens or secrets it references:

```sh
cp ~/.ws/.env.example ~/.ws/.env
```

See [CONFIGURATION](CONFIGURATION.md) for the full schema and all config commands.

---

## Phase 5 — git-workspace walkthrough

[git-workspace](https://github.com/orf/git-workspace) clones a fleet of repos into a
`<provider>/<org>/<repo>` layout under `$GIT_WORKSPACE` and tracks them in
`workspace.toml`. `bh` reads that layout to derive rig identity and, when the integration is
enabled, reads providers and org lists from it automatically.

**Probe first:**

```sh
command -v git-workspace
```

Pick the sub-branch that matches your situation.

### Sub-branch A: git-workspace already configured with repos

> **Situation (c) skip-point** — land here if git-workspace is already good.

You have git-workspace installed, `workspace.toml` is present, and repos are cloned under
`$GIT_WORKSPACE`. Confirm the layout is clean:

```sh
bh git workspace list
```

If the list looks correct, enable the integration in `~/.ws/config.yaml`:

```sh
bh config set git_workspace.enabled true
```

Skip to [Phase 6](#phase-6--rig-onboarding).

### Sub-branch B: repos cloned but not using git-workspace

> **Situation (b) skip-point** — land here after bh + config are set up.

You have repos cloned under `$GIT_WORKSPACE` (or elsewhere) but no `workspace.toml`. The
`agf:setup-git-workspace` sub-skill guides this path; load it from Claude:

> Load the `agf:setup-git-workspace` skill to continue.

The import process:

1. **Scan** — classifies each repo as `READY`, `PUSH_NEEDED`, `WIP_DIRTY`, or `NO_ORIGIN`.
2. **Snapshot** — dirty repos get a dated WIP branch so no work is lost.
3. **Publish** — repos with no origin are published before the import gate.
4. **Pre-flight check** — gate verifies the repo state before any `git workspace update`.
5. **Optional layout migration** — moves repos into the `<provider>/<org>/<repo>` structure
   that `bh` uses for identity derivation. You choose whether to migrate.

Backups happen before any mutation. After import, enable the integration:

```sh
bh config set git_workspace.enabled true
```

Then proceed to [Phase 6](#phase-6--rig-onboarding).

### Sub-branch C: nothing yet — first-time git-workspace setup

Install git-workspace:

```sh
brew install git-workspace
```

Set `GIT_WORKSPACE` in your shell profile if it differs from `~/workspace`:

```sh
export GIT_WORKSPACE="$HOME/workspace"
```

Then declare your providers and orgs in `workspace.toml`. The `agf:setup-git-workspace`
sub-skill walks through this step:

> Load the `agf:setup-git-workspace` skill to continue.

That sub-skill explains what `$GIT_WORKSPACE` is, how the `<provider>/<org>/<repo>` layout
maps to bh rig identity, what a provider token needs, and drives the `git workspace update`
that clones your repos.

After setup, enable the integration:

```sh
bh config set git_workspace.enabled true
```

Proceed to [Phase 6](#phase-6--rig-onboarding).

### What gets tracked vs what stays local

`bh rig init` (run in Phase 6) is **zero-footprint by default** — nothing is tracked and
nothing is committed; `.beads/` stays behind `.git/info/exclude`. Tracked furniture is a
declared, ownership-gated opt-in (`--furnish`, implied by `--claude`/`--agents`/`--skills`):

- **Tracked (furnished rigs only)** — `.beads/config.yaml`, `.beads/metadata.json`,
  `.beads/issues.jsonl`, `.beads/.gitignore`, `.claude/settings.json`, `CLAUDE.md` /
  `AGENTS.md` hints.
- **Host-local only** (`.git/info/exclude`, never the tracked `.gitignore`) — `.ws/`,
  `.claude/settings.local.json`, and on zero-footprint rigs all of `.beads/`.

`bd init` writes its own `.beads/.gitignore` that keeps the Dolt db, locks, backups, and
sockets out of commits. On a furnished rig `bh rig init` repairs any stealth exclusion and
commits the scaffold as `chore(agf): rig scaffolding (beads + agent config)` (re-runs amend
if unpushed, or commit as `chore(agf): rig scaffolding repair`). External rigs (forks /
distinct-upstream repos) can never be furnished.

---

## Phase 6 — Rig onboarding

A **rig** is a repo's beads database. Onboarding a rig materializes beads locally
(zero-footprint by default), registers the repo in `~/.ws/config.yaml`, and optionally
furnishes it with rig furniture (Claude settings, skills, agents — owner-only). This is a
**per-repo** step; run it once per repo you want to track.

### Phase 6a — Survey candidate rigs

> **Situation (c) skip-point** — land here if git-workspace is configured and repos are cloned.

Before committing to any onboarding, generate a fleet triage table to see which repos are
ready candidates and which need attention first:

```sh
bh rig survey --available --sort difficulty
```

This shows every unregistered on-disk repo with columns `REG`, `CLASS`, `COMMITS`, `DIRTY`,
`DISK`, and `DIFFICULTY` (`EASY` / `MEDIUM` / `HARD` / `NOT-A-CANDIDATE`). Start with `EASY`
rows — they have no hard signals and `bh rig ready` will pass immediately after init.

See [RIGS — bh rig survey](RIGS.md#bh-rig-survey) for the full column and difficulty
semantics.

The custodian seat can run this fleet-wide via the `bh role custodian` path:

```sh
bh role custodian
```

or launch a Claude session from `$GIT_WORKSPACE` and ask it to triage rigs.

### Phase 6b — Onboard a rig

For each candidate, onboard it end-to-end:

```sh
# Dry-run first — see the preflight plan without mutating anything:
bh rig onboard github/myorg/myrepo --dry-run

# Onboard in place, zero-footprint (repo already cloned):
bh rig onboard github/myorg/myrepo

# Onboard + furnish with agent furniture (owner-only; each flag implies --furnish):
bh rig onboard github/myorg/myrepo --claude --skills --agents

# Onboard and clone from remote (if not yet cloned):
bh rig onboard github/myorg/myrepo \
  --clone-url https://github.com/myorg/myrepo.git \
  --claude --skills --agents
```

Flag summary:

| Flag | Installs |
|---|---|
| `--furnish` | Declares tracked in-repo furniture (ownership-gated; default is zero-footprint) |
| `--claude` | `.claude/settings.json` + statusLine + plugin or copy of seat agents |
| `--skills` | Role skills (dev, dispatcher, merger, …) |
| `--agents` | `AGENTS.md` / `CLAUDE.md` Beadflow hint stanza |
| `--observaloop` | OTel telemetry profile for this rig (optional) |

The preflight DAG (`bh rig onboard --dry-run`) shows every check id before any mutation.
Overridable checks (e.g. `dirty-tree`, `on-default-branch`) can be downgraded to warnings
with `--skip-check <id>` when you have a reason:

```sh
bh rig onboard github/myorg/myrepo --claude \
  --skip-check dirty-tree
```

See [RIGS](RIGS.md) for onboarding details, kind classification, prefix derivation, and the
tracked-scaffold convention.

### Phase 6c — Verify and hand off

After onboarding each rig, confirm rig readiness:

```sh
bh rig ready          # pass/fail check for this repo
bh rig ready -v       # line-item breakdown (required + optional checks)
```

Check the whole fleet:

```sh
bh doctor             # fleet-level health: providers, orgs, rig counts, warnings
```

Build the hub so cross-rig views work:

```sh
bh sync               # aggregate every registered rig into ~/.ws/hub
bh hq bd ready        # actionable work across all rigs
```

When the fleet is green, launch a dispatcher to drive beads:

```sh
bh role dispatcher
```

This opens a Claude session with the **dispatcher** seat loaded — the normal entry point
for assigning and dispatching bead work.

The **custodian seat** (discover → onboard → configure → verify → hand off) is the
agent-native way to run Phase 6 at fleet scale. Ask Claude to act as the custodian for
a batch onboarding session.

---

## GitLab-only / no-GitHub path

> **Situation (d) skip-point** — enter at your brew/uv/bh state; follow these notes through Phases 1–3.

If you use GitLab, Gitea, or local bare repos and have no GitHub account:

- **Install `gh` anyway** — `bh setup check` probes all five tools unconditionally, including
  `gh`, and exits 1 if any are absent. Install `gh` to pass the gate even if you never
  configure a GitHub provider. Making `gh` conditional on the configured provider is a
  planned improvement — for now it is a required dep for the gate to pass.

  ```sh
  brew install gh
  gh auth login   # skip or choose "no" for GitHub integration if prompted
  ```

- In `~/.ws/config.yaml`, set `providers: [gitlab]` (or `gitea`, etc.) and omit the
  `github` entry. Provider entries are not required at all if the git-workspace integration
  is enabled (it reads providers from `workspace.toml`).
- In `workspace.toml`, declare a `[[provider]]` with `path = "gitlab"` (or the appropriate
  host path) and your org name.
- `bh rig survey` and `bh rig onboard` work identically for GitLab rigs as long as the
  repo is under `$GIT_WORKSPACE/<provider>/<org>/<repo>`.

---

## Future sections

The following are documented as design intent but not yet built.

### Other operating systems

This guide targets **macOS + Claude Code**. Linux (apt/nix prereqs) and other harnesses
(Codex, etc.) are planned future extensions. The `bh setup check` probe table will grow
OS-specific install paths when those land; the gate contract (`setup==true` in
`~/.ws/setup-state.json`) records an OS tag for this purpose.

### PyPI wheel install

The current install path (`uv tool install 'beadhive[otel]'`) pulls from the source tree or
a git reference. A public PyPI wheel (`uv tool install beadhive` / `pipx install beadhive`) is
planned as a separate release track (both install the `bh` binary — `bh` itself is not a
reservable package name; see
[limn-naming-strategy-adr.md](design/limn-naming-strategy-adr.md)). When it ships, Phase 2
simplifies to:

```sh
uv tool install beadhive
```

No other steps change. This is a distribution change only.

### Multi-backend beads selection

Today beads = `bd` + Dolt. When `beads-rust` or `beadwork` land, a `beads.backend` config
key will select the backend and the `bh setup check` dependency table will make `dolt` and
`colima` conditional on the configured backend rather than always required. The cache tag
already records a backend slot for this purpose.

---

## Reference

- [OVERVIEW](OVERVIEW.md) — command map and one-page mental model
- [RIGS](RIGS.md) — onboarding, kinds, prefix derivation, the scaffold convention
- [CONFIGURATION](CONFIGURATION.md) — `~/.ws/config.yaml` schema, all `bh config` commands
- [HUB](HUB.md) — `bh sync` and the cross-rig aggregate (`~/.ws/hub`)
- [INTEGRATIONS](INTEGRATIONS.md) — the git-workspace integration
- [WORK](WORK.md) — `bh work` and the bead lifecycle
- [DIAGNOSTICS](DIAGNOSTICS.md) — `bh doctor`
- [setup skill][setup-skill] — the interactive onboarding driver

[setup-skill]: https://github.com/beadhive/claude-plugin/blob/main/bh/skills/setup/SKILL.md
