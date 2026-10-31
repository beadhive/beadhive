# bh-w56ts `load.py` output

Captured stdout of `../load.py` against XTDB 2.2.0 on 2026-10-30 (setup: the spike doc).

```text
# load.py stdout, first and only load into an empty XTDB 2.2.0 node (2026-10-30)
# script wall clock ~50 s; container peak ~907 MiB / 3 GiB (docker stats sampled every 2 s)
# data dir after load: 112 MB (inputs: bh-export.jsonl 16.3 MB + bh-events.json 32.9 MB)
{
  "beads_a_s": 7.96,
  "beads_b_s": 5.04,
  "label_rows": 21760,
  "label_event_snapshot_anomalies": 0,
  "edge_rows": 10321,
  "edge_unresolved_removals": 32,
  "edge_event_snapshot_anomalies": 8,
  "split_rows_s": 12.68,
  "bd_events_s": 23.31,
  "bd_events_rows": 36716
}
```
