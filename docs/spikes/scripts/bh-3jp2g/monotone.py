"""Monotone SYSTEM_TIME backfill: syntax, ties, nulls, out-of-order input, aborts, reads.

    monotone.py 1|2|3   each on its OWN fresh node (`xtdb.sh down && xtdb.sh up`)
    monotone.py 4       follow-up reads on the part-2 node

Sections inside a part run in order on that node; later sections rely on what earlier ones did
(an aborted backfill tx, a wall-clock tx, a future system time).
"""

# ruff: noqa: E501  (throwaway spike script: long SQL literals kept on one line)

from __future__ import annotations

import datetime as dt

from xt import Transit, connect, fmt, q

T = "2020-01-{:02d}T00:00:00Z".format


def last_tx(conn, n=1):
    rows = conn.execute(
        "SELECT _id, committed, system_time, error FROM xt.txs ORDER BY _id DESC LIMIT %s", [n]
    ).fetchall()
    for r in reversed(rows):
        err = "" if r[3] is None else str(r[3]).replace("\n", " ")[:200]
        print(f"   xt.txs: id={r[0]} committed={r[1]} system_time={fmt(r[2])} {err}")


def tx(conn, title, begin, stmts, commit="COMMIT", setup=()):
    """Explicit BEGIN ... COMMIT on an autocommit connection; report where it fails."""
    print(f"\n== {title}\n   {begin}" + "".join(f"; {s}" for s in setup))
    step = begin
    try:
        conn.execute(begin)
        for s in setup:
            step = s
            conn.execute(s)
        for sql, params in stmts:
            step = sql
            conn.execute(sql, params)
        step = commit
        conn.execute(commit)
        print("   client: OK")
    except Exception as e:  # noqa: BLE001
        print(f"   client: ERR at `{step.split()[0]}`:", str(e).splitlines()[0][:220])
        try:
            conn.execute("ROLLBACK")
        except Exception:  # noqa: BLE001
            pass
    last_tx(conn)


def ins(table, doc):
    return (f"INSERT INTO {table} RECORDS %s", [Transit(doc)])


def tsv(s):
    return {"~#time/instant": s}


def sysfrom(conn, table, _id):
    return q(
        conn,
        f"{table} {_id}: every bitemporal row",
        f"""SELECT _id, v, _valid_from, _valid_to, _system_from, _system_to FROM {table}
        FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE _id = %s ORDER BY _system_from, _valid_from""",
        [_id],
    )


