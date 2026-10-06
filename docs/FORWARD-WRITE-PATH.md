# Forward write path, option A (bh-g7dlo)

ADR [hive-writer-partitioning-adr.md](design/hive-writer-partitioning-adr.md) §3, condition 16
and Decision 6, as amended on 2026-10-06 by the M13 spike
([bh-uhx2r](spikes/bh-uhx2r-forward-rpc-option-b.md)). Server hardening (watchdog, read-only
root, templates) is in [DOLT-SERVER-HARDENING.md](DOLT-SERVER-HARDENING.md).

Execution frames that claim, create or close beads on a cut-over hive point their bd at the
**current primary's** hive `dolt sql-server`. Claims are then granted in one place, so bd's claim,
heartbeat and reclaim work as designed (`bh-sieai` E8). A forwarder to a demoted primary fails
closed. Option A **narrows and detects**; it does not prevent (see [Residual trust](#residual-trust)).

Both sides are **opt-in per frame**, and every default below is configurable.

## The primary's side

Set `host.forward.serve.enabled: true` on the frame whose hive server takes forwarders. The
server itself is provisioned from `deploy/dolt/hive-server.yaml.example` (LAN only, TLS
required, `max_connections: 100`) and runs with a read-only `DOLT_ROOT_PATH`
(`beadhive-dolt-hive.service.example`).

### Per-frame accounts with table-scoped grants

```sh
# A new account: the password comes from stdin (or BH_FORWARD_NEW_PASSWORD), never argv.
bh hive forward provision <hive> --account "'fwd-e1'@'10.0.0.21'" --password-stdin
# Re-run at any time: it regrants from the current table list (bd added a table) and narrows.
bh hive forward provision <hive> --account "'fwd-e1'@'10.0.0.21'"
bh hive forward revoke <hive> --account "'fwd-e1'@'10.0.0.21'"
```

Each forwarding frame gets its own account, `'<principal>'@'<frame address>'`, created
`REQUIRE SSL`. Provisioning refuses a shared login (a root, operator or Dolt system login, or a
principal already pinned to another address) and an unpinned host (`%`, a pattern). The
password crosses only as its `mysql_native_password` hash.

The grants are **table-scoped, never database-wide** (M13 E2–E4):

| Table | Privileges |
|---|---|
| every bd base table (`SHOW FULL TABLES`), and `dolt_ignore` | `SELECT, INSERT, UPDATE, DELETE` |
| `bh_writer`, `bh_epoch_live`, `bh_local_ident` (and any other `bh_*`) | `SELECT` |
| `bh_write_mark` | `SELECT, INSERT` (the guard trigger's mark insert runs as the invoker) |

There is no database-level grant, so a forwarder has no `DOLT_COMMIT`, `DOLT_MERGE` or
`DOLT_RESET` right. bd's create, claim and close still return 0: bd's post-write commit prints
`post-tx dolt commit failed … (data already committed; change rides the next dolt commit)` and
the write rides the primary's next managed push. Forwarded writes run under the primary's guard
and identity, so their marks are stamped at the primary's epoch and `fence_audit` stays clean.

Why: a forwarder with `ALL` on the hive database lands a stale write with no global at all, and
re-stamps its marks so `fence_audit` cannot see it (E2). Database-wide DML lets it rewrite
`bh_local_ident` and become a second claim-granting writer that nothing detects (E3). The
table-scoped shape closes both.

### Conformance

`bh hive forward check <hive>` and `bh doctor` (section *Forward write path*) report, on a
serving frame, for each cut-over hive in server mode:

- the watched globals: `dolt_force_transaction_commit=0`, `dolt_transaction_commit=0`,
  `read_only=0`, `max_connections=100` (`host.forward.serve.watched` / `unwatched` change them);
- the read-only `DOLT_ROOT_PATH` (`host.forward.serve.root_path`; unset is reported as "not
  checked");
- grant conformance: every account that is not an operator (`host.forward.serve.operators`,
  default `root`, `watchdog`) or a Dolt built-in is a forwarder, and any database-wide,
  server-wide, `ALL`, grant-option, fence-write or other-database grant, an unpinned host, a
  shared principal or a missing `REQUIRE SSL` is a finding.

`check` exits 1 on any finding. Provisioning runs the same check on its own result and refuses
if the account is still wider than the shape.

### Quiesce before a divert reset

Before a demoted primary resets to the remote head (`DOLT_RESET --hard`, the managed rejoin),
bh kills every forwarder session on its hive server. An in-flight forwarded transaction is then
refused (the client sees a lost connection) rather than acknowledged and silently dropped by the
reset (M13 E5). Operator logins and bd's own login are never killed. `bh hive forward quiesce
<hive>` does it on demand. `host.forward.serve.quiesce_before_reset: false` or
`BH_FORWARD_QUIESCE=off` turns it off.

A session that reconnects between the kill and the reset can still be acknowledged and dropped;
forwarders read back what they wrote (bh's claim path already does, `claim_won`) and treat a
missing row as not done.

## The forwarder's side

Set `host.forward.enabled: true` and one endpoint per primary frame:

```yaml
host:
  forward:
    enabled: true
    endpoints:
      factory-1:                       # the primary's frame id, as placement names it
        host: 10.0.0.10
        port: 3307
        database: bh                   # the hive's bd database
        user: fwd-e1                   # this frame's principal on that server
        tls_mode: required
        server_name: 10.0.0.10
        ca_file: /etc/beadhive/forward/ca.pem
        credential: {config_path: /etc/beadhive/fnox.toml, profile: fleet, key: FWD_E1_PW}
```

```sh
bh hive forward point <hive>    # resolve the placed primary, preflight, record
bh hive forward status <hive>
bh hive forward stop <hive>
```

`point` reads the hive's cached placement (this frame's HQ clone, the guard's own read) and
picks `endpoints[<placed frame>]`. Over the forwarder's own TLS login it reads the target's
`bh_local_ident` and `bh_writer`, and refuses unless both name the placed frame at the placed
epoch. A demoted primary, an incomplete adopt, a stale placement cache or an unreachable server
is a refusal, recorded and reported by `status` and `bh doctor`.

From then on every bd that bh runs in that hive (`bh bd …`, `bh work …`) gets bd's server
environment for the target: server mode, TLS with the endpoint's CA (`SSL_CERT_FILE`), the
frame's principal and password (from fnox, or `BH_FORWARD_PASSWORD`). bd's tracked
`metadata.json` is never edited; the marker is git-private (`<git common dir>/bh/forward.json`)
and holds no secret. When the cached placement moves, the next bd bh runs re-points first. While
forwarding is refused bh does not run bd for the hive at all (fail closed). State push and pull
are the primary's job, so bh skips them in a forwarded hive. A raw `bd` that bypasses bh writes
this frame's own replica, whose write guard refuses: it is not `bh_writer`.

A HOST file that carries `host.forward` is refused by bh older than 0.23.0 (strict schema), so
set it only on frames already running 0.23.0.

## Residual trust

For the 0.23.0 release note (M11, `bh-qucma`); the same text is
`beadhive.hive_forward.RESIDUAL_TRUST`:

> Forwarding frames hold a Dolt login on the primary's hive server. Their grants are
> table-scoped (DML on bd's tables and dolt_ignore, SELECT on the fence tables, SELECT and
> INSERT on bh_write_mark, no DOLT_COMMIT right), which closes the stale-write and
> identity-spoof paths the M13 spike measured. Table-scoped grants cannot stop a forwarder
> issuing SET GLOBAL or SET PERSIST, or editing any bead row of the hive. A forwarder is
> trusted not to; detection is the globals watchdog plus fence_audit's history check.

Option B (a per-hive bh RPC service, so executors hold no Dolt login) is feasible and dormant as
`bh-453vk`; the M13 record lists when to revisit it.

## Evidence

- `tests/test_hive_forward.py`: grant shape, conformance, provisioning, quiesce, target and
  preflight refusals, the marker, the bd environment, doctor (fakes, no Dolt).
- `tests/test_hive_forward_int.py` (`dolt_server`): private scratch Dolt servers with throwaway
  TLS certificates. Provisioning and conformance against real grants; bd create, claim and close
  through a narrow forwarder login over TLS; a claim race from several forwarder accounts with
  one winner per round; a demoted primary refused; an in-flight forwarded transaction refused
  by the quiesce before the reset.
