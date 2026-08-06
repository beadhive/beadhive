# Onboarding — fresh Mac to configured Beadflow workspace

This guide walks you from a freshly imaged Mac (with Claude Code already running) to a fully
configured Beadflow workspace: `bh` installed, MCP server wired, config initialised, repos registered
as hives, and a dispatcher ready to drive beads.

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
| **(c) git-workspace already good** | git-workspace configured, repos cloned under `$GIT_WORKSPACE` | [Phase 2](#phase-2--install-bh) if `bh` not installed; [Phase 3](#phase-3--validate-post-bh-dependencies) if already installed | [Phase 6a](#phase-6a--survey-candidate-hives) |
| **(d) GitLab-only / no gh** | GitLab, Gitea, or local repos only; no GitHub account | Enter at your brew/uv/bh state (Phase 0–2); skip `gh` in Phase 3 | [GitLab-only path](#gitlab-only--no-github-path) |

Finer-grained skip-points within each situation:

| Skip when... | Jump to |
|---|---|
| `brew` already installed | [Phase 1b](#phase-1b--install-uv) |
| `brew` and `uv` both installed | [Phase 2](#phase-2--install-bh) |
| `bh` installed, MCP not yet wired | [Phase 2b](#phase-2b--wire-the-mcp-server-at-user-scope) |
| `bh` installed and MCP wired | [Phase 3](#phase-3--validate-post-bh-dependencies) |
| All deps validated (`bh setup check` green) | [Phase 4](#phase-4--initialise-bh-config) |
| `~/.beadhive/config.yaml` already exists | [Phase 5](#phase-5--git-workspace-walkthrough) |
| git-workspace configured, repos cloned | [Phase 6a](#phase-6a--survey-candidate-hives) |
| Hives already registered | [Phase 6c](#phase-6c--verify-and-hand-off) |

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
| container runtime | system | **PREREQUISITE — not installed by bootstrap** | Phase 3 | operator supplies | Docker Desktop / colima / OrbStack on macOS, distro daemon on Linux. `bh` drives whichever it finds; native mode needs none |
| `mise` | system | `Brewfile`: `brew "mise"` | not needed | `just bootstrap` | tool-version manager (provides developer tools) |
| `python` | 3.12 | `.mise.toml` | not needed | `just bootstrap` | bh runtime |
| `just` | 1.54.0 | `.mise.toml` | not needed | `mise exec -- just bootstrap` | task runner. NOT installable by `just bootstrap` — that needs `just` to already exist. `mise exec --` installs the pinned version on demand |
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

The `bh` MCP server exposes planning, work, hive, and config tools to every Claude Code session
across all hives once it is registered at **user scope** (one-time setup; no per-hive wiring).

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
bh setup check        # probe all post-bh deps; cache result in ~/.beadhive/setup-state.json
bh setup show         # report cached status (read-only; does not re-probe)
```

`bh setup check` probes each tool in the table below, exits 0 only when all required deps
pass, and writes a cache to `~/.beadhive/setup-state.json`. Every `bh` verb except `setup`,
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
| container runtime | `command -v docker \|\| command -v colima \|\| command -v podman` | operator-supplied — **NOT in the Brewfile** | container runtime | Yes, for container mode |

**Notes:**

- `beads` and `dolt` are in the repo's `Brewfile` (`brew "beads", args: ["HEAD"]`,
  `brew "dolt"`). `gh` is pinned in `.mise.toml` at `gh = "2.95.0"`.
  `git-workspace` is an external tool not in the Brewfile.
- **Put mise's shims on `PATH`, or `bh` cannot see half its dependencies.** `bh setup check`
  resolves tools with `shutil.which()` on the inherited `PATH` (`setup.py :: PROBE_TABLE`),
  and `.mise.toml` tools reach `PATH` only once mise is activated. Measured on a bare Debian
  host after a *successful* `just bootstrap`: `gh` reported **not found** while installed and
  working. Brewfile tools were visible, mise tools were not.

  ```sh
  export PATH="$HOME/.local/share/mise/shims:$PATH"   # once, in the profile
  ```

  Shims — not `mise activate` — because activation only affects an interactive shell, whereas
  `bh` needs `PATH` lookup to work in any process that runs it. Pins are preserved: through
  the shim, `gh --version` is 2.95.0. Note this also makes `docker` resolve to the pinned
  `docker-cli` from `.mise.toml` rather than the distro's binary.
- **The container runtime is NOT in the Brewfile** (bh-q160.1). `brew "colima"` used to be,
  and it installed a macOS VM manager on Linux — measured at 4.6G of transitive
  dependencies on a bare Debian host. Supply your own: Docker Desktop, colima or OrbStack
  on macOS, the distro daemon on Linux. `bh` probes for docker/colima/podman and drives
  whichever it finds.
- **`beads` is pinned to HEAD deliberately.** Since 0.8.0 a new hive is created on `bd`'s
  shared `dolt sql-server`, and every tagged `bd` release through v1.1.2 embeds a dolt older
  than v2.2.0 — the build whose `bd dolt pull` can hang indefinitely on a large store. Install
  it with `brew install --HEAD beads`. See [DOLT](DOLT.md) and
  [UPGRADING § 0.7.x → 0.8.0](UPGRADING.md#07x--080--the-store-engine-moves-to-bds-shared-dolt-server).
- `gh` is probed unconditionally — ALL five tools must be found for `setup==true`. Making
  `gh` conditional on the configured provider is a planned improvement. If you are on
  GitLab or Gitea only, install `gh` to pass the gate but skip GitHub-specific config.
- `dolt` and a container runtime are required by the `bd` + Dolt backend (the only backend today). When
  alternative backends land, they will become conditional — see [Future sections](#future-sections).

---

## Phase 4 — Initialise bh config

**Probe first:**

```sh
bh doctor
```

If `~/.beadhive/config.yaml` already exists and the doctor output looks correct, skip to
[Phase 5](#phase-5--git-workspace-walkthrough).

Move into `$GIT_WORKSPACE` (the workspace root where all repos live; defaults to
`~/workspace`). Create it if it does not exist:

```sh
mkdir -p "${GIT_WORKSPACE:-$HOME/workspace}"
cd "${GIT_WORKSPACE:-$HOME/workspace}"
```

`$GIT_WORKSPACE` is the canonical HQ launch directory. The `setup` skill sets it to
`~/workspace` if unset. When you open a Claude session from this directory, the dispatcher
and related roles discover your hives automatically.

Then scaffold the starter config files:

```sh
bh config init
```

This writes `~/.beadhive/config.yaml`, `~/.beadhive/docker-compose.yml`, and `.env.example` from bundled
templates. **Existing files are never overwritten** (`bh config init` is idempotent; pass
`--force` to overwrite intentionally).

### Key fields to tune

Open `~/.beadhive/config.yaml` and review:

| Field | What to set |
|---|---|
| `providers:` | List of git hosts you use (`github`, `gitlab`, `gitea`). Can be omitted — bh always reads providers from `workspace.toml` too (git-workspace is a required dep, not an optional toggle). |
| `orgs:` | Add your GitHub/GitLab orgs with a short `code:` and `policy:`. Orgs not listed fall back to `sanitize(name)[:2]` + `personal`. |
| `work.identity.name` | Your seat identity for Beadflow sessions (e.g. `dev/dev1`). |
| `claude.source` | `plugin` (default) installs seat agents via the `agf` plugin; `copy` writes them directly into each hive (legacy / airgap). |

Use `bh config set` to edit values without opening the file:

```sh
bh config set work.identity.name "dev/yourname"
```

Copy `.env.example` to `.env` and fill in any tokens or secrets it references:

```sh
cp ~/.beadhive/.env.example ~/.beadhive/.env
```

See [CONFIGURATION](CONFIGURATION.md) for the full schema and all config commands.

---

## Phase 5 — git-workspace walkthrough

[git-workspace](https://github.com/orf/git-workspace) clones a fleet of repos into a
`<provider>/<org>/<repo>` layout under `$GIT_WORKSPACE` and tracks them in
`workspace.toml`. `bh` reads that layout to derive hive identity, and always reads providers
and org lists from it automatically too — git-workspace is a required dep, not an optional
toggle, so there's nothing to "enable".

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

If the list looks correct, you're done — bh already reads it (no `enabled` flag to set).

Skip to [Phase 6](#phase-6--hive-onboarding).

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

Backups happen before any mutation. After import, bh already reads the result (no `enabled`
flag to set).

Then proceed to [Phase 6](#phase-6--hive-onboarding).

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
maps to bh hive identity, what a provider token needs, and drives the `git workspace update`
that clones your repos.

After setup, bh already reads it (no `enabled` flag to set).

Proceed to [Phase 6](#phase-6--hive-onboarding).

### What gets tracked vs what stays local

`bh hive init` (run in Phase 6) is **zero-footprint by default** — nothing is tracked and
nothing is committed; `.beads/` stays behind `.git/info/exclude`. Tracked furniture is a
declared, ownership-gated opt-in (`--furnish`, implied by `--claude`/`--agents`/`--skills`):

- **Tracked (furnished hives only)** — `.beads/config.yaml`, `.beads/metadata.json`,
  `.beads/issues.jsonl`, `.beads/.gitignore`, `.claude/settings.json`, `CLAUDE.md` /
  `AGENTS.md` hints.
- **Host-local only** (`.git/info/exclude`, never the tracked `.gitignore`) — `.bh/`,
  `.claude/settings.local.json`, and on zero-footprint hives all of `.beads/`.

`bd init` writes its own `.beads/.gitignore` that keeps the Dolt db, locks, backups, and
sockets out of commits. On a furnished hive `bh hive init` repairs any stealth exclusion and
commits the scaffold as `chore(agf): hive scaffolding (beads + agent config)` (re-runs amend
if unpushed, or commit as `chore(agf): hive scaffolding repair`). External hives (forks /
distinct-upstream repos) can never be furnished.

---

## Phase 6 — Hive onboarding

A **hive** is a repo's beads database. Onboarding a hive materializes beads locally
(zero-footprint by default), registers the repo in `~/.beadhive/config.yaml`, and optionally
furnishes it with hive furniture (Claude settings, skills, agents — owner-only). This is a
**per-repo** step; run it once per repo you want to track.

### Phase 6a — Survey candidate hives

> **Situation (c) skip-point** — land here if git-workspace is configured and repos are cloned.

Before committing to any onboarding, generate a fleet triage table to see which repos are
ready candidates and which need attention first:

```sh
bh hive survey --available --sort difficulty
```

This shows every unregistered on-disk repo with columns `REG`, `CLASS`, `COMMITS`, `DIRTY`,
`DISK`, and `DIFFICULTY` (`EASY` / `MEDIUM` / `HARD` / `NOT-A-CANDIDATE`). Start with `EASY`
rows — they have no hard signals and `bh hive ready` will pass immediately after init.

See [HIVES — bh hive survey](HIVES.md#bh-hive-survey) for the full column and difficulty
semantics.

The custodian seat can run this fleet-wide via the `bh role custodian` path:

```sh
bh role custodian
```

or launch a Claude session from `$GIT_WORKSPACE` and ask it to triage hives.

### Phase 6b — Onboard a hive

For each candidate, onboard it end-to-end:

```sh
# Dry-run first — see the preflight plan without mutating anything:
bh hive onboard github/myorg/myrepo --dry-run

# Onboard in place, zero-footprint (repo already cloned):
bh hive onboard github/myorg/myrepo

# Onboard + furnish with agent furniture (owner-only; each flag implies --furnish):
bh hive onboard github/myorg/myrepo --claude --skills --agents

# Onboard and clone from remote (if not yet cloned):
bh hive onboard github/myorg/myrepo \
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
| `--observaloop` | OTel telemetry profile for this hive (optional) |

**Where the new hive's beads land.** Onboarding creates the hive's Dolt database on `bd`'s
shared `dolt sql-server` — one per host, started by `bd` itself on `127.0.0.1:3308`, with the
database under `~/.beads/shared-server/dolt/<database>`. There is nothing to start and nothing
to configure; it is the default for every newly onboarded hive since 0.8.0. Hives you onboarded
*before* 0.8.0 keep their in-repo `.beads/embeddeddolt/` engine until you move them with
`bh hive migrate-storage` — re-running onboarding never moves one. Both modes publish issue
history the same way, as `refs/dolt/data` on the repo's own git remote. See [DOLT](DOLT.md).

The preflight DAG (`bh hive onboard --dry-run`) shows every check id before any mutation.
Overridable checks (e.g. `dirty-tree`, `on-default-branch`) can be downgraded to warnings
with `--skip-check <id>` when you have a reason:

```sh
bh hive onboard github/myorg/myrepo --claude \
  --skip-check dirty-tree
```

See [HIVES](HIVES.md) for onboarding details, kind classification, prefix derivation, and the
tracked-scaffold convention.

### Phase 6c — Verify and hand off

After onboarding each hive, confirm hive readiness:

```sh
bh hive ready          # pass/fail check for this repo
bh hive ready -v       # line-item breakdown (required + optional checks)
```

Check the whole fleet:

```sh
bh doctor             # fleet-level health: providers, orgs, hive counts, warnings
```

Build the hub so cross-hive views work:

```sh
bh sync               # aggregate every registered hive into ~/.beadhive/hub
bh hq bd ready        # actionable work across all hives
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

- In `~/.beadhive/config.yaml`, set `providers: [gitlab]` (or `gitea`, etc.) and omit the
  `github` entry. Provider entries are not required at all if the git-workspace integration
  is enabled (it reads providers from `workspace.toml`).
- In `workspace.toml`, declare a `[[provider]]` with `path = "gitlab"` (or the appropriate
  host path) and your org name.
- `bh hive survey` and `bh hive onboard` work identically for GitLab hives as long as the
  repo is under `$GIT_WORKSPACE/<provider>/<org>/<repo>`.

---

## Adding a second machine — the daily driver stays HQ

Everything above stands up **one** machine. This section is for the next question: *I have one
working machine and I want to add another.* It is not "provision a fleet host from nothing" —
that is [`bh host provision`](CLI.md)'s own path, documented in
[UPGRADING.md's 0.6→0.7 section](UPGRADING.md), and this section is the shape around it rather
than a second description of the verb.

### Read this first: your new machine needs an HQ *remote*

A second machine joins by cloning Factory HQ, and **you cannot clone something that only
exists on one laptop.** If you followed the single-machine path, your HQ is deliberately
local-only — no remote, because a remote earns its keep only for backup or a second host.
Adding a worker is exactly when it starts earning it.

So the graduation step comes **first**, on the machine you already have:

```sh
# create an empty private repo for HQ under your account or org, then:
bh hq push        # refuses if no remote is configured — configure it, then re-run
bh hq status      # confirm the remote is wired and current
```

Do this before touching the new machine. Otherwise you meet the requirement as a provisioning
failure halfway through setting up the worker, which is the same lesson learned twice.

### Who does what

The division of labour is the part that makes the role flags make sense:

| | daily driver | added machine |
|---|---|---|
| **Is** | the HQ machine, and your supervisor interface | a dedicated worker |
| **Does** | files, grooms, visualises and manages beads | holds leases and works beads |
| **Role** | `primary-default` (or `adopt-on-demand`) | `worker` |
| **Is not** | a worker | where beads get filed |

The role vocabulary, at the point you have to choose one:

- **`primary-default`** — this host is the default primary for hives it registers. The daily
  driver's normal setting.
- **`adopt-on-demand`** — registers hives but takes primary only when asked. Use when you want
  a machine to participate without it claiming ownership by default.
- **`worker`** — takes primary for particular repos and holds their leases, executing work.
  What you want for an added machine.

### On the new machine

Install `bh` ([INSTALL.md](../INSTALL.md), managed path), then:

```sh
bh host provision --role worker    # clones HQ from the remote you just wired
```

### Verify it landed

From **both** machines, not just the new one:

```sh
bh host list                      # both hosts appear, neither stale
bh host list --lease-hive <hive>  # the lease is visibly held by the worker
```

Two hosts listed on one machine and one on the other means HQ is not syncing — re-check
`bh hq status` on each.

### Known gap, deferred deliberately

The daily driver may still **create** beads, but ideally should not **update** ones that are in
flight or claimed on another host. **This is not enforced.** It is a deferred operator decision
recorded on `bh-vmdq.6`, not an oversight — nothing today will stop you, so treat it as a
working convention until it is.

---

## Future sections

The following are documented as design intent but not yet built.

### Other operating systems

This guide targets **macOS + Claude Code**. Linux (apt/nix prereqs) and other harnesses
(Codex, etc.) are planned future extensions. The `bh setup check` probe table will grow
OS-specific install paths when those land; the gate contract (`setup==true` in
`~/.beadhive/setup-state.json`) records an OS tag for this purpose.

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
- [HIVES](HIVES.md) — onboarding, kinds, prefix derivation, the scaffold convention
- [CONFIGURATION](CONFIGURATION.md) — `~/.beadhive/config.yaml` schema, all `bh config` commands
- [HUB](HUB.md) — `bh sync` and the cross-hive aggregate (`~/.beadhive/hub`)
- [INTEGRATIONS](INTEGRATIONS.md) — the git-workspace integration
- [WORK](WORK.md) — `bh work` and the bead lifecycle
- [DIAGNOSTICS](DIAGNOSTICS.md) — `bh doctor`
- [setup skill][setup-skill] — the interactive onboarding driver

[setup-skill]: https://github.com/beadhive/claude-plugin/blob/main/bh/skills/setup/SKILL.md
