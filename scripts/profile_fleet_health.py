#!/usr/bin/env python3
"""Throwaway profiling harness for (spike).

Attributes wall-time of the ``_section_fleet_health`` per-repo work across the
three cost buckets called out in the bead:

  1. ``safety.scan`` git subprocess calls   (scan total MINUS the disk walk)
  2. ``_measure_disk_usage`` os.walk         (working tree + .git sizing)
  3. ``last_commit_age_days``                (one ``git log -1`` per repo)

It profiles the REAL code path (``ws.safety``) over every ``github/<org>/<repo>``
git repo under $GIT_WORKSPACE — the same universe ``doctor._scan`` feeds into
``_section_fleet_health``. Not shipped; run ad hoc via ``uv run``.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

from ws import safety


def _repos(root: Path) -> list[Path]:
    out: list[Path] = []
    gh = root / "github"
    if not gh.is_dir():
        return out
    for org in sorted(p for p in gh.iterdir() if p.is_dir()):
        for repo in sorted(p for p in org.iterdir() if p.is_dir()):
            if (repo / ".git").exists():
                out.append(repo)
    return out


def main() -> int:
    root = Path(os.environ.get("GIT_WORKSPACE", str(Path.home() / "workspace")))
    repos = _repos(root)
    n = len(repos)
    print(f"root={root}  repos={n}\n")
    if not n:
        print("no repos found")
        return 1

    t_disk = 0.0        # _measure_disk_usage (os.walk + count-objects)
    t_scan_git = 0.0    # safety.scan git subprocess calls ONLY (disk walk neutralized)
    t_age = 0.0         # last_commit_age_days
    bytes_total = 0
    per_repo: list[tuple[str, float, float, float]] = []

    # Neutralize scan's internal disk walk so we time ONLY its git subprocess calls.
    # (Subtracting a separately-timed walk is confounded by OS page-cache warming.)
    real_measure = safety._measure_disk_usage

    wall0 = time.perf_counter()
    for repo in repos:
        p = str(repo)

        # (1) git-only scan: patch disk sizing to a no-op for this call
        safety._measure_disk_usage = lambda _p: 0  # noqa: E731
        s0 = time.perf_counter()
        safety.scan(p)
        s1 = time.perf_counter()
        safety._measure_disk_usage = real_measure

        # (2) disk walk in isolation (also gives real disk_bytes)
        d0 = time.perf_counter()
        b = real_measure(p)
        d1 = time.perf_counter()

        # (3) last-commit age
        a0 = time.perf_counter()
        safety.last_commit_age_days(p)
        a1 = time.perf_counter()

        scan_git = s1 - s0
        disk = d1 - d0
        age = a1 - a0

        t_scan_git += scan_git
        t_disk += disk
        t_age += age
        bytes_total += b
        per_repo.append((repo.name, scan_git, disk, age))
    wall1 = time.perf_counter()

    scan_git_total = t_scan_git
    # Effective per-repo cost in the REAL path: scan(git) + 1 disk walk + age.
    effective = t_scan_git + t_disk + t_age

    print("=== per-repo wall-time buckets (real _section_fleet_health path) ===")
    print(f"{'bucket':<32}{'total s':>12}{'per-repo ms':>14}{'% of work':>12}")
    for name, val in [
        ("safety.scan git calls", scan_git_total),
        ("_measure_disk_usage os.walk", t_disk),
        ("last_commit_age_days", t_age),
    ]:
        print(f"{name:<32}{val:>12.3f}{val / n * 1000:>14.1f}{val / effective * 100:>11.1f}%")
    print(f"{'-' * 70}")
    eff_row = f"{'effective per-repo (scan+age)':<32}{effective:>12.3f}"
    print(f"{eff_row}{effective / n * 1000:>14.1f}{100.0:>11.1f}%")
    print(f"{'measured wall (harness, 2x disk)':<32}{wall1 - wall0:>12.3f}")
    print(f"\ntotal disk measured: {safety.format_bytes(bytes_total)}")

    print("\n=== top 8 repos by disk-walk cost ===")
    for name, sg, dk, ag in sorted(per_repo, key=lambda r: r[2], reverse=True)[:8]:
        row = f"  {name:<34} disk={dk * 1000:7.1f}ms  scan_git={sg * 1000:6.1f}ms"
        print(f"{row}  age={ag * 1000:5.1f}ms")

    return 0


if __name__ == "__main__":
    sys.exit(main())
