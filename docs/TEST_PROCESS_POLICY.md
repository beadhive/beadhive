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

## Hang watchdog

The parallel `just test` and `just test-integration-land` recipes run under
`scripts/test-watchdog.py`. The default deadline is 900 seconds and can be changed explicitly with
`BH_TEST_TIMEOUT_SECONDS`. A timeout:

1. prints the descendant PID, parent PID, state, elapsed time, and executable without dumping
   potentially secret command arguments;
2. asks descendant Python processes for all-thread stacks through `SIGUSR1`;
3. terminates the complete process group, checking group liveness independently of the command
   leader and escalating surviving descendants to a kill after the grace period; and
4. exits 124, never zero.

Pytest controllers and xdist workers register `SIGUSR1` with `faulthandler`, so the stack request
is actionable on macOS and Linux. On a future hang, retain the timeout output, identify the worker
and child that stopped progressing, rerun the focused selection with `-n 2` and `-n 6`, then use
`-n 0` only as a comparison. A serial pass does not waive a parallel failure.
