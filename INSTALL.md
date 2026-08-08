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
    # `nix profile add` (not the deprecated `install` alias) needs NIX >= 2.30.0, the release
    # that renamed it — `add` is absent from 2.29.0's `src/nix/profile.cc` and present in
    # 2.30.0's, tag dated 2025-07-08. The installer below pins a current nix, so this only
    # binds a host with a pre-existing older one, where it fails loudly with `unknown command`
    # rather than doing the wrong thing quietly. The remaining `nix profile install` call sites
    # are code, not docs, and are bh-2igmr.
    #
    # `latest` IS A RELEASE CHANNEL BRANCH — not a version, and NOT the default branch. CI
    # moves it onto each release's commit only after that release has actually published, so
    # this line carries no version and needs no release-day edit (bh-7daa6; the reasoning is
    # docs/design/release-channel-branches-adr.md).
    #
    # DO NOT "SIMPLIFY" THIS TO `github:beadhive/beadhive#default`. That resolves the DEFAULT
    # BRANCH, which is not the latest release and is gated on nothing: `main` measured 4 commits
    # ahead of v0.8.4 on 2026-08-07, and 31 commits ahead of the release on 2026-08-06. Paired
    # with the `uv tool install` below — which pulls the RELEASED wheel — that hands a new user
    # toolchain-from-`main` plus bh-from-release, and the skew is invisible because both halves
    # exit 0. That argument is why a `v0.8.0` tag was pinned here originally, and it is still
    # correct; it argues against `#default`, not FOR a pin, and the channel answers it without
    # one. Immutability now lives one layer down, on the commit and its tag, the way npm's
    # `latest` dist-tag and a Docker tag's `sha256:` digest do.
    #
    # SCOPE — THE CHANNEL IS FOR THIS BOOTSTRAP CASE ONLY: no `bh` is installed yet, so there is
    # no version to derive from. `bh setup toolchain` must KEEP deriving `v{version}` from the
    # installed package (`src/beadhive/setup.py`, `toolchain_flake_ref()`); that is strictly
    # better there, because it names the immutable tag matching the `bh` that is running, so the
    # tool and its toolchain cannot disagree. `tests/test_flake_toolchain.py` asserts that ref is
    # a `v*` tag — switching it to a channel "for consistency" fails that test on purpose.
    - kind: script
      os: [macos, linux]
      command: nix profile add github:beadhive/beadhive/latest#default && uv tool install --force 'beadhive[otel]'
    # PyPI installers (uv > pipx > pip) pour prebuilt wheels — seconds, no toolchain —
    # but install `bh` ONLY. Run `bh setup check` afterwards to see what is missing.
    # Homebrew is last because it compiles the native deps (pydantic-core, cryptography,
    # rpds-py) from source unless a bottle is already published — minutes, and a full
    # rust+llvm build.
    - kind: package
      manager: uv
      os: [macos, linux]
      command: uv tool install --force 'beadhive[otel]'
    - kind: package
      manager: pipx
      command: pipx install --force 'beadhive[otel]'
    - kind: package
      manager: pip
      command: pip install --upgrade 'beadhive[otel]'
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
    # 4. Finish setup — the guided walk from a scaffolded config to a working AGF
    #    workspace (orgs/providers, git-workspace, HQ, the first hive). LAST on purpose:
    #    it is the one step that assumes everything before it may already be done.
    #
    #    IT OVERLAPS STEPS 1-3 ON PURPOSE — DO NOT "DEDUPLICATE" IT (bh-0olv9.9). The
    #    bundled guide's steps 050 (config init), 060 (MCP wiring) and 065 (plugin) cover
    #    these same three commands, and every one of them is PROBE-FIRST: each reads the
    #    machine's state, reports ALREADY SATISFIED, and moves on rather than re-running
    #    (src/beadhive/assets/guides/setup/steps/050-config-init.md names THIS configure[]
    #    block as the reason its normal path is a no-op). Deleting either side to remove
    #    the duplication breaks the other side's audience: drop the guide steps and a
    #    reader who installed by `brew`/`pip` — never seeing this file — loses them
    #    entirely; drop these three and a conforming installer stops short of a
    #    configured machine.
    - kind: script
      command: bh setup guide
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
nix profile add github:beadhive/beadhive/latest#default       # bd, dolt, gh, git-workspace, git, uv, just
uv tool install --force 'beadhive[otel]'                      # bh itself (uv came from the line above)
bh --version                                                  # must print the released version
```

`--force` is load-bearing if you **already have `bh`**. Without it `uv tool
install` prints "already installed", exits 0, and leaves the old `bh` in place —
a fresh nix toolchain wrapped around a stale binary, with nothing in the output
saying so. Measured on macOS with 0.7.1 installed: the unforced command reported
"Installed 2 executables: bh, bh-mcp" and `bh --version` still said 0.7.1. That
is why the third line is a step and not a suggestion — this path is done when
`bh --version` says the released version, not when the install exits 0.

`latest` is a **release channel branch**, not a version: CI moves it onto each
release's commit once that release has published, so this line stays current
without anyone editing it here. Do not shorten it to
`github:beadhive/beadhive#default` — that resolves the repository's *default
branch*, which is not the latest release and would install a toolchain the
released `bh` does not ship. (`bh setup toolchain`, which runs on a machine that
already **has** `bh`, deliberately uses the immutable `v{version}` tag matching
the running `bh` instead; the channel exists for this bootstrap case, where there
is no installed version to derive from. See
[the ADR](docs/design/release-channel-branches-adr.md).)

