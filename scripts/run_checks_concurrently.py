#!/usr/bin/env python3
"""Run independent read-only check commands through a small bounded pool.

Each positional argument is one command line (split with ``shlex``, never a shell). Output of a
check is buffered and printed as one block when it finishes, so concurrent checks cannot
interleave their diagnostics. Every check always runs to completion, so one failing check never
hides another's report; the exit status is non-zero when any check failed (the first failing
command's own status, in the order the commands were given).

Callers own the ordering contract: anything that WRITES evidence a check reads must run before
this script, never inside it.
"""

from __future__ import annotations

import argparse
import shlex
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor

DEFAULT_JOBS = 4
_print_lock = threading.Lock()


def _run(command: str) -> tuple[str, int, float, str]:
    started = time.monotonic()
    completed = subprocess.run(
        shlex.split(command),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        errors="replace",
        check=False,
    )
    elapsed = time.monotonic() - started
    with _print_lock:
        status = "ok" if completed.returncode == 0 else f"FAILED (exit {completed.returncode})"
        print(f"==> {command} [{status}, {elapsed:.1f}s]", flush=True)
        if completed.stdout:
            sys.stdout.write(completed.stdout)
            if not completed.stdout.endswith("\n"):
                sys.stdout.write("\n")
        sys.stdout.flush()
    return command, completed.returncode, elapsed, completed.stdout


def run_checks(commands: list[str], jobs: int) -> int:
    """Run ``commands`` with at most ``jobs`` in flight; return the process exit status."""
    if jobs < 1:
        raise ValueError("--jobs must be >= 1")
    with ThreadPoolExecutor(max_workers=min(jobs, max(len(commands), 1))) as pool:
        results = list(pool.map(_run, commands))
    failed = [(command, code) for command, code, _, _ in results if code != 0]
    if failed:
        print(f"{len(failed)} of {len(results)} checks failed:", file=sys.stderr)
        for command, code in failed:
            print(f"  exit {code}: {command}", file=sys.stderr)
        return failed[0][1] or 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--jobs", type=int, default=DEFAULT_JOBS, help="max concurrent checks")
    parser.add_argument("commands", nargs="+", help="one command line per check")
    args = parser.parse_args(argv)
    return run_checks(args.commands, args.jobs)


if __name__ == "__main__":
    sys.exit(main())
