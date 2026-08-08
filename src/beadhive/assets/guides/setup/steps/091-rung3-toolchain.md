---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: rung3-toolchain
  title: "Rung 3 — the managed toolchain (optional, orthogonal, needs nix)"
  requires: [first-hive]
  performer: either
  action:
    type: prompt
    prompt: |
      OPT-IN ONLY, and ORTHOGONAL: rung 3 is about tool INTEGRITY, not
      reach. It can be taken before rung 2, after rung 4, or never. Do not
      present it as the next rung after 090.

      GATE FIRST, on preflight's `managed_route.nix_present`:

      nix ABSENT — REFUSE, explain, and STOP. Do not attempt to install
        nix, and do not install it under sudo on the user's behalf even if
        they offer. It needs root: a system daemon, and on macOS an
        encrypted APFS volume for /nix. That is a human step (ADR Decision
        3). What to do instead: offer the installer command exactly as
        step 020 does, say what it will do to the machine, and stop. If
        and when the human has run it, re-run scripts/preflight.sh and
        re-enter this step.

        Also say what NOT taking it costs, because it is less than it
        sounds: rung 3 is orthogonal. Rung 1 works, rung 2 works, and the
        four tools bh drives stay whatever this machine already has.

      nix ABSENT AND `managed_route.supported` is false — refuse HARDER,
        and permanently. On Intel macOS there is no managed path at all;
        nixpkgs dropped darwin-x86_64. Installing nix would not help. Say
        so plainly rather than leaving the user to discover it.

      nix PRESENT — offer, in order:
        bh setup toolchain     # bd, dolt, gh, git-workspace from the
                               # pinned flake; no checkout, no flake ref
        bh --version           # after ANY reinstall: must print the
                               # released version, not merely exit 0
        bh setup check         # all four present — that is the whole point

      Report the four tools by name. If `bh setup check` still shows a
      gap, the profile install did not take: report which tool and stop,
      rather than declaring the rung reached.
  verify:
    type: agent_judgment
  interactions:
    - id: want-rung-3
      when: before
      kind: confirm
      prompt: |
        Optional, and independent of every other rung. Rung 3 installs the
        four tools bh drives — bd, dolt, gh, git-workspace — and PINS them
        together from one flake.lock, instead of leaving them as whatever
        this machine happens to have. It needs nix, which needs root, and
        costs roughly 130 seconds and 2-3 GB cold. Take it now?
      required: false
    - id: nix-required
      when: on_failure
      kind: confirm
      prompt: |
        This rung needs nix and this Guide will not install it for you —
        it needs root, and on macOS creates an encrypted APFS volume. Run
        the installer yourself if you want it, then come back. Declining
        is fine: rung 3 is orthogonal, and rungs 1, 2 and 4 do not depend
        on it.
      required: false
  on_failure:
    - strategy: abort
      reason: |
        NIX ABSENT — this rung is unavailable and this Guide does not
        install nix (ADR Decision 3: root, a system daemon, and an APFS
        volume on macOS are a human's to authorise, not an install agent's).
        Abort THIS STEP, not the run: rung 3 is orthogonal, so nothing else
        the user has is diminished by stopping here.
    - strategy: ask
      reason: |
        NO MANAGED PATH ON THIS MACHINE — Intel macOS, which nixpkgs
        dropped. Installing nix would not unlock it. Say so and close the
        subject rather than leaving a rung the user cannot reach on the
        table.
    - strategy: ask
      reason: |
        TOOLCHAIN INSTALLED BUT `bh setup check` STILL SHOWS A GAP. The
        profile install did not take. Name the missing tool and ask; do not
        report the rung as reached.
  effect: reversible
  terminates_at: rung-1-reached
  estimated_duration_minutes: 10
  tags: [rung-transition, optional, toolchain]
---

`docs/ADOPTION.md`'s rung 3: **the managed toolchain.** `bh` drives four other tools — `bd`,
`dolt`, `gh` and `git-workspace` — and this is the rung where they are installed and
version-pinned together by `flake.lock` rather than being whatever the machine happened to have.

## Orthogonal, and that word is doing real work

Rung 3 is not "after rung 2". It is not about reach at all; it is about tool *integrity*. Take
it before rung 2, after rung 4, or never. Presenting it as the next rung in a ladder is how a
user ends up doing work they did not need in order to get to the thing they actually wanted.

Because it is orthogonal, declining costs nothing structural: rung 1 works, rung 2 works, rung 4
works. The user just keeps whatever `bd` and `dolt` their machine already had.

## It refuses rather than installing nix

This is the single hardest rule in the Guide, and it is a decision rather than a limitation
(ADR Decision 3). Installing nix needs root — a system daemon, plus an encrypted APFS volume for
`/nix` on macOS. An agent that takes root once has taught the user that agents take root, and a
single yes/no is not informed consent for that.

So when nix is absent this step **refuses with an explanation** and stops. It offers the
installer command exactly as step 020 does, says what it will do, and waits. If the human runs
it, re-run `scripts/preflight.sh` and re-enter.

`on_failure`'s first clause is an `abort` of *this step*, not of the run. Nothing else the user
has is diminished by not taking an orthogonal rung.

## When nix would not help either

On Intel macOS there is no managed path at all — nixpkgs dropped `darwin-x86_64`. Installing nix
there buys nothing. Say that plainly and close the subject; leaving an unreachable rung on the
table is worse than not mentioning it.

## `bh setup toolchain` needs no checkout

The shortcut for a machine that already has `bh`: no repo clone and no flake reference to type.
It installs the four tools from the pinned flake, and then:

- `bh --version` — after **any** reinstall, this must print the released version. Exiting 0 is
  not the check (`INSTALL.md:120-126`, and step 030's whole argument).
- `bh setup check` — all four present. That is the rung, stated as a check you can run.

A remaining gap after a successful-looking install means the profile install did not take. Name
the tool and stop; do not report the rung as reached.
