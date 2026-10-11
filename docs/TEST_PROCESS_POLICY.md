# Test process and hang policy

The test harness uses `spawn` for ordinary multiprocessing on macOS and Linux. Python 3.13
warns when a multithreaded process calls `fork`, and pytest-xdist workers contain threads; a fork
from one can inherit locks whose owning threads no longer exist and deadlock nondeterministically.
Linux is not exempt from that failure mode even though its interpreter default remains `fork`.

## Adding a process test

Import `process_context` from `harness.processes`, keep the process target at module scope, and
pass only serializable inputs and synchronization objects created from that context. Test modules
must not import `multiprocessing` directly: that also excludes ambient-context spawners such as
`Manager`, `Pool`, and `Process`. Do not use `concurrent.futures.ProcessPoolExecutor` or `os.fork`.
`tests/test_process_harness_policy.py` follows simple aliases and fails if a process-capable import
or call bypasses the reviewed helper; thread executors remain allowed.

The test must continue to assert the real cross-process behavior. Replacing a process with a
thread does not prove flock, append, admission, owner-death, or ledger behavior.

`isolated_fork_context` exists only for a reviewed test of fork semantics that cannot be expressed
with spawn. It refuses to run in an xdist worker or any multithreaded parent. Test programs that
exercise process-group behavior may fork inside a fresh, standalone subprocess; those literal
programs do not fork the xdist worker itself. A helper-based fork test must have a separately
documented serial gate phase; it must never be silently admitted to the parallel selection.

## Hub, HQ, and onboarding test boundary

Fast unit selection keeps branch-facing behavior on deterministic seams. Durable store behavior
that needs `bd` stays in the integration selection and is bounded by the `dolt_server` marker.
These named cases are classified as follows:

| Tests | Selection | Contract retained |
| --- | --- | --- |
| `tests/test_hub_rebuild.py::test_rm_rf_the_hub_then_rehydrate_yields_the_identical_aggregate` | `integration`, `dolt_server` | Real-store rebuild fidelity, source prefixes, and stable aggregate identity. |
| `tests/test_hub_rebuild.py::test_hq_publishes_no_hive_derived_beads_and_prune_makes_it_so` | `integration`, `dolt_server` | Real HQ prune scope and durable native-bead preservation. |
| `tests/test_bd_repo_sync_additive.py::test_bd_repo_sync_is_additive` | `integration`, `dolt_server` | Real `bd repo sync` preserves native beads and imports source IDs with their prefix. |
| `tests/test_hq.py::test_ensure_store_stands_up_git_bd_repo_prefix_hq` | `integration`, `dolt_server` | `hub.ensure_store` creates and reuses the durable Git + bd HQ store. |
| `tests/test_hive_opencode.py::test_onboard_opencode_writes_config_agents_and_agf_hint`, `test_onboard_opencode_is_idempotent`, `test_onboard_opencode_force_refreshes` | unit | OpenCode installer routing, generated files, local-edit preservation, and explicit refresh. Their fixture stubs auto-export, host bd config, and hub synchronization, which are separate durable-store concerns. |
| `tests/test_hub_bulk_int.py::test_bulk_copy_matches_a_real_bd_produced_aggregate` | `integration`, `dolt_server` | Real `bd` aggregate parity, event/wisp fidelity, copy exclusions, target identity, and ancestor validation. One multi-database query snapshots the same explicit content surfaces. |
| `tests/test_hub_bulk_int.py::test_hub_sync_row_counts_are_non_decreasing_per_prefix_across_a_sync` | `integration`, `dolt_server` | Real `hub.sync()` with per-prefix non-decreasing counts across steady and growth passes. Fixture writes are batched; the bulk path reads co-located source databases directly, so source `bd export` setup is unnecessary. |
| `tests/test_localloop_int.py::test_restart_mid_molecule_neither_double_claims_nor_leaves_a_seat_spending` | `integration` | Real `bd` claims, orphan process-group reap, cancellation label, and completed molecule after restart. The test waits for actual replacement-seat exits instead of running fixed-delay polling passes. |
| `tests/test_plan_repair.py::test_repair_and_approve_converge_hand_assembled_epic_real_bd` | `integration` | Real hand-assembled `bd` graph, repair/approval refusal routing, generated swarm and root gates, origin/dependent exclusions, real-storage repair replay, real gate approval, verify, and dispatcher start. Dependency setup is one transaction; the fast `test_repair_then_approve_twice_each_converges` retains direct command idempotence coverage for both commands. |

The fast hub/HQ contracts remain covered by fake-based tests in `tests/test_hub.py`,
`tests/test_hub_bulk.py`, and `tests/test_hq.py`, including repository routing, source-prefix
handling, sync failure behavior, and HQ initialization ordering. The named integration proofs are
selected by `just test-integration-land`; they are not skipped or hidden behind a quarantine.
The branch-facing fast counterparts for the four `.6` scenarios are:

| Integration proof | Fast contract |
| --- | --- |
| Hub bulk aggregate parity | `tests/test_hub_bulk.py::test_copy_hive_success_touches_every_content_table_and_nothing_else` and `test_run_bulk_pass_never_queries_a_denied_or_undecided_table` |
| Hub sync per-prefix preservation | `tests/test_hub.py::test_sync_bulk_enabled_calls_run_bulk_pass_with_resolved_entries` and `test_sync_bulk_pass_receives_changed_flag_from_the_existing_watermark` |
| Local-loop restart | `tests/test_localloop.py::test_the_loop_never_spawns_two_processes_for_one_bead`, `test_a_finished_run_is_harvested_and_its_group_reaped`, and `test_shutdown_terminates_children_through_the_group_and_unclaims` |
| Plan repair convergence | `tests/test_plan_repair.py::test_repair_backfills_swarm_gates_state_and_labels`, `test_repair_excludes_origin_report_children_from_roots`, and `test_repair_then_approve_twice_each_converges` |

