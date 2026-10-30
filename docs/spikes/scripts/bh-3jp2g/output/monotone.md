# bh-3jp2g `monotone.py` output

Captured stdout against XTDB 2.2.0 on 2026-10-30, each part on its own FRESH node
(`xtdb.sh down && xtdb.sh up`), run in the order part 1, part 3, part 2. Long `xt.txs` error
payloads are cut at 200 characters by the script.

## Part 1 (`monotone.py 1`): syntax, ties, retries, out-of-order, null

```text
XTDB version: PostgreSQL 16 (XTDB 2.2.0)

######## S1. Which SYSTEM_TIME syntaxes does 2.2.0 accept?

== S1a BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP ...)
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-01T00:00:00Z')
   client: OK
   xt.txs: id=0 committed=True system_time=2020-01-01T00:00:00Z 

== S1b BEGIN READ WRITE, SYSTEM_TIME = TIMESTAMP ...
   BEGIN READ WRITE, SYSTEM_TIME = TIMESTAMP '2020-01-02T00:00:00Z'
   client: OK
   xt.txs: id=1556 committed=True system_time=2020-01-02T00:00:00Z 

== S1c BEGIN; SET TRANSACTION READ WRITE, SYSTEM_TIME = ...
   BEGIN; SET TRANSACTION READ WRITE, SYSTEM_TIME = TIMESTAMP '2020-01-03T00:00:00Z'
   client: OK
   xt.txs: id=3112 committed=True system_time=2020-01-03T00:00:00Z 

== S1d SYSTEM_TIME as a plain string literal (no TIMESTAMP keyword)
   BEGIN READ WRITE WITH (SYSTEM_TIME = '2020-01-04T00:00:00Z')
   client: OK
   xt.txs: id=4668 committed=True system_time=2020-01-04T00:00:00Z 

== S1e SYSTEM_TIME with microseconds
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-05T00:00:00.000001Z')
   client: OK
   xt.txs: id=6224 committed=True system_time=2020-01-05T00:00:00.000001Z 

== S1f SYSTEM_TIME as a bind parameter
   client: OK
   xt.txs: id=7780 committed=True system_time=2020-01-06T00:00:00Z 

== S1g `SETTING SYSTEM_TIME = ...` prefix on DML (SETTING is documented for queries)
   client: ERR Errors parsing SQL statement:

-- S1 rows and their system time
   SELECT _id, _system_from, _valid_from FROM s1 ORDER BY _id
   cols: ['_id', '_system_from', '_valid_from']
    ('a', '2020-01-01T00:00:00Z', '2020-01-01T00:00:00Z')
    ('b', '2020-01-02T00:00:00Z', '2020-01-02T00:00:00Z')
    ('c', '2020-01-03T00:00:00Z', '2020-01-03T00:00:00Z')
    ('d', '2020-01-04T00:00:00Z', '2020-01-04T00:00:00Z')
    ('e', '2020-01-05T00:00:00.000001Z', '2020-01-05T00:00:00.000001Z')
    ('f', '2020-01-06T00:00:00Z', '2020-01-06T00:00:00Z')

######## S2. Tie: SYSTEM_TIME equal to the latest committed transaction

== S2 tie with S1e (2020-01-05T00:00:00.000001Z)
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-05T00:00:00.000001Z')
   client: ERR at `COMMIT`: specified system-time must be later than current tx
   xt.txs: id=9336 committed=False system_time=2026-10-30T22:58:51.671747Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",7780,"~:system-time",["~#time/

-- S2 atomicity: rows of the aborted tie tx
   SELECT _id FROM s2
   ERR ProtocolViolation Errors planning SQL statement:

######## S3. Retrying after an abort

== S3a set the clock: commit at 2020-01-10
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-10T00:00:00Z')
   client: ERR at `COMMIT`: specified system-time must be later than current tx
   xt.txs: id=10924 committed=False system_time=2026-10-30T22:58:51.761712Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",9336,"~:system-time",["~#time/

== S3b tie at 2020-01-10 (aborts)
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-10T00:00:00Z')
   client: ERR at `COMMIT`: specified system-time must be later than current tx
   xt.txs: id=12480 committed=False system_time=2026-10-30T22:58:51.803839Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",10924,"~:system-time",["~#time

== S3c retry at +1us 2020-01-10T00:00:00.000001Z
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-10T00:00:00.000001Z')
   client: ERR at `COMMIT`: specified system-time must be later than current tx
   xt.txs: id=14036 committed=False system_time=2026-10-30T22:58:51.847942Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",12480,"~:system-time",["~#time

== S3d retry at +2us
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-10T00:00:00.000002Z')
   client: ERR at `COMMIT`: specified system-time must be later than current tx
   xt.txs: id=15592 committed=False system_time=2026-10-30T22:58:51.887127Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",14036,"~:system-time",["~#time

-- s3 x: every bitemporal row
   SELECT _id, v, _valid_from, _valid_to, _system_from, _system_to FROM s3 FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE _id = %s ORDER BY _system_from, _valid_from
   ERR ProtocolViolation Errors planning SQL statement:

######## S4. Out-of-order (earlier) SYSTEM_TIME, then the load continues

== S4a earlier than latest (2020-01-08)
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-08T00:00:00Z')
   client: ERR at `COMMIT`: specified system-time must be later than current tx
   xt.txs: id=17148 committed=False system_time=2026-10-30T22:58:51.962375Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",15592,"~:system-time",["~#time

== S4b next in order (2020-01-11)
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-11T00:00:00Z')
   client: ERR at `COMMIT`: specified system-time must be later than current tx
   xt.txs: id=18704 committed=False system_time=2026-10-30T22:58:52.006257Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",17148,"~:system-time",["~#time

-- S4 rows
   SELECT _id, _system_from FROM s4
   ERR ProtocolViolation Errors planning SQL statement:

######## S5. Does a tx that fails for a non-time reason consume its SYSTEM_TIME?

== S5a statement error (record without _id) at 2020-01-12
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-12T00:00:00Z')
   client: ERR at `COMMIT`: missing '_id'
   xt.txs: id=18704 committed=False system_time=2026-10-30T22:58:52.006257Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",17148,"~:system-time",["~#time

== S5b ASSERT failure at 2020-01-13
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-13T00:00:00Z')
   client: ERR at `COMMIT`: specified system-time must be later than current tx
   xt.txs: id=20260 committed=False system_time=2026-10-30T22:58:52.099458Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",18704,"~:system-time",["~#time

== S5c fixed retry at the same 2020-01-13
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-13T00:00:00Z')
   client: ERR at `COMMIT`: specified system-time must be later than current tx
   xt.txs: id=22176 committed=False system_time=2026-10-30T22:58:52.134709Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",20260,"~:system-time",["~#time

== S5d fixed retry at 2020-01-14
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-14T00:00:00Z')
   client: ERR at `COMMIT`: specified system-time must be later than current tx
   xt.txs: id=23732 committed=False system_time=2026-10-30T22:58:52.172469Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",22176,"~:system-time",["~#time

######## S6. Null / default SYSTEM_TIME

== S6a SYSTEM_TIME = NULL
   BEGIN READ WRITE WITH (SYSTEM_TIME = NULL)
   client: OK
   xt.txs: id=25288 committed=True system_time=2026-10-30T22:58:52.210482Z 

== S6b SET TRANSACTION ..., SYSTEM_TIME = DEFAULT after setting one
   BEGIN; SET TRANSACTION READ WRITE, SYSTEM_TIME = TIMESTAMP '2020-01-15T00:00:00Z'; SET TRANSACTION READ WRITE, SYSTEM_TIME = DEFAULT
   client: OK
   xt.txs: id=26835 committed=True system_time=2026-10-30T22:58:52.257475Z 

-- S6 rows
   SELECT _id, _system_from FROM s6
   cols: ['_id', '_system_from']
    ('d', '2026-10-30T22:58:52.257475Z')
    ('n', '2026-10-30T22:58:52.210482Z')

== S6c after any wall-clock tx, can backfill still continue? (2020-01-16)
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-16T00:00:00Z')
   client: ERR at `COMMIT`: specified system-time must be later than current tx
   xt.txs: id=28382 committed=False system_time=2026-10-30T22:58:52.302595Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",26835,"~:system-time",["~#time
```

