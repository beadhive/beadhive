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
| `bh hq placement policy [<prefix>] [--failover-after <ROLE=D,...>] [--executor-floor <D>] [--confirm]` | Shows or sets the failover policy (section 6). |

`place` also takes `--failover-after executor=75m,transient=40m`, which sets the hive's overrides
in the same transaction as the CAS. A refused value refuses the placement too.

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

**Cause.** Every `place` records why it placed, in the same CAS. An operator `place` is always
`planned`: the adopting frame reclaims no claims (M14 D5c). Only the failover loop (§4) writes
`failover`, and there is no flag to set it by hand. `show` prints it as `"cause"`. It is `null`
for a seed, a release, a receiver-written row, or a row placed before 0.23.0. See §5 for how
frames read it.

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

- a director account with any write right other than `UPDATE` on `hq_live_hive_leases` and
  `INSERT`/`UPDATE`/`DELETE` on `hq_live_failover_policy`, or without the first;
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

- **`failover_after`** is read per hive and role in the same verified read (section 6). The
  signed authority carries no role, so every frame counts as an executor (the longest window).
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

## 5. The placement cause and mixed versions

The adopting frame needs to know whether a placement was a failover. Only a failover adopt
reclaims the dead frame's claims in its bump commit (`bh.reclaim.failover.mode`, M3,
`bh-4z2rx`). The director writes the cause into the row's `request_id` column, in the same
`UPDATE` as the placement (`bh-16347.6`). It never goes in `lease_json`.

- **Why that column.** The receiver, the 0.22.8 bridge and every 0.22.x reader parse
  `lease_json` strictly: exactly `{authority, lease}`, the five-field lease, and an authority
  equal to the frame's grant. `request_id` is `CHAR(36)`. Before 0.23.0 the director filled it
  with a random UUID that nothing read. The receiver only looks a prior row's `request_id` up
  in its inbox, and a director row is absent there either way. A Φ3 rollback to the receiver is
  unchanged.
- **The token.** `request_id` is a version-8 UUID: a digest of the hive prefix, the row's fresh
  revision and the cause. It is bound to that one revision, so it cannot carry over to a later
  row. A frame trusts it only on a director row, where the placement witness verifies.
- **Mixed versions fail safe.** A frame that does not read the cause (0.22.x, or a 0.23 build
  before `bh-16347.6`) resumes a director placement with the kind unknown. It reclaims nothing,
  which is the pre-0.23.0 behaviour. The same holds for a row without a cause. Unknown never
  rewinds a claim: the D5b sweep or the manual runbook (D10) handles it.
- **Exactly once.** The reclaim rides in the adopt's single bump commit. A re-run of the same
  adopt stops at its data check once the bump has landed, so it never reclaims twice.
- **The adopt reads the cause from the row it resumes.** If the row moves between that read and
  the adopt, the adopt refuses before anything is written. Re-run it.

## 6. Failover policy (`failover_after` and the executor floor)

`failover_after` is per role and per hive, and it lives in HQ data (ADR §4, `bh-4biq8`). It is
never a `host.yaml` or fleet-config key, so changing it never moves the config head and never
needs an authority renewal.

| Setting | Default | Scope |
|---|---|---|
| `executor` | 60 min | a hive, or `*` (the fleet default) |
| `transient` | 30 min | a hive, or `*` |
| `executor_floor` | 45 min | `*` only |
| `viewer` | never placed, never fails over | not settable |

The values live in `hq_live_failover_policy`, one row per `(scope, setting)` with whole
`seconds`. They do not live in the placement row itself. Every older reader of
`hq_live_hive_leases` is strict: the receiver and 0.22.x readers refuse any extra key in
`lease_json`, compare `authority` by equality, and insert the row by position. A separate table
keeps the placement row byte-for-byte what they expect, so a Φ3 rollback still works. The
`hq_live_` name keeps the table under the runtime database's `dolt_ignore` rule, so it never
lands in a commit.

### Provision it once

```sh
bh hq placement policy    # on an unprovisioned HQ, prints "provision": {"statements": [...]}
```

Run the printed `CREATE TABLE` and `GRANT` with the server-local provisioning account, replacing
`<host>` with the director account's host. Until the table exists, the code defaults apply.

### Show and set

```sh
bh hq placement policy                                     # the fleet defaults and the floor
bh hq placement policy <prefix>                            # one hive's effective values
bh hq placement policy <prefix> --failover-after executor=75m          # preview
bh hq placement policy <prefix> --failover-after executor=75m --confirm
bh hq placement policy <prefix> --failover-after executor=default --confirm   # clear
bh hq placement policy --executor-floor 40m --confirm      # the fleet floor
```

### Validation: refused, never clamped

Values are checked when they are written and again whenever they are loaded. The hard bounds
come from `bh-cvk70` E20:

- at least the bd lease TTL plus reclaim grace (15 min);
- at least 2 × (session TTL + sync interval). The session TTL is `hq_liveness_policy`'s
  (default 5 min) and the sync interval is 0 in `dolt-server` HQ, so this bound is 10 min.

An `executor` value below the executor floor is refused. A floor below either hard bound is
refused, and so is a floor above the executor default it governs. A floor at or above the hard
bounds but below 35.4 min is accepted with a warning: 35.4 min is the false-stale stretch
measured live (`bh-cvk70` E17).

A write that would be refused, or that would make an existing row refused (for example, raising
the floor above a hive's executor override), writes nothing. A row that is refused on load is
never clamped. The loop logs it once at `WARNING` and uses the default: the fleet `*` row, or
else the code default. `bh doctor` shows refusals and warnings under **Host Daemon** on a host
whose failover loop is enabled.
