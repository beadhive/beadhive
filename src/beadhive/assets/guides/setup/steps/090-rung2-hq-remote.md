---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: rung2-hq-remote
  title: "Rung 2 — give HQ a remote (optional; the run already finished at 080)"
  requires: [first-hive]
  performer: either
  action:
    type: prompt
    prompt: |
      OPT-IN ONLY. The run reached its goal at 080. Do not enter this step
      unless the user asked for it, or asked for something it is the
      prerequisite of (a backup, a second repo's worth of aggregate, or a
      second machine).

      PROBE FIRST — run:
        scripts/check-hq-remote.sh

      exit 0  already on rung 2 and both halves current. Report ALREADY
              SATISFIED and stop; there is nothing to do.
      exit 3  a remote is wired but at least one half is not published.
              Skip the repo creation and go straight to `bh hq push`.
      exit 1  no remote — this is the rung-1 posture. Continue below.

      Then, in order, offering each command:

      1. The remote must be an empty PRIVATE repo. HQ carries fleet
         configuration and every bead in every hive. Either create it
         yourself under your account or org, or let bh do it:
           bh hq init --create
         `--create` makes the remote (private, empty) when it does not
         exist yet, and re-running once it is wired is a clean no-op.
         `bh hq init --dry-run` previews the pre-push backup plan with
         zero mutation, and is worth offering first.

      2. Publish BOTH halves:
           bh hq push
         It refreshes the aggregate, then pushes the git half
         (fleet.yaml / workspace.toml / hosts/) AND the Dolt half (bead
         state), reporting what moved on each. It refuses if no remote is
         configured.

      3. Verify BOTH halves:
           bh hq status
         Read the `git:` line AND the `dolt:` line. A green git half over
         an unpushed Dolt half is the failure that looks like success:
         the fleet config published, the beads did not, and a host that
         clones this HQ gets a factory with no work in it.

      Then say what it bought and what it costs.
        Buys: a durable backup of BOTH halves of HQ, a shareable
              fleet.yaml, and the one hard prerequisite for rung 4.
              (The cross-repo aggregate is the hub's — `bh sync`,
              `bh hub bd ready`, `bh hub intake` — and needs no remote.)
        Costs: a private repo, and a push discipline — `bh hq push` is not
              automatic.
  verify:
    type: script
    script: scripts/check-hq-remote.sh
    success_exit: 0
    output_schema: text
  interactions:
    - id: want-rung-2
      when: before
      kind: confirm
      prompt: |
        Optional, and beyond where this run already finished. Rung 2 gives
        Factory HQ a remote: a durable backup of your fleet config AND your
        bead state, the cross-repo aggregate view, and the prerequisite for
        adding a second machine. It costs one empty private repo and a push
        habit — `bh hq push` is not automatic. Climb to rung 2 now?
      required: false
    - id: remote-is-private
      when: before
      kind: confirm
      prompt: |
        Confirm the HQ remote is a PRIVATE repo. It will hold your fleet
        configuration and every bead in every hive you have onboarded.
        There is no per-hive visibility filter on the way out.
  on_failure:
    # `reason` is a kebab-case LABEL, matched against the runtime's
    # `step.failed.fields.reason`; the argument for each is in the body.
    - reason: half-unpublished
      strategy: retry
      max_retries: 1
    - reason: remote-unconfirmed
      strategy: ask
  effect: reversible
  terminates_at: rung-1-reached
  estimated_duration_minutes: 8
  tags: [rung-transition, optional, hq]
---

`docs/ADOPTION.md`'s rung 2: **HQ has a remote.** The graduation step, and the one most people
reach for the day HQ becomes worth keeping.

## This is past the finish line

The run reached `rung-1-reached` at step 080 and is complete. This step, `091` and `092` are
rung *transitions* — entered on request, never offered as the obvious next thing. A user who
wanted a working loop already has one and has missed nothing.

That is also why this step still declares `terminates_at: rung-1-reached`: the Guide's scored
goal is rung 1, and there is deliberately no higher-scoring end state for climbing. Climbing is
optional, so declining must not score lower.

## Why a failed climb scores zero even though rung 1 already stands

There is a real asymmetry here and it is deliberate. **Declining** rung 2 ends the walk at 080's
`rung-1-reached` and scores **1.0**. **Succeeding** at it also scores 1.0. But opting in and
hitting `remote-unconfirmed` routes to `@stuck` and scores **0**, even though nothing that was
working at 080 has been broken.