## Part 3 (`monotone.py 3`): a non-time abort; an attached database

```text

######## S12. Abort for a non-time reason inside a backfill

== S12a commit at 2020-01-01
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-01T00:00:00Z')
   client: OK
   xt.txs: id=0 committed=True system_time=2020-01-01T00:00:00Z 

== S12b ASSERT failure at 2020-01-02
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-02T00:00:00Z')
   client: ERR at `COMMIT`: forced
   xt.txs: id=1556 committed=False system_time=2020-01-02T00:00:00Z ["~#xt/error",["~:conflict","~:xtdb/assert-failed","forced",["^ ","~:tx-key",["~#xtdb/tx-key",["^ ","~:tx-id",1556,"~:system-time",["~#time/instant","2020-01-02T00:00:00Z"]]],"~:tx-op-idx",0,"~:sql","

== S12c next tx at 2020-01-03
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-03T00:00:00Z')
   client: OK
   xt.txs: id=3472 committed=True system_time=2020-01-03T00:00:00Z 
   xt.txs: id=0 committed=True system_time=2020-01-01T00:00:00Z 
   xt.txs: id=1556 committed=False system_time=2020-01-02T00:00:00Z ["~#xt/error",["~:conflict","~:xtdb/assert-failed","forced",["^ ","~:tx-key",["~#xtdb/tx-key",["^ ","~:tx-id",1556,"~:system-time",["~#time/instant","2020-01-02T00:00:00Z"]]],"~:tx-op-idx",0,"~:sql","
   xt.txs: id=3472 committed=True system_time=2020-01-03T00:00:00Z 

######## S13. Does an attached database keep its own system-time clock?

-- S13 attach
   ATTACH DATABASE bf2

== S13a backfill 2020-01-01 into the fresh attached database bf2
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-01T00:00:00Z')
   client: OK
   xt.txs: id=0 committed=True system_time=2020-01-01T00:00:00Z 

-- S13 bf2 rows
   SELECT _id, _system_from FROM s13
   cols: ['_id', '_system_from']
    ('a', '2020-01-01T00:00:00Z')
```

