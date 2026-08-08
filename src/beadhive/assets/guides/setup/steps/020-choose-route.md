---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: choose-route
  title: Choose the install route — the one real fork in this Guide
  requires: [preflight]
  performer: either
  action:
    type: prompt
    prompt: |
      Resolve the install route to exactly one of `managed` or `pypi`, and
      record it — steps 030 and 040 both read it and neither re-derives it.

      Resolve it in this order, stopping at the first rule that fires:

      1. preflight `managed_route.supported == false` — the route is FORCED
         to `pypi`. Do NOT present the choice. Tell the user which route
         they are on AND read them `managed_route.blocked_reason` verbatim,
         because a forced choice with an unstated reason reads as a bug.
      2. preflight `managed_route.nix_present == false` and the user wants
         the managed route — offer the nix installer command below, explain
         what it does to the machine, and WAIT. Do not run it. If they run
         it, re-probe and continue on `managed`. If they decline, fall
         through to `pypi` and say plainly that declining costs rung 3 (the
         pinned toolchain), not rung 1 (a working loop).
      3. Otherwise present the `route` choice and take the user's answer.

      The nix installer command to OFFER, never to run:

        curl --proto '=https' --tlsv1.2 -sSf -L \
          https://install.determinate.systems/nix | sh -s -- install

      What to say about it: it needs `sudo`, installs a system daemon, and
      on macOS creates an encrypted APFS volume for /nix. It leaves
      uninstall receipts, so `/nix/nix-installer uninstall` backs the whole
      thing out if they are only evaluating.
  verify:
    type: agent_judgment
  interactions:
    - id: route
      when: before
      kind: choice
      prompt: |
        Two ways to install. They differ in ONE thing — what happens to the
        four other tools `bh` drives (`bd`, `dolt`, `gh`, `git-workspace`).
        Which do you want?
      choices:
        - "Managed — bd, dolt, gh and git-workspace installed and pinned together with bh, so `bh setup check` reports all four present. Costs: nix, which needs root, and about 130 seconds and 2-3 GB the first time."
        - "PyPI — bh only, in seconds, no toolchain. The four tools it drives stay whatever this machine already has, which may be nothing. Costs: `bh setup check` will report gaps, and that is expected rather than broken."
    - id: nix-install-handoff
      when: before
      kind: confirm
      prompt: |
        The managed route needs nix and this Guide will not install it for
        you — that needs root, and on macOS creates an encrypted APFS volume
        for /nix. Run the installer command above yourself in another
        terminal, then confirm here. Answering no is fine: this Guide falls
        through to the PyPI route, which is exactly who that route is for.
      required: false
  on_failure:
    strategy: ask
  effect: none
  estimated_duration_minutes: 3
  tags: [decide, fork]
---

This is the only real fork in the Guide. Everything after it is the same sequence, so this is
the one decision worth spending the user's attention on — and the one that is awkward to
reverse once tools are on disk.

## Present it by consequence, not by mechanism

"nix profile install" and "uv tool install" are mechanisms. A user choosing between them is
being asked to have an opinion about package managers. The consequence is the thing they can
actually judge:

- **Managed** — `bh` *and* the four tools it drives, installed and version-pinned together by
  `flake.lock`. `bh setup check` reports all four present. Costs root (once, for nix) and about
  130 seconds and 2–3 GB cold, measured on Apple Silicon, almost all of it download.
- **PyPI** — `bh` alone, from a prebuilt wheel, in seconds. The four tools it drives stay
  whatever this machine already has. `bh setup check` will report gaps, and on this route that
  is the expected reading, not a fault.

That difference is the whole of the choice. Say it that way.

## The order is read from `INSTALL.md`, not re-argued here

Managed first, PyPI a labelled fallback, Homebrew last. `INSTALL.md`'s `methods:` block is
ordered by preference and its comment block carries the entire reasoning — including why
Homebrew is last (it compiles `pydantic-core`, `cryptography` and `rpds-py` from source unless
a bottle is already published). Do not re-rank the routes here, and do not restate the argument:
a second copy is correct on the day it is written and wrong the day `INSTALL.md` changes. See
the decision record, Decision 2.

## When the choice is not the user's to make

If preflight reported `managed_route.supported == false`, **do not present the fork.** The
route is PyPI, and the user is told so together with the reason:

- **Intel macOS** — nixpkgs dropped `darwin-x86_64`. There is no managed path on that machine
  at all, so PyPI is not a downgrade, it is the only route.
- Anything that is neither macOS nor Linux — the managed path targets those two.

A forced choice presented as a choice wastes the user's time; a forced choice with the reason
withheld reads as a bug. Say which route, and say why.

## This Guide does not install nix

Decision 3 of the decision record, and it is a decision rather than an omission. Installing nix
needs root: it installs a system daemon, and on macOS creates an encrypted APFS volume for
`/nix`. That is a human step, not something an install agent performs unattended on a machine
it was invited onto to install a CLI.

So: **offer the command, explain what it does, and wait.** If the user runs it, re-run
`scripts/preflight.sh` and continue on the managed route. If they decline — or cannot, on a
corporate-managed machine that forbids a root daemon — fall through to PyPI and say what that
costs. It costs rung 3, the pinned toolchain, which `docs/ADOPTION.md` records as **orthogonal**:
takeable later, or never. It does not cost rung 1, which is where this Guide is going.

## What can go wrong

`on_failure` is `ask`, because every failure mode here is a question for the human rather than
something an agent can resolve: the user wants managed but cannot get root, or is unsure and
wants the trade-off restated. Abandoning at this point has installed nothing and written
nothing, which is the `aborted-clean` end state — a clean exit, not a broken machine.
