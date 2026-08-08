---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: verify
  title: Read the toolchain state with bh setup check — and read it BY ROUTE
  requires: [install-bh]
  performer: agent
  action:
    type: prompt
    prompt: |
      Run:
        bh setup check

      It probes the four tools bh drives (bd, dolt, gh, git-workspace) and
      caches the result in ~/.beadhive/setup-state.json. `bh setup show`
      re-reads that cache without re-probing.

      Then read the result AGAINST THE ROUTE recorded at 020 — the same
      output means different things on the two routes:

      managed  all four present is the route's CONTRACT. Fewer than four
               means the profile install did not take. Report exactly which
               are missing and STOP; continuing hides a broken install.
      pypi     missing tools are the EXPECTED state, not a failure. Report
               which are missing, say plainly that this is what the PyPI
               route buys and costs, and CONTINUE. `bh setup check` is
               advice on this route, not a gate.

      Report the tools by name either way. "3 of 4" without naming the
      missing one is not a report.

      There is no --json on `bh setup check` at this version; read the
      rendered output. Machine-readable state is filed as bh-0olv9.2, and
      when it lands this step should consume it instead of parsing text.
  verify:
    type: agent_judgment
  interactions:
    - id: route-reading
      when: after
      kind: confirm
      prompt: |
        Confirm the reading: on the managed route, all four tools present;
        on the PyPI route, the missing tools named and accepted as expected.
        On PyPI this is a "yes, understood", not a "yes, fixed".
  on_failure:
    - strategy: abort
      reason: |
        MANAGED ROUTE — `bh setup check` reported fewer than all four tools.
        All four IS this route's contract; anything less means the nix
        profile install did not take, and every later step would be running
        against a toolchain the user was told they had. Stop here and fix
        the install rather than continuing past it.
    - strategy: ask
      reason: |
        PYPI ROUTE — tools are missing. This is the expected state and NOT a
        failure of this step; the intended outcome is to report them and
        carry on. It is an `ask` only because the 0.1 step schema has no
        `continue` strategy (retry / recover / abort / ask); the answer this
        clause exists to take is "continue", and it is the default.
  effect: reversible
  terminates_at: installed-unwired
  estimated_duration_minutes: 2
  tags: [verify, route-sensitive]
---

`bh --version` was already corroborated at 030. This step is about the *other four* tools —
`bd`, `dolt`, `gh` and `git-workspace` — and the whole point is that the same output has two
different correct readings depending on which route was taken.

## The same output, two readings

- **Managed route: all four present is the contract.** That route exists precisely because it
  installs and pins those four alongside `bh`. If `bh setup check` reports fewer, the nix
  profile install did not take — and continuing past it means every later step runs against a
  toolchain the user was told they had. `on_failure` is `abort`.
- **PyPI route: missing tools are the expected state.** That route installs `bh` alone and
  leaves the four to whatever the machine already has, which is frequently nothing. Reporting
  a gap here is the route working as designed. `bh setup check` is *advice* on this route, not
  a gate, so the outcome is: name what is missing, say what it costs, continue.

Naming the tools matters. "3 of 4" tells the user nothing they can act on; "`dolt` is missing,
which you will need before `bh hq init`" does.

## Why the PyPI clause is `ask` and not `continue`

Because there is no `continue`. The agentguides 0.1 step schema's failure strategies are
`retry`, `recover`, `abort` and `ask` — so the PyPI route's intended behaviour (report and
carry on) has no direct spelling. It is written as the second labelled clause, whose stated
answer is *continue* and whose reason says so, so the difference between the two routes is
still recorded in the step rather than living in an agent's judgment. The managed clause is a
real `abort`; only the PyPI one is a schema workaround.

## This step writes something

`effect` is `reversible`, not `read-only`: `bh setup check` caches its result in
`~/.beadhive/setup-state.json`. That is the only mutation, it is a cache, and `bh setup show`
re-reads it without re-probing. It is also why the probe at step 010 does *not* call this
command — 010 must not disturb the state it reports.

## Stopping here is a real finish

This step declares `terminates_at: installed-unwired`. A user who wanted a working `bh` binary
and nothing else has got exactly that, and the run is scored as a success (0.5) rather than an
abandonment. Offer the configuration block that follows; do not push past a "not now".
