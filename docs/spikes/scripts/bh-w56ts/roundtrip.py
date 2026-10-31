"""Read every bead back over pgwire and compare field-by-field with the export.

Checks, per issue_type:
  A/transit  layout (a), SET fallback_output_format='transit' (typed nested values)
  A/json     layout (a), default JSON fallback output (what a naive client sees)
  B/transit  layout (b), bead doc + current label/edge/comment rows reassembled
"""

from __future__ import annotations

import collections
import datetime as dt
import json

from common import connect, decode_transit, iso_z, load_events, load_export, set_output
from transit.transit_types import TaggedValue, frozendict

TEMPORAL = {"_valid_from", "_valid_to", "_system_from", "_system_to"}


def norm(v):
    """Normalise a read-back value into export-comparable JSON."""
    if isinstance(v, dt.datetime):
        return iso_z(v)
    if isinstance(v, TaggedValue):
        if v.tag == "time/zoned-date-time":
            return iso_z(v.rep)
        return {"<tag>": v.tag, "rep": norm(v.rep)}
    if isinstance(v, (dict, frozendict)):
        return {k: norm(x) for k, x in v.items()}
    if isinstance(v, frozenset):
        return sorted(norm(x) for x in v)
    if isinstance(v, (list, tuple)):
        return [norm(x) for x in v]
    return v


def fetch(conn, sql):
    cur = conn.execute(sql)
    names = [d.name for d in cur.description]
    transit_cols = {i for i, d in enumerate(cur.description) if d.type_code == 16384}
    rows = []
    for raw in cur.fetchall():
        rec = {}
        for i, (n, v) in enumerate(zip(names, raw, strict=True)):
            if v is None or n in TEMPORAL:
                continue
            rec[n] = decode_transit(v) if i in transit_cols else v
        rows.append(rec)
    return rows


def to_export_shape(rec: dict) -> dict:
    out = {("_type" if k == "bd_record_type" else k): norm(v) for k, v in rec.items() if k != "_id"}
    return out


def expected(b: dict) -> dict:
    e = dict(b)
    if "labels" in e:
        e["labels"] = sorted(e["labels"])
    return e


def diff(exp: dict, got: dict) -> list[str]:
    causes = []
    for k in sorted(set(exp) | set(got)):
        if k not in got:
            causes.append(f"{k}:missing")
        elif k not in exp:
            causes.append(f"{k}:extra")
        elif exp[k] != got[k]:
            causes.append(f"{k}:value")
    return causes


def report(name, beads, got_by_id):
    per_type = collections.defaultdict(lambda: [0, 0])
    causes = collections.Counter()
    examples = {}
    for b in beads:
        t = b["issue_type"]
        per_type[t][0] += 1
        got = got_by_id.get(b["id"])
        c = ["row:missing"] if got is None else diff(expected(b), got)
        if not c:
            per_type[t][1] += 1
        for x in c:
            causes[(t, x)] += 1
            if x not in examples:
                f = x.split(":")[0]
                examples[x] = (
                    b["id"],
                    json.dumps(expected(b).get(f), ensure_ascii=False)[:160],
                    json.dumps(got.get(f) if got else None, ensure_ascii=False)[:160],
                )
    print(f"\n== {name}: rows read {len(got_by_id)}")
    print("issue_type | beads | field-equal")
    for t, (n, ok) in sorted(per_type.items(), key=lambda kv: -kv[1][0]):
        print(f"{t} | {n} | {ok}")
    for (t, x), n in sorted(causes.items()):
        print(f"  divergence {t} {x}: {n}")
    for x, ex in examples.items():
        print(f"  example {x}: {ex[0]}\n    export={ex[1]}\n    xtdb  ={ex[2]}")


def main():
    beads = load_export()
    conn = connect()

    set_output(conn, "transit")
    a_t = {r["_id"]: to_export_shape(r) for r in fetch(conn, "SELECT * FROM beads_a")}
    report("A/transit", beads, a_t)

    set_output(conn, "json")
    cur = conn.execute("SELECT * FROM beads_a")
    names = [d.name for d in cur.description]
    a_j = {}
    for raw in cur.fetchall():
        rec = {n: v for n, v in zip(names, raw, strict=True) if v is not None and n not in TEMPORAL}
        a_j[rec["_id"]] = to_export_shape(rec)
    report("A/json", beads, a_j)

    set_output(conn, "transit")
    docs = {r["_id"]: to_export_shape(r) for r in fetch(conn, "SELECT * FROM beads_b")}
    labels = collections.defaultdict(list)
    for r in fetch(conn, "SELECT bead_id, label FROM bead_labels"):
        labels[r["bead_id"]].append(r["label"])
    edges = collections.defaultdict(list)
    for r in fetch(
        conn, "SELECT from_id, type, to_id, created_at, created_by, metadata FROM bead_edges"
    ):
        edges[r["from_id"]].append(
            {
                "issue_id": r["from_id"],
                "depends_on_id": r["to_id"],
                "type": r["type"],
                "created_at": norm(r["created_at"]),
                "created_by": r["created_by"],
                "metadata": r["metadata"],
            }
        )
    comments = collections.defaultdict(list)
    for r in fetch(conn, "SELECT _id, bead_id, author, text, created_at FROM bead_comments"):
        comments[r["bead_id"]].append(
            {
                "id": str(r["_id"]),
                "issue_id": r["bead_id"],
                "author": r["author"],
                "text": r["text"],
                "created_at": norm(r["created_at"]),
            }
        )
    order = {b["id"]: b for b in beads}
    for bid, d in docs.items():
        b = order[bid]
        if labels.get(bid):
            d["labels"] = sorted(labels[bid])
        if edges.get(bid):
            # split rows carry no list order: restore the export's order to compare as a multiset
            pos = {
                (x["depends_on_id"], x["type"]): i for i, x in enumerate(b.get("dependencies", []))
            }
            d["dependencies"] = sorted(
                edges[bid], key=lambda x: pos.get((x["depends_on_id"], x["type"]), 1e9)
            )
        if comments.get(bid):
            pos = {x["id"]: i for i, x in enumerate(b.get("comments", []))}
            d["comments"] = sorted(comments[bid], key=lambda x: pos.get(x["id"], 1e9))
    report("B/transit (reassembled)", beads, docs)

    # bd events table: null fields come back absent, so compare against the export minus nulls
    got = {
        str(r["_id"]): {k: norm(v) for k, v in r.items() if k != "_id"}
        for r in fetch(conn, "SELECT * FROM bd_events")
    }
    bad = collections.Counter()
    nulls_dropped = 0
    for e in load_events():
        exp = {k: v for k, v in e.items() if k != "id" and v is not None}
        nulls_dropped += sum(1 for v in e.values() if v is None)
        g = got.get(e["id"])
        for c in ["row:missing"] if g is None else diff(exp, g):
            bad[c] += 1
    print(
        f"\n== bd_events: rows read {len(got)}, divergences {dict(bad) or 'none'}, "
        f"null fields read back as absent: {nulls_dropped}"
    )


if __name__ == "__main__":
    main()
