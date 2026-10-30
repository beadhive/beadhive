"""As-of reproduction and system-time reads against the full load (run after load.py).

V1  FOR VALID_TIME AS OF each cut, every bead, vs the plain-Python replay (history.py) and vs
    bd's own pre-image witnesses; the 60-bead stratified sample reported separately.
V2  current state (no temporal clause) vs bh-export.jsonl.
V3  FOR SYSTEM_TIME reads: AS OF each cut vs VALID_TIME AS OF the same cut, both axes pinned,
    a real label whose removal shows the two axes apart, BETWEEN on one bead, row census.
"""

# ruff: noqa: E501  (throwaway spike script: long SQL literals kept on one line)

from __future__ import annotations

import collections
import datetime as dt
import json
import time

from history import SCALAR_CMP, TEXT, h
from xt import WORK, connect, fmt, parse_ts, q

COLS = ", ".join(["_id", *SCALAR_CMP, *TEXT])


def iso(v):
    if isinstance(v, dt.datetime):
        return v.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return v


def read_state(conn, clause: str, params=()):
    """{bead_id: projection} for every bead visible under `clause` (a FOR ... AS OF ...)."""
    t0 = time.monotonic()
    beads = conn.execute(f"SELECT {COLS} FROM beads {clause}", params).fetchall()
    labels = conn.execute(f"SELECT bead_id, label FROM bead_labels {clause}", params).fetchall()
    edges = conn.execute(f"SELECT from_id, type, to_id FROM bead_edges {clause}", params).fetchall()
    secs = time.monotonic() - t0
    out = {}
    for r in beads:
        p = dict(zip(SCALAR_CMP, (iso(v) for v in r[1 : 1 + len(SCALAR_CMP)]), strict=True))
        p.update({k: h(v) for k, v in zip(TEXT, r[1 + len(SCALAR_CMP) :], strict=True)})
        p["labels"], p["edges"] = [], []
        out[r[0]] = p
    for bid, lbl in labels:
        out.setdefault(bid, {"labels": [], "edges": [], "orphan": True})["labels"].append(lbl)
    for src, typ, dst in edges:
        out.setdefault(src, {"labels": [], "edges": [], "orphan": True})["edges"].append(
            f"{typ}|{dst}"
        )
    for p in out.values():
        p["labels"].sort()
        p["edges"].sort()
    return out, secs, (len(beads), len(labels), len(edges))


def compare(expected: dict, got: dict, fields) -> tuple[int, int, collections.Counter, list]:
    match = differ = 0
    by_field, examples = collections.Counter(), []
    for bid, exp in expected.items():
        g = got.get(bid)
        if exp is None and g is None:
            match += 1
            continue
        if exp is None or g is None:
            differ += 1
            by_field["existence"] += 1
            examples.append((bid, "existence", exp is not None, g is not None))
            continue
        bad = [f for f in fields if f in exp and exp.get(f) != g.get(f)]
        if bad:
            differ += 1
            by_field.update(bad)
            if len(examples) < 6:
                examples.append((bid, bad[0], exp.get(bad[0]), g.get(bad[0])))
        else:
            match += 1
    return match, differ, by_field, examples


def report(title, res):
    m, d, f, ex = res
    print(f"   {title}: {m} match, {d} differ" + (f"  by field {dict(f)}" if f else ""))
    for e in ex[:4]:
        print("      e.g.", tuple(str(x)[:70] for x in e))


