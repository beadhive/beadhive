---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: install-bh
  title: Install bh by the chosen route, with --force
  requires: [choose-route]
  performer: agent
  action:
    type: prompt
    prompt: |
      Run the command for the route recorded at 020. Offer it and wait for
      approval before running it.

      managed:
        nix profile install github:beadhive/beadhive/<TAG>#default
        uv tool install --force 'beadhive[otel]'

      pypi — pick the FIRST installer preflight found on this machine, in
      this order (uv, then pipx, then pip, then Homebrew):
        uv tool install --force 'beadhive[otel]'
        pipx install --force 'beadhive[otel]'
        pip install --upgrade 'beadhive[otel]'
        brew install beadhive/tap/beadhive

      <TAG> is the pinned release tag from INSTALL.md's `methods:` entry.
      Read it from there rather than from memory: it is a TAG and not a
      branch ref on purpose, because `#default` on the default branch can
      resolve a toolchain the release does not ship.

      --force / --upgrade is load-bearing, not decoration. Without it an
      already-installed bh makes the installer a no-op that still exits 0.

      If the managed command fails with `nix: command not found`, that is
      the expected fall-through and not an error to escalate: go back to
      020's PyPI branch and install from there.
  verify:
    type: script
    script: scripts/verify-bh-version.sh
    success_exit: 0
    output_schema: text
  interactions:
    - id: approve-install
      when: before
      kind: confirm
      prompt: |
        This is the first command that changes the machine. It downloads and
        installs `bh` (and, on the managed route, bd/dolt/gh/git-workspace).
        Approve the exact command shown before it runs.
  on_failure:
    - strategy: retry
      max_retries: 1
      reason: |
        MISMATCH (verify exit 1) — `bh --version` disagrees with the version
        the package manager reports installed. A stale `bh` is still earlier
        on PATH. Re-run the route command WITH --force, re-open the shell,
        and verify again.
    - strategy: ask
      reason: |
        INCONCLUSIVE (verify exit 2) — `bh` is absent, or no package manager
        could be queried, so the version could not be corroborated. That is
        not a pass. Most often PATH: `uv tool` installs into ~/.local/bin.
        Ask the human before continuing.
  effect: reversible
  estimated_duration_minutes: 5
  tags: [install, mutates]
---

One command on the PyPI route, two on the managed route. Both are offered before they run —
this is the first step that changes the machine.

## `--force` is load-bearing

`uv tool install` without `--force` on a machine that already has `bh` prints
`Installed 2 executables: bh, bh-mcp`, exits `0`, and leaves the old binary in place. Measured
on macOS with 0.7.1 installed: the unforced command reported success and `bh --version` still
said `0.7.1` (`INSTALL.md:120-126`). On the managed route that produces the worst version of
the failure — a fresh, correctly pinned nix toolchain wrapped around a stale `bh`, with nothing
in the output saying so.

So `--force` on `uv` and `pipx`, `--upgrade` on `pip`. It is a step, not a suggestion.

## Verify on the version string, not the exit code

`scripts/verify-bh-version.sh` ignores the installer's exit code entirely and compares two
things that must agree:

- what `bh --version` prints, from the `bh` that is actually first on `PATH`; and
- what the package manager says it installed (`uv tool list`, `pipx list --short`,
  `pip show beadhive`, or `brew list --versions`).

A no-op install, or a stale binary shadowing the new one, makes those disagree — and that
disagreement is the only reliable signal here, because the exit code has already been measured
lying.

The script has **three** outcomes, not two:

| Exit | Meaning | `on_failure` |
|---|---|---|
| 0 | the two versions match | — |
| 1 | MISMATCH: a stale `bh` is earlier on `PATH` | `retry` once, with `--force` |
| 2 | INCONCLUSIVE: nothing to compare against | `ask` the human |

Exit 2 is deliberately not 0. An install that cannot be corroborated is not a verified install,
and quietly greening it here would reintroduce exactly the false pass this step exists to
prevent.

## PATH, the usual culprit

If the install reports success and `bh` is still not found, `uv tool` places binaries in
`~/.local/bin`. Add it to the shell profile and re-open the shell:

```sh
export PATH="$HOME/.local/bin:$PATH"
```

## `nix: command not found` is a fall-through, not a failure

If the managed command fails that way, nix was never installed (step 020 offers it and waits;
this Guide never installs it). The correct response is to return to 020's PyPI branch and
install from there — "cannot install nix" is precisely who the PyPI route is for. Do not
escalate it as an install error.
