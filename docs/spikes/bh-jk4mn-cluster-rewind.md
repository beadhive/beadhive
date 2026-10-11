# Spike `bh-jk4mn` — what is the fastest safe rewind for a running writer-fencing cluster?

**Bead:** `bh-jk4mn` · **Seat:** `dev/rewind` · **Type:** spike (evidence scripts only, no
product code)
**Feeds decision on:** how the writer-fencing integration tests stop paying for a fresh
cluster on every test. The fixture is [`tests/harness/writer_fencing.py`](../../tests/harness/writer_fencing.py)
from [`bh-eybn7`](bh-eybn7-writer-fencing-fixture.md), plus the composed world in
[`tests/harness/composed_fence.py`](../../tests/harness/composed_fence.py) from `bh-jbb6r`.

## Question

A writer-fencing cluster has a shared `git+file://` hive remote, an HQ stand-in, and 2–4 frames.
Each frame has its own Dolt replica: bd embedded, bd shared-server, or the Dolt CLI. Which
mechanism returns such a cluster to its exact post-bootstrap state fastest?

- **A.** `DOLT_RESET --hard` to a bootstrap tag.
- **B.** A branch per test, selected with `DOLT_CHECKOUT`.
- **C.** Copying a template of the stopped cluster with `cp --reflink=auto`.

And which state does Dolt **not** version, so that it needs its own reset step?

"Safe" is measured, not assumed. After the rewind, a full fingerprint of the cluster must equal
the bootstrap fingerprint. The fingerprint covers remote refs, HQ rows and users, and for every
frame: refs, tags, status, local-only tables, users, globals, bd files and Git refs.

This spike does **not** ask whether the fence is correct, and it changes no product code. It
also does not decide test sharding. Here, a cluster is built once per composition and rewound
between scenarios.

## Method

All runs used Dolt `2.3.5`, bd `1.3.0 (f45b249ce)`, GNU coreutils `cp` 9.7 and Linux 6.12.
Clusters lived on tmpfs (`/tmp`), with a copy probe on ext4. Following the operator's gate
policy, every run waited until no full gate was running. Only one cluster existed at a time.
The 1-minute load average was 3.7–5.7 for the recorded run and 3.6–19 across earlier runs.

1. **Dolt semantics probe**:
   [`artifacts/bh-jk4mn/dolt_semantics_probe.py`](artifacts/bh-jk4mn/dolt_semantics_probe.py),
   with results in
   [`dolt_semantics_probe.result.json`](artifacts/bh-jk4mn/dolt_semantics_probe.result.json).
   - **Setup.** One repo served by a private `dolt sql-server` and pushed to a `git+file://`
     remote. It has a `dolt_ignore`'d table, a monotonic `BEFORE UPDATE` trigger and a tag
     `boot`.
   - **Changes.** The probe then commits and pushes, and adds a branch, a tag, a versioned
     trigger, an untracked table, a user, `SET PERSIST`, `SET GLOBAL` and an open transaction
     in another session.
   - **Checks.** It records what `CALL DOLT_RESET('--hard', 'boot')` and `dolt reset --hard boot`
     restore. It also checks checkout scope, the per-database default-branch global, restoring
     globals, fetching after the remote moved backwards, and what survives a server restart.
