"""Which Postgres drivers work against XTDB 2.2.0 pgwire? psycopg 3, asyncpg, pg8000.

Per driver: connect with no password, SELECT version(), count a table, parameterised SELECT,
typed DML (INSERT ... RECORDS with a bound parameter), read back, timestamptz round trip.
Usage: python drivers.py [port]
"""

import asyncio
import json
import sys

port = int(sys.argv[1]) if len(sys.argv) > 1 else 15434
res = {}


def attempt(name, fn):
    try:
        res[name] = fn()
    except Exception as e:  # noqa: BLE001
        res[name] = f"FAIL {type(e).__name__}: {str(e)[:300]}"


def psycopg_run():
    import psycopg
    from psycopg.types.string import StrDumper

    out = {}
    c = psycopg.connect(host="127.0.0.1", port=port, user="xtdb", dbname="xtdb", autocommit=True)
    c.adapters.register_dumper(str, StrDumper)
    out["version"] = c.execute("SELECT version()").fetchone()[0]
    out["count"] = c.execute("SELECT count(*) FROM beads_b").fetchone()[0]
    out["param_select"] = c.execute(
        "SELECT title FROM beads_b WHERE _id = %s", ["bh-q0lol.5"]
    ).fetchone()[0][:40]
    c.execute("INSERT INTO drv_probe RECORDS {_id: %s, who: %s, n: %s}", ["psycopg", "psycopg", 7])
    out["dml_readback"] = c.execute(
        "SELECT who, n FROM drv_probe WHERE _id = %s", ["psycopg"]
    ).fetchone()
    ts = c.execute("SELECT created_at FROM beads_b WHERE _id = %s", ["bh-q0lol.5"]).fetchone()[0]
    out["timestamptz"] = f"{type(ts).__name__} {ts.isoformat()}"
    return out


def pg8000_run():
    import pg8000.native

    out = {}
    c = pg8000.native.Connection(user="xtdb", host="127.0.0.1", port=port, database="xtdb")
    out["version"] = c.run("SELECT version()")[0][0]
    out["count"] = c.run("SELECT count(*) FROM beads_b")[0][0]
    out["param_select"] = c.run("SELECT title FROM beads_b WHERE _id = :i", i="bh-q0lol.5")[0][0][
        :40
    ]
    try:
        c.run(
            "INSERT INTO drv_probe (_id, who, n) VALUES (:i, :w, :n)", i="pg8000", w="pg8000", n=7
        )
    except Exception as e:  # noqa: BLE001
        out["dml_bound_params"] = f"FAIL {type(e).__name__}: {str(e)[:100]}"
        c.run("INSERT INTO drv_probe RECORDS {_id: 'pg8000', who: 'pg8000', n: 7}")
        out["dml_literal_only"] = "ok"
    out["dml_readback"] = c.run("SELECT who, n FROM drv_probe WHERE _id = :i", i="pg8000")[0]
    ts = c.run("SELECT created_at FROM beads_b WHERE _id = :i", i="bh-q0lol.5")[0][0]
    out["timestamptz"] = f"{type(ts).__name__} {ts.isoformat()}"
    c.close()
    return out


async def asyncpg_run():
    import asyncpg

    out = {}
    c = await asyncpg.connect(host="127.0.0.1", port=port, user="xtdb", database="xtdb")
    out["version"] = await c.fetchval("SELECT version()")
    out["count"] = await c.fetchval("SELECT count(*) FROM beads_b")
    out["param_select"] = (
        await c.fetchval("SELECT title FROM beads_b WHERE _id = $1", "bh-q0lol.5")
    )[:40]
    try:
        await c.execute(
            "INSERT INTO drv_probe RECORDS {_id: $1, who: $2, n: $3}", "asyncpg", "asyncpg", 7
        )
    except Exception as e:  # noqa: BLE001
        out["dml_bound_params"] = f"FAIL {type(e).__name__}: {str(e)[:90]}"
        await c.execute("INSERT INTO drv_probe RECORDS {_id: 'asyncpg', who: 'asyncpg', n: 7}")
        out["dml_literal_only"] = "ok"
    out["dml_readback"] = tuple(
        await c.fetchrow("SELECT who, n FROM drv_probe WHERE _id = $1", "asyncpg")
    )
    ts = await c.fetchval("SELECT created_at FROM beads_b WHERE _id = $1", "bh-q0lol.5")
    out["timestamptz"] = f"{type(ts).__name__} {ts.isoformat()}"
    await c.close()
    return out


attempt("psycopg", psycopg_run)
attempt("pg8000", pg8000_run)
try:
    res["asyncpg"] = asyncio.run(asyncpg_run())
except Exception as e:  # noqa: BLE001
    res["asyncpg"] = f"FAIL {type(e).__name__}: {str(e)[:300]}"
print(json.dumps(res, indent=1, default=str))
