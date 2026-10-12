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

## Scheduling and rewind savings on integration and stateful lanes (bh-05dza)

Measures the combined effect of longest-first ordering with work stealing (bh-bpc9b), the
rewindable cluster fixture (bh-i970d) and the fence suites moved onto it (bh-jvgj6) on the two
16-worker native lanes, against the post-bh-exqka/vcx4i main baseline.

**Status of the evidence.** Measured 2026-10-11 on a shared 32-core host. The baseline runs are
whole-gate runs and ran while other heavy work was active; the after runs were taken one at a time
with `BH_DOLT_SLOTS=12 BH_TEST_TIMEOUT_SECONDS=2400`. Integration has five after-runs and two
baseline runs. Stateful has two after-runs and two baseline runs, fewer than the five asked for,
so its figures are indicative only. The after tree is the epic container (779cb194), which
predates main's latest commits; three stateful tests fail on it for that reason (see below).

### Method

- Baseline: post-bh-exqka main gate runs under `.bh/validation/runs` whose `gate.log` shows the
  lane's `pytest_with_report.py` selection and whose rootdir is a `verify-main-*` checkout.
  Integration (`-m "integration"`, `-n 16 --dist worksteal`): `run-530250ed...` (750 s) and
  `run-7bf8e234...` (817 s, main cbac6d09). Stateful (`-m "not integration and not pants_profile"`,
  `-n 16`): `run-6dd1dd8f...` (691 s, main 25d8d223) and `run-28bbb788...` (752 s).
- After: `just test-integration-land` (five runs) and `just stateful-native` (two runs) from the
  bh-05dza worktree.
- Aggregation is the one in the bh-y4c4z section: `<testsuite time>` is wall, the sum of
  non-skipped `<testcase time>` is test-seconds, per-file figures are summed case times, and each
  figure is the median across runs. Parsing follows `scripts/refresh_test_durations.py`.
- Ideal is test-seconds divided by 16 workers. Tail is wall minus ideal, so it is the time lost to
  imperfect packing, startup and Dolt-slot waiting.

### Lane totals

| Selection class | Runs | Wall (s) | Test-seconds | Ideal (s) | Tail (s) |
| --- | ---: | ---: | ---: | ---: | ---: |
| Integration, before | 2 | 783 (750-817) | 8,228 | 514 | 269 |
| Integration, after | 5 | 555 (551-584) | 6,503 | 406 | 149 |
| Integration delta | | -228 | -1,725 | -108 | -120 |
| Stateful, before | 2 | 722 (691-752) | 8,728 | 545 | 176 |
| Stateful, after | 2 | 622 (613-631) | 8,164 | 510 | 112 |
| Stateful delta | | -100 | -564 | -35 | -64 |

### Integration, per file (median test-seconds)

| File | Before | After | Delta |
| --- | ---: | ---: | ---: |
| `tests/test_epoch_fencing_int.py` | 1,433 | 1,249 | -183 |
| `tests/test_fence_composed_int.py` | 774 | 592 | -181 |
| `tests/test_fence_orphan_int.py` | 475 | 358 | -118 |
| `tests/test_agf_modalities.py` | 392 | 307 | -85 |
| `tests/test_storage_migrate_int.py` | 376 | 386 | +10 |
| `tests/test_onboard_server_mode_int.py` | 369 | 306 | -64 |
| `tests/test_hq_sql_provision_history_int.py` | 346 | 41 | -305 |
| `tests/test_placement_authority_int.py` | 314 | 50 | -264 |
| `tests/test_fence_cutover_int.py` | 301 | 250 | -51 |
| `tests/test_hive_forward_int.py` | 271 | 222 | -49 |
| `tests/test_failover_reclaim_int.py` | 240 | 247 | +7 |
| `tests/test_frame_release_upgrade_cli_int.py` | 231 | 195 | -37 |
| `tests/test_localloop_int.py` | 208 | 161 | -48 |
| `tests/test_cluster_rewind_int.py` (new) | 0 | 199 | +199 |

