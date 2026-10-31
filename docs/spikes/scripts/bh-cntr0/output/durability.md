# bh-cntr0 `counts.py / crashwrite.py / crashverify.py` output

Digest = sha256 over the ordered scalar columns of each table, so equal digests mean equal
content. 2026-10-30.

## counts-before-kill.json

counts.py after the load, before the first kill -9.

```text
{"beads_a": [9088, "a6e79cda66e1e450"], "beads_b": [9088, "a6e79cda66e1e450"], "bead_labels": [21760, "1a436398732cb6c0"], "bead_edges": [10321, "4f24bd9b88d07527"], "bead_comments": [676, "a1fe5843e369adbf"], "bd_events": [36716, "23d198a25bb39114"], "query_all_s": 2.08}
```

## counts-after-kill9-1.json

counts.py after `docker kill -s KILL` and `docker start`; no block had been flushed, so log replay
only.

```text
{"beads_a": [9088, "a6e79cda66e1e450"], "beads_b": [9088, "a6e79cda66e1e450"], "bead_labels": [21760, "1a436398732cb6c0"], "bead_edges": [10321, "4f24bd9b88d07527"], "bead_comments": [676, "a1fe5843e369adbf"], "bd_events": [36716, "23d198a25bb39114"], "query_all_s": 2.27}
```

## counts-after-kill9-2.json

counts.py after a second kill -9, after `POST /system/finish-block` (blocks on disk).

```text
{"beads_a": [9088, "a6e79cda66e1e450"], "beads_b": [9088, "a6e79cda66e1e450"], "bead_labels": [21760, "1a436398732cb6c0"], "bead_edges": [10321, "4f24bd9b88d07527"], "bead_comments": [676, "a1fe5843e369adbf"], "bd_events": [36716, "23d198a25bb39114"], "query_all_s": 2.37}
```

## counts-restored-copy.json

counts.py on a fresh container started on a `cp -a` copy of the stopped node data dir.

```text
{"beads_a": [9088, "a6e79cda66e1e450"], "beads_b": [9088, "a6e79cda66e1e450"], "bead_labels": [21760, "1a436398732cb6c0"], "bead_edges": [10321, "4f24bd9b88d07527"], "bead_comments": [676, "a1fe5843e369adbf"], "bd_events": [36716, "23d198a25bb39114"], "query_all_s": 2.84}
```

## crashwrite.py and crashverify.py

The writer was killed with the server; the verifier ran after restart.

```text
writer stopped after acked batch 119: OperationalError: consuming input failed: server closed the connection unexpectedly
    This probably

last acked batch file: 119
last acked batch: 119; batches present: 120; max present: 120; partial batches: {}; acked-but-missing/incomplete: []
```
