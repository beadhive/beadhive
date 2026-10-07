# Runbook — cutting a hive over to the in-data epoch fence

> **Status:** operator procedure for 0.23.0 (`bh-oarxp`, P-M4). The verb it describes,
> `bh hive fence`, is **hidden and temporary**. It appears in no `--help` output and no CLI
> reference, and this runbook is its only documentation. It is removed (E1) once every hive is
> cut over; after that, a rollback follows §5 by hand.

Design: [ADR — hive writer partitioning](hive-writer-partitioning-adr.md) (Decision 3, conditions
11–12). Evidence: [spike `bh-32379`](../spikes/bh-32379-writer-partitioning-migration.md) §1 (C1–C6)
and §3 (R1–R5), cases T1–T6.

## 1. What the cutover does

A cut-over hive carries its own writer fence in its Dolt data: `bh_writer`, `bh_epoch_live`,
`bh_write_mark` and 44 `bh_*` triggers (42 guard triggers on the 14 versioned bd tables, plus 2
monotonic triggers). From then on the hive's data is the switch ("data is the switch",
condition 12). No `host.yaml` or fleet-config key turns it on, and nothing cuts a hive over
automatically. During coexistence (Φ2) the legacy carriers, the HQ placement and
`refs/bh/epoch`, stay in lockstep with `bh_writer` on every adopt.

The verb has three actions:

| Command | Effect |
|---|---|
| `bh hive fence cutover <hive> --others-published` | Runs C1–C6 on this host, which must be the current holder. Idempotent. On a replica of a hive that is already cut over, it provisions only the replica's identity, and the flag is not needed. |
| `bh hive fence status <hive> [--json]` | Read-only. Prints `{hive, E, cutover commit, ref sha, fence_audit, trigger count}`. Exits 1 on any finding. |
| `bh hive fence rollback <hive>` | Runs R1–R5 on the current `bh_writer` holder. On a replica of a hive that is already rolled back, runs only R5. |

`bh doctor` reads the same status for every hive that is cut over on the host. It adds a
**Writer fence** section that shows the `fence_audit` fields `stale_marks`, `epoch_regressed`,
`placement_ahead` and late writes, the trigger count against 44, and the cutover record. It
raises a warning for each finding. `placement_ahead` keeps its own "adopt incomplete" warning,
which carries the recovery command.

## 2. Before you start

- **The holder runs 0.23.0 or later.** Every replica that will keep writing must also be on
  0.23.0 before it pulls the cutover. An older replica fails closed (T3): it can read but not
  write `main`.
