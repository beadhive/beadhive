---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/skill-guide-extension.schema.json
name: beadhive-setup-rescue
description: |
  Rescue Guide for the Beadhive setup Guide. Handles ONE failure family: a
  tool, CLI or harness feature that a setup step needs is absent because of
  the install route taken or the harness in use. Names the gap from
  machine-readable state, then either FILLS it (runs the remedy `bh setup
  check --json` already computed) or ACCEPTS it (records the absence and
  what it costs) so the calling step can be re-entered and decided instead
  of left open. Invoked as the `on_failure.recover_with` target of the setup
  Guide's route- and harness-conditional steps; runnable standalone when
  someone asks "bh says dolt is missing, what do I do".
license: MIT
compatibility: |
  Same as the setup Guide it rescues: macOS (Apple Silicon) or Linux, a
  POSIX shell, outbound HTTPS. Installs nothing without approval and never
  installs nix or a second agent harness — those absences are ACCEPTED here,
  not filled.
allowed-tools: Bash Read AskUserQuestion
metadata:
  type: guide
  guide:
    entry: GUIDE.md
---

# Beadhive setup — rescue

This Skill is a **Guide**, and normally a *called* one: the setup Guide names it as
`recover_with: "guide:./guides/rescue"` on every step whose failure is a missing prerequisite
rather than a broken action.

If your harness only supports plain Skills, read `GUIDE.md` then walk `steps/` in order. There
is exactly one fork — fill the gap or accept it — and both arms are successful exits.
