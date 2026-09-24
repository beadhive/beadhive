#!/usr/bin/env python3
"""Reproducible cold/warm package-cache locality benchmark.

Each repetition gets a fresh application cache through the normal resolver.  The requested
package manager populates it through its supported command; this script never inspects, copies,
or removes opaque cache entries.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import statistics
import subprocess
import time
from collections.abc import Sequence
from pathlib import Path

from beadhive.cache_locality import adapter_for_application, resolve_cache


def _existing_path(path: Path) -> Path:
    """Return the nearest existing ancestor when a dependency target is not created yet."""
    probe = path
    while not probe.exists():
        probe = probe.parent
    return probe


def _capacity(path: Path) -> dict[str, int]:
    info = os.statvfs(_existing_path(path))
    return {
        "available_bytes": info.f_bavail * info.f_frsize,
        "available_inodes": info.f_favail,
    }


def _hardlink_observation(target: Path, cache_device: int) -> dict[str, object]:
    """Inspect installed-target metadata only; never walk opaque cache contents."""
    if not target.exists():
        return {
            "available": False,
            "files": 0,
            "files_with_multiple_links": 0,
            "hardlink_identities": [],
        }
    files = 0
    hardlink_identities: list[dict[str, int | str]] = []
    for path in target.rglob("*"):
        try:
            if path.is_symlink() or not path.is_file():
                continue
            files += 1
            info = path.stat()
            if info.st_nlink > 1 and info.st_dev == cache_device:
                hardlink_identities.append(
                    {
                        "path": str(path.relative_to(target)),
                        "device": info.st_dev,
                        "inode": info.st_ino,
                        "link_count": info.st_nlink,
                    }
                )
        except OSError:
            continue
    return {
        "available": True,
        "files": files,
        "files_with_multiple_links": len(hardlink_identities),
        "hardlink_identities": hardlink_identities,
    }


def _observation(command: Sequence[str], checkout: Path, env: dict[str, str], selection) -> dict:
    cache_before_path = _existing_path(selection.path)
    cache_before = _capacity(cache_before_path)
    shared_device = selection.cache_device == selection.target_device
    target_before_path = None if shared_device else _existing_path(selection.target)
    target_before = None if shared_device else _capacity(target_before_path)
    started = time.perf_counter()
    result = subprocess.run(
        command,
        cwd=checkout,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    elapsed = time.perf_counter() - started
    cache_after_path = _existing_path(selection.path)
    cache_after = _capacity(cache_after_path)
    target_after_path = None if shared_device else _existing_path(selection.target)
    target_after = None if shared_device else _capacity(target_after_path)
    output = f"{result.stdout}\n{result.stderr}"
    link_metadata = _hardlink_observation(selection.target, selection.cache_device)
    return {
        "seconds": elapsed,
        "returncode": result.returncode,
        "materialization": {
            "configured_method": selection.link_method,
            "same_device": shared_device,
            "hardlink_use_observed": (
                link_metadata["files_with_multiple_links"] > 0
                if link_metadata["available"] and selection.link_method == "hardlink"
                else False
                if link_metadata["available"] and selection.link_method == "copy"
                else None
            ),
            "hardlinked_target_files": link_metadata["files_with_multiple_links"],
            "hardlink_identities": link_metadata["hardlink_identities"],
            "target_files_inspected": link_metadata["files"],
            "cross_device_copy_fallback_observed": (
                result.returncode == 0 and not shared_device and selection.link_method == "copy"
            ),
        },
        "capacity": {
            "cache": {
                "root": str(selection.path),
                "sampled_from_before": str(cache_before_path),
                "sampled_from_after": str(cache_after_path),
                "device": selection.cache_device,
                "before": cache_before,
                "after": cache_after,
                "bytes_consumed": (
                    cache_before["available_bytes"] - cache_after["available_bytes"]
                ),
                "inodes_consumed": (
                    cache_before["available_inodes"] - cache_after["available_inodes"]
                ),
            },
            "target": {
                "root": str(selection.target),
                "sampled_from_before": None if shared_device else str(target_before_path),
                "sampled_from_after": None if shared_device else str(target_after_path),
                "device": selection.target_device,
                "same_device_as": "cache" if shared_device else None,
                "before": target_before,
                "after": target_after,
                "bytes_consumed": (
                    None
                    if shared_device
                    else target_before["available_bytes"] - target_after["available_bytes"]
                ),
                "inodes_consumed": (
                    None
                    if shared_device
                    else target_before["available_inodes"] - target_after["available_inodes"]
                ),
            },
        },
        "cache_hit_reported": any(word in output.lower() for word in ("cache hit", "cached")),
        "warnings": [line for line in output.splitlines() if "warning" in line.lower()],
    }


@contextlib.contextmanager
def _fresh_checkout(source: Path, repetition: int):
    """Create one detached dependency target without touching the source checkout's state."""
    target = source.parent / f".bh-cache-benchmark-{os.getpid()}-{repetition + 1}"
    if target.exists():
        raise RuntimeError(f"benchmark checkout already exists: {target}")
    added = subprocess.run(
        ["git", "-C", str(source), "worktree", "add", "--detach", str(target), "HEAD"],
        text=True,
        capture_output=True,
        check=False,
    )
    if added.returncode:
        raise RuntimeError(f"could not create fresh benchmark checkout: {added.stderr.strip()}")
    try:
        yield target
    finally:
        removed = subprocess.run(
            ["git", "-C", str(source), "worktree", "remove", "--force", str(target)],
            text=True,
            capture_output=True,
            check=False,
        )
        if removed.returncode:
            raise RuntimeError(
                f"could not remove fresh benchmark checkout: {removed.stderr.strip()}"
            )


