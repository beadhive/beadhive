"""Write batches of 100 rows (one XTDB tx each) until the server dies; log last acked batch.

Usage: python crashwrite.py <ackfile>
Each row: {_id: 'b<batch>-<i>', batch: n, i: i, pad: <200 chars>}.  After the server is
kill -9'd and restarted, crashverify.py checks: every acked batch is fully present, and no
batch is partially present (tx atomicity).
"""

import sys

import psycopg
from psycopg.types.string import StrDumper

ack = sys.argv[1]
conn = psycopg.connect("host=127.0.0.1 port=15434 user=xtdb dbname=xtdb", autocommit=False)
conn.adapters.register_dumper(str, StrDumper)
n = 0
try:
    while True:
        n += 1
        with conn.transaction():
            conn.execute(
                "INSERT INTO crash_probe RECORDS "
                + ", ".join(["{_id: %s, batch: %s, i: %s, pad: %s}"] * 100),
                [x for i in range(100) for x in (f"b{n}-{i}", n, i, "x" * 200)],
            )
        with open(ack, "w") as f:
            f.write(str(n))
except Exception as e:  # noqa: BLE001
    print(f"writer stopped after acked batch {n - 1}: {type(e).__name__}: {str(e)[:80]}")
