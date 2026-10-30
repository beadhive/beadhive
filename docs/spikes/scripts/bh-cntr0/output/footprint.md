# bh-cntr0 `loadrun.py / harness.py` output

Captured results of `loadrun.py` (cgroup `memory.current` sampled twice a second; `memory.peak`
for the peak) for the bh-w56ts shape load (`../bh-w56ts/load.py`, 87.6k rows) at several container
caps, XTDB 2.2.0, one container at a time, data on `/data` (ext4). `peak_mib` and `end_mib`
include page cache; the `*_docker_stats_mib` fields (only present from the `2g-final` run on)
subtract inactive file cache, as `docker stats` does. 2026-10-30.

## 2g

```text
{
 "label": "2g",
 "rc": 0,
 "idle_mib": 385,
 "load_wall_s": 47.3,
 "peak_mib": 1022,
 "end_mib": 1008,
 "memory_events": {
  "low": "0",
  "high": "0",
  "max": "0",
  "oom": "0",
  "oom_kill": "0",
  "oom_group_kill": "0"
 },
 "data_bytes": 116948451,
 "log_bytes": 116948451,
 "buffers_bytes": 0
}
load.py stdout:
{
  "beads_a_s": 8.47,
  "beads_b_s": 4.54,
  "label_rows": 21760,
  "label_event_snapshot_anomalies": 0,
  "edge_rows": 10321,
  "edge_unresolved_removals": 32,
  "edge_event_snapshot_anomalies": 8,
  "split_rows_s": 11.31,
  "bd_events_s": 21.02,
  "bd_events_rows": 36716
}
memory.current MiB every 5 s [t_s, MiB]:
[[0.0, 385], [5.0, 533], [10.0, 720], [15.0, 772], [20.0, 775], [25.0, 784], [30.0, 883], [35.0, 907], [40.0, 945], [45.1, 1005], [50.1, 1010], [55.1, 1010], [60.1, 1010], [65.1, 1010], [70.1, 1010], [75.1, 1010]]
```

## 2g-final

```text
{
 "label": "2g-final",
 "container_state_after_load": "running exit=0 oom_killed=false",
 "rc": 0,
 "idle_mib": 396,
 "load_wall_s": 47.1,
 "idle_docker_stats_mib": 396,
 "peak_docker_stats_mib": 883,
 "end_docker_stats_mib": 873,
 "peak_mib": 1003,
 "end_mib": 985,
 "memory_events": {
  "low": "0",
  "high": "0",
  "max": "0",
  "oom": "0",
  "oom_kill": "0",
  "oom_group_kill": "0"
 },
 "data_bytes": 116932371,
 "log_bytes": 116932371,
 "buffers_bytes": 0
}
load.py stdout:
{
  "beads_a_s": 8.33,
  "beads_b_s": 4.57,
  "label_rows": 21760,
  "label_event_snapshot_anomalies": 0,
  "edge_rows": 10321,
  "edge_unresolved_removals": 32,
  "edge_event_snapshot_anomalies": 8,
  "split_rows_s": 11.23,
  "bd_events_s": 21.1,
  "bd_events_rows": 36716
}
memory.current MiB every 5 s [t_s, MiB]:
[[0.0, 396], [5.0, 527], [10.0, 674], [15.0, 711], [20.0, 720], [25.1, 729], [30.1, 825], [35.1, 900], [40.1, 930], [45.1, 987], [50.1, 985], [55.1, 985], [60.1, 985], [65.1, 985]]
```

## 1536m

```text
{
 "label": "1536m",
 "container_state_after_load": "running exit=0 oom_killed=false",
 "rc": 0,
 "idle_mib": 330,
 "load_wall_s": 58.0,
 "peak_mib": 903,
 "end_mib": 889,
 "memory_events": {
  "low": "0",
  "high": "0",
  "max": "0",
  "oom": "0",
  "oom_kill": "0",
  "oom_group_kill": "0"
 },
 "data_bytes": 116938448,
 "log_bytes": 116938448,
 "buffers_bytes": 0
}
load.py stdout:
{
  "beads_a_s": 10.69,
  "beads_b_s": 5.45,
  "label_rows": 21760,
  "label_event_snapshot_anomalies": 0,
  "edge_rows": 10321,
  "edge_unresolved_removals": 32,
  "edge_event_snapshot_anomalies": 8,
  "split_rows_s": 12.55,
  "bd_events_s": 27.57,
  "bd_events_rows": 36716
}
memory.current MiB every 5 s [t_s, MiB]:
[[0.0, 330], [5.0, 477], [10.0, 701], [15.0, 739], [20.0, 758], [25.0, 765], [30.0, 782], [35.0, 823], [40.0, 837], [45.1, 854], [50.1, 866], [55.1, 893], [60.1, 889], [65.1, 889], [70.1, 889], [75.1, 889]]
```

