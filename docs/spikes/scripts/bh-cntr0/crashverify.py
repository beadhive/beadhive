"""Verify crash_probe after kill -9: acked batches complete, no partial batch.

Usage: crashverify.py <ackfile>
"""

import sys

import psycopg

acked = int(open(sys.argv[1]).read())
c = psycopg.connect("host=127.0.0.1 port=15434 user=xtdb dbname=xtdb", autocommit=True)
rows = c.execute("SELECT batch, count(*) FROM crash_probe GROUP BY batch").fetchall()
cnt = dict(rows)
present = len(cnt)
partial = {b: n for b, n in cnt.items() if n != 100}
missing = [b for b in range(1, acked + 1) if cnt.get(b) != 100]
print(
    f"last acked batch: {acked}; batches present: {present}; "
    f"max present: {max(cnt) if cnt else 0}; "
    f"partial batches: {partial}; acked-but-missing/incomplete: {missing}"
)
