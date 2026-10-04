# Spike `bh-sieai` — local write guard and non-primary write paths

**Bead:** `bh-sieai` · **Seat:** `dev/guard` · **Type:** spike (test-only code, no product code)
**Feeds decision on:** `bh-pr889` (hive writer partitioning ADR), with `bh-vje85` (epoch/FK
fence, run in parallel) and `bh-jbb6r` (end-to-end). Parent molecule `bh-qlgmm`. Proposal:
[hive-writer-partitioning-proposal.md §2 and §4](../design/hive-writer-partitioning-proposal.md#2-fencing-the-epoch-lives-in-the-data).
**Executable evidence:**
[`tests/spikes/test_bh_sieai_write_guard_int.py`](../../tests/spikes/test_bh_sieai_write_guard_int.py)
(`integration`, 4 of 6 tests also `dolt_server`; about 6.5 min at `-n 2` with the soak
settings below). The guard prototype is
[`tests/harness/write_guard.py`](../../tests/harness/write_guard.py). Both build on the bh-eybn7
fixture ([`tests/harness/writer_fencing.py`](../../tests/harness/writer_fencing.py)).

## Question

1. Can a Dolt trigger on bead tables refuse `main` writes on every replica that is not
   `bh_writer.frame`, raw bd included, without blocking the merge or pull that delivers an epoch
   bump? Do triggers fire on merge, pull, sync, import and migrations under bd's linked Dolt
   (embedded) and in shared-server mode? Do bd migrations or `bd doctor` drop or reject the guard?
2. Which write path works for frames that are not primary, given bd 1.3's claim, lease and
   reclaim semantics: **forward** (bd on the frame points at the primary's Dolt server) or
   **branch** (commit to `frame/<id>`, the primary merges)?

Not asked here: the epoch/FK fence itself (scenarios 1–6, `bh-vje85`), HQ placement
(`bh-cvk70`) and the randomized run (`bh-jbb6r`). Where a result composes with the fence it is
called out, not re-tested.

## Method

- Tools: bd `1.3.0 (f45b249ce)`, which links Dolt `v0.40.5-0.20260715172757-a6690826d767`;
  Dolt CLI `2.3.5`; git `2.55.0`. bd 1.3 source read at `f45b249ce` for the claim CAS
  (`issueops/claim.go`), commit staging (`dolt/issues.go`), branch handling
  (`store.branch = "main"`, `embeddeddolt` `@@<db>_head_ref = 'main'`) and migrations.
- Every Dolt process ran with its own `HOME`/`DOLT_ROOT_PATH`. `~/.dolt/config_global.json` was
  not touched (mtime predates the session). No `SET GLOBAL`/`SET PERSIST`.
- Engine coverage. `bd-embedded` frames execute every bd write through bd's **linked** Dolt.
  `bd-server` frames execute through the frame's own `dolt sql-server`, which bd starts from the
  **2.3.5 CLI** binary (bh-eybn7 Evidence 2). Guard DDL and bh-side SQL go through the Dolt CLI:
  on the embedded store directory, or as a client of the frame's server.
- The six tests, in file order:

  | # | Test | Frames | What it pins |
  |---|---|---|---|
  | T1 | `test_dolt_trigger_semantics_the_guard_depends_on` | Dolt CLI repo | Six engine behaviours that fix the guard's shape; the guard's refusals; ALTER survival; detached `active_branch()` |
  | T2 | `test_merge_cherry_pick_and_revert_do_not_fire_the_guard_on_the_dolt_cli` | 2 CLI repos | Pull-merge, local merge, cherry-pick and revert on a stale replica |
  | T3 | `test_stale_replica_raw_bd_writes_are_refused_after_pulling_the_epoch_bump` | embedded + server | Raw bd writes refused on both engines; `bd dolt pull` and `bd sync` true merges succeed |
  | T4 | `test_import_migrations_doctor_and_fresh_joins_with_the_guard` | 2 embedded + 2 server | `bd import`, bd's own migrations 0059/0060, `bd doctor` (+`--fix`), fresh `bd init` joins |
  | T5 | `test_forward_path_claims_leases_and_failover` | embedded + server + forwarder | Claim race, heartbeat/reclaim, failover, forward to a demoted primary |
  | T6 | `test_branch_path_merges_contention_and_raw_push_hazard` | 3 embedded | #4796, #5657, branch claims, manifest contention, private data ref, raw push hazard |

- Soak settings for the numbers below: `BH_SIEAI_CLAIM_ROUNDS=60 BH_SIEAI_CLAIM_RACERS=8
  BH_SIEAI_PUSH_ROUNDS=10` (defaults 20/6/6 keep the gate run short). `BH_SIEAI_EVIDENCE=<file>`
  writes the measured values as JSON lines.

### The guard as built

```sql
-- versioned, committed by the writer:
bh_writer(id=1, frame, epoch)            -- proposal shape; bh-vje85 owns its fence semantics
bh_write_mark(id uuid, epoch, tbl)       -- one row per guarded main statement
procedure bh_guard_check():
  select frame into w from bh_writer where id = 1;
  select frame, role into me, r from bh_local_ident where id = 1;
  if coalesce(r,'') <> 'branch' and (w is null or me is null or w <> me) then
    signal sqlstate '45000' set message_text = 'bh-guard: this replica is not the bh_writer for main';
-- per guarded table, per event: BEFORE trigger
  declare e bigint;
  if coalesce(active_branch(), 'main') = 'main' then
    select epoch into e from bh_writer where id = 1;
    insert into bh_write_mark (id, epoch, tbl) values (uuid(), e, '<table>');
    call bh_guard_check();               -- last statement
  end if;
-- node-local, dolt_ignore'd ('bh_local_%'), never committed:
bh_local_ident(id=1, frame, role)        -- role 'replica' (default) or 'branch'
```

Guarded tables (14 × 3 events = 42 triggers): `issues, dependencies, labels, comments, config,
issue_counter, issue_snapshots, compaction_snapshots, custom_statuses, custom_types,
federation_peers, routes, interactions, provenance_events`. All `dolt_ignore`d tables are left
out, since they never merge or push. `child_counters` and `metadata` are left out too
(Evidence 6). Install is one idempotent drop-then-create script, about 1 s.

## Evidence

### 1. Dolt 2.3.5 trigger semantics decide the guard's shape (T1)

| Probe | Result | Consequence |
|---|---|---|
| `IF @w <> @me THEN SIGNAL` with session `@`-variables set in the same trigger | **Write accepted** although the values differ | Fails open. Conditions use `DECLARE`d locals or inline subqueries, which refuse correctly. |
| Statements after a `CALL` in a trigger body | Silently skipped | The `CALL` must be the last statement. |
| DML inside a procedure `CALL`ed from a trigger | Kept under autocommit, **silently dropped inside `BEGIN … COMMIT`** | bd writes in explicit transactions (`withRetryTx`). The first prototype's mark insert lived in the procedure and bd writes left no mark on either engine. The mark is now inline. |
| Multi-row statement, trigger that `CALL`s | Trigger body runs for the **first row only** | One mark per statement, not per row. The first row's check covers the statement, because writer and identity are the same for every row. A refusal still aborts the whole statement. |
| `information_schema` read inside a `CALL`ed procedure, multi-row statement | Reads 0 rows from the second row on | An identity-existence check there refused the writer's own migration 0059. Removed. |
| Trigger naming a `dolt_ignore`d table inside a non-taken `IF` branch | `table not found: bh_local_ident` on **every** branch where the table is absent | Table names resolve at plan time. The identity read moved into a procedure, which resolves only when called. |
| Scalar subquery inside `INSERT … VALUES` | Known bug ("unable to find field with index N") | Values come from `SELECT … INTO` locals. |
| `active_branch()` on `db/<hash>` / `db/<tag>` | `NULL`; the revision is read-only anyway | `COALESCE(active_branch(), 'main')` fails closed. |
| `ALTER TABLE` add / modify / rename / drop column | Triggers kept and still refuse | DDL migrations are safe. |

Guard behaviour on the CLI (T1): an unprovisioned `main` write fails `table not found:
bh_local_ident`. A foreign identity is refused for single-row and multi-row statements, under
autocommit and inside explicit transactions, and the refusal also rolls back the mark. The
writer's six statements leave six marks at its epoch. A non-`main` branch write is accepted with
no identity provisioned.

### 2. Triggers do not fire on merge, pull, cherry-pick or revert (T2, T3)

- **Dolt CLI (T2).** Stale replica B (identity B, writer A) has a local commit on an unguarded
  table. `dolt pull` makes a true merge and brings in A's two guarded inserts. It succeeds, and B
  holds exactly A's two marks: none was minted. Then `dolt cherry-pick`, `dolt merge frame/b` and
  `dolt revert` (of A's commit `w2`) all land on `main` without firing. The revert also took back
  the mark that `w2` carried. A direct delete afterwards is still refused.
- **bd's linked engine (T3, embedded).** Frame a, epoch 1, commits a guarded write X and does
  not push. b adopts epoch 2 and publishes issue Y, a label and a comment. Then `bd dolt pull`
  on a:
  - It makes a **true merge**: HEAD has 2 parents.
  - It brings in Y and `bh_writer = (b, 2)`.
  - a's mark set afterwards equals the union of the two parents. No trigger fired; a firing one
    would have SIGNALled, because a is no longer the writer.
- **bd's shared-server engine (T3, mirror).** b at epoch 2 commits W unpushed. a adopts epoch 3
  and publishes V. `bd dolt pull` on b makes a true merge and succeeds.
- **`bd sync` (T3).** On the stale embedded frame it exits 0: `{"attempts": 1, "pulled": true,
  "pushed": true}`. The merge did not fire. The push then **published X**, a pre-bump write,
  onto remote `main` after the bump.
  - The guard alone cannot stop this: X was written while a was still the writer.
  - It is exactly the pull-then-push hole that bh-vje85's epoch FK closes. X's commit carries
    an epoch-1 mark (Evidence 4).

**Consequence: the guard cannot wedge the epoch bump.** The same property is also a bypass: any
actor who can write a non-`main` branch can merge it into `main`, or cherry-pick from it, past
the guard. Raw bd cannot do this, because bd writes only `main` (Evidence 7). A Dolt-CLI-level
actor can, and can equally `DROP TRIGGER`. **The guard stops raw bd, not a hostile operator.**

### 3. A stale replica's raw bd writes are refused after it pulls the bump (T3)

Every raw write verb was run against an existing bead on three replicas: a non-writer on the
server engine, the stale embedded frame after its pull, and the stale server frame after its
pull. Every one failed in SQL:

```text
create  Error: insert issue into issues: Error 1644 (45000): bh-guard: this replica is not the bh_writer for main
update    fx-04l: updating issue: failed to update issue: Error 1644 (45000): bh-guard: …
claim     fx-04l: updating issue: failed to claim issue: Error 1644 (45000): bh-guard: …
label   Error: label added: added label 'stale' on fx-04l: add label: Error 1644 (45000): bh-guard: …
comment Error: adding comment: add comment to comments: Error 1644 (45000): bh-guard: …
close   Error closing fx-04l: failed to close issue: Error 1644 (45000): bh-guard: …
```

(The embedded engine prints `Error 1644: bh-guard: …` without the SQLSTATE.) After each refusal:

- `dolt_status` is empty: nothing half-written.
- `bd list` and `bd show` keep working.
- On the stale embedded frame, a non-`main` write through the Dolt CLI
  (`call dolt_checkout('-b', 'frame/a')`, insert, commit) succeeds and leaves `main` unchanged.

Overhead (median of 12 creates plus 12 label adds, same host and session):

| Engine | Unguarded | Guarded | Delta |
|---|---|---|---|
| Embedded | 0.572 s | 0.650 s | +78 ms |
| Server | 0.386 s | 0.462 s | +76 ms |

### 4. Where the mark lands differs by engine (T3) — composition note for bh-vje85

| Engine | Same commit as the write? | Mechanism |
|---|---|---|
| Embedded | Yes (`HEAD` has 1 mark after one create) | bd commits with `-Am` |
| Shared server | **No** (working set 4, `HEAD` 1; `dolt_status` shows `bh_write_mark` unstaged) | bd `DOLT_ADD`s only the tables it dirtied, then `DOLT_COMMIT('-m')` |

On the shared-server engine the marks stay uncommitted until bd's own "commit pending before
pull" step sweeps them into a commit. That step precedes the merge in `bd dolt pull` and
`bd sync`, and T3 asserts the stale marks are in `HEAD` after the pull. So the marks are in
history when a pull merges the epoch bump, which is when bh-vje85's FK has to see them.

A plain `bd dolt push` with no pull pushes the write **without** its mark. That push only lands
if it is a fast-forward, which means no bump has happened yet. If bh-vje85 wants the mark inside
the write's own commit on every engine, the robust shape is a column on the guarded row set by
the `BEFORE` trigger (`SET NEW.bh_epoch = e`), not a side table.

### 5. `bd import`, bd's migrations and `bd doctor` (T4)

- **`bd import` fires the guard.** On the writer, importing 3 issues with a label each added 6
  marks. On a non-writer it fails with `import failed: failed to insert issue fx-jpz: insert
  issue into issues: Error 1644 (45000): bh-guard: …`, leaving `dolt_status` empty.
- **DDL migrations keep the guard.** On the embedded writer, the schema was rolled back to v58
  (drop `storage_class`, cursor below 59). `bd migrate schema --force` then re-ran 0059–0066:
  - `max(version)` returned to 66 and `issues.storage_class` came back.
  - All 42 triggers survived.
  - 0059's `UPDATE issues SET is_blocked = 0, updated_at = updated_at`, which touches every
    issue row, passed the guard and stamped **1** mark (one per statement, Evidence 1).
- **DML migrations are refused on a non-writer.** The same rollback and command on the
  shared-server non-writer failed:

  ```text
  migration 0059_recompute_null_gate_is_blocked.up.sql: Error 1644 (45000): bh-guard: this
  replica is not the bh_writer for main (lock release also failed: schema: release migration
  lock: schema migration lock release failed: driver: bad connection)
  ```

  It took about 27 s, and bd refused every later command on that schema with its #4259 "adopt"
  gate. After an adopt through SQL, the identical command passed on that frame. **Only the
  writer may migrate.** That is already bd's own rule (#4259: one designated migrator); the
  guard makes it hard.
- **`bd doctor` ignores the guard.**
  - On shared-server, plain `bd doctor` and `bd doctor --fix --yes` print only the unrelated
    clone-local-FK notice. `bd doctor --server` reports "All server health checks passed", and
    `bd doctor --check=validate` reports "All data-integrity checks passed".
  - No output mentions `bh_` or triggers, and after `--fix` all 42 triggers and the identity are
    still present.
  - On embedded, `bd doctor` prints "not yet supported in embedded mode". bd doctor only checks
    that required tables exist (`cmd/bd/doctor/dolt.go`).
- **Re-install path.** No bd 1.3 migration drops and recreates a *guarded* table. 0028, 0055 and
  0062 rebuild only ignored tables. Should a later bd do so, the guard script is idempotent
  (drop-then-create): re-run it on the writer, commit, and push. bh should check the trigger
  count (42, `write_guard.guard_state`) at adopt and at start-up, and refuse to act as writer
  when it is short.

### 6. Fresh joins and identity provisioning (T4)

- **`child_counters` broke every fresh join.** The first guard covered `child_counters`. Every
  fresh `bd init` join of the guarded hive, embedded and shared-server alike, then failed in bd's
  clone-local ignored migration:

  ```text
  migrate: ignored migrations: migration 0011_cleanup_orphaned_child_counters.up.sql: Error 1105:
  delete from with explicit target tables does not support triggers; retry with single table deletes
  ```

  Dolt refuses every multi-table `DELETE` on a table with triggers, whatever the rows. 0011
  (`DELETE cc FROM child_counters cc … JOIN`) replays on every clone because its cursor is
  `dolt_ignore`d. `child_counters` is therefore unguarded. A counter bump only happens inside the
  child-create transaction, whose `issues` INSERT is guarded.
- **`metadata` produced a warning on every join.** With `metadata` guarded, each join printed
  `Warning: failed to write repo_id metadata: … table not found: bh_local_ident`, and the same
  for `clone_id` and `last_import_time`. bd writes these clone-local values on every join and
  import, and bd's auto-resolver treats `metadata` as machine-local. `metadata` is unguarded.
- **With both left out, fresh joins succeed on both engines with no warning.** After a join:
  - The 42 triggers have arrived through the clone.
  - `bd list` and `bd dolt pull` work.
  - A `main` write fails closed (`Error 1146: table not found: bh_local_ident`).
  - A non-`main` write works.
  - After `provision()`, the frame is an ordinary refused non-writer.

  **Provisioning rule:** the frame's identity row is created right after `bd init` joins and
  before any bd write. It is never committed (it is `dolt_ignore`d) and never pulled. An
  unprovisioned frame is read-only on `main`.

### 7. bd writes only `main`

Both stores pin the branch:

- Server: `store.branch = "main"`, commented "replaces the former branch-per-worker approach
  (BD_BRANCH)".
- Embedded: `SET @@<db>_head_ref = 'main'` from every store factory.

bd 1.3 has no option to write another branch. `bd branch` creates branches; `bd vc merge` merges
them into `main`. So a branch-path frame's bd writes land on its *local* `main`, which the guard
refuses unless the frame's identity has `role = 'branch'`. That role is what T6 uses.

### 8. Forward path: claims, leases and failover (T5)

Setup: frame `f` has a bd workspace whose `.beads/metadata.json` points at primary `p`'s
sql-server (`dolt_mode: server`, host and port). Forwarded writes execute on `p`, under `p`'s
guard and identity. `bd create` from `f` succeeds with `created_by = f`, and the mark is
stamped on `p`.

**Claim race.** 60 rounds. Each round starts 8 concurrent `bd update <id> --claim --json`
processes as 8 distinct actors, released together by a gate file.

| Measure | Result |
|---|---|
| Rounds with exactly one exit-0 claimer | 60 / 60 |
| Double wins | **0** |
| `claim_won` read-back winners | exactly 1 in every round, always the exit-0 actor |
| Losers' error (prototype run, 20 rounds × 4) | `issue already claimed by <winner>` |
| Wall time | 104 s |

On one server, bd 1.3's claim is a CAS: a conditional `UPDATE … WHERE row_lock = ?` inside a
transaction, plus Dolt's transaction-commit conflict check. gastownhall/beads#4657 did not
reproduce through a single server. `work_next.claim_won`'s read-back stays the right belt.

**Heartbeat and reclaim on the granting replica.** Clients of one server are one replica, so
they share `BEADS_NODE_ID=p`.

- The holder's `bd heartbeat` succeeds. Any other actor gets `issue already claimed by …`.
- The lease row records `granted_node = p`.
- `bd reclaim --older-than 0s` reaps nothing while the lease is live. After the lease is aged in
  SQL (the TTL is fixed at 5 min), it reaps exactly that bead:
  `{"count": 1, "reclaimed": [{"id": …, "previous_owner": "w4"}]}`.

**Failover.** `p` pushes and is killed. `q` adopts epoch 2 and pulls.

- `q` sees the claimed beads as `in_progress` with their assignees, and has **0** lease rows:
  `leases` is `dolt_ignore`d and node-local.
- `bd reclaim --older-than 0s`, `--any-replica`, and `--any-replica --id <bead>` each reclaim
  `count: 0`.
- **58** beads stay stranded `in_progress`: every claim the 60-round race left except the reaped
  one and the one test-reverted.
- `bd unclaim <id> --force`, run on `q` as the new writer, does revert one.

This confirms bh-cvk70: **after failover bh must revert the dead primary's `in_progress` beads
itself.** bd's reclaim cannot see them on another replica.

**Demoted primary.** `p` restarts, still holding identity `p`, and pulls the bump. The next
forwarded `bd create` is refused with `Error 1644 (45000): bh-guard: this replica is not the
bh_writer for main`. A forwarder that still points at a demoted primary fails closed.

**Durability caveat.** A forwarded write lives in the primary's working set and commits. It
reaches the remote only when the primary pushes. If the primary dies before pushing, those
writes stay on its disk: bh-vje85's orphan path, not lost but not on `main`.

### 9. Branch path: merges, collisions and contention (T6)

Frames `b1` and `b2` (embedded, `role = 'branch'`) pull `main`. Each then runs
`bd create --parent <epic>`, `bd label add <L> lab-<name>` and `bd update <C> --claim`, and
publishes its local `main` as `frame/<name>` with `dolt push origin main:frame/<name>`. Remote
`main` is untouched. The primary fetches and merges each branch with `bd vc merge`.

Merge results:

- **gastownhall/beads#4796 reproduces by construction.** Both branches allocate the same child
  ID, `<epic>.1`, from their own copy of `child_counters`.
- **Both claims succeed locally.** Two frames each believe they hold `C`.
- **The second merge is refused.** `bd vc merge origin/frame/b1` is clean. `bd vc merge
  origin/frame/b2` fails: `Error 1105: Merge conflict detected, @autocommit transaction rolled
  back …`. `dolt_status` is empty afterwards.
- **`--strategy` resolves it by silently dropping data.** `bd vc merge origin/frame/b2
  --strategy ours` succeeds:
  - `child-b2` **no longer exists**: `<epic>.1` is `child-b1`.
  - `C`'s assignee is `b1`, while `b2` still believes it holds the claim.
- **Labels union cleanly:** `{lab-b1, lab-b2}`. Disjoint label rows merge at row level, so
  gastownhall/beads#5657 (label read-then-write) does not arise *across* branches. It is a
  same-store race.

**Raw push hazard.** On a branch-role frame, `bd dolt pull` followed by `bd create` and
`bd dolt push` lands the new issue on remote **`main`**. bd pushes
`CALL DOLT_PUSH('origin', 'main')`. The guard allows the local write (role `branch`), and the
push is a fast-forward. The guard cannot see pushes, and the fence only catches marks from a
retired epoch.

**Frame-private data ref.** Give a branch-role frame's `origin` the same Git remote with its own
data ref:

```text
dolt remote add --ref refs/dolt/frame/b2 origin <hive-url>
```

Then:

- Raw `bd dolt push` writes `refs/dolt/frame/b2` and leaves `refs/dolt/data` and remote `main`
  unchanged.
- The primary adds `--ref refs/dolt/frame/b2 frame-b2 <url>`, fetches it and merges
  `frame-b2/main`.
- bd honours the per-remote `--ref` configured through the Dolt CLI.

**Manifest contention** (10 rounds per shape; an uncontended data push makes 2 `--force-with-lease`
CAS calls on `refs/dolt/data`). For each pair, "CAS calls" are the first frame's, then the second's:

| Shape (pair) | Both succeeded | One rejected | CAS calls per round | Median / max wall |
|---|---|---|---|---|
| `main` (primary, bd) vs `frame/b1` (CLI), both parked at their first CAS and released together | 10 / 10 | 0 | 4–5 vs 3–5 (1–3 retries each) | 2.24 / 2.31 s |
| same pair, free-running | 10 / 10 | 0 | 2–4 vs 2–4 (≈ 1 retry per round) | 2.03 / 2.28 s |
| `main` vs frame-private ref (`bd dolt push`) | 10 / 10 | 0 | 2 vs 0 on `refs/dolt/data` | 1.49 / 1.55 s |
| control: `main` vs `main`, both parked | 0 / 10 | **10 / 10** | 3–4 vs 3–4 | 1.91 / 1.95 s |

- A `frame/<id>` push and a `main` push **do** contend on the one `refs/dolt/data` manifest
  (gastownhall/beads#5157 residue). Dolt's git blobstore retries the lease and never lost a push
  in 20 contended rounds. The cost is 1–3 extra CAS round-trips, about 0.5–0.7 s on a local
  `file://` remote.
- A private ref removes the contention entirely.
- Not measured against GitHub-hosted refs, where each retry is a network round-trip.

### 10. Other observations

- **Mixed founders.** An embedded bd joiner could not open a store founded by a `bd-server`
  frame: `schema skew check: probing schema_migrations existence: table has unknown fields`.
  T5 uses an embedded founder for that reason. Seen once and not chased, but `bh-32379`'s
  mixed-fleet cutover should re-check it.
- **`dolt_commit_ancestors`.** On the 2.3.5 sql-server, any `WHERE` on `dolt_commit_ancestors`
  failed with `result max1Row iterator returned more than one row` once `HEAD` was a merge
  commit. T3 filters client-side.
- **`dolt_nonlocal_tables` does not rescue the inline guard.** It was tried as an alternative to
  the procedure (make `bh_local_ident` visible from every branch). Once the nonlocal row for
  `bh_local_%` exists on `main`, `CREATE TABLE bh_local_ident` on `main` is refused: "matches a
  name present in dolt_nonlocal_tables". A fresh clone receives that row before it can create
  its identity. Rejected.

## Verdict — **GO** for the guard and the forward path; branch path **restricted**

**Write guard: GO.** A committed `BEFORE` trigger set plus a `dolt_ignore`d identity row refuses
every raw bd write verb on a non-writer's `main`. That holds on bd's linked engine and on the
shared-server engine, before and after the replica pulls the epoch bump (Evidence 3). The guard
never fires on what delivers the bump: `bd dolt pull` and `bd sync` true merges on both engines,
and Dolt pull, merge, cherry-pick and revert (Evidence 2). It also leaves alone:

- `bd doctor`, including `--fix` (Evidence 5);
- bd's DDL migrations (Evidence 5);
- fresh `bd init` joins (Evidence 6);
- non-`main` branches.

It costs about 77 ms per bd write. The GO is conditional on the shape in Evidence 1 and the
table set in Evidence 6. A naive trigger (the proposal's sketch with `@`-variables, or a mark
insert inside the procedure) **fails open or loses its marks** on Dolt 2.3.5, and guarding
`child_counters` breaks every join. The guard is a guard against raw bd, not against an operator
with Dolt access (Evidence 2), and it does not fence pre-bump writes. That is bh-vje85's job, and
Evidence 4 is the composition note for it.

**Forward: GO** for any frame that claims or creates.

- The primary is the only place claims are granted.
- Measured double wins: 0 in 60 eight-way races.
- `claim_won` agrees in every round.
- Heartbeat and reclaim work on the granting replica.
- Forwarders follow the server's guard, so a demoted primary fails them closed.

**Branch: restricted.** It is viable only for edits that do not allocate IDs or claim. The
reasons:

- Child IDs collide (#4796).
- Branch claims double-win by construction, and `--strategy` silently drops the losing side.
- bd cannot write a non-`main` branch, so the frame needs `role = 'branch'`. Its raw
  `bd dolt push` then publishes to `main` unless `origin` is a frame-private data ref.

## Recommendation

1. **Adopt the guard as built in `tests/harness/write_guard.py`** (shape in Evidence 1, 14
   tables in Evidence 6). Product rules:
   - **Install:** the writer installs at adopt time by re-running the idempotent script, commits
     and pushes. The guard spreads to replicas by pull.
   - **Provision:** each frame creates its `bh_local_ident` row right after `bd init`, before
     any bd write.
   - **Check:** at adopt and at start-up, verify that 42 triggers exist; a frame short of them
     refuses to act as writer.
   - **Migrate:** only the writer migrates. The guard enforces this; non-writers take the
     schema by pull.
   - **Re-install:** after any bd upgrade, re-run the guard script on the writer.
2. **Per frame role:**

   | Frame role | Write path |
   |---|---|
   | Execution frames and agents that claim, create or close | **Forward** to the primary's server over the LAN. |
   | Frames that cannot reach the primary (partitioned, cross-site, offline) | **Branch**, limited to non-allocating edits (comments, labels, notes, status of beads they already hold), published to a **frame-private data ref** (`refs/dolt/frame/<id>`), not `frame/<id>` inside `refs/dolt/data`. |
   | The primary | Writes `main` directly. |

   The private ref removes manifest contention and makes a raw `bd dolt push` on that frame
   harmless.
3. **bh owns failover cleanup:** after an adopt, revert every `in_progress` bead whose holder
   was granted by the old primary. On the new writer, `bd unclaim --force` does it, as T5 shows.
   bd's reclaim cannot see these beads.
4. **Upstream bd fixes, none required for the forward path:**
   - **#4796**, frame-scoped child IDs: needed before the branch path may create children.
   - **Branch writes** (a `BD_BRANCH`-style option or `--branch`): lets branch frames keep the
     guard strict on `main` instead of using `role = 'branch'`.
   - **Reclaim of lease-less claims** (for example `bd reclaim --orphaned`, or replicated lease
     rows): replaces bh's failover cleanup.
   - **Staging:** server-mode commits that stage triggered side tables (or `-Am`), so marks ride
     in the write's own commit (Evidence 4; relevant to bh-vje85).
5. **Upstream Dolt bugs to file** (all reproduced on 2.3.5 by T1 and T3):
   - `@`-variable `IF` comparisons fail open in triggers.
   - Statements after `CALL` are skipped.
   - Procedure DML is dropped inside explicit transactions.
   - Only the first row's trigger body runs after a `CALL`.
   - `information_schema` reads 0 for later rows.
   - Multi-table `DELETE` is refused on tables with triggers.
   - `dolt_commit_ancestors` fails `max1Row` on merge heads.
   - Already known: the scalar subquery inside `INSERT … VALUES`.

   Pin the guard's shape to these tests and re-run them on every Dolt or bd bump. bd's linked
   engine (`a6690826`) showed the same mark loss as the CLI.
6. **For `bh-jbb6r`:**
   - Reuse `write_guard.install`, `provision` and `guard_state`. `guard_state` is a ready
     `Recorder` `guard_probe`.
   - Use an embedded founder.
   - Keep the soak knobs (`BH_SIEAI_CLAIM_ROUNDS`, `BH_SIEAI_PUSH_ROUNDS`) out of the land gate.
