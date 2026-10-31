# bh-3jp2g `erase.py` output

Captured stdout against the same node right after `verify.py`, 23:36:08–23:36:15 UTC.
Re-checks 150 s later (23:38:55 UTC) and again at teardown (`grep -rlaF` on the data dir) still
found the erased title's bytes in the same five files:

```text
buffers/v06/tables/public$beads/data/l01-r20260907-b00.arrow
buffers/v06/tables/public$beads/data/l01-rc-b00.arrow
buffers/v06/tables/public$beads/data/l00-rc-b00.arrow
log/LOG
log/REPLICA_LOG
```

```text
# erase.py — target bh-q0lol.5 ('Serve the authenticated factory and exact run directory')

## Before ERASE: rows of the target under each read (beads/labels/edges/comments)
   default (now)                                                               {'beads': 1, 'bead_labels': 6, 'bead_edges': 17, 'bead_comments': 0}
   FOR VALID_TIME AS OF 2026-08-21T00:00:00Z                                   {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR SYSTEM_TIME AS OF 2026-08-21T00:00:00Z                                  {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR SYSTEM_TIME AS OF 2026-08-21T00:00:00Z FOR VALID_TIME AS OF 2026-08-21T00:00:00Z {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR VALID_TIME AS OF 2026-09-18T00:00:00Z                                   {'beads': 1, 'bead_labels': 6, 'bead_edges': 17, 'bead_comments': 0}
   FOR SYSTEM_TIME AS OF 2026-09-18T00:00:00Z                                  {'beads': 1, 'bead_labels': 6, 'bead_edges': 17, 'bead_comments': 0}
   FOR SYSTEM_TIME AS OF 2026-09-18T00:00:00Z FOR VALID_TIME AS OF 2026-09-18T00:00:00Z {'beads': 1, 'bead_labels': 6, 'bead_edges': 17, 'bead_comments': 0}
   FOR VALID_TIME AS OF 2026-10-16T00:00:00Z                                   {'beads': 1, 'bead_labels': 6, 'bead_edges': 17, 'bead_comments': 0}
   FOR SYSTEM_TIME AS OF 2026-10-16T00:00:00Z                                  {'beads': 1, 'bead_labels': 6, 'bead_edges': 17, 'bead_comments': 0}
   FOR SYSTEM_TIME AS OF 2026-10-16T00:00:00Z FOR VALID_TIME AS OF 2026-10-16T00:00:00Z {'beads': 1, 'bead_labels': 6, 'bead_edges': 17, 'bead_comments': 0}
   FOR ALL VALID_TIME                                                          {'beads': 18, 'bead_labels': 33, 'bead_edges': 17, 'bead_comments': 0}
   FOR ALL SYSTEM_TIME                                                         {'beads': 18, 'bead_labels': 33, 'bead_edges': 17, 'bead_comments': 0}
   FOR ALL SYSTEM_TIME FOR ALL VALID_TIME                                      {'beads': 35, 'bead_labels': 60, 'bead_edges': 17, 'bead_comments': 0}
   FOR SYSTEM_TIME BETWEEN 2026-06-01 AND 2026-12-01 FOR ALL VALID_TIME        {'beads': 35, 'bead_labels': 60, 'bead_edges': 17, 'bead_comments': 0}
   other beads' bitemporal rows: {'beads': 38995, 'bead_labels': 24032, 'bead_edges': 10392, 'bead_comments': 691}; inbound edges from other beads: 31
   data-dir files containing the title prefix 'Serve the authenticated factory and exac': ['buffers/v06/tables/public$beads/data/l00-rc-b00.arrow', 'buffers/v06/tables/public$beads/data/l01-r20260907-b00.arrow', 'buffers/v06/tables/public$beads/data/l01-rc-b00.arrow', 'log/LOG', 'log/REPLICA_LOG']

## ERASE (one wall-clock transaction)

-- xt.txs, last
   SELECT _id, committed, system_time, error FROM xt.txs ORDER BY _id DESC LIMIT 1
   cols: ['_id', 'committed', 'system_time', 'error']
    (133000118, True, '2026-10-30T23:36:09.921067Z', None)

## After ERASE: rows of the target under each read (beads/labels/edges/comments)
   default (now)                                                               {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR VALID_TIME AS OF 2026-08-21T00:00:00Z                                   {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR SYSTEM_TIME AS OF 2026-08-21T00:00:00Z                                  {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR SYSTEM_TIME AS OF 2026-08-21T00:00:00Z FOR VALID_TIME AS OF 2026-08-21T00:00:00Z {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR VALID_TIME AS OF 2026-09-18T00:00:00Z                                   {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR SYSTEM_TIME AS OF 2026-09-18T00:00:00Z                                  {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR SYSTEM_TIME AS OF 2026-09-18T00:00:00Z FOR VALID_TIME AS OF 2026-09-18T00:00:00Z {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR VALID_TIME AS OF 2026-10-16T00:00:00Z                                   {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR SYSTEM_TIME AS OF 2026-10-16T00:00:00Z                                  {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR SYSTEM_TIME AS OF 2026-10-16T00:00:00Z FOR VALID_TIME AS OF 2026-10-16T00:00:00Z {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR ALL VALID_TIME                                                          {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR ALL SYSTEM_TIME                                                         {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR ALL SYSTEM_TIME FOR ALL VALID_TIME                                      {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   FOR SYSTEM_TIME BETWEEN 2026-06-01 AND 2026-12-01 FOR ALL VALID_TIME        {'beads': 0, 'bead_labels': 0, 'bead_edges': 0, 'bead_comments': 0}
   all zero: True
   other beads' bitemporal rows: {'beads': 38995, 'bead_labels': 24032, 'bead_edges': 10392, 'bead_comments': 691}  unchanged: True
   inbound edges from other beads (not erased, now dangling): 31

-- title search across all time
   SELECT COUNT(*) FROM beads FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE title = %s
   cols: ['_column_1']
    (0,)

## Every other bead still reproduces the replay
   cut 2026-08-21T00:00:00Z: 9087 match, 0 differ 
   cut 2026-09-18T00:00:00Z: 9087 match, 0 differ 
   cut 2026-10-16T00:00:00Z: 9087 match, 0 differ 

   data-dir files containing the title prefix right after ERASE: ['buffers/v06/tables/public$beads/data/l00-rc-b00.arrow', 'buffers/v06/tables/public$beads/data/l01-r20260907-b00.arrow', 'buffers/v06/tables/public$beads/data/l01-rc-b00.arrow', 'log/LOG', 'log/REPLICA_LOG']
```
