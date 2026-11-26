#!/usr/bin/env python3
"""Stand-in seat binary for ``tests/test_runtime_loop_contract.py`` (bh-lxv3v). Standard library.

Launched by ``bh role <role> --outcome-sink bead --session-id <sid>`` (through the hive's
``work.dispatch.seat_command``) with the settled role-binary argv. It:

1. records what it was launched with (role, bead, workspace, session id, cwd) as
   ``<log>/<session_id>.json``, so the test can check the argv the seat really received;
2. holds its turn until ONE line arrives on stdin (the readiness handshake: the session host
   writes it, nothing sleeps). The line is ``<status> <summary>``;
3. prints ONE ``SeatRun`` line with that status and exits 0. ``bh role`` then writes the same line
   to the bead's ``bh.outcome.<session_id>`` metadata.

EOF on stdin (the host stopped the session) prints nothing and exits 1: ``bh role`` then records
a synthesized killed-run envelope, which classifies ``failed``.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--log", required=True)
    parser.add_argument("--role", required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--bead", required=True)
    parser.add_argument("--instructions", default="")
    parser.add_argument("--session_id", required=True)
    args, _unknown = parser.parse_known_args(argv)

    log = Path(args.log)
    log.mkdir(parents=True, exist_ok=True)
    launched = {
        "role": args.role,
        "bead": args.bead,
        "workspace": args.workspace,
        "session_id": args.session_id,
        "cwd": os.getcwd(),
    }
    (log / f"{args.session_id}.json").write_text(json.dumps(launched), encoding="utf-8")

    line = sys.stdin.readline()
    if not line:
        return 1
    status, _, summary = line.strip().partition(" ")
    seat_run = {
        "outcome": {"status": status, "summary": summary, "bead_id": args.bead},
        "session_id": args.session_id,
        "cost_usd": 0.25,
        "usage": {},
        "packs": [],
    }
    sys.stdout.write(json.dumps(seat_run) + "\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
