"""Column type widening on heterogeneous fields: what the column becomes, what clients read,
and how predicates behave once a column holds a union of types.
"""

from __future__ import annotations

import collections
import json
import uuid

from common import Transit, connect, decode_transit, load_events, set_output, to_transit
from psycopg.types.numeric import Int2

RUN = uuid.uuid4().hex[:6]  # fresh tables per run: widening is permanent for a table


def coltype(conn, table, col):
    r = conn.execute(
        "SELECT data_type FROM information_schema.columns WHERE table_name=%s AND column_name=%s",
        [table, col],
    ).fetchone()
    return r[0] if r else "(none)"


def show(conn, table, col, label):
    out = {"after": label, "type": coltype(conn, table, col)}
    for fmt in ("json", "transit"):
        set_output(conn, fmt)
        cur = conn.execute(f"SELECT _id, {col} FROM {table} ORDER BY _id")
        tc = cur.description[1].type_code
        out[fmt] = [
            (r[0], type(r[1]).__name__, r[1] if tc != 16384 else f"transit:{r[1]}")
            for r in cur.fetchall()
        ]
    set_output(conn, "json")
    print(json.dumps(out, default=str))


def q(conn, sql, params=None):
    try:
        rows = conn.execute(sql, params).fetchall()
        print("  OK ", sql, "->", rows)
    except Exception as e:  # noqa: BLE001
        print("  ERR", sql, "->", str(e).splitlines()[0][:160])


def main():
    conn = connect()

    print("-- 1. timestamp then string in the same column")
    t = f"wid_ts_{RUN}"
    conn.execute(
        f"INSERT INTO {t} RECORDS %s", [Transit({"_id": 1, "ts": "~t2026-10-30T19:25:00Z"})]
    )
    show(conn, t, "ts", "timestamp row")
    conn.execute(f"INSERT INTO {t} RECORDS %s", [Transit({"_id": 2, "ts": "2026-10-30T19:25:00Z"})])
    show(conn, t, "ts", "+ string row")
    q(conn, f"SELECT _id FROM {t} WHERE ts < TIMESTAMP '2027-01-01T00:00:00Z' ORDER BY _id")
    q(conn, f"SELECT _id FROM {t} WHERE ts = '2026-10-30T19:25:00Z' ORDER BY _id")
    q(conn, f"SELECT _id FROM {t} ORDER BY ts")

    print("-- 2. bigint then string (priority 2 vs 'P2')")
    t = f"wid_int_{RUN}"
    conn.execute(f"INSERT INTO {t} RECORDS %s", [Transit({"_id": 1, "p": 2})])
    show(conn, t, "p", "int row")
    conn.execute(f"INSERT INTO {t} RECORDS %s", [Transit({"_id": 2, "p": "P2"})])
    show(conn, t, "p", "+ string row")
    q(conn, f"SELECT _id FROM {t} WHERE p < 3")
    q(conn, f"SELECT _id FROM {t} WHERE p = 'P2'")

    print("-- 3. native psycopg ints: int2 then int8")
    t = f"wid_nat_{RUN}"
    conn.execute(f"INSERT INTO {t} RECORDS {{_id: %s, p: %s}}", ["a", Int2(2)])
    show(conn, t, "p", "int2 row (psycopg default for small ints)")
    conn.execute(f"INSERT INTO {t} RECORDS {{_id: %s, p: %s}}", ["b", 2**40])
    show(conn, t, "p", "+ int8 row")
    q(conn, f"SELECT _id FROM {t} WHERE p > 1 ORDER BY _id")

    print("-- 4. list[str] then list[int] then scalar string")
    t = f"wid_list_{RUN}"
    conn.execute(f"INSERT INTO {t} RECORDS %s", [Transit({"_id": 1, "l": ["a"]})])
    show(conn, t, "l", "list[str]")
    conn.execute(f"INSERT INTO {t} RECORDS %s", [Transit({"_id": 2, "l": [1]})])
    show(conn, t, "l", "+ list[int]")
    conn.execute(f"INSERT INTO {t} RECORDS %s", [Transit({"_id": 3, "l": "x"})])
    show(conn, t, "l", "+ string")
    q(conn, f"SELECT x._id, v.e FROM {t} x, UNNEST(x.l) AS v(e) ORDER BY x._id")

    print("-- 5. nested struct field int then string")
    t = f"wid_nest_{RUN}"
    conn.execute(f"INSERT INTO {t} RECORDS %s", [Transit({"_id": 1, "m": {"a": 1, "b": "k"}})])
    conn.execute(f"INSERT INTO {t} RECORDS %s", [Transit({"_id": 2, "m": {"a": "x"}})])
    show(conn, t, "m", "struct{a:int} + struct{a:str}")
    q(conn, f"SELECT _id, (m).a, (m).b FROM {t} ORDER BY _id")
    q(conn, f"SELECT _id FROM {t} WHERE (m).a = 1")

    print("-- 6. real data: bd events new_value with embedded JSON decoded")
    t = f"wid_ev_{RUN}"
    rows = []
    for e in load_events():
        v = e["new_value"]
        try:
            dec = json.loads(v) if v and v[:1] in "{[" else v
        except ValueError:
            dec = v
        rows.append(
            {"_id": "~u" + e["id"], "event_type": e["event_type"], "new_value": to_transit(dec)}
        )
    for i in range(0, len(rows), 1000):
        with conn.transaction():
            chunk = rows[i : i + 1000]
            conn.execute(
                f"INSERT INTO {t} RECORDS " + ", ".join(["%s"] * len(chunk)),
                [Transit(r) for r in chunk],
            )
    print("  column type:", coltype(conn, t, "new_value"))
    for fmt in ("json", "transit"):
        set_output(conn, fmt)
        cur = conn.execute(f"SELECT event_type, new_value FROM {t}")
        tc = cur.description[1].type_code
        kinds = collections.Counter()
        for _et, v in cur.fetchall():
            if tc == 16384 and v is not None:
                v = decode_transit(v)
            kinds[(type(v).__name__)] += 1
        print(f"  {fmt}: wire type_code={tc} python types={dict(kinds)}")
    set_output(conn, "json")
    q(conn, f"SELECT count(*) FROM {t} WHERE (new_value).status = 'in_progress'")
    q(conn, f"SELECT count(*) FROM {t} WHERE new_value = ''")


if __name__ == "__main__":
    main()
