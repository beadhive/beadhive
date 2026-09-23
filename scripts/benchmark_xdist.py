#!/usr/bin/env python3
"""Opt-in, reproducible pytest-xdist benchmark for the two land partitions."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import platform
import re
import shutil
import signal
import statistics
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SELECTIONS = {"unit": "not integration", "integration-land": "integration"}
SUMMARY_RE = re.compile(
    r"=+\s*(?P<body>.+?)\s+in\s+(?P<seconds>\d+(?:\.\d+)?)s"
    r"(?:\s+\([^)]*\))?\s*=+",
    re.MULTILINE,
)
COUNT_RE = re.compile(
    r"(?P<count>\d+)\s+(?P<kind>passed|failed|skipped|error(?:s)?|xfailed|xpassed)"
)
DURATION_RE = re.compile(
    r"^\s*(?P<seconds>\d+(?:\.\d+)?)s\s+(?P<phase>setup|call|teardown)\s+(?P<test>\S.*)$",
    re.MULTILINE,
)


class BenchmarkError(RuntimeError):
    """The benchmark configuration or pytest output is unsafe or malformed."""


def parse_workers(raw: str) -> list[int]:
    try:
        workers = [int(item.strip()) for item in raw.split(",")]
    except ValueError as exc:
        raise BenchmarkError("workers must be a comma-separated list of positive integers") from exc
    if not workers or any(worker <= 0 for worker in workers):
        raise BenchmarkError("workers must be a comma-separated list of positive integers")
    return workers


def parse_pytest_output(output: str, returncode: int) -> dict[str, Any]:
    matches = list(SUMMARY_RE.finditer(output))
    if not matches:
        raise BenchmarkError("pytest output has no parseable result summary")
    summary = matches[-1]
    counts = {key: 0 for key in ("passed", "skipped", "failed", "errors")}
    for match in COUNT_RE.finditer(summary.group("body")):
        kind = match.group("kind")
        key = "errors" if kind.startswith("error") else kind
        if key in counts:
            counts[key] += int(match.group("count"))
    if returncode and not counts["failed"] and not counts["errors"]:
        counts["errors"] = 1
    return {
        **counts,
        "pytest_elapsed_seconds": float(summary.group("seconds")),
        "slow_phases": [
            {
                "seconds": float(match.group("seconds")),
                "phase": match.group("phase"),
                "test": match.group("test"),
            }
            for match in DURATION_RE.finditer(output)
        ],
    }


def aggregate_runs(runs: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = {}
    for run in runs:
        grouped.setdefault((run["selection"], run["workers"]), []).append(run)
    return [
        {
            "selection": selection,
            "workers": workers,
            "repetitions": len(samples),
            "successful_repetitions": sum(sample["exit_code"] == 0 for sample in samples),
            "median_pytest_elapsed_seconds": statistics.median(
                sample["pytest_elapsed_seconds"] for sample in samples
            ),
            "median_external_wall_seconds": statistics.median(
                sample["external_wall_seconds"] for sample in samples
            ),
            "external_wall_spread_seconds": {
                "min": min(sample["external_wall_seconds"] for sample in samples),
                "max": max(sample["external_wall_seconds"] for sample in samples),
                "stdev": statistics.stdev(sample["external_wall_seconds"] for sample in samples)
                if len(samples) > 1
                else 0.0,
            },
            "pytest_elapsed_spread_seconds": {
                "min": min(sample["pytest_elapsed_seconds"] for sample in samples),
                "max": max(sample["pytest_elapsed_seconds"] for sample in samples),
                "stdev": statistics.stdev(sample["pytest_elapsed_seconds"] for sample in samples)
                if len(samples) > 1
                else 0.0,
            },
            "median_dolt_slot_queue_seconds": _median_observed_slot_metric(
                samples, "total_queue_seconds"
            ),
            "median_dolt_slot_hold_seconds": _median_observed_slot_metric(
                samples, "total_hold_seconds"
            ),
            "dolt_slot_telemetry_repetitions": sum(
                sample.get("dolt_slots", {}).get("available", False) for sample in samples
            ),
            "median_server_processes_started": statistics.median(
                sample.get("processes", {}).get("server_processes_started", 0) for sample in samples
            ),
            "median_max_active_servers": statistics.median(
                sample.get("processes", {}).get("max_active_server_processes", 0)
                for sample in samples
            ),
            "median_max_process_tree_count": statistics.median(
                sample.get("processes", {}).get("max_pytest_process_tree", 0) for sample in samples
            ),
        }
        for (selection, workers), samples in sorted(grouped.items())
    ]


def _median_observed_slot_metric(samples: Sequence[dict[str, Any]], key: str) -> float | None:
    values = [
        sample["dolt_slots"][key]
        for sample in samples
        if sample.get("dolt_slots", {}).get("available", False)
    ]
    return statistics.median(values) if values else None


def parse_dolt_slot_events(path: Path) -> dict[str, Any]:
    tests: dict[str, dict[str, float]] = {}
    event_count = 0
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines()[:10000]:
            event = json.loads(line)
            event_count += 1
            row = tests.setdefault(event["test"], {"queue_seconds": 0.0, "hold_seconds": 0.0})
            if event["event"] == "acquired":
                row["queue_seconds"] = event["queue_seconds"]
            elif event["event"] == "released":
                row["hold_seconds"] = event["hold_seconds"]
    return {
        "available": event_count > 0,
        "event_count": event_count,
        "tests": tests,
        "total_queue_seconds": sum(row["queue_seconds"] for row in tests.values()),
        "total_hold_seconds": sum(row["hold_seconds"] for row in tests.values()),
    }


def ensure_external_scratch(path: Path) -> Path:
    path = path.expanduser().resolve()
    path.mkdir(parents=True, exist_ok=True)
    discovered = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
        text=True,
        capture_output=True,
        check=False,
    )
    if discovered.returncode == 0:
        raise BenchmarkError(
            f"unsafe scratch root {path}: Git discovers parent repository "
            f"{discovered.stdout.strip()}; choose a directory outside every checkout"
        )
    return path


def package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not-installed"


def command_output(command: Sequence[str], cwd: Path = ROOT) -> str:
    result = subprocess.run(command, cwd=cwd, text=True, capture_output=True, check=True)
    return result.stdout.strip()


def memory_limit_bytes() -> int:
    candidates: list[int] = []
    for path in (
        Path("/sys/fs/cgroup/memory.max"),
        Path("/sys/fs/cgroup/memory/memory.limit_in_bytes"),
    ):
        try:
            value = path.read_text(encoding="utf-8").strip()
            if value != "max":
                candidates.append(int(value))
        except (OSError, ValueError):
            pass
    physical = os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    plausible = [value for value in candidates if 0 < value < (1 << 60)]
    return min([physical, *plausible])


def device(path: Path) -> dict[str, Any]:
    capacity = os.statvfs(path)
    return {
        "path": str(path),
        "device_id": path.stat().st_dev,
        "available_bytes": capacity.f_bavail * capacity.f_frsize,
        "available_inodes": capacity.f_favail,
    }


def parse_materialization_observation(output: str, returncode: int, cache: Path) -> dict[str, Any]:
    if returncode != 0:
        return {
            "observed": False,
            "link_mode": "unknown",
            "hardlinks_used": "unknown",
            "fallback_copies_used": "unknown",
            "diagnostic": output.strip()[-1000:],
        }
    fallback = "falling back to full copy" in output or "falling back to copy" in output
    cache_archive = f"{cache}/archive-v0/" in output
    hardlink_attempt = "Failed to hard link" in output or "Failed to hardlink" in output
    if fallback and cache_archive and hardlink_attempt:
        return {
            "observed": True,
            "link_mode": "copy-fallback",
            "hardlinks_used": False,
            "fallback_copies_used": True,
            "evidence": "uv cache archive -> isolated target: hardlink failed; copied",
        }
    return {
        "observed": False,
        "link_mode": "unknown",
        "hardlinks_used": "unknown",
        "fallback_copies_used": "unknown",
        "diagnostic": "uv install succeeded without an explicit cache-to-target method trace",
    }


def observe_uv_materialization(scratch: Path, cache: Path, root: Path) -> dict[str, Any]:
    probe = Path(tempfile.mkdtemp(prefix="bh-xdist-uv-probe-", dir=scratch))
    try:
        wheels = probe / "wheels"
        target = probe / "target"
        target.mkdir()
        build = subprocess.run(
            ["uv", "build", "--wheel", "--offline", "--out-dir", str(wheels)],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        wheel = next(wheels.glob("*.whl"), None)
        if build.returncode != 0 or wheel is None:
            observed = parse_materialization_observation(
                build.stdout + build.stderr, build.returncode or 1, cache
            )
            return {**observed, "target": device(target)}
        install = subprocess.run(
            [
                "uv",
                "pip",
                "install",
                "--target",
                str(target),
                "--offline",
                "--no-deps",
                "--reinstall",
                "--verbose",
                str(wheel),
            ],
            cwd=root,
            text=True,
            capture_output=True,
            check=False,
        )
        observed = parse_materialization_observation(
            install.stdout + install.stderr, install.returncode, cache
        )
        return {**observed, "target": device(target)}
    finally:
        shutil.rmtree(probe, ignore_errors=True)


def cache_locality(scratch: Path, root: Path) -> dict[str, Any]:
    cache = Path(command_output(["uv", "cache", "dir"], cwd=root)).resolve()
    environment = root / ".venv"
    target = (environment if environment.exists() else Path(sys.prefix)).resolve()
    same_device = cache.stat().st_dev == target.stat().st_dev
    requested = os.environ.get("UV_LINK_MODE", "auto")
    return {
        "cache": device(cache),
        "target": device(target),
        "same_device": same_device,
        "requested_link_mode": requested,
        "hardlinks_supported_by_device_layout": same_device,
        "expected_auto_mode": "hardlink" if same_device else "copy-fallback",
        "materialization_observation": observe_uv_materialization(scratch, cache, root),
    }


def effective_cpu_count() -> int:
    try:
        return len(os.sched_getaffinity(0))
    except AttributeError:
        return os.cpu_count() or 1


def validation_slots(root: Path) -> dict[str, Any]:
    override = os.environ.get("BH_VALIDATION_SLOTS")
    if override is not None:
        return {"effective": int(override), "source": "BH_VALIDATION_SLOTS"}
    result = subprocess.run(
        ["bh", "config", "get", "work.validation_slots", "--scope", "host"],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip():
        return {"effective": int(result.stdout.strip()), "source": "host-config"}
    return {"effective": 1, "source": "default"}


def provenance(scratch: Path, root: Path) -> dict[str, Any]:
    return {
        "checkout": str(root),
        "commit": command_output(["git", "rev-parse", "HEAD"], cwd=root),
        "tree": command_output(["git", "rev-parse", "HEAD^{tree}"], cwd=root),
        "dirty": bool(
            command_output(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root)
        ),
        "platform": platform.platform(),
        "python_executable": sys.executable,
        "python_version": platform.python_version(),
        "pytest_version": package_version("pytest"),
        "xdist_version": package_version("pytest-xdist"),
        "effective_cpu_count": effective_cpu_count(),
        "effective_memory_bytes": memory_limit_bytes(),
        "validation_slots": validation_slots(root),
        "xdist_auto_worker_cap": os.environ.get("PYTEST_XDIST_AUTO_NUM_WORKERS", "unset"),
        "cache_locality": cache_locality(scratch, root),
    }


def run_process_metrics(scratch: Path, root_pid: int) -> dict[str, Any]:
    """Sample pytest descendants and Dolt servers configured below this run's scratch root."""
    proc_root = Path("/proc")
    if not proc_root.is_dir():
        return {
            "server_pids_seen": [],
            "active_server_count": 0,
            "process_tree_count": 0,
        }
    children: dict[int, list[int]] = {}
    process_rows: dict[int, tuple[int, list[str]]] = {}
    for entry in proc_root.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            fields = (entry / "stat").read_text(encoding="utf-8").rsplit(")", 1)[1].split()
            if fields[0] != "Z":
                pid = int(entry.name)
                parent_pid = int(fields[1])
                cmdline = (entry / "cmdline").read_bytes().split(b"\0")
                args = [arg.decode(errors="replace") for arg in cmdline if arg]
                process_rows[pid] = (parent_pid, args)
                children.setdefault(parent_pid, []).append(pid)
        except (OSError, ValueError, IndexError):
            continue
    pending = [root_pid]
    seen: set[int] = set()
    while pending:
        pid = pending.pop()
        if pid in seen:
            continue
        seen.add(pid)
        pending.extend(children.get(pid, ()))
    root_text = str(scratch.resolve())
    server_pids = {
        pid
        for pid, (_parent_pid, args) in process_rows.items()
        if root_text in " ".join(args)
        and any("dolt" in Path(arg).name.lower() for arg in args[:2])
        and "sql-server" in args
    }
    return {
        "server_pids_seen": sorted(server_pids),
        "active_server_count": len(server_pids),
        "process_tree_count": len(seen),
    }


