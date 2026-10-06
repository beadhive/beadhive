# bd issue drafts (gastownhall/beads)

> **Tracked internally, not filed upstream.** ADR Decision 7
> (`docs/design/hive-writer-partitioning-adr.md`): these drafts stay in this tree. Fixes would
> make the fence conditions belts rather than the only brace; none is required. Evidence is
> pinned to Dolt 2.3.5 and the bd build exercised in the cited spike.

## bd-1: server-mode write commit omits trigger side-effect tables

*Evidence:* `docs/spikes/bh-vje85-in-data-epoch-fencing.md` E7;
`docs/spikes/bh-sieai-write-guard-and-nonprimary-writes.md` Evidence 4.

*Observed:* server-mode bd stages the tables it wrote by name
(`doltAddAndCommitInTx(ctx, tx, []string{"issues", "events"}, ...)`, `internal/storage/dolt/issues.go:238`
and siblings). A row a trigger writes to another table (`bh_write_mark`) is left unstaged, so
the head commit after `bd create` changes only `issues`. Embedded bd commits with
`DOLT_COMMIT('-Am')` and carries it.

*Expected:* server and embedded modes commit the same set of changes; stage triggered side
tables (or use `-Am`).

*Impact:* any trigger-maintained side table is lost from the write's own commit in server mode.

## bd-2: `bd vc merge` skips the pre-merge commit

*Evidence:* vje85 E7, E8 (`cmd/bd/vc.go:76-128`).

*Observed:* pull and sync call `commitBeforePull` (`store.go:3238`) and federation commits
before syncing; `bd vc merge` stages nothing first. In server mode a stale writer's uncommitted
mark therefore reaches `bd vc merge origin/main` without being committed, and
`Successfully merged` is printed; the following `bd dolt push` lands the stale write.

*Expected:* the same pre-merge commit as the other merge paths, or a refusal on a dirty working
set.

## bd-3: `MergeWithStrategy` skips the FK settle gate

*Evidence:* vje85 E8 (`versioncontrolops/mergesettle.go:293-301`).

*Observed:* `MergeWithStrategy` sets `dolt_force_transaction_commit` and returns before its FK
settle gate when a merge has constraint violations but no conflicts. Only an incidental
recompute commit (`is_blocked recompute failed ... constraint violations`) stops the bad merge
today.

*Expected:* the settle gate runs whether or not the merge reported conflicts.

## bd-4: federation sync exits 0 on a failed merge

*Evidence:* vje85 E9.

*Observed:* `bd federation sync --peer hub --strategy ours --json` exits 0 with
`"Merged": false` and `"Error": {}` (the error is serialised as an empty object) when the merge
is refused (embedded: `merge failed: ... constraint violations, transaction rolled back`).

*Expected:* a non-zero exit and a populated error string.

*bh side:* `BdEngine.sync_state` must treat `Merged: false`, a non-null `Error` or a failure
line as failure regardless (tracked separately).