def benchmark(options) -> dict[str, object]:
    adapter = adapter_for_application(options.application)
    checkout = options.checkout.resolve()
    rows: list[dict[str, object]] = []
    for repetition in range(options.repetitions):
        with _fresh_checkout(checkout, repetition) as target_checkout:
            env = os.environ.copy()
            # The benchmark intentionally installs into a fresh detached checkout, not the
            # caller's active environment. Avoid uv's misleading VIRTUAL_ENV mismatch warning.
            env.pop("VIRTUAL_ENV", None)
            for key in (
                adapter.cache_environment,
                *adapter.cache_environment_aliases,
                *adapter.native_aliases,
            ):
                env.pop(key, None)
            run_root = options.cache_root.resolve() / f"run-{repetition + 1}"
            if run_root.exists():
                raise RuntimeError(
                    f"cold benchmark cache already exists: {run_root}; choose a fresh --cache-root"
                )
            if options.checkout_class == "ephemeral":
                env["BH_TMPFS_CACHE_DIR"] = str(run_root)
            else:
                env["BH_DURABLE_CACHE_DIR"] = str(run_root)
            if options.native_cache_root:
                if not adapter.cache_environment:
                    raise RuntimeError(
                        f"{adapter.application} does not expose an explicit cache override"
                    )
                native_root = (
                    options.native_cache_root.resolve()
                    / adapter.application
                    / f"run-{repetition + 1}"
                )
                if native_root.exists():
                    raise RuntimeError(
                        f"explicit benchmark cache already exists: {native_root}; "
                        "choose a fresh --native-cache-root"
                    )
                env[adapter.cache_environment] = str(native_root)
            env["BH_CACHE_MIN_FREE_BYTES"] = str(options.min_free_bytes)
            env["BH_CACHE_MIN_FREE_INODES"] = str(options.min_free_inodes)
            selection = resolve_cache(
                adapter,
                target_checkout,
                target_checkout / adapter.dependency_directory,
                env,
                ephemeral=options.checkout_class == "ephemeral",
            )
            env.update(selection.environment)
            cold = _observation(options.command, target_checkout, env, selection)
            warm = _observation(options.command, target_checkout, env, selection)
            relation = (
                "equals" if selection.cache_device == selection.target_device else "differs from"
            )
            rows.append(
                {
                    "repetition": repetition + 1,
                    "fresh_checkout": str(target_checkout),
                    "source_revision": "HEAD",
                    "selection": selection.report(),
                    "materialization_evidence": {
                        "requested_method": selection.link_method,
                        "device_compatible": selection.cache_device == selection.target_device,
                        "basis": (
                            f"cache device {selection.cache_device} "
                            f"{relation} "
                            f"target device {selection.target_device}; framework control was "
                            f"{selection.environment}"
                        ),
                        "hardlink_fallback_warning": any(
                            "hardlink" in warning.lower()
                            for phase in (cold, warm)
                            for warning in phase["warnings"]
                        ),
                    },
                    "cold": cold,
                    "warm": warm,
                }
            )
    return {
        "schema_version": 1,
        "application": options.application,
        "checkout": str(checkout),
        "checkout_class": options.checkout_class,
        "command": options.command,
        "repetitions": options.repetitions,
        "median_seconds": {
            phase: statistics.median(float(row[phase]["seconds"]) for row in rows)
            for phase in ("cold", "warm")
        },
        "runs": rows,
        "cleanup": (
            "benchmark roots are retained; remove them only while idle using the package "
            "manager's supported cache command or discard the containing tmpfs"
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--application", required=True, choices=("uv", "pnpm"))
    parser.add_argument("--checkout", required=True, type=Path)
    parser.add_argument("--checkout-class", required=True, choices=("ephemeral", "persistent"))
    parser.add_argument("--cache-root", required=True, type=Path)
    parser.add_argument(
        "--native-cache-root",
        type=Path,
        help=(
            "optional base for an explicit framework cache override; useful to measure "
            "cross-device copy fallback against the checkout filesystem"
        ),
    )
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--min-free-bytes", type=int, default=0)
    parser.add_argument("--min-free-inodes", type=int, default=0)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    options = _parser().parse_args(argv)
    if options.command[:1] == ["--"]:
        options.command = options.command[1:]
    if not options.command:
        raise SystemExit("a package-manager command is required after --")
    if options.repetitions < 1:
        raise SystemExit("--repetitions must be positive")
    report = benchmark(options)
    print(json.dumps(report, indent=2, sort_keys=True))
    failed = any(run[phase]["returncode"] for run in report["runs"] for phase in ("cold", "warm"))
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
