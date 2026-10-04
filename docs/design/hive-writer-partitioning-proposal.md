# Hive writer partitioning — epoch in the data, receiver out

**Status:** proposal, 2026-10-04; amended 2026-10-04 before kickoff with probe evidence (see
[Pre-kickoff probe evidence](#pre-kickoff-probe-evidence)). Not an ADR. The spike molecule's
decision bead produces the ADR.
**Seat:** planning, with the operator.
**Amends (if accepted):** [multi-host-model-adr.md](multi-host-model-adr.md) Amendment 1 §§1–4;
[frame-dolt-server-hq-mode-adr.md](frame-dolt-server-hq-mode-adr.md) binding amendment `bh-v0k3i`
(trusted receiver). See [Filed beads](#filed-beads).

## Summary

Beadhive already partitions writes by hive. Each hive has one primary (the host lease at
`refs/bh/lease/<prefix>` in HQ) and a writer epoch (the fence `refs/bh/epoch` beside
`refs/dolt/data`). This proposal keeps that partitioning model. It changes three things about
how the model is enforced:

1. **Move the fencing token into the hive's own Dolt data.** A `writer` row holding
   `{frame, epoch}` is committed on `main`. The remote's non-fast-forward rejection fences a
   stale writer's direct push, and a foreign key that retires the old epoch stops a stale
   writer that pulls first and then pushes. This closes the two gaps Amendment 1 §2 records as
   unfixable: the CAS-to-push race, and raw `bd dolt push` bypassing the fence. `bd dolt push
   --force` stays break-glass.
2. **Make safety independent of clocks.** The epoch and the remote CAS decide who may write.
   Lease time decides only *when to fail over*. A primary no longer degrades to read-only
   because a cached lease expired while HQ was unreachable.
3. **Remove the trusted receiver.** Frame liveness becomes a server-timed row each frame
   writes under its own SQL grant. Conformance evidence is measured on its own schedule. HQ
   keeps only operator-signed authority and placement. Dispatch checks all of it at read time,
   which it already does.

## Context

### What exists

| Piece | Where | Role today |
|---|---|---|
| Host lease | `host_lease.py`, `host_lease_contracts.py`; `refs/bh/lease/<prefix>` in HQ | Who *should* be primary. TTL 30 min, renewal every 5 min, `epoch+1` on adopt. Expiry is checked against the local wall clock (`HostLease.is_expired`). |
| Epoch fence | `host_fence.py`; `refs/bh/epoch` beside `refs/dolt/data` | `reserve_managed_push` CASes `{epoch, host_id, seq}`, then `bd dolt push` runs, then `verify_managed_push` checks the reservation afterwards. |
| Write guard | `guard.py` `guard_primary` / `guard_claim_epoch`; `frame_eligibility.py` `GuardedClaimSession` | Refuses bh write verbs when this host is not the live primary. Claims record `epoch`/`host_id` (`claim_authority.py`). |
| Frame heartbeat | `hq_framelease_contracts.HeartbeatLease`, `heartbeat_report.py` | A signed envelope that embeds full conformance. The renewal time is the sender's clock. |
| Trusted receiver | `hq_sql_receiver.SqlTrustedReceiver` | Validates inbox envelopes and writes receipts, floors and hive-lease CAS results. It has no entrypoint in `src/` and is deployed separately. |
| Eligibility | `frame_eligibility.decision_for` → `plane.read_eligibility` | Reads receipts and authority at dispatch time and rereads them at claim time. |

### What hurts

- **The fence is not atomic with the data.** bd forces `core.hooksPath=/dev/null` and owns a
  transient data ref, so `refs/bh/epoch` and `refs/dolt/data` cannot share one push
  (Amendment 1 §2, `bh-tfapu` evidence). A takeover between reserve and push can land a
  stale host's data. A raw `bd dolt push`, or bd's own auto-push, bypasses the fence entirely,
  so bh refuses `bh bd dolt push|sync` even on the primary.
- **Safety depends on clocks.** A primary whose cached lease expires while HQ is unreachable
  goes read-only (Amendment 1 §4). That is correct only because time is doing the fencing.
- **Liveness and conformance share one path.** On the deployed factory frame, measurement
  takes about 210 s and the heartbeat TTL is 300 s (infra
  `docs/beadhive-factory-recovery-2026-10-03.md`). One slow probe expires a healthy frame.
- **The receiver is an availability anchor.** Every accepted heartbeat and every hive-lease
  CAS for a frame waits on it. In the live deployment it runs as a LaunchAgent on the
  operator's laptop.
- **Upstream multi-writer bugs.** gastownhall/beads
  [#4796](https://github.com/gastownhall/beads/issues/4796) (colliding child IDs block sync),
  [#4657](https://github.com/gastownhall/beads/issues/4657) (`--claim` is not a hard CAS),
  [#5157](https://github.com/gastownhall/beads/issues/5157) (concurrent auto-push contention) and
  [#5657](https://github.com/gastownhall/beads/issues/5657) (label read-then-write). All of
  them get worse the more writers a hive has. Upstream has no multi-writer or quorum design.
  [#6051](https://github.com/gastownhall/beads/issues/6051) (BDP) addresses federation reads,
  not write arbitration.

### Why partition rather than multi-leader or quorum

| Approach | Examples | Verdict |
|---|---|---|
| Multi-leader plus merge | beads today, CouchDB, CRDTs | Fine for edits that commute. Wrong for claims and counters. |
| Leaderless quorum (W+R>N) | Dynamo, Cassandra | Doesn't fit a commit graph. |
| Consensus group per partition | CockroachDB/TiKV ranges | Gold standard. Needs at least 3 independent voters per partition. |
| **Single leader per partition plus epoch fencing** | Kafka leader epochs, Vitess primaries, Dolt cluster roles | **Chosen.** It already matches Beadhive's model. |

Bead write volume is small. The reasons to partition are fault isolation, locality, and
removing push races, not SQL throughput.

## Proposal

### 1. Placement: one writer per hive, decided in one place

- HQ holds a **placement** record per hive: `{hive, frame, epoch}`. It is changed only by CAS
  at a single linearizable point: the existing HQ lease ref in `git` mode, or one protected row
  in `dolt-server` mode.
- Placement is stable. Each frame is primary for a fixed set of hives. Handoffs are rare and
  deliberate (release, drain) or triggered by failover.
- Two places that both "vote" are not a quorum. Use one CAS authority. Graduate to a
  three-voter store (etcd or Consul) only if unattended failover must survive losing a node.

### 2. Fencing: the epoch lives in the data

Each hive database gains three tables:

- `bh_writer`: one row, `{frame, epoch, placed_at}`. Only an adopt writes it.
- `bh_epoch_live`: one row, the current epoch.
- `bh_write_mark`: `{id uuid, epoch}`, with `epoch` a foreign key to `bh_epoch_live`. The write
  guard (below) inserts one row on every guarded `main` write. The writer may prune rows at any
  time; each row has its own key, so concurrent writes never contend on one row.

```text
adopt(hive, frame):
  1. CAS HQ placement            {old_frame, e} → {frame, e+1}          (decides who)
  2. pull main; in one commit: bh_writer := {frame, e+1};               (enforces it)
     delete bh_write_mark rows and bh_epoch_live row for e; insert bh_epoch_live(e+1); push
     non-fast-forward → pull, re-check placement is still ours, retry; otherwise abandon
```

From the moment step 2 lands on the remote:

- **A direct push from the old writer is rejected as non-fast-forward.** This holds for
  managed pushes, raw `bd dolt push` and bd auto-push alike.
- **A pull-then-push from the old writer is stopped by epoch retirement.** Non-fast-forward
  rejection alone does not cover this: if the old writer pulls, the merge puts the epoch bump
  under its stale commits, and the following push is a fast-forward. The probe reproduced
  exactly that. With epoch retirement, the stale commits carry `bh_write_mark` rows for the
  retired epoch, so the merge produces a foreign-key violation and Dolt refuses to commit it.
  The push stays rejected. Whether bd's own pull paths respect that refusal is the main
  question for `bh-vje85`.
- The old writer sees `bh_writer.epoch > held`. It must not push `main` again. bh pushes its
  unpublished commits to `frame/<id>/orphan`. The new writer merges them deliberately and
  re-stamps or drops their marks as part of that merge.
- **`bd dolt push --force` is break-glass.** It overwrites the remote, and nothing in the data
  can stop it. GitHub cannot protect `refs/dolt/data`. This is no worse than today, where raw
  pushes bypass `refs/bh/epoch` entirely. The spikes state what bh can detect afterwards.
- The old writer never edits `bh_writer`, so the merge applies a one-sided change and has
  no conflict to resolve. Two simultaneous adopters at the same epoch produce a both-changed
  conflict, and `bd sync` halts rather than resolving it. Step 1's CAS already prevents that
  case.

**A local write guard stops raw bd on stale replicas.** A Dolt trigger on bead tables refuses
writes on `main` unless `bh_writer.frame` equals this node's identity, which is held in a
`dolt_ignore`d local table. The same trigger inserts the `bh_write_mark` row. Non-`main`
branches are unrestricted. After a stale writer pulls the epoch bump, every further local
`main` write fails in SQL, whether it came from bh or raw bd. Triggers do not fire when a pull
merges, so the guard cannot block the merge that delivers the bump.

This answers Amendment 1's objection to a fence inside the data ("can never be resolved by a
cell-level merge policy into something both hosts think they hold") with three rules:
`bh_writer` only ever increases; only adopt writes it; and its conflicts are never
auto-resolved. That last rule means `hive_sync`'s `ours`/`theirs` strategies must exclude
`bh_writer`, and no default bd path may commit a merge that carries `bh_write_mark`
foreign-key violations. The spikes must prove these rules hold or report NO-GO.

### 3. Safety without clocks; time only drives failover

- A writer keeps writing for as long as it holds the newest epoch. It does not need HQ to
  renew in order to stay safe. An HQ outage blocks *handoff*, never writes.
- Failover: when a primary's session row has been stale longer than `failover_after` (long,
  for example 30–60 min, per role), the director or operator CASes placement to a new frame.
  That frame runs adopt. If the old primary was only partitioned, it is fenced on its next
  push.
- `HostLease.expires_at` stops being a write-safety input. It survives only as a failover
  hint, or is removed.

### 4. Writes from frames that are not primary

Per hive, one of:

- **Forward:** connect bd to the primary's Dolt server over the LAN. The primary is then the
  only place claims are granted, so bd's claim, lease, `heartbeat` and `reclaim` work as
  designed ("enforceable only on the node that granted them").
- **Branch:** commit to `frame/<id>` and push it. No one else writes that branch, so there is no
  merge contention. On git remotes, though, every branch lives in the single `refs/dolt/data`
  manifest, so a branch push still competes with `main` pushes for that ref's
  compare-and-swap. The spike measures that cost. The primary merges the branch into `main`.
  This is the "proposal" flow done natively in Dolt, with no publication service.

Single-writer `main` makes #4796 (child-ID collisions) go away by construction and reduces push
contention (#5157) to retries on the shared manifest ref. #4657 still matters for agents running
concurrently on the primary, and `work_next.claim_won` already works around it by reading the
bead back.

### 5. Liveness and conformance without a receiver

- **Session:** each frame's SQL principal can write only its own `frame_<id>_session` row:
  `UPDATE … SET renewed_at = NOW()`. The server clock stamps it. No envelope, no replay
  window, no skew check. The table is `dolt_ignore`d, so it creates no history.
- **Evidence:** a separate conformance job, run on a timer and after relevant changes, writes
  `frame_<id>_evidence {release_digest, profile, status, measured_at, expires_at}`. Signing it
  is optional, for audit, and is verified at read time. A hive's health is scoped to that
  hive.
- **Authority:** operator grants stay signed and operator-writable only. The operator key is
  off the frame, so that signature means something.
- **Eligibility at read time:** grant valid ∧ session fresh by server `NOW()` ∧ evidence
  unexpired ∧ evidence release matches the grant ∧ placement names this frame (for writes).
  `frame_eligibility` already rereads at its own read point. Only its inputs change.
- **Why the receiver adds little:** the frame's signing key and its SQL credential live on
  the same host. A compromised frame can sign a false heartbeat as easily as it can write
  one. Database grants already establish which principal wrote a row, and dispatch already
  re-qualifies the frame at read time.
- **Open question:** `git` HQ mode has no database authentication, so signed envelopes
  verified by the reader may still be needed there. Verifying at read time would replace the
  receiver's copy step in that mode too. Scoping execution frames to `dolt-server` HQ is a
  valid alternative.

### 6. What gets deleted or narrowed (if GO)

| Today | After |
|---|---|
| `refs/bh/epoch` and `reserve_managed_push` / `verify_managed_push` | Replaced by `bh_writer` plus the remote's native CAS. The fence ref is kept read-only for one release, for mixed fleets. |
| Refusal of `bh bd dolt push` / `sync` on the primary | Lifted. bd auto-push becomes safe. |
| `HostLease.expires_at` as a write gate | A failover hint only. |
| `SqlTrustedReceiver`, inbox tables, receipts/floors | Deleted for `dolt-server` HQ. |
| `HeartbeatLease` with embedded conformance | A session row plus a separate evidence row. |
| `legacy_lease_policy` path | Retired once every hive has a `bh_writer` row. |

## Risks and unknowns (the spikes answer these)

1. Is a Dolt push non-fast-forward rejection a true CAS for every remote type in use:
   `git+ssh` (`refs/dolt/data`), `file://`, and remotesapi? Does any bd path force-push?
   *(Probe: yes for `file://` and `git+file://`. bd has a user-reachable `--force` push.)*
2. Do Dolt triggers fire on merges, pulls or `bd import`? A trigger that blocks the merge
   carrying the epoch bump would wedge the fence. *(Probe: not on a Dolt CLI merge. bd links
   its own Dolt build, which must be re-tested.)*
2a. Does any default bd path commit a merge with foreign-key violations, through
   `dolt_force_transaction_commit`, the auto-resolver, or `--strategy`? If so, epoch retirement
   does not hold under bd.
3. Can `bd sync` or `hive_sync --strategy ours` be made unable to resolve `bh_writer` in a
   stale writer's favour?
4. What happens to writes a partitioned old primary made before it learned of the new epoch?
   Are they diverted to an orphan branch on the managed path? What does raw `bd sync` do
   with them?
5. Who writes HQ placement without a receiver in `dolt-server` mode: the director with
   authority credentials, or a stored procedure? And what is the HQ-down behaviour?
6. Mixed-version fleets and a cutover of the live factory frame (epoch 2), including rollback.

## Pre-kickoff probe evidence

Run 2026-10-04 in a scratch directory against Dolt CLI 2.3.5 and bd 1.3.0, before the spike
molecule was kicked off. It used plain Dolt clones, not bd, so it informs the spikes and
proves nothing about bd's behaviour.

| Probe | Result |
|---|---|
| Stale writer pushes after the epoch bump (`file://`) | Rejected as non-fast-forward. |
| Same, on a `git+file://` remote | Rejected. Dolt pushes `refs/dolt/data` with `--force-with-lease=refs/dolt/data:<expected>` (git trace2), a true CAS. |
| Non-writer inserts on `main` with the guard trigger | Refused: `SIGNAL 45000`. |
| Non-writer inserts on a `frame/<id>` branch | Allowed. |
| Non-writer pulls a guarded insert through a true (non-fast-forward) merge | Merge succeeds. Triggers do not fire on merge. |
| **Stale writer: write, push rejected, `dolt pull`, `dolt push`** | **Push succeeds. The stale row lands on `main` after the bump.** This is the hole that epoch retirement closes. |
| Same, with `bh_write_mark` → `bh_epoch_live` FK and adopt retiring epoch e | `dolt pull` stops with a constraint violation on `bh_write_mark`. The push is still rejected and remote `main` is unchanged. |
| Trigger whose `INSERT … VALUES` uses a scalar subquery | Dolt bug: "unable to find field with index 5 in row of 4 columns". Assigning to a variable first works. |

bd 1.3's binary contains `DOLT_PUSH('--force', …)`, `dolt_force_transaction_commit`,
`dolt_allow_commit_conflicts` and `DOLT_CONFLICTS_RESOLVE('--ours'|'--theirs', <table>)`, and
links Dolt `v0.40.5-0.20260715172757-a6690826d767` rather than the 2.3.5 CLI. `bh-vje85` and
`bh-sieai` carry these as explicit checks.

Guard and mark shape used by the probe:

```sql
create table bh_epoch_live(epoch int primary key);
create table bh_write_mark(id varchar(64) primary key, epoch int,
  foreign key (epoch) references bh_epoch_live(epoch));
-- one BEFORE trigger per event on each bead table:
create trigger guard_ins before insert on issues for each row begin
  if active_branch() = 'main' then
    if coalesce((select frame from bh_writer where id = 1), '')
       <> coalesce((select frame from bh_local_ident where id = 1), '?') then
      signal sqlstate '45000' set message_text = 'bh not the writer for main';
    end if;
    set @bh_e = (select epoch from bh_writer where id = 1);
    insert into bh_write_mark values (uuid(), @bh_e);
  end if;
end;
-- adopt, one commit:
update bh_writer set frame = 'B', epoch = 2 where id = 1;
delete from bh_write_mark where epoch = 1;
delete from bh_epoch_live where epoch = 1;
insert into bh_epoch_live values (2);
```

## Validation approach

Build a fault-injection fixture from existing parts:

- `stateful_fixtures._bound_concurrent_dolt_servers` and `_sandbox_shared_server`
- `file://` bare remotes, as in `test_host_fence_int.py`
- `harness.processes.process_context`, as required by `TEST_PROCESS_POLICY.md`
- markers `integration` plus `dolt_server`; `tests/conftest.py` stays the only conftest

The fixture drives these scenarios:

| # | Scenario | Pass condition |
|---|---|---|
| 1 | Planned handoff A→B while A is idle | B's epoch bump lands. A's next push is rejected. A's local `main` writes fail. |
| 2 | Handoff while A is mid-write | A's data either lands before the bump or is rejected. Never after the bump. |
| 3 | A partitioned (remote unreachable), failover to B, A rejoins | A is fenced on reconnect. A's unpublished commits survive on `frame/A/orphan`. |
| 4 | Two adopters race for the same epoch | Exactly one placement CAS wins. A loser that reached step 2 is rejected and abandons. |
| 5 | Stale A runs raw `bd dolt push`, `bd dolt pull` then `bd dolt push`, `bd sync`, and auto-push | Push is rejected, the trigger blocks the write, or the merge stops on an FK violation. Stale writes never land on `main` after the epoch bump. |
| 6 | `hive_sync --strategy ours` on a stale replica | `bh_writer` is never reverted. |
| 7 | HQ unreachable for longer than the lease TTL | The current primary keeps writing. Handoff is refused. |
| 8 | A non-primary forwards a claim, and a non-primary pushes a branch | One claim winner. The branch merges into `main` through the primary. |
| 9 | Frame killed; session goes stale; reclaim | Placement moves after `failover_after`. In-progress beads are reclaimed exactly once. |
| 10 | Conformance probe slower than the session TTL | The session stays fresh. Expired evidence alone blocks new execution. |

Each scenario records the remote head, the `bh_writer` row and the node-local guard state
before and after, so a failure names the exact step that let a stale write through.

## Filed beads

Spike molecule `bh-qlgmm`, filed from
[hive-writer-partitioning-spike-molecule.yaml](hive-writer-partitioning-spike-molecule.yaml).
It is gated: `kickoff=pending`.

| Bead | Spike | Depends on |
|---|---|---|
| `bh-eybn7` | Multi-frame Dolt fault-injection fixture | — |
| `bh-cvk70` | HQ placement CAS authority and failover policy | — |
| `bh-wtsrc` | Receiver removal: session rows, evidence, read-time eligibility | — |
| `bh-vje85` | In-data epoch fencing (scenarios 1–6) | `bh-eybn7` |
| `bh-sieai` | Local write guard and non-primary write paths, with bd claims | `bh-eybn7` |
| `bh-jbb6r` | End-to-end scenario run plus randomized interleavings | `bh-vje85`, `bh-sieai`, `bh-cvk70` |
| `bh-32379` | Migration map and cutover plan | `bh-vje85`, `bh-sieai`, `bh-cvk70`, `bh-wtsrc` |
| `bh-pr889` | DECISION: the ADR, then replan on GO | all of the above |

No implementation beads are filed before the decision's verdict.
