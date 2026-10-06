# Bounded signed-mode inbox (0.22.x patch)

0.22.0 shipped `hq.sql.liveness: signed` with the trust delta "the inbox grows: each
heartbeat adds about one row to the frame's inbox table, and there is no pruning yet". This
patch closes that delta (bh-ce886). It changes no wire contract, schema or frame grant.

## What ships

- **`bh hq authority prune-inbox --frame <frame-id> [--confirm]`.** An operator verb that
  deletes expired heartbeat rows from the frame's inbox tables. It is a dry run without
  `--confirm`. It runs with the `authority_writer` credential, directly or through
  `BH_HQ_OPERATOR_SETTINGS`, and is refused on a frame.
- **A configurable retention margin.** `hq.sql.inbox_retention_s` in the operator settings
  file, then `BH_HQ_INBOX_RETENTION`, then a 1 hour default. It is opt-in: nothing prunes
  unless the operator runs the verb.

## Trust delta

- **The inbox is now bounded.** With an honest sender a frame's inbox holds at most one
  heartbeat per interval inside lease + 30 s skew + the retention margin, plus the newest
  verified beat. A frame can still INSERT junk rows into its own inbox, as before.
- **No beat a reader still needs is deleted.** A heartbeat row is pruned only once its
  signed `renewTime` + `leaseDurationSeconds` + 30 s skew + the margin has passed on the
  operator's clock. A 0.22.x signed reader and the receiver both refuse a beat at or past
  its lease, within that skew, so a pruned row could no longer make the frame eligible. The
  newest verified beat is always kept, because the sender derives its next `seq` from it.
  `registration` and `hive_lease` rows are never pruned.
- **Frames gain no DELETE.** The operator grants `SELECT, DELETE` on each inbox table to the
  `authority_writer` account only. Frame principals keep `SELECT, INSERT, UPDATE`.

## Rollout

1. Install this patch where the operator runs `bh hq authority`. Frames need no change.
2. Grant the operator account delete on each inbox:
   `GRANT SELECT, DELETE ON <runtime-db>.hq_live_inbox_<principal>_<epoch> TO <authority_writer>;`
3. Run `bh hq authority prune-inbox --frame <frame-id>` to see the counts, then again with
   `--confirm`. Rerun until `"complete": true` if the first backlog is large.

Details: [HQ — signed-mode inbox retention](../HQ.md#signed-mode-inbox-retention).
