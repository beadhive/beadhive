---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/install.schema.json
install:
  id: beadhive
  summary: Beadhive — the `bh` CLI, the integration-plane driver for Agentic Git Flow (AGF) and cross-repo beads issue tracking.
  methods:
    # Alternatives — pick ONE. Order is preference, and the FIRST is the recommended
    # one (bh-vmdq.1, 2026-08-06): the managed path is the only route that also installs
    # and PINS the four tools `bh` drives (bd, dolt, gh, git-workspace) via flake.lock.
    # Everything below it installs `bh` alone and leaves those four to the machine.
    #
    # The managed path is listed as `kind: script` rather than excluded like Docker,
    # because neither Docker objection applies: it ends with `bh` natively on the HOST
    # PATH, so `verify` and every `configure` step below work unchanged.
    #
    # ITS ONE PRECONDITION IS NIX, which this entry deliberately does NOT install: that
    # needs root (and on macOS creates an APFS volume), so it is a human step, not
    # something an install agent should perform unattended. If nix is absent the command
    # fails immediately with `nix: command not found` and an agent falls through to the
    # PyPI methods below — which is the correct outcome, since "cannot install nix" is
    # exactly who the PyPI route is for. The prose below covers installing nix.
    #
    # The v0.8.0 TAG is deliberate, not a branch ref: `github:beadhive/beadhive#default`
    # resolves the default branch, which can lag the release and would silently install a
    # toolchain this version does not ship. Tag refs are immutable. Keeping this in sync
    # with the released version is bh-wp6h.
    - kind: script
      os: [macos, linux]
      command: nix profile install github:beadhive/beadhive/v0.8.0#default && uv tool install 'beadhive[otel]'
    # PyPI installers (uv > pipx > pip) pour prebuilt wheels — seconds, no toolchain —
    # but install `bh` ONLY. Run `bh setup check` afterwards to see what is missing.
    # Homebrew is last because it compiles the native deps (pydantic-core, cryptography,
    # rpds-py) from source unless a bottle is already published — minutes, and a full
    # rust+llvm build.
    - kind: package
      manager: uv
      os: [macos, linux]
      command: uv tool install 'beadhive[otel]'
    - kind: package
      manager: pipx
      command: pipx install 'beadhive[otel]'
    - kind: package
      manager: pip
      command: pip install 'beadhive[otel]'
    - kind: package
      manager: homebrew
      os: [macos, linux]
      command: brew install beadhive/tap/beadhive
    # The Docker path is documented in prose below but DELIBERATELY absent here.
    # Two reasons, both checked rather than assumed:
    #   1. The schema has no container/docker/image kind — it allows only package,
    #      plugin, skill, guide, script and manual. Forcing it into `script` or
    #      `manual` would misdescribe it.
    #   2. More important: `verify` and every `configure` step below assume `bh` on
    #      the HOST PATH. A Docker user has no host `bh` — `bh --version` fails and
    #      `bh config init` has to run inside the container. Listing Docker as a
    #      peer method would hand an automated installer instructions that cannot work.
    # Revisit if the schema gains a container kind AND per-method verify/configure.
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

`bh` doesn't work alone — it drives four other tools: `bd` (beads), `dolt`, `gh`
and `git-workspace`. **That is the whole difference between these two routes.**
The managed path installs and version-pins all five together. The PyPI route
installs `bh` and leaves the other four to whatever your machine happens to have.

### Managed path (recommended)

Two commands, after a one-time nix install.

**a. Install nix**, if you don't have it. This needs `sudo` — it installs a
system daemon, and on macOS creates an encrypted APFS volume for `/nix`:

```sh
curl --proto '=https' --tlsv1.2 -sSf -L https://install.determinate.systems/nix | sh -s -- install
```

It leaves uninstall receipts, so `/nix/nix-installer uninstall` backs the whole
thing out cleanly if you're only evaluating.

**b. Install the toolchain and `bh`:**

```sh
nix profile install github:beadhive/beadhive/v0.8.0#default   # bd, dolt, gh, git-workspace, git, uv, just
uv tool install 'beadhive[otel]'                              # bh itself (uv came from the line above)
```

Measured cold on an Apple Silicon Mac: **~130 seconds** and ~2–3 GB of disk for
step b, almost all of it download rather than compilation.

**Requirements and limits, stated up front:**

- **macOS: Apple Silicon only.** Intel Macs are gone from nixpkgs, so there is no
  managed path for them — use the PyPI route below.
- **Linux: x86_64 is proven**; arm64 evaluates but hasn't been run in anger.
- **You need root for step a.** On a corporate-managed machine that forbids a
  root daemon install or an APFS volume, this path is not available to you at
  all — that is what the PyPI route is for, and it is a legitimate reason to
  use it.

**Optional add-ons for plugins** (Orca and friends) are **plain nixpkgs installs**, not flake
outputs — `flake.nix` exposes only `beads`, `default`, `image` and `metadata`, and there is
deliberately no per-plugin output. Add what a plugin needs to the same profile:

```sh
nix profile install nixpkgs#<package>
```

