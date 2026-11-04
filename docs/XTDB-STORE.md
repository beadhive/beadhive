# XTDB BeadLog store: operator runbook

The XTDB store is an **opt-in, rebuildable projection** of a BeadLog (`.bdlg`) in a bi-temporal
database: valid time (`occurred_at`) is always kept, and recorded time (`recorded_at`) is kept
when the BeadLog is loaded the right way into a fresh database (see
[Initial and incremental loads](#initial-and-incremental-loads)). The BeadLog stays the source of
truth. Losing the XTDB data directory costs a reload, never data.

**You run the node.** `bh` never starts, stops, upgrades or supervises XTDB, the same posture as
Dolt. The plugin only connects, probes and reports. This page is everything an operator needs to
run it safely.

- [Deploy](#deploy-the-node) and [start and stop](#start-and-stop-on-demand)
- [Configure bh](#configure-bh) and [diagnose it](#doctor-failure-modes-and-remediation)
- [Load and read](#initial-and-incremental-loads): [`--store xtdb`](#apply-and-export-with---store-xtdb),
  [times and losses](#the-microsecond-split-and-the-null-recorded_at-tail)
- [Back up](#backup), [upgrade](#upgrade) and [erase](#erasure)
- [Known hazards](#known-hazards)

## Evidence

Every rule below comes from a spike or from the code that landed. Where the two differ, this page
documents the code.

| Spike | Document | Settled |
|---|---|---|
| `bh-o1x4r` | [store landscape](spikes/bh-o1x4r-beadlog-store-landscape.md) | Why XTDB is the one surveyed store that natively honors both time axes |
| `bh-w56ts` | [bead shape](spikes/bh-w56ts-xtdb-bead-shape.md) | Table layout, `_id` rule, transit write and read, type contract |
| `bh-cntr0` | [operational fit](spikes/bh-cntr0-xtdb-operational-fit.md) | **Primary for this page:** footprint, deployment, security defaults, backup, health checks, upgrade posture |
| `bh-3jp2g` | [bitemporal load](spikes/bh-3jp2g-xtdb-bitemporal-load.md) | Monotone `SYSTEM_TIME` rules, nulls, same-instant writes, `ERASE` |

The decisions made from them are the amendments A1 to A8 of the
[BeadLog format ADR](design/beadlog-format-adr.md#amendment-2026-10-31-xtdb-spike-findings). The
code is the [`beadlog-xtdb`](../packages/beadlog-xtdb/README.md) library and the
[`beadhive-beadlog-xtdb`](../packages/beadhive-beadlog-xtdb/README.md) plugin.

## Deploy the node

One container, single node, XTDB `2.2.x`, with the `Local` log and storage on a real-disk
directory. This is the only shape the spikes validated.

```sh
docker run -d --name xtdb-beadlog --restart unless-stopped \
  --memory 2g --memory-swap 2g --user "$(id -u):$(id -g)" \
  -p 127.0.0.1:5432:5432 -p 127.0.0.1:8080:8080 \
  --env-file /data/<operator-only>/xtdb.env -e XTDB_LOG_FORMAT=json \
  --health-cmd 'wget -q -O /dev/null http://127.0.0.1:8080/healthz/alive' \
  --health-interval 10s --health-start-period 30s \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v /data/<real-disk>/xtdb:/var/lib/xtdb \
  ghcr.io/xtdb/xtdb@sha256:a6a426505b95a36e4e0e1d0de88efcf4220cb6bde8f1f5e594b0a1f92a948a77
```

The digest is the XTDB 2.2.0 image the loader was validated against. Take it from
`bh plugin beadlog-xtdb status` (the `image` row) rather than copying it from here, since that is
what `doctor` compares against.

Each setting is a requirement, not a preference:

| Setting | Why |
|---|---|
| `--memory 2g --memory-swap 2g` | The 9,000-bead loads peak at 0.55 to 0.9 GiB, so 2 GiB has margin; 1.5 GiB was also clean. Do not go below 1 GiB (at 1 GiB add `-e JDK_JAVA_OPTIONS=-XX:MaxRAMPercentage=40`). The JVM heap is a fixed 25 % of the cap, so never raise the cap without bound. Below the floor a heap `OutOfMemoryError` stops ingestion while the process stays up ([hazards](#known-hazards)). |
| `-p 127.0.0.1:...` | Inside the image pgwire (5432), Flight SQL (3000) and healthz (8080) listen on every interface, so `-p` alone decides exposure. Publish pgwire and healthz on loopback only. **Never publish 3000.** `doctor` fails a node whose pgwire port also answers on a non-loopback address of this host. |
| `--env-file` with `XTDB_PASSWORD=...` | With no password XTDB runs in TRUST mode: any user name with any password connects as the superuser. The file must be readable by the operator only (`chmod 600`). Do not pass the password with `-e` on the command line (it lands in shell history and `docker inspect`). |
| `--health-cmd` with a GET | The image's own `HEALTHCHECK` sends `HEAD` (`wget --spider`), XTDB answers `405`, and a healthy node is reported `unhealthy`. Override it with a GET. |
| `-v <real-disk dir>:/var/lib/xtdb` | The data directory must be on real disk. **Never `tmpfs` or `ramfs`, and never `/tmp`, which is RAM on some hosts** (including the factory host): the store would be lost on reboot and its pages compete with the node's memory cap. `doctor` refuses such a directory. Budget about 0.5 GB per `bh`-scale store plus log growth; XTDB keeps two logs under `log/`, so the log is about twice the written data. |
| `--user "$(id -u):$(id -g)"` | The image runs as UID 20000 by default and needs a chowned mount. This works with a directory you own. |
| `--log-opt max-size=10m --log-opt max-file=3` | The Docker `json-file` driver does not rotate by default. |
| the image `@sha256:` digest | Never `latest`, `nightly` or a tag. The `image` setting is validated as digest-pinned. |

Notes.

- The password is the node's `XTDB_PASSWORD` for the single user `xtdb`. Put the **same value** in
  the host's fnox secret broker for `bh` ([Configure bh](#configure-bh)). It is never written to
  `bh` configuration, and `bh` never prints it.
- Publishing 8080 on loopback is what lets `doctor` do its `GET` health probe. The container's own
  health command reaches 8080 from inside and works either way.
- `--restart unless-stopped` keeps a node you stopped by hand stopped across a daemon restart.
  Drop it if you want the node to stay down until you start it.
- Four harmless `sun.misc.Unsafe` warnings print at every start.
- Licence: XTDB is MPL-2.0. Running the upstream image locally imposes nothing on `bh`. Do not
  bundle or redistribute the image from `bh`; operators pull the pinned digest themselves
  ([`bh-cntr0` E10](spikes/bh-cntr0-xtdb-operational-fit.md)).

## Start and stop on demand

Keep the node **off unless needed**. A cold start takes about 20 seconds (18 to 22 s over 11
starts) and an idle node holds about 390 MiB, so starting it for a backfill or query session is
cheap. Always-on is an operator choice, not a default.

```sh
docker start xtdb-beadlog
until [ "$(docker inspect -f '{{.State.Health.Status}}' xtdb-beadlog)" = healthy ]; do sleep 2; done
bh plugin beadlog-xtdb doctor        # before the first backfill of a session

docker stop xtdb-beadlog             # about 1 s, exit 143: this is the clean stop
```

- `bh` does not start the node. With the node down, `bh backfill apply|export --store xtdb` exits 1
  with `cannot connect to the XTDB node at xtdb://...`. Start it and retry.
- The stock container health state is only meaningful with the GET override above.
- A `kill -9` or a crash loses nothing that was acknowledged and never half-applies a transaction
  (three `kill -9` runs, including one during writes). Power loss and a full disk were not tested.
- To rebuild from scratch, see [Erasure](#erasure): stop, delete the data directory, start fresh,
  reload.

## Configure bh

Everything is under `plugins.beadlog-xtdb`, **host-owned**, and **off by default**.

```sh
uv tool install 'beadhive[xtdb]'      # adds psycopg (LGPL-3.0); in a checkout: uv sync --extra xtdb
bh config set plugins.beadlog-xtdb.enabled true
bh config set plugins.beadlog-xtdb.data_dir /data/<real-disk>/xtdb
bh config set plugins.beadlog-xtdb.credential.config_path /abs/path/fnox.toml
bh config set plugins.beadlog-xtdb.credential.profile <profile>
bh config set plugins.beadlog-xtdb.credential.key XTDB_PASSWORD
bh plugin beadlog-xtdb status
bh plugin beadlog-xtdb doctor
```

| Key | Default | Meaning |
|---|---|---|
| `enabled` | `false` | The plugin's own gate. Installing the package enables nothing. |
| `dsn` | `host=127.0.0.1 port=5432 user=xtdb dbname=xtdb` | libpq string or `postgresql://` URI of the loopback pgwire port. A DSN that embeds a password is refused. Change `port` if you published another one. |
| `credential.config_path` / `.profile` / `.key` | unset | fnox lookup of the node's `XTDB_PASSWORD`. Unset means no password, which `doctor` reports as an error. |
| `image` | the validated 2.2.0 digest | The digest-pinned image you run (`<repository>@sha256:<64 hex>`; a tag is refused). `doctor` warns on any other digest. |
| `data_dir` | unset | Absolute host path mounted at `/var/lib/xtdb`, for `doctor`'s file-system check. Unset means it is not checked. |
| `health_url` | `http://127.0.0.1:8080/healthz/alive` | Loopback healthz URL, probed with `GET`. |

`bh` reaches only a loopback node. A `dsn` whose host is not loopback is refused before the
password is looked up. Nothing in configuration, `status`, `doctor` or any error repeats the
password.

`bh plugin beadlog-xtdb status [--json]` is offline and read-only. Its state is `ready`,
`disabled`, `not-installed` (install `beadhive[xtdb]`) or `invalid-config` (run
`bh config validate`).

## Doctor: failure modes and remediation

`bh plugin beadlog-xtdb doctor [--json]` is read-only and loopback-only. It never starts, stops,
restarts or writes to the node. While the plugin is disabled it checks only configuration and the
data directory and always exits 0. When enabled it exits 1 if any row is an error.

| Row | Severity | Failure | Remediation |
|---|---|---|---|
| `data-dir` | error | The directory is on `tmpfs` or `ramfs` | Stop the node, move the directory to a real-disk path (never `/tmp` on a host where it is RAM), start with `-v <real-disk dir>:/var/lib/xtdb`, update `data_dir` |
| `data-dir` | warn | `data_dir` unset, the path does not exist on this host, or the file-system type cannot be read (non-Linux) | Set it to the host directory behind `/var/lib/xtdb`; confirm by hand that it is not `tmpfs` or `ramfs` |
| `image` | warn | The configured digest is not the validated 2.2.0 one | Follow [Upgrade](#upgrade) before switching digests |
| `password` | error | No `credential` configured, the fnox lookup failed, or the entry is empty | Store `XTDB_PASSWORD` in fnox and set `credential.config_path`, `.profile` and `.key`; start the node with the same value |
| `bind` | error | `dsn` or `health_url` is not loopback, or the pgwire port also answers on a non-loopback address of this host | Publish with `-p 127.0.0.1:<port>:5432`; point `dsn` and `health_url` at `127.0.0.1`; never publish 5432, 3000 or 8080 off-host |
| `health` | error | `GET <health_url>` failed or answered non-200 | Start the node; use the GET health override; check `docker logs` for `OutOfMemoryError` (the process can stay up after a heap OOM) and restart if so |
| `version` | error | Cannot connect, or XTDB is outside `>=2.2.0,<2.3` | Start the node and check the `dsn` port and the password; run the pinned 2.2.x image |
| `auth` | error | The node accepts a connection **without** the password (TRUST mode: any user maps to the superuser) | Restart the node with `XTDB_PASSWORD` in its `--env-file` |

What `doctor` does **not** do: it does not scan the container log for `OutOfMemoryError`, does not
check that 3000 is unpublished, and does not run a write-then-read probe. A node stuck after a heap
OOM can still answer `SELECT`. Those stay operator checks ([hazards](#known-hazards)).

## Initial and incremental loads

How a load is written decides whether recorded time survives. In XTDB 2.2 preserving it is a
**one-shot, all-or-nothing property of a fresh database**
([A2](design/beadlog-format-adr.md#a2-recorded-time-monotone-load-semantics)).

| | Initial load | Incremental load |
|---|---|---|
| Target | A **fresh** database: no row in any loader table and no transaction at or after the first `recorded_at` | Any database that already holds a load |
| Transactions | One `BEGIN READ WRITE WITH (SYSTEM_TIME = ...)` per distinct `recorded_at`, committed SYNC, strictly increasing | Plain wall-clock transactions |
| Valid time | `occurred_at` | `occurred_at` |
| Recorded time | **Preserved** (the loader capability `recorded-time-monotone-load`) | The load time; reported `recorded-time.reassigned` |
| Replay | `index/recorded.jsonl` order, equal `recorded_at` grouped into one transaction | Events already in the store (by id) are skipped, so a reload is idempotent |

The loader picks the mode itself (`auto`): initial for a fresh target, incremental otherwise. An
initial load into a target that is not fresh is refused before anything is written, with a message
saying which table holds rows or which transaction is too late.

**Fresh database only, to keep recorded time.** A fresh node (an empty data directory), or an
`ATTACH DATABASE` database on the node, which has its own clock. Prefer a fresh node: the
durability of an attached database's storage was not verified.

**Abort means discard and reload.** If any statement or `COMMIT` fails, or `xt.txs` shows an
aborted transaction afterwards, the loader raises an abort telling you to discard the database and
reload. It never retries in place and never reports success. Do not retry by hand: a tie or an
out-of-order transaction is stamped with wall-clock time and ends backfill for the whole database
(every later backfill transaction aborts, even one at +1 microsecond), and a non-time abort
silently loses that transaction's data. The recovery is always:

1. Stop the node.
2. Delete the whole data directory.
3. Start a fresh node on an empty directory.
4. Run the load again from the `.bdlg`.

**Refusals before any write.** A `recorded_at` later than the wall clock (a future `SYSTEM_TIME`
hides its rows until the clock passes it); an index that is out of recorded-time order or does not
list every event; a [microsecond split](#the-microsecond-split-and-the-null-recorded_at-tail) that
would reach the next `recorded_at`.

**Cost.** One transaction per distinct `recorded_at` is the price of keeping recorded time. At
`bh` scale (9,088 beads, 59,387 operations) that was 28,402 transactions in about 6 minutes, with a
552 MiB peak and 370 MB of log. Incremental loads batch events into fewer transactions.

**Everything is also kept whole.** The loader keeps each loaded event, with its original id, in the
`bead_events` journal table. Values the layout tables cannot hold (a field with no column, a time
finer than a microsecond) stay in the journal as written, which is also what `--store xtdb`
pre-state exports read back.

### Which `bh` verb gives you which

`bh backfill apply PLAN --store xtdb` writes **one bead per unit**. On a fresh database the *first*
unit takes the monotone initial load, and every later unit loads incrementally, so only that first
bead's recorded times are preserved; each unit's loss report says which. A faithful, whole-history
recorded-time load of a `.bdlg` is the library call `beadlog_xtdb.load_archive(loader, path)`,
which replays the archive's `index/recorded.jsonl` in one initial load. There is no `bh` verb for
that today. Use it from Python against a fresh database when recorded time matters.

## The microsecond split and the null `recorded_at` tail

Two kinds of events cannot be loaded as written, and both are reported, never silent
([A4](design/beadlog-format-adr.md#a4-null-recorded_at),
[A5](design/beadlog-format-adr.md#a5-same-instant-microsecond-split)).

**Same-instant writes (the microsecond split).** Within one transaction, two writes to the same
entity with the same `_valid_from` keep only the last; the earlier one disappears from every read.
`bh` data has 136 such cases. The operator chose to split them rather than lose one:

- Within a group, each entity's writes sharing one `occurred_at` are numbered from `k = 0`.
  Write `0` stays in the group's transaction.
- Write `k >= 1` gets its **own transaction** at `recorded_at + k` microseconds, with
  `_valid_from = occurred_at + k` microseconds.
- If `recorded_at + k` would reach the next distinct `recorded_at`, the load is refused before
  anything is written. `bd` timestamps have one-second resolution, so this does not arise from
  `bd`; another source may.
- The stored times of a split write differ from the event's. It is reported as
  `x-xtdb.us-split`, disposition `approximated`, once for `/occurred_at` and once for
  `/recorded_at`, with `stored_as` holding the shifted value. The event id is unchanged.

**Null `recorded_at` (the tail).** XTDB cannot store "unknown" recorded time: `SYSTEM_TIME = NULL`
means wall-clock time, which ends backfill. Events with a null `recorded_at` sort last in the
index. The loader stops the replay at the first null and loads that tail afterwards, in
wall-clock transactions, each with an explicit `_valid_to` (the `occurred_at` of the next known
version of the same entity, or open-ended), so a later-in-time version is not clobbered. Each is
reported `recorded-time.null`. The loader never invents a recorded time.

### Loss report codes

Every load, dry run or apply, returns a report that validates against
`docs/schemas/beadlog/v0.1/loss-report.schema.json`. A dry run reports exactly what the apply
will, because every loss is decided before anything is written. `apply --dry-run` prints the codes
with counts; `--json` includes the whole report.

| Code | Disposition | Meaning |
|---|---|---|
| `recorded-time.null` | reassigned | Null `recorded_at`: loaded in the wall-clock tail |
| `recorded-time.reassigned` | reassigned | Incremental load: recorded time is the load time |
| `valid-time.missing` | reassigned | Null `occurred_at`: valid from `recorded_at` (or the load time) |
| `precision.lost` | degraded | `_valid_from` is an instant; the `occurred_precision` interval is lost (kept in the journal) |
| `field.unsupported` | degraded | A field has no `beads` column (kept in the journal) |
| `erasure.incomplete` | degraded | A tombstone was applied as a **logical** erase; see [Erasure](#erasure) |
| `edge.dangling-target` | degraded | An edge points at a bead with no `beads` row after the load (including inbound edges of an erased bead) |
| `x-xtdb.us-split` | approximated | A same-instant write moved by `k` microseconds |
| `x-xtdb.sub-microsecond` | approximated | A time finer than a microsecond was truncated for the store; the journal keeps it as written |
| `x-xtdb.column-coerced` | degraded | A value was stored as its column's one type (`2` in a text column becomes `"2"`), so the column never widens to a union |
| `x-xtdb.column-refused` | degraded | A value cannot take its column's type; the column is left without it, the journal keeps it |
| `x-xtdb.erased` | dropped | An event erased by a tombstone in the same load, or already in the store; never written |
| `x-xtdb.id-unrepresentable` | dropped | A bead id (or edge target) containing a pipe character (or a colon in a multi-group load), which the composite ids cannot spell |

## Apply and export with `--store xtdb`

Prerequisites: the plugin installed and enabled, the node running, `doctor` green.

```sh
bh backfill apply PLAN.bdlg --store xtdb --dry-run    # the loader's loss report; writes nothing
bh backfill apply PLAN.bdlg --store xtdb              # the full safety envelope
bh backfill export --store xtdb [--out F.bdlg]        # the store's current state, as a .bdlg
```

- **Apply** runs the same [safety envelope](BACKFILL.md#safety-envelope) as the bd store: the
  corpus hash is checked against the store's own history, a whole-plan dry run assesses the losses,
  a `.bdlg` of the store's pre-apply history is written first as the recovery artifact
  (`<plan>.pre-state.bdlg` by default), each bead is written with an audit record (`--audit`), and
  the result is verified. Read the loss codes from the dry run before applying. A plan for a fresh
  store is a `beadlog reconcile` against an empty baseline.
- **A plan with tombstones is refused.** The engine requires operator acknowledgement of
  `erasure.incomplete`, and `bh backfill apply` has no flag for it today. Erase through the
  library path in [Erasure](#erasure).
- **On failure** the engine rolls back by loading the inverse of each touched event. Those
  inverses are normally incremental, wall-clock loads. If the target was a fresh database that
  you wanted to keep recorded time in, treat any failed apply as an abort: discard the database
  and reload.
- **Export** reads the current belief through the as-of reader for the hive's bead group and
  writes `<prefix>.xtdb.bdlg` by default. The manifest's `annotations["beadhive.store"]` names the
  store and target and counts what the read could not carry back (for example a re-read comment
  has a new event id; the stored one is in `annotations.xtdb.comment_id`). `--history` reads the bd
  events table and is refused with `--store xtdb`.
- Both verbs exit 1 with a one-line message when the plugin is disabled, not installed, or
  misconfigured, when the node is unreachable, or when a loader guard refuses the load. None of
  those messages contains the password.

## Backup

The BeadLog is the source of truth and a full reload takes about 6 minutes at `bh` scale, so
backing up XTDB is a convenience, not a correctness requirement. You have two options.

1. **Reload the `.bdlg`.** Keep the `.bdlg` (and its pre-state exports). To restore, follow the
   discard-and-reload steps above. This is the only way to get recorded time back exactly, and it
   is the recommended backup.
2. **Stop and copy the data directory.** Verified: `docker stop`, `cp -a` the whole data directory
   (0.25 s for 257 MB), start a node on the copy (about 21 s to ready), and every table matched by
   digest.

   ```sh
   docker stop xtdb-beadlog
   cp -a /data/<real-disk>/xtdb /data/<backups>/xtdb-$(date +%Y%m%d)
   docker start xtdb-beadlog
   ```

   Do not copy a running node. A copy taken while it was running and idle was intact, but that
   tested no concurrent writes. Keep the copy on real disk with the same care as the original, and
   remember that it holds every byte the original does, including [erased](#erasure) content.

## Upgrade

Pin the digest and treat every minor as breaking: each XTDB minor so far has carried breaking
configuration or SQL changes, and 2.2 writes a log format that 2.1.0 cannot read. There is no
published storage-format compatibility guarantee. The loader's version gate is **`>=2.2.0,<2.3`**
and it refuses anything else, so an unvalidated minor cannot be used by accident.

To change the pinned digest:

1. Stop the node and `cp -a` the data directory ([Backup](#backup)).
2. Start the **new** image against the **copy**, on another container name and loopback port, with
   its own `--env-file`.
3. Run the loader-conformance suite and the integration tests against it. The library tests are
   integration-marked and skip unless a node is named. Give the DSN the new port, and the password
   through libpq's environment:

   ```sh
   PGPASSWORD=... BEADLOG_XTDB_TEST_DSN="host=127.0.0.1 port=15440 user=xtdb dbname=xtdb" \
     uv run --inexact --package beadlog-xtdb pytest -m integration packages/beadlog-xtdb/tests
   ```

   The `beadlog.testing` loader-conformance kit runs there against the new version.
4. If it passes, switch: stop the old container, run the new image on the real data directory
   (or reload into a fresh one), and set `plugins.beadlog-xtdb.image` to the new digest so
   `doctor` stops warning.
5. If the new image cannot open the old data, nothing is lost: discard the copy, start the new
   image on an empty directory and reload the `.bdlg`.

Moving the version gate to a new minor (for example 2.3) is a code change in `beadlog-xtdb` and
needs the same conformance run first.

## Erasure

A `tombstone` event is applied as XTDB `ERASE`, and it is **logical only**
([A1](design/beadlog-format-adr.md#a1-erase-is-logical-in-xtdb)).

- **What you get.** The erased bead or comment text disappears from every read: the default read,
  `FOR VALID_TIME AS OF`, `FOR SYSTEM_TIME AS OF`, both `FOR ALL` axes, and `BETWEEN`. Every other
  bead is intact.
- **What you do not get.** The erased bytes stay in the transaction log (`log/LOG`,
  `log/REPLICA_LOG`) and in block files, immediately and after at least 150 seconds. Whether
  compaction or log retention ever removes them was not observed. The loader declares the
  capability `erase-logical` (never `erase`), reports `erasure.incomplete` for each tombstone, and
  nothing it says claims physical removal.
- **Inbound edges.** `ERASE` removes what its predicate names. Edge rows owned by *other* beads
  that point at the erased bead remain, and are reported as `edge.dangling-target`. Whether a
  tombstone should also erase them is undecided
  ([A1 point 5](design/beadlog-format-adr.md#a1-erase-is-logical-in-xtdb)).
- **Erasing already-loaded content.** A tombstone loaded into a store that already holds the
  erased events erases the affected rows and rewrites the surviving ones at load time, so their
  recorded time is not preserved (the report says how many).
- **Acknowledge it.** `bh backfill apply --store xtdb` refuses a plan with tombstones
  ([above](#apply-and-export-with---store-xtdb)). Tombstones load through
  `beadlog_xtdb.load_archive`, which does not ask: the `erasure.incomplete` rows in its report are
  what the person running it must read and accept before treating the content as gone.

### Physical purge is a rebuild

XTDB is a rebuildable projection, so the only way to remove erased bytes is to rebuild the
database from a **purged** BeadLog. The six steps
([A1 decision 4](design/beadlog-format-adr.md#a1-erase-is-logical-in-xtdb)):

1. **Purge the BeadLog itself.** The tombstone is merged in and no part, checkpoint or index line
   carries the erased content (format ADR section 10).
2. **Stop the node.** The operator owns start and stop; the plugin never does.
3. **Delete the whole data directory**, `log/` and `buffers/` both, **and every copy of it**.
   A stop-and-copy backup holds the same bytes.
4. **Start a fresh node** on an empty real-disk data directory (pinned image, as above).
5. **Run the initial monotone load** of the purged BeadLog, about 6 minutes at `bh` scale
   ([Initial and incremental loads](#initial-and-incremental-loads)).
6. **Check that the erased text is gone from the new data directory.** For example
   `grep -r '<erased text>' /data/<real-disk>/xtdb`, as the spike did over the log and block
   files. XTDB does not attest this, so it is an operator step. Also check any `.bdlg` pre-state
   exports, which are copies of the store's history.

## Known hazards

| Hazard | What happens | Do |
|---|---|---|
| **Heap OOM keeps the container "running"** | At too small a cap (512 MiB, default JVM flags) ingestion failed with `java.lang.OutOfMemoryError: Java heap space`, yet the process stayed up, reads still answered, and the writes had not been applied (later `Table not found`). A supervisor that watches only container state will not notice. What `/healthz/*` reports in that state was not checked. | Keep the 2 GiB cap. `doctor` does not scan logs: after a failed load, `docker logs xtdb-beadlog \| grep OutOfMemoryError`. If found, discard and reload |
| **`COMMIT ASYNC` reports false success** | It returns success even when the transaction aborts (`bh-3jp2g` S10). The loader always commits SYNC. | Never run `COMMIT ASYNC` by hand against the loader tables without checking `xt.txs` afterwards |
| **No authentication by default** | Without `XTDB_PASSWORD`, any user name with any password connects as the superuser; pgwire, Flight SQL and healthz listen on every interface inside the image. | Set `XTDB_PASSWORD` and bind loopback, both. `doctor` checks the password, the bind and `auth` |
| **Stock `HEALTHCHECK` lies** | It sends `HEAD`, XTDB answers `405`, and a healthy node shows `unhealthy`. | Use the GET override; do not trust `docker ps` health without it |
| **A tie or out-of-order system time ends backfill** | Recorded-time preservation is lost for the whole database, permanently. | Discard and reload; never retry in place |
| **Erased bytes survive in the log, blocks and every copy** | `ERASE` is logical only | [Physical purge is a rebuild](#physical-purge-is-a-rebuild) |
| **Only psycopg works for writes** | asyncpg and pg8000 fail on bound-parameter DML (XTDB 2.2 returns an empty parameter description). | Nothing to do; the loader uses psycopg |
| **Not tested** | Host power loss and the `Local` log's fsync policy, a full disk, log growth and compaction over weeks, copying a node under active writes, podman, Flight SQL reads | Keep the `.bdlg`; treat the store as disposable |

## Where things live

| What | Where |
|---|---|
| Plugin (config, `doctor`, `status`, the `beadlog.store` binding) | [`packages/beadhive-beadlog-xtdb`](../packages/beadhive-beadlog-xtdb/README.md) |
| Loader and as-of reader (op mapping, layout, transit, loss codes) | [`packages/beadlog-xtdb`](../packages/beadlog-xtdb/README.md) |
| Configuration keys | [`plugins.beadlog-xtdb`](CONFIGURATION.md) |
| `bh backfill` verbs and the safety envelope | [Backfill](BACKFILL.md#other-stores---store) |
| Decisions | [Format ADR A1 to A8](design/beadlog-format-adr.md#amendment-2026-10-31-xtdb-spike-findings) and the [backfill platform ADR](design/backfill-platform-adr.md) |
