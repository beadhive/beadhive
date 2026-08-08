---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/guide.schema.json
guide:
  id: beadhive-setup-rescue
  version: "0.1.0"
  summary: |
    The setup Guide's sibling rescue. One failure family only: a step needed
    something this machine does not have — a tool the PyPI route never
    installs, a `claude` CLI on a machine that runs another harness — and
    the step cannot decide on its own whether that absence should be filled
    or accepted. This Guide decides it: name the gap from machine-readable
    state, then fill it or accept it, and say what the choice costs.
  goal_state: |
    The absence the calling step tripped on is RESOLVED — either the missing
    thing is installed and the step's own probe will now pass, or the
    absence is recorded as deliberate, with its cost stated and nothing
    downstream silently depending on it. Either way the calling step can be
    re-entered and answered rather than left hanging.
  prerequisites:
    - id: called-with-a-named-gap
      performer: agent
      description: >
        The caller knows WHAT is absent and WHICH step asked — a `bh setup
        check --json` payload, a probe exit code, or preflight's `harness`.
        This Guide reads state; it does not re-probe the machine, because
        two probe implementations is how a Guide and `bh` start disagreeing
        about the same machine.
    - id: interactive-operator
      performer: human
      description: >
        Filling a gap installs software, so a human approves it. Accepting a
        gap is equally a decision the user makes, not one inferred from
        silence.
  external_resources:
    - title: "The Guide this one rescues"
      path: "../../GUIDE.md"
    - title: "INSTALL.md — what each install route does and does not include"
      url: https://github.com/beadhive/beadhive/blob/main/INSTALL.md
    - title: "ADOPTION.md — which rung an accepted gap actually costs"
      url: https://github.com/beadhive/beadhive/blob/main/docs/ADOPTION.md
  rollback_strategy: none
  end_states:
    - id: gap-filled
      description: >
        The missing thing was installed or wired, and the calling step's own
        probe will now pass on its own terms. Nothing was accepted on the
        user's behalf.
      score: 1.0
    - id: gap-accepted
      description: >
        The absence stands and is now a RECORDED decision rather than an
        open failure: named, costed, and confirmed by the user. A full
        success — this is the outcome the PyPI route and every non-Claude
        harness are supposed to reach.
      score: 1.0
    - id: gap-unresolved
      description: >
        Neither filled nor accepted — the user stopped, or the gap could not
        be named from the state on hand. The calling run is reported as
        stuck rather than resumed.
      score: 0.0
  estimated_duration_minutes: 4
  tags: [setup, rescue, recovery, beadhive]
---

# Beadhive setup — rescue

## When this Guide runs

The setup Guide calls it. Several of its steps can fail for a reason that is not a fault:

- **040-verify** on the PyPI route — `bh setup check` reports missing tools because that route
  installs `bh` alone, by design.
- **060-mcp-wiring** on a machine with no `claude` CLI — there is no MCP to wire.
- **065-plugin** off Claude Code, or when the user simply declines it.

Each of those is an *absence*, and an absence has two correct resolutions — fill it or accept
it — which is exactly one more than the calling step can choose between on its own. So the step
routes here (`on_failure.recover_with`) instead of terminating.

## Why this exists as a Guide and not as a strategy

The agentguides 0.1 failure strategies are `retry`, `recover`, `abort` and `ask`. There is no
`continue` and no `skip`: a bare `ask` with no `recover_with` **resolves as abort**, so writing
"the answer is continue" into a clause's `reason` does not make the runtime able to continue —
it dead-ends the run at `@stuck` with no end state and no score.

`recover` is the strategy 0.1 does give you for "this is not the end of the road", and a
recovery has to be a Guide. So "continue past an absence" is spelled here, as a rescue whose
**`gap-accepted` end state is a full 1.0 success**, with `resume_after_recovery: true` sending
the caller back into the step it tripped on. That is not a workaround; it is where 0.1 puts
this decision, and it buys something a `continue` strategy would not: the absence gets named,
costed and recorded instead of stepped over in silence.

## Decision criteria during execution

- **Read state, do not re-probe.** The caller already ran the probe. `bh setup check --json`
  hands over `missing[]` and a per-tool `remedy`; a probe exit code says which of three answers
  came back. Re-running a different probe here is how two implementations start disagreeing.
- **Accepting is a real success, not a consolation.** `gap-accepted` scores 1.0 alongside
  `gap-filled`. A PyPI-route machine with no `dolt` and no `claude` has exactly the setup that
  route promised.
- **Never fill a gap this Guide family refuses to fill.** nix (the setup Guide's Decision 3)
  and a second agent harness are always *accepted*, never installed. Offering to install
  Claude Code so that an optional Claude-only step can pass is not a trade to put in front of
  anyone.
- **State the cost in rungs, not in adjectives.** "You will not be able to run `bh hq init`
  until `dolt` is here" is actionable; "some features may be limited" is not.

## Structure

`steps/` is walked in order and forks once, after the gap is named: `020-fill-the-gap` and
`030-accept-the-gap` are the two arms, exactly one of which runs. Both are leaves and both
declare `terminates_at`, so neither can pick up an end state by accident.