Those are **not** pinned by `flake.lock` — only the four toolchain deps are. That is the
tradeoff for not inventing a flake output per plugin.

### PyPI route (not recommended)

Use this if you can't install nix, or won't. It works, and it is genuinely one
command — but it installs **`bh` only**:

```sh
uv tool install 'beadhive[otel]'   # puts `bh` on PATH (~/.local/bin)
pipx install 'beadhive[otel]'      # or: pip install 'beadhive[otel]'
brew install beadhive/tap/beadhive # slower — see note
```

**What it does not cover:** `bd`, `dolt`, `gh` and `git-workspace` are not
installed, not version-matched, and not pinned. Run this straight afterwards to
see exactly where you stand on your machine:

```sh
bh setup check
```

On this route you also need to install `bd` yourself, from HEAD —
`brew install --HEAD beads` — because every tagged release through v1.1.2 embeds
a dolt older than v2.2.0, whose pull can hang on a large store. **The managed
path above needs none of this**: its `bd` is already that build, pinned.
[`docs/DOLT.md`](docs/DOLT.md) has the detail.

Homebrew is last because it builds `bh`'s native deps (pydantic-core,
cryptography, rpds-py) from source unless a bottle is published for your
platform — a full rust + llvm build, minutes. Prefer `uv` unless you specifically
want the `brew` workflow.

The `[otel]` extra enables OpenTelemetry signals out of the box; drop it if you
don't want them. The MCP server ships in the core install.

Since 0.8.0 a new hive's Dolt database is created on **`bd`'s own shared
`dolt sql-server`** — started by `bd` itself on `127.0.0.1:3308`, nothing for you
to run, on either route.

### Docker (nothing installed but Docker)

The container image bundles `bh` with the tools it drives — `bd`, `dolt`, `git`,
`gh`, `git-workspace`, `jq`, `yq`, `just` — at versions validated together, so
there is nothing to install and nothing to match up.

**Build it locally.** There is no published image yet, so this is a *bake*, not a
`docker compose pull`:

```sh
git clone https://github.com/beadhive/beadhive && cd beadhive
just image core      # or `just image` for core + the agent image
docker compose up -d
```

Then inside the container:

```sh
docker compose exec bh sh
bh setup check       # passes unattended — reads the image's component manifest
bh config init       # scaffolds config.yaml + host.yaml into the mounted volume
bh config show
```

`bh setup check` needs no interactive fix in-container: the image ships
`/etc/beadhive/image-manifest.json` recording every bundled component, and `bh`
reads it instead of probing each binary. If you find advice elsewhere to set
`BH_SKIP_SETUP_CHECK=1`, it predates that and is no longer needed.

`bh config init` is required before `bh config show`, `bh wt` or `bh hq` will
run — each errors with the exact command to fix it, but running the two above in
order avoids meeting the errors at all.

> **Not yet covered here:** authorizing GitHub from inside the container (device
> flow), logging in to an agent harness, and cloning your first repo. Those are
> tracked separately and are not part of this path yet.

State lives in four named Docker volumes (`bh-hq`, `bh-workspace`,
`bh-worktrees`, `bh-harness`) so `docker system df -v` sizes each area and your
credentials survive a rebuild. See `.env.example` for the knobs, including
running a headless host with `CLAUDE_CODE_OAUTH_TOKEN`.

## 2. Verify

```sh
bh --version     # bh itself
bh setup check   # the four tools bh drives
```

`bh --version` should print a version. If it does not, `bh` is not on your PATH —
`uv tool` and `pipx` install to `~/.local/bin`; add it to your shell profile:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

`bh setup check` is the one that tells the two routes apart. On the managed path
it reports **4 of 4** — that is the whole point of it. On the PyPI route it
reports whatever your machine already had, and anything it lists as missing or
unpinned is yours to install and keep matched by hand.

## 3. Configure

Run these in order.

1. **Scaffold the config home** (required, one-time per machine). Writes
   `config.yaml` and templates into `~/.beadhive/`:

   ```sh
   bh config init
   ```

   This only scaffolds a static template — no orgs/providers are configured and
   git-workspace is not set up yet. Claude Code users: run `/setup` (or ask
   Claude to load the `agf:setup` skill) now to be walked interactively through
   orgs, providers, git-workspace, and registering your first hive.

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

That's it. `bh` is installed, verified, and configured. `bh config init` only
scaffolds a static template — you still need orgs/providers, a git-workspace, and
a registered hive before you have a working AGF workspace. Next steps:

- **Guided path (recommended):** run `/setup`, or ask Claude to load the
  `agf:setup` skill. It's the interactive agent-native driver that walks you
  from here through orgs/providers, the git-workspace walkthrough, and hive
  registration, probing before it acts and safe to re-run.
- **Reference narrative:** [`docs/ONBOARDING.md`](docs/ONBOARDING.md) is the
  full fresh-machine-to-working-hive walkthrough the `setup` skill drives —
  read it standalone if you'd rather follow the steps by hand, or as reference
  behind the skill.
- [`README.md`](README.md) has the overview and docs map.