def main() -> None:
    with open(WORK / "expected.json") as fh:
        X = json.load(fh)
    conn = connect(autocommit=True)
    fields = [*SCALAR_CMP, *TEXT, "labels", "edges"]
    sample = X["sample"]
    print("# verify.py")

    print("\n## V1. FOR VALID_TIME AS OF <cut> vs the plain-Python replay of the events table")
    for cut in [*X["cuts"], X["pre_window_cut"]]:
        got, secs, n = read_state(conn, "FOR VALID_TIME AS OF %s", [parse_ts(cut)])
        exp = X["expected"][cut]
        print(
            f"\n-- cut {cut}: XTDB rows beads/labels/edges = {n}, read {secs:.2f} s; "
            f"replay beads existing = {sum(v is not None for v in exp.values())}"
        )
        report("all 9088 beads", compare(exp, got, fields))
        report(f"sample of {len(sample)}", compare({b: exp[b] for b in sample}, got, fields))
        if cut in X["witness"]:
            w = X["witness"][cut]
            wexp = {
                b: {k: v for k, v in x.items() if k not in ("event", "at")} for b, x in w.items()
            }
            report(
                f"bd pre-image witnesses ({len(w)} beads)",
                compare(
                    wexp, got, ["status", "assignee", "priority", "title", "issue_type", "labels"]
                ),
            )
            rexp = {b: X["expected"][cut][b] for b in w}
            report(
                "replay vs the same witnesses",
                compare(
                    {b: {k: v for k, v in wexp[b].items()} for b in w},
                    {b: rexp[b] for b in w},
                    ["status", "assignee", "priority", "title", "issue_type", "labels"],
                ),
            )

    print("\n## V2. Current state (no temporal clause) vs bh-export.jsonl and vs the replay's end")
    got, secs, n = read_state(conn, "")
    fin = X["final"]
    print(f"   read {secs:.2f} s, rows beads/labels/edges = {n}")
    report("XTDB now vs export", compare({b: v["export"] for b, v in fin.items()}, got, fields))
    report(
        "XTDB now vs replay end state",
        compare(
            {b: {k: v for k, v in x.items() if k != "export"} for b, x in fin.items()}, got, fields
        ),
    )

    print("\n## V3. System time")
    for cut in X["cuts"]:
        a, sa, _ = read_state(conn, "FOR SYSTEM_TIME AS OF %s", [parse_ts(cut)])
        b, sb, _ = read_state(conn, "FOR VALID_TIME AS OF %s", [parse_ts(cut)])
        c, sc, _ = read_state(
            conn, "FOR SYSTEM_TIME AS OF %s FOR VALID_TIME AS OF %s", [parse_ts(cut), parse_ts(cut)]
        )
        same_ab = sum(a.get(k) == b.get(k) for k in set(a) | set(b))
        same_cb = sum(c.get(k) == b.get(k) for k in set(c) | set(b))
        print(
            f"   cut {cut}: SYSTEM_TIME AS OF == VALID_TIME AS OF for {same_ab}/{len(set(a) | set(b))}"
            f" beads; both axes pinned == VALID_TIME AS OF for {same_cb}/{len(set(c) | set(b))}"
            f"  (reads {sa:.2f}/{sb:.2f}/{sc:.2f} s)"
        )

    row = conn.execute(
        """SELECT bead_id, label, _valid_from, _valid_to FROM bead_labels FOR ALL VALID_TIME
           WHERE label = 'review:pending' AND _valid_to IS NOT NULL
           ORDER BY _valid_to DESC LIMIT 1"""
    ).fetchone()
    bid, lbl, vf, vt = row
    print(f"\n   real label period: {bid} {lbl} [{fmt(vf)}, {fmt(vt)})")
    before, after = vt - dt.timedelta(seconds=1), vt + dt.timedelta(seconds=1)
    for sys_t, val_t, what in [
        (before, after, "belief just before the removal was recorded"),
        (after, after, "belief just after"),
        (before, before, "both axes before the removal"),
    ]:
        r = conn.execute(
            "SELECT COUNT(*) FROM bead_labels FOR SYSTEM_TIME AS OF %s FOR VALID_TIME AS OF %s "
            "WHERE bead_id = %s AND label = %s",
            [sys_t, val_t, bid, lbl],
        ).fetchone()[0]
        print(
            f"   SYSTEM_TIME AS OF {fmt(sys_t)} VALID_TIME AS OF {fmt(val_t)}: present={bool(r)}"
            f"  ({what})"
        )
    q(
        conn,
        f"every bitemporal row of {bid}|{lbl}",
        """SELECT _valid_from, _valid_to, _system_from, _system_to FROM bead_labels
         FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE _id = %s ORDER BY _system_from, _valid_from""",
        [f"{bid}|{lbl}"],
        limit=8,
    )

    q(
        conn,
        "bead versions of bh-q0lol.5 recorded in system time BETWEEN 09-02 and 09-03",
        """SELECT status, assignee, src, _valid_from, _system_from, _system_to FROM beads
         FOR SYSTEM_TIME BETWEEN TIMESTAMP '2026-09-02T00:00:00Z' AND TIMESTAMP '2026-09-03T00:00:00Z'
         FOR ALL VALID_TIME WHERE _id = 'bh-q0lol.5' ORDER BY _system_from, _valid_from""",
        limit=10,
    )
    for table in ("beads", "bead_labels", "bead_edges", "bead_comments"):
        q(
            conn,
            f"{table} census",
            f"""SELECT COUNT(*) AS bitemporal_rows, COUNT(DISTINCT _id) AS entities,
              COUNT(DISTINCT _system_from) AS distinct_system_from, MIN(_system_from), MAX(_system_from)
              FROM {table} FOR ALL SYSTEM_TIME FOR ALL VALID_TIME""",
        )
    q(
        conn,
        "rows whose system time differs from their valid-from (expect 0: recorded = occurred)",
        """SELECT COUNT(*) FROM beads FOR ALL SYSTEM_TIME FOR ALL VALID_TIME
         WHERE _system_from <> _valid_from AND _system_to IS NULL AND _valid_to IS NULL""",
    )
    t0 = time.monotonic()
    q(
        conn,
        "lookup one bead as of a cut (latency)",
        "SELECT status, assignee FROM beads FOR VALID_TIME AS OF TIMESTAMP '2026-09-18T00:00:00Z' "
        "WHERE _id = 'bh-q0lol.5'",
    )
    print(f"   {1000 * (time.monotonic() - t0):.0f} ms")


if __name__ == "__main__":
    main()
