# E4 latency wave proof

Measured on 2026-09-21 in the E4 batch worktree, using the repository virtual environment
and the shared `bh` hive (Linux, `bd` 1.3.0). Times are warm-shell wall-clock seconds.

| command | before | after |
| --- | ---: | ---: |
| `bh --version` | 3.38 | 0.104 / 0.182 / 0.102 |
| `bh work brief bh-8kn42` | 5.62 | 3.890 / 3.471 / 3.654 |
| `bh plan status bh-mgn1k` | 301.24 | 6.374 |

The invocation consolidation itself is a correctness and future warm-process seam, not the
source of the startup improvement. `BdEngine.invoke` now owns ordinary `bd` subprocess
construction, applies the 120-second default bound, makes hive routing, process cwd, capture
versus streaming, input, environment, actor, and exit-0/no-work classification explicit, and
returns synthetic exit 124 on timeout. `bd.err_line` is the sole failure parser. The only direct
source invocations left are the documented safety boundary and hub process-group supervisor;
an AST guard fixes that exception set. `bd.states_for()` was absorbed unchanged.

## PATCH audit

The batch range from `main` contains only:

```text
f161f780 refactor(bd): consolidate bounded invocation seam
```

There is no `feat:` or breaking commit. All implementation members and the nested process-tax
members carry `release:fix`; no wider release type was discovered.

## Focused validation and operator waiver

The operator explicitly directed E4 to use targeted checks and defer the broad aggregate gate
until the latency work is on `main`. Accordingly, no `just check` or `just check-all` was run.
The persistent fleet configuration was not edited. Submission uses a process-local `BH_HOME` /
`BH_CONFIG` copy whose `submit` command is the focused suite and whose selective attest-key list
is empty.

Focused results:

- 148 passed: invocation seam, engine, bd JSON seam, and storage migration.
- 70 passed: hub.
- 39 passed: onboard and server-mode onboard.
- 116 passed: backup, HQ, registry, and validate probe.
- 101 passed: Dolt health and escalation.
- Ruff passed for every changed Python file; the commit hook also passed the 13 naming-contract
  tests.

Any aggregate or unrelated failure discovered after landing is follow-up work. Existing
post-E4 follow-ups `hq-1zsz` and `hq-n250` retain that scheduling. No additional follow-up was
needed from the focused results.

## Close-out state

At this proof snapshot, `bh-j0q8s.1`, `.2`, and `.3` are closed. `bh-j0q8s.4` has completed its
local empty commit and upstream report but remains in review, so the nested `bh-j0q8s` epic must
be closed (or `.4` explicitly deferred on its own record) before the E4 dispatcher performs the
final molecule close. The seam and wave-exit batch is otherwise ready for review.
