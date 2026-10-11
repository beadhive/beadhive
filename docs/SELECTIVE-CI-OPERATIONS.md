# Selective-CI operational report

Evidence date: 2026-09-10 UTC

## Current disposition

- Certified closures: **0**
- Uncertified closures: **24**
- Production selective routes: **0**

No production selective-CI savings are claimed. Focused developer timings and
exact-tree receipt reuse answer different questions and are not extrapolated into this
report. Both promoted boundaries therefore retain unavailable measurements until
qualifying production samples exist.

## Production measurements

### `commit`

- Qualifying samples: 0
- Before wall time: unavailable
- Before compute time: unavailable
- Before queue delay: unavailable
- After wall time: unavailable
- After compute time: unavailable
- After queue delay: unavailable
- Full-suite frequency: unavailable
- Miss rate: unavailable
- Flake rate: unavailable
- Measured savings: unavailable

### `main-integration`

- Qualifying samples: 0
- Before wall time: unavailable
- Before compute time: unavailable
- Before queue delay: unavailable
- After wall time: unavailable
- After compute time: unavailable
- After queue delay: unavailable
- Full-suite frequency: unavailable
- Miss rate: unavailable
- Flake rate: unavailable
- Measured savings: unavailable

`unavailable` means there are zero qualifying observations, not zero cost or zero
failures. The current policy routes all real work through a full gate because no
closure is eligible.

## Closure inventory

Every row uses `just check` on uncertainty. A row can be reconsidered only after all
listed triggers are cleared and fresh certification plus shadow evidence is checked.

### `adapters`

- Kind: adapter
- State: uncertified
- Evidence date: 2026-09-10
- Owner: adapters closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `config.fragments`

- Kind: contract
- State: uncertified
- Evidence date: 2026-09-10
- Owner: config.fragments closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `config.pure`

- Kind: contract
- State: uncertified
- Evidence date: 2026-09-10
- Owner: config.pure closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `config.store`

- Kind: contract
- State: uncertified
- Evidence date: 2026-09-10
- Owner: config.store closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `contract.agent-launch`

- Kind: contract
- State: uncertified
- Evidence date: 2026-09-10
- Owner: contract.agent-launch closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `contracts`

- Kind: contract
- State: uncertified
- Evidence date: 2026-09-10
- Owner: contracts closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `integration`

- Kind: integration
- State: uncertified
- Evidence date: 2026-09-10
- Owner: integration closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `kernel`

- Kind: kernel
- State: uncertified
- Evidence date: 2026-09-10
- Owner: kernel closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `kernel.lifecycle`

- Kind: kernel
- State: uncertified
- Evidence date: 2026-09-10
- Owner: kernel.lifecycle closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `kernel.plugins`

- Kind: kernel
- State: uncertified
- Evidence date: 2026-09-10
- Owner: kernel.plugins closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `kernel.telemetry`

- Kind: kernel
- State: uncertified
- Evidence date: 2026-09-10
- Owner: kernel.telemetry closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.agents`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.agents closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.config`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.config closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.hives`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.hives closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.planning`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.planning closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.state`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.state closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.work`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.work closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `module.worktrees`

- Kind: module
- State: uncertified
- Evidence date: 2026-09-10
- Owner: module.worktrees closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `plugin.herdr`

- Kind: plugin
- State: uncertified
- Evidence date: 2026-09-10
- Owner: plugin.herdr closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `plugin.hitch`

- Kind: plugin
- State: uncertified
- Evidence date: 2026-09-10
- Owner: plugin.hitch closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `plugin.observaloop`

- Kind: plugin
- State: uncertified
- Evidence date: 2026-09-10
- Owner: plugin.observaloop closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `plugin.orca`

- Kind: plugin
- State: uncertified
- Evidence date: 2026-09-10
- Owner: plugin.orca closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `plugin.repowise`

- Kind: plugin
- State: uncertified
- Evidence date: 2026-09-10
- Owner: plugin.repowise closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

### `system-smoke`

- Kind: system
- State: uncertified
- Evidence date: 2026-09-10
- Owner: system-smoke closure maintainers
- Fallback: `just check`
- Recertification triggers:
  - `compatibility-facade-or-generated-artifact-change`
  - `dynamic-discovery-or-subprocess-ambiguity`
  - `input-digest-mismatch`
  - `missing-or-stale-coverage`
  - `shared-contract-or-schema-change`
  - `test-infrastructure-or-certifier-change`
  - `unenforceable-public-port`
  - `unknown-or-unowned-path`

## Registration and invalidation

A new module or plugin cannot register without a present closure and a conformance
declaration. The checked closure row is that declaration: it names ownership, source
scope, direct tests, shared-contract tests, and reverse-dependent tests.
`just test-closure-check` discovers registrations and rejects missing or incomplete
rows.

