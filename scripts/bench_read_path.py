#!/usr/bin/env python3
"""Manual benchmark for bh's read path, cold vs warm, per verb.

Run via `just bench-read-path` — NOT part of `just check` / any CI gate. A single cold
`bh doctor` alone runs ~60s, and these numbers measure the host as much as the code, so
folding this into the fast/full gate would make every `bh work check` minutes slower for a
number nobody asked it to prove. Invoke by hand when judging a read-path change or an
adoption before/after (bh-amq08, child of bh-13spb).

COLD means: bh's own JSON read-caches under `config.cache_dir()` are deleted before the
run — today that's `metadata.json` (hive/repo survey) and `bd-schema-version.json` (dolt
schema-version probe). It does NOT clear the OS page cache or dolt server buffers — neither
is clearable without root, and doing so would make results unreproducible across hosts
anyway. WARM means: run the same verb again immediately after, caches populated.

Once bh-13spb.2/.3 land the source/fact/view layers, extend `_VERBS` (or add a companion
table) to report per-layer timings instead of only per-verb. Not built yet — no consumer.

Run this from a registered hive's own clone (e.g. this repo's main checkout), not an
ephemeral `bh work` worktree: `hive ready`/`doctor` resolve the current hive from cwd, and a
worktree under `~/.beadhive/worktrees/` isn't itself registered under $GIT_WORKSPACE — cwd
there makes `hive ready` fail fast and silently report a bogus near-zero number instead of
the real check. `_time_once` below flags a nonzero exit so this shows up as FAILED, not as a
suspiciously fast row.
"""

from __future__ import annotations

import re
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from beadhive import config

_CACHE_FILES = ("metadata.json", "bd-schema-version.json")

# Each verb resolved against the CURRENT hive by cwd (this repo, "bh"), matching how a
# developer would run it day to day.
_VERBS: list[tuple[str, list[str]]] = [
    ("hive list", ["bh", "hive", "list"]),
    ("hive status --json", ["bh", "hive", "status", "--hive", "bh", "--json"]),
    ("bd export (bh)", ["bh", "--hive", "bh", "bd", "export"]),
    ("hive ready", ["bh", "hive", "ready"]),
    ("doctor", ["bh", "doctor"]),
]


def _clear_cold_caches() -> None:
    for name in _CACHE_FILES:
        (config.cache_dir() / name).unlink(missing_ok=True)


def _time_once(cmd: list[str]) -> tuple[float, bool]:
    t0 = time.perf_counter()
    result = subprocess.run(cmd, capture_output=True, check=False)
    return time.perf_counter() - t0, result.returncode == 0


def _hive_count() -> int:
    out = subprocess.run(["bh", "hive", "list"], capture_output=True, text=True, check=False)
    m = re.search(r"\((\d+)\)", out.stdout)
    return int(m.group(1)) if m else -1


def _bh_version() -> str:
    out = subprocess.run(["bh", "--version"], capture_output=True, text=True, check=False)
    return out.stdout.strip() or out.stderr.strip()


def main() -> int:
    header = [
        "# bh read-path benchmark",
        "",
        f"date:        {datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}",
        f"bh version:  {_bh_version()}",
        f"hives:       {_hive_count()} registered",
        "cold clears: bh's JSON read-caches under config.cache_dir() only "
        f"({', '.join(_CACHE_FILES)}) — NOT the OS page cache or dolt buffers "
        "(not clearable without root; would make results unreproducible).",
        "",
        f"{'VERB':<22}{'COLD':>10}{'WARM':>10}{'GAP':>10}",
    ]
    print("\n".join(header), flush=True)
    lines = list(header)
    for name, cmd in _VERBS:
        _clear_cold_caches()
        cold, cold_ok = _time_once(cmd)
        warm, warm_ok = _time_once(cmd)
        gap = cold - warm
        row = f"{name:<22}{cold:>9.2f}s{warm:>9.2f}s{gap:>9.2f}s"
        if not (cold_ok and warm_ok):
            row += "  FAILED (nonzero exit — run from a registered hive clone, see docstring)"
        lines.append(row)
        print(row, flush=True)

    footer = (
        "\nNote: an outlier here (a verb whose cold/warm both drift far from a prior "
        "baseline while its neighbours don't) is signal, not noise — call it out by name "
        "rather than averaging it into the table. See bh-13spb.1 for the open `hive ready` "
        "discrepancy against the epic's recorded baseline."
    )
    lines.append(footer)
    print(footer)

    report_dir = Path(__file__).resolve().parent.parent / ".bench"
    report_dir.mkdir(exist_ok=True)
    report_path = report_dir / f"read-path-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.md"
    report_path.write_text("\n".join(lines) + "\n")

    print(f"\nreport written to: {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
