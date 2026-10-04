# Spike `bh-vje85` — does in-data epoch fencing fence a stale writer under real bd?

**Bead:** `bh-vje85` · **Seat:** `dev/fencing`
**Type:** spike (test-only code, no product code)
**Parent:** `bh-qlgmm` (hive writer partitioning spike molecule)
**Feeds decision on:** `bh-pr889` (the ADR); inputs to `bh-jbb6r` (end-to-end run) and
`bh-32379` (migration)
**Proposal:** [hive-writer-partitioning-proposal.md](../design/hive-writer-partitioning-proposal.md)
§2 and "Pre-kickoff probe evidence"

## Question

Does committing `{frame, epoch}` into the hive's own Dolt `main` fence a stale writer on every
push path, under real bd 1.3, without the CAS-to-push race that `host_fence.py` documents? The
fence has three parts:

- the remote's non-fast-forward rejection;
- epoch retirement through a foreign key: every guarded write stamps a `bh_write_mark` row whose
  epoch must be live, and adopt retires the old epoch;
- a local guard trigger that refuses `main` writes on a node that is not the writer.

The push paths are a managed push, raw `bd dolt push`, bd auto-push, `bd sync`, and
`bd dolt pull` followed by a push. The remote types are `git+file://` (the `git+ssh` shape) and
`file://`.

The spike also asks what each bd force path does to the fence, whether `bh_writer` can be
reverted by `bd sync`, `bd dolt pull` or `hive_sync` with `--strategy ours|theirs`, and what
happens to writes a partitioned old writer made before it learned of the new epoch.

It does not decide the design of the write guard: the meaning of a NULL `active_branch()`, which
tables the guard covers, or how it is provisioned. That is `bh-sieai`. The guard here is the
proposal's, used only to stamp marks and to refuse a stale node's next write. The spike also
does not decide the placement authority (`bh-cvk70`) or receiver removal (`bh-wtsrc`). It
changes no product code.

## Method

Run on 2026-10-04 on `beadhive-factory` with bd `1.3.0 (f45b249ce)`, Dolt CLI and sql-server
`2.3.5`, git `2.55.0`, and bd's linked Dolt `v0.40.5-0.20260715172757-a6690826d767` for embedded
frames. Every Dolt process ran with a private `HOME` and `DOLT_ROOT_PATH`.
`~/.dolt/config_global.json` had the same checksum before and after the work.

1. **Fixture.** The tests use the `bh-eybn7` fixture (`Cluster`, `HQ`, `Frame`, `Recorder`)
   unchanged. A cluster is three frames on one `git+file://` remote:
   - `a`: bd embedded, which writes through bd's linked Dolt;
   - `b`: bd shared-server, with its own `dolt sql-server`;
   - `c`: the Dolt CLI.

   Fencing is judged on remote `main` and on table contents, never on `refs/dolt/data`
   (`bh-eybn7` Evidence 6).
2. **Fence model.** The model is test-only code in
   [`tests/harness/epoch_fence.py`](../../tests/harness/epoch_fence.py). It is the proposal's
   shape with the changes that `bh-cvk70` and this spike found necessary:
   - `bh_writer`: one row `{frame, epoch, revision}`. Every adopt rewrites `revision`.
   - `bh_epoch_live`: one row (`id` primary key, `epoch` UNIQUE). Because it is a single row,
     two adopts conflict on the epoch itself (`bh-cvk70` E12).
   - `bh_write_mark (id, epoch)`: `epoch` is a foreign key to `bh_epoch_live(epoch)`.
   - `bh_local_ident`: the node's identity. It is `dolt_ignore`d.
   - Guard triggers `BEFORE INSERT` and `BEFORE UPDATE` on `issues`. This is a versioned table,
     because `events` and `wisp_*` are dolt-ignored. The trigger refuses a `main` write unless
     `bh_writer.frame` is this node, then stamps a mark with `INSERT … SELECT`.
   - Monotonic triggers on `bh_writer` and `bh_epoch_live`, added by this spike (E10).
   - **Adopt step 2**, one commit: name the writer, delete marks below the new epoch, move the
     live epoch. With this spike's addition, it also inserts an `adopt-<epoch>` sentinel mark
     (E8). Adopt is placement-first and idempotent, as in `bh-cvk70` Recommendation 3.
   - **Orphan diversion:** fetch, compare epochs, push to `frame/<id>/orphan`, then reset.
   - **The new writer's deliberate orphan merge**, which re-stamps the orphan's marks.
   - **An audit** that reads only remote `main` and HQ.