def main():
    c = connect(autocommit=True)
    print("XTDB version:", c.execute("SELECT version()").fetchone()[0] if True else "")
    last_tx(c, 3)

    # ---------------------------------------------------------------- S1 syntax variants
    print("\n######## S1. Which SYSTEM_TIME syntaxes does 2.2.0 accept?")
    tx(
        c,
        "S1a BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP ...)",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(1)}')",
        [ins("s1", {"_id": "a", "v": 1})],
    )
    tx(
        c,
        "S1b BEGIN READ WRITE, SYSTEM_TIME = TIMESTAMP ...",
        f"BEGIN READ WRITE, SYSTEM_TIME = TIMESTAMP '{T(2)}'",
        [ins("s1", {"_id": "b", "v": 1})],
    )
    tx(
        c,
        "S1c BEGIN; SET TRANSACTION READ WRITE, SYSTEM_TIME = ...",
        "BEGIN",
        [ins("s1", {"_id": "c", "v": 1})],
        setup=[f"SET TRANSACTION READ WRITE, SYSTEM_TIME = TIMESTAMP '{T(3)}'"],
    )
    tx(
        c,
        "S1d SYSTEM_TIME as a plain string literal (no TIMESTAMP keyword)",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = '{T(4)}')",
        [ins("s1", {"_id": "d", "v": 1})],
    )
    tx(
        c,
        "S1e SYSTEM_TIME with microseconds",
        "BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-05T00:00:00.000001Z')",
        [ins("s1", {"_id": "e", "v": 1})],
    )
    print("\n== S1f SYSTEM_TIME as a bind parameter")
    try:
        c.execute(
            "BEGIN READ WRITE WITH (SYSTEM_TIME = %s)", [dt.datetime(2020, 1, 6, tzinfo=dt.UTC)]
        )
        c.execute(*ins("s1", {"_id": "f", "v": 1}))
        c.execute("COMMIT")
        print("   client: OK")
    except Exception as e:  # noqa: BLE001
        print("   client: ERR", str(e).splitlines()[0][:200])
        c.execute("ROLLBACK")
    last_tx(c)
    print("\n== S1g `SETTING SYSTEM_TIME = ...` prefix on DML (SETTING is documented for queries)")
    try:
        c.execute(
            f"SETTING SYSTEM_TIME = TIMESTAMP '{T(7)}' INSERT INTO s1 RECORDS {{_id: 'g', v: 1}}"
        )
        print("   client: OK")
    except Exception as e:  # noqa: BLE001
        print("   client: ERR", str(e).splitlines()[0][:200])
    q(
        c,
        "S1 rows and their system time",
        "SELECT _id, _system_from, _valid_from FROM s1 ORDER BY _id",
    )

    # ---------------------------------------------------------------- S2-S6 monotone rule
    print("\n######## S2. Tie: SYSTEM_TIME equal to the latest committed transaction")
    tx(
        c,
        "S2 tie with S1e (2020-01-05T00:00:00.000001Z)",
        "BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-05T00:00:00.000001Z')",
        [ins("s2", {"_id": "tie", "v": 1}), ins("s2", {"_id": "tie2", "v": 1})],
    )
    q(c, "S2 atomicity: rows of the aborted tie tx", "SELECT _id FROM s2")

    print("\n######## S3. Retrying after an abort")
    tx(
        c,
        "S3a set the clock: commit at 2020-01-10",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(10)}')",
        [ins("s3", {"_id": "x", "v": 1})],
    )
    tx(
        c,
        "S3b tie at 2020-01-10 (aborts)",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(10)}')",
        [ins("s3", {"_id": "x", "v": 2})],
    )
    tx(
        c,
        "S3c retry at +1us 2020-01-10T00:00:00.000001Z",
        "BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-10T00:00:00.000001Z')",
        [ins("s3", {"_id": "x", "v": 3})],
    )
    tx(
        c,
        "S3d retry at +2us",
        "BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-10T00:00:00.000002Z')",
        [ins("s3", {"_id": "x", "v": 4})],
    )
    sysfrom(c, "s3", "x")

    print("\n######## S4. Out-of-order (earlier) SYSTEM_TIME, then the load continues")
    tx(
        c,
        "S4a earlier than latest (2020-01-08)",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(8)}')",
        [ins("s4", {"_id": "late", "v": 1})],
    )
    tx(
        c,
        "S4b next in order (2020-01-11)",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(11)}')",
        [ins("s4", {"_id": "next", "v": 1})],
    )
    q(c, "S4 rows", "SELECT _id, _system_from FROM s4")

    print("\n######## S5. Does a tx that fails for a non-time reason consume its SYSTEM_TIME?")
    tx(
        c,
        "S5a statement error (record without _id) at 2020-01-12",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(12)}')",
        [ins("s5", {"_id": "ok", "v": 1}), ins("s5", {"v": "no id"})],
    )
    tx(
        c,
        "S5b ASSERT failure at 2020-01-13",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(13)}')",
        [("ASSERT 1 = 2, 'forced'", None), ins("s5", {"_id": "ok", "v": 1})],
    )
    tx(
        c,
        "S5c fixed retry at the same 2020-01-13",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(13)}')",
        [ins("s5", {"_id": "ok", "v": 1})],
    )
    tx(
        c,
        "S5d fixed retry at 2020-01-14",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(14)}')",
        [ins("s5", {"_id": "ok", "v": 1})],
    )

    print("\n######## S6. Null / default SYSTEM_TIME")
    tx(
        c,
        "S6a SYSTEM_TIME = NULL",
        "BEGIN READ WRITE WITH (SYSTEM_TIME = NULL)",
        [ins("s6", {"_id": "n", "v": 1})],
    )
    tx(
        c,
        "S6b SET TRANSACTION ..., SYSTEM_TIME = DEFAULT after setting one",
        "BEGIN",
        [ins("s6", {"_id": "d", "v": 1})],
        setup=[
            f"SET TRANSACTION READ WRITE, SYSTEM_TIME = TIMESTAMP '{T(15)}'",
            "SET TRANSACTION READ WRITE, SYSTEM_TIME = DEFAULT",
        ],
    )
    q(c, "S6 rows", "SELECT _id, _system_from FROM s6")
    tx(
        c,
        "S6c after any wall-clock tx, can backfill still continue? (2020-01-16)",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(16)}')",
        [ins("s6", {"_id": "after", "v": 1})],
    )


