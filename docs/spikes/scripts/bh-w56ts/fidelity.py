"""pgwire type fidelity from Python (psycopg 3): write via transit RECORDS, read back two ways.

Each case gets its own table so the column type is not widened by other cases. The value is
stored twice per row: top-level column `top` and nested inside a struct `nested.inner`.
Read-back modes: default JSON fallback output, and SET fallback_output_format='transit'.
"""

from __future__ import annotations

import datetime as dt
import uuid

from common import Transit, connect, decode_transit, set_output
from transit.transit_types import Boolean, Keyword, TaggedValue, false, frozendict, true

BIG = "x" * (1024 * 1024)
UNI = "em—dash 中文 \U0001f41d emoji é שלום"
TS = dt.datetime(2026, 10, 30, 19, 25, 0, 123456, tzinfo=dt.UTC)

# name, transit-encoded value, expected python value
CASES = [
    ("text", "hello", "hello"),
    ("text_1mib", BIG, BIG),
    ("unicode", UNI, UNI),
    ("text_tilde", "~~not a tag", "~not a tag"),
    ("int", 2, 2),
    ("int_big", 2**62, 2**62),
    ("float", 1.5, 1.5),
    ("bool", True, True),
    ("ts_tagged", "~t2026-10-30T19:25:00.123456Z", TS),
    ("ts_instant_tag", {"~#time/instant": "2026-10-30T19:25:00.123456Z"}, TS),
    ("ts_plain_string", "2026-10-30T19:25:00Z", "2026-10-30T19:25:00Z"),
    ("list_str", ["a", "b"], ["a", "b"]),
    ("list_empty", [], []),
    ("list_mixed", [1, "a", True], [1, "a", True]),
    ("set_str", {"~#set": ["b", "a"]}, {"a", "b"}),
    ("set_empty", {"~#set": []}, set()),
    ("struct", {"git.commits": '["abc"]', "n": 1}, {"git.commits": '["abc"]', "n": 1}),
    ("struct_deep", {"a": {"b": {"c": [1, {"d": "e"}]}}}, {"a": {"b": {"c": [1, {"d": "e"}]}}}),
    ("struct_empty", {}, {}),
    (
        "list_of_struct",
        [{"type": "blocks", "at": "~t2026-10-30T19:25:00Z"}],
        [{"type": "blocks", "at": TS.replace(microsecond=0)}],
    ),
    ("json_text", '{"holder":"dev/x"}', '{"holder":"dev/x"}'),
    (
        "uuid",
        "~u5ce912df-0cd0-5de8-96c9-0b04d7fd512f",
        uuid.UUID("5ce912df-0cd0-5de8-96c9-0b04d7fd512f"),
    ),
    ("keyword", "~:blocks", "blocks"),
]


def py(v):
    """Normalise a transit-decoded value to plain python for equality checks."""
    if isinstance(v, TaggedValue):
        if v.tag == "time/zoned-date-time":
            s = v.rep.replace("[UTC]", "")
            return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return ("tag", v.tag, py(v.rep))
    if isinstance(v, Boolean):  # transit-python2 decodes true/false to its own singletons
        return v is true if v in (true, false) else bool(v)
    if isinstance(v, Keyword):
        return str(v)
    if isinstance(v, (dict, frozendict)):
        return {k: py(x) for k, x in v.items()}
    if isinstance(v, frozenset):
        return {py(x) for x in v}
    if isinstance(v, (list, tuple)):
        return [py(x) for x in v]
    if isinstance(v, dt.datetime):
        return v.astimezone(dt.UTC)
    return v


def kind(v):
    if v is None:
        return "NULL"
    t = type(v).__name__
    if isinstance(v, list) and v:
        t += "[" + ",".join(sorted({type(x).__name__ for x in v})) + "]"
    return t


def short(v, n=60):
    s = repr(v)
    return s if len(s) <= n else s[:n] + f"...({len(s)} chars)"


def main():
    conn = connect()
    for name, enc, _exp in CASES:
        conn.execute(
            f"INSERT INTO fid_{name} RECORDS %s",
            [Transit({"_id": name, "top": enc, "nested": {"inner": enc}})],
        )
    print("case | col type | top/json | nested/json | top/transit | nested/transit")
    for name, _enc, exp in CASES:
        coltype = conn.execute(
            "SELECT data_type FROM information_schema.columns"
            " WHERE table_name=%s AND column_name='top'",
            [f"fid_{name}"],
        ).fetchone()
        coltype = coltype[0] if coltype else "(no column)"
        out = []
        for fmt in ("json", "transit"):
            set_output(conn, fmt)
            cur = conn.execute(f"SELECT top, nested FROM fid_{name}")
            top, nested = cur.fetchone()
            codes = [d.type_code for d in cur.description]
            if codes[0] == 16384:
                top = decode_transit(top)
            if codes[1] == 16384:
                nested = decode_transit(nested)
            inner = (
                None
                if nested is None
                else (nested.get("inner") if hasattr(nested, "get") else nested)
            )
            for v in (top, inner):
                ok = py(v) == exp
                out.append(f"{kind(v)} {'OK' if ok else 'DIFF ' + short(v)}")
        print(f"{name} | {coltype} | " + " | ".join([out[0], out[1], out[2], out[3]]))
    set_output(conn, "json")

    # ---- null vs absent
    print("\n-- null vs absent")
    tn = "fid_null_" + uuid.uuid4().hex[:8]  # fresh table per run
    conn.execute(
        f"INSERT INTO {tn} RECORDS %s, %s",
        [
            Transit({"_id": "present-null", "v": None, "keep": 1, "n": {"a": None, "b": 1}}),
            Transit({"_id": "absent", "keep": 1, "n": {"b": 1}}),
        ],
    )
    cols = [
        r[0]
        for r in conn.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name=%s "
            "ORDER BY column_name",
            [tn],
        ).fetchall()
        if not r[0].startswith("_")
    ]
    print("user columns:", cols)
    set_output(conn, "transit")
    for r in conn.execute(f"SELECT _id, n FROM {tn} ORDER BY _id").fetchall():
        print(r[0], "n =", dict(decode_transit(r[1])))
    set_output(conn, "json")
    conn.execute(f"INSERT INTO {tn} RECORDS %s", [Transit({"_id": "has-v", "v": "x"})])
    for r in conn.execute(f"SELECT _id, v FROM {tn} ORDER BY _id").fetchall():
        print(r)

    # ---- NUL character (Postgres text forbids it)
    print("\n-- NUL char")
    try:
        conn.execute("INSERT INTO fid_nul RECORDS %s", [Transit({"_id": "nul", "t": "a\u0000b"})])
        print(conn.execute("SELECT t FROM fid_nul").fetchone())
    except Exception as e:  # noqa: BLE001
        print("ERR", str(e).splitlines()[0][:200])

    # ---- typed per-field params instead of transit (psycopg native adapters)
    print("\n-- native psycopg params in RECORDS {k: %s}")
    from psycopg.types.json import Jsonb

    conn.execute(
        "INSERT INTO fid_native RECORDS {_id: %s, ts: %s, l: %s, j: %s, i: %s}",
        ["n1", TS, ["a", "b"], Jsonb({"x": [1, 2], "at": "2026-10-30T19:25:00Z"}), 7],
    )
    cur = conn.execute("SELECT ts, l, j, i FROM fid_native")
    row = cur.fetchone()
    print(
        [
            (d.name, d.type_code, kind(v), short(v))
            for d, v in zip(cur.description, row, strict=True)
        ]
    )


if __name__ == "__main__":
    main()
