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
just check-native      # fast native gate: lint, contracts, every non-integration core test
just check-pants       # fast Pants impact-selected gate
just check-all-native  # full native gate: core, integration, packages, builds, demos
just check-all-pants   # full Pants gate: proven partition, packages, integration, demos
just check            # alias for the currently selected fast profile
just check-all        # alias for the currently selected full profile
just test       # unit tests only
just test ""    # full suite (unit + integration; integration self-skips without a real bd)
just lint       # ruff check
just fmt        # ruff format
```

The four profile-explicit commands are stable across a profile switch. `check` and `check-all`
are convenience aliases; use the explicit command when recording a validation verdict. The
`pre-commit` hook runs the ~3s
`just conventions` subset, deliberately — a six-minute pre-commit gets `--no-verify`'d within a
week.

`just check-all` is the **main-merge gate**: lefthook's `pre-push` job runs it automatically,
and only when the push updates `main` (`scripts/main-push-gate.sh`). It refuses to run at all
without `bd` on PATH rather than passing vacuously — every integration test self-skips without
the binary, so a `bd`-less run of the full suite is green and proves nothing.

## Pushing `main` — use `just push`, not `git push`

```sh
just push          # origin main, through the gate, then verified with ls-remote
```

**A green gate is not a landed push.** `git push` opens its connection to the remote *before*
the pre-push hook runs — the hook needs the remote's ref list on stdin, so the socket is already
open when the ~390s gate starts. GitHub closes the idle connection, and git finishes a fully
green gate and then writes to a dead socket:

```text
Connection to github.com closed by remote host.
EXIT=141                                              # 128+13 = SIGPIPE
error: failed to push some refs to github.com:beadhive/beadhive.git
```

Measured three times while pushing 0.11.2. One of those runs passed the gate clean
(`main-gate (371.39 seconds)`, every phase green) and the remote **still never moved** — it was
reported as a successful push on the strength of the green output and was caught an hour later
by `git ls-remote`.

`just push` ([`scripts/push-main.sh`](scripts/push-main.sh)) fixes and detects that:

- it sets `GIT_SSH_COMMAND='ssh -o ServerAliveInterval=30 -o ServerAliveCountMax=10'`, so the
  connection is not idle during the gate. **Do not remove this as unexplained cruft** — without
  it a green gate silently fails to push, roughly one run in three on this link;
- it verifies with `git ls-remote` against the **actual remote**, not the local `origin/main`
  tracking ref (which a failed push never updates — that is how the false success was reported);
- it reports a green-gate-then-SIGPIPE failure *distinctly* from a failed suite, because those
  need different responses.

Two traps worth knowing even if you never read the script:

- **`git push | tail` returns *tail's* exit status.** Piping the push through anything —
  `| tail`, `| grep`, `| cat` — throws away git's exit code unless you also set
  `set -o pipefail` or read `${PIPESTATUS[0]}`. That hid this failure twice in one evening.
  The trap applies to **every command whose status matters**, not only `git push`: reading it as
  a rule about the push is how `push-main.sh` shipped with `git ls-remote … | awk`, which turned
  a failed *verification* into the confident sentence "that combination should be impossible".
  "I could not look" is now its own outcome, with its own exit code (`3`).
- **Never "fix" a stuck push with `--no-verify`.** That bypasses the gate entirely, and once it
  becomes habit the gate is gone.

## Release artifact provenance

The release workflow builds and checks one wheel and one source distribution without OIDC
privileges, records their SHA-256 hashes, and transfers those exact files to a publish-only job.
That job uses PyPI Trusted Publishing and Sigstore keyless signing to upload a PEP 740 publish
attestation for each distribution through PyPI's Integrity API. It never rebuilds the files.

Git signatures and distribution attestations prove different things. Signed commits and signed
release tags authenticate objects in the Git history using the maintainer's configured signing
key. PEP 740 attestations authenticate the `.whl` and `.tar.gz` files using the GitHub Actions
OIDC identity trusted by PyPI. They do not embed deprecated wheel signatures or upload detached
PGP signatures.

After PyPI publishes a release, verify both files and require their signing identity to match this
repository. For example, the v0.16.2 release proof is:

```sh
uvx pypi-attestations verify pypi \
  --repository https://github.com/beadhive/beadhive \
  pypi:beadhive-0.16.2-py3-none-any.whl
uvx pypi-attestations verify pypi \
  --repository https://github.com/beadhive/beadhive \
  pypi:beadhive-0.16.2.tar.gz
```

The verifier fetches each file's provenance from PyPI's Integrity API and fails if the artifact,
attestation, or expected repository identity does not match.

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
