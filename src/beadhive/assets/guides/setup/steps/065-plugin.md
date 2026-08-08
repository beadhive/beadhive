---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: plugin
  title: Install the bh harness plugin — optional, and Claude Code only
  requires: [mcp-wiring]
  accepts_skipped: true
  performer: agent
  action:
    type: prompt
    prompt: |
      OPTIONAL. Ask before doing anything here, and take no for an answer.

      SKIP CONDITION, checked first: preflight `harness` is not
      `claude-code`, or there is no `claude` CLI. Then skip and say where
      the equivalent lives for that harness:
        OpenCode  — `bh hive onboard --opencode` at step 080 furnishes the
                    hive for it; that is the supported path, not this one.
        anything  — the role skills are also readable straight out of the
        else        repo, and `bh` itself needs no plugin to work.
      Do NOT install Claude Code in order to satisfy this step.

      PROBE, if the harness IS Claude Code:
        claude plugin list

      If `bh@beadhive` is listed, report ALREADY SATISFIED and move on —
      INSTALL.md's configure[] block installs it before this Guide starts,
      so this is a common arrival state.

      Otherwise offer BOTH commands together, in order, and wait for
      approval:
        claude plugin marketplace add beadhive/claude-plugin
        claude plugin install bh@beadhive

      The first registers the marketplace; the second installs from it.
      Neither is useful alone, so present them as one decision.

      Re-run `claude plugin list` afterwards. A newly installed plugin
      loads in the NEXT session, so "not active yet" in this one is
      expected and is not a failed install.
  verify:
    type: agent_judgment
  interactions:
    - id: want-plugin
      when: before
      kind: confirm
      prompt: |
        Optional: install the bh Claude Code plugin (`bh@beadhive`). It adds
        the AGF seat definitions and role skills — the coordinator,
        developer and merger guides — so an agent can take a seat without
        being handed the docs each time. It changes only Claude Code's own
        plugin config. `bh` works fully without it. Install it?
      required: false
  on_failure:
    # `reason` is a kebab-case LABEL, matched against the runtime's
    # `step.failed.fields.reason`, and it is ALSO the discriminator between
    # this step's two recovery nodes; the argument for each is in the body.
    - reason: not-claude-code
      strategy: recover
      recover_with: "guide:./guides/rescue"
      resume_after_recovery: true
    - reason: plugin-declined-or-install-failed
      strategy: recover
      recover_with: "guide:./guides/rescue"
      resume_after_recovery: true
  effect: reversible
  estimated_duration_minutes: 2
  tags: [configure, optional, claude-code]
---

The `bh@beadhive` plugin carries the AGF seat definitions and role skills, so an agent taking a
coordinator, developer or merger seat has the process to hand instead of being pointed at the
docs each time. It is a convenience, and this Guide treats it as one.

## Optional means optional

Two properties keep it that way, and both are load-bearing:

- **It is approval-gated.** The `want-plugin` interaction is a `confirm` with
  `required: false`. "No thanks" is a complete answer and the run continues without it.
- **Nothing downstream requires it.** `070` and `080` do not read it, and the `rung-1-reached`
  end state does not mention it. A user who declines still finishes.

## It is Claude Code specific, so it must skip cleanly elsewhere

`claude plugin marketplace add` and `claude plugin install` are Claude Code commands. A guide
that hard-requires them silently excludes every other harness — and OpenCode, which `bh`
genuinely supports, is furnished through a completely different mechanism:
`bh hive onboard --opencode` at step 080.

So branch on what preflight found in `harness`, and when it is not `claude-code`, **skip with
a pointer** rather than failing or improvising. Installing a second agent harness to satisfy an
optional install step is not a trade to offer.

`accepts_skipped: true` is what makes this legal at the schema level: this step requires `060`,
and `060` legitimately skips on a machine with no `claude` CLI. Without it, one skipped step
would strand its successor.

## Failure routing — both clauses `recover`, and neither `ask`s

`on_failure`'s `reason` is a **kebab-case label**. Here it does double duty: the runtime matches
it against `step.failed.fields.reason`, *and* it is the discriminator between this step's two
recovery nodes, since a step with more than one recovery clause gets one node per label. A
paragraph in that field is unmatchable and unreadable as an id both.

The two labels cover the two ways this step does not happen — **`not-claude-code`** (wrong
harness, and bh will not install one to make it apply) and
**`plugin-declined-or-install-failed`** (the user said no, or the install did not take). Both
are *outcomes*, not faults, and on a machine that is not running Claude Code both sit on the
modal path.

Neither can be an `ask`. 0.1 has no `skip` and no `continue`, and the reflex of writing
`strategy: ask` with the intended answer in `reason` does not work: **a bare `ask` with no
`recover_with` resolves as abort**, so the runtime never presents that answer and the run
terminates at `@stuck` with no end state and no score.

So both `recover` into the sibling rescue Guide (`guide:./guides/rescue`) with
`resume_after_recovery: true`. It records the absence — the pointer to
`bh hive onboard --opencode` for the wrong-harness case, the install command kept for later for
the declined one — reaches `gap-accepted` (scored 1.0, level with actually filling the gap), and
returns here. Its `030-accept-the-gap` step is explicit that it must **not** re-offer what was
just declined; a decline that gets asked again is how an optional step becomes a nag.

## Present both commands as one decision

`claude plugin marketplace add beadhive/claude-plugin` registers the marketplace;
`claude plugin install bh@beadhive` installs from it. Neither does anything useful without the
other, so asking twice buys nothing but a second chance to say no halfway.

## Already installed is a normal arrival state

`INSTALL.md`'s `configure[]` block ends with exactly this plugin (`kind: plugin`,
`harness: claude-code`, `ref: bh@beadhive`). Probe with `claude plugin list` first and report
*already satisfied*; do not reinstall to be sure.

One expected non-failure to name for the user: a newly installed plugin loads in the **next**
session. Not being active in the current one is how it works, not a botched install.