## Part 2 (`monotone.py 2`): within-tx writes, DELETE defaults, reads, null-recorded, ASYNC

S10a/S10b ran after the wall-clock tx of S11c, so both were out of order: they show that
`COMMIT ASYNC` reports success to the client for transactions that abort.

```text

######## S7. Several versions of one entity inside one backfill tx

== S7 v1@01-01, v2@01-03, then v3 and v4 both @01-05 (same _valid_from)
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-05T00:00:00Z')
   client: OK
   xt.txs: id=0 committed=True system_time=2020-01-05T00:00:00Z 

-- s7 e: every bitemporal row
   SELECT _id, v, _valid_from, _valid_to, _system_from, _system_to FROM s7 FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE _id = %s ORDER BY _system_from, _valid_from
   cols: ['_id', 'v', '_valid_from', '_valid_to', '_system_from', '_system_to']
    ('e', 1, '2020-01-01T00:00:00Z', '2020-01-03T00:00:00Z', '2020-01-05T00:00:00Z', None)
    ('e', 2, '2020-01-03T00:00:00Z', '2020-01-05T00:00:00Z', '2020-01-05T00:00:00Z', None)
    ('e', 4, '2020-01-05T00:00:00Z', None, '2020-01-05T00:00:00Z', None)

######## S8. INSERT/DELETE defaults under a backfill SYSTEM_TIME

== S8a INSERT with no _valid_from at SYSTEM_TIME 2020-01-06
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-06T00:00:00Z')
   client: OK
   xt.txs: id=1708 committed=True system_time=2020-01-06T00:00:00Z 

== S8b plain DELETE (no FOR PORTION) at SYSTEM_TIME 2020-01-08
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-08T00:00:00Z')
   client: OK
   xt.txs: id=3264 committed=True system_time=2020-01-08T00:00:00Z 

-- s8 lbl: every bitemporal row
   SELECT _id, v, _valid_from, _valid_to, _system_from, _system_to FROM s8 FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE _id = %s ORDER BY _system_from, _valid_from
   cols: ['_id', 'v', '_valid_from', '_valid_to', '_system_from', '_system_to']
    ('lbl', 1, '2020-01-06T00:00:00Z', '2020-01-08T00:00:00Z', '2020-01-06T00:00:00Z', None)
    ('lbl', 1, '2020-01-08T00:00:00Z', None, '2020-01-06T00:00:00Z', '2020-01-08T00:00:00Z')

== S8c re-add at 2020-01-09; DELETE FOR PORTION OF VALID_TIME FROM 01-10 TO NULL at 2020-01-10
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-09T00:00:00Z')
   client: OK
   xt.txs: id=4020 committed=True system_time=2020-01-09T00:00:00Z 

== S8c delete open-ended with TO NULL
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-10T00:00:00Z')
   client: OK
   xt.txs: id=5576 committed=True system_time=2020-01-10T00:00:00Z 

-- s8 lbl: every bitemporal row
   SELECT _id, v, _valid_from, _valid_to, _system_from, _system_to FROM s8 FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE _id = %s ORDER BY _system_from, _valid_from
   cols: ['_id', 'v', '_valid_from', '_valid_to', '_system_from', '_system_to']
    ('lbl', 1, '2020-01-06T00:00:00Z', '2020-01-08T00:00:00Z', '2020-01-06T00:00:00Z', None)
    ('lbl', 1, '2020-01-08T00:00:00Z', None, '2020-01-06T00:00:00Z', '2020-01-08T00:00:00Z')
    ('lbl', 2, '2020-01-09T00:00:00Z', '2020-01-10T00:00:00Z', '2020-01-09T00:00:00Z', None)
    ('lbl', 2, '2020-01-10T00:00:00Z', None, '2020-01-09T00:00:00Z', '2020-01-10T00:00:00Z')

== S8d INSERT with a _valid_from AFTER the tx SYSTEM_TIME (future-dated)
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-11T00:00:00Z')
   client: OK
   xt.txs: id=6404 committed=True system_time=2020-01-11T00:00:00Z 

-- s8 fut: every bitemporal row
   SELECT _id, v, _valid_from, _valid_to, _system_from, _system_to FROM s8 FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE _id = %s ORDER BY _system_from, _valid_from
   cols: ['_id', 'v', '_valid_from', '_valid_to', '_system_from', '_system_to']
    ('fut', 1, '2020-01-20T00:00:00Z', None, '2020-01-11T00:00:00Z', None)

######## S9. Bitemporal reads: a backdated correction

== S9a sys 01-12: price=10 valid from 01-12
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-12T00:00:00Z')
   client: OK
   xt.txs: id=7960 committed=True system_time=2020-01-12T00:00:00Z 

== S9b sys 01-14: correction price=9 valid [01-11, 01-13)
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-14T00:00:00Z')
   client: OK
   xt.txs: id=9516 committed=True system_time=2020-01-14T00:00:00Z 

-- s9 p: every bitemporal row
   SELECT _id, v, _valid_from, _valid_to, _system_from, _system_to FROM s9 FOR ALL SYSTEM_TIME FOR ALL VALID_TIME WHERE _id = %s ORDER BY _system_from, _valid_from
   cols: ['_id', 'v', '_valid_from', '_valid_to', '_system_from', '_system_to']
    ('p', 10, '2020-01-12T00:00:00Z', '2020-01-13T00:00:00Z', '2020-01-12T00:00:00Z', '2020-01-14T00:00:00Z')
    ('p', 10, '2020-01-13T00:00:00Z', None, '2020-01-12T00:00:00Z', None)
    ('p', 9, '2020-01-11T00:00:00Z', '2020-01-13T00:00:00Z', '2020-01-14T00:00:00Z', None)

-- S9 FOR SYSTEM_TIME AS OF 2020-01-13T00:00:00Z FOR VALID_TIME AS OF 2020-01-12T00:00:00Z
   SELECT v FROM s9 FOR SYSTEM_TIME AS OF TIMESTAMP '2020-01-13T00:00:00Z' FOR VALID_TIME AS OF TIMESTAMP '2020-01-12T00:00:00Z' WHERE _id = 'p'
   cols: ['v']
    (10,)

-- S9 FOR SYSTEM_TIME AS OF 2020-01-15T00:00:00Z FOR VALID_TIME AS OF 2020-01-12T00:00:00Z
   SELECT v FROM s9 FOR SYSTEM_TIME AS OF TIMESTAMP '2020-01-15T00:00:00Z' FOR VALID_TIME AS OF TIMESTAMP '2020-01-12T00:00:00Z' WHERE _id = 'p'
   cols: ['v']
    (9,)

-- S9 FOR SYSTEM_TIME AS OF 2020-01-15T00:00:00Z FOR VALID_TIME AS OF 2020-01-11T00:00:00Z
   SELECT v FROM s9 FOR SYSTEM_TIME AS OF TIMESTAMP '2020-01-15T00:00:00Z' FOR VALID_TIME AS OF TIMESTAMP '2020-01-11T00:00:00Z' WHERE _id = 'p'
   cols: ['v']
    (9,)

-- S9 FOR SYSTEM_TIME AS OF 2020-01-13T00:00:00Z FOR VALID_TIME AS OF 2020-01-11T00:00:00Z
   SELECT v FROM s9 FOR SYSTEM_TIME AS OF TIMESTAMP '2020-01-13T00:00:00Z' FOR VALID_TIME AS OF TIMESTAMP '2020-01-11T00:00:00Z' WHERE _id = 'p'
   cols: ['v']

-- S9 FOR SYSTEM_TIME BETWEEN 01-12 AND 01-14 (inclusive end)
   SELECT v, _system_from, _system_to, _valid_from, _valid_to FROM s9 FOR SYSTEM_TIME BETWEEN TIMESTAMP '2020-01-12T00:00:00Z' AND TIMESTAMP '2020-01-14T00:00:00Z' FOR ALL VALID_TIME ORDER BY _system_from, _valid_from
   cols: ['v', '_system_from', '_system_to', '_valid_from', '_valid_to']
    (10, '2020-01-12T00:00:00Z', '2020-01-14T00:00:00Z', '2020-01-12T00:00:00Z', '2020-01-13T00:00:00Z')
    (10, '2020-01-12T00:00:00Z', None, '2020-01-13T00:00:00Z', None)
    (9, '2020-01-14T00:00:00Z', None, '2020-01-11T00:00:00Z', '2020-01-13T00:00:00Z')

-- S9 FOR SYSTEM_TIME FROM 01-12 TO 01-14 (exclusive end)
   SELECT v, _system_from, _system_to, _valid_from, _valid_to FROM s9 FOR SYSTEM_TIME FROM TIMESTAMP '2020-01-12T00:00:00Z' TO TIMESTAMP '2020-01-14T00:00:00Z' FOR ALL VALID_TIME ORDER BY _system_from, _valid_from
   cols: ['v', '_system_from', '_system_to', '_valid_from', '_valid_to']
    (10, '2020-01-12T00:00:00Z', None, '2020-01-12T00:00:00Z', None)

-- S9 SETTING DEFAULT SYSTEM_TIME AS OF 01-13, DEFAULT VALID_TIME AS OF 01-11
   SETTING DEFAULT SYSTEM_TIME AS OF TIMESTAMP '2020-01-13T00:00:00Z', DEFAULT VALID_TIME AS OF TIMESTAMP '2020-01-11T00:00:00Z' SELECT v FROM s9 WHERE _id = 'p'
   cols: ['v']

-- S9 FOR SYSTEM_TIME AS OF with a bind parameter
   SELECT v FROM s9 FOR SYSTEM_TIME AS OF %s FOR VALID_TIME AS OF %s WHERE _id = 'p'
   cols: ['v']
    (9,)

######## S11. A null-recorded row loaded LAST, at wall-clock system time

== S11a sys 01-21: v1 valid from 01-21
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-21T00:00:00Z')
   client: OK
   xt.txs: id=11072 committed=True system_time=2020-01-21T00:00:00Z 

== S11b sys 01-22: v2 valid from 01-22
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-22T00:00:00Z')
   client: OK
   xt.txs: id=12660 committed=True system_time=2020-01-22T00:00:00Z 

== S11c wall clock: seed v0 valid [01-15, 01-21) on n (bounded), [01-15, inf) on m (unbounded)
   BEGIN READ WRITE
   client: OK
   xt.txs: id=14248 committed=True system_time=2026-10-30T23:00:27.866517Z 

-- S11 current-system-time history of n and m
   SELECT _id, v, _valid_from, _valid_to FROM s11 FOR ALL VALID_TIME ORDER BY _id, _valid_from
   cols: ['_id', 'v', '_valid_from', '_valid_to']
    ('m', 0, '2020-01-15T00:00:00Z', None)
    ('n', 0, '2020-01-15T00:00:00Z', '2020-01-21T00:00:00Z')
    ('n', 1, '2020-01-21T00:00:00Z', '2020-01-22T00:00:00Z')
    ('n', 2, '2020-01-22T00:00:00Z', None)

== S11d after the wall-clock tx, backfill at 2020-01-23
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-23T00:00:00Z')
   client: ERR at `COMMIT`: specified system-time must be later than current tx
   xt.txs: id=15859 committed=False system_time=2026-10-30T23:00:27.926855Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",14248,"~:system-time",["~#time

== S11e future SYSTEM_TIME 2026-10-31T23:00:28Z
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2026-10-31T23:00:28Z')
   client: OK
   xt.txs: id=15859 committed=False system_time=2026-10-30T23:00:27.926855Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",14248,"~:system-time",["~#time

== S11f plain wall-clock tx after a future-dated one
   BEGIN READ WRITE
   client: OK
   xt.txs: id=15859 committed=False system_time=2026-10-30T23:00:27.926855Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",14248,"~:system-time",["~#time

-- S11 rows visible now
   SELECT _id, _system_from FROM s11 ORDER BY _system_from
   cols: ['_id', '_system_from']
    ('n', '2020-01-22T00:00:00Z')
    ('m', '2026-10-30T23:00:27.866517Z')
   xt.txs: id=12660 committed=True system_time=2020-01-22T00:00:00Z 
   xt.txs: id=14248 committed=True system_time=2026-10-30T23:00:27.866517Z 
   xt.txs: id=15859 committed=False system_time=2026-10-30T23:00:27.926855Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",14248,"~:system-time",["~#time

######## S10. COMMIT ASYNC with an out-of-order SYSTEM_TIME

== S10a ASYNC, out of order (2020-01-02)
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-02T00:00:00Z')
   client: OK
   xt.txs: id=15859 committed=False system_time=2026-10-30T23:00:27.926855Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",14248,"~:system-time",["~#time

== S10b ASYNC, in order (2020-01-20)
   BEGIN READ WRITE WITH (SYSTEM_TIME = TIMESTAMP '2020-01-20T00:00:00Z')
   client: OK
   xt.txs: id=15859 committed=False system_time=2026-10-30T23:00:27.926855Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",14248,"~:system-time",["~#time

-- S10 rows (after awaiting)
   SELECT _id FROM s10
   ERR ProtocolViolation Errors planning SQL statement:
   xt.txs: id=14248 committed=True system_time=2026-10-30T23:00:27.866517Z 
   xt.txs: id=15859 committed=False system_time=2026-10-30T23:00:27.926855Z ["~#xt/error",["~:incorrect","~:invalid-system-time","specified system-time must be later than current tx",["^ ","~:latest-completed-tx",["~#xtdb/tx-key",["^ ","~:tx-id",14248,"~:system-time",["~#time
```

