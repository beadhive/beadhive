---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: name-the-gap
  title: Name what is absent, which step asked for it, and what it costs
  requires: []
  performer: agent
  action:
    type: prompt
    prompt: |
      READ-ONLY. Do not probe again — the caller already did, and two probe
      implementations is how a Guide and `bh` start disagreeing about the
      same machine. Read what you were handed:

      from 040-verify   the `bh setup check --json` payload. `missing[]` is
                        the list of names; each `tools[]` row carries the
                        `remedy` for THAT tool, non-null exactly when the
                        row is unsatisfied. That remedy is the fill command
                        — do not invent one.
      from 060/065      the probe's exit code (3 = no `claude` CLI) and
                        preflight's `harness`. The absent thing is the
                        harness feature, and there is no remedy field
                        because bh does not install agent harnesses.

      Then answer three questions, out loud, by name:

      1. WHAT is absent. Name every item. "3 of 4" is not a report.
      2. WHY it is absent — which install route or which harness leads
         here. On the PyPI route and off Claude Code the answer is "this is
         what you chose", not "something broke".
      3. WHAT IT COSTS, in rungs (docs/ADOPTION.md), not in adjectives:
         which later command stops working, and which do not. `dolt`
         missing blocks `bh hq init`; MCP unwired costs a convenience and
         no capability at all.

      Finish by stating whether a fill is even OFFERABLE. It is not for a
      missing agent harness and not for nix — those are accepted, never
      installed.
  verify:
    type: agent_judgment
  interactions:
    - id: fill-or-accept
      when: after
      kind: choice
      choices: ["fill the gap now", "accept the gap and continue"]
      prompt: |
        You have been told exactly what is missing, why, and what it costs.
        Install it now, or accept the absence and carry on? Accepting is a
        complete, successful answer — on the PyPI route and on a machine
        that does not run Claude Code it is usually the right one.
      required: true
  on_failure:
    strategy: abort
  effect: read-only
  estimated_duration_minutes: 2
  tags: [rescue, read-only, diagnose]
---

A rescue that opens by re-running the probe has already lost the plot: the caller ran it, its
result is the reason this Guide was entered, and a second probe is a second opinion nobody
asked for. This step reads state and turns it into a decision the user can actually make.

## The remedy is a field, not a guess

`bh setup check --json` computes a **per-tool** remedy — the single command that fixes that one
tool — and hands it over in `tools[].remedy`, non-null exactly when the row is unsatisfied. It
is derived from bh's own dependency table, so it already knows the difference between a tool
`bh dep install` can fetch, a tool only the pinned toolchain supplies, and a tool bh knows
about but will not drive. Read it. A remedy typed here would be a fourth opinion about
something bh already answers.

There is deliberately **no** remedy for a missing agent harness. bh does not install harnesses,
and this Guide will not offer to.

## Cost in rungs, not in adjectives

The user is deciding whether to spend the next ten minutes installing something. "Some features
may be limited" does not help them; "`dolt` is missing, so `bh hq init` at step 070 will not
run — everything up to it will" does. [ADOPTION.md](https://github.com/beadhive/beadhive/blob/main/docs/ADOPTION.md)
is the shared vocabulary for that, and the setup Guide's end states are already scored against
it.

Two costs worth naming precisely because they are smaller than they look:

- **MCP unwired** costs a convenience, not a capability. Every `bh` verb works from a shell.
- **No harness plugin** costs the seat definitions being to hand; the role skills are readable
  straight out of the repo.

## Why the fork is an interaction and not an inference

`fill-or-accept` is a `kind: choice` with `required: true`. Neither arm is inferable: a user on
the PyPI route may well want `dolt` after all, and a user on the managed route may be perfectly
happy to leave `gh` out. Silence is not consent to install software, and it is not consent to
give up on it either.

## This step aborts, and that is correct

`on_failure` is a plain `abort`. If the gap cannot be named from the state on hand, the rescue
has nothing to offer — and per the spec a failed recovery run returns its caller to `@stuck`
rather than resuming it. That is the honest outcome: an unnamed absence is not an accepted one.
