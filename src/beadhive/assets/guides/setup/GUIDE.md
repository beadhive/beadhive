---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/guide.schema.json
guide:
  id: beadhive-setup
  version: "0.1.0"
  summary: |
    Set up Beadhive on this machine, step by step, without the user reading
    the docs: probe what is already here, pick ONE install route (the managed
    nix path if nix exists, PyPI otherwise), install and verify `bh`, then
    configure it — config, MCP wiring, the harness plugin, Factory HQ and a
    first hive — until `bh work ready` answers. Every command is offered and
    explained before it runs.
  goal_state: |
    `bh --version` prints the released version, `bh setup check` reports the
    machine's toolchain state, `~/.beadhive` is scaffolded, the bh MCP server
    is registered with the user's harness, Factory HQ is initialised, one repo
    is onboarded as a hive, and `bh work ready` answers from that hive — i.e.
    rung 1 of docs/ADOPTION.md, reached with the user having read no prose.
  prerequisites:
    - id: interactive-operator
      performer: human
      description: >
        A human is present and will approve each command. This Guide never
        runs an install or a configuration command unasked, and it stops
        rather than guessing when a choice is the user's to make.
    - id: posix-shell-and-network
      performer: agent
      description: >
        A POSIX shell on macOS or Linux, with outbound HTTPS to PyPI and
        GitHub. Every install route downloads; none works offline.
    - id: root-for-nix-if-managed
      performer: human
      description: >
        ONLY for the managed route, and deliberately not the agent's to
        satisfy: installing nix needs root (a system daemon, plus an
        encrypted APFS volume on macOS). The Guide offers the installer
        command and waits. Declining is fine and costs only rung 3 — see
        docs/design/setup-guide-adr.md, Decision 3.
  external_resources:
    - title: "Decision record: where this Guide lives, what it refuses to do, and the order it teaches"
      url: https://github.com/beadhive/beadhive/blob/main/docs/design/setup-guide-adr.md
    - title: "INSTALL.md — the route axis, and the source of this Guide's method ordering"
      url: https://github.com/beadhive/beadhive/blob/main/INSTALL.md
    - title: "ADOPTION.md — the four rungs this Guide's end states are scored against"
      url: https://github.com/beadhive/beadhive/blob/main/docs/ADOPTION.md
    - title: "ONBOARDING.md — where to go next, by starting situation"
      url: https://github.com/beadhive/beadhive/blob/main/docs/ONBOARDING.md
  rollback_strategy: none
  end_states:
    - id: rung-1-reached
      description: >
        `bh` installed and verified, `~/.beadhive` scaffolded, MCP wired,
        Factory HQ initialised, one hive onboarded, and `bh work ready`
        answers. Rung 1 of docs/ADOPTION.md.
      score: 1.0
    - id: installed-unwired
      description: >
        `bh` is on PATH and `bh --version` verifies, but configuration was
        not started — the user stopped after the install, or deferred it.
        A SUCCESSFUL exit: a working binary is a real outcome, and pushing
        past a "not now" is a worse one.
      score: 0.5
    - id: aborted-clean
      description: >
        The run was abandoned before anything was installed, and nothing was
        half-written — no partial `~/.beadhive`, no orphaned HQ. The machine
        is as it was found.
      score: 0.2
  estimated_duration_minutes: 25
  tags: [setup, install, onboarding, beadhive]
  requires:
    tools:
      - "git@>=2"
---

# Beadhive setup

## When to use this Guide

Someone wants Beadhive working on this machine and would rather be walked through it than
read [INSTALL.md](https://github.com/beadhive/beadhive/blob/main/INSTALL.md). One run sets up
ONE machine: it probes what is already there, installs `bh` by one route, and configures it as
far as the user wants to go.

It is equally the right Guide for a **partly set-up** machine. `INSTALL.md`'s `configure[]`
block may already have run `bh config init`, `bh mcp install` and the plugin install before
this Guide starts, so "already done" is the normal case, not an error — every step probes
first and reports *already satisfied* rather than failing or redoing.

## When NOT to use this Guide

- **Upgrading an existing install** — that is
  [UPGRADING.md](https://github.com/beadhive/beadhive/blob/main/docs/UPGRADING.md), which
  covers route migrations this Guide does not.
- **Onboarding another repo into a working setup** — that is one command,
  `bh hive onboard <provider>/<org>/<repo>`. Only run this Guide for the *first* one.
- **Climbing past rung 1 on a machine that already has it** — the rung transitions have their
  own steps here (090+), but if `bh work ready` already answers, jump to those rather than
  walking from the top.
- **Docker** — `bh` in a container has no host `bh`, so verification and every configuration
  step below assume something a Docker user does not have. `INSTALL.md`'s prose covers it;
  this Guide does not.

## Decision criteria during execution

- **There is exactly ONE real fork: the install route.** Managed (nix) or PyPI. Everything
  after it is the same sequence.
- **The method order is [INSTALL.md](https://github.com/beadhive/beadhive/blob/main/INSTALL.md)'s
  and is read from it, not re-argued here** — managed path first, PyPI a labelled fallback,
  Homebrew last. That file's `methods:` comment block carries the whole reasoning, including
  why Homebrew is last. Do not re-rank the routes inside this Guide; see the decision record,
  Decision 2.
- **This Guide does not install nix.** It offers the installer command, explains what it does
  to the machine, and waits for a human. If nix is absent and the user declines, the correct
  outcome is to fall through to the PyPI route — which is exactly who that route is for.
  Decision 3 of the decision record.
- **Probe before every action.** A step that treats an already-completed state as an error
  fails for the majority of its users.
- **Rung 1 is the goal, and stopping short of it is allowed.** Per
  [ADOPTION.md](https://github.com/beadhive/beadhive/blob/main/docs/ADOPTION.md), rung 3 (the
  managed toolchain) is orthogonal and rung 4 requires rung 2; none of them is on the path to
  a working loop. Offer, never insist.

## What you need at hand

- A terminal on macOS (Apple Silicon) or Linux, and the ability to approve commands.
- Roughly 25 minutes, most of it waiting on downloads.
- For a hive: one git repo you want to drive with `bh`, already cloned or clonable.
- For the managed route only: someone who can type a `sudo` password.

## Structure

`steps/` is walked in order. The numbering leaves gaps so steps can be inserted without
renumbering, and the boundary that matters is after the configuration block: everything from
`090` on is a **rung transition**, entered only on request. A run that stops at the boundary
has reached `rung-1-reached` and is finished.

Step content is authored per-step and lives entirely in `steps/`; this file carries only the
envelope — goal, prerequisites, the fork, and how a run is scored.

## Degradation

This Guide is linear apart from the route fork in the install block, so a harness that cannot
walk a Guide loses branching and nothing else: read this file for framing, then read `steps/`
in order and apply the route choice by hand. See `SKILL.md`.