## Part 4 (`monotone.py 4`): follow-up probe on the part-2 node, right after part 2

Default reads hide rows and `xt.txs` entries whose system time is in the future; reading
with `FOR ALL SYSTEM_TIME FOR ALL VALID_TIME` shows them. These three queries were first run as
two ad-hoc probes on that node and then folded unchanged into `main4`.

```text
-- txs, every row
   SELECT _id, committed, system_time, (error IS NOT NULL) AS has_error FROM xt.txs FOR ALL SYSTEM_TIME FOR ALL VALID_TIME ORDER BY _id DESC LIMIT 6
   cols: ['_id', 'committed', 'system_time', 'has_error']
    (22082, False, '2026-10-31T23:00:28.000003Z', True)
    (20526, False, '2026-10-31T23:00:28.000002Z', True)
    (18971, True, '2026-10-31T23:00:28.000001Z', False)
    (17415, True, '2026-10-31T23:00:28Z', False)
    (15859, False, '2026-10-30T23:00:27.926855Z', True)
    (14248, True, '2026-10-30T23:00:27.866517Z', False)

-- s11 all system time
   SELECT _id, v, _system_from, _valid_from FROM s11 FOR ALL SYSTEM_TIME FOR ALL VALID_TIME ORDER BY _system_from
   cols: ['_id', 'v', '_system_from', '_valid_from']
    ('m', 1, '2020-01-21T00:00:00Z', '2020-01-21T00:00:00Z')
    ('m', 1, '2020-01-21T00:00:00Z', '2020-01-22T00:00:00Z')
    ('n', 1, '2020-01-21T00:00:00Z', '2020-01-21T00:00:00Z')
    ('n', 1, '2020-01-21T00:00:00Z', '2020-01-22T00:00:00Z')
    ('m', 2, '2020-01-22T00:00:00Z', '2020-01-22T00:00:00Z')
    ('n', 2, '2020-01-22T00:00:00Z', '2020-01-22T00:00:00Z')
    ('m', 0, '2026-10-30T23:00:27.866517Z', '2020-01-15T00:00:00Z')
    ('n', 0, '2026-10-30T23:00:27.866517Z', '2020-01-15T00:00:00Z')
    ('f', 1, '2026-10-31T23:00:28Z', '2026-10-31T23:00:28Z')
    ('after-future', 1, '2026-10-31T23:00:28.000001Z', '2026-10-31T23:00:28.000001Z')

-- s10 all
   SELECT _id, _system_from FROM s10 FOR ALL SYSTEM_TIME FOR ALL VALID_TIME
   ERR ProtocolViolation Errors planning SQL statement:
```