Measured cold on an Apple Silicon Mac: **~130 seconds** and ~2–3 GB of disk for
step b, almost all of it download rather than compilation.

**Requirements and limits, stated up front:**

- **macOS: Apple Silicon only.** Intel Macs are gone from nixpkgs, so there is no
  managed path for them — use the PyPI route below.
- **Linux: x86_64 is proven**; arm64 evaluates but hasn't been run in anger.
- **nix ≥ 2.30** for `nix profile add`, which 2.30.0 renamed from `nix profile
  install` (tag dated 2025-07-08; `add` is absent in 2.29.0). Step a's installer
  pins a current nix, so this only binds a host with an older pre-existing one —
  there the command fails loudly with `unknown command`, and the deprecated
  `nix profile install` spelling still works.
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
uv tool install --force 'beadhive[otel]'   # puts `bh` on PATH (~/.local/bin)
pipx install --force 'beadhive[otel]'      # or: pip install --upgrade 'beadhive[otel]'
brew install beadhive/tap/beadhive         # slower — see note
bh --version                               # must print the released version
```

Same reason for `--force` as the managed path above: unforced, every one of
these no-ops on a machine that already has `bh` and exits 0 anyway. Trust
`bh --version`, not the exit code.

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
   git-workspace is not set up yet. Step 4 below is what walks you through those;
   don't stop here.

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

4. **Finish setup** — the steps above leave you with a configured *binary*, not a
   working workspace. This is the walk from here to orgs/providers, a
   git-workspace, HQ, and your first registered hive:

   ```sh
   bh setup guide
   ```

   It ships **inside `bh`**, so it needs no plugin, no marketplace, and no
   particular harness — it exports the guide to `~/.beadhive/guides/setup/`, hands
   it to a Guide-aware agent, and falls back to an interactive walk in the
   terminal when there isn't one.

   **Its first three steps deliberately repeat steps 1–3 above, and that is not a
   bug.** Guide steps 050 / 060 / 065 cover `bh config init`, `bh mcp install` and
   the plugin, and each one **probes before it acts**: on a machine that already
   ran the commands above it reports *already satisfied* and moves on. That
   overlap is what lets the same guide serve a reader who never saw this file —
   someone who ran `brew install` or `pip install`. Don't delete either copy to
   "fix" the duplication.

That's it. `bh` is installed, verified, and configured. Where to go next:

- **Guided path (recommended):** `bh setup guide`, step 4 above. It is the
  primary next step because it ships with `bh` itself, works with no plugin
  installed, and doesn't care which agent harness (if any) you're reading this
  in — it probes before it acts and is safe to re-run.
- **Claude Code alternative:** run `/setup`, or ask Claude to load the `agf:setup`
  skill — the same walk driven by the claude-plugin, if you installed it in
  step 3.
- **Reference narrative:** [`docs/ONBOARDING.md`](docs/ONBOARDING.md) is the
  full fresh-machine-to-working-hive walkthrough behind both of those — read it
  standalone if you'd rather follow the steps by hand.
- [`README.md`](README.md) has the overview and docs map.
