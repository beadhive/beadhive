"""Load the bh export into both candidate layouts and the bd events table; print timings.

Layout (a) nested:  beads_a                 one doc per bead, labels (set) / deps / comments nested
Layout (b) split:   beads_b                 bead doc minus labels/deps/comments
                    bead_labels             one row per (bead, label), own valid period
                    bead_edges              one row per (from, type, to), own valid period
                    bead_comments           one row per comment, valid from comment.created_at
Events:             bd_events               the bd `events` table as-is (timeline source)

Label/edge valid periods in (b) are derived from the bd events table where it has evidence
(label_added/label_removed, dependency_added/dependency_removed), else from the snapshot
(dep.created_at, or bead.created_at for labels with no add event: events start 2026-08-07).
"""

from __future__ import annotations

import collections
import json
import re
import time

from common import (
    bead_doc_nested,
    bead_doc_split,
    connect,
    edge_id,
    esc,
    insert_records,
    label_id,
    load_events,
    load_export,
    to_transit,
    tts,
)

DEP_ADDED = re.compile(r"Added dependency: (\S+) (\S+) (\S+)$")
DEP_REMOVED = re.compile(r"Removed dependency on (\S+)$")


def label_of(e: dict) -> str | None:
    c = e.get("comment") or ""
    for prefix in ("Added label: ", "Removed label: "):
        if c.startswith(prefix):
            return c[len(prefix) :]
    v = e["new_value"] if e["event_type"] == "label_added" else e["old_value"]
    return v or None


def intervals(events: list[tuple[str, str]], start: str, present: bool):
    """events: sorted [(ts, 'add'|'remove')]. Yields (valid_from, valid_to|None); counts gaps."""
    out, open_at, last_close, anomalies = [], None, None, 0
    for ts, kind in events:
        if kind == "add":
            if open_at is None:
                open_at = ts
        elif open_at is None and last_close is not None:
            anomalies += 1  # a second remove with no add in between
        else:
            out.append((open_at or start, ts))
            open_at, last_close = None, ts
    if present:
        if open_at is None and last_close is not None:
            anomalies += 1  # removed, then present again with no add event: re-add time unknown
        out.append((open_at or last_close or start, None))
    elif open_at is not None:
        anomalies += 1  # events say "added, never removed" but the snapshot lacks it
    return [iv for iv in out if iv[1] is None or iv[0] < iv[1]], anomalies


def main() -> None:
    beads = load_export()
    events = load_events()
    by_id = {b["id"]: b for b in beads}
    conn = connect(autocommit=False)
    stats: dict[str, object] = {}

    t0 = time.monotonic()
    insert_records(conn, "beads_a", [bead_doc_nested(b) for b in beads])
    stats["beads_a_s"] = round(time.monotonic() - t0, 2)

    t0 = time.monotonic()
    insert_records(conn, "beads_b", [bead_doc_split(b) for b in beads])
    stats["beads_b_s"] = round(time.monotonic() - t0, 2)

    # ---- labels with per-element valid periods
    lab_ev: dict[tuple[str, str], list] = collections.defaultdict(list)
    for e in events:
        if e["event_type"] in ("label_added", "label_removed") and e["issue_id"] in by_id:
            lbl = label_of(e)
            if lbl:
                kind = "add" if e["event_type"] == "label_added" else "remove"
                lab_ev[(e["issue_id"], lbl)].append((e["created_at"], kind))
    current = {(b["id"], lbl) for b in beads for lbl in b.get("labels", [])}
    label_rows, anomalies = [], 0
    for key in set(lab_ev) | current:
        bid, lbl = key
        ivs, a = intervals(sorted(lab_ev.get(key, [])), by_id[bid]["created_at"], key in current)
        anomalies += a
        for vf, vt in ivs:
            row = {
                "_id": esc(label_id(bid, lbl)),
                "bead_id": bid,
                "label": esc(lbl),
                "_valid_from": tts(vf),
            }
            if vt:
                row["_valid_to"] = tts(vt)
            label_rows.append(row)
    stats["label_rows"] = len(label_rows)
    stats["label_event_snapshot_anomalies"] = anomalies

    # ---- edges with per-element valid periods
    edge_ev: dict[tuple[str, str, str], list] = collections.defaultdict(list)
    last_type: dict[tuple[str, str], str] = {}
    unresolved_removals = 0
    for e in sorted(
        (e for e in events if e["event_type"] in ("dependency_added", "dependency_removed")),
        key=lambda e: e["created_at"],
    ):
        if e["event_type"] == "dependency_added":
            m = DEP_ADDED.match(e["new_value"] or "")
            if m:
                src, typ, dst = m.groups()
                last_type[(src, dst)] = typ
                edge_ev[(src, typ, dst)].append((e["created_at"], "add"))
        else:
            m = DEP_REMOVED.match(e["new_value"] or "")
            if m:
                src, dst = e["issue_id"], m.group(1)
                typ = last_type.get((src, dst))
                if typ is None:
                    unresolved_removals += 1
                    continue
                edge_ev[(src, typ, dst)].append((e["created_at"], "remove"))
    cur_edges = {}
    for b in beads:
        for d in b.get("dependencies", []):
            cur_edges[(d["issue_id"], d["type"], d["depends_on_id"])] = d
    edge_rows, e_anom = [], 0
    for key in set(edge_ev) | set(cur_edges):
        src, typ, dst = key
        if src not in by_id:
            continue
        d = cur_edges.get(key)
        start = d["created_at"] if d else by_id[src]["created_at"]
        ivs, a = intervals(sorted(edge_ev.get(key, [])), start, d is not None)
        e_anom += a
        for vf, vt in ivs:
            row = {
                "_id": esc(edge_id(src, typ, dst)),
                "from_id": src,
                "type": typ,
                "to_id": esc(dst),
                "_valid_from": tts(vf),
            }
            if d is not None:
                row["created_by"] = esc(d["created_by"])
                row["created_at"] = tts(d["created_at"])
                row["metadata"] = esc(d["metadata"])
            if vt:
                row["_valid_to"] = tts(vt)
            edge_rows.append(row)
    stats["edge_rows"] = len(edge_rows)
    stats["edge_unresolved_removals"] = unresolved_removals
    stats["edge_event_snapshot_anomalies"] = e_anom

    comment_rows = [
        {
            "_id": c["id"],
            "bead_id": c["issue_id"],
            "author": esc(c["author"]),
            "text": esc(c["text"]),
            "created_at": tts(c["created_at"]),
            "_valid_from": tts(c["created_at"]),
        }
        for b in beads
        for c in b.get("comments", [])
    ]

    t0 = time.monotonic()
    insert_records(conn, "bead_labels", label_rows)
    insert_records(conn, "bead_edges", edge_rows)
    insert_records(conn, "bead_comments", comment_rows)
    stats["split_rows_s"] = round(time.monotonic() - t0, 2)

    ev_rows = [
        {
            "_id": "~u" + e["id"],
            **{
                k: (tts(v) if k == "created_at" else to_transit(v))
                for k, v in e.items()
                if k != "id"
            },
        }
        for e in events
    ]
    t0 = time.monotonic()
    insert_records(conn, "bd_events", ev_rows, batch=1000)
    stats["bd_events_s"] = round(time.monotonic() - t0, 2)
    stats["bd_events_rows"] = len(ev_rows)
    print(json.dumps(stats, indent=2))


if __name__ == "__main__":
    main()