Scenario setup is consolidated without replacing the real-store or real-process assertions.

## Hang watchdog

The parallel `just test` and `just test-integration-land` recipes run under
`scripts/test-watchdog.py`. The default deadline is 900 seconds and can be changed explicitly with
`BH_TEST_TIMEOUT_SECONDS`. A timeout:

1. prints the descendant PID, parent PID, state, elapsed time, and executable without dumping
   potentially secret command arguments;
2. prints each pytest process's active test with parameter values removed, then asks only pytest
   processes that registered the private diagnostics contract for all-thread stacks through
   `SIGUSR1` (helpers such as multiprocessing's `resource_tracker` are never signaled);
3. terminates the complete process group, checking group liveness independently of the command
   leader and escalating surviving descendants to a kill after the grace period; and
4. exits 124, never zero.

Pytest controllers and xdist workers register `SIGUSR1` with `faulthandler` to private per-process
files, so the stack request is actionable on macOS and Linux even when xdist captures worker
stderr. Stack dumps contain file/function locations but no locals or arguments. On a future hang,
retain the timeout output, identify the worker and child that stopped progressing, rerun the
focused selection with `-n 2` and `-n 6`, then use `-n 0` only as a comparison. A serial pass does
not waive a parallel failure. A live multiprocessing `resource_tracker` is expected until its
worker exits; it is not evidence of a deadlock by itself.

## Time in tests

A test never waits on real time to make time pass, and a timeout is never the fix for a slow or
hung test. Pick the clock by where the time is read:

| The code under test... | Use | Why |
|---|---|---|
| takes a clock through a seam (`StateClock.now()`, a `stop=` / sleep callable such as `SessionRenewer.run`) | `tests/harness/clock.py` `FakeClock` | Explicit, deterministic, no global patching; `sleep()` advances instead of blocking and `monotonic()` never decreases. |
| reads `time.time()`, `time.monotonic()`, or `datetime.now()` directly and cannot take a seam | `time-machine` (`time_machine.travel`) | Patches the process clocks in-test; prefer adding a seam when you own the code. |
| expires on the Dolt server clock (`UTC_TIMESTAMP()` in SQL) | Data-driven expiry: write rows whose timestamps are already past or far future | Neither fake clock reaches the server; do not sleep to let a row expire. |
| waits for another process (server, worker, subprocess) | A readiness handshake (poll a socket, file, or status with a bounded deadline) | Sleeping a guessed interval races under `-n auto`; see the contention note in the hang watchdog. |
| hangs or is slow | Fix the cause; never `@pytest.mark.timeout` | `pytest-timeout` is installed only as a backstop (`timeout = 600`, `timeout_method = "thread"`, below the 900 s `BH_TEST_TIMEOUT_SECONDS` watchdog) so one hung test is named before the run is killed. |

### Guard against new wall-clock waits (bh-zbn8a)

`scripts/check_test_wall_clock.py` (run by `architecture-check` and the concurrent
`architecture-structural-check` pool, so inside the gate) parses `tests/**` with the stdlib AST and
fails on any `time.sleep` / `asyncio.sleep` with a non-zero wait, and on an `assert` comparing a
measured `time.monotonic()` / `time.time()` duration to a bound. Child programs inside string
literals are not parsed. An accepted wait needs a reasoned `[[allow]]` entry (path + function
anchor + kind + reason) in `tests/wall_clock_allowlist.toml`; stale entries fail, so the list only
shrinks. Latency or overlap budgets go under `@pytest.mark.perf`: exempt from the check and
deselected from every gate lane (`tests/harness/perf_marker.py`); run them on a quiet host with
`-m perf` or `BH_PERF_TESTS=1`.

Watch item: `tests/test_hq_authority_backend.py::test_real_foreign_holder_takeover_requires_authenticated_incumbent_evidence`
failed once under 3-way concurrency during bh-p4bbw. Host contention is unconfirmed; if it recurs,
diagnose contention before reaching for retries or longer sleeps.

## Selective-CI policy

The checked [Selective-CI operational report](SELECTIVE-CI-OPERATIONS.md) is the authoritative
closure inventory and measurement ledger. As of 2026-09-10, production selective routes: **0**.
All 24 closures are uncertified, so no before/after wall time, compute time, queue delay,
full-suite frequency, miss rate, flake rate, or savings measurement is available. Focused
developer timings and exact-tree receipt reuse are not extrapolated into production savings.

Commit and main-integration are the only provisioned selective boundaries. They run a closure only
after exact current certification, qualifying shadow evidence, and trusted route verification;
otherwise they run `just check`. `leaf-merge`, `child-epic-finish`,
`final-workstream-submit`, `final-workstream-review`, `scheduled`, and `release` are permanently
full-only at this policy version and run `just check-all`.

An escaped regression, boundary change, stale coverage, tool-version change, or schema change
automatically restores `just check` and requires recertification before selective use. A new module
or plugin must add a checked closure/conformance declaration before registration. To disable the
provisioned routes in one change, set `selective_ci.mode = "full"` in
`tests/selective-ci-policy.toml`.

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
