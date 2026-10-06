# Runbook — director placement and unattended failover in SQL HQ

> **Status:** operator procedure for 0.23.0 (`bh-16347.5`). The verb it describes,
> `bh hq placement`, is **hidden**. It appears in no `--help` output and no CLI reference, and
> this runbook is its only documentation, like `bh hive fence`
> ([cutover runbook](hive-writer-cutover-runbook.md)). The failover loop is **off by default**.

Design: [ADR — hive writer partitioning](hive-writer-partitioning-adr.md) §1 (placement, design
A) and §4 (failover policy). Library: `beadhive.hq_sql_placement` and
`beadhive.failover_observer` (`bh-a94qw`).

## 1. What placement is

Placement says which frame should write a hive. In `dolt-server` HQ it is one
`hq_live_hive_leases` row per hive. Only a compare-and-swap (CAS) on that row's `revision`
changes it. The CAS runs on the **director credential**, an account that holds `UPDATE` on
that table, `SELECT` on what it verifies, and no other write right. Frames hold `SELECT` only.

Every write gets a fresh revision. A lost CAS is never retried with the same expectation: read
the row again and decide again.

In `git` HQ placement stays the `refs/bh/lease/<prefix>` CAS. Nothing here applies there.

## 2. The director credential

Bind it in an operator settings file, never in a frame's `host.yaml`. The file is refused when
it binds `hq.sql.runtime`.

```yaml
hq:
  sql:
    runtime: null
    placement_writer:
      host: hq.example.net
      port: 3308
      database: beadhive_hq_runtime
      user: director
      server_name: hq.example.net
      ca_file: /etc/ssl/hq.pem
      credential: {config_path: /etc/fnox.toml, profile: hq, key: DIRECTOR}
```

Name the file with `--operator-settings <path>` or `BH_HQ_OPERATOR_SETTINGS`, as for
`bh hq authority`. The file may also carry `authority_writer`; `check` then uses that operator
account to read other accounts' grants.

## 3. The verb

Every action prints JSON and exits 1 on a refusal. `place` and `release` are dry runs unless you
pass `--confirm`. The dry run refuses whatever the CAS would refuse, so a clean preview means the
CAS can only lose a race. Invalid values are refused, never clamped.

| Command | Effect |
|---|---|
| `bh hq placement show <prefix>` | Read-only. The row: frame, host, epoch, revision, `released`, and `source` (`director` or `receiver`). |
| `bh hq placement seed <prefix> --epoch <N>` | Renders the one-time `INSERT` for a never-placed hive. It executes nothing. |
| `bh hq placement place <prefix> --frame <F> --expected-revision <R> [--epoch <N>] [--tenure <T>] [--confirm]` | Places frame `F` on the hive at epoch `N`. |
| `bh hq placement release <prefix> --expected-revision <R> [--confirm]` | Releases the hive to a tombstone at the placed epoch (planned handoff). |
| `bh hq placement check` | Grant and trigger conformance for the director and every frame account. Exits 1 on any finding. |

### Seed a never-placed hive

The director never inserts, so a hive with no row gets one from operator provisioning, once.
Seed it at the hive's current `refs/bh/epoch`, or at 1 when the hive was never fenced:

```sh
bh hq placement seed <prefix> --epoch <N>   # prints {"statement": "INSERT INTO ...", ...}
```

Run the printed statement verbatim (`dolt sql -q`) on the HQ server with the server-local
provisioning account. With a settings file the verb first reads the row and refuses a hive that
is already seeded.

### Place and release

```sh
bh hq placement show <prefix>                                  # note "revision" and "epoch"
bh hq placement place <prefix> --frame <F> --expected-revision <R>            # preview
bh hq placement place <prefix> --frame <F> --expected-revision <R> --confirm  # CAS
```

- **Epoch.** The default is the row's epoch + 1. An explicit `--epoch` must be higher than the
  row's. When `refs/bh/epoch` is ahead of the row, pass the epoch the adopt will carry, so the
  row equals the fence the frame then installs.
- **Tenure.** `--tenure` accepts seconds or `12h`. The default and maximum is 24 h. Expiry is a
  failover hint only, never a write gate.
- **Frame.** The frame must hold an active, uncordoned grant bound to the hive's
  operator-signed policy.

`release` keeps the epoch and writes a tombstone. The next `place` raises the epoch.

| Message | Do |
|---|---|
| `placement moved` / `CAS … lost` | Someone placed meanwhile. Run `show` again and decide again. |
| `… sent but its commit acknowledgment is unknown` | Run `show`. The row is either the candidate revision (won) or not (lost). Never re-issue with the same expectation. |
| `has no placement row` | Seed it first (above). |
| `PLACEMENT: frame … has no active, uncordoned grant` / `… no operator-signed frame policy` | Fix the grant or policy with `bh hq authority`, then place. |
| `placement must raise the epoch` | Pass a higher `--epoch`, or omit it. |

### Conformance

```sh
bh hq placement check
```

The check reads `mysql.user`, `SHOW GRANTS` and `information_schema.triggers`. It needs an
account that can read other accounts' grants (`authority_writer` when the file carries one).
It reports:

- a director account with any write right other than `UPDATE` on `hq_live_hive_leases`, or
  without that right;
- a frame account (from `hq_principal_registry`) with any write right outside its own inbox and
  session tables;
- any trigger on a protected table, any trigger body that names one, and any trigger on a
  frame-writable table that runs DML.

## 4. Unattended failover (host daemon)

The host daemon (`bh host daemon serve`) runs the director's failover loop when the host enables
it. Nothing else runs it, and it is off by default:

```yaml
host:
  daemon:
    failover:
      enabled: true                 # default false
      interval_seconds: 60          # default 60; (0, 3600]
      operator_settings: /etc/bh/director.yaml   # else BH_HQ_OPERATOR_SETTINGS
```

Each tick makes one verified read on the director credential. It reads the signed authority,
every placement row, and each active frame's session staleness by the HQ server's own
`UTC_TIMESTAMP(6)`. A frame fails over only when `min(server staleness, observed window)` is
greater than `failover_after`. The window resets on an unreachable HQ and on any gap between
observations longer than `failover_after / 2`, so keep `interval_seconds` well below that.

- **`failover_after`** is the executor default (60 min) for every frame. The per-role, per-hive
  value on the placement row arrives with `bh-4biq8`.
- **Successor.** The freshest other frame that is active, uncordoned, bound to the hive's policy
  and observed live. Ties break by frame id. If there is none, nothing is placed. Spreading
  placement across executors is `bh-zncqo`.
- **Unobserved frames never fail over.** A frame whose incarnation has no session table
  (`bh-owqdg`) has no server-stamped staleness.
- **Lost CAS.** The loop logs it and decides again on the next tick.

The daemon refuses to start, before it binds a socket, when failover is enabled and the host's
HQ is `git`, no settings file is named, or the file lacks `placement_writer`. It never runs
without the loop it was configured to run. Each failover is logged at `WARNING`
(`director failover placed: <prefix> <dead> -> <successor>`).
