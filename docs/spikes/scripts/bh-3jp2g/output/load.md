# bh-3jp2g `load.py --metrics` output

Captured stdout of the instrumented full load into a fresh XTDB 2.2.0 node on 2026-10-30.
A sibling spike (`bh-cntr0`) was loading its own container on the same host at the same time,
and the host load average was about 6. An earlier, uninstrumented dry-run load of an op stream
that differed only in some `closed_at` values took 363.1 s (78.2 tx/s), also with 0 aborts.

```text
  2000/28402 txs, 35 s, 58 tx/s
  4000/28402 txs, 63 s, 64 tx/s
  6000/28402 txs, 89 s, 67 tx/s
  8000/28402 txs, 114 s, 70 tx/s
  10000/28402 txs, 139 s, 72 tx/s
  12000/28402 txs, 164 s, 73 tx/s
  14000/28402 txs, 189 s, 74 tx/s
  16000/28402 txs, 213 s, 75 tx/s
  18000/28402 txs, 237 s, 76 tx/s
  20000/28402 txs, 260 s, 77 tx/s
  22000/28402 txs, 284 s, 78 tx/s
  24000/28402 txs, 307 s, 78 tx/s
  26000/28402 txs, 331 s, 79 tx/s
  28000/28402 txs, 355 s, 79 tx/s
{
  "xtdb_version": "PostgreSQL 16 (XTDB 2.2.0)",
  "load_seconds": 359.6,
  "tx_submitted": 28402,
  "tx_planned": 28402,
  "failed": null,
  "xt_txs": {
    "count": 28402,
    "aborted": 0,
    "min_system_time": "2026-06-29 03:43:46+00:00",
    "max_system_time": "2026-10-30 21:32:38+00:00"
  },
  "ops_by_kind_table": {
    "del:bead_edges": 68,
    "del:bead_labels": 2332,
    "ins:bead_comments": 691,
    "ins:bead_edges": 10341,
    "ins:bead_labels": 21760,
    "ins:beads": 24195
  },
  "tx_per_second": 79.0
}
{
  "xtdb_version": "2.2.0",
  "image": "ghcr.io/xtdb/xtdb:2.2.0",
  "container_mem_cap": "3g (--memory 3g --memory-swap 3g)",
  "load_started_utc": "2026-10-30T23:15:01.393255+00:00",
  "load_ended_utc": "2026-10-30T23:21:01.013638+00:00",
  "load_seconds": 359.6,
  "rows_by_table": {
    "beads": 24195,
    "bead_labels": 21760,
    "bead_edges": 10341,
    "bead_comments": 691
  },
  "deletes_by_table": {
    "bead_labels": 2332,
    "bead_edges": 68
  },
  "source": {
    "beads": 9088,
    "events": 36716,
    "event_rows_loaded_from_export_beads": 36702
  },
  "tx_count": 28402,
  "tx_aborted": 0,
  "commit_mode": "SYNC (default), one tx per distinct recorded_at",
  "data_dir_bytes": 369728267,
  "idle_mem_mib_before_load": 416.8,
  "peak_mem_mib": 552.4,
  "end_mem_mib": 552.2,
  "sample_interval_s": "~2-3 (docker stats --no-stream + du -sb)",
  "notes": "t is seconds relative to load start; post-load idle readings are appended later under post_load."
}
exit 0
```
