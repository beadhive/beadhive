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

The fast hub/HQ contracts remain covered by fake-based tests in `tests/test_hub.py`,
`tests/test_hub_bulk.py`, and `tests/test_hq.py`, including repository routing, source-prefix
handling, sync failure behavior, and HQ initialization ordering. The four real-store proofs above
are selected by `just test-integration-land`; they are not skipped or hidden behind a quarantine.

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