### Stateful, per file (median test-seconds)

| File | Before | After | Delta |
| --- | ---: | ---: | ---: |
| `tests/test_hq_authority_backend.py` | 1,505 | 1,687 | +182 |
| `tests/test_work.py` | 909 | 871 | -38 |
| `tests/test_contract_release.py` | 522 | 529 | +7 |
| `tests/test_wire_schemas.py` | 351 | 312 | -40 |
| `tests/test_test_closures.py` | 281 | 265 | -16 |
| `tests/test_wire_release_publish.py` | 278 | 259 | -18 |
| `tests/test_cli.py` | 217 | 205 | -12 |
| `tests/test_test_closure_certification_process.py` | 193 | 115 | -78 |

### Reading the numbers

- **Fence-touching integration: wall 12.5-13.6 min to 9.2 min** (about 3.8 min saved), and the
  tail fell from about 270 s to about 150 s (about 45%). That is most of the roughly 230 s tail
  the scheduling work targeted. The rewind fixture removed about 1,725 test-seconds, mostly from
  the provision-history, placement-authority and dolt-health suites, and the composed and orphan
  suites. The 2-3 min rewind estimate is met (the wall saving is larger, 3.8 min, because
  scheduling and rewind stack).
- **The 5-6 min target is not met.** After-wall is 9.2 min. Ideal alone is 406 s (6.8 min), so
  even perfect packing of the current work misses 6 min. Two limits remain: total work
  (`tests/test_epoch_fencing_int.py` alone is 1,249 test-seconds and was not moved onto the
  rewindable cluster) and indivisible long tests (the longest single case is about 213 s,
  `test_s5_stale_writer_is_refused_on_every_default_bd_path`, which bounds the tail from below).
- **Stateful** improved by about 100 s wall (about 14%), nearly all from scheduling (tail
  176 s to 112 s) since stateful runs no fence-heavy real-Dolt suites. It is dominated by
  `tests/test_hq_authority_backend.py` (1,687 test-seconds, with a single case near 168 s).
  Only two after-runs, so treat the 100 s as approximate.
- **Noise.** Wall differs by 67 s between the two baseline integration runs of similar trees, so
  the baseline is noisier than the after-runs; per-file medians over two runs are rough.
- **Reliability.** One of five integration after-runs failed:
  `tests/test_cluster_rewind_int.py::test_a_fencing_footprint_rewinds_to_the_bootstrap_fingerprint`
  saw a stray `/processes` entry after rewind. The failure is in the new fixture's leak guard, not
  the savings. Both stateful after-runs fail the same three tests,
  `test_xdist_worker_cap.py::test_live_integration_recipe_redistributes_unstarted_tests_with_fixed_worker_bound`,
  `test_test_closure_operational_report.py` and `test_test_closure_certification_process.py`,
  because the container tree lacks main's later changes (the generated report and the justfile
  recipe text differ from main). They are expected to clear when the container is refreshed from
  main.

### Follow-ups

- bh-t8hr4: move `tests/test_epoch_fencing_int.py` onto the rewindable cluster (the biggest
  remaining file, 1,249 test-seconds).
- bh-56wtv: shorten or split the longest integration cases, which set the tail floor.
- bh-u5mag: fix the rewind guard's intermittent stray-process failure.
- bh-88f8w: cut `tests/test_hq_authority_backend.py` on the stateful lane.

## Epoch-fencing suite on the rewindable cluster (bh-t8hr4)

**Correction to the bh-05dza reading.** `tests/test_epoch_fencing_int.py` was already on the pool:
bh-jvgj6 (2072633f) moved every cluster test to `rewound_cluster`, so the 1,249 test-seconds above
are post-migration. What stayed per test was the fence install: each test acquired a bare cluster
and ran `install_fence(cluster, "c")` itself. bh-t8hr4 pools that too. The module-level
`fenced_cluster` factory builds the cluster and installs the fence, and tests take it through
`rewound_custom`, so the fenced state is the template.