2. **Dolt docs**, read for the claims the bh-eybn7 survey could not verify:
   - [`DOLT_RESET` / `DOLT_CHECKOUT`](https://docs.dolthub.com/sql-reference/version-control/dolt-sql-procedures):
     "`--hard`: Resets the working tables and staged tables. Any changes to **tracked** tables
     in the working tree … are discarded"; and "`DOLT_CHECKOUT()` … Switches **this session**
     to a different branch".
   - [Access management](https://docs.dolthub.com/sql-reference/server/access-management):
     "Users and grants are stored in the `.doltcfg/privileges.db` file".
   - [`dolt_ignore`](https://docs.dolthub.com/sql-reference/version-control/dolt-system-tables):
     ignored tables "should not be staged for commit".

   Every claim below was also reproduced by experiment.
3. **Cluster benchmark**:
   [`artifacts/bh-jk4mn/cluster_rewind_bench.py`](artifacts/bh-jk4mn/cluster_rewind_bench.py),
   with the recorded run in
   [`cluster_rewind_bench.result.json`](artifacts/bh-jk4mn/cluster_rewind_bench.result.json).
   - **World.** One `composed_world`: SqlHQ (bh-cvk70 design A with bh-wtsrc session and
     evidence rows), bd-embedded `a`, bd-server `b` and Dolt CLI `c`. It has the full fence and
     guard (44 triggers) and each frame's `bh_local_ident`.
   - **Bootstrap checkpoint.** A `fx-boot` tag on every frame and on HQ, after HQ commits its
     placement. Every remote object is pinned under `refs/fixture/boot/*`. A fingerprint is
     taken, and the rows of local-only tables plus bd's plain files are saved.
   - **The test, `dirty()`.** One round does what a fencing test does:
     - a write and push on `a`;
     - an HQ `place("b")`, then an adopt and a write and push on `b`;
     - a branch, a tag, an untracked table and an identity role change on `c`;
     - a user, `SET PERSIST` and `SET GLOBAL` on `b`'s server;
     - `refs/bh/epoch` and `refs/bh/backup/*` on the remote, and `refs/bh/lease/fx` in `a`'s
       clone;
     - an HQ session renewal and an evidence publish;
     - a partition of `c`, from the remote and from HQ;
     - a hold armed on `a`;
     - in alternate rounds, a SIGKILL of `b` (the server and the agent).
   - **Rewinds.** Each rewind is followed by a fingerprint diff against the bootstrap.

## Evidence

Items 1–8 come from the Dolt semantics probe and items 9–16 from the cluster benchmark.

1. **Hard reset to a tag works and is cheap.** `CALL DOLT_RESET('--hard', 'boot')` took
   2.7–2.9 ms in SQL; `dolt reset --hard boot` took 0.11–0.12 s on the CLI, which is almost all
   process start-up. It moved `main` to the tag commit. It also rewound versioned schema
   objects: a trigger created after the tag was gone afterwards. A new session saw the reset at
   once, because `--hard` "implicitly commits the current transaction" (docs).
2. **A monotonic trigger does not block a reset.** `w.epoch` went from 9 back to 5 under a
   `BEFORE UPDATE … IF new.epoch <= old.epoch THEN SIGNAL` trigger. Reset is not DML, so a
   rewind can move the fence epochs (`bh_writer`, `bh_epoch_live`, `bh_write_mark`) backwards
   even though the guard refuses that move at runtime.
3. **A hard reset leaves all of this in place:**

   | State | After `DOLT_RESET --hard <tag>` | Probe key |
   |---|---|---|
   | Extra branches | kept | `after_sql_reset…branches` |
   | Extra tags | kept | `…tags` |
   | Remote-tracking refs (`origin/main`) | kept, still ahead of the rewound remote | `…remote_branches` |
   | Untracked new (non-ignored) table | kept, with status `new table` | `…tables`, `…status` |
   | `dolt_ignore`'d table rows | kept | `…loc_ident` |
   | Users and grants (`.doltcfg/privileges.db`) | kept, and kept across a restart | `…users` |
   | `SET PERSIST` (in `.dolt/config.json`) | kept, and kept across a restart | `persisted_config_files` |
   | `SET GLOBAL` | kept until the server restarts | `after_restart.net_read_timeout` |

4. **An open transaction in another session commits across the reset.** The other session held
   an uncommitted `INSERT`. After the reset, its `COMMIT` succeeded, and the row (99) reappeared
   in the rewound working set (`stale_txn_commit: committed`). A rewind must therefore end every
   other session first, with `KILL` or a server restart.
5. **Checkout is per session, but a database-wide global redirects new sessions.**
   `DOLT_CHECKOUT` changed only the calling session; another session stayed on `main`.
   `SET @@GLOBAL.<db>_default_branch = 'per-test'` moved every new session to `per-test`.
6. **Two ways to restore globals do not work in 2.3.5.**
   - `SET GLOBAL a = …, b = …` applied only the first variable: the result was `[151, 77]`, not
     `[151, 28800]`.
   - `RESET PERSIST` is a syntax error. Undoing `SET PERSIST` means editing `.dolt/config.json`
     and running `SET GLOBAL`, one variable per statement.
7. **Fetch does not move a remote-tracking ref backwards.** After `refs/dolt/data` was rewound
   on the remote, `CALL DOLT_FETCH('origin')` left `origin/main` at the newer commit
   (`unchanged: true`). The ref has to be deleted (`DOLT_BRANCH('-d', '-r', '-f', …)`) and then
   fetched again. That is what `composed_fence.prune_remote_branches` already does
   (`tests/harness/composed_fence.py:349`).
8. **A push also moves a second remote ref.** The remote has three refs: `refs/dolt/data`,
   `refs/heads/__dolt_remote_info__` and the seed `refs/heads/main`. Every push moves
   `__dolt_remote_info__` as well. `Cluster.rewind` restores only `refs/dolt/data`
   (`tests/harness/writer_fencing.py:944`).

9. **Build cost (cluster benchmark, recorded run).** `composed_world` with three frames, SqlHQ
   and the installed fence took **22.6 s**. Of that, founding `a` took 7.0 s and joining `b`
   and `c` in parallel took 9.5 s. The rest was HQ, the fence install and provisioning. The
   bh-eybn7 per-test figures of 21–51 s include this build in every test (bh-eybn7
   Evidence 12).
10. **Today's rewind is fast but incomplete.** The naive round reproduces
    `Cluster.rewind` + `rewind_world`. It took **2.17 s** and left **21** fingerprint
    differences:
    - **Remote.** `__dolt_remote_info__`, `refs/bh/epoch` and `refs/bh/backup/fx-0/c`.
    - **HQ.**
      - `bh_placement` was left `modified` in the working set, because `restore()` is
        autocommit DML and not a reset.
      - Both frames' session and evidence rows were changed.
    - **Frame `a`.**
      - `events` and `local_metadata` (bd's `issue-create/v1/*` counter) had new rows.
      - `.beads/last-touched` was new.
      - `refs/bh/lease/fx` was new in its clone.
    - **Frame `b`.**
      - The same bd tables and file as `a`.
      - Extra user `u0@%`.
      - Persisted `max_connections`, and live `max_connections` and `net_read_timeout`.
    - **Frame `c`.** Branch `scratch-0`, tag `t-0`, the untracked table `scratch_untracked`,
      and `bh_local_ident.role` changed from `replica` to `branch`.
11. **Mechanism A is complete but slow when each step is its own process** (`full_rewind`).
    One `bd sql` or `dolt` process per statement, frames in series, took **13.6 s** in the
    recorded run and 13.8–18.7 s in an earlier run. Frame `b` alone took 7.7–10.6 s, because a
    `bd sql` call costs about 0.3–0.5 s. Its only leftover was the multi-assignment `SET GLOBAL`
    of Evidence 6.
12. **Mechanism A done fast** (`fast_rewind`) took **1.36 s and 1.40 s** without a kill, and
    **2.01 s** when `b` had been SIGKILLed. The restart of `b` added 0.63 s. The fingerprint
    diff was **empty in all three rounds**. Where the time went:
    - remote refs: one `git update-ref --stdin` transaction, 12–15 ms;
    - HQ and all frames in parallel: 0.88–0.91 s in total;
      - `a` (embedded): 0.88–0.90 s;
      - `b` (server, one pymysql connection): 0.59–0.67 s;
      - `c` (CLI): 0.42–0.43 s;
    - each frame's time is mostly its `dolt_fetch` through the gate. For the CLI and embedded
      frames it also includes two `dolt` start-ups: one read, then one script.
13. **Mechanism B, a branch per test, does not isolate a fencing test.** The probe ran once
    against the live cluster.
    - **Routing works.** Creating the branch on 3 frames took 0.43 s. Checkout took 0.11 s
      (CLI, embedded store) and 0.24–0.26 s (server global).
    - **The embedded frame followed the branch.** bd writes on `a` followed the CLI's
      checked-out branch: the write landed on `per-test`, not on `main`.
    - **The server frame followed it only through the global.** `DOLT_CHECKOUT` did not carry
      over to bd's next command, which ran on `main`. With the default-branch global set, bd's
      write landed on `per-test`.
    - **The write guard stayed silent.** It wrote **0** marks for those writes, because it only
      fires when `active_branch() = 'main'` (`tests/harness/write_guard.py:12`,
      `src/beadhive/fence_schema.py:199`). A per-test branch therefore turns off the very
      fence under test.
    - **The shared remote still moved.** `bd dolt push` from `b` on `per-test` changed remote
      `main` (`e2chqv35…` → `0f4utgdd…`). Nothing per-branch rewinds the remote, HQ, or any
      state in Evidence 3.
    - **Getting back is not free.** Returning `a` to `main` failed ("uncommitted changes already
      on branch 'main'"), so the probe needed `checkout -f`.
14. **Mechanism C, the template copy, is complete by construction and as fast.**
    - **Capture.** The template is taken right after the checkpoint: stop 0.77 s, copy 0.06 s
      (a 9.0 MB tree on tmpfs), start 1.25 s. The cluster restarted from the copy matched the
      bootstrap fingerprint exactly.
    - **Rewind.** Three rounds took **1.30, 1.32 and 2.02 s** (stop, `rm`, `cp`, start). Every
      diff was **empty**:
      - stop took 0.06 s when `b` had been killed, and 0.78 s for a graceful `bd dolt stop`;
      - `rm` took 0.02–0.03 s and `cp -a` 0.04 s;
      - start took 1.18–1.19 s: agent spawns, `bd dolt start` for `b`, and SqlHQ.
    - An earlier run, with the template captured later, measured 1.50 and 2.25 s.
15. **Reflink does not apply on this host, and is not needed at this size.**
    - `cp --reflink=always` fails with "failed to clone … Operation not supported" on both
      tmpfs and ext4. So `--reflink=auto` silently falls back to a byte copy.
    - That byte copy of the 9 MB template takes 0.04 s on tmpfs and 0.06 s on ext4.
    - Reflink pays off only on a filesystem that supports `FICLONE` (btrfs, or XFS with
      `reflink=1`) and only when a template grows to hundreds of MB.
    - macOS APFS clones through `clonefile(2)` (BSD `cp -c`), which is the optional adapter for
      operator Macs. It is not measured here.
16. **A template must go back to the same absolute path.** The remote URL
    (`git+file://<root>/remote.git`), the gate shim (`ctl` path), bd's
    `dolt-server-config.yaml` (`cfg_dir`) and SqlHQ's `server.yaml` (`data_dir`) all embed
    absolute paths. The listener ports for `b` and SqlHQ are fixed in their configs. So the
    restore copies back over the same root and restarts on the same ports. A port taken in
    between fails the start, and a retry must rebind and rewrite the configs.

### Non-versioned state inventory and reset step

`A` is the reset step that Mechanism A needs. In the template column, "copy" means the item is
on disk under the cluster root, so Mechanism C restores it by copying, with no code per item.
Evidence numbers point to where the item was observed.

| # | State | Where it lives | Survives `DOLT_RESET`? | Reset step (A) | Template (C) |
|---|---|---|---|---|---|
| 1 | Remote `refs/dolt/data` | hive `remote.git` | n/a (Git) | Restore it in one `update-ref --stdin` transaction with the other remote refs | copy |
| 2 | Remote `refs/heads/__dolt_remote_info__` | hive `remote.git` | n/a | Same transaction (E8, E10) | copy |
| 3 | `refs/bh/epoch`, `refs/bh/lease/<prefix>`, `refs/bh/backup/*`, `refs/bh/release-recovery*` | hive remote (git-mode HQ: `hq.git`) | n/a | Same transaction: delete refs that are not in the snapshot, and reset the rest (E10) | copy |
| 4 | Reachability of bootstrap objects | hive `remote.git` | n/a | Pin them under `refs/fixture/boot/*` at checkpoint time, so a `receive.autogc` prune can never take them | copy |
| 5 | Remote-tracking `origin/*` | each frame | yes (E3); fetch does not move it back (E7) | `DOLT_BRANCH('-d','-r','-f', …)` for each ref, then `DOLT_FETCH` | copy |
| 6 | Local `main`, working set and staged changes | each frame | no, the reset restores them | `DOLT_RESET('--hard','fx-boot')` | copy |
| 7 | Versioned triggers and procedures, including the 44 fence triggers | each frame and HQ (`dolt_schemas`) | no, the reset restores them | (in 6) | copy |
| 8 | Fence epochs (`bh_writer`, `bh_epoch_live`, `bh_write_mark`) | each frame (versioned) | no; monotonic triggers do not block it (E2) | (in 6) | copy |
| 9 | Extra branches and tags | each frame | yes (E3) | `DOLT_BRANCH('-D')` and `DOLT_TAG('-d')` for every name not in the snapshot | copy |
| 10 | Untracked non-ignored tables | each frame | yes (E3) | `DROP TABLE` for every table not in the snapshot | copy |
| 11 | `dolt_ignore`'d tables: `bh_local_ident` (identity and role), bd's `events`, `local_metadata` (issue-create counters), `leases`, `wisps*`, `repo_mtimes` (`src/beadhive/fence_schema.py:72`) | each frame | yes (E3, E10) | `DELETE`, then re-insert the snapshot rows | copy |
| 12 | Users and grants (`.doltcfg/privileges.db`) | `b`'s server and HQ | yes, across restart too (E3) | `DROP USER` for every user not in the snapshot; `REVOKE` or re-`GRANT` for changed grants | copy |
| 13 | `SET PERSIST` (`.dolt/config.json`) | `b`'s server | yes, across restart too (E3) | Remove the key from the file (there is no `RESET PERSIST`, E6), then `SET GLOBAL` | copy |
| 14 | `SET GLOBAL`, including `<db>_default_branch` | `b`'s server and HQ | yes, until restart (E3, E5) | `SET GLOBAL`, one variable per statement (E6) | restart |
| 15 | Open sessions and transactions (bd connections, `Renewer`, `Forwarder`) | servers and HQ | the transaction commits across the reset (E4) | `KILL` every other session before the reset; stop the test's own renewer and forwarder processes | stop |
| 16 | HQ placement (SqlHQ `bh_placement`) | HQ, written by autocommit | yes, unless committed | Commit at checkpoint, then `DOLT_RESET('--hard','fx-boot')` on HQ (E10) | copy |
| 17 | HQ liveness rows `frame_<id>_session` and `_evidence` (ignored, stamped by the server clock) | HQ | yes, and DML cannot set them because their triggers overwrite the value | `DROP TRIGGER …_upd`, `UPDATE` to the snapshot value, then let the HQ reset restore the trigger. Re-creating it by hand dirties `dolt_schemas` (measured) | copy |
| 18 | HQ reachability and outage (`_cut`, `down()`) | Python and the process | n/a | `heal()` every frame, `resume()` | restart |
| 19 | bd plain files: `.beads/last-touched`, `push-state.json`, `backup/` (bd auto-backup), `config.yaml`, `metadata.json` | each bd frame's checkout | n/a (files) | Copy them back from the checkpoint snapshot, compared by hash (E10) | copy |
| 20 | Local Git refs, such as a cached `refs/bh/lease/<prefix>` | each bd frame's `.git` | n/a | Delete `refs/bh/*` refs that are not in the snapshot (E10) | copy |
| 21 | Frame `HOME` and XDG state (bh caches, heartbeat and conformance cache) | `frames/<n>/home` | n/a | Snapshot restore; not exercised by this test | copy |
| 22 | Fixture controls: `ctl/partitioned`, `holds`, `held`, `arrived`, `git.jsonl` | `frames/<n>/ctl` | n/a | `heal()`, then remove the files | copy |
| 23 | Frame processes: the agent, `b`'s `dolt sql-server`, any parked gate | the process table | n/a | `restart()` dead frames; `bd dolt start` | restart |
| 24 | bd's `beads_global` database and `branch_control.db` | `b`'s server dir | not touched by these tests | none needed today; covered by the copy | copy |
| 25 | Python-side caches: `Remote._cache`, `MoveLog`, `hq._last`, `Frame._pending` | the test process | n/a | Reset them after every rewind | reset |
| 26 | Listener ports for `b` and SqlHQ | config files and the kernel | n/a | none | rebind on conflict (E16) |

## Verdict — **GO: template copy of a stopped cluster (Mechanism C)**

- **C is complete, and costs 1.3–2.0 s per rewind** against 22.6 s for a fresh build. It is
  complete because it restores every on-disk item in the inventory by copying it, with no code
  per item. Restarting the processes ends every session, which removes E4 and rows 14–15 as
  well.
- **A, the fast variant, is equally fast** at 1.4–2.0 s and also left an empty fingerprint.
  But it is safe only for as long as its 26-row inventory stays complete. Every new
  node-local table, file, global, user or ref is a silent leak, until someone adds a step for
  it. It also needs a direct SQL connection per server: done through `bd sql`, the same
  protocol costs 13.6–18.7 s.
- **B is NO-GO.** A per-test branch switches off the write guard, which only fires on `main`.
  It does not isolate the shared remote: `bd dolt push` from the branch moved remote `main`. It also
  rewinds none of the non-versioned state.
- **Reflink is optional.** At about 9 MB, a plain `cp -a` is 0.04 s, so the reflink and
  clonefile adapters only matter for much larger templates.

## Recommendation

1. **Add a template rewind to the fixture.**
   - Add `Cluster.snapshot()` and `Cluster.restore()` to `tests/harness/writer_fencing.py`,
     with a `World`-level wrapper in `composed_fence.py` that also stops and starts HQ.
   - The steps:
     1. stop the agents, `bd dolt stop` / reap, and stop HQ;
     2. `cp -a --reflink=auto` the root to a sibling template;
     3. on restore, run `rm -rf` and the copy back to the **same** path;
     4. restart the agents, `bd dolt start` and HQ;
     5. reset the Python caches (row 25).
   - Restart the server frames and HQ in parallel. Start-up (about 1.2 s) is most of the cost.
2. **Test fixture shape.** Build one cluster per composition per xdist worker (a
   session-scoped fixture keyed by the frame list). Snapshot it after `install()` and
   provisioning, and restore it at the start of each test. With the bh-eybn7 numbers, this
   saves about 20 s per test after the first in each composition.
3. **Make ports robust.** On a restore whose start fails, pick new ports and rewrite `b`'s
   `dolt-server-config.yaml`, its `BEADS_DOLT_SERVER_PORT` and SqlHQ's `server.yaml`. Then
   retry once.
4. **Keep fast Mechanism A only for mid-scenario rewinds** that must not restart processes, as
   `run_schedule` soak loops do today. Fix `rewind_world` to the `fast_rewind` shape: one
   remote-ref transaction, `DOLT_RESET` to a tag, one connection or script per frame, and the
   explicit steps for rows 5 and 9–20. Today's version leaves the 21 differences in E10.
5. **Keep the fingerprint as an opt-in assertion.** Check it after a rewind in a debug or soak
   mode, so that a new state class shows up as a diff and not as a flaky test.
6. **No reflink work now.** Revisit only if a template passes about 100 MB. Then gate a
   `clonefile` / `FICLONE` adapter on capability, so tmpfs and ext4 keep the plain copy.