3. **Tests.** [`tests/test_epoch_fencing_int.py`](../../tests/test_epoch_fencing_int.py) holds
   11 tests, marked `integration` and `dolt_server`. They cover proposal scenarios 1–6, the bd
   force paths, mark growth and pruning, and the `file://` CAS. Each step runs inside
   `Recorder.step`, so the trace records the remote head, remote `main`, the fence rows and each
   frame's state before and after. With `BH_SPIKE_TRACE_DIR` set, the test also keeps every
   command's exit code and output tail. The evidence below quotes those traces.

   ```sh
   uv run pytest tests/test_epoch_fencing_int.py -n 2 -q   # 11 passed, ~8.5 min wall
   ```

4. **Exploration first.** I drove each path by hand before writing the tests. Those throwaway
   scratchpad drivers are not committed, and the tests pin what they found. Where a result
   surprised me, I read bd's source at `f45b249ce` and Dolt's at the module-cache version. The
   file:line references below point there.

## Evidence

### The fence holds on every default path

**E1. Scenario 1: planned handoff, old writer idle**
(`test_s1_planned_handoff_fences_the_idle_old_writer`; old writer bd-embedded, then
bd-server).

- The adopter's bump lands: remote `bh_writer = (c, e+1)`, live `[e+1]`, and the old epoch's
  marks are gone. The old writer's published write stays on `main`.
- The old writer's next push, before it has seen the bump, fails with
  `! [rejected] main -> main (non-fast-forward)`. Remote `main` is unchanged.
- Once the old writer has the bump, its local write fails in SQL with
  `Error 1644 (45000): bh: not the writer for main`. That holds in bd embedded, where the
  trigger fires inside bd's linked Dolt, and in bd server.

**E2. Scenario 2: handoff while the old writer is mid-push**
(`test_s2_…`; barrier at the push CAS, both orders, both bd modes).

- **Bump first.** The adopter lands while the old writer is parked before its CAS. The released
  push is `non-fast-forward` and the title never reaches `main`.
- **Write first.** The parked push is released from inside the adopter's step 2 and lands. The
  adopter's push is then rejected. Adopt loops, restarts from the new head and lands: the
  frame's gate log shows two or more `cas` pushes. The write sits *below* the bump, and its mark
  was retired.
- Neither order produced a write after the bump.

**E3. Scenario 3: partition, failover, rejoin** (`test_s3_…`; both bd modes).

- While partitioned, the old writer commits two writes locally; its push fails at the gate.
  The director re-places to `c` at HQ, and `c` adopts.
