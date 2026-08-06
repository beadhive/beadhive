# Contributing to Beadhive

Thanks for looking at `bh`. This is a plain-git contributor path — you don't need the `bh`/`bd`
tooling this repo uses internally (see [docs/AGF.md](docs/AGF.md) if you're curious how the
maintainers drive their own changes).

## Set up

```sh
git clone https://github.com/beadhive/beadhive.git
cd beadhive
brew bundle --file=Brewfile   # provides mise — the one tool bootstrap cannot self-install
mise exec -- just bootstrap   # mise installs just@1.54.0 from .mise.toml, then runs the rest
```

If you already have `just`, plain `just bootstrap` does the same thing. On a **new machine you
do not**, and `just` is pinned in `.mise.toml` rather than the Brewfile — so `just bootstrap`
would be a command you cannot type yet. `mise exec --` resolves that: it installs the pinned
`just` on demand, which also means you get 1.54.0 rather than whatever `brew install just`
would have handed you.

`just bootstrap` installs the toolchain (Homebrew bundle + [mise](https://mise.jdx.dev/) for
pinned tool versions), syncs Python deps with [uv](https://docs.astral.sh/uv/), and wires the
tracked git hooks (`pre-commit` → `just check`). See [README.md](README.md#develop) for the
individual pieces if you'd rather do it by hand.

A **container runtime is a prerequisite**, not something bootstrap installs — Docker Desktop,
colima or OrbStack on macOS, the distro's daemon on Linux. `bh` drives whichever it finds.
Native mode needs none.

## Run tests and checks

```sh
just check      # fast gate: ruff lint + markdown lint + unit tests — run this before pushing
just check-all  # full gate: the above + the real-bd integration suite (needs `bd` on PATH)
just test       # unit tests only
just test ""    # full suite (unit + integration; integration self-skips without a real bd)
just lint       # ruff check
just fmt        # ruff format
```

`just check` is the one to have green before you open a PR. The `pre-commit` hook runs the ~3s
`just conventions` subset, deliberately — a six-minute pre-commit gets `--no-verify`'d within a
week.

`just check-all` is the **main-merge gate**: lefthook's `pre-push` job runs it automatically,
and only when the push updates `main` (`scripts/main-push-gate.sh`). It refuses to run at all
without `bd` on PATH rather than passing vacuously — every integration test self-skips without
the binary, so a `bd`-less run of the full suite is green and proves nothing.

## Submitting a change

1. Branch off `main`.
2. Commit using [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`,
   `chore:`, `docs:`, etc.) — this repo's changelog and version bumps are generated from commit
   history via [Commitizen](https://commitizen-tools.github.io/commitizen/).
3. Make sure `just check` passes.
4. Open a PR against `main`. See [CODEOWNERS](.github/CODEOWNERS) for who reviews what area of
   the codebase.

## Code style

Formatting and linting are enforced by `ruff` (`just fmt` / `just lint`) and markdown by
`markdownlint-cli2` (`just lint-md`) — run `just check` rather than guessing at style by hand.

## Questions

Start at [docs/ONBOARDING.md](docs/ONBOARDING.md) or [docs/OVERVIEW.md](docs/OVERVIEW.md) for
how the project fits together, or open an issue.
