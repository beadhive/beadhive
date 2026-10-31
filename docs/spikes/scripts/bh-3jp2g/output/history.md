# bh-3jp2g `history.py` output

Captured stdout of `../history.py` on 2026-10-30 (pure Python; no XTDB). Re-running gives the
same transactions, and the same ops per entity in the same order. Only the order of ops on
*different* entities inside one transaction follows Python set iteration (`PYTHONHASHSEED`), which
does not change what is loaded (checked by diffing two runs).

`preimage_drift_cases` rows are (bead, event, field, replay value length, bd pre-image value
length, equal ignoring whitespace).

```text
# history.py
{
  "window_start": "2026-08-07T07:25:10Z",
  "first_recorded": "2026-06-29T03:43:46Z",
  "last_recorded": "2026-10-30T21:32:38Z",
  "bead_versions": 24195,
  "beads_created_before_window": 2075,
  "beads_created_in_window": 7013,
  "beads_with_created_event": 6559,
  "beads_without_any_preimage": 7174,
  "closed_at_taken_from_bd_value": 3341,
  "closes_synthesized_from_snapshot_in_window": 2791,
  "closes_synthesized_from_snapshot_pre_window": 1392,
  "comment_rows_event": 15,
  "comment_rows_export": 676,
  "edge_delete_ops": 68,
  "edge_insert_ops": 1239,
  "edge_remove_unresolved_type_unknown": 28,
  "edge_rows_from_snapshot": 9102,
  "empty_updated_ignored_no_next_preimage": 8,
  "empty_updated_post_from_next_preimage": 309,
  "events_skipped_bead_not_in_export": 14,
  "events_used_claimed": 1257,
  "events_used_closed": 3356,
  "events_used_commented": 15,
  "events_used_created": 6559,
  "events_used_dependency_added": 1239,
  "events_used_dependency_removed": 88,
  "events_used_label_added": 15537,
  "events_used_label_removed": 2332,
  "events_used_lease_reclaimed": 20,
  "events_used_reopened": 18,
  "events_used_status_changed": 1405,
  "events_used_updated": 4876,
  "final_edge_mismatch_beads": 8,
  "final_label_mismatch_beads": 0,
  "label_add_when_held_noop": 2,
  "label_delete_ops": 2332,
  "label_insert_ops": 15535,
  "label_rows_from_snapshot": 6225,
  "ops_total": 59387,
  "preimage_reanchor_checks": 7196,
  "scalar_tie_groups": 136,
  "scalar_tie_groups_reordered_by_preimage_chain": 74,
  "transactions_distinct_recorded_at": 28402,
  "ops_by_kind_table": {
    "del:bead_edges": 68,
    "del:bead_labels": 2332,
    "ins:bead_comments": 691,
    "ins:bead_edges": 10341,
    "ins:bead_labels": 21760,
    "ins:beads": 24195
  },
  "preimage_drift_by_field": {
    "status": 1,
    "notes": 3
  },
  "preimage_drift_cases": [
    [
      "bh-merge-slot",
      "updated",
      "status",
      11,
      4,
      false
    ],
    [
      "bh-b5v4y",
      "updated",
      "notes",
      3532,
      3814,
      false
    ],
    [
      "bh-5sizy",
      "updated",
      "notes",
      2067,
      2339,
      false
    ],
    [
      "bh-n17d",
      "updated",
      "notes",
      348,
      1488,
      false
    ]
  ],
  "largest_tx_ops": 113,
  "witnesses_per_cut": {
    "2026-08-21T00:00:00Z": {
      "beads": 101,
      "scalar": 101,
      "labels": 5
    },
    "2026-09-18T00:00:00Z": {
      "beads": 91,
      "scalar": 91,
      "labels": 65
    },
    "2026-10-16T00:00:00Z": {
      "beads": 47,
      "scalar": 46,
      "labels": 31
    }
  },
  "beads_existing_per_cut": {
    "2026-08-21T00:00:00Z": 3381,
    "2026-09-18T00:00:00Z": 5763,
    "2026-10-16T00:00:00Z": 8419,
    "2026-07-20T00:00:00Z": 621
  },
  "sample_size": 60
}
```