- After healing, raw `bd sync` on the old writer is refused and remote `main` is unchanged.
  Embedded bd reports `pull merge left constraint violations bd cannot auto-repair`. Server-mode
  bd reports Dolt's `CONSTRAINT VIOLATION (content): Merge created constraint violation in
  bh_write_mark`.
- The managed path then fetches without merging, sees `bh_writer.epoch` above the epoch it
  holds, and pushes local `main` to `frame/<id>/orphan`. Both unpublished titles are on that
  branch and absent from `main`. It then resets to the remote, and its next write is refused
  by the guard.
- `c` merges the orphan on purpose, in one SQL session:
  `SET @@dolt_force_transaction_commit = 1`, `DOLT_MERGE`, re-stamp every mark whose epoch is
  not live to the live epoch, delete the `dolt_constraint_violations_bh_write_mark` rows, then
  commit. The trace reports `restamped = 2` for the embedded writer and `restamped = 3` for the
  server writer. The extra mark belongs to the published write: bd never committed it (E7), so
  it first reached history in the pre-diversion commit.
- After the merge: no violations, no open merge, the titles are on `main`, every mark is at the
  live epoch, and the audit is clean.
- With the fence triggers installed, writing the re-stamp as
  `UPDATE … SET epoch = (SELECT …) WHERE epoch NOT IN (SELECT …)` fails on Dolt 2.3.5 with
  `unable to find field with index 5 in row of 4 columns` (the trigger-analyzer bug class).
  Assigning to a variable first works.

**E4. Scenario 4: racing adopters** (`test_s4_…`).

- **HQ placement.** Two threads CAS HQ placement from the same epoch, and exactly one wins. The
  other gets `stale placement`.
- **Loser reached step 2.** Frame `a` won epoch `e` and passed its HQ check. The director then
  failed over to `b` at `e+1`, and `b` landed first. `a`'s push is non-fast-forward; its retry
  reads `b@e+1 ≥ e` from the data and returns `abandoned: superseded in data`. `a`'s next write
  is refused by the guard.
- **Reverse order.** `a`'s stale bump lands first, and `a` writes once at that epoch. `b`'s push
  is rejected; `b` retries on top and lands `b@e+1`, which retires `a`'s marks. `a`'s next write
  is fenced: `bd dolt pull` hits the settle refusal and the push is non-fast-forward.

**E5. Scenario 5: a stale writer on every default bd path**
(`test_s5_stale_writer_is_refused_on_every_default_bd_path`). Each row starts from a fresh
stale writer holding one unpublished write. In every row, remote `main` was byte-identical
before and after, and the stale title never landed.

| Path | bd embedded (bd's linked Dolt) | bd server (Dolt 2.3.5 sql-server) |
|---|---|---|
| `bd dolt push` | `! [rejected] main -> main (non-fast-forward)` | same, via `dolt push failed` |
| `bd dolt pull` then `bd dolt push` | pull: `pull merge left constraint violations bd cannot auto-repair` (aborted, working set restored); push: non-fast-forward | pull: `CONSTRAINT VIOLATION (content) … bh_write_mark`, merge left open in the server (E6); push: non-fast-forward |
| `bd sync` | `sync failed: pull: … bd cannot auto-repair` | `sync failed: pull: dolt pull failed: … CONSTRAINT VIOLATION` |
| auto-push (`BD_DOLT_AUTO_PUSH=true bd create`) | create succeeds; `Warning: dolt auto-push failed: … (non-fast-forward)` | same |
| raw Dolt (CLI on the store, or `CALL DOLT_PULL`) | `dolt pull`: CONSTRAINT VIOLATION; `dolt commit`: `the table(s) bh_write_mark have constraint violations`; push rejected | `CALL DOLT_PULL`: `cannot merge with uncommitted changes`; after `bd dolt commit`, the pull is still refused; push rejected |

A Dolt CLI frame as the stale writer behaves like the raw-Dolt row: pull hits the CONSTRAINT
VIOLATION and the push is rejected. `bh-cvk70` E12 measured the same on `file://`.

None of the default paths force. Auto-push is one plain `Push`
(`cmd/bd/dolt_autopush.go:84-193`), and `bd sync` pushes with `force=false`
(`cmd/bd/sync.go:838`). `dolt_force_transaction_commit` and `dolt_allow_commit_conflicts` are
set only in merge-settle code. Every observed rejection was an ordinary non-fast-forward.

**E6. Server mode takes the weaker pull route, and it fails closed.** For a git-protocol remote,
bd's server mode shells out to `dolt pull` (`internal/storage/dolt/store.go:4076`, routed at
`:4492-4498`). `finishCLIPull` rolls back only its own transaction. The half-done merge stays in
the server's working set: `is_merging = 1`, `bh_write_mark` violated, and `bh_writer` already
naming the new writer. In that state:

- `bd create` and `bd update` fail with `bh: not the writer for main`, because the guard reads
  the merged `bh_writer`.
- `bd label add` and `bd comments add` touch tables the guard does not cover. They still fail
  with `Committing this transaction resulted in a working set with constraint violations,
  transaction rolled back`.
- `bd dolt commit` fails: `the table(s) bh_write_mark have constraint violations`.
- A second `bd dolt pull` or `bd sync` fails while committing pending changes, on the same
  violation.
- The push stays non-fast-forward.

The frame is wedged, but it cannot publish anything. Recovery is the managed path in E3:
divert, then reset.

### The holes, and the guards that close them

**E7. bd's server-mode write commit does not carry the mark.** Server-mode bd stages the
tables it wrote by name: `doltAddAndCommitInTx(ctx, tx, []string{"issues", "events"}, …)`
(`internal/storage/dolt/issues.go:238` and its siblings). The trigger's `bh_write_mark` row is
left unstaged.

