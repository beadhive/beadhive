"""Full bitemporal load of the bh history into a FRESH XTDB node (layout (b)).

One XTDB transaction per distinct recorded_at, in strictly increasing order:
    BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '<recorded_at>')
      INSERT INTO <table> RECORDS $1, $2, ...         (consecutive inserts into one table)
      DELETE FROM <table> FOR PORTION OF VALID_TIME FROM $t TO NULL WHERE _id = $id
    COMMIT                                           (SYNC: waits for indexing, surfaces aborts)

Stops at the first failed transaction: an aborted backfill tx takes wall-clock system time, which
ends backfill for the whole database (monotone.py S2/S3), so continuing would be pointless.

    load.py [--metrics]   --metrics: write load-metrics.json from sampler.sh samples
"""

from __future__ import annotations

import collections
import datetime as dt
import json
import pickle
import subprocess
import sys
import time

from xt import SCRATCH, WORK, Transit, connect, parse_ts

CONTAINER = "xtdb-spike-bh-3jp2g"


def docker_mem_mib() -> float:
    out = subprocess.run(
        ["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", CONTAINER],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()[0]
    for unit, f in (("GiB", 1024), ("MiB", 1), ("KiB", 1 / 1024)):
        if out.endswith(unit):
            return round(float(out[: -len(unit)]) * f, 1)
    raise ValueError(out)


def du_bytes() -> int:
    out = subprocess.run(["du", "-sb", str(SCRATCH / "xtdb")], capture_output=True, text=True)
    return int(out.stdout.split()[0])


def run_tx(conn, ts: str, ops: list) -> None:
    conn.execute(f"BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '{ts}')")
    i = 0
    while i < len(ops):
        op = ops[i]
        if op[0] == "ins":
            j = i
            while j < len(ops) and ops[j][0] == "ins" and ops[j][1] == op[1]:
                j += 1
            docs = [o[2] for o in ops[i:j]]
            conn.execute(
                f"INSERT INTO {op[1]} RECORDS " + ", ".join(["%s"] * len(docs)),
                [Transit(d) for d in docs],
            )
            i = j
        else:
            _, table, _id, t = op
            conn.execute(
                f"DELETE FROM {table} FOR PORTION OF VALID_TIME FROM %s TO NULL WHERE _id = %s",
                [parse_ts(t), _id],
            )
            i += 1
    conn.execute("COMMIT")


def main() -> None:
    metrics = "--metrics" in sys.argv
    with open(WORK / "ops.pkl", "rb") as fh:
        stream = pickle.load(fh)
    rows = collections.Counter()
    for _, ops in stream:
        for op in ops:
            rows[f"{op[0]}:{op[1]}"] += 1
    conn = connect(autocommit=True)
    idle = []
    if metrics:
        for _ in range(5):
            idle.append(docker_mem_mib())
            time.sleep(2)
    t_start = time.time()
    done = 0
    failed = None
    for ts, ops in stream:
        try:
            run_tx(conn, ts, ops)
        except Exception as e:  # noqa: BLE001
            failed = {"recorded_at": ts, "error": str(e).splitlines()[0][:300]}
            break
        done += 1
        if done % 2000 == 0:
            el = time.time() - t_start
            print(f"  {done}/{len(stream)} txs, {el:.0f} s, {done / el:.0f} tx/s", flush=True)
    t_end = time.time()
    load_s = round(t_end - t_start, 1)
    txs = conn.execute(
        "SELECT COUNT(*), SUM(CASE WHEN committed THEN 0 ELSE 1 END), "
        "MIN(system_time), MAX(system_time) FROM xt.txs"
    ).fetchone()
    result = {
        "xtdb_version": conn.execute("SELECT version()").fetchone()[0],
        "load_seconds": load_s,
        "tx_submitted": done,
        "tx_planned": len(stream),
        "failed": failed,
        "xt_txs": {
            "count": txs[0],
            "aborted": txs[1],
            "min_system_time": str(txs[2]),
            "max_system_time": str(txs[3]),
        },
        "ops_by_kind_table": dict(sorted(rows.items())),
        "tx_per_second": round(done / load_s, 1),
    }
    print(json.dumps(result, indent=2, default=str))
    if not metrics:
        return
    time.sleep(10)  # let the last samples land
    samples = []
    with open(SCRATCH / "samples.jsonl") as fh:
        for line in fh:
            s = json.loads(line)
            if t_start - 30 <= s["t"] <= time.time():
                samples.append(
                    {
                        "t": round(s["t"] - t_start, 1),
                        "mem_mib": s["mem_mib"],
                        "data_dir_bytes": s["data_dir_bytes"],
                    }
                )
    in_load = [s for s in samples if 0 <= s["t"] <= load_s + 4]
    out = {
        "xtdb_version": "2.2.0",
        "image": "ghcr.io/xtdb/xtdb:2.2.0",
        "container_mem_cap": "3g (--memory 3g --memory-swap 3g)",
        "load_started_utc": dt.datetime.fromtimestamp(t_start, dt.UTC).isoformat(),
        "load_ended_utc": dt.datetime.fromtimestamp(t_end, dt.UTC).isoformat(),
        "load_seconds": load_s,
        "rows_by_table": {k.split(":")[1]: v for k, v in rows.items() if k.startswith("ins:")},
        "deletes_by_table": {k.split(":")[1]: v for k, v in rows.items() if k.startswith("del:")},
        "source": {"beads": 9088, "events": 36716, "event_rows_loaded_from_export_beads": 36702},
        "tx_count": done,
        "tx_aborted": txs[1],
        "commit_mode": "SYNC (default), one tx per distinct recorded_at",
        "data_dir_bytes": du_bytes(),
        "idle_mem_mib_before_load": max(idle),
        "peak_mem_mib": max(s["mem_mib"] for s in in_load),
        "end_mem_mib": samples[-1]["mem_mib"],
        "sample_interval_s": "~2-3 (docker stats --no-stream + du -sb)",
        "samples": samples,
        "notes": "t is seconds relative to load start; post-load idle readings are appended "
        "later under post_load.",
    }
    with open(SCRATCH / "load-metrics.json", "w") as fh:
        json.dump(out, fh, indent=1)
    print(json.dumps({k: v for k, v in out.items() if k != "samples"}, indent=2))


if __name__ == "__main__":
    main()