**Measured cost (probe on the shared host, two runs).** 3-frame composition: build 24-27 s, fence
install 4.0-4.5 s, restore 6.4 s. 2-frame composition: build 8-9 s, install 2.4-2.6 s, restore
0.2 s. The change saves one install per reused acquisition, about 4 s (3-frame) or 2.5 s
(2-frame).

**Why the file barely moves.** A rewind only pays off when one worker runs two tests of the same
composition. At `-n 4` the 10 cluster tests gave 5 reuses (about 18 test-seconds saved by this
change, on top of the build the pool already saved). On the 16-worker gate most tests land on
their own worker, so neither the pool nor this change saves much there. Builds are about 220 of
the file's roughly 1,050-1,250 test-seconds. The rest is the test bodies: real bd and Dolt
pushes, pulls and syncs. The levers that remain are splitting the longest cases (bh-56wtv) and
moving the pure protocol scenarios onto the in-memory fake (bh-utvyj).

**Runs** (`NO_COLOR=1 BH_DOLT_SLOTS=12 ./scripts/hermetic.sh uv run --no-sync python -m pytest -q
-p no:cacheprovider -n 4 --durations=0 tests/test_epoch_fencing_int.py`, 11 passed every time;
test-seconds are the summed setup/call/teardown durations). Load average ranged from 9 to 35
because a full gate was running, which is larger than the effect measured:

| Run | Wall (s) | Test-seconds |
| --- | ---: | ---: |
| Before (main 600596fc), unguarded, run 1 | 357 | 1,053 |
| Before (main 600596fc), unguarded, run 2 | 371 | 1,078 |
| After, `BH_CLUSTER_REWIND_GUARD=1`, run 1 | 409 | 1,211 |
| After, `BH_CLUSTER_REWIND_GUARD=1`, run 2 | 392 | 1,195 |
| After, `BH_CLUSTER_REWIND_GUARD=1`, run 3 | 351 | 1,033 |
| After, unguarded, run 1 | 399 | 1,173 |
| After, unguarded, run 2 | 416 | 1,245 |

The leak guard passed on all three guarded runs, so no test needs `cluster_destructive`.

## Dolt slot throttle and worksteal packing on the integration lane (bh-xqtp8)

**Question.** bh-56wtv's longest-first simulation predicted an integration-lane wall of total
work / 16, about 415-435 s. Green gates took 513-593 s. The hypothesis was that
`BH_DOLT_SLOTS=12` with 16 xdist workers makes workers queue for Dolt servers, or that slots are
held longer than needed.

**Answer.** The slots are not the main cause. Most of the gap is a scheduling defect: the xdist
`worksteal` scheduler defeats longest-first ordering, so long tests start late and most workers sit
idle for the last ~100 s. Two smaller costs are outside JUnit entirely: per-worker collection and
the Dolt-slot capacity bound. Packing worksteal's initial chunks fixes the tail. Measured wall:
**575-580 s before, 503-504 s after**.

### Instrumentation

`BH_TEST_SCHEDULE_TELEMETRY=1` (off by default, `tests/harness/schedule_telemetry.py`) records:

- each test's worker, start, end and setup/call/teardown time;
- each `dolt_server_slot` acquisition: queue wait, hold, and where in the test it was taken and
  released;
- each worker's collection time, idle gaps and idle tail;
- a host sample every 5 s: load average, MemAvailable, and live `dolt` processes in the fence.

The summary is written next to the JUnit report as `<report>.schedule.json`, and a one-line digest
is printed in the terminal summary. `scripts/benchmark_xdist.py` already reads the same slot events
via `BH_DOLT_SLOT_EVENTS`. Those events now also carry the worker id.

