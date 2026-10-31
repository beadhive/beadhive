"""Shared helpers for spike bh-3jp2g (throwaway, not product code).

Reuses the bh-w56ts helpers (transit dumper, StrDumper, escaping, decoders) and points them at
this spike's container (pgwire 127.0.0.1:15433). Run every script with:

    uv run --no-project --with 'psycopg[binary]' --with transit-python2 python <script>.py
"""

from __future__ import annotations

import datetime as dt
import os
import sys
from pathlib import Path

os.environ.setdefault("XTDB_DSN", "host=127.0.0.1 port=15433 user=xtdb dbname=xtdb")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "bh-w56ts"))

from common import (  # noqa: E402  (w56ts helpers, re-exported)
    DATA,
    DSN,
    Transit,
    connect,
    decode_transit,
    esc,
    load_events,
    load_export,
    set_output,
    to_transit,
    tts,
)

SCRATCH = Path("/data/bees/beadhive/scratch/xtdb-spike/bh-3jp2g")
WORK = SCRATCH / "work"

__all__ = [
    "DATA",
    "DSN",
    "SCRATCH",
    "WORK",
    "Transit",
    "connect",
    "decode_transit",
    "esc",
    "fmt",
    "load_events",
    "load_export",
    "parse_ts",
    "q",
    "set_output",
    "to_transit",
    "tts",
]


def parse_ts(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def fmt(v):
    if isinstance(v, dt.datetime):
        s = v.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%S.%f").rstrip("0").rstrip(".")
        return s + "Z"
    return v


def q(conn, title, sql, params=None, limit=20, quiet=False):
    """Run a query, print a compact result block, return rows (or None on error)."""
    print(f"\n-- {title}\n   {' '.join(sql.split())}")
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall() if cur.description else []
        if cur.description and not quiet:
            print("   cols:", [d.name for d in cur.description])
            for r in rows[:limit]:
                print("   ", tuple(fmt(v) for v in r))
            if len(rows) > limit:
                print(f"    ... {len(rows)} rows")
        return rows
    except Exception as e:  # noqa: BLE001
        print("   ERR", type(e).__name__, str(e).splitlines()[0][:240])
        if not conn.autocommit:
            conn.rollback()
        else:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
        return None
