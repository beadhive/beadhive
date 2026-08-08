---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: fill-the-gap
  title: Run the remedy bh already computed — one item at a time
  requires: [name-the-gap]
  performer: agent
  action:
    type: prompt
    prompt: |
      Entered ONLY when 010's choice was "fill the gap now". If it was
      "accept", this step is skipped and 030 runs instead.

      Run the remedy from `tools[].remedy` for each missing item, ONE AT A
      TIME, and report each result before starting the next. Do not batch
      them: a remedy that fails is a different conversation from the one
      that follows it, and a batched failure hides which item failed.

      Never substitute a command of your own. If a row's remedy is a NOTE
      rather than a command ("bh knows about this tool but will not install
      it"), the note is the whole remedy — relay it and move on to the next
      item. That is not a failure of this step.

      Re-read the state when every item has been attempted:
        bh setup check --json
      and report the new `missing[]` against the old one. Fewer missing is
      progress; report it as such even if the list is not yet empty.

      NEVER fill by installing nix or an agent harness. If the gap is one
      of those, 010 already said a fill was not offerable and this step
      should not have been entered — go to 030 and accept it.
  verify:
    type: agent_judgment
  interactions:
    - id: approve-remedies
      when: before
      kind: confirm
      prompt: |
        These are the remedy commands `bh setup check` computed for the
        items you asked to fill, in order. They install software on this
        machine and nothing else — no repo and no harness config is
        touched. Approve running them?
      required: true
  on_failure:
    # `reason` is a kebab-case LABEL, matched against the runtime's
    # `step.failed.fields.reason`; the argument for each is in the body.
    - reason: remedy-did-not-take
      strategy: retry
      max_retries: 1
  effect: reversible
  terminates_at: gap-filled
  estimated_duration_minutes: 5
  tags: [rescue, install, approval-gated]
---

The fill arm. Its whole job is to run commands `bh` already chose, in an order the user can
follow, and report honestly on each one.

## One at a time, because a batch hides which item failed

Missing tools are independent — `dolt` arriving has nothing to do with `gh` arriving — so there
is no reason to couple them into one approval and one outcome. Run, report, next. A user
watching four remedies go past as a single block cannot tell which of them is the one that
matters when the summary says "3 succeeded".

## A note IS a remedy

`bh setup check --json` distinguishes three kinds of remedy, and the distinction is
load-bearing: a tool `bh dep install <name>` fetches, a tool only the pinned toolchain
supplies, and a tool bh knows about but deliberately will not drive. The third kind comes back
as prose — the note *is* the answer, and relaying it is this step completing successfully, not
failing. Substituting a command bh declined to offer is how a Guide installs something the
project decided not to install for you.

## Retry once, then hand the decision back

`on_failure` is a single clause: **`remedy-did-not-take` → `retry`, `max_retries: 1`.** A
download or a transient registry failure is the common cause, so one retry earns its keep; and
*only* one, because a remedy that fails twice is a real problem the user should hear about
rather than a slow one the Guide should keep grinding at. When it is exhausted, name the item
and the error and let the user choose between fixing it by hand and accepting the gap. There is
no fallback command, because guessing at an install route is exactly the failure mode
`tools[].remedy` exists to end.

`reason` is a **kebab-case label**, not prose — including here, in the Guide that exists to fix
prose reasons. The runtime matches it verbatim against `step.failed.fields.reason`
(`walk_path`), so a paragraph is unselectable; with no `default` clause to fall back to, the
failure is recorded as an *unknown* segment and the `max_retries: 1` written above is never
enforced. A retry bound that the runtime cannot reach is a comment, not a policy.

If the retry is exhausted, the rescue run fails and — per the 0.1 spec's recovery flow — its
caller is reported stuck rather than resumed. That is the right shape: a fill that did not
happen and was not accepted is an open question, not a resolved one.

## Partial progress is progress

Re-read `bh setup check --json` at the end and diff `missing[]` against what 010 reported. Two
of four filled is a real result and the user should be told it in those terms, along with what
the remaining two still cost. `terminates_at: gap-filled` covers that case too: the arm the
user chose ran, and the caller can now re-enter its own probe and read the machine as it now
is.