**Historical JUnit data.** This covers 18 green `-n 16 --dist worksteal -m "integration"` runs in
`.bh/validation/runs` (2026-10-10 to 2026-10-11). The gap between wall and JUnit test-seconds / 16
was 188 s median (range 122-274 s). JUnit cannot explain the gap, for three reasons:

- The slot wait is inside the test time, because the slot fixture runs in setup.
- Worker collection is outside every test.
- Idle workers record nothing.

### What the telemetry showed (baseline, main dff07a5a)

| Component | Per worker | Lane cost | Note |
| --- | ---: | ---: | --- |
| Collection before the first test | 21-44 s | 21-44 s of wall | Each worker imports all 581 test modules (11,851 items) to keep 279. Serial collection is 15 s; the rest is contention. |
| Idle tail after a worker's last test | 100-126 s mean | ~110-125 s of wall | Long tests start at 340-460 s on a few workers while the rest are done. |
| Dolt slot wait | 8-9 s median, 115-117 s max | 400-550 test-seconds | Concentrated on workers that reach a slot test while 12 long holders run. Saturated (all 12 held, someone queued) for only 176-201 s. |
| Slot released before test end | — | 1.7 s total | Slots are taken at test start and released at teardown end. Teardown of all holders sums to 39 s. |

**Why the tail happens.** On its first `schedule()`, worksteal hands worker *i* a contiguous chunk
of `len(pending) // (workers - i)` items. With longest-first order, one worker gets the 17 longest
tests. Others steal the tail half of its queue by count, not by duration. The long tests they steal
then start late on another worker. A discrete-event simulation of that scheduler with measured
durations reproduces the baseline wall: 536 s of test phase + 44 s of collection = 580 s.

**Why the cap does not explain the gap.** 202 of 279 tests hold a slot (the `dolt_server` marker
or a `rewound_*` fixture). That accounts for 75% of the work. Slot-holding work divided by 12 slots
is 431-438 s, close to total / 16 (396-403 s without slot wait). So with perfect packing the cap
costs at most ~35 s. The 150 s gap was mostly the tail.

### Candidates, one at a time

| Candidate | Result |
| --- | --- |
| Release slots earlier on teardown | **No-op.** Slots are released 1.7 s before test end in total. Nothing to move. |
| Pool-aware accounting for rewindable clusters | **Already true.** `ClusterPool.release` parks (stops) the cluster under the slot, so an idle pooled cluster holds no slot. Hold time equals test duration, with no hold outside a test. |
| Server sharing or pre-warming | **Not pursued.** Hold time is the test body (real bd and Dolt operations). Builds are a minor share (bh-t8hr4), and the pool already reuses them per worker. |
| Worksteal packing (`BH_TEST_WORKSTEAL_PACK=1`) | **Adopted for the integration land lane.** See below. Tail drops from 100-126 s to 29-34 s per worker. Wall drops by 72-77 s. |
| A different cap | **Not changed.** CPU has no headroom (see below). After packing, the slot bound (hold / 12 = 453-460 s) is only 7-40 s above the CPU ideal. |

**Packing** (`tests/harness/duration_order.py::pack` and `tests/harness/worksteal_pack.py`) lays out
the selected items as worksteal's exact initial chunks:

- Each chunk is a longest-processing-time bin of predicted work, from `test_durations.json`, which
  was refreshed from the 269 recent 16-worker lane reports.
- Within a chunk, tests run longest first, so a steal takes only short tests.
- With `BH_DOLT_SLOTS` < workers, slot-holding tests are binned onto `slots` chunks and slot-free
  tests onto the rest.

It runs `trylast` so that it orders the post-`-m` selection. xdist resets `config.option.dist` to
`"no"` on workers, so the controller exports its `--dist` as `BH_TEST_XDIST_DIST`. The first
"packed" run (`pack1` below) was silently a plain longest-first run because of this. It is opt-in
everywhere, and `test-integration-land` turns it on (`BH_TEST_WORKSTEAL_PACK=0 just
test-integration-land` turns it off). The stateful lane is unchanged, and measuring it there is a
follow-up.