- **One hive at a time.** Use a low-traffic hive as the canary, and cut `bh` over last.
- **GitHub-hosted remotes:** run the condition 11 canary against that remote first
  ([section 2a](#2a-condition-11-the-github-canary)).
- **Rotation count (condition 14):** while the hive's frame lease is still legacy-bound, check the
  rotation count ([section 2b](#2b-condition-14-the-rotation-count)).
- **Quiesce the hive.** A re-install drops each trigger and then re-creates it, so there is a
  moment without that trigger. Stop dispatch and agents on the hive while it is cut over.
- **Server mode is slow.** The install is about 95 `bd sql` calls, which takes 30 s or more.
- **Check for unpublished commits on other hosts.** Every replica must have published
  everything it committed. Commits made before the cutover carry no marks, so the fence cannot
  see them. For the live frame, check the operator Mac's clone (L7). Then you pass
  `--others-published`.
- **Publish on the holder.** Run `bh hive sync --push`. The cutover resets the holder to the
  remote head, so it refuses while this node has a dirty working set or local commits. bd's
  clone-local `metadata` table is the one exception.

## 2a. Condition 11: the GitHub canary

Before the **first** cutover of a hive whose remote is GitHub-hosted, show on that real remote
that the ref compare-and-swap holds. Cutover relies on it twice: the remote's non-fast-forward
rejection is the writer token's CAS, and C5 reserves `refs/bh/epoch` the same way. The spike
([`bh-vje85`](../spikes/bh-vje85-in-data-epoch-fencing.md) E13) proved it on `git+file://` and
`file://` remotes only. No `bh` verb runs this canary; it is an operator procedure with two
parts, and both must pass before C1.

1. **Two-writer push race.** From two clones of the target remote, commit on the same base and
   push to the same ref at the same instant, for several rounds (E13 used 8). Each round must
   have exactly one winner, the loser must get `non-fast-forward`, and the remote ref must equal
   the winner's commit. Use a scratch ref or a scratch repository on the same provider and
   protocol, never the hive's real `main` or `refs/dolt/data`.
2. **Backup-ref lifecycle.** Push a ref under `refs/bh/backup/`, CAS-update it, then delete it
   (the shape the state/work pairing feature uses; [WORK](../WORK.md#statework-pairing--backups-before-state-condition-18),
   ADR Addendum M14). Confirm that no GitHub Actions run starts and that the UI shows no
   "recent branch" prompt.

A round with two winners, or a loser that is not rejected, blocks the cutover: report it and
keep the hive on the legacy fence.

## 2b. Condition 14: the rotation count

Until the cutover, a signed-mode frame lease stays bound to the grant it was adopted under, and
survives only while that grant is in the active-rotation archive, which keeps the newest 16
entries (`HISTORY_LIMIT = 16`, `beadhive.frame_release_upgrade`; see the
[release-upgrade runbook](frame-release-upgrade-runbook.md#archive)). Count the active frame's
rotations (`release_upgrade_history` in `bh host release-upgrade check`) per lease. Before a
**16th** rotation without a renewal or re-adopt, re-adopt the lease deliberately. Otherwise the
lease's grant falls out of the archive and forces an unplanned re-adopt at a new lease epoch,
and in-flight claims refuse their bead write. A cut-over hive removes the horizon: placement
names the frame, and a rotation never moves the writer epoch.

## 3. Cutover (C1–C6)

On the holder:

```sh
bh hive fence status <hive>                         # expect: not cut over
bh hive fence cutover <hive> --others-published
bh hive fence status <hive>                         # expect: clean, 44 triggers
```

| Step | What the verb does | Refuses when |
|---|---|---|
| C1 | Reads `refs/bh/epoch` and HQ placement, and checks that this node has nothing unpublished. | You did not attest `--others-published`. The ref or placement is missing. The two carriers disagree (`placement_ahead` included). This host is not their holder. This node has unpublished work. |
| C2 | Seeds `E = max(ref, placement)` with no bump, so in-flight claim tokens stay valid. | — |
| C3 | Resets this node to the remote head, then makes one commit. The commit holds the tables, the ignore row `bh_local_%`, `bh_writer`/`bh_epoch_live` at `E`, the `adopt-E` and `cutover-E` sentinels and the guard (44 triggers). The commit message carries a `bh-cutover:` line with `hive`, `epoch`, `holder` and the `ref` sha. | The seed would not name this host at `E`. |
| C4 | Provisions this host's `bh_local_ident` after the commit and before the push. The identity is never staged or pushed. | The ignore row is not on `HEAD`. |
| C5 | Reserves `refs/bh/epoch` (`seq + 1`, a CAS on the sha that C1 read), pushes without force, and verifies the reservation. A non-fast-forward push resets the node and redoes C3, up to 3 rounds. | The ref moved since C1, or every round lost the push race. |
| C6 | Verifies that `fence_audit` is clean, that all 44 triggers are present, and that the holder's guard passes `CALL bh_guard_check()`. Then it prints the record. | Any check fails. This is the `CutoverFailed` case: the cutover is published but not verified. |

On every refusal before C5's push, the verb resets the holder to the remote head and drops its
identity, so nothing is published. Re-running a completed cutover is safe. It provisions a
missing identity and verifies again.

**Then, on every other replica:** pull (`bh hive sync`), then run
`bh hive fence cutover <hive>` there. On a replica of a hive that is already cut over, the verb
does only one thing: it provisions that replica's `bh_local_ident`. It never resets, commits or
pushes, and it refuses until the replica has pulled the cutover. A replica is read-only on
`main` either way (T3). With an identity, the guard refuses it by name instead of by a missing
table, and a later adopt can make it the writer. Doctor's Writer fence section shows a replica
without one as `identity unprovisioned`.

### Refusal and failure recovery

| Message | Do |
|---|---|
| `C1 refused — … never been adopted` / `no live placement` / `disagree` | Run a legacy adopt (`bh host lease adopt <hive>`) on the intended holder, then re-run the cutover there. |
| `C1 refused — only the current holder …` | Run the cutover on the host that placement names. |
| `… holds unpublished work` | Run `bh hive sync --push`, then re-run. |
| `C5 refused — refs/bh/epoch moved since C1` | Something adopted or pushed meanwhile. Read `bh hive fence status`, quiesce the hive, then re-run. |
| `C5 lost 3 push rounds` | Quiesce the hive's writers, then re-run. |
| `C5 verify failed` / `C6 verification failed` (`CutoverFailed`) | The cutover may be published. Stop writes on the hive and read `bh hive fence status`. If the guard is short, re-run the cutover while the hive is quiet. It is idempotent and re-verifies. For a `fence_audit` finding, see §4. |

## 4. Status and detection

`bh hive fence status <hive>` and doctor's Writer fence section report these findings:

- `stale_marks` — a commit stamped by a retired epoch was forced past the foreign key.
- `epoch_regressed` — `bh_writer` is below an epoch that its own history reached.
- `placement_ahead` — adopt incomplete. Re-run `bh host lease adopt <hive>` on the placed host.
- `late_write` — a commit written before an adopt entered `main` after it (`bh-uhx2r` E2).
- `guard: N of 44` — this node is short of triggers. Re-run the cutover while the hive is quiet.
- `unmerged orphan frame/<id>/orphan-<epoch>-<n>` — a superseded frame diverted its unpublished
  commits there. Merge it on the writer (§4a).

## 4a. Orphans after a partitioned writer rejoins

On a cut-over hive every managed push (`bh hive sync --push`, report publication, the lifecycle
verbs) commits the working set first. In server mode a failed commit refuses the push, because
the guard's marks would not travel with the write. The push then fetches without merging. If the
remote head's `bh_writer.epoch` is above the epoch this frame's committed `main` holds, the frame
has been superseded and does this instead of pushing `main`:

1. it commits its working set and pushes its unpublished commits to
   `frame/<id>/orphan-<held epoch>-<n>`, where `<n>` is the next free number;
2. it checks that the branch landed on the remote, and stops with nothing reset if it did not;
3. it resets local `main` to the remote head. In server mode the reset first kills every
   forwarder session (`host.forward.serve.quiesce_before_reset`), so an in-flight forwarded
   write is refused rather than acknowledged and then dropped.

The push reports `superseded` and exits non-zero. Nothing is lost: the work is on the orphan, and
a bead's worktree commits are still on its per-(bead, frame) backup ref (rule P). Legacy hives
never take this path.

List and merge orphans on the **writer**:

```sh
bh hive fence orphans <hive>                       # read-only; doctor lists them too
bh hive fence orphan-merge <hive> --branch frame/<id>/orphan-<epoch>-<n>
```

`orphan-merge` refuses, with nothing written, unless this host is the local `bh_writer` with all
44 triggers, the remote writer is not ahead, the branch is an orphan on the remote and the merge
raises no conflict. It never resolves a conflict, on `bh_*` or anywhere else, and it never runs
a plain `vc merge`. In one SQL session it merges `--no-ff`, re-stamps the orphan's marks to the
live epoch, clears the foreign-key violation rows and commits `bh: merge frame/…`. That is the
subject `fence_audit`'s history check sanctions. Every statement after the merge is guarded in
SQL on the merge being open, because bd's batch does not stop at a failing statement. It then
publishes through the managed push. A merge that does not land as that exact merge commit is
aborted, and local `main` is hard-reset to its pre-merge head (in server mode after the
forwarder sessions are killed), so nothing the failed batch committed can be pushed. Run it
while the hive is quiet: a write that lands on the writer during a failed merge is discarded
with it. If the merge
conflicts, reconcile the beads by hand on the writer (re-apply the orphan's edits as ordinary
writes), then delete the orphan branch.

## 5. Rollback (R1–R5)

| Step | What happens |
|---|---|
| R1 | **You** stop new adopts for the hive: pause dispatch and do not run `bh host lease adopt`. Then run `bh hive fence rollback <hive>` on the current `bh_writer` holder. The verb refuses on any other host. |
| R2 | The verb reads `floor = max(dolt_history_bh_writer, bh_writer, refs/bh/epoch, placement)` before it drops anything. It refuses a **below-floor** state, where the ref or placement does not name this host at the floor. Run a coexistence adopt (`bh host lease adopt <hive>`), then re-run the rollback. |
| R3 | One commit drops the 44 triggers, `bh_guard_check()`, `bh_write_mark`, `bh_epoch_live` and `bh_writer`. It **keeps** the `dolt_ignore` row. |
| R4 | The commit is published on the managed path: the ref is reserved (its epoch never moves back), then a fast-forward push, then verify. The verb never uses `--force`, `reset-data` or a ref rewind. |
| R5 | The holder drops its `bh_local_ident`. **On every other replica**, run `bh hive fence rollback <hive>` after it pulls. That drops its identity. |

After R1–R5 the legacy model is back in force, and the next adopt mints an epoch above the
floor. Downgrading below 0.23.0 is allowed only after R5 on every replica.

Before C5's push, a rollback is a local reset: `DOLT_RESET('--hard', 'origin/main')` on the
holder, then drop its identity. The verb does this itself when it refuses.

## 5a. Rollout order and mixed versions

The phases are those of the migration spike (`bh-32379` §2) and the ADR's operator rollout
outline:

| Phase | State | Notes |
|---|---|---|
| Φ1 | 0.23.0 installed, no hive cut over | Behaves as 0.22.x: no `bh_writer`, so the lease gate and reserve/verify. Downgrade is free. |
| Φ2 | A hive cut over; `refs/bh/epoch` and placement kept in lockstep by every adopt | Cut over **one hive at a time**, canary hive first, `bh` last. An older replica reads but cannot write `main`: install 0.23.0 on every replica before you pull a cutover to it. |
| Φ3 | `dolt-server` HQ: director placement and session/evidence rows, the receiver still running | The director's revisions are in the receiver's format, so a rollback to the receiver works. Frames dual-write session rows and signed inbox beats. |
| Φ3b | Receiver stopped | Only after the soak. Old adopt and release then fail closed, and a 0.21.3 receiver-mode reader goes stale, so none may remain. |

`host.forward` is rejected by `bh` older than 0.23.0 (the host schema is strict): set it only on
frames already running 0.23.0, and agents on a forwarding frame must use `bh bd`, because a raw
`bd` is not redirected ([forward write path](../FORWARD-WRITE-PATH.md)). The wire release that
carries the hidden verbs is 2.5.0 (`docs/schemas/wire/v2.5.0/`). Placement and failover are in the
[placement runbook](hq-placement-runbook.md); trust and the rollout order are summarised in the
[0.23.0 release note](../releases/hive-writer-partitioning-0.23.0.md).

## 6. Removal

E1 deletes `bh hive fence` once every hive is cut over. After that, a rollback runs §5 by hand:
the same R1–R5 statements (`beadhive.fence_schema.rollback_statements`), committed and pushed on
the managed path.