def write_payload(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(path)


def render_table(aggregates: Sequence[dict[str, Any]], run_provenance: dict[str, Any]) -> str:
    lines = [
        "| selection | workers | ok/runs | median pytest (s) | median wall (s) "
        "| Dolt queue (s) | Dolt hold (s) | servers (max) | processes (max) |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for row in aggregates:
        queue = row.get("median_dolt_slot_queue_seconds")
        hold = row.get("median_dolt_slot_hold_seconds")
        queue_text = f"{queue:.3f}" if queue is not None else "n/a"
        hold_text = f"{hold:.3f}" if hold is not None else "n/a"
        lines.append(
            f"| {row['selection']} | {row['workers']} | {row['successful_repetitions']}/"
            f"{row['repetitions']} | {row['median_pytest_elapsed_seconds']:.3f} | "
            f"{row['median_external_wall_seconds']:.3f} | "
            f"{queue_text} | {hold_text} | "
            f"{row.get('median_max_active_servers', 0.0):.1f} | "
            f"{row.get('median_max_process_tree_count', 0.0):.1f} |"
        )
    locality = run_provenance["cache_locality"]
    cache = locality["cache"]
    target = locality["target"]
    observed = locality["materialization_observation"]
    probe_target = observed.get("target")
    lines.extend(
        [
            "",
            "| locality | device | available bytes | available inodes |",
            "|---|---:|---:|---:|",
            f"| uv cache | {cache['device_id']} | {cache['available_bytes']} | "
            f"{cache['available_inodes']} |",
            f"| interpreter target | {target['device_id']} | {target['available_bytes']} | "
            f"{target['available_inodes']} |",
            "",
            f"uv link mode: requested `{locality['requested_link_mode']}`, expected "
            f"`{locality['expected_auto_mode']}`, observed `{observed['link_mode']}` "
            f"(hardlinks used: `{observed['hardlinks_used']}`, fallback copies used: "
            f"`{observed['fallback_copies_used']}`).",
        ]
    )
    if probe_target:
        lines.insert(
            -2,
            f"| uv probe target | {probe_target['device_id']} | "
            f"{probe_target['available_bytes']} | {probe_target['available_inodes']} |",
        )
    return "\n".join(lines)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--workers", default="6,12,18,24", help="explicit worker matrix")
    result.add_argument("--repetitions", type=int, default=3)
    result.add_argument("--selection", choices=(*SELECTIONS, "all"), default="all")
    result.add_argument(
        "--deselect",
        action="append",
        default=[],
        help="pytest node ID to exclude (may be repeated; retained in the JSON configuration)",
    )
    result.add_argument(
        "--checkout",
        type=Path,
        default=ROOT,
        help="source checkout to benchmark (defaults to the checkout containing this script)",
    )
    result.add_argument("--scratch-root", type=Path, default=Path(tempfile.gettempdir()))
    result.add_argument("--output", type=Path, default=ROOT / "xdist-benchmark.json")
    return result


def main(argv: Sequence[str] | None = None) -> int:
    options = parser().parse_args(argv)
    try:
        workers = parse_workers(options.workers)
        if options.repetitions <= 0:
            raise BenchmarkError("repetitions must be a positive integer")
        scratch = ensure_external_scratch(options.scratch_root)
        root = options.checkout.expanduser().resolve()
        if not (root / "pyproject.toml").is_file():
            raise BenchmarkError(f"checkout does not contain pyproject.toml: {root}")
        if command_output(["git", "status", "--porcelain", "--untracked-files=normal"], cwd=root):
            raise BenchmarkError(f"checkout must be clean for benchmark comparison: {root}")
        selections = (
            SELECTIONS
            if options.selection == "all"
            else {options.selection: SELECTIONS[options.selection]}
        )
        payload: dict[str, Any] = {
            "schema_version": 1,
            "provenance": provenance(scratch, root),
            "configuration": {
                "worker_matrix": workers,
                "repetitions": options.repetitions,
                "selections": selections,
                "scratch_root": str(scratch),
                "checkout": str(root),
                "deselected_tests": options.deselect,
            },
            "runs": [],
            "complete": False,
        }
        output_path = options.output.resolve()
        write_payload(output_path, payload)
        any_failed = False
        for selection, marker in selections.items():
            for worker in workers:
                for repetition in range(1, options.repetitions + 1):
                    run_scratch = Path(tempfile.mkdtemp(prefix="bh-xdist-", dir=scratch))
                    command = [
                        sys.executable,
                        "-m",
                        "pytest",
                        "-n",
                        str(worker),
                        "-m",
                        marker,
                    ]
                    for nodeid in options.deselect:
                        command.extend(("--deselect", nodeid))
                    env = os.environ.copy()
                    env["TMPDIR"] = str(run_scratch)
                    existing_pythonpath = env.get("PYTHONPATH")
                    env["PYTHONPATH"] = str(root / "src") + (
                        os.pathsep + existing_pythonpath if existing_pythonpath else ""
                    )
                    slot_events = run_scratch / "dolt-slot-events.jsonl"
                    env["BH_DOLT_SLOT_EVENTS"] = str(slot_events)
                    started = time.monotonic()
                    stdout_path = run_scratch / "pytest.stdout.log"
                    stderr_path = run_scratch / "pytest.stderr.log"
                    max_servers = 0
                    max_processes = 1
                    server_pids: set[int] = set()
                    with (
                        stdout_path.open("w", encoding="utf-8") as stdout_file,
                        stderr_path.open("w", encoding="utf-8") as stderr_file,
                    ):
                        completed = subprocess.Popen(
                            command,
                            cwd=root,
                            env=env,
                            text=True,
                            stdout=stdout_file,
                            stderr=stderr_file,
                            start_new_session=True,
                        )
                        try:
                            while completed.poll() is None:
                                metrics = run_process_metrics(run_scratch, completed.pid)
                                server_pids.update(metrics["server_pids_seen"])
                                max_servers = max(max_servers, metrics["active_server_count"])
                                max_processes = max(max_processes, metrics["process_tree_count"])
                                time.sleep(0.2)
                        except KeyboardInterrupt:
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(completed.pid, signal.SIGTERM)
                            try:
                                completed.wait(timeout=10)
                            except subprocess.TimeoutExpired:
                                with contextlib.suppress(ProcessLookupError):
                                    os.killpg(completed.pid, signal.SIGKILL)
                                completed.wait()
                            final_servers = run_process_metrics(run_scratch, completed.pid)[
                                "server_pids_seen"
                            ]
                            for pid in server_pids | set(final_servers):
                                with contextlib.suppress(ProcessLookupError):
                                    os.kill(pid, signal.SIGTERM)
                            raise
                    stdout = stdout_path.read_text(encoding="utf-8")
                    stderr = stderr_path.read_text(encoding="utf-8")
                    final_metrics = run_process_metrics(run_scratch, completed.pid)
                    server_pids.update(final_metrics["server_pids_seen"])
                    max_servers = max(max_servers, final_metrics["active_server_count"])
                    max_processes = max(max_processes, final_metrics["process_tree_count"])
                    output = stdout + "\n" + stderr
                    try:
                        parsed = parse_pytest_output(output, completed.returncode)
                    except BenchmarkError as exc:
                        failed_run = {
                            "selection": selection,
                            "marker": marker,
                            "workers": worker,
                            "repetition": repetition,
                            "command": command,
                            "exit_code": completed.returncode,
                            "external_wall_seconds": round(time.monotonic() - started, 3),
                            "summary_missing": True,
                            "summary_error": str(exc),
                            "stdout_log": str(stdout_path),
                            "stderr_log": str(stderr_path),
                            "terminal_output_tail": output[-12000:],
                            "dolt_slots": parse_dolt_slot_events(slot_events),
                            "processes": {
                                "server_pids_seen": sorted(server_pids),
                                "server_processes_started": len(server_pids),
                                "max_active_server_processes": max_servers,
                                "max_pytest_process_tree": max_processes,
                            },
                            "passed": 0,
                            "skipped": 0,
                            "failed": 0,
                            "errors": 1,
                            "pytest_elapsed_seconds": 0.0,
                            "slow_phases": [],
                        }
                        payload["runs"].append(failed_run)
                        payload["aggregates"] = aggregate_runs(payload["runs"])
                        write_payload(output_path, payload)
                        raise BenchmarkError(
                            f"{exc}; child exit={completed.returncode}; logs: "
                            f"{stdout_path}, {stderr_path}"
                        ) from exc
                    elapsed = round(time.monotonic() - started, 3)
                    run_result = {
                        "selection": selection,
                        "marker": marker,
                        "workers": worker,
                        "repetition": repetition,
                        "command": command,
                        "exit_code": completed.returncode,
                        "external_wall_seconds": elapsed,
                        "dolt_slots": parse_dolt_slot_events(slot_events),
                        "processes": {
                            "server_pids_seen": sorted(server_pids),
                            "server_processes_started": len(server_pids),
                            "max_active_server_processes": max_servers,
                            "max_pytest_process_tree": max_processes,
                        },
                        **parsed,
                    }
                    if completed.returncode != 0:
                        run_result.update(
                            {
                                "stdout_log": str(stdout_path),
                                "stderr_log": str(stderr_path),
                                "terminal_output_tail": output[-12000:],
                            }
                        )
                    payload["runs"].append(run_result)
                    any_failed |= completed.returncode != 0
                    payload["aggregates"] = aggregate_runs(payload["runs"])
                    write_payload(output_path, payload)
                    print(
                        f"{selection} n={worker} rep={repetition}: "
                        f"exit={completed.returncode} "
                        f"pytest={parsed['pytest_elapsed_seconds']:.3f}s "
                        f"wall={elapsed:.3f}s servers={len(server_pids)} max={max_servers}",
                        flush=True,
                    )
                    if completed.returncode == 0:
                        shutil.rmtree(run_scratch, ignore_errors=True)
        payload["aggregates"] = aggregate_runs(payload["runs"])
        payload["complete"] = True
        write_payload(output_path, payload)
        print(render_table(payload["aggregates"], payload["provenance"]))
        print(f"\nJSON: {output_path}")
        return 1 if any_failed else 0
    except (BenchmarkError, OSError, subprocess.SubprocessError) as exc:
        print(f"xdist-benchmark: FAILED: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