- In the test, the head commit after a server-mode `bd create` changes only `issues`. In
  embedded mode it changes `issues` and `bh_write_mark`, because embedded bd commits with
  `DOLT_COMMIT('-Am')` (`embeddeddolt/version_control.go:135`).
- Every default merge path stages the working set before it merges, so the mark still reaches
  the merge. This is `commitBeforePull` for pull and sync (`store.go:3238`) and the pre-sync
  commit for federation. That is why E5 holds in server mode.
- `bd vc merge` has no such step (`cmd/bd/vc.go:76-128`).

**E8. With the proposal's bump, plain `bd vc merge` lands a stale write in server mode. A
sentinel mark closes it** (`test_s5_server_mode_mark_is_not_in_the_write_commit_…`).

- **The hole.** The stale bd-server frame fetched, then ran `bd vc merge origin/main` with no
  flags. It printed `Successfully merged origin/main`, and the next `bd dolt push` printed
  `Push complete.` The stale title is on remote `main` after the bump.
  - Remote `main` carries a mark at the retired epoch, so the audit (E12) flags it.
  - Embedded bd, given the same command, refuses with `Committing this transaction resulted in
    a working set with constraint violations`.
- **The guard.** Adopt also inserts an `adopt-<epoch>` mark. Every bump then changes
  `bh_write_mark`, so Dolt refuses to merge it over the stale frame's uncommitted mark:
  `error: local changes would be stomped by merge: bh_write_mark Please commit your changes
  before you merge.` Nothing lands. The sentinel is retired by the next adopt like any other
  mark. The same sentinel also closes the `--strategy` reverts in E11, because the losing
  side's sentinel always points at an epoch that is no longer live.
- **Control: the mark is committed.** Run `bd dolt commit` first, so the mark is in the stale
  commit. Then `bd vc merge` with no strategy, `--strategy ours` and `--strategy theirs` are all
  refused, in both modes.
  - The strategy variants fail at bd's post-merge recompute commit:
    `merge succeeded but is_blocked recompute failed: … bh_write_mark have constraint
    violations`.
  - `bd conflicts resolve --conclude` will not settle the result: `Neither class has an
    ours/theirs resolution — bd conflicts resolve cannot settle them.`
- **A latent bd bug.** `MergeWithStrategy` sets `dolt_force_transaction_commit` and returns
  before its FK settle gate when a merge has violations but no conflicts
  (`versioncontrolops/mergesettle.go:293-301`). Only that incidental recompute commit stops it
  today.

**E9. Scenario 6: `hive_sync`, `bd sync` and `bd dolt pull` never revert `bh_writer`**
(`test_s6_…`). The test runs bh's real `hive_sync` engine call,
`BdEngine.sync_state(strategy="ours")`, which shells out to
`bd federation sync --peer hub --strategy ours --json`, inside each frame's environment.

- **One-sided** (a stale *writer*). The old writer never edits `bh_writer`, so there is no
  conflict to resolve in its favour. The merge brings the bump in together with the FK
  violation. Embedded federation sync prints
  `✗ merge failed: … constraint violations, transaction rolled back`. Server-mode federation
  sync prints `✗ fetch failed: failed to get peer credentials: not found: federation peer hub`.
  Remote `main` is unchanged in both modes.
- **Both changed** (a stale *adopter* holding its own bump):
  - Embedded `bd dolt pull` refuses: `merge conflicts in bh_epoch_live, bh_writer require
    operator resolution; merge aborted and working set restored`. bd's auto-resolver never
    touches `bh_*` (`mergesettle.go:99`).
  - Server-mode `bd dolt pull` leaves both conflicts in the working set. `bd conflicts resolve
    --all --table bh_writer --ours` then fails with `Merge conflict detected, @autocommit
    transaction rolled back`.
  - `bd sync` halts (exit 2). Embedded bd explains that the pull route restored the working
    set. Server-mode bd reports `3 table(s) are unmerged`: the two conflicts, plus the sentinel
    violation from E8.
  - `hive_sync --strategy ours` cannot merge: Dolt rejects the conflicted `DOLT_MERGE` under
    autocommit before bd reaches `ResolveConflicts`.
  - Remote `bh_writer` stayed at the current writer every time.