| Trigger | Automatic fallback | Action |
| --- | --- | --- |
| `boundary-change` | `just check` | recertify before selective use |
| `coverage-staleness` | `just check` | recertify before selective use |
| `escaped-regression` | `just check` | recertify before selective use |
| `schema-change` | `just check` | recertify before selective use |
| `tool-version-change` | `just check` | recertify before selective use |

## Retained safety boundaries

The complete `just check-all` gate is mandatory for these boundaries:

- `child-epic-finish`
- `final-workstream-review`
- `final-workstream-submit`
- `leaf-merge`
- `release`
- `scheduled`

Commit and main-integration routing may use a closure only after certification;
every uncertainty uses `just check`. Changing
`selective_ci.mode` from `certified` to `full` is the one-value rollback.

## Fence spike-merge saving on integration runs (bh-y4c4z)

Measures the effect of retiring the four fence spikes (bh-n0zj5 jbb6r, bh-3nfbn wtsrc and the
fixture shrink, bh-rdcjw sieai, bh-mclbc uhx2r) on the `-m "integration"` selection.

**Status of the evidence.** The "before" numbers are history-derived medians over 70 fence-touching
integration runs. The "after" numbers come from a **single targeted run** of the permanent
replacement files (2026-10-11, `-n 4`, `BH_DOLT_SLOTS=12`), not from five or more post-merge gate
runs. Confirmation will accrue from normal gates once the epic lands; re-run the aggregation below
then and replace the "after" column.

### Aggregation method

- Runs: every `.bh/validation/runs/run-*` whose `gate.log` header contains
  `pytest_with_report.py ... -m "integration"` and whose JUnit `<testsuite tests=...>` is at least
  200 (71 runs; 70 still contained the jbb6r spike). Unreadable or truncated XML is skipped.
- Per file, per run: the sum of `<testcase time>` over non-skipped cases whose `classname` maps to
  the file (JUnit case time includes setup and teardown, so expensive module fixtures are
  attributed to the first test that builds them). Per-file figure: the median across runs.
- Segment wall is the `<testsuite time>` attribute; total test-seconds is the sum over all
  non-skipped cases. Both are medians across runs. Parsing follows
  `scripts/refresh_test_durations.py` (bh-bpc9b).
- After: `NO_COLOR=1 BH_DOLT_SLOTS=12 ./scripts/hermetic.sh uv run --no-sync python -m pytest -q
  -p no:cacheprovider -n 4 --durations=0 tests/test_hq_sql_dolt_semantics_int.py
  tests/test_bd_branch_merge_int.py tests/test_writer_fencing_fixture_int.py
  tests/test_fence_composed_int.py tests/test_fence_trigger_canary_int.py` (29 passed, 244 s
  wall). Per-file seconds are the sum of the printed setup/call/teardown durations (cases under
  1 s are hidden by pytest, adding at most a few seconds).

### Before and after

| File | Before (s, median of 70) | After (s, one run) | Delta (s) |
| --- | ---: | ---: | ---: |
| `tests/spikes/test_bh_jbb6r_writer_partitioning_e2e_int.py` | 1,200 | 0 (retired) | -1,200 |
| `tests/spikes/test_bh_uhx2r_forward_rpc_int.py` | 668 | 0 (retired) | -668 |
| `tests/spikes/test_bh_sieai_write_guard_int.py` | 447 | 0 (retired) | -447 |
| `tests/spikes/test_bh_wtsrc_receiver_removal_liveness*.py` | 173 | 0 (retired) | -173 |
| `tests/test_writer_fencing_fixture_int.py` | 181 | 76 | -105 |
| `tests/test_fence_composed_int.py` | 504 | 559 | +54 |
| `tests/test_fence_trigger_canary_int.py` | 33 | 26 | -7 |
| `tests/test_bd_branch_merge_int.py` (new) | 0 | 30 | +30 |
| `tests/test_hq_sql_dolt_semantics_int.py` (new) | 0 | 5 | +5 |
| **Total** | **3,206** | **695** | **-2,511** |

Whole-selection baseline (71 runs): median segment wall 777 s and median 9,402 test-seconds (the
earlier 54-run baseline was 763 s and 8,487). Net saving about 2,500 test-seconds, which at the
roughly 11-way Dolt-slot-bound parallelism is an estimated 230 s (about 3.8 min) of segment wall,
an upper bound because the segment tail is set by the longest remaining files rather than by
total work. This exceeds the 1,000 test-second threshold, so no follow-up beads are needed.

Why it exceeds the ~1,100-1,300 test-second estimate: the estimate was made before
per-file medians were available, and the jbb6r and uhx2r spikes each carried 400 s-class
module-scoped fixtures that the permanent suites no longer pay. The replacement tests moved
rather than vanished (composed +54 s, bd branch merge +30 s), which is the only added cost. The
single targeted run used `-n 4` and may show contention differences versus the 16-worker gate.
