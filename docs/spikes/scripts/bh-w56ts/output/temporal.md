# bh-w56ts `temporal.py` output

Captured stdout of `../temporal.py` against XTDB 2.2.0 on 2026-10-30 (setup: the spike doc).

```text

-- (b) beads that gained and lost a label (closed label periods)
   SELECT bead_id, COUNT(*) AS closed FROM bead_labels FOR ALL VALID_TIME WHERE _valid_to IS NOT NULL GROUP BY bead_id ORDER BY closed DESC, bead_id
   cols: ['bead_id', 'closed']
    ('bh-q0lol.5', 27)
    ('bh-q0lol.8', 21)
    ('bh-id9pp.3', 17)
    ... 1361 rows

-- (b) every label period of bh-q0lol.5
   SELECT label, _valid_from, _valid_to FROM bead_labels FOR ALL VALID_TIME WHERE bead_id = %s ORDER BY label, _valid_from
   cols: ['label', '_valid_from', '_valid_to']
    ('complexity:COMPLEX', '2026-08-24T20:56:02Z', None)
    ('component:runtime', '2026-08-24T20:56:02Z', None)
    ('org:beadhive', '2026-08-24T20:56:02Z', None)
    ('provider:github', '2026-08-24T20:56:02Z', None)
    ('repo:beadhive', '2026-08-24T20:56:02Z', None)
    ('review:changes-requested', '2026-09-02T23:55:55Z', '2026-09-03T00:10:48Z')
    ('review:changes-requested', '2026-09-03T00:16:50Z', '2026-09-03T00:27:27Z')
    ('review:changes-requested', '2026-09-03T00:37:36Z', '2026-09-03T00:51:59Z')
    ('review:changes-requested', '2026-09-03T00:59:58Z', '2026-09-03T01:14:50Z')
    ('review:changes-requested', '2026-09-03T01:23:43Z', '2026-09-03T01:40:13Z')
    ('review:changes-requested', '2026-09-03T01:49:38Z', '2026-09-03T02:05:34Z')
    ('review:changes-requested', '2026-09-03T02:12:14Z', '2026-09-03T02:22:40Z')
    ('review:changes-requested', '2026-09-03T02:34:06Z', '2026-09-03T02:59:34Z')
    ('review:changes-requested', '2026-09-03T03:06:38Z', '2026-09-03T03:17:49Z')
    ('review:changes-requested', '2026-09-03T03:27:56Z', '2026-09-03T03:38:07Z')
    ('review:changes-requested', '2026-09-03T03:46:27Z', '2026-09-03T03:56:55Z')
    ('review:changes-requested', '2026-09-03T04:05:12Z', '2026-09-03T04:21:06Z')
    ('review:changes-requested', '2026-09-03T04:28:56Z', '2026-09-03T04:42:53Z')
    ('review:pending', '2026-09-02T23:49:59Z', '2026-09-02T23:55:55Z')
    ('review:pending', '2026-09-03T00:10:48Z', '2026-09-03T00:16:50Z')
    ('review:pending', '2026-09-03T00:27:27Z', '2026-09-03T00:37:36Z')
    ('review:pending', '2026-09-03T00:51:59Z', '2026-09-03T00:59:58Z')
    ('review:pending', '2026-09-03T01:14:50Z', '2026-09-03T01:23:43Z')
    ('review:pending', '2026-09-03T01:40:13Z', '2026-09-03T01:49:38Z')
    ('review:pending', '2026-09-03T02:05:34Z', '2026-09-03T02:12:14Z')
    ('review:pending', '2026-09-03T02:22:40Z', '2026-09-03T02:34:06Z')
    ('review:pending', '2026-09-03T02:59:34Z', '2026-09-03T03:06:38Z')
    ('review:pending', '2026-09-03T03:17:49Z', '2026-09-03T03:27:56Z')
    ('review:pending', '2026-09-03T03:38:07Z', '2026-09-03T03:46:27Z')
    ('review:pending', '2026-09-03T03:56:55Z', '2026-09-03T04:05:12Z')
    ('review:pending', '2026-09-03T04:21:06Z', '2026-09-03T04:28:56Z')
    ('review:pending', '2026-09-03T04:42:53Z', '2026-09-03T04:50:23Z')
    ('size:l', '2026-08-24T20:56:02Z', None)

-- (b) labels of bh-q0lol.5 as of 2026-09-02T23:55:54+00:00 (param) vs now
   SELECT label FROM bead_labels FOR VALID_TIME AS OF %s WHERE bead_id = %s ORDER BY label
   cols: ['label']
    ('complexity:COMPLEX',)
    ('component:runtime',)
    ('org:beadhive',)
    ('provider:github',)
    ('repo:beadhive',)
    ('review:pending',)
    ('size:l',)

-- (b) labels of bh-q0lol.5 now
   SELECT label FROM bead_labels WHERE bead_id = %s ORDER BY label
   cols: ['label']
    ('complexity:COMPLEX',)
    ('component:runtime',)
    ('org:beadhive',)
    ('provider:github',)
    ('repo:beadhive',)
    ('size:l',)

-- (b) corpus: how long each bead held review:pending (top 5)
   SELECT bead_id, _valid_from, _valid_to, (_valid_to - _valid_from) AS held FROM bead_labels FOR ALL VALID_TIME WHERE label = 'review:pending' AND _valid_to IS NOT NULL ORDER BY held DESC LIMIT 5
   cols: ['bead_id', '_valid_from', '_valid_to', 'held']
    ('bh-jksq.1', '2026-08-22T08:46:07Z', '2026-08-24T05:46:34Z', 'PT45H27S')
    ('bh-6eu2c.2', '2026-08-22T18:48:27Z', '2026-08-24T05:48:28Z', 'PT35H1S')
    ('bh-airg8', '2026-10-16T18:19:57Z', '2026-10-17T18:36:13Z', 'PT24H16M16S')
    ('bh-lniwf.6', '2026-10-16T18:31:39Z', '2026-10-17T18:46:33Z', 'PT24H14M54S')
    ('bh-gsex3.1', '2026-08-21T18:42:41Z', '2026-08-22T18:39:09Z', 'PT23H56M28S')

-- (b) corpus: beads carrying review:pending at 2026-10-01T00:00Z
   SELECT COUNT(*) FROM bead_labels FOR VALID_TIME AS OF TIMESTAMP '2026-10-01T00:00:00Z' WHERE label = 'review:pending'
   cols: ['_column_1']
    (155,)

-- (b) removed edges with their valid periods
   SELECT from_id, type, to_id, _valid_from, _valid_to FROM bead_edges FOR ALL VALID_TIME WHERE _valid_to IS NOT NULL ORDER BY _valid_to LIMIT 5
   cols: ['from_id', 'type', 'to_id', '_valid_from', '_valid_to']
    ('bh-c6dk.5', 'blocks', 'bh-bh2h.2.2', '2026-08-10T15:26:49Z', '2026-08-10T15:59:57Z')
    ('bh-e7rju', 'blocks', 'bh-d75bi', '2026-08-11T22:57:49Z', '2026-08-11T22:58:57Z')
    ('bh-7ig9k', 'blocks', 'bh-d75bi', '2026-08-11T22:57:53Z', '2026-08-11T22:59:03Z')
    ('bh-htqbn', 'blocks', 'bh-d75bi', '2026-08-11T22:57:55Z', '2026-08-11T22:59:07Z')
    ('bh-uf6jt', 'blocks', 'obs-vaqj', '2026-08-15T02:08:34Z', '2026-08-15T02:09:08Z')

-- (a) whole-document versions: every label change AND the title change rewrite the doc
   SELECT title, labels, _valid_from, _valid_to FROM temporal_a_fb6250 FOR ALL VALID_TIME ORDER BY _valid_from
   cols: ['title', 'labels', '_valid_from', '_valid_to']
    ('T', ['a'], '2026-08-01T00:00:00Z', '2026-08-02T00:00:00Z')
    ('T', ['review:pending', 'a'], '2026-08-02T00:00:00Z', '2026-08-03T00:00:00Z')
    ('T2', ['review:pending', 'a'], '2026-08-03T00:00:00Z', '2026-08-04T00:00:00Z')
    ('T2', ['a'], '2026-08-04T00:00:00Z', None)

-- (a) per-label period needs UNNEST over versions + merge of adjacent fragments
   SELECT l.label, MIN(v._valid_from) AS vf, MAX(COALESCE(v._valid_to, TIMESTAMP '9999-12-31T00:00:00Z')) AS vt, COUNT(*) AS fragments FROM temporal_a_fb6250 FOR ALL VALID_TIME AS v, UNNEST(v.labels) AS l(label) GROUP BY l.label ORDER BY l.label
   cols: ['label', 'vf', 'vt', 'fragments']
    ('a', '2026-08-01T00:00:00Z', '9999-12-31T00:00:00Z', 4)
    ('review:pending', '2026-08-02T00:00:00Z', '2026-08-04T00:00:00Z', 2)
   (MIN/MAX is only right when the label was held once; re-adds need gaps-and-islands)

-- INSERT with a later _valid_from: does it close the earlier row?
   SELECT v, _valid_from, _valid_to FROM temporal_ins_fb6250 FOR ALL VALID_TIME ORDER BY _valid_from
   cols: ['v', '_valid_from', '_valid_to']
    (1, '2026-08-01T00:00:00Z', '2026-08-05T00:00:00Z')
    (2, '2026-08-05T00:00:00Z', None)

-- then INSERT with an EARLIER _valid_from and no _valid_to (out-of-order backfill)
   SELECT v, _valid_from, _valid_to FROM temporal_ins_fb6250 FOR ALL VALID_TIME ORDER BY _valid_from
   cols: ['v', '_valid_from', '_valid_to']
    (0, '2026-07-01T00:00:00Z', None)

-- ... system-time view of the same (what the clobbered rows look like)
   SELECT v, _valid_from, _valid_to, _system_from, _system_to FROM temporal_ins_fb6250 FOR ALL VALID_TIME FOR ALL SYSTEM_TIME ORDER BY _system_from, _valid_from
   cols: ['v', '_valid_from', '_valid_to', '_system_from', '_system_to']
    (1, '2026-08-01T00:00:00Z', '2026-08-05T00:00:00Z', '2026-10-30T21:56:36Z', '2026-10-30T21:56:36Z')
    (1, '2026-08-05T00:00:00Z', None, '2026-10-30T21:56:36Z', '2026-10-30T21:56:36Z')
    (2, '2026-08-05T00:00:00Z', None, '2026-10-30T21:56:36Z', '2026-10-30T21:56:36Z')
    (0, '2026-07-01T00:00:00Z', None, '2026-10-30T21:56:36Z', None)

-- out-of-order backfill WITH an explicit _valid_to (bounded)
   SELECT v, _valid_from, _valid_to FROM temporal_ins2_fb6250 FOR ALL VALID_TIME ORDER BY _valid_from
   cols: ['v', '_valid_from', '_valid_to']
    (0, '2026-07-01T00:00:00Z', '2026-08-01T00:00:00Z')
    (1, '2026-08-01T00:00:00Z', '2026-08-05T00:00:00Z')
    (2, '2026-08-05T00:00:00Z', None)

-- PATCH with assignee: null -> OK

-- after PATCH {assignee: null, status: closed}
   SELECT _id, assignee, status FROM temporal_patch_fb6250
   cols: ['_id', 'assignee', 'status']
    ('p', 'dev/x', 'closed')

-- after UPDATE SET assignee = NULL
   SELECT _id, assignee, status FROM temporal_patch_fb6250
   cols: ['_id', 'assignee', 'status']
    ('p', None, 'closed')

-- history
   SELECT assignee, status, _valid_from, _valid_to FROM temporal_patch_fb6250 FOR ALL VALID_TIME ORDER BY _valid_from
   cols: ['assignee', 'status', '_valid_from', '_valid_to']
    ('dev/x', 'open', '2026-10-30T21:56:36Z', '2026-10-30T21:56:36Z')
    ('dev/x', 'closed', '2026-10-30T21:56:36Z', '2026-10-30T21:56:36Z')
    (None, 'closed', '2026-10-30T21:56:36Z', None)

-- UPDATE ... FOR PORTION OF VALID_TIME on a row with no history there
   SELECT status, _valid_from, _valid_to FROM temporal_patch_fb6250 FOR ALL VALID_TIME ORDER BY _valid_from
   cols: ['status', '_valid_from', '_valid_to']
    ('open', '2026-10-30T21:56:36Z', '2026-10-30T21:56:36Z')
    ('closed', '2026-10-30T21:56:36Z', '2026-10-30T21:56:36Z')
    ('closed', '2026-10-30T21:56:36Z', None)

-- (b) dangling edge targets: anti-join, current valid time
   SELECT e.from_id, e.type, e.to_id FROM bead_edges AS e WHERE NOT EXISTS (SELECT 1 FROM beads_b AS b WHERE b._id = e.to_id) ORDER BY e.from_id
   cols: ['from_id', 'type', 'to_id']
    ('bh-f62lu.1', 'blocks', 'external:infra:gateway-read-development-deployed-v1')
    ('bh-gjeve.1', 'blocks', 'external:beadhive-ui:gateway-read-demo-catalog-v1')
    ('bh-gjeve.2', 'blocks', 'external:infra:gateway-read-development-deployed-v1')
    ('bh-ku9n9.12', 'blocks', 'discovered-from:bh-ku9n9.1')
    ('bh-ku9n9.14', 'blocks', 'discovered-from:bh-ku9n9.2')
    ('bh-ti7ws', 'blocks', 'discovered-from:bh-b5v4y')
    ('bh-tqarf.1', 'blocks', 'external:beadhive-ui:gateway-read-demo-catalog-v1')

-- (a) dangling targets from nested deps: UNNEST then anti-join
   SELECT b._id, d.dep FROM beads_a AS b, UNNEST(b.dependencies) AS d(dep) WHERE NOT EXISTS (SELECT 1 FROM beads_a AS t WHERE t._id = (d.dep).depends_on_id) ORDER BY b._id
   cols: ['_id', 'dep']
    ('bh-f62lu.1', {'issue_id': 'bh-f62lu.1', 'depends_on_id': 'external:infra:gateway-read-development-deployed-v1', 'type': 'blocks', 'created_at': '2026-08-27T16:41:30Z', 'created_by': 'Brian Cripe', 'metadata': '{}'})
    ('bh-gjeve.1', {'issue_id': 'bh-gjeve.1', 'depends_on_id': 'external:beadhive-ui:gateway-read-demo-catalog-v1', 'type': 'blocks', 'created_at': '2026-08-27T16:04:33Z', 'created_by': 'Brian Cripe', 'metadata': '{}'})
    ('bh-gjeve.2', {'issue_id': 'bh-gjeve.2', 'depends_on_id': 'external:infra:gateway-read-development-deployed-v1', 'type': 'blocks', 'created_at': '2026-08-27T16:04:33Z', 'created_by': 'Brian Cripe', 'metadata': '{}'})
    ... 7 rows

-- (b) edges whose source is missing
   SELECT COUNT(*) FROM bead_edges AS e WHERE NOT EXISTS (SELECT 1 FROM beads_b AS b WHERE b._id = e.from_id)
   cols: ['_column_1']
    (0,)

-- (b) label rows whose bead is missing
   SELECT COUNT(*) FROM bead_labels AS l WHERE NOT EXISTS (SELECT 1 FROM beads_b AS b WHERE b._id = l.bead_id)
   cols: ['_column_1']
    (0,)
```