- **bh reporting gap.** In every one of these cases, bd exits 0 and serialises the error as
  `"Error": {}` with `"Merged": false` in `--json`. bh's `sync_state` (`engine.py:599-634`)
  therefore returns `SyncOutcome(ok=True, …)`. The fence holds, but `bh hive sync` reports
  success for a merge that never happened.

**E10. Nothing in the data stops a lower epoch written on top of the head. A monotonic trigger
does.**

- While building scenario 6, a test step re-ran a stale adopter's bump (epoch `e`) after
  resetting to a head that already held `c@e+1`. That is a one-sided change, and `bd sync`
  fast-forwarded it: `Sync complete.`, with remote `bh_writer` back at the stale adopter.
- Adopt itself never does this: it stops when `bh_writer.epoch ≥ mine`. But any local SQL
  could.
- A `BEFORE UPDATE` trigger on `bh_writer` and `bh_epoch_live` that signals unless
  `NEW.epoch > OLD.epoch` refuses it: `bh: fence epoch must increase`. The test pins that
  refusal.
- The trigger cannot stop a `--strategy` merge, because merges do not fire triggers (E11).

### Force paths: break-glass, mapped and detected

**E11. What each force path does to the fence** (`test_force_paths_…`,
`test_force_remote_reset_data_…`). Each was executed unless marked "by source".

| Path | What reaches it | Defeats the fence? | Detected by (E12) |
|---|---|---|---|
| `bd dolt push --force` | operator flag; `cmd/bd/dolt.go:587` → `CALL DOLT_PUSH('--force', …)` (`versioncontrolops/remotes.go:86-89`) | **Yes.** Remote `main` becomes the stale history; the bump is gone. | `placement_ahead` |
| `bd dolt remote reset-data origin --yes` | operator command; clears `refs/dolt/data` | **Yes.** The stale store's next plain `bd dolt push` succeeds, because nothing is left to be non-fast-forward against. | `placement_ahead` |
| `bd dolt pull --strategy ours` (embedded only; server says `does not support --strategy pulls (#4992)`) | operator flag; `MergeWithStrategy` sets `dolt_allow_commit_conflicts` and `dolt_force_transaction_commit`, then `DOLT_CONFLICTS_RESOLVE('--ours', <table>)` on every conflicted table | **With the proposal's bump, yes, for a stale adopter.** `bh_writer` and `bh_epoch_live` are resolved to the stale side, so the merge has no FK violation: `Pull complete.`, `Push complete.`, and remote `bh_writer` is reverted. **With the adopt sentinel, no:** the new writer's `adopt-<e>` mark is left pointing at an epoch that is not live, and bd refuses with `pull merge left constraint violations bd cannot auto-repair`. A stale *writer* is refused either way (E5 row 2). | `epoch_regressed`, `placement_ahead` |
| `bd vc merge --strategy ours` + `bd conflicts resolve --conclude` | operator flags, both modes | **With the proposal's bump, yes, for a stale adopter:** the same revert. **With the sentinel, no:** `conflicts resolved with 'ours' strategy but merge left constraint violations bd cannot auto-repair`. A committed stale-write mark is refused either way (E8 control). | `epoch_regressed`, `placement_ahead` |
| `bd vc merge` (no flag), server mode | plain command; the mark is not in the write commit (E7) | **Yes, with the proposal's bump. No, with the adopt sentinel** (E8). | `stale_marks` |
| `bd conflicts resolve --all --table T` (server, after a CLI pull left conflicts) | operator command | No: autocommit rejects it. | — |
| `bd federation sync --strategy` (`hive_sync`) | `bh hive sync --strategy` | No: autocommit rejects the conflicted merge before bd resolves anything (E9). | — |
| `bd conflicts resolve --conclude` on a merge left open with a violation | operator command | No: `Neither class has an ours/theirs resolution — bd conflicts resolve cannot settle them.` | — |
| raw `dolt commit --force` after a refused pull | Dolt CLI on the store (not bd) | **Yes.** It commits the merge with the violation; a plain push then lands it. | `stale_marks` |
| `dolt_force_transaction_commit` / `dolt_allow_commit_conflicts` | set only inside bd's merge-settle code (`mergesettle.go:65-69, 268-272`); no default path, config or env enables them | Only through the `--strategy` rows above. An operator can `SET GLOBAL` either variable with no privilege check (`bh-wtsrc`). | as above |
| `bd backup restore --force` | operator command; replaces the whole local DB (by source) | Local only: it drops the fence tables and triggers, and the next push is non-fast-forward unless forced. | `placement_ahead` if forced |

