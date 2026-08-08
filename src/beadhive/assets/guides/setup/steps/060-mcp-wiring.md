---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/step.schema.json
step:
  id: mcp-wiring
  title: Wire the bh MCP server into the harness — probe before installing
  requires: [config-init]
  performer: agent
  action:
    type: prompt
    prompt: |
      PROBE FIRST — run:
        scripts/check-mcp-wired.sh

      which reads `claude mcp list`. Three answers, three responses:

      exit 0  already registered. Report ALREADY SATISFIED and move on.
              This is common: INSTALL.md's configure[] runs `bh mcp install`
              before this Guide starts, and the bh Claude plugin supplies
              the same server as `plugin:bh:bh`.
      exit 1  `claude` is here but there is no bh entry. Offer:
                bh mcp install
              It shells out to:
                claude mcp add bh --scope user -- bh mcp serve
              User scope, so it is wired ONCE for every hive and every
              future session — there is no per-hive MCP wiring.
      exit 3  no `claude` CLI. SKIP this step cleanly and say why: MCP
              wiring is Claude Code specific. OpenCode is furnished at 080
              through `bh hive onboard --opencode` instead, and nothing
              later in this Guide depends on MCP being wired. Do not
              install Claude Code to satisfy this step.

      Re-run the probe after installing. In a fresh Claude session,
      `bh doctor` also shows the MCP section as connected.
  verify:
    type: script
    script: scripts/check-mcp-wired.sh
    success_exit: 0
    output_schema: text
  interactions:
    - id: approve-mcp-install
      when: before
      kind: confirm
      prompt: |
        `bh mcp install` registers the bh MCP server with Claude Code at
        USER scope — one entry, shared by every hive and every future
        session. It edits Claude's own config, not this machine's PATH and
        not any repo. Approve?
      required: false
  on_failure:
    # `reason` is a kebab-case LABEL, matched against the runtime's
    # `step.failed.fields.reason`; the argument for each is in the body.
    - reason: no-claude-cli
      strategy: recover
      recover_with: "guide:./guides/rescue"
      resume_after_recovery: true
    - reason: still-unwired-after-install
      strategy: retry
      max_retries: 1
  effect: reversible
  estimated_duration_minutes: 2
  tags: [configure, claude-code, optional-off-claude]
---

The `bh` MCP server exposes planning, work, hive and config tools to every Claude Code session,
across every hive, once it is registered at **user scope**. One-time; there is no per-hive
wiring to do afterwards.

## Probe before installing, not after

`claude mcp list` is read-only and cheap, and running it first is what makes this step safe to
re-enter. `INSTALL.md`'s `configure[]` block runs `bh mcp install` before this Guide is
invoked, so arriving here already wired is ordinary rather than exceptional.

`scripts/check-mcp-wired.sh` accepts **two** registration shapes as wired:

- `bh: …` — the user-scope entry `bh mcp install` adds.
- `plugin:bh:bh: …` — the same server supplied by the bh Claude plugin (step 065), which Claude
  namespaces as `plugin:<plugin>:<server>`.

`docs/ONBOARDING.md:212` matches only the first (`grep -q '^bh '`), which reports a
plugin-wired machine as unwired and sends the user to re-install something they already have.
Measured against `claude mcp list` output on 2026-08-08.

## Off Claude Code this step skips, it does not fail

The probe exits **3** for "no `claude` CLI", deliberately distinct from both 0 (wired) and 1
(present but unwired). A guide that hard-requires Claude Code silently excludes every other
harness, and installing a second agent harness to satisfy an install step is not a trade anyone
asked for.

OpenCode is supported through a different mechanism entirely — `bh hive onboard --opencode` at
step 080 — and nothing after this step depends on MCP being wired. Say that when skipping, so
the user knows they have lost a convenience and not a capability.

## Failure routing — two labels, and why neither is `ask`

`on_failure`'s `reason` is a **kebab-case label**: the runtime matches it verbatim against
`step.failed.fields.reason`, so a clause labelled with a paragraph can never be selected. The
argument for each clause lives here.

**`no-claude-cli` (probe exit 3) → `recover` into `guide:./guides/rescue`,
`resume_after_recovery: true`.** The step lifecycle has a `skipped` terminal state and a
conformant harness reaches it here without entering `on_failure` at all. This clause is the net
underneath that — it catches a harness that treats "not applicable" as a failure — and on a
machine running any other harness it is the path most runs take, so it is the one clause that
absolutely must not dead-end.

It cannot be spelled `ask`. The 0.1 strategies are `retry`, `recover`, `abort` and `ask`; there
is no `skip`, and **a bare `ask` with no `recover_with` resolves as abort**. Writing "skip and
continue" into the clause's `reason` gives the runtime nothing it can present, and the run
terminates at `@stuck` with no end state and no score. So the clause recovers into the sibling
rescue Guide, which names the absence, states that OpenCode is furnished at 080 by a different
mechanism, and reaches `gap-accepted` — a 1.0 end state, because a machine with no Claude Code
has lost nothing it was promised. Resume returns here with that recorded.

**`still-unwired-after-install` (probe exit 1) → `retry`, `max_retries: 1`.** `claude` is
present but `claude mcp list` still has no bh entry after `bh mcp install`. Re-run the probe
once; if it persists, run the underlying command by hand —
`claude mcp add bh --scope user -- bh mcp serve`.
