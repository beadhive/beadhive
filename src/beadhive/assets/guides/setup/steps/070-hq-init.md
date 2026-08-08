---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: hq-init
  title: Stand up Factory HQ — local-only, and say what that costs
  requires: [config-init]
  performer: agent
  action:
    type: prompt
    prompt: |
      PROBE FIRST. Read preflight's `config.hq`, or run:
        bh hq status

      If it prints an HQ path (with or without a remote), HQ already
      exists — report ALREADY SATISFIED and move on. `bh hq init` is
      idempotent and re-running it is a clean no-op, but saying "already
      done" is better than running a command to find that out.

      Otherwise offer:
        bh hq init

      Then — and this is not optional narration — TELL THE USER THE COST,
      in these terms:

        HQ is LOCAL with no remote — the posture, not an omission. Costs:
        no backup, and no second machine until you wire one.

      Say it INLINE, at this step, in the same breath as the success
      message. Not as a footnote, not "see the docs". A user who does not
      know rung 1 is deliberately remote-less reads a local-only HQ as a
      broken install and goes looking for the setting they missed.

      Do NOT wire a remote here and do not offer `--create`. Wiring one is
      rung 2, it is step 090, and it is entered only on request.
  verify:
    type: agent_judgment
  interactions:
    - id: approve-hq-init
      when: before
      kind: confirm
      prompt: |
        `bh hq init` stands up Factory HQ — the store that aggregates every
        hive you onboard — under ~/.beadhive. It is local-only: no remote is
        wired and nothing is pushed anywhere. Approve?
      required: false
    - id: acknowledge-local-only
      when: after
      kind: confirm
      prompt: |
        Understood: HQ is LOCAL with no remote — the posture, not an
        omission. Costs: no backup, and no second machine until you wire
        one. Wiring one is step 090 and can wait as long as you like.
  on_failure:
    strategy: ask
  effect: reversible
  estimated_duration_minutes: 2
  tags: [configure, hq, rung-1]
---

Factory HQ is the store that aggregates every hive you onboard — the cross-hive `bh work ready`,
the fleet-wide intake inbox, the host registry. At rung 1 it lives on this machine only.

## The cost note is part of the step

The wording is not this Guide's invention and should not be paraphrased. The justfile's
`local-install` `_step5_note` already says it exactly right:

> HQ is LOCAL with no remote — the posture, not an omission. Costs: no backup, and no second
> machine until you wire one.

Three things are doing work in one sentence, which is why it survives verbatim:

- **"the posture"** — this is the designed rung-1 shape, not an incomplete install.
- **"no backup"** — the honest cost, stated before the user finds it out the hard way.
- **"no second machine until you wire one"** — names the exact thing rung 2 unlocks, so the
  user knows what to come back for.

Say it **inline, at this step**, next to the success message. A footnote is not a disclosure. A
user who does not know a local-only HQ is deliberate will read it as a bug and go hunting for
the remote setting they think they missed — and the next thing they find is `bh hq init
--create`, which is rung 2 taken by accident.

## Do not wire a remote here

Rung 2 is a separate decision with its own cost (a private repo, and a push discipline), and it
is step 090 — reachable on request, after the run has already reached its goal. Offering
`--create` at this point turns an opt-in graduation into a default, and the user has not yet
seen anything that would tell them whether they want it.

## Already done is a normal arrival state

`bh hq init` is idempotent — re-running it once the remote is wired is a documented clean no-op.
Probe anyway: reporting *already satisfied* costs a read, and running a mutation command to
discover it had nothing to do is the habit this Guide is trying not to teach.