With the sentinel, the paths that still defeat the fence are `bd dolt push --force`,
`bd dolt remote reset-data`, and Dolt itself told to commit a violation (`dolt commit --force`,
or SQL that sets `dolt_force_transaction_commit` and deletes the violation rows). None is
reachable without an explicit operator flag or raw SQL.

`bd dolt push --force` is break-glass. Nothing in the data can stop it, and GitHub cannot
protect `refs/dolt/data`.

**E12. After-the-fact detection from remote `main` and HQ** (`fence_audit`). Each executed
force row that landed raised the signal named in E11, and each recovery cleared it.

- `stale_marks`: marks on `main` whose epoch is not live, i.e. a write stamped by a retired
  epoch was committed past the foreign key.
- `epoch_regressed`: `MAX(epoch)` in `dolt_history_bh_writer` is above the current
  `bh_writer.epoch`. A strategy merge keeps the newer adopt in its ancestry, so the history
  still shows the higher epoch.
- `placement_ahead`: the HQ placement epoch is above `bh_writer.epoch`. This is the same check
  as `bh-cvk70`'s "adopt incomplete".

Recovery in every case was re-running adopt step 2 at a fresh placement epoch. It retires the
stale marks and restores `bh_writer`. It cannot remove a stale write that already landed,
because that data is now history on `main`.

### Remote types, marks, cost

**E13. Non-fast-forward rejection is a true CAS on both remote types tested.**

- **`git+file://` (the `git+ssh` shape).** Dolt pushes the manifest with
  `--force-with-lease=refs/dolt/data:<old>` (`bh-eybn7` E2). E2 above shows both orders of a
  barrier-parked race producing exactly one winner and a `non-fast-forward` loser.
- **`file://`** (`test_file_remote_push_is_a_cas_…`). Two clones commit on the same base and
  push at the same instant, 8 rounds. Each round has exactly one winner; the loser gets
  `non-fast-forward`, and remote `main` equals the winner's commit. The file manifest update
  holds `LOCK` and compares the last lock hash (`store/nbs/file_manifest.go:177-198, 531-533`),
  and the push reports `ErrMergeNeeded` as non-fast-forward (`env/actions/remotes.go:125-126`).
- **Not tested:** `git+ssh` against GitHub-hosted refs (this relies on the host honouring
  `--force-with-lease`, as any Git server must), and remotesapi (not a production transport;
  `bh-eybn7` E11).
- No bd path forces except the operator commands in E11.

**E14. Marks: growth, pruning, and a hazard**
(`test_marks_grow_one_per_write_and_only_published_marks_may_be_pruned`).

- **Growth.** One row per guarded write: five `bd create`s plus the adopt sentinel gave 6 rows.
  A row is a 36-character UUID and a `BIGINT`. Each adopt deletes every mark below its epoch,
  so the table holds one epoch's writes plus one sentinel.
- **Safe prune.** The live writer prunes only *published* marks:
  `DELETE … WHERE id IN (SELECT id FROM bh_write_mark AS OF 'origin/main')`. An adopt racing
  that prune deletes the same rows, so the merge has no conflict. The writer's unpublished mark
  survives the prune and still fences it: the next pull hits the settle refusal.
- **Hazard.** A stale writer that deletes its *unpublished* marks escapes the fence. Its pull is
  a clean merge and its push lands the stale write. The rule "the writer may prune at any time"
  is therefore unsafe as written.

**E15. Triggers under bd.**

- The guard fires inside bd's linked Dolt (embedded) and in the Dolt CLI sql-server: E1's
  refusals came from `bd create` itself.
- No pull or merge was ever blocked by the guard's `SIGNAL`. Every refused merge failed on the
  foreign key or on a conflict. A pull delivering the bump to a non-writer applies cleanly,
  which matches the proposal's probe.

