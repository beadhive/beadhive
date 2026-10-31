"""Per-element valid periods (layout b vs a), INSERT/PATCH/UPDATE temporal semantics,
and dangling edge targets without foreign keys.

Requires load.py to have run (beads_a, beads_b, bead_labels, bead_edges).
"""

from __future__ import annotations

import datetime as dt
import uuid

from common import Transit, connect, set_output

RUN = uuid.uuid4().hex[:6]


def q(conn, title, sql, params=None, limit=12):
    print(f"\n-- {title}\n   {' '.join(sql.split())}")
    try:
        cur = conn.execute(sql, params)
        rows = cur.fetchall()
        print("   cols:", [d.name for d in cur.description])
        for r in rows[:limit]:
            print(
                "   ",
                tuple(
                    v.strftime("%Y-%m-%dT%H:%M:%SZ") if isinstance(v, dt.datetime) else v for v in r
                ),
            )
        if len(rows) > limit:
            print(f"    ... {len(rows)} rows")
        return rows
    except Exception as e:  # noqa: BLE001
        print("   ERR", str(e).splitlines()[0][:200])
        return None


def ins(conn, table, doc):
    conn.execute(f"INSERT INTO {table} RECORDS %s", [Transit(doc)])


def main():
    conn = connect()
    set_output(conn, "json")

    # ------------------------------------------------------------ layout (b), real data
    rows = q(
        conn,
        "(b) beads that gained and lost a label (closed label periods)",
        """SELECT bead_id, COUNT(*) AS closed FROM bead_labels FOR ALL VALID_TIME
                WHERE _valid_to IS NOT NULL GROUP BY bead_id ORDER BY closed DESC, bead_id""",
        limit=3,
    )
    bead = rows[0][0] if rows else "bh-24ay9"
    q(
        conn,
        f"(b) every label period of {bead}",
        """SELECT label, _valid_from, _valid_to FROM bead_labels FOR ALL VALID_TIME
         WHERE bead_id = %s ORDER BY label, _valid_from""",
        [bead],
        limit=40,
    )
    mid = conn.execute(
        """SELECT _valid_to FROM bead_labels FOR ALL VALID_TIME
           WHERE bead_id = %s AND _valid_to IS NOT NULL ORDER BY _valid_to LIMIT 1""",
        [bead],
    ).fetchone()[0]
    before = mid - dt.timedelta(seconds=1)
    q(
        conn,
        f"(b) labels of {bead} as of {before.isoformat()} (param) vs now",
        """SELECT label FROM bead_labels FOR VALID_TIME AS OF %s
         WHERE bead_id = %s ORDER BY label""",
        [before, bead],
        limit=40,
    )
    q(
        conn,
        f"(b) labels of {bead} now",
        "SELECT label FROM bead_labels WHERE bead_id = %s ORDER BY label",
        [bead],
        limit=40,
    )
    q(
        conn,
        "(b) corpus: how long each bead held review:pending (top 5)",
        """SELECT bead_id, _valid_from, _valid_to, (_valid_to - _valid_from) AS held
         FROM bead_labels FOR ALL VALID_TIME
         WHERE label = 'review:pending' AND _valid_to IS NOT NULL
         ORDER BY held DESC LIMIT 5""",
    )
    q(
        conn,
        "(b) corpus: beads carrying review:pending at 2026-10-01T00:00Z",
        """SELECT COUNT(*) FROM bead_labels FOR VALID_TIME AS OF TIMESTAMP '2026-10-01T00:00:00Z'
         WHERE label = 'review:pending'""",
    )
    q(
        conn,
        "(b) removed edges with their valid periods",
        """SELECT from_id, type, to_id, _valid_from, _valid_to FROM bead_edges FOR ALL VALID_TIME
         WHERE _valid_to IS NOT NULL ORDER BY _valid_to LIMIT 5""",
    )

    # ------------------------------------------------------------ layout (a), versioned doc
    t = f"temporal_a_{RUN}"
    base = {"_id": "bh-x", "title": "T", "description": "d" * 2000}
    ins(conn, t, {**base, "labels": {"~#set": ["a"]}, "_valid_from": "~t2026-08-01T00:00:00Z"})
    ins(
        conn,
        t,
        {
            **base,
            "labels": {"~#set": ["a", "review:pending"]},
            "_valid_from": "~t2026-08-02T00:00:00Z",
        },
    )
    ins(
        conn,
        t,
        {
            **base,
            "title": "T2",
            "labels": {"~#set": ["a", "review:pending"]},
            "_valid_from": "~t2026-08-03T00:00:00Z",
        },
    )
    ins(
        conn,
        t,
        {
            **base,
            "title": "T2",
            "labels": {"~#set": ["a"]},
            "_valid_from": "~t2026-08-04T00:00:00Z",
        },
    )
    q(
        conn,
        "(a) whole-document versions: every label change AND the title change rewrite the doc",
        f"SELECT title, labels, _valid_from, _valid_to FROM {t} FOR ALL VALID_TIME"
        " ORDER BY _valid_from",
    )
    q(
        conn,
        "(a) per-label period needs UNNEST over versions + merge of adjacent fragments",
        f"""SELECT l.label, MIN(v._valid_from) AS vf,
                 MAX(COALESCE(v._valid_to, TIMESTAMP '9999-12-31T00:00:00Z')) AS vt,
                 COUNT(*) AS fragments
          FROM {t} FOR ALL VALID_TIME AS v, UNNEST(v.labels) AS l(label)
          GROUP BY l.label ORDER BY l.label""",
    )
    print("   (MIN/MAX is only right when the label was held once; re-adds need gaps-and-islands)")

    # ---------------------------------------------- INSERT with later/earlier _valid_from
    t = f"temporal_ins_{RUN}"
    ins(conn, t, {"_id": "k", "v": 1, "_valid_from": "~t2026-08-01T00:00:00Z"})
    ins(conn, t, {"_id": "k", "v": 2, "_valid_from": "~t2026-08-05T00:00:00Z"})
    q(
        conn,
        "INSERT with a later _valid_from: does it close the earlier row?",
        f"SELECT v, _valid_from, _valid_to FROM {t} FOR ALL VALID_TIME ORDER BY _valid_from",
    )
    ins(conn, t, {"_id": "k", "v": 0, "_valid_from": "~t2026-07-01T00:00:00Z"})
    q(
        conn,
        "then INSERT with an EARLIER _valid_from and no _valid_to (out-of-order backfill)",
        f"SELECT v, _valid_from, _valid_to FROM {t} FOR ALL VALID_TIME ORDER BY _valid_from",
    )
    q(
        conn,
        "... system-time view of the same (what the clobbered rows look like)",
        f"""SELECT v, _valid_from, _valid_to, _system_from, _system_to
          FROM {t} FOR ALL VALID_TIME FOR ALL SYSTEM_TIME ORDER BY _system_from, _valid_from""",
        limit=20,
    )
    t2 = f"temporal_ins2_{RUN}"
    ins(conn, t2, {"_id": "k", "v": 1, "_valid_from": "~t2026-08-01T00:00:00Z"})
    ins(conn, t2, {"_id": "k", "v": 2, "_valid_from": "~t2026-08-05T00:00:00Z"})
    ins(
        conn,
        t2,
        {
            "_id": "k",
            "v": 0,
            "_valid_from": "~t2026-07-01T00:00:00Z",
            "_valid_to": "~t2026-08-01T00:00:00Z",
        },
    )
    q(
        conn,
        "out-of-order backfill WITH an explicit _valid_to (bounded)",
        f"SELECT v, _valid_from, _valid_to FROM {t2} FOR ALL VALID_TIME ORDER BY _valid_from",
    )

    # ------------------------------------------------------------ PATCH cannot clear a field
    t = f"temporal_patch_{RUN}"
    ins(conn, t, {"_id": "p", "assignee": "dev/x", "status": "open"})
    for sql, params in [
        (
            f"PATCH INTO {t} RECORDS %s",
            [Transit({"_id": "p", "assignee": None, "status": "closed"})],
        ),
    ]:
        try:
            conn.execute(sql, params)
            print("\n-- PATCH with assignee: null -> OK")
        except Exception as e:  # noqa: BLE001
            print("\n-- PATCH ERR", str(e).splitlines()[0][:200])
    q(
        conn,
        "after PATCH {assignee: null, status: closed}",
        f"SELECT _id, assignee, status FROM {t}",
    )
    conn.execute(f"UPDATE {t} SET assignee = NULL WHERE _id = 'p'")
    q(conn, "after UPDATE SET assignee = NULL", f"SELECT _id, assignee, status FROM {t}")
    q(
        conn,
        "history",
        f"SELECT assignee, status, _valid_from, _valid_to FROM {t} FOR ALL VALID_TIME "
        "ORDER BY _valid_from",
    )
    conn.execute(
        f"UPDATE {t} FOR PORTION OF VALID_TIME FROM TIMESTAMP '2026-01-01T00:00:00Z' TO "
        f"TIMESTAMP '2026-02-01T00:00:00Z' SET status = 'blocked' WHERE _id = 'p'"
    )
    q(
        conn,
        "UPDATE ... FOR PORTION OF VALID_TIME on a row with no history there",
        f"SELECT status, _valid_from, _valid_to FROM {t} FOR ALL VALID_TIME ORDER BY _valid_from",
    )

    # ------------------------------------------------------------ dangling edges, no FKs
    q(
        conn,
        "(b) dangling edge targets: anti-join, current valid time",
        """SELECT e.from_id, e.type, e.to_id FROM bead_edges AS e
         WHERE NOT EXISTS (SELECT 1 FROM beads_b AS b WHERE b._id = e.to_id)
         ORDER BY e.from_id""",
        limit=20,
    )
    q(
        conn,
        "(a) dangling targets from nested deps: UNNEST then anti-join",
        """SELECT b._id, d.dep FROM beads_a AS b, UNNEST(b.dependencies) AS d(dep)
         WHERE NOT EXISTS (SELECT 1 FROM beads_a AS t WHERE t._id = (d.dep).depends_on_id)
         ORDER BY b._id""",
        limit=3,
    )
    q(
        conn,
        "(b) edges whose source is missing",
        """SELECT COUNT(*) FROM bead_edges AS e
         WHERE NOT EXISTS (SELECT 1 FROM beads_b AS b WHERE b._id = e.from_id)""",
    )
    q(
        conn,
        "(b) label rows whose bead is missing",
        """SELECT COUNT(*) FROM bead_labels AS l
         WHERE NOT EXISTS (SELECT 1 FROM beads_b AS b WHERE b._id = l.bead_id)""",
    )


if __name__ == "__main__":
    main()
