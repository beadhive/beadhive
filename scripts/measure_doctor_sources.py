#!/usr/bin/env python3
"""Throwaway harness: attribute `bh doctor`'s wall time to SOURCES, not sections (bh-13spb.1).

`bh doctor -v` already reports per-SECTION timings (bh-8nnh7). This answers the other
question the cache molecule needs: which external reading costs what, how often the SAME
reading is repeated inside one run, and which sections consume it.

Method: monkeypatch `subprocess.run` (the single seam every bh spawn funnels through —
`run.run` calls it, and the handful of deliberate bypasses in safety.py/doctor.py call it
directly too) to record argv + cwd + wall time, and monkeypatch `doctor._timed` to tag each
record with the section that was executing. Then call `doctor.doctor_payload()` in-process
once and print a JSON report.

Not a permanent instrument. Run by hand from a registered clone (never a `bh work`
worktree — `doctor` resolves the fleet from config, but the hive-local probes read cwd):

    uv run python3 scripts/measure_doctor_sources.py > /tmp/sources.json
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections import defaultdict
from pathlib import Path

_real_run = subprocess.run
_section = "startup"
_records: list[dict] = []


def _traced(cmd, *a, **kw):
    t0 = time.monotonic()
    try:
        return _real_run(cmd, *a, **kw)
    finally:
        argv = cmd if isinstance(cmd, list | tuple) else [str(cmd)]
        _records.append(
            {
                "section": _section,
                "argv": [str(x) for x in argv],
                "cwd": str(kw.get("cwd") or ""),
                "ms": round((time.monotonic() - t0) * 1000, 2),
            }
        )


def _source_key(rec: dict) -> str:
    """A SOURCE = the reading, independent of which hive/path it was taken against.

    `bd config get beads.role` in 15 different hives is one source read 15 times — that is
    exactly the number the cache molecule needs, so flags/keys stay in the key and paths and
    hive ids are dropped.
    """
    argv = rec["argv"]
    tool = os.path.basename(argv[0]) if argv else "?"
    parts = [tool]
    skip_next = False
    for tok in argv[1:]:
        if skip_next:
            skip_next = False
            continue
        if tok in ("-C", "--hive", "--target", "--catalog", "--profiles", "--dir", "--repo"):
            parts.append(tok)
            skip_next = True
            continue
        if "/" in tok or tok.startswith("~"):
            continue
        parts.append(tok)
    return " ".join(parts)


def _identical_key(rec: dict) -> str:
    """The literal same call: same argv AND same cwd — a pure duplicate a per-run memo kills."""
    return json.dumps([rec["argv"], rec["cwd"]])


def main() -> int:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
    from beadhive import doctor

    real_timed = doctor._timed

    def timed(timings, key, fn, *a, **kw):
        global _section
        prev, _section = _section, key
        try:
            return real_timed(timings, key, fn, *a, **kw)
        finally:
            _section = prev

    subprocess.run = _traced
    doctor._timed = timed
    t0 = time.monotonic()
    payload = doctor.doctor_payload()
    wall = round((time.monotonic() - t0) * 1000, 2)
    subprocess.run = _real_run
    doctor._timed = real_timed

    by_source: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "ms": 0.0, "sections": defaultdict(int)}
    )
    for r in _records:
        s = by_source[_source_key(r)]
        s["calls"] += 1
        s["ms"] = round(s["ms"] + r["ms"], 2)
        s["sections"][r["section"]] += 1

    dupes: dict[str, list[dict]] = defaultdict(list)
    for r in _records:
        dupes[_identical_key(r)].append(r)
    repeated = [
        {
            "argv": json.loads(k)[0],
            "cwd": json.loads(k)[1],
            "times": len(v),
            "ms_total": round(sum(x["ms"] for x in v), 2),
            "sections": sorted({x["section"] for x in v}),
        }
        for k, v in dupes.items()
        if len(v) > 1
    ]

    report = {
        "wall_ms": wall,
        "section_timings": payload.get("data", payload).get("timings", {}),
        "spawn_count": len(_records),
        "spawn_ms_total": round(sum(r["ms"] for r in _records), 2),
        "by_source": [
            dict(source=k, **{**v, "sections": dict(v["sections"])})
            for k, v in sorted(by_source.items(), key=lambda kv: -kv[1]["ms"])
        ],
        "repeated_identical": sorted(repeated, key=lambda r: -r["ms_total"]),
        "records": _records,
    }
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
