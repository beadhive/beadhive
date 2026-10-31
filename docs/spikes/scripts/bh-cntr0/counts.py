"""Row counts + content digests of every spike table (integrity check across kill -9 / restore).

Usage: python counts.py [port]   (default 15434).  Prints one JSON line.
Digest = sha256 over the ordered rows of simple scalar columns (no nested values).
"""

import hashlib
import json
import sys
import time

import psycopg
from psycopg.types.string import StrDumper

port = sys.argv[1] if len(sys.argv) > 1 else "15434"
t0 = time.monotonic()
conn = psycopg.connect(f"host=127.0.0.1 port={port} user=xtdb dbname=xtdb", autocommit=True)
conn.adapters.register_dumper(str, StrDumper)
Q = {
    "beads_a": "SELECT _id, title, status FROM beads_a ORDER BY _id",
    "beads_b": "SELECT _id, title, status FROM beads_b ORDER BY _id",
    "bead_labels": (
        "SELECT _id, _valid_from, _valid_to FROM bead_labels FOR ALL VALID_TIME "
        "ORDER BY _id, _valid_from"
    ),
    "bead_edges": (
        "SELECT _id, _valid_from, _valid_to FROM bead_edges FOR ALL VALID_TIME "
        "ORDER BY _id, _valid_from"
    ),
    "bead_comments": "SELECT _id, text FROM bead_comments ORDER BY _id",
    "bd_events": "SELECT _id, event_type FROM bd_events ORDER BY _id",
}
out = {}
for t, q in Q.items():
    rows = conn.execute(q).fetchall()
    h = hashlib.sha256(repr(rows).encode()).hexdigest()[:16]
    out[t] = [len(rows), h]
out["query_all_s"] = round(time.monotonic() - t0, 2)
print(json.dumps(out))
