"""ERASE one bead (its bead / label / outgoing-edge / comment rows) and prove it is gone from
every valid-time and system-time read, while every other bead is untouched. Run after verify.py.
"""

# ruff: noqa: E501  (throwaway spike script: long SQL literals kept on one line)

from __future__ import annotations

import json
import subprocess
import sys

from history import SCALAR_CMP, TEXT
from verify import compare, read_state
from xt import SCRATCH, WORK, connect, parse_ts, q

BEAD = sys.argv[1] if len(sys.argv) > 1 else "bh-q0lol.5"
TABLES = {
    "beads": "_id",
    "bead_labels": "bead_id",
    "bead_edges": "from_id",
    "bead_comments": "bead_id",
}


def clauses(cuts):
    out = [("default (now)", "", [])]
    for c in cuts:
        t = parse_ts(c)
        out += [
            (f"FOR VALID_TIME AS OF {c}", "FOR VALID_TIME AS OF %s", [t]),
            (f"FOR SYSTEM_TIME AS OF {c}", "FOR SYSTEM_TIME AS OF %s", [t]),
            (
                f"FOR SYSTEM_TIME AS OF {c} FOR VALID_TIME AS OF {c}",
                "FOR SYSTEM_TIME AS OF %s FOR VALID_TIME AS OF %s",
                [t, t],
            ),
        ]
    out += [
        ("FOR ALL VALID_TIME", "FOR ALL VALID_TIME", []),
        ("FOR ALL SYSTEM_TIME", "FOR ALL SYSTEM_TIME", []),
        ("FOR ALL SYSTEM_TIME FOR ALL VALID_TIME", "FOR ALL SYSTEM_TIME FOR ALL VALID_TIME", []),
        (
            "FOR SYSTEM_TIME BETWEEN 2026-06-01 AND 2026-12-01 FOR ALL VALID_TIME",
            "FOR SYSTEM_TIME BETWEEN TIMESTAMP '2026-06-01T00:00:00Z' AND "
            "TIMESTAMP '2026-12-01T00:00:00Z' FOR ALL VALID_TIME",
            [],
        ),
    ]
    return out


def bead_counts(conn, cuts):
    res = {}
    for name, clause, params in clauses(cuts):
        res[name] = {
            t: conn.execute(
                f"SELECT COUNT(*) FROM {t} {clause} WHERE {col} = %s", [*params, BEAD]
            ).fetchone()[0]
            for t, col in TABLES.items()
        }
    return res


def others(conn):
    return {
        t: conn.execute(
            f"SELECT COUNT(*) FROM {t} FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE {col} <> %s",
            [BEAD],
        ).fetchone()[0]
        for t, col in TABLES.items()
    }


def grep_data_dir(needle: str) -> list[str]:
    r = subprocess.run(
        ["grep", "-rlaF", needle, str(SCRATCH / "xtdb")], capture_output=True, text=True
    )
    return sorted(p.replace(str(SCRATCH / "xtdb") + "/", "") for p in r.stdout.split())


def main() -> None:
    with open(WORK / "expected.json") as fh:
        X = json.load(fh)
    cuts = X["cuts"]
    conn = connect(autocommit=True)
    title = conn.execute("SELECT title FROM beads WHERE _id = %s", [BEAD]).fetchone()[0]
    needle = title[:40]
    print(f"# erase.py — target {BEAD} ({title!r})")
    before = bead_counts(conn, cuts)
    print("\n## Before ERASE: rows of the target under each read (beads/labels/edges/comments)")
    for k, v in before.items():
        print(f"   {k:<75} {v}")
    inbound = conn.execute(
        "SELECT COUNT(*) FROM bead_edges FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE to_id = %s",
        [BEAD],
    ).fetchone()[0]
    o_before = others(conn)
    print(f"   other beads' bitemporal rows: {o_before}; inbound edges from other beads: {inbound}")
    print(f"   data-dir files containing the title prefix {needle!r}: {grep_data_dir(needle)}")

    print("\n## ERASE (one wall-clock transaction)")
    conn.execute("BEGIN READ WRITE")
    for t, col in TABLES.items():
        conn.execute(f"ERASE FROM {t} WHERE {col} = %s", [BEAD])
    conn.execute("COMMIT")
    q(
        conn,
        "xt.txs, last",
        "SELECT _id, committed, system_time, error FROM xt.txs ORDER BY _id DESC LIMIT 1",
    )

    after = bead_counts(conn, cuts)
    print("\n## After ERASE: rows of the target under each read (beads/labels/edges/comments)")
    for k, v in after.items():
        print(f"   {k:<75} {v}")
    print("   all zero:", all(n == 0 for v in after.values() for n in v.values()))
    o_after = others(conn)
    print(f"   other beads' bitemporal rows: {o_after}  unchanged: {o_after == o_before}")
    inbound_after = conn.execute(
        "SELECT COUNT(*) FROM bead_edges FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE to_id = %s",
        [BEAD],
    ).fetchone()[0]
    print(f"   inbound edges from other beads (not erased, now dangling): {inbound_after}")
    q(
        conn,
        "title search across all time",
        "SELECT COUNT(*) FROM beads FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE title = %s",
        [title],
    )

    fields = [*SCALAR_CMP, *TEXT, "labels", "edges"]
    print("\n## Every other bead still reproduces the replay")
    for cut in cuts:
        got, _, _ = read_state(conn, "FOR VALID_TIME AS OF %s", [parse_ts(cut)])
        exp = {b: v for b, v in X["expected"][cut].items() if b != BEAD}
        m, d, f, ex = compare(exp, got, fields)
        print(f"   cut {cut}: {m} match, {d} differ {dict(f) if f else ''}")
    print(
        f"\n   data-dir files containing the title prefix right after ERASE: {grep_data_dir(needle)}"
    )


if __name__ == "__main__":
    main()
