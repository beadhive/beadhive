# Beadhive (`bh`)

![Ship software, not slop.](docs/assets/brand/banner-readme.png)

[![PyPI version](https://img.shields.io/pypi/v/beadhive)](https://pypi.org/project/beadhive/)
[![Python versions](https://img.shields.io/pypi/pyversions/beadhive)](https://pypi.org/project/beadhive/)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)

`bh` is a single CLI for managing **beads** issue tracking across many repositories. Each
repo is its own beads database (a **hive**) with a short, stable prefix; `bh` onboards them,
keeps their labels consistent, runs `bd`/`git` across one or all of them, and aggregates
every hive into one cross-repo view — even hives whose code isn't checked out.

It's a thin orchestrator over `bd`, `git`, `git-workspace`, `dolt`, and `docker`: `bh`
encodes the conventions, the registry, validation, and routing. Config and runtime state live
under `~/.beadhive/`; **no issue data lives there** — each hive's issues live in its own Dolt
DB under `refs/dolt/data` on that repo's own git remote.

`bh` is the **Beadhive** umbrella's workspace CLI — the integration-plane driver for **AGF**
(Agentic Git Flow), the abstract, tracker-independent process. **Beadflow** is that process
implemented on beads: this repo's concrete implementation, unchanged behavior under a naming
layer. See [docs/AGF.md](docs/AGF.md) for the process and
[docs/design/limn-naming-strategy-adr.md](docs/design/limn-naming-strategy-adr.md) for the
naming decision record.

This repo is the CLI's source (Python package `beadhive` on PyPI, command `bh`). For what
Beadhive is conceptually, rather than how to drive it, see [beadhive.ai](https://beadhive.ai).

## Install

**Agents:** point your agent at [`INSTALL.md`](INSTALL.md) — the preferred install path. It
carries a structured `install:` frontmatter block (the agent reads it, discloses the plan,
and asks before each command) plus a prose fallback any agent or human can follow.

Doing it by hand? There are two routes, in this order.

### Managed path (recommended)

`bh` doesn't work alone — it drives `bd`, `dolt`, `gh` and `git-workspace`. This is the only
route that installs and **version-pins all of them with it**, from `flake.lock`:

```sh
nix profile add github:beadhive/beadhive/latest#default       # bd, dolt, gh, git-workspace, git, uv, just
uv tool install --force 'beadhive[otel]'                      # bh itself (uv came from the line above)
bh --version                                                  # must print the released version
```

`--force` and that third line are both load-bearing, not decoration: unforced, `uv tool
install` no-ops on a machine that already has `bh` and **still exits 0**. Measured on macOS
with 0.7.1 installed, it reported "Installed 2 executables: bh, bh-mcp" and `bh --version`
still said 0.7.1. This step is done when the version is right, not when the install exits 0.

`latest` is a **release channel branch**, not a version: CI moves it onto each release's commit
once that release publishes, so this line never carries a version and never needs a release-day
edit. Don't shorten it to `github:beadhive/beadhive#default` — that resolves the *default
branch*, which is not the latest release.

The one precondition is nix, which needs root — a system daemon, and an APFS volume on macOS.
[`INSTALL.md`](INSTALL.md#managed-path-recommended) carries the one-time installer, the ~130s
/ 2–3 GB cold cost, the platform limits (macOS: Apple Silicon only) and the nix ≥ 2.30 that
`nix profile add` needs.

### PyPI route (fallback, not recommended)

For machines where you can't install nix, or won't. It works, and it's genuinely one command
— but it installs **`bh` alone**, leaving the other four tools to whatever the machine happens
to have, including a `bd` you then install from HEAD by hand:

```sh
uv tool install --force 'beadhive[otel]'   # or: pipx install --force 'beadhive[otel]'
brew install beadhive/tap/beadhive         # Homebrew — slower, builds native deps from source
bh --version                               # same check, and for the same reason
bh setup check                             # reports which of the four tools you're missing
```

See [`INSTALL.md`](INSTALL.md#pypi-route-not-recommended) for what that leaves you to keep
matched by hand, and for the Docker route.

### First run — rung 1

One laptop, local-only. From a fresh install to a ready list:

```sh
bh config init                              # scaffold ~/.beadhive
bh mcp install                              # Claude Code: claude mcp add bh --scope user
bh hq init                                  # local-only HQ; no remote wired, deliberately
bh hive onboard <provider>/<org>/<repo>     # zero-footprint by default
bh work ready
```

Run `bh setup guide` to finish setup — a guided, probe-first walk from a bare install to a
configured workspace. It covers the sequence above plus the parts that aren't one command
(orgs, providers, git-workspace), checking each step's state before it acts, so it is also
safe on a machine that is already half-configured. Reach for it if you installed via
`brew`, `pip` or a copy-pasted command and never saw [`INSTALL.md`](INSTALL.md).

**What that costs:** HQ is local — no backup, and no second machine yet. That's the posture,
not an omission; wiring a remote is rung 2. See [`docs/ADOPTION.md`](docs/ADOPTION.md) for the
four rungs, what each buys, and what staying on this one costs.

### Agent harnesses

`bh` furnishes AGF seats for **Claude Code** (`--claude`) and **OpenCode** (`--opencode`) —
pass either to `bh hive onboard <provider>/<org>/<repo>`. `docs/AGF.md` carries the
[per-harness support matrix](docs/AGF.md#per-harness-support-matrix), including what does and
doesn't apply for **codex**. On Claude Code, the `bh` claude-plugin vends the seat agent defs
and role skills:

```sh
claude plugin marketplace add beadhive/claude-plugin
claude plugin install bh@beadhive
```

## Going further

One line each, and who it's for:

- [`docs/ADOPTION.md`](docs/ADOPTION.md) — **it works; what's the next rung?** The four rungs,
  what each buys, and what staying on yours costs.
- [`INSTALL.md`](INSTALL.md) — **picking a route.** Managed path, PyPI and Docker, and the
  tradeoffs between them.
- [`docs/ONBOARDING.md`](docs/ONBOARDING.md) — **fresh machine, step by step.** Zero to a
  configured AGF workspace with registered hives.
- [`docs/UPGRADING.md`](docs/UPGRADING.md) — **moving between versions, or between routes.**
- [`docs/HQ.md`](docs/HQ.md) — **Factory HQ.** What it is and what it stores.
- [`docs/HIVES.md`](docs/HIVES.md) and the
  [multi-host ADR](docs/design/multi-host-model-adr.md) — **more than one host.** Hive kinds,
  leases, and host roles.
- [beadhive.ai](https://beadhive.ai) — **what Beadhive is, conceptually**, if you want the
  shape before the commands.
- [`docs/OVERVIEW.md`](docs/OVERVIEW.md) — **everything else.** Design and reasoning,
  configuration, the full command surface, component by component.

## Questions / feedback

General questions, feedback, and bug reports go through
[GitHub Issues](https://github.com/beadhive/beadhive/issues). For security vulnerabilities,
see [`SECURITY.md`](SECURITY.md) instead of filing a public issue.

## Develop

<details>
<summary><strong>Developing <code>bh</code> itself</strong></summary>

You don't need any of this to *use* `bh` — it's for working on the CLI's own source.

```sh
# On a NEW machine you do not have `just` yet — it is pinned in .mise.toml, not the Brewfile:
brew bundle --file=Brewfile     # provides mise
mise exec -- just bootstrap     # mise installs the pinned just, then runs bootstrap

just bootstrap   # brew bundle + mise install + uv sync   (once per machine; needs just)
just install     # build + install this checkout → ~/.local/bin/bh
just lint        # ruff check
just fmt         # ruff format
just test        # pytest
just build       # wheel + sdist into dist/  (stamped local)
```

`just install` and `just build` stamp the artifact with a PEP 440 **local
segment** — `0.11.5+local.g790ef0d`, plus `.dirty` when the checkout had
uncommitted changes — so `bh --version` distinguishes your build from the
release it was built from, and the wheel filename says what is under test. PyPI
forbids local segments, so a local build can never be published by accident;
`just build-release` is the deliberate opt-out that produces a publishable
artifact (and is what CI runs on a `v*` tag).

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for the plain-git contributor path — setup, tests,
and how to submit a change.

*Collapsed on purpose, not by oversight.* It pairs with "Manual install" on beadhive.ai:
both are real content that simply isn't what most readers came for, so it is disclosed
rather than deleted. Please leave it closed.

</details>
