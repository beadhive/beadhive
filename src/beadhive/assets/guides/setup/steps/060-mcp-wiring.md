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
    - strategy: ask
      reason: |
        NOT APPLICABLE (probe exit 3) — no `claude` CLI on this machine, so
        there is nothing to wire. This is a SKIP, not a failure, and the
        answer this clause takes is "skip and continue". It is an `ask` only
        because the 0.1 step schema's strategies are retry / recover /
        abort / ask, with no `skip`. Nothing downstream requires MCP.
    - strategy: retry
      max_retries: 1
      reason: |
        NOT WIRED (probe exit 1) — `claude` is present but `claude mcp list`
        still has no bh entry after `bh mcp install`. Re-run once; if it
        persists, run the underlying command by hand:
        `claude mcp add bh --scope user -- bh mcp serve`.
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

`on_failure`'s first clause is an `ask` whose stated answer is *skip and continue*: the 0.1
step schema's strategies are `retry`, `recover`, `abort` and `ask`, with no `skip`, so the
intent is written into the clause's reason rather than left to inference.
