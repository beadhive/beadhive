#!/usr/bin/env python3
"""Run a test command with a fail-closed hang timeout and process diagnostics."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from dataclasses import dataclass

TIMEOUT_EXIT = 124


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    parent: int
    state: str
    elapsed: str
    command: str


def _process_table() -> list[ProcessInfo]:
    try:
        result = subprocess.run(
            ["ps", "-axo", "pid=,ppid=,state=,etime=,comm="],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"child process diagnostics unavailable: {type(exc).__name__}", file=sys.stderr)
        return []
    rows = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(maxsplit=4)
        if len(fields) != 5:
            continue
        try:
            rows.append(ProcessInfo(int(fields[0]), int(fields[1]), *fields[2:]))
        except ValueError:
            continue
    return rows


def _descendants(root: int, rows: list[ProcessInfo]) -> list[ProcessInfo]:
    selected: list[ProcessInfo] = []
    parents = {root}
    remaining = list(rows)
    while parents:
        children = [row for row in remaining if row.parent in parents]
        selected.extend(children)
        remaining = [row for row in remaining if row not in children]
        parents = {row.pid for row in children}
    root_row = next((row for row in rows if row.pid == root), None)
    return ([root_row] if root_row is not None else []) + selected


def _diagnose(process: subprocess.Popen[bytes]) -> None:
    print(
        f"\n=== TEST WATCHDOG TIMEOUT: pid {process.pid} did not finish ===",
        file=sys.stderr,
        flush=True,
    )
    rows = _descendants(process.pid, _process_table())
    print("child process diagnostics (pid ppid state elapsed executable):", file=sys.stderr)
    if rows:
        for row in rows:
            print(
                f"  {row.pid} {row.parent} {row.state} {row.elapsed} {row.command}",
                file=sys.stderr,
            )
    else:
        print("  no live descendants found", file=sys.stderr)
    if hasattr(signal, "SIGUSR1"):
        python_rows = [row for row in rows if "python" in row.command.lower()]
        print(
            f"requesting Python all-thread stacks from {len(python_rows)} process(es)",
            file=sys.stderr,
            flush=True,
        )
        for row in python_rows:
            try:
                os.kill(row.pid, signal.SIGUSR1)
            except OSError:
                pass


def _stop(process: subprocess.Popen[bytes], grace: float) -> None:
    if process.poll() is not None:
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    else:
        process.terminate()
    try:
        process.wait(timeout=grace)
        return
    except subprocess.TimeoutExpired:
        pass
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    else:
        process.kill()
    process.wait(timeout=max(grace, 1.0))


def run(command: list[str], *, timeout: float, grace: float) -> int:
    if not command:
        raise ValueError("a command is required after --")
    process = subprocess.Popen(command, start_new_session=os.name == "posix")
    try:
        return process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        _diagnose(process)
        time.sleep(grace)
        _stop(process, grace)
        print(f"test watchdog exiting nonzero ({TIMEOUT_EXIT}) after timeout", file=sys.stderr)
        return TIMEOUT_EXIT
    except KeyboardInterrupt:
        print("test watchdog interrupted; terminating child process tree", file=sys.stderr)
        _stop(process, grace)
        return 130


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--timeout", type=float, required=True)
    parser.add_argument("--grace", type=float, default=2.0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if args.timeout <= 0 or args.grace < 0:
        parser.error("--timeout must be positive and --grace must be non-negative")
    return run(command, timeout=args.timeout, grace=args.grace)


if __name__ == "__main__":
    raise SystemExit(main())