### Runs

Every run: `NO_COLOR=1 BH_DOLT_SLOTS=12 BH_TEST_TIMEOUT_SECONDS=2400 BH_TEST_SCHEDULE_TELEMETRY=1
BH_TEST_REPORT_DIR=<dir> uv run python scripts/test-watchdog.py --timeout 2400 --
./scripts/hermetic.sh uv run python scripts/pytest_with_report.py -n 16 tests --dist worksteal -m
"integration"`, one lane at a time, on a shared 32-core / 47 GB host. Two other developers were
running `-n 4` Dolt tests. The runs differ as follows:

- Baselines set `BH_TEST_DURATIONS_FILE` to main's durations file. `base1` used main's file
  in-tree.
- `pack2` set `BH_TEST_WORKSTEAL_PACK=1`.
- `pack3` ran `just test-integration-land` itself.

All runs: 275 passed, 4 skipped, no failures.

| Run | Order | Load at start | Wall (s) | Busy / 16 (s) | Collection (s) | Tail per worker (s) | Slot wait total (s) | Slot wait per worker, median / max (s) | Slot hold total (s) | Slot hold per worker, median (min-max) (s) | Mean slots held | Load median / max | Min MemAvailable (GB) | Max `dolt` procs |
| --- | --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| base1 | main | 8.3 | 580 | 436 | 44 | 100 | 547 | 8 / 115 | 5,257 | 371 (94-534) | 9.8 | 27 / 36 | 15.1 | 21 |
| base2 | main | 26.2 | 575 | 428 | 21 | 126 | 401 | 9 / 117 | 5,234 | 355 (97-487) | 9.5 | 30 / 40 | 15.3 | 18 |
| pack1 | longest-first, refreshed durations (packing inert, see above) | 8.5 | 548 | 420 | 21 | 107 | 381 | 7 / 120 | 5,168 | 320 (102-497) | 9.9 | 27 / 35 | 15.5 | 21 |
| pack2 | packed | 14.7 | **504** | 453 | 22 | 29 | 507 | 17 / 126 | 5,521 | 408 (1-469) | 11.5 | 30 / 44 | 14.4 | 23 |
| pack3 | packed, via `just` | 20.0 | **503** | 447 | 22 | 34 | 541 | 34 / 101 | 5,440 | 389 (60-464) | 11.4 | 26 / 37 | 15.5 | 22 |

### Reading the numbers

- **Before and after.** Wall goes from 575-580 s to 503-504 s (-13%). The gap to busy / 16 falls
  from 144-147 s to 51-56 s, which closes about 63% of it.
  - Refreshing the durations alone (`pack1`) is worth about 30 s.
  - Packing is worth the rest.
- **What remains.** About 22 s of collection, ~30 s of tail, and the slot bound. Packed runs keep
  11.4-11.5 of 12 slots busy, so the lane is now slot-bound. Hold / 12 (453-460 s) + collection
  (22 s) ≈ 475-482 s, against 503 s measured.
  - Busy time rose 3-5%: more concurrent Dolt work is slightly slower per test.
- **Cap headroom.** Memory has room: at least 14.4 GB available, and at most 23 `dolt` processes
  in the fence. CPU does not: load median is 26-30 and peak is 36-44 on 32 cores, and that
  includes the other developers' work. A higher cap would add Dolt servers to an oversubscribed
  CPU. That is the condition behind load-induced timeouts like bh-u5b0e. It would also buy at most
  the 7-40 s between the slot bound and the CPU ideal. So `BH_DOLT_SLOTS=12` stays.
- **Flakes.** All five lanes were green, including both packed runs at the higher slot occupancy.
  bh-u5b0e is a stateful-lane test and is untouched by this change.
- **Next lever.** Collection, at 21-44 s per run. Each worker imports the whole suite to select
  ~2% of it. A safe pre-filter for marker-selected lanes would recover most of that.
