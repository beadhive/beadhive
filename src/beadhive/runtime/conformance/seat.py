"""Stand-in seat binary for the runtime conformance kit. Standard library only.

The kit hands a runtime ``<python> seat.py --hive <dir>`` as its ``bh-{role}`` seat command, so
this file speaks the settled role-binary argv (``--workspace --bead --instructions --session_id``)
and has to run in any interpreter, with no beadhive import. It plays one seat turn:

1. note the start in the hive (``starts/<bead>.log``) so the kit can count dispatches;
2. wait until the kit releases the turn (``release/<session_id>``), so ``observe()`` has a real
   ``running`` window to be checked in;
3. print ONE SeatRun JSON line on stdout and, as ``bh role --outcome-sink bead`` would, store
   the same line under the one metadata key ``bh.outcome.<session_id>`` (append-only).

It touches no claim, status or label, which is the behaviour the ADR gives a seat.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

OUTCOME_PREFIX = "bh.outcome."
SEAT_SCRIPT_NAME = "seat.py"


def seat_run_line(bead: str, session_id: str) -> str:
    return json.dumps(
        {
            "outcome": {"status": "done", "summary": "conformance turn", "bead_id": bead},
            "session_id": session_id,
            "cost_usd": 0.0,
            "usage": {},
            "packs": [],
        }
    )


def write_outcome(hive: Path, bead: str, session_id: str, line: str) -> bool:
    """Store *line* under ``bh.outcome.<session_id>``; False when a record already exists."""
    path = hive / "beads" / f"{bead}.json"
    state = json.loads(path.read_text(encoding="utf-8"))
    key = OUTCOME_PREFIX + session_id
    if key in state["metadata"]:
        return False
    state["metadata"][key] = json.loads(line)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state), encoding="utf-8")
    os.replace(tmp, path)
    return True


_SHARED_SERVER_ENV = frozenset(
    {"BEADS_DOLT_SHARED_SERVER", "BEADS_SHARED_SERVER_DIR", "BEADS_DOLT_SERVER_PORT"}
)


def scrubbed_env() -> dict[str, str]:
    """The environment for ``bd`` against the kit's own scratch store: without the caller's
    shared-server controls, which would point bd at a server that is not this store's."""
    return {k: v for k, v in os.environ.items() if k not in _SHARED_SERVER_ENV}


def write_outcome_bd(store: Path, bead: str, session_id: str, line: str) -> bool:
    """The same append-only write, through the real ``bd`` binary against the scratch store at
    *store* (what ``bh role --outcome-sink bead`` does): False when a record already exists.

    bd-seam-justified: the stand-in seat is a standalone stdlib script with no beadhive import."""
    key = OUTCOME_PREFIX + session_id
    shown = subprocess.run(
        ["bd", "-C", str(store), "show", bead, "--json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        env=scrubbed_env(),
    )
    issue = json.loads(shown.stdout)
    issue = issue[0] if isinstance(issue, list) else issue
    if key in (issue.get("metadata") or {}):
        return False
    subprocess.run(
        ["bd", "-C", str(store), "update", bead, "--metadata", json.dumps({key: json.loads(line)})],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
        env=scrubbed_env(),
    )
    return True


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--hive", required=True)
    parser.add_argument("--bd-store", default="", help="record the outcome through bd here")
    parser.add_argument("--workspace", default="")
    parser.add_argument("--bead", required=True)
    parser.add_argument("--instructions", default="")
    parser.add_argument("--session_id", required=True)
    parser.add_argument("--model", default="")
    parser.add_argument("--bundle", default="")
    parser.add_argument("--input-format", default="")
    args, _unknown = parser.parse_known_args(argv)
    hive = Path(args.hive)

    starts = hive / "starts"
    starts.mkdir(parents=True, exist_ok=True)
    with (starts / f"{args.bead}.log").open("a", encoding="utf-8") as log:
        log.write(args.session_id + "\n")

    release = hive / "release" / args.session_id
    deadline = time.monotonic() + 120
    while not release.exists():
        if time.monotonic() > deadline:
            return 3
        time.sleep(0.02)

    line = seat_run_line(args.bead, args.session_id)
    if args.bd_store:
        write_outcome_bd(Path(args.bd_store), args.bead, args.session_id, line)
    else:
        write_outcome(hive, args.bead, args.session_id, line)
    sys.stdout.write(line + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
