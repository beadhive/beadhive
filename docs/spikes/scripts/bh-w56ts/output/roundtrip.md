# bh-w56ts `roundtrip.py` output

Captured stdout of `../roundtrip.py` against XTDB 2.2.0 on 2026-10-30 (setup: the spike doc).

```text

== A/transit: rows read 9088
issue_type | beads | field-equal
event | 3176 | 3176
gate | 2241 | 2241
task | 1217 | 1217
feature | 792 | 792
bug | 750 | 750
epic | 386 | 386
molecule | 314 | 314
chore | 188 | 188
spike | 13 | 13
decision | 11 | 11

== A/json: rows read 9088
issue_type | beads | field-equal
event | 3176 | 3126
gate | 2241 | 2241
task | 1217 | 43
feature | 792 | 12
bug | 750 | 35
epic | 386 | 3
molecule | 314 | 311
chore | 188 | 3
spike | 13 | 0
decision | 11 | 0
  divergence bug comments:value: 1
  divergence bug dependencies:value: 14
  divergence bug labels:value: 715
  divergence chore dependencies:value: 4
  divergence chore labels:value: 185
  divergence decision labels:value: 11
  divergence epic comments:value: 1
  divergence epic dependencies:value: 2
  divergence epic labels:value: 383
  divergence event dependencies:value: 50
  divergence feature dependencies:value: 23
  divergence feature labels:value: 780
  divergence molecule dependencies:value: 3
  divergence spike dependencies:value: 1
  divergence spike labels:value: 12
  divergence task comments:value: 4
  divergence task dependencies:value: 41
  divergence task labels:value: 1174
  example labels:value: bh-24ay9
    export=["complexity:COMPLEX", "component:intake", "kickoff:approved", "org:beadhive", "provider:github", "repo:beadhive", "wave:1-issue-sync"]
    xtdb  =["org:beadhive", "component:intake", "complexity:COMPLEX", "kickoff:approved", "wave:1-issue-sync", "repo:beadhive", "provider:github"]
  example dependencies:value: bh-vfrem.8
    export=[{"issue_id": "bh-vfrem.8", "depends_on_id": "bh-v07xt", "type": "blocks", "created_at": "2026-10-22T04:44:23Z", "created_by": "dev/claude", "metadata": "{}"}, 
    xtdb  =[{"issue_id": "bh-vfrem.8", "depends_on_id": "bh-v07xt", "type": "blocks", "created_at": "2026-10-22T04:44:23Z", "created_by": "dev/claude", "metadata": "{}"}, 
  example comments:value: bh-16347
    export=[{"id": "d80c5171-887b-5e99-a9a9-fabcc046647c", "issue_id": "bh-16347", "author": "Brian Cripe", "text": "bh:validation-override {\"actor\":\"Brian Cripe\",\"be
    xtdb  =[{"id": "d80c5171-887b-5e99-a9a9-fabcc046647c", "issue_id": "bh-16347", "author": "Brian Cripe", "text": "bh:validation-override {\"actor\":\"Brian Cripe\",\"be

== B/transit (reassembled): rows read 9088
issue_type | beads | field-equal
event | 3176 | 3176
gate | 2241 | 2241
task | 1217 | 1217
feature | 792 | 792
bug | 750 | 750
epic | 386 | 386
molecule | 314 | 314
chore | 188 | 188
spike | 13 | 13
decision | 11 | 11

== bd_events: rows read 36716, divergences none, null fields read back as absent: 52369
```
