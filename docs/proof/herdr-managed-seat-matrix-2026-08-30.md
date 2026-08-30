# Herdr-managed exact-seat matrix — 2026-08-30

## Verdict

**NO-GO; capability remains unreleased.** The deterministic six-row transport matrix passes,
and one installed Codex developer row passed end to end. Five installed rows still lack empirical
execution evidence, so this change does not advertise them as supported.

## Installed boundary

- Herdr `0.8.2` exposes discrete `pane split --env KEY=VALUE` transport and
  `agent start ... -- [AGENT_ARG]...` passthrough.
- Claude Code `2.1.251` and Codex CLI `0.147.0` are installed.
- An isolated named Herdr session `bh-proof-4bhs7` was used; no operator Space/session was
  selected. The proof allocation was removed after the run.

| Harness | Seat | Hermetic authority/env/cwd | Installed launch | Release row |
| --- | --- | --- | --- | --- |
| Claude | developer | PASS | BLOCKED at first-run workspace trust; no completed probe | NO |
| Claude | dispatcher | PASS | Not executed after conjunctive matrix failed | NO |
| Claude | planner | PASS | Not executed after conjunctive matrix failed | NO |
| Codex | developer | PASS | PASS: Herdr-owned Agent reported `gpt-5.6-sol low`, `/tmp`, baked `developer` seat, and `proof-redacted-developer`; exact pane close left `agents: []` | YES, evidence only |
| Codex | dispatcher | PASS | Not executed after conjunctive matrix failed | NO |
| Codex | planner | PASS | Not executed after conjunctive matrix failed | NO |

The Claude trust screen is an installed-harness startup limitation for a fresh isolated checkout,
not authority evidence. It is recorded rather than bypassed with a dangerous permission flag.
The Codex trust prompt was confirmed interactively inside the isolated proof session without
persisting user configuration; the subsequent provider response independently named its baked
seat and observed the redacted environment value.

A follow-up attempt to run all six rows independently from the already-managed batch worktree was
denied at the execution boundary: external Claude/Codex processes could disclose private checkout
contents or configuration to provider destinations without explicit operator authorization. The
denial explicitly prohibited indirect execution or a workaround. The disclosure-safe opt-in runner
is [`scripts/probe-herdr-managed-seats.sh`](../../scripts/probe-herdr-managed-seats.sh); it requires
a non-default isolated session and an operator-approved exact Git checkout. No denied row is
counted as empirical support.

## Cancellation and recovery accounting

Closing the exact Codex pane terminated its Herdr allocation; the immediate authoritative
`agent list` returned an empty array. Hermetic tests cover generation/digest mismatch refusal and
receipt scrubbing. A full process-tree descendant probe and stopped-session restart adoption have
not passed against all six installed rows. Hermetic exact-metadata tests do prove restart adoption,
idempotent absence, and stale-generation refusal. Therefore installed cancellation/recovery
support is not claimed.

Native Claude Task and Codex collaboration children remain unmanaged. Neither is adopted into
the managed receipt, generation ledger, or cleanup boundary.
