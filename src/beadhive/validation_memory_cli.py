"""Ad-hoc memory-guarded runs for agents and operators (bh-jg7fy).

The same guard validation uses — MemAvailable admission floor, a memory-bounded user systemd
scope, and a peak report — for ad-hoc test runs::

    uv run python -m beadhive.validation_memory_cli -- uv run pytest -n 2 tests/test_x.py

It takes **no** host validation slot, so never wrap ``bh work check``/``submit`` (which admit
themselves) with it.  Kept apart from :mod:`beadhive.validation_memory` so the policy module
never depends on the launcher in :mod:`beadhive.validation_admission`.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

from . import validation_admission
from .config_consumer_ports import work_settings as config
from .validation_memory import (
    MemoryAdmissionTimeout,
    _human,
    finalize,
    outcome_line,
    read_report,
    settings,
    wait_for_floor,
)


def _load_cfg() -> dict:
    try:
        return config.load()
    except Exception:  # noqa: BLE001 - an ad-hoc guard must still run without a bh config
        return {}


def main(argv: list[str] | None = None) -> int:
    """Ad-hoc guarded run: floor wait, bounded scope, peak report.  Takes no host slot."""
    parser = argparse.ArgumentParser(
        prog="python -m beadhive.validation_memory_cli",
        description=(
            "Run a command under the validation memory guard: wait for the MemAvailable floor, "
            "bound it in a user systemd scope, and report its peak memory. Takes no host "
            "validation slot, so never wrap `bh work check`/`submit` with it."
        ),
    )
    parser.add_argument("--memory-max", help="override work.validation_memory.memory_max")
    parser.add_argument("--memory-high", help="override work.validation_memory.memory_high")
    parser.add_argument("--floor", help="override work.validation_memory.admission_floor")
    parser.add_argument("--json", action="store_true", help="print the memory record as JSON")
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("a command is required after --")
    cfg = _load_cfg()
    overrides = {
        key: value
        for key, value in (
            ("memory_max", args.memory_max),
            ("memory_high", args.memory_high),
            ("admission_floor", args.floor),
        )
        if value is not None
    }
    if overrides:
        work = dict(cfg.get("work") or {}) if isinstance(cfg, dict) else {}
        work["validation_memory"] = {**dict(work.get("validation_memory") or {}), **overrides}
        cfg = {**(cfg if isinstance(cfg, dict) else {}), "work": work}
    policy = settings(cfg)
    if args.floor is not None:
        policy = replace(policy, admission_floor=args.floor)
    say = lambda line: print(line, file=sys.stderr)  # noqa: E731
    try:
        wait_for_floor(policy, echo=say)
    except MemoryAdmissionTimeout as exc:
        say(f"✗ {exc}")
        return 75
    with tempfile.TemporaryDirectory(prefix="bh-memory-guard-") as scratch:
        report_path = Path(scratch) / "memory.json"
        argv_, _priority, record = validation_admission.guarded_command(
            cfg, command, report_path=report_path, policy=policy
        )
        try:
            rc = subprocess.run(argv_, check=False).returncode
        except KeyboardInterrupt:
            return 130
        record = finalize(record, read_report(report_path))
    note = f" ({record['note']})" if record and record.get("note") else ""
    say(
        f"  → memory guard: {record.get('mechanism') if record else 'unavailable'}{note}; "
        f"peak {_human((record or {}).get('peak_bytes'))}"
    )
    if args.json:
        print(json.dumps(record, sort_keys=True))
    line = outcome_line(record)
    if line:
        say(line)
        return rc if rc > 0 else 137
    return rc if rc >= 0 else 128 - rc


if __name__ == "__main__":
    sys.exit(main())