## 1g

```text
{
 "label": "1g",
 "container_state_after_load": "running exit=0 oom_killed=false",
 "rc": 0,
 "idle_mib": 321,
 "load_wall_s": 191.6,
 "peak_mib": 785,
 "end_mib": 770,
 "memory_events": {
  "low": "0",
  "high": "0",
  "max": "0",
  "oom": "0",
  "oom_kill": "0",
  "oom_group_kill": "0"
 },
 "data_bytes": 116940756,
 "log_bytes": 116940756,
 "buffers_bytes": 0
}
load.py stdout:
{
  "beads_a_s": 11.24,
  "beads_b_s": 7.14,
  "label_rows": 21760,
  "label_event_snapshot_anomalies": 0,
  "edge_rows": 10321,
  "edge_unresolved_removals": 32,
  "edge_event_snapshot_anomalies": 8,
  "split_rows_s": 14.17,
  "bd_events_s": 157.54,
  "bd_events_rows": 36716
}
memory.current MiB every 5 s [t_s, MiB]:
[[0.0, 321], [5.0, 496], [10.0, 609], [15.0, 622], [20.0, 655], [25.0, 660], [30.0, 666], [35.0, 673], [40.0, 688], [45.1, 696], [50.1, 709], [55.1, 710], [60.1, 712], [65.1, 713], [70.1, 714], [75.1, 715], [80.1, 725], [85.1, 726], [90.1, 728], [95.1, 732], [100.1, 732], [105.1, 733], [110.1, 734], [115.1, 735], [120.1, 736], [125.1, 738], [130.2, 740], [135.2, 743], [140.2, 745], [145.2, 748], [150.2, 749], [155.2, 750], [160.2, 751], [165.2, 768], [170.2, 773], [175.2, 776], [180.2, 775], [185.2, 775], [190.2, 776], [195.2, 770], [200.2, 770], [205.2, 770], [210.2, 770]]
```

## 1g-heap40

```text
{
 "label": "1g-heap40",
 "container_state_after_load": "running exit=0 oom_killed=false",
 "rc": 0,
 "idle_mib": 320,
 "load_wall_s": 54.2,
 "peak_mib": 933,
 "end_mib": 919,
 "memory_events": {
  "low": "0",
  "high": "0",
  "max": "0",
  "oom": "0",
  "oom_kill": "0",
  "oom_group_kill": "0"
 },
 "data_bytes": 116933069,
 "log_bytes": 116933069,
 "buffers_bytes": 0
}
load.py stdout:
{
  "beads_a_s": 9.77,
  "beads_b_s": 5.35,
  "label_rows": 21760,
  "label_event_snapshot_anomalies": 0,
  "edge_rows": 10321,
  "edge_unresolved_removals": 32,
  "edge_event_snapshot_anomalies": 8,
  "split_rows_s": 11.58,
  "bd_events_s": 25.91,
  "bd_events_rows": 36716
}
memory.current MiB every 5 s [t_s, MiB]:
[[0.0, 320], [5.0, 499], [10.0, 741], [15.0, 791], [20.0, 798], [25.0, 804], [30.0, 830], [35.0, 858], [40.0, 877], [45.1, 889], [50.1, 899], [55.1, 919], [60.1, 919], [65.1, 919], [70.1, 919]]
```

## 768m-heap40

