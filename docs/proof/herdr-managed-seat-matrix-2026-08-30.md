# Herdr-managed exact-seat matrix — 2026-08-30

## Verdict

**GO under the amended restart contract.** The deterministic and installed six-row transport
matrices pass, as do installed process-tree cancellation and generation-fenced retry. Herdr
`0.8.2` authoritatively terminates Agents on graceful server shutdown and server crash. Beadhive
does not infer work completion: it detects the absent exact generation and relaunches generation
`N+1` in the same exact seat and worktree. Live-process survival across Herdr restart is not part
of the released contract.

## Installed boundary

- Herdr `0.8.2` exposes discrete `pane split --env KEY=VALUE` transport and
  `agent start ... -- [AGENT_ARG]...` passthrough.
- Claude Code `2.1.251` and Codex CLI `0.147.0` are installed.
- An isolated named Herdr session `bh-proof-4bhs7` was used; no operator Space/session was
  selected. The proof allocation was removed after the run.

| Harness | Seat | Hermetic authority/env/cwd | Installed launch | Transport evidence |
| --- | --- | --- | --- | --- |
| Claude | developer | PASS | PASS: exact `bh:developer` seat and `proof-redacted-claude-developer` observed | PASS |
| Claude | dispatcher | PASS | PASS: exact `bh:dispatcher` seat and `proof-redacted-claude-dispatcher` observed | PASS |
| Claude | planner | PASS | PASS: exact `bh:planner` seat and `proof-redacted-claude-planner` observed | PASS |
| Codex | developer | PASS | PASS: developer-level seat and `proof-redacted-codex-developer` observed | PASS |
| Codex | dispatcher | PASS | PASS: developer-level seat and `proof-redacted-codex-dispatcher` observed | PASS |
| Codex | planner | PASS | PASS: developer-level seat and `proof-redacted-codex-planner` observed | PASS |

The operator explicitly authorized the six external provider launches against the exact private
checkout `/tmp/bh-worktrees/github/beadhive/beadhive/batch-bh-4bhs7`. The opt-in runner was invoked
with `BH_HERDR_PROOF_SESSION=bh-proof-4bhs7` and that exact `BH_HERDR_PROOF_CWD`; it returned zero
after printing six `ROW PASS` records. Each row was independently created and removed through
Herdr. The runner remains fail closed without both a non-default session and exact Git checkout.

## Cancellation and recovery accounting

Installed cancellation passed for both provider process shapes:

- closing the exact Claude pane removed the Agent plus all 7 sampled live descendants, including
  MCP and browser-tool descendants;
- closing the exact Codex pane removed all 9 sampled Agent-tree processes, including code-mode,
  MCP/browser, sandbox wrapper, and a deliberately live `sleep` descendant; and
- every matrix pane close removed its named Agent before the next row, with a final authoritative
  `agent list` of `[]`.

Installed generation fencing also passed after two defects exposed by the live probe were fixed:

- roster ownership now validates a generation-tagged target from `bead + launch_id`, rather than
  comparing every managed launch with the legacy bead-only target;
- generation `6` plus a conflicting digest was refused as `stale_generation`, leaving generation
  `7` live;
- the exact generation `7` and digest reaped the pane; and
- retrying that exact receipt returned `already_reaped` without another close. The absence check
  intentionally precedes live-generation validation only when the exact receipt pane, target, and
  raw pane are all absent; a live successor or partial match still refuses.

Installed server restart behavior was proved twice:

1. graceful `herdr --session bh-proof-4bhs7 server stop` preserved the workspace/pane topology but
   terminated Codex; after restart the pane contained a shell and `agent get` returned
   `agent_not_found`;
2. force-stopping only the exact named proof-server PID while the tagged Codex PID was live also
   terminated the Agent before restart.

This is the authoritative loss signal required by the amended contract. A fresh Beadhive process
reads the restored topology, refuses the missing old generation as adoptable, preserves native
bead/worktree state, advances the monotonic generation, and launches the same canonical seat in a
new pane. Repeating recovery adopts only that exact live successor; an old, foreign, partial, or
conflicting generation refuses without mutation. Hermetic tests cover the transition because the
installed stop/crash probes already establish the provider-loss boundary itself.

All proof-created workspace IDs were absent, `agent list` was empty, and the exact named proof
session was stopped and deleted after the run.

Native Claude Task and Codex collaboration children remain unmanaged. Neither is adopted into
the managed receipt, generation ledger, or cleanup boundary.
