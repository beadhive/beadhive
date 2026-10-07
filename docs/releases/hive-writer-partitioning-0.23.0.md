# Hive writer partitioning in 0.23.0

0.23.0 is the **coexistence minor** of the hive writer partitioning design
([ADR](../design/hive-writer-partitioning-adr.md)). It adds, to every hive, an optional writer
fence kept in the hive's own Dolt data, and, to `dolt-server` HQ, director-written placement and
server-stamped session rows in place of the trusted receiver. It is **dormant until a hive's data
switches it on**: with no `bh_writer` table in a hive's `main`, 0.23.0 behaves as 0.22.x, and
downgrading is free. Nothing cuts a hive over automatically, and no `host.yaml` or fleet-config
key is a phase switch.

## What ships

1. **In-data epoch fence** (`bh-uz46l`). A cut-over hive carries `bh_writer`, `bh_epoch_live`,
   `bh_write_mark` and 44 `bh_*` triggers. The remote's non-fast-forward CAS, epoch retirement by
   foreign key and a local guard trigger enforce it. `fence_audit` detects after the fact
   (`stale_marks`, `epoch_regressed`, `placement_ahead`, and a history check for late writes), and
   `bh doctor` reports it. See [BEADS-SYNC](../BEADS-SYNC.md#the-epoch-fence-beside-the-data-multi-host).
2. **Clock-free writes** (`bh-12hev`). On a cut-over hive "may this frame write?" is the local
   `bh_writer` row, with no clock and no HQ read, so an established writer keeps writing while HQ
   is down. Lease expiry is advisory everywhere. Legacy hives keep a best-effort liveness renewal
   that never refuses a write.
3. **Hidden, temporary cutover verb** (`bh-oarxp`). `bh hive fence cutover|status|rollback|orphans|
   orphan-merge`, documented only in the
   [cutover runbook](../design/hive-writer-cutover-runbook.md) (C1 to C6, R1 to R5), and removed
   once every hive is cut over.
4. **Raw `bd dolt push|sync` lifted on cut-over hives** (`bh-9c9hh`), writer frame only. The
   break-glass forms (`dolt push --force`, `dolt remote reset-data`, `backup restore --force`, any
   `--strategy`, `vc merge`, `conflicts resolve` on `bh_*`) are refused through `bh bd`.
5. **Orphan divert and orphan merge** (`bh-4z3oz`). A superseded writer's managed push diverts to
   `frame/<id>/orphan-<epoch>-<n>`; the writer merges it.
6. **Director placement** (`bh-a94qw`). In `dolt-server` HQ the director writes placement by a CAS
   on its own credential (`hq.sql.placement_writer`, in an operator settings file), with the hidden
   `bh hq placement show|seed|place|release|check|policy` verb
   ([placement runbook](../design/hq-placement-runbook.md)). It retires the 0.22.8 proposal
   resolver for each hive it places. The failover loop in the host daemon
   (`host.daemon.failover.*`) is off by default; it spreads primaries by fewest held
   (`bh-zncqo`), and `bh doctor` warns above `max_primary_spread`.
7. **`failover_after` in data** (`bh-4biq8`). Defaults 60 min (executor) and 30 min (transient),
   executor floor 45 min (warning below 35.4 min, hard bounds refused), held in the
   `hq_live_failover_policy` table. They are never `host.yaml` or fleet-config keys.
8. **Session and evidence rows** (`bh-owqdg`). Server-stamped, per incarnation, with a separate
   renewal loop (`python -m beadhive.heartbeat_sender renew`), a data-switched reader,
   `evidence_ttl_s` 900 and claim-time audit stamps. `BH_FRAME_HEARTBEAT` is ignored on a switched
   frame. See [HQ](../HQ.md#session-rows).
9. **Forward write path, option A** (`bh-g7dlo`): `host.forward` points a non-primary executor's
   bd at the primary's hive server. See [FORWARD-WRITE-PATH](../FORWARD-WRITE-PATH.md).
10. **State/work pairing** (`bh-55vvh`), opt-in per hive and off by default: `bh hive policy`,
   `bh work backup`, and failover reclaim driven by backup presence
   ([WORK](../WORK.md#statework-pairing--backups-before-state-condition-18)).
11. **Canary version ranges**: `BH_FENCE_CANARY_DOLT_RANGE` and `BH_FENCE_CANARY_BD_RANGE`
    ([CONFIGURATION](../CONFIGURATION.md#fence-canary-ranges)). They govern the test canary only,
    not frame release grants (release ranges, `bh-vfrem`, are not in 0.23.0).
12. **Wire release 2.5.0** carries the new hidden verbs and the authority flags
    `--operator-settings`, `--max-duration` and `--min-remaining`.

`hq.sql.liveness` is **deprecated** (still read; see [CONFIGURATION](../CONFIGURATION.md)).

Advanced scheduling beyond fewest-primaries spreading and the observed-window failover rule is
deferred. The operator may move it to external quorum tooling such as ZooKeeper.

## Rollout order

1. **Φ1: install 0.23.0 on every host.** No hive is cut over, so behaviour is 0.22.x. Install it
   on every replica **before** any cutover reaches it: an older replica of a cut-over hive can read
   but not write `main`.
2. **Condition 11 canary**, for a GitHub-hosted remote, then **Φ2: cut over one hive at a time**,
   a low-traffic canary hive first and `bh` last, on the current holder, with the other hosts
   published. Merge any orphans on the writer afterwards.
3. **Φ3 (`dolt-server` HQ): provision the director credential, the failover-policy table and the
   session/evidence tables, then soak with the receiver running.** The director's revisions are in
   the receiver's format, so a rollback to the receiver works. Only after the soak, stop the
   receiver (Φ3b), and then unset `BH_FRAME_HEARTBEAT=advisory` everywhere.
4. **Only then admit additional executor frames** and enable `host.forward` on them.

`host.forward` is **rejected by `bh` older than 0.23.0** (the host schema is strict), so set it
only on frames that already run 0.23.0. Agents on a forwarding frame must use `bh bd`: a raw `bd`
is not redirected and writes the frame's own replica, whose write guard refuses it.

## Trust delta

- **Forwarders hold a Dolt login on the primary's hive server.** The residual trust, verbatim
  from `beadhive.hive_forward.RESIDUAL_TRUST`:

  > Forwarding frames hold a Dolt login on the primary's hive server. Their grants are
  > table-scoped (DML on bd's tables and dolt_ignore, SELECT on the fence tables, SELECT and
  > INSERT on bh_write_mark, no DOLT_COMMIT right), which closes the stale-write and
  > identity-spoof paths the M13 spike measured. Table-scoped grants cannot stop a forwarder
  > issuing SET GLOBAL or SET PERSIST, or editing any bead row of the hive. A forwarder is
  > trusted not to; detection is the globals watchdog plus fence_audit's history check.

- **A session-level `SET` is not preventable** on Dolt 2.3.5: any login can set a global, and
  2.4.0 and 2.4.1 show no change. Option A narrows and detects (the globals watchdog and
  `fence_audit`'s history check); it does not prevent. Option B (no Dolt login for executors) is
  dormant as `bh-453vk`.
- **Account hardening is mandatory**, because it replaces the one guarantee the receiver gave that
  grants do not: frame accounts are `'<principal>'@'<frame address>'`, TLS is required, the server
  runs with a read-only `DOLT_ROOT_PATH`, and no hive database is co-hosted on the HQ server.
- **The director credential** is a new operator-held write right on placement (`UPDATE` on
  `hq_live_hive_leases` and the failover-policy table only). It lives in an operator settings file
  and never on a frame that is a runtime frame.
- **Raw `bd dolt push --force` and other raw bypasses** stay undetectable before the fact, as
  today; `fence_audit` reports them afterwards. A non-writer writing at the live epoch is not
  detectable from data, which is why forwarder grants are table-scoped.
- **Expiry never gates a write.** An HQ outage blocks handoff, not writes.