```text
{
 "label": "768m-heap40",
 "container_state_after_load": "running exit=0 oom_killed=false",
 "rc": 0,
 "idle_mib": 316,
 "load_wall_s": 99.2,
 "peak_mib": 768,
 "end_mib": 749,
 "memory_events": {
  "low": "0",
  "high": "0",
  "max": "292",
  "oom": "0",
  "oom_kill": "0",
  "oom_group_kill": "0"
 },
 "data_bytes": 116925689,
 "log_bytes": 116925689,
 "buffers_bytes": 0
}
load.py stdout:
{
  "beads_a_s": 10.94,
  "beads_b_s": 5.93,
  "label_rows": 21760,
  "label_event_snapshot_anomalies": 0,
  "edge_rows": 10321,
  "edge_unresolved_removals": 32,
  "edge_event_snapshot_anomalies": 8,
  "split_rows_s": 12.82,
  "bd_events_s": 67.97,
  "bd_events_rows": 36716
}
memory.current MiB every 5 s [t_s, MiB]:
[[0.0, 316], [5.0, 458], [10.0, 626], [15.0, 659], [20.0, 701], [25.0, 704], [30.0, 718], [35.0, 754], [40.0, 766], [45.1, 760], [50.1, 764], [55.1, 764], [60.1, 765], [65.1, 763], [70.1, 762], [75.1, 762], [80.1, 764], [85.1, 765], [90.1, 752], [95.1, 753], [100.1, 749], [105.1, 749], [110.1, 749], [115.1, 749]]
```

## 768m

```text
no result file: the run hit the 600 s `timeout` (rc=124) before the load finished; see capmatrix-logs.md
```

## 512m

```text
{
 "label": "512m",
 "container_state_after_load": "running exit=0 oom_killed=false",
 "rc": 1,
 "idle_mib": 317,
 "load_wall_s": 378.3,
 "peak_mib": 512,
 "end_mib": 503,
 "memory_events": {
  "low": "0",
  "high": "0",
  "max": "19",
  "oom": "0",
  "oom_kill": "0",
  "oom_group_kill": "0"
 },
 "data_bytes": 56238489,
 "log_bytes": 56238489,
 "buffers_bytes": 0
}
load.py stdout:

load.py stderr tail:
^^^^^^^^^^
  File "psycopg_binary/_psycopg/waiting.pyx", line 255, in psycopg_binary._psycopg.wait_c
  File "/home/bees/.cache/uv/archive-v0/6z-8kpbX6AIXuTqo/lib/python3.12/site-packages/psycopg/transaction.py", line 115, in _exit_gen
    yield from self._commit_gen()
  File "/home/bees/.cache/uv/archive-v0/6z-8kpbX6AIXuTqo/lib/python3.12/site-packages/psycopg/transaction.py", line 140, in _commit_gen
    yield from self._conn._exec_command(command)
  File "/home/bees/.cache/uv/archive-v0/6z-8kpbX6AIXuTqo/lib/python3.12/site-packages/psycopg/_connection_base.py", line 487, in _exec_command
    raise e.error_from_result(result, encoding=self.pgconn._encoding)
psycopg.errors.InternalError_: Java heap space
DETAIL:  {"category":"fault","code":"xtdb.error/unknown","message":"Java heap space"}
memory.current MiB every 5 s [t_s, MiB]:
[[0.0, 317], [5.0, 398], [10.0, 420], [15.0, 428], [20.0, 454], [25.0, 457], [30.0, 455], [35.0, 459], [40.0, 470], [45.1, 478], [50.1, 484], [55.1, 486], [60.1, 499], [65.1, 501], [70.1, 503], [75.1, 504], [80.1, 505], [85.1, 506], [90.1, 507], [95.1, 507], [100.1, 507], [105.1, 505], [110.1, 505], [115.1, 508], [120.1, 506], [125.1, 506], [130.1, 506], [135.2, 507], [140.2, 507], [145.2, 507], [150.2, 507], [155.2, 509], [160.2, 505], [165.2, 505], [170.2, 505], [175.2, 505], [180.2, 505], [185.2, 505], [190.2, 505], [195.2, 506], [200.2, 506], [205.2, 506], [210.2, 506], [215.2, 507], [220.2, 506], [225.3, 506], [230.3, 506], [235.3, 506], [240.3, 506], [245.3, 506], [250.3, 507], [255.3, 506], [260.3, 507], [265.3, 506], [270.3, 506], [275.3, 507], [280.3, 506], [285.3, 506], [290.3, 507], [295.3, 507], [300.3, 507], [305.3, 507], [310.4, 507], [315.4, 507], [320.4, 507], [325.4, 507], [330.4, 507], [335.4, 507], [340.4, 507], [345.4, 507], [350.4, 507], [355.4, 507], [360.4, 508], [365.4, 508], [370.4, 508], [375.4, 508], [380.4, 503], [385.4, 503], [390.4, 503], [395.5, 503]]
```
