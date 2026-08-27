#!/usr/bin/env python3
"""Run a test command with a fail-closed hang timeout and process diagnostics."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

TIMEOUT_EXIT = 124


@dataclass(frozen=True)
class ProcessInfo:
    pid: int
    parent: int
    state: str
    elapsed: str
    command: str


@dataclass(frozen=True)
class ActiveTest:
    pid: int
    worker: str
    nodeid: str | None
    signal_ready: bool


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


def _active_tests(active_dir: Path, descendant_pids: set[int]) -> list[ActiveTest]:
    active: list[ActiveTest] = []
    for path in active_dir.glob("*.json"):
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            pid = int(value["pid"])
            worker = str(value["worker"])
            nodeid = value.get("nodeid")
            signal_ready = value.get("signal_ready") is True
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            continue
        if pid not in descendant_pids or path.name != f"{pid}.json":
            continue
        if nodeid is not None and not isinstance(nodeid, str):
            continue
        active.append(
            ActiveTest(
                pid=pid,
                worker="".join(char for char in worker if char.isalnum() or char in "-_")[:32],
                nodeid=nodeid[:512] if nodeid else None,
                signal_ready=signal_ready,
            )
        )
    return sorted(active, key=lambda item: (item.worker, item.pid))


def _diagnose(process: subprocess.Popen[bytes], active_dir: Path) -> list[ActiveTest]:
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
    active = _active_tests(active_dir, {row.pid for row in rows})
    print("active pytest tests (parameter values omitted):", file=sys.stderr)
    if active:
        for item in active:
            print(
                f"  pid {item.pid} worker {item.worker or '?'}: {item.nodeid or 'idle'}",
                file=sys.stderr,
            )
    else:
        print("  no registered pytest processes", file=sys.stderr)
    if hasattr(signal, "SIGUSR1"):
        registered = [item for item in active if item.signal_ready]
        print(
            f"requesting registered pytest stacks from {len(registered)} process(es)",
            file=sys.stderr,
            flush=True,
        )
        for item in registered:
            try:
                os.kill(item.pid, signal.SIGUSR1)
            except OSError:
                pass
    return active


def _print_pytest_stacks(active_dir: Path, active: list[ActiveTest]) -> None:
    print("pytest stack diagnostics (arguments and locals omitted):", file=sys.stderr)
    printed = False
    for item in active:
        path = active_dir / f"{item.pid}.stack"
        try:
            stack = path.read_text(encoding="utf-8")
        except OSError:
            continue
        if not stack:
            continue
        printed = True
        print(f"--- pid {item.pid} worker {item.worker or '?'} ---", file=sys.stderr)
        print(stack[-65536:], file=sys.stderr, end="" if stack.endswith("\n") else "\n")
    if not printed:
        print("  no registered pytest stacks were captured", file=sys.stderr)


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_process_group(
    process: subprocess.Popen[bytes], process_group: int, timeout: float
) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        process.poll()  # Reap an exited leader; descendants keep the process group alive.
        if not _process_group_exists(process_group):
            return True
        time.sleep(0.02)
    process.poll()
    return not _process_group_exists(process_group)


def _signal_process_group(process_group: int, sig: signal.Signals) -> None:
    try:
        os.killpg(process_group, sig)
    except OSError:
        pass


def _stop(process: subprocess.Popen[bytes], grace: float, *, process_group: int | None) -> None:
    if process_group is not None:
        _signal_process_group(process_group, signal.SIGTERM)
        if not _wait_for_process_group(process, process_group, grace):
            _signal_process_group(process_group, signal.SIGKILL)
            _wait_for_process_group(process, process_group, max(grace, 1.0))
        if process.poll() is None:
            process.wait(timeout=max(grace, 1.0))
        return

    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=grace)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=max(grace, 1.0))


def run(command: list[str], *, timeout: float, grace: float) -> int:
    if not command:
        raise ValueError("a command is required after --")
    with tempfile.TemporaryDirectory(prefix="bh-test-watchdog-") as active_root:
        active_dir = Path(active_root)
        child_env = os.environ.copy()
        child_env["BH_TEST_ACTIVE_DIR"] = str(active_dir)
        process = subprocess.Popen(command, start_new_session=os.name == "posix", env=child_env)
        process_group = process.pid if os.name == "posix" else None
        try:
            return process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            active = _diagnose(process, active_dir)
            time.sleep(grace)
            _print_pytest_stacks(active_dir, active)
            _stop(process, grace, process_group=process_group)
            print(f"test watchdog exiting nonzero ({TIMEOUT_EXIT}) after timeout", file=sys.stderr)
            return TIMEOUT_EXIT
        except KeyboardInterrupt:
            print("test watchdog interrupted; terminating child process tree", file=sys.stderr)
            _stop(process, grace, process_group=process_group)
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
