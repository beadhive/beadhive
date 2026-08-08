---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: config-init
  title: Scaffold ~/.beadhive with bh config init
  requires: [verify]
  performer: agent
  action:
    type: prompt
    prompt: |
      PROBE FIRST. Read preflight's `config.config_yaml`, or run:
        scripts/check-config-scaffold.sh

      Exit 0 means ~/.beadhive/config.yaml already exists — report ALREADY
      SATISFIED and move on. Do not re-run, do not treat it as an error.
      This is the NORMAL case: INSTALL.md's configure[] block runs
      `bh config init` before this Guide is ever invoked, so most users
      arrive here already done.

      Only if the probe reports ABSENT or PARTIAL, offer:
        bh config init

      It scaffolds ~/.beadhive (config.yaml plus templates) and is
      idempotent, so it also completes a half-written scaffold.

      Say what it wrote afterwards — `bh config path` prints where the
      config it will use actually lives, which is the one thing a user needs
      if they later want to edit it by hand.
  verify:
    type: script
    script: scripts/check-config-scaffold.sh
    success_exit: 0
    output_schema: text
  interactions:
    - id: approve-config-init
      when: before
      kind: confirm
      prompt: |
        `bh config init` creates ~/.beadhive and writes config.yaml plus
        templates there. It touches nothing outside that directory and no
        repo. Approve?
      required: false
  on_failure:
    strategy: ask
  effect: reversible
  estimated_duration_minutes: 1
  tags: [configure, idempotent]
---

Everything after this reads `~/.beadhive/config.yaml`, so this is the step that turns an
installed binary into a configured one. It is also the step most likely to have already run.

## Already done is the normal case

`INSTALL.md`'s `configure[]` block is three entries, and the first is `bh config init`. A
conforming installer runs it *before* this Guide starts. So the common path through this step
is: probe, find `~/.beadhive/config.yaml`, report **already satisfied**, continue.

A step that treats a completed state as an error fails for the majority of its users. Probe
first, and say "already done" out loud — a silent skip leaves the user unsure whether anything
happened.

## The probe is also the verify

`scripts/check-config-scaffold.sh` is used twice on purpose: once before acting, to decide
whether to act at all, and once after, as this step's verify. One implementation means the
before-state and the after-state are judged by the same rule, so "already satisfied" and
"successfully scaffolded" cannot diverge.

It distinguishes three states, because they need different responses:

| State | Meaning | Response |
|---|---|---|
| OK | `config.yaml` present and non-empty | already satisfied — skip |
| PARTIAL | the directory exists, `config.yaml` does not | run `bh config init`; it is idempotent |
| EMPTY | `config.yaml` exists with no content | a half-write, not a scaffold — re-run |

It honours `BH_HOME`, so a relocated root (containers, test rigs) is not reported as a missing
one.

## What it does not do

`bh config init` scaffolds and nothing more. It does not wire MCP, initialise HQ, or onboard a
repo — those are the next three steps, and each is separately declinable. Nothing here touches
a git repository.
