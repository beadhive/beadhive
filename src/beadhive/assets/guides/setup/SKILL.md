---
# yaml-language-server: $schema=https://agentguides.io/schemas/0.1/skill-guide-extension.schema.json
name: beadhive-setup
description: |
  Walk a user through setting up Beadhive on this machine without them
  reading the docs: probe what is already installed, choose ONE install
  route (the managed nix path, or PyPI as the labelled fallback), install
  and verify `bh`, then configure it — `bh config init`, MCP wiring, the
  harness plugin, Factory HQ, and a first hive — until `bh work ready`
  answers. Offers and explains every command before running it, and never
  installs nix on the user's behalf. Activate when asked to "install bh",
  "install Beadhive", "set up Beadhive on this machine", "get me started
  with bh", "walk me through the bh install", "finish setting up bh", or
  when `bh` is missing, half-configured, or `bh setup check` reports gaps.
license: MIT
compatibility: |
  macOS (Apple Silicon) or Linux, a POSIX shell, and outbound HTTPS. The
  managed route additionally needs nix, which this Guide will NOT install
  for you — it offers the command and waits for a human, then falls through
  to PyPI if you decline. MCP wiring and the plugin step are Claude Code
  specific and are skipped on other harnesses; nothing else is.
allowed-tools: Bash Read Write AskUserQuestion
metadata:
  type: guide
  guide:
    entry: GUIDE.md
---

# Beadhive setup

This Skill is a **Guide**. A Guide-aware harness loads `GUIDE.md` to begin a run.

If your harness only supports plain Skills, treat `GUIDE.md` and `steps/` as a structured
runbook: read `GUIDE.md` for framing, prerequisites and how the run is scored, then walk
`steps/` in order. This Guide is linear apart from one fork — the install route — which you
resolve once and carry forward.
