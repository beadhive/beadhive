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
        bh setup check --json

      It probes the four tools bh drives (bd, dolt, gh, git-workspace) —
      plus a container-runtime row when `dolt.backend` selects one — and
      caches the result in ~/.beadhive/setup-state.json. `bh setup show`
      re-reads that cache without re-probing.

      --json puts ONE JSON document on stdout and nothing else. Read these
      fields; do not parse the table the bare command renders:

        schema_version  1, alongside command: "setup check". Check the pair
                        before reading anything else — the version is
                        per-command, and a different one is a different
                        contract.
        satisfied       true only when every probed row is present.
        tools[]         one row per tool: name, found, version, satisfied,
                        and remedy — the single command that fixes THAT
                        tool, non-null exactly when the row is unsatisfied.
        missing[]       the unsatisfied names, already collected.
        advisories[]    {id, message} notes that are NOT gates: a present
                        but outdated tool, an unreachable dolt server.
                        Relay them; never read one as a missing tool.

      EXIT CODE 1 IS NOT AN ERROR HERE. `bh setup check` exits 1 whenever
      anything is missing, with or without --json, on BOTH routes. What
      that means is decided by the route below, not by the code.

      Then read the result AGAINST THE ROUTE recorded at 020 — the same
      payload means different things on the two routes:

      managed  all four present is the route's CONTRACT. Fewer than four
               means the profile install did not take. Report exactly which
               are missing and STOP; continuing hides a broken install.
      pypi     missing tools are the EXPECTED state, not a failure. Report
               which are missing, say plainly that this is what the PyPI
               route buys and costs, and CONTINUE. `bh setup check` is
               advice on this route, not a gate.

      Report the tools by name either way, and quote `remedy` when you name
      one. "3 of 4" without naming the missing one is not a report.
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
    # `reason` is a kebab-case LABEL, matched against the runtime's
    # `step.failed.fields.reason`; the argument for each is in the body.
    - reason: managed-route-toolchain-incomplete
      strategy: abort
    - reason: pypi-route-tools-absent
      strategy: recover
      recover_with: "guide:./guides/rescue"
      resume_after_recovery: true
  effect: reversible
  terminates_at: installed-unwired
  estimated_duration_minutes: 2
  tags: [verify, route-sensitive]
---

`bh --version` was already corroborated at 030. This step is about the *other four* tools —
`bd`, `dolt`, `gh` and `git-workspace` — and the whole point is that the same payload has two
different correct readings depending on which route was taken.

## Read the payload, not the table

`bh setup check --json` emits one JSON document on stdout and nothing else: a
`{schema_version: 1, command: "setup check", …}` envelope over per-tool rows carrying `found`,
`version`, `satisfied` and `remedy`, plus `missing[]`, a run-level `remedy`, and `advisories[]`.
The human rendering is built from that same object rather than assembled separately, so the two
cannot disagree — but only one of them is a contract. Parsing the rendered table means an agent's
reading of this machine depends on a terminal width and a Rich version, which is precisely the
failure this flag exists to end.

Two fields do work that would otherwise land on this step:

- **`tools[].remedy`** is per-tool and non-null exactly when that row is unsatisfied — the one
  command that fixes *that* tool, derived from bh's own dependency table. It is what the rescue
  Guide runs, and it is why neither this step nor that one has to guess at an install command.
- **`advisories[]`** are notes, not gates: a present-but-outdated `bd`, an unreachable dolt
  server. They are payload fields rather than a stderr line, so the machine path does not lose
  them. Relay one; never count it as a missing tool.

`satisfied` is the verdict and `found` is the observation. They agree today and are not
duplicates — read `satisfied`, and this step keeps working the day a version floor becomes
blocking.

## Exit 1 is a fact, not a verdict

`bh setup check` exits 1 whenever anything is missing, on both routes, with or without `--json`.
On the managed route that exit is a stop; on PyPI it is the expected state. The exit code cannot
tell you which — only the route recorded at 020 can. An agent that branches on the exit code
alone will abort a perfectly correct PyPI install.

## The same payload, two readings

- **Managed route: all four present is the contract.** That route exists precisely because it
  installs and pins those four alongside `bh`. If `bh setup check` reports fewer, the nix
  profile install did not take — and continuing past it means every later step runs against a
  toolchain the user was told they had. `on_failure` is `abort`.
- **PyPI route: missing tools are the expected state.** That route installs `bh` alone and
  leaves the four to whatever the machine already has, which is frequently nothing. Reporting
  a gap here is the route working as designed. `bh setup check` is *advice* on this route, not
  a gate, so the outcome is: name what is missing, say what it costs, continue.

Naming the tools matters. "3 of 4" tells the user nothing they can act on; "`dolt` is missing,
which you will need before `bh hq init` — `bh dep install dolt`" does, and the last third of
that sentence is `tools[].remedy` read verbatim.

## Failure routing — two labels, two strategies

`on_failure`'s `reason` is a **kebab-case label**, not prose: the runtime matches it verbatim
against `step.failed.fields.reason`, so a clause labelled with a paragraph can never be selected
and its routing never fires. The argument for each clause belongs here, in the body.

**`managed-route-toolchain-incomplete` → `abort`.** Fewer than four tools on the managed route
means the nix profile install did not take. Every later step would run against a toolchain the
user was told they had, so this stops. There is nothing to accept — the route's contract was not
met.

**`pypi-route-tools-absent` → `recover` into `guide:./guides/rescue`,
`resume_after_recovery: true`.** Missing tools here are the expected state, and the intended
behaviour is to name them, cost them and carry on.

That intent has no direct spelling. The 0.1 strategies are `retry`, `recover`, `abort` and `ask`
— there is no `continue` — and the tempting move, `strategy: ask` with the answer written into
`reason`, does not work: **a bare `ask` with no `recover_with` resolves as abort.** The runtime
has nothing to present, so the run terminates at `@stuck` with no end state and no score. On the
PyPI route that is not an edge case; it is the modal outcome.

`recover` is what 0.1 does give you for "this is not the end of the road". The clause routes to
the sibling rescue Guide, which names the missing tools, offers `tools[].remedy` for each, and —
when the user accepts the absence, the expected answer here — reaches a `gap-accepted` end state
scored **1.0**. Control returns to this step with every absence recorded, and the reading it then
reports is the route's contract rather than a dead end.

## This step writes something

`effect` is `reversible`, not `read-only`: `bh setup check` caches its result in
`~/.beadhive/setup-state.json`. That is the only mutation, it is a cache, and `bh setup show`
re-reads it without re-probing. It is also why the probe at step 010 does *not* call this
command — 010 must not disturb the state it reports.

## Stopping here is a real finish

This step declares `terminates_at: installed-unwired`. A user who wanted a working `bh` binary
and nothing else has got exactly that, and the run is scored as a success (0.5) rather than an
abandonment. Offer the configuration block that follows; do not push past a "not now".