def main3():
    """Run on a fresh node: does a NON-time abort (ASSERT) also end backfill? Attached databases."""
    c = connect(autocommit=True)
    print("\n######## S12. Abort for a non-time reason inside a backfill")
    tx(
        c,
        "S12a commit at 2020-01-01",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(1)}')",
        [ins("s12", {"_id": "a", "v": 1})],
    )
    tx(
        c,
        "S12b ASSERT failure at 2020-01-02",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(2)}')",
        [("ASSERT 1 = 2, 'forced'", None), ins("s12", {"_id": "b", "v": 1})],
    )
    tx(
        c,
        "S12c next tx at 2020-01-03",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(3)}')",
        [ins("s12", {"_id": "c", "v": 1})],
    )
    last_tx(c, 3)

    print("\n######## S13. Does an attached database keep its own system-time clock?")
    q(c, "S13 attach", "ATTACH DATABASE bf2")
    import psycopg
    from common import StrDumper, TransitDumper  # noqa: F401
    from xt import DSN

    c2 = psycopg.connect(DSN.replace("dbname=xtdb", "dbname=bf2"), autocommit=True)
    c2.adapters.register_dumper(Transit, TransitDumper)
    c2.adapters.register_dumper(str, StrDumper)
    tx(
        c2,
        "S13a backfill 2020-01-01 into the fresh attached database bf2",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(1)}')",
        [ins("s13", {"_id": "a", "v": 1})],
    )
    q(c2, "S13 bf2 rows", "SELECT _id, _system_from FROM s13")