That is right, because the end-state score is not a report card on how much the user got — it is
the answer to *"is this run finished, or does it need a human?"* A user who declined has nothing
outstanding. A user whose `bh hq push` did not land **asked for a durable backup of every bead in
every hive and does not have one**, and the worst outcome available is a Guide that reports 1.0
and lets them believe otherwise. `@stuck` is how a run says "come and look at this", and this is
exactly a run someone should look at.

Check the incentive it creates, because that is the test this Guide applies elsewhere
(`installed-unwired` scores 0.5 so the Guide never pushes past a "not now"; the rescue Guide's
`gap-accepted` scores 1.0 so it never pushes an install on someone who declined). Here, both
declining *and* succeeding score 1.0, so the only route to 0 is opting in and hitting a genuine
fault. The pressure that creates is to make the rung work — not to stop offering it, and not to
paper over the failure. That is the pressure you want.

It is worth being straight that 0.1 could not express the alternative anyway: a failure clause
chooses among `retry` / `recover` / `abort` / `ask`, and `terminates_at` only applies on verify
*success*, so there is no way to say "fail this step but keep the end state the run already
earned". This is a position, not a workaround for that gap — but the gap is real, and if a later
schema version adds that spelling, this is the paragraph to revisit.

The contrast that proves the line is 091's `nix-absent`, one step over, which *does* recover:
nothing there is broken and nothing needs a human. nix is absent because the user declined to
install it, which the ADR's Decision 3 says is a fine answer. An absence is not a fault.

## Check both halves, because only one of them fails quietly

HQ has two halves and they publish independently:

- the **git half** — `fleet.yaml`, `workspace.toml`, `hosts/`;
- the **Dolt half** — the bead state itself.

A green git half over an unpushed Dolt half is the failure that looks like success. `bh hq
push` reports what moved on each, and `bh hq status` prints ahead/behind for both — which is
why `scripts/check-hq-remote.sh` extracts the `git:` line *and* the `dolt:` line and echoes
both, rather than trusting a single summary marker. A host that clones a half-published HQ gets
the fleet configuration and none of the work, and discovers it much later.

The script's exit codes map onto the three states that need different handling:

| Exit | State | Response |
|---|---|---|
| 0 | remote wired, both halves current | already satisfied |
| 1 | no remote — the rung-1 posture | create the repo, `bh hq init --create` |
| 3 | wired, at least one half behind | `bh hq push`, then re-verify |
| 2 | `bh hq status` unreadable | ask; HQ may not be initialised |

## Failure routing — both clauses stay stops, and that is on purpose

`on_failure`'s `reason` is a **kebab-case label**: the runtime matches it verbatim against
`step.failed.fields.reason`, so a clause labelled with a paragraph can never be selected and its
declared routing never fires. The argument goes here.

- **`half-unpublished` (probe exit 3) → `retry`, `max_retries: 1`.** A remote is wired but at
  least one half is ahead or behind. Run `bh hq push` and re-verify. Do not read a wired remote
  as a published one.
- **`remote-unconfirmed` (probe exit 1, or `bh hq status` inconclusive at exit 2) → `ask`.** Rung
  2 did not happen. Most often the remote repo does not exist yet, or `gh` is not authenticated
  for the org. Rung 1 is unaffected and the machine still works.

Neither of these is the "expected absence" that 040, 060 and 065 route into the rescue Guide,
and neither is written as an `ask` whose intended answer is *continue*. The user **opted in** to
rung 2 and it did not happen — that is a fault with a fixable cause, not a posture anyone chose.
`ask` is the right strategy precisely because the answer it CAN offer is the answer this clause
wants: fix the cause and retry, or stop. That is the line — an `ask` is honest when retry-or-stop
is the real choice, and a lie when the real answer is "carry on".

## Private, and say why

The remote holds fleet configuration and every bead in every onboarded hive. There is no
per-hive visibility filter on the way out, so "private" is not a default to be quietly assumed —
it is confirmed with the user before anything is pushed.

## What it buys, what it costs

- **Buys.** A durable backup of both halves of HQ; a shareable `fleet.yaml`; and the one hard
  prerequisite for rung 4. (The cross-repo aggregate is the [hub](../../../../../docs/HUB.md)'s
  — `bh sync`, `bh hub bd ready`, `bh hub intake` — and needs no remote at all.)
- **Costs.** A private repo, and a push discipline — `bh hq push` is not automatic.

Related and worth naming if the user asks: `bh backup usage` reports what the backup roots are
consuming, and `bh hq restore --list` shows what a pre-push snapshot can recover.
