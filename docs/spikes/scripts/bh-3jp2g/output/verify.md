# bh-3jp2g `verify.py` output

Captured stdout against the instrumented full load on 2026-10-30, 23:35:29–23:35:39 UTC
(after the post-load observation window).

```text
# verify.py

## V1. FOR VALID_TIME AS OF <cut> vs the plain-Python replay of the events table

-- cut 2026-08-21T00:00:00Z: XTDB rows beads/labels/edges = (3381, 7593, 3499), read 0.49 s; replay beads existing = 3381
   all 9088 beads: 9088 match, 0 differ
   sample of 60: 60 match, 0 differ
   bd pre-image witnesses (101 beads): 101 match, 0 differ
   replay vs the same witnesses: 101 match, 0 differ

-- cut 2026-09-18T00:00:00Z: XTDB rows beads/labels/edges = (5763, 12588, 6261), read 0.46 s; replay beads existing = 5763
   all 9088 beads: 9088 match, 0 differ
   sample of 60: 60 match, 0 differ
   bd pre-image witnesses (91 beads): 91 match, 0 differ
   replay vs the same witnesses: 91 match, 0 differ

-- cut 2026-10-16T00:00:00Z: XTDB rows beads/labels/edges = (8419, 18378, 9586), read 0.42 s; replay beads existing = 8419
   all 9088 beads: 9088 match, 0 differ
   sample of 60: 60 match, 0 differ
   bd pre-image witnesses (47 beads): 47 match, 0 differ
   replay vs the same witnesses: 47 match, 0 differ

-- cut 2026-07-20T00:00:00Z: XTDB rows beads/labels/edges = (621, 1621, 647), read 0.07 s; replay beads existing = 621
   all 9088 beads: 9088 match, 0 differ
   sample of 60: 60 match, 0 differ

## V2. Current state (no temporal clause) vs bh-export.jsonl and vs the replay's end
   read 0.49 s, rows beads/labels/edges = (9088, 19428, 10273)
   XTDB now vs export: 9076 match, 12 differ  by field {'edges': 8, 'status': 1, 'assignee': 1, 'notes': 2}
      e.g. ('bh-mnpkc', 'edges', "['blocks|bh-iqksj', 'blocks|bh-jr6ez', 'blocks|bh-ztu99', 'parent-chil", "['blocks|bh-iqksj', 'blocks|bh-jr6ez', 'blocks|bh-ztu99', 'parent-chil")
      e.g. ('bh-mw38f', 'edges', "['blocks|bh-1mnp1', 'blocks|bh-2a5iw', 'blocks|bh-4jy9g', 'blocks|bh-8", "['blocks|bh-1mnp1', 'blocks|bh-2a5iw', 'blocks|bh-4jy9g', 'blocks|bh-8")
      e.g. ('bh-tfko9', 'edges', "['blocks|bh-cokbi', 'blocks|bh-eo8br']", "['blocks|bh-cokbi', 'blocks|bh-eo8br', 'parent-child|bh-24ay9']")
      e.g. ('bh-uf986', 'edges', "['blocks|bh-w22e8']", "['blocks|bh-w22e8', 'parent-child|bh-5d54p']")
   XTDB now vs replay end state: 9088 match, 0 differ

## V3. System time
   cut 2026-08-21T00:00:00Z: SYSTEM_TIME AS OF == VALID_TIME AS OF for 3381/3381 beads; both axes pinned == VALID_TIME AS OF for 3381/3381  (reads 0.20/0.18/0.22 s)
   cut 2026-09-18T00:00:00Z: SYSTEM_TIME AS OF == VALID_TIME AS OF for 5763/5763 beads; both axes pinned == VALID_TIME AS OF for 5763/5763  (reads 0.37/0.25/0.33 s)
   cut 2026-10-16T00:00:00Z: SYSTEM_TIME AS OF == VALID_TIME AS OF for 8419/8419 beads; both axes pinned == VALID_TIME AS OF for 8419/8419  (reads 0.50/0.37/0.40 s)

   real label period: bh-e0kzf review:pending [2026-10-30T00:43:27Z, 2026-10-30T00:44:32Z)
   SYSTEM_TIME AS OF 2026-10-30T00:44:31Z VALID_TIME AS OF 2026-10-30T00:44:33Z: present=True  (belief just before the removal was recorded)
   SYSTEM_TIME AS OF 2026-10-30T00:44:33Z VALID_TIME AS OF 2026-10-30T00:44:33Z: present=False  (belief just after)
   SYSTEM_TIME AS OF 2026-10-30T00:44:31Z VALID_TIME AS OF 2026-10-30T00:44:31Z: present=True  (both axes before the removal)

-- every bitemporal row of bh-e0kzf|review:pending
   SELECT _valid_from, _valid_to, _system_from, _system_to FROM bead_labels FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE _id = %s ORDER BY _system_from, _valid_from
   cols: ['_valid_from', '_valid_to', '_system_from', '_system_to']
    ('2026-10-30T00:43:27Z', '2026-10-30T00:44:32Z', '2026-10-30T00:43:27Z', None)
    ('2026-10-30T00:44:32Z', None, '2026-10-30T00:43:27Z', '2026-10-30T00:44:32Z')

-- bead versions of bh-q0lol.5 recorded in system time BETWEEN 09-02 and 09-03
   SELECT status, assignee, src, _valid_from, _system_from, _system_to FROM beads FOR SYSTEM_TIME BETWEEN TIMESTAMP '2026-09-02T00:00:00Z' AND TIMESTAMP '2026-09-03T00:00:00Z' FOR ALL VALID_TIME WHERE _id = 'bh-q0lol.5' ORDER BY _system_from, _valid_from
   cols: ['status', 'assignee', 'src', '_valid_from', '_system_from', '_system_to']
    ('open', None, 'seed', '2026-08-24T20:56:02Z', '2026-08-24T20:56:02Z', None)
    ('open', None, 'seed', '2026-09-02T19:32:52Z', '2026-08-24T20:56:02Z', '2026-09-02T19:32:52Z')
    ('open', 'dev/daemon-factory', '770a0df3-556f-5db7-aecd-666025fd877c', '2026-09-02T19:32:52Z', '2026-09-02T19:32:52Z', None)
    ('open', 'dev/daemon-factory', '770a0df3-556f-5db7-aecd-666025fd877c', '2026-09-02T22:39:35Z', '2026-09-02T19:32:52Z', '2026-09-02T22:39:35Z')
    ('in_progress', 'dev/daemon-factory', '4098b8d3-5c6b-586e-bb7e-f16f245c94c7', '2026-09-02T22:39:35Z', '2026-09-02T22:39:35Z', None)
    ('in_progress', 'dev/daemon-factory', '4098b8d3-5c6b-586e-bb7e-f16f245c94c7', '2026-09-02T23:49:53Z', '2026-09-02T22:39:35Z', '2026-09-02T23:49:53Z')
    ('in_progress', 'dev/daemon-factory', '1155055d-b0b9-55c1-9e79-9e2ea13dca31', '2026-09-02T23:49:53Z', '2026-09-02T23:49:53Z', None)

-- beads census
   SELECT COUNT(*) AS bitemporal_rows, COUNT(DISTINCT _id) AS entities, COUNT(DISTINCT _system_from) AS distinct_system_from, MIN(_system_from), MAX(_system_from) FROM beads FOR ALL SYSTEM_TIME FOR ALL VALID_TIME
   cols: ['bitemporal_rows', 'entities', 'distinct_system_from', '_column_4', '_column_5']
    (39030, 9088, 22501, '2026-06-29T03:43:46Z', '2026-10-30T21:32:38Z')

-- bead_labels census
   SELECT COUNT(*) AS bitemporal_rows, COUNT(DISTINCT _id) AS entities, COUNT(DISTINCT _system_from) AS distinct_system_from, MIN(_system_from), MAX(_system_from) FROM bead_labels FOR ALL SYSTEM_TIME FOR ALL VALID_TIME
   cols: ['bitemporal_rows', 'entities', 'distinct_system_from', '_column_4', '_column_5']
    (24092, 21241, 6761, '2026-06-29T03:43:46Z', '2026-10-30T21:24:56Z')

-- bead_edges census
   SELECT COUNT(*) AS bitemporal_rows, COUNT(DISTINCT _id) AS entities, COUNT(DISTINCT _system_from) AS distinct_system_from, MIN(_system_from), MAX(_system_from) FROM bead_edges FOR ALL SYSTEM_TIME FOR ALL VALID_TIME
   cols: ['bitemporal_rows', 'entities', 'distinct_system_from', '_column_4', '_column_5']
    (10409, 10327, 7830, '2026-06-29T15:27:44Z', '2026-10-30T21:24:56Z')

-- bead_comments census
   SELECT COUNT(*) AS bitemporal_rows, COUNT(DISTINCT _id) AS entities, COUNT(DISTINCT _system_from) AS distinct_system_from, MIN(_system_from), MAX(_system_from) FROM bead_comments FOR ALL SYSTEM_TIME FOR ALL VALID_TIME
   cols: ['bitemporal_rows', 'entities', 'distinct_system_from', '_column_4', '_column_5']
    (691, 691, 690, '2026-07-03T16:33:07Z', '2026-10-28T08:01:12Z')

-- rows whose system time differs from their valid-from (expect 0: recorded = occurred)
   SELECT COUNT(*) FROM beads FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE _system_from <> _valid_from AND _system_to IS NULL AND _valid_to IS NULL
   cols: ['_column_1']
    (0,)

-- lookup one bead as of a cut (latency)
   SELECT status, assignee FROM beads FOR VALID_TIME AS OF TIMESTAMP '2026-09-18T00:00:00Z' WHERE _id = 'bh-q0lol.5'
   cols: ['status', 'assignee']
    ('closed', 'dev/daemon-factory')
   20 ms
```