def main2():
    """Run on a SECOND fresh node: within-tx semantics, DELETE defaults, bitemporal reads."""
    c = connect(autocommit=True)
    print("\n######## S7. Several versions of one entity inside one backfill tx")
    tx(
        c,
        "S7 v1@01-01, v2@01-03, then v3 and v4 both @01-05 (same _valid_from)",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(5)}')",
        [
            ins("s7", {"_id": "e", "v": 1, "_valid_from": tsv(T(1))}),
            ins("s7", {"_id": "e", "v": 2, "_valid_from": tsv(T(3))}),
            ins("s7", {"_id": "e", "v": 3, "_valid_from": tsv(T(5))}),
            ins("s7", {"_id": "e", "v": 4, "_valid_from": tsv(T(5))}),
        ],
    )
    sysfrom(c, "s7", "e")

    print("\n######## S8. INSERT/DELETE defaults under a backfill SYSTEM_TIME")
    tx(
        c,
        "S8a INSERT with no _valid_from at SYSTEM_TIME 2020-01-06",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(6)}')",
        [ins("s8", {"_id": "lbl", "v": 1})],
    )
    tx(
        c,
        "S8b plain DELETE (no FOR PORTION) at SYSTEM_TIME 2020-01-08",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(8)}')",
        [("DELETE FROM s8 WHERE _id = 'lbl'", None)],
    )
    sysfrom(c, "s8", "lbl")
    tx(
        c,
        "S8c re-add at 2020-01-09; DELETE FOR PORTION OF VALID_TIME FROM 01-10 TO NULL at 2020-01-10",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(9)}')",
        [ins("s8", {"_id": "lbl", "v": 2})],
    )
    tx(
        c,
        "S8c delete open-ended with TO NULL",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(10)}')",
        [
            (
                "DELETE FROM s8 FOR PORTION OF VALID_TIME FROM TIMESTAMP '2020-01-10T00:00:00Z' TO NULL "
                "WHERE _id = 'lbl'",
                None,
            )
        ],
    )
    sysfrom(c, "s8", "lbl")
    tx(
        c,
        "S8d INSERT with a _valid_from AFTER the tx SYSTEM_TIME (future-dated)",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(11)}')",
        [ins("s8", {"_id": "fut", "v": 1, "_valid_from": tsv(T(20))})],
    )
    sysfrom(c, "s8", "fut")

    print("\n######## S9. Bitemporal reads: a backdated correction")
    tx(
        c,
        "S9a sys 01-12: price=10 valid from 01-12",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(12)}')",
        [ins("s9", {"_id": "p", "v": 10})],
    )
    tx(
        c,
        "S9b sys 01-14: correction price=9 valid [01-11, 01-13)",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(14)}')",
        [ins("s9", {"_id": "p", "v": 9, "_valid_from": tsv(T(11)), "_valid_to": tsv(T(13))})],
    )
    sysfrom(c, "s9", "p")
    for sys_t, val_t in [(T(13), T(12)), (T(15), T(12)), (T(15), T(11)), (T(13), T(11))]:
        q(
            c,
            f"S9 FOR SYSTEM_TIME AS OF {sys_t} FOR VALID_TIME AS OF {val_t}",
            f"SELECT v FROM s9 FOR SYSTEM_TIME AS OF TIMESTAMP '{sys_t}' "
            f"FOR VALID_TIME AS OF TIMESTAMP '{val_t}' WHERE _id = 'p'",
        )
    q(
        c,
        "S9 FOR SYSTEM_TIME BETWEEN 01-12 AND 01-14 (inclusive end)",
        f"SELECT v, _system_from, _system_to, _valid_from, _valid_to FROM s9 FOR SYSTEM_TIME BETWEEN "
        f"TIMESTAMP '{T(12)}' AND TIMESTAMP '{T(14)}' FOR ALL VALID_TIME ORDER BY _system_from, _valid_from",
    )
    q(
        c,
        "S9 FOR SYSTEM_TIME FROM 01-12 TO 01-14 (exclusive end)",
        f"SELECT v, _system_from, _system_to, _valid_from, _valid_to FROM s9 FOR SYSTEM_TIME FROM "
        f"TIMESTAMP '{T(12)}' TO TIMESTAMP '{T(14)}' FOR ALL VALID_TIME ORDER BY _system_from, _valid_from",
    )
    q(
        c,
        "S9 SETTING DEFAULT SYSTEM_TIME AS OF 01-13, DEFAULT VALID_TIME AS OF 01-11",
        f"SETTING DEFAULT SYSTEM_TIME AS OF TIMESTAMP '{T(13)}', DEFAULT VALID_TIME AS OF TIMESTAMP '{T(11)}' "
        "SELECT v FROM s9 WHERE _id = 'p'",
    )
    q(
        c,
        "S9 FOR SYSTEM_TIME AS OF with a bind parameter",
        "SELECT v FROM s9 FOR SYSTEM_TIME AS OF %s FOR VALID_TIME AS OF %s WHERE _id = 'p'",
        [dt.datetime(2020, 1, 15, tzinfo=dt.UTC), dt.datetime(2020, 1, 11, 12, tzinfo=dt.UTC)],
    )

    print("\n######## S11. A null-recorded row loaded LAST, at wall-clock system time")
    tx(
        c,
        "S11a sys 01-21: v1 valid from 01-21",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(21)}')",
        [ins("s11", {"_id": "n", "v": 1}), ins("s11", {"_id": "m", "v": 1})],
    )
    tx(
        c,
        "S11b sys 01-22: v2 valid from 01-22",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(22)}')",
        [ins("s11", {"_id": "n", "v": 2}), ins("s11", {"_id": "m", "v": 2})],
    )
    tx(
        c,
        "S11c wall clock: seed v0 valid [01-15, 01-21) on n (bounded), [01-15, inf) on m (unbounded)",
        "BEGIN READ WRITE",
        [
            ins("s11", {"_id": "n", "v": 0, "_valid_from": tsv(T(15)), "_valid_to": tsv(T(21))}),
            ins("s11", {"_id": "m", "v": 0, "_valid_from": tsv(T(15))}),
        ],
    )
    q(
        c,
        "S11 current-system-time history of n and m",
        "SELECT _id, v, _valid_from, _valid_to FROM s11 FOR ALL VALID_TIME ORDER BY _id, _valid_from",
    )
    tx(
        c,
        "S11d after the wall-clock tx, backfill at 2020-01-23",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(23)}')",
        [ins("s11", {"_id": "z", "v": 1})],
    )
    future = (dt.datetime.now(dt.UTC) + dt.timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    tx(
        c,
        f"S11e future SYSTEM_TIME {future}",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{future}')",
        [ins("s11", {"_id": "f", "v": 1})],
    )
    tx(
        c,
        "S11f plain wall-clock tx after a future-dated one",
        "BEGIN READ WRITE",
        [ins("s11", {"_id": "after-future", "v": 1})],
    )
    q(c, "S11 rows visible now", "SELECT _id, _system_from FROM s11 ORDER BY _system_from")
    last_tx(c, 3)

    print("\n######## S10. COMMIT ASYNC with an out-of-order SYSTEM_TIME")
    tx(
        c,
        "S10a ASYNC, out of order (2020-01-02)",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(2)}')",
        [ins("s10", {"_id": "a", "v": 1})],
        commit="COMMIT ASYNC",
    )
    tx(
        c,
        "S10b ASYNC, in order (2020-01-20)",
        f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{T(20)}')",
        [ins("s10", {"_id": "b", "v": 1})],
        commit="COMMIT ASYNC",
    )
    q(c, "S10 rows (after awaiting)", "SELECT _id FROM s10")
    last_tx(c, 2)


def main4():
    """Follow-up on the part-2 node: rows and txs whose system time is in the future."""
    c = connect(autocommit=True)
    q(
        c,
        "txs, every row",
        "SELECT _id, committed, system_time, (error IS NOT NULL) AS has_error "
        "FROM xt.txs FOR ALL SYSTEM_TIME FOR ALL VALID_TIME ORDER BY _id DESC LIMIT 6",
    )
    q(
        c,
        "s11 all system time",
        "SELECT _id, v, _system_from, _valid_from FROM s11 "
        "FOR ALL SYSTEM_TIME FOR ALL VALID_TIME ORDER BY _system_from",
    )
    q(c, "s10 all", "SELECT _id, _system_from FROM s10 FOR ALL SYSTEM_TIME FOR ALL VALID_TIME")


if __name__ == "__main__":
    import sys

    {"1": main, "2": main2, "3": main3, "4": main4}[(sys.argv[1:] or ["1"])[0]]()
