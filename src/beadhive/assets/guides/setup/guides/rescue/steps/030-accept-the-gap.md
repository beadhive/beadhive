---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: accept-the-gap
  title: Record the absence as a decision, with its cost, and hand back
  requires: [name-the-gap]
  performer: agent
  action:
    type: prompt
    prompt: |
      Entered when 010's choice was "accept the gap and continue" — and it
      is ALSO where a gap that is not offerable to fill belongs: a missing
      agent harness, or nix. This Guide never installs either.

      Write the decision down where the caller will read it, in one line
      per item:
        <item> — absent, accepted. Costs: <the concrete thing that will not
        work>. Fix later with: <the remedy string, or the note>.

      Then say the sentence that closes the question for the calling step,
      in the caller's own terms:
        040-verify  "PyPI route: <names> are missing, which is what this
                    route buys and costs. Continuing."
        060         "No `claude` CLI, so there is no MCP to wire. OpenCode
                    is furnished at 080 by `bh hive onboard --opencode`.
                    Nothing later needs this."
        065         "The plugin is Claude Code only / was declined. Rung 1
                    is reachable without it."

      Do NOT re-offer the install. The user already answered. Asking twice
      is how an optional step becomes a nag.

      Nothing is mutated here. Accepting a gap changes the record, not the
      machine.
  verify:
    type: agent_judgment
  interactions:
    - id: confirm-accepted
      when: after
      kind: confirm
      prompt: |
        Confirm the absence and its cost as stated — this is a "yes,
        understood", not a "yes, fixed". Say no if any of it reads as a
        surprise; a cost the user did not expect is worth re-opening the
        fill offer for.
      required: true
  on_failure:
    strategy: abort
  effect: none
  terminates_at: gap-accepted
  estimated_duration_minutes: 1
  tags: [rescue, record, no-mutation]
---

The accept arm, and the one that matters most — it is the modal outcome. A machine that took
the PyPI route and does not run Claude Code reaches this step three or four times in one run,
and every one of them is the setup working as designed.

## Accepting scores 1.0

`gap-accepted` is a **full success** in this Guide's `end_states`, level with `gap-filled`. That
is the whole point of routing an absence here rather than terminating on it: the PyPI route
promised `bh` alone and delivered `bh` alone, and a run that scores that as a failure is
scoring the user's own choice against them.

`effect: none` says the same thing structurally. This step is a coordination act — it changes
what is recorded, not what is installed.

## Write it down in the caller's terms

`resume_after_recovery: true` means control returns to the step that tripped, and that step is
about to re-read the same machine and see the same absence. What has changed is that the
absence is now *decided*. So the record has to be legible to the step re-entering: name the
item, name the cost, name the fix for later, and then say the one sentence that lets that step
close its own question.

That is also why the remedy string is preserved rather than dropped. "Fix later with
`bh dep install dolt`" is the difference between an accepted gap and a forgotten one.

## Do not ask twice

The user declined at 010, or the item was never offerable in the first place. Re-offering it
here — or at the caller, on resume — turns an optional step into a nag and teaches the user to
stop reading the prompts. The `confirm-accepted` interaction is a comprehension check on the
*cost*, not a second chance to sell the install; the only thing a "no" reopens is the
explanation.

## Where the not-offerable gaps land

Two absences arrive here without ever passing through 010's fork as a real choice:

- **nix**, which the setup Guide refuses to install on the user's behalf (its Decision 3) — the
  managed route is offered, the installer command is shown, and a decline falls through to
  PyPI. That is an accepted gap by construction.
- **an agent harness.** Installing Claude Code so that a Claude-only step can pass is not a
  trade to put in front of anyone, and `bh` needs no harness to work.

## This step aborts, and that is correct

If the user cannot be given a truthful account of what the absence costs, there is nothing to
accept. `abort` there is honest; a recorded "accepted" that the user never actually understood
is worse than a stuck run, because it is invisible.