**E16. Cost.**

- A three-frame cluster takes about 18 s to build. A stale-writer round (adopt, adopt, write,
  path) takes 2–5 s.
- The file runs in about 8.5 min wall at `-n 2` on a host with load 6–12. The test calls take
  between 13 s and 190 s; scenario 5 is the longest.
- bd-server snapshots dominate, because every local read goes through `bd sql`.

### Against today's `refs/bh/epoch`

**E17. Comparison with `host_fence.py`.**

| | `refs/bh/epoch` (today) | In-data fence (this spike) |
|---|---|---|
| **Token** | A sibling ref, CASed *before* the push (`reserve_managed_push`), then verified *after* it (`verify_managed_push`). | The fence rows inside the data. The push's own branch CAS is the check. |
| **Reserve-to-push window** | A takeover in that window can land stale data. It is detected only afterwards (`host_fence.py:12-17`). | None: there is no separate token to race. E2 ran both interleavings, and neither produced a write after the bump. |
| **Raw `bd dolt push`, auto-push, `bd sync`** | Bypass the fence entirely, so bh refuses `bh bd dolt push` and `sync` on the primary. | Fenced (E5): non-fast-forward, or refused on the foreign key at merge. |
| **Pull then push** | Not fenced (the proposal's probe). | Refused on the foreign key in every bd mode (E5, E6). |
| **Remaining bypasses** | Every raw push. | Only explicit operator flags (E11), each detectable from the data (E12). |
| **Conflict resolution** | Cannot be resolved by a merge, because the ref never merges. | With the adopt sentinel, every resolution of a `bh_writer` conflict leaves a foreign-key violation, and bd refuses it (E11). Only Dolt told to commit a violation gets past it. |

## Verdict — **GO**

In-data epoch fencing fences a stale writer on every default bd path, in both bd storage modes,
on both remote types tested, with no reserve-to-push window. That covers scenarios 1–6 and the
CLI. The non-fast-forward rejection is a true CAS (E2, E13). A stale writer that pulls first
trips the retired epoch's foreign key: in embedded mode bd's settle gate refuses the merge, and
in server mode Dolt leaves it open and every later write or commit refuses (E5, E6). No default
bd path commits a foreign-key violation or resolves `bh_writer`. Writes from a partitioned old
writer are either rejected or preserved on `frame/<id>/orphan` and merged on purpose by the new
writer (E3). Nothing landed on `main` after a bump on any default path.

This also answers `bh-cvk70`'s hand-off (its Recommendation 6). With the singleton live epoch
and the adopt sentinel, every resolution of a `bh_writer` conflict leaves a foreign-key
violation. bd's linked Dolt refuses those merges the same way the Dolt CLI does (E5, E11).

The GO is conditional. These are binding conditions, each backed by a failing case above:

1. **Every bump inserts an `adopt-<epoch>` sentinel mark** (E8, E11). Without it:
   - plain `bd vc merge` in server mode lands a stale write, because bd's server-mode write
     commit leaves the trigger's mark unstaged (E7);
   - `bd dolt pull --strategy ours` and `bd vc merge --strategy ours` revert `bh_writer` to a
     stale adopter.

   With it, all three are refused. The upstream fix for the first is for that commit to stage
   the tables the transaction dirtied, or for `bd vc merge` to commit pending changes before
   merging, as pull already does.
2. **The live epoch is a single row, and every adopt rewrites `revision`** (`bh-cvk70` E12). The
   fence rows are monotonic under local DML (E10).
3. **Marks are deleted only by adopt, or by the writer for marks already on `origin/main`**
   (E14).
4. **`--force`, `reset-data` and raw `dolt commit --force` are break-glass on fenced hives**
   (E11). bh refuses them, and the `--strategy` paths as a second line of defence, through
   `bh bd`, and `bh doctor` runs the E12 audit.

## Recommendation

For the ADR (`bh-pr889`) and the implementation molecule, if the decision is GO:

1. **Fence schema.** Use the shape in E1–E10 and
   [`tests/harness/epoch_fence.py`](../../tests/harness/epoch_fence.py):
   - singleton `bh_epoch_live (id PK, epoch UNIQUE)`;
   - `bh_write_mark.epoch` as a foreign key to it;
   - `bh_writer.revision` rewritten on every adopt;
   - monotonic `BEFORE UPDATE` triggers on both fence tables;
   - guard triggers only on versioned bead tables.

   Write any statement that reads the fence inside trigger-heavy SQL with variables, not
   subqueries (E3).
2. **Adopt step 2** is `bh-cvk70`'s idempotent loop with one change to the bump: insert the
   `adopt-<e>` sentinel in the same commit (E8, E11). It is the one mechanism that closes both
   the server-mode `bd vc merge` hole and the `--strategy` reverts. Recovery from any force path
   is re-running step 2 at a fresh placement epoch (E12).
3. **Managed write path.**
   - In server mode, `bd dolt commit` (which stages every dirty non-config table) runs before
     each managed `bd dolt push`. The mark then travels with the write, and a planned handoff
     leaves no uncommitted marks behind to trip the old writer's next pull (E7).
   - On any fetch that shows `bh_writer.epoch` above the epoch held, the frame stops writing
     `main`, diverts to `frame/<id>/orphan` and resets (E3).
   - The new writer's orphan merge is one SQL session: forced merge, re-stamp marks to the live
     epoch, clear the violation rows, commit (E3). Drop the marks instead only if the orphan's
     writes are to be discarded.
4. **Lift the refusals.** Lift the refusal of `bh bd dolt push|sync` and of bd auto-push on the
   primary, as the proposal's §6 plans (E5). In the same change, refuse on fenced hives:
   - `bh bd dolt push --force`, `bh bd dolt remote reset-data`, `bh bd backup restore --force`;
   - any `--strategy` (`dolt pull`, `vc merge`, `federation sync`, `bh hive sync --strategy`);
   - `bh bd conflicts resolve` touching `bh_*`;
   - plain `bh bd vc merge`.
5. **`bh doctor` runs `fence_audit`** (E12) against remote `main` and HQ, and reports any signal
   as `fence breached` with the recovery in item 2.
6. **Fix bh's `hive_sync` reporting gap** (E9). Treat `Merged: false`, a non-null `Error`, or a
   `✗` line as failure. Today a refused federation merge reports `ok=True`.
7. **Upstream bd issues** to file against gastownhall/beads:
   - The server-mode write commit omits trigger side-effect tables (E7). A fix makes condition 1
     a belt rather than the only brace.
   - `bd vc merge` skips the pre-merge commit (E7).
   - `MergeWithStrategy` skips the FK settle gate when a merge has no conflicts
     (`mergesettle.go:293-301`, E8).
   - Federation sync exits 0 with a failed merge and serialises the error as `{}` (E9).
8. **Hand-offs to sibling beads.**
   - `bh-sieai`:
     - The guard fires under bd's linked Dolt. Pin the NULL `active_branch()` semantics there;
       this spike treated NULL as `main`, failing closed.
     - In server mode the guard's mark is never staged by bd (E7).
     - A refused server-mode pull leaves `bh_writer` merged into the working set, so the guard
       already refuses there (E6).
     - The trigger-analyzer bug also hits a plain `UPDATE` with subqueries once triggers exist
       (E3).
   - `bh-jbb6r`: build the randomized interleavings on `harness.epoch_fence`. Use the E12 audit
     as the invariant: after every round, no `stale_marks`, no `epoch_regressed`, and
     `placement_ahead` only while an adopt is in flight. Scenario 9's reclaim belongs in the
     same commit as the bump (`bh-cvk70` Recommendation 5).
   - `bh-32379`: the cutover adds the fence tables and triggers in one commit and seeds
     `bh_writer` from today's lease and epoch. The fence ref stays read-only for one release, as
     the proposal plans.
9. **Residual risks.**
   - `git+ssh` against GitHub is untested here.
   - In server mode, the link between a write and its mark lives in the working set until the
     next commit of the whole working set. Raw SQL that discards the working set
     (`DOLT_RESET('--hard')`) but keeps the commit would drop the mark, and the stale write
     would then merge cleanly on any path. bd's own abort keeps a dirty working set
     (`mergesettle.go:340-347`), so no default path does this. Item 3's commit before each
     managed push narrows the window; only the upstream fix in item 7 closes it.
   - `bd dolt push --force` remains break-glass, as today.
