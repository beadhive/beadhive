# bh-cntr0 `harness.py matrix logs` output

Raw sequential logs of the cap matrix. `rc=124` is `timeout` firing; the counts lines are
`counts.py` output after each load. The runs were driven by small shell helpers that were folded
into `harness.py` for the repository (the file-type check accepts no `.sh` here); the logic is
the same.

## capmatrix.log

```text
=== 1536m 1536m
started after 22.1s
{'label': '1536m', 'container_state_after_load': 'running exit=0 oom_killed=false', 'rc': 0, 'idle_mib': 330, 'peak_mib': 903, 'end_mib': 889, 'load_wall_s': 58.0, 'data_bytes': 116938448, 'memory_events': {'low': '0', 'high': '0', 'max': '0', 'oom': '0', 'oom_kill': '0', 'oom_group_kill': '0'}}
{"beads_a": [9088, "a6e79cda66e1e450"], "beads_b": [9088, "a6e79cda66e1e450"], "bead_labels": [21760, "1a436398732cb6c0"], "bead_edges": [10321, "4f24bd9b88d07527"], "bead_comments": [676, "a1fe5843e369adbf"], "bd_events": [36716, "23d198a25bb39114"], "query_all_s": 2.08}
rc=0
=== 768m 768m
started after 21.0s
rc=124
=== 512m 512m
started after 21.0s
{'label': '512m', 'container_state_after_load': 'running exit=0 oom_killed=false', 'rc': 1, 'idle_mib': 317, 'peak_mib': 512, 'end_mib': 503, 'load_wall_s': 378.3, 'data_bytes': 56238489, 'memory_events': {'low': '0', 'high': '0', 'max': '19', 'oom': '0', 'oom_kill': '0', 'oom_group_kill': '0'}}
Traceback (most recent call last):
  File "/tmp/bh-worktrees/github/beadhive/beadhive/bh-cntr0/docs/spikes/scripts/bh-cntr0/counts.py", line 24, in <module>
    rows = conn.execute(q).fetchall()
           ^^^^^^^^^^^^^^^
  File "/home/bees/.cache/uv/archive-v0/nQSNZLCownPc2Qxi/lib/python3.12/site-packages/psycopg/connection.py", line 304, in execute
    raise ex.with_traceback(None)
psycopg.errors.ProtocolViolation: Errors planning SQL statement:
  - Table not found: bead_edges
  - Column not found: _id
  - Column not found: _id
DETAIL:  {"category":"incorrect","code":"xtdb/sql-error","message":"Errors planning SQL statement:\n  - Table not found: bead_edges\n  - Column not found: _id\n  - Column not found: _id","data":{"errors":[{"tx-db-name":null,"table-chain":["bead_edges"]},{"chain":["_id"]},{"chain":["_id"]}]}}
Caused by: java.lang.OutOfMemoryError: Java heap space
java.lang.OutOfMemoryError: Java heap space
java.lang.OutOfMemoryError: Java heap space
rc=0
=== 1g-heap40 1g -e JDK_JAVA_OPTIONS=-XX:MaxRAMPercentage=40
started after 19.7s
{'label': '1g-heap40', 'container_state_after_load': 'running exit=0 oom_killed=false', 'rc': 0, 'idle_mib': 320, 'peak_mib': 933, 'end_mib': 919, 'load_wall_s': 54.2, 'data_bytes': 116933069, 'memory_events': {'low': '0', 'high': '0', 'max': '0', 'oom': '0', 'oom_kill': '0', 'oom_group_kill': '0'}}
{"beads_a": [9088, "a6e79cda66e1e450"], "beads_b": [9088, "a6e79cda66e1e450"], "bead_labels": [21760, "1a436398732cb6c0"], "bead_edges": [10321, "4f24bd9b88d07527"], "bead_comments": [676, "a1fe5843e369adbf"], "bd_events": [36716, "23d198a25bb39114"], "query_all_s": 2.32}
rc=0
DONE
```

## capmatrix2.log

```text
=== 768m-heap40 768m -e JDK_JAVA_OPTIONS=-XX:MaxRAMPercentage=40
started after 20.2s
{'label': '768m-heap40', 'container_state_after_load': 'running exit=0 oom_killed=false', 'rc': 0, 'idle_mib': 316, 'peak_mib': 768, 'end_mib': 749, 'load_wall_s': 99.2, 'data_bytes': 116925689, 'memory_events': {'low': '0', 'high': '0', 'max': '292', 'oom': '0', 'oom_kill': '0', 'oom_group_kill': '0'}}
{"beads_a": [9088, "a6e79cda66e1e450"], "beads_b": [9088, "a6e79cda66e1e450"], "bead_labels": [21760, "1a436398732cb6c0"], "bead_edges": [10321, "4f24bd9b88d07527"], "bead_comments": [676, "a1fe5843e369adbf"], "bd_events": [36716, "23d198a25bb39114"], "query_all_s": 3.18}
health: unhealthy streak=5
rc=0
=== 2g-final 2g
started after 19.6s
{'label': '2g-final', 'container_state_after_load': 'running exit=0 oom_killed=false', 'rc': 0, 'idle_mib': 396, 'peak_mib': 1003, 'end_mib': 985, 'load_wall_s': 47.1, 'data_bytes': 116932371, 'memory_events': {'low': '0', 'high': '0', 'max': '0', 'oom': '0', 'oom_kill': '0', 'oom_group_kill': '0'}}
{"beads_a": [9088, "a6e79cda66e1e450"], "beads_b": [9088, "a6e79cda66e1e450"], "bead_labels": [21760, "1a436398732cb6c0"], "bead_edges": [10321, "4f24bd9b88d07527"], "bead_comments": [676, "a1fe5843e369adbf"], "bd_events": [36716, "23d198a25bb39114"], "query_all_s": 2.03}
health: unhealthy streak=3
rc=0
DONE
```
