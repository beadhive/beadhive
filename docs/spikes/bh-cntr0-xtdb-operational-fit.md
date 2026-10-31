# Spike `bh-cntr0` — Is XTDB 2.2 operationally acceptable on the factory, and how would bh reach it?

**Bead:** `bh-cntr0` · **Seat:** `dev/xtdb-ops` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-psu1b` (adopt XTDB v2 as the first bi-temporal BeadLog loader?).
Parent: epic `bh-5wu0u`. Siblings: `bh-w56ts` (document shape, merged), `bh-3jp2g` (bi-temporal
load, ran concurrently), `bh-o1x4r` (store landscape, merged). Context:
[backfill platform ADR](../design/backfill-platform-adr.md),
[BeadLog store landscape](../design/beadlog-store-landscape-research.md).

## Question

Can bh run XTDB 2.2 as an **optional, operator-started** bi-temporal store on the factory host
(47 GB RAM, `/tmp` is tmpfs) at an acceptable footprint, and reach it from Python as a separately
installed opt-in plugin, with a licence and upgrade posture it can live with? Concretely: memory
and disk at idle and under the 9k-bead load, how low the memory cap can go, deployment mode,
client path, packaging and version floor, MPL-2.0 obligations, durability across `kill -9`,
backup, health checks, logging and default security.

**Not asked:** bi-temporal semantics and `SYSTEM_TIME` backfill behaviour (`bh-3jp2g`), document
shape and type fidelity (`bh-w56ts`), the BeadLog wire format (`bh-65jen`), multi-node or
object-storage deployments (not needed for a single factory host).

## Method

**Host.** Debian, Linux 6.12, cgroup v2, Docker 29.7.1 (json-file logging, no `daemon.json`),
32 cores, 47 GB RAM, data on ext4 `/data`. No JDK on the host. `podman` is present; not tested.

**Server.** `ghcr.io/xtdb/xtdb:2.2.0` (revision `c6c19623`, JDK 25.0.4.1, image digest
`sha256:a6a426505b95a36e4e0e1d0de88efcf4220cb6bde8f1f5e594b0a1f92a948a77`), the image's own
`config.yaml` (`log: !Local`, `storage: !Local`, pgwire 5432, Flight SQL 3000, healthz 8080).
One container of mine at a time, named `xtdb-spike-bh-cntr0`, with `--memory <cap> --memory-swap
<cap> --user "$(id -u):$(id -g)"`, pgwire and healthz published on `127.0.0.1` only, data
bind-mounted from real disk under `/data/bees/beadhive/scratch/xtdb-spike/bh-cntr0/` (never
`/tmp`). All data dirs were removed at the end.

**Memory.** Read from the container's cgroup (`memory.current` twice a second, exact
`memory.peak`, `memory.events`). `memory.current` includes page cache, so it overstates what
`docker stats` shows; the final 2 GiB run also records the `docker stats` basis (current minus
inactive file cache) so the numbers compare with `bh-w56ts` and `bh-3jp2g`, which used
`docker stats`.

**Load.** The `bh-w56ts` shape load unchanged (`../bh-w56ts/load.py`: 9,088 beads in both
layouts, 21,760 label rows, 10,321 edge rows, 676 comments, 36,716 bd events; 87.6k rows in
500 to 1,000-row transactions). For the "time spike's load" I read `bh-3jp2g`'s
`load-metrics.json` and ran read-only `docker stats` and `du` on its container and data dir; I did
not connect to its database.

**Clients.** `uv run --no-project --with 'psycopg[binary]' ...` (psycopg 3.3.6, asyncpg 0.32.0,
pg8000 1.31.6). No repository dependency files were touched.

**Sources.** `gh release view` for `v2.0.0`, `v2.1.0`, `v2.1.1`, `v2.2.0` and the `v2.2.0`
pre-releases; docs.xtdb.com pages (authentication, configuration, log, storage, backup and
restore, maintenance, monitoring, troubleshooting, Docker install); the upstream `LICENSE`; the
image's labels, `docker save` layers and `xtdb.jar` contents. Fetched 2026-10-30.

**Scripts** (throwaway, under [`scripts/bh-cntr0/`](scripts/bh-cntr0/); captured outputs in
[`scripts/bh-cntr0/output/`](scripts/bh-cntr0/output/)):

| Script | Does |
|---|---|
| `harness.py` | Start the loopback-only container, poll `/healthz/started`, run the cap matrix (`captest`, `matrix`) |
| `loadrun.py` | Run the shape load, sample cgroup memory twice a second, report idle, peak, end, data size |
| `counts.py` | Row counts plus content digests of every table (integrity across restart and restore) |
| `crashwrite.py`, `crashverify.py` | Write 100-row transactions until `kill -9`, then verify atomicity and acked data |
| `drivers.py` | psycopg, pg8000 and asyncpg: connect, read, bound-parameter DML, timestamps |
| `auth.py` | Authentication probe with and without `XTDB_PASSWORD` |

## Evidence

### E1. Footprint: idle, shape load, and the time spike's load

Idle after start is **about 390 MiB** (`docker stats`: 391.4, 391.7, 392.4 MiB over 30 s at a
2 GiB cap; first-boot peak 416 MiB), at **3 to 8 % of one core** and 77 to 80 threads. The
same figure appeared in `bh-w56ts` (431 MiB at a 3 GiB cap, where the heap is larger) and in
`bh-3jp2g` (416.8 MiB at 3 GiB).

| Run | Cap | Idle (MiB) | Peak (MiB) | End (MiB) | Wall | Basis |
|---|---|---|---|---|---|---|
| `bh-w56ts` shape load (merged spike) | 3 GiB | 431 | 907 | about 1,090 after all experiments | about 50 s | `docker stats` |
| `bh-cntr0` shape load, same script | 2 GiB | 385 | 1,022 | 1,008 | 47.3 s | cgroup, with cache |
| `bh-cntr0` shape load, final rerun | 2 GiB | 396 | 1,003 (883) | 985 (873) | 47.1 s | cgroup (`docker stats` in brackets) |
| `bh-3jp2g` bi-temporal load (9,088 beads, 36.7k events, 28,402 SYNC transactions) | 3 GiB | 416.8 | 552.4 | 552.2 | 359.6 s | `docker stats` |
| `bh-3jp2g` same container, about 14 min after the load ended | 3 GiB | — | — | 581.7 | — | `docker stats` |

Reading: the two loads differ in shape, not in size. My load writes 500 to 1,000 rows per
transaction and peaks near 0.9 GiB (`docker stats` basis). The time spike's load commits 28.4k
small transactions (one per distinct recorded time, as the 2.2 `SYSTEM_TIME` rule forces), runs
six times longer, and peaks near 0.55 GiB. **Both fit comfortably in 2 GiB.** Memory does not
return to the idle figure after a load: the live index stays in memory until a block is written
(E4). After a restart plus a full read pass of every table the node sat at 679 MiB right after a
`finish-block`.

Where the memory goes. The JVM in the container takes `MaxRAMPercentage` 25 % as max heap (`jcmd
VM.flags`: `MaxHeapSize` 536870912 at 2 GiB, G1), and the Arrow allocator's direct memory
defaults to the same figure. Heap plus direct plus metaspace (138 MiB committed) and thread
stacks is the working set. Raw runs: [`output/footprint.md`](scripts/bh-cntr0/output/footprint.md),
[`output/capmatrix-logs.md`](scripts/bh-cntr0/output/capmatrix-logs.md).

### E2. How low can `--memory` go? (the 9k-bead shape load)

Same load, fresh data dir each time, default JVM flags unless stated. Row counts and content
digests matched the 2 GiB run in every run that finished.

| Cap | Max heap | Idle | Peak | Load wall | Outcome |
|---|---|---|---|---|---|
| 2 GiB | 512 MiB | 385 | 1,022 | 47 s | Clean |
| 1.5 GiB | 384 MiB | 330 | 903 | 58 s | Clean |
| 1 GiB | 256 MiB | 321 | 785 | **192 s** | Completes, but the bd-events phase took 158 s against 21 s at 2 GiB (GC pressure) |
| 1 GiB, `JDK_JAVA_OPTIONS=-XX:MaxRAMPercentage=40` | 410 MiB | 320 | 933 | 54 s | Clean, as fast as 2 GiB |
| 768 MiB, `MaxRAMPercentage=40` | 307 MiB | 316 | 768 (at the cap) | 99 s | Completes; the cgroup limit was hit 292 times (page cache reclaim); not recommended |
| 768 MiB, default | 192 MiB | — | — | over 600 s | Did not finish inside my 600 s limit; CPU pegged, no result |
| 512 MiB, default | 128 MiB | 317 | 512 | 378 s, then failed | `java.lang.OutOfMemoryError: Java heap space`; **the process stayed up** but ingestion stopped (`Table not found: bead_edges` afterwards) |

Two things to carry forward. First, the floor is JVM sizing, not the kernel: no run hit the
kernel OOM killer (`oom_kill` 0 everywhere), because the JVM exhausts its own heap first.
Second, a heap OOM does **not** kill the node. The container stayed "running" and the server
still answered reads, yet the writes had not been applied. A supervisor that only watches the
container state will not notice. I did not check what `/healthz/alive` or `/healthz/started`
reported in that state.

### E3. Disk

| State | `log/` | `buffers/` | Total |
|---|---|---|---|
| After the shape load (inputs: 49 MB of JSON), no block flushed yet | 117 MB (`LOG` 57 + `REPLICA_LOG` 60) | 0 | 117 MB |
| After `POST /system/finish-block` (plus 12k rows of crash-test writes) | 124 MB | 132 MB (95 files) | 257 MB |
| `bh-3jp2g` after its bi-temporal load (28.4k transactions) | 370 MB | 96 MB | 466 MB |

Notes. 2.2 keeps two logs under `log/` (source and replica), so the log is about twice the
written data. Log size scales with the **transaction count** (about 13 KB per transaction in the
28.4k-transaction load, against about 1.3 KB per row in 500-row batches), so a loader should batch.
Storage blocks appear only when a block is cut: 102,400 rows, 15 minutes on a quiet node (the 2.2
default, was 4 hours), or `POST /system/finish-block`. I saw no truncation of the `Local` log in
the roughly 40 minutes of observation, so assume it grows with write volume. At bh scale the
whole store is under half a gigabyte.

### E4. Startup, shutdown, `kill -9`, restart

| Event | Result |
|---|---|
| Cold start to `/healthz/started` = 200 | 18.2 to 22.1 s over 11 starts (median about 20 s); first log line at about 5.6 s |
| `docker stop` | 0.97 s, exit 143, logs `Stopping node...` |
| `kill -9` after the load (117 MB log, no block flushed, so full replay) | Back in 20.2 s. All six tables identical by digest |
| `kill -9` while a writer committed 100-row transactions at about 2k rows/s | Writer acked 119 transactions; after restart 120 present (the 120th was in flight and committed), **0 partial, 0 acked-but-missing** |
| `kill -9` after `finish-block` (blocks on disk) | Back in 20.7 s. All tables identical by digest |

So `kill -9` of the process loses nothing acknowledged and never half-applies a transaction, with
the single-node `Local` log. Replay of about 117 MB of log added no measurable time to the roughly
20 s boot. **Not tested:** host power loss or kernel crash (fsync behaviour of the `Local` log), a
full disk, and multi-day soak. Raw: [`output/durability.md`](scripts/bh-cntr0/output/durability.md).

### E5. Backup and restore

XTDB's own guidance is that storage files are immutable and aligned to log blocks, and that the
log holds the not-yet-indexed tail ([backup and restore overview](https://docs.xtdb.com/ops/backup-and-restore/overview.html)).
For a single `Local` node the whole state is the data dir. Verified:

1. `docker stop`, `cp -a` of the data dir (0.25 s for 257 MB), new container on the copy: 20.8 s
   to ready, all six tables identical by digest and the crash-test batches intact.
2. A copy taken while the node was **running but idle**, then started on its own: also intact.
   This is weak evidence (no concurrent writes), so the supported procedure should be
   stop, copy, start, which costs about 21 s of downtime.

The store is also a **rebuildable projection**: the BeadLog (`.bdlg`) is the source of truth
and a full reload takes about 6 minutes at bh scale (E1). Backup of XTDB is therefore a
convenience, not a correctness requirement.

### E6. Health checks, metrics, logging

- **Healthz** is an HTTP server on 8080 in the image config: `/healthz/alive`, `/healthz/started`
  (also waits for indexing to catch up), `/healthz/ready`, Prometheus text at `/metrics`, and
  `POST /system/finish-block`. All answered 200 on GET. By default XTDB disables healthz when no
  `healthz:` block is configured; the image config enables it.
- **The image's own `HEALTHCHECK` is wrong on 2.2.0.** It runs `wget -q --spider
  http://localhost:8080/healthz/alive`; `--spider` sends HEAD, XTDB answers `405 Method Not
  Allowed`, so a healthy node is reported `unhealthy` after three tries (observed on my 2 GiB
  and 768 MiB runs and, by read-only `docker inspect`, on `bh-3jp2g`'s 3 GiB container).
  Overriding with a GET (`--health-cmd 'wget -q -O /dev/null
  http://127.0.0.1:8080/healthz/alive'`) gave `healthy` on six consecutive samples. I found no
  matching upstream issue and filed nothing. A supervisor must not trust the stock check.
- **Logging** goes to stdout/stderr: plain text by default, one JSON object per line with
  `XTDB_LOG_FORMAT=json`; level via `XTDB_LOGGING_LEVEL[_PGWIRE|_INDEXER|_COMPACTOR]`. Idle
  output is tiny (1.7 KB in 90 s). The Docker `json-file` driver has no rotation by default, so
  set `--log-opt max-size=10m --log-opt max-file=3`. Four harmless `sun.misc.Unsafe` warnings
  print at every start.
- Version probe: `SELECT version()` returns `PostgreSQL 16 (XTDB 2.2.0)`;
  `current_setting('xtdb.version')` returns the version alone (2.2 release notes).

Raw: [`output/ops-probes.md`](scripts/bh-cntr0/output/ops-probes.md).

### E7. Security defaults

- **No authentication by default.** With no password configured the default `!SingleRootUser`
  runs under TRUST: **any user name with any password connected and was mapped to the `xtdb`
  superuser** (four of four attempts, including `bob` and a wrong password).
- Setting `XTDB_PASSWORD` turns on password authentication for the single user `xtdb`: missing
  password, wrong password and any other user name were all rejected; the right password
  connected. `!UserList` (hashed passwords, static, restart to change) and OIDC exist in 2.2.
- **Bind address.** Inside the image, pgwire, Flight SQL (3000) and healthz (8080) listen on all
  interfaces (`host: '*'`), so exposure is decided entirely by `docker run -p`. Upstream's own
  default for a bare config is `localhost`. With `-p 127.0.0.1:<port>:5432` the host showed only
  loopback listeners. Flight SQL has the same auth rules and was not published.
- Upstream's Docker page describes the one-line image as a "standalone (non-production,
  non-distributed)" server. The container runs as UID 20000 by default and needs a chowned
  mount; `--user "$(id -u):$(id -g)"` works with a bind mount we own.
- Secrets: the password is a plain environment variable; keep it in an env-file readable by the
  operator only. Inline S3 or Azure credentials in `ATTACH DATABASE` are deprecated in 2.2 and not
  relevant to a local node.

### E8. Client path: Postgres drivers

Reads work everywhere; **bound-parameter writes work only with psycopg.**

| Driver | Connect, `version()`, count, bound-param `SELECT`, `timestamptz` | DML with bound parameters | Verdict |
|---|---|---|---|
| psycopg 3.3.6 | OK, `datetime` with UTC | OK once `str` is registered with `StrDumper` (OID 0 is refused in DML) | Use this |
| asyncpg 0.32.0 | OK | **Fails client-side:** "the server expects 0 arguments for this query, 3 were passed" (the server's parameter description for DML is empty) | Reads only; literals for writes |
| pg8000 1.31.6 | OK | **Fails server-side:** generic `XX000 xtdb.error/unknown`; the server log shows `IndexOutOfBoundsException` in `xtdb.pgwire/->xtify-arg` | Reads only; literals for writes |

The root cause for the two failures is the same: those drivers bind from the server's
`ParameterDescription`, which XTDB 2.2.0 returns empty for DML, whereas psycopg declares types in
`Parse`. Literal-only inserts (`INSERT INTO t RECORDS {_id: 'x', ...}`) worked in both, but
inlining document text is not an acceptable write path. **Decision: psycopg only**, which is also
the only driver `bh-w56ts` validated for transit parameters. Note two dependencies:

- **Transit** is needed for writes of whole documents (one transit parameter per document, typed
  instants) and for reading nested columns with types intact (`bh-w56ts` E3). The JSON fallback
  loses nested timestamps and sets. `transit-python2` 0.8.321 is Apache-2.0 but a one-person fork
  with its last release in 2022-02 and last push in 2023-08. A loader should pin it exactly or
  hand-write the narrow transit-JSON subset it needs.
- **psycopg** is LGPL-3.0 (binary wheel too). Using it unmodified as an installed dependency
  imposes no obligation on bh code.
- Flight SQL / ADBC (new in 2.2, Arrow results into pandas or Polars) is an alternative read path
  through `adbc-driver-flightsql`. Not tested; the port would need publishing and the same
  authentication.

Raw: [`output/drivers.md`](scripts/bh-cntr0/output/drivers.md).

### E9. Versions, upgrade stability, cadence (release notes, `gh release view`)

| Release | Date | Gap |
|---|---|---|
| 2.0.0 | 2025-06-12 | — |
| 2.1.0 | 2025-12-01 | 5.6 months |
| 2.1.1 (patch, single purpose) | 2026-10-21 | — |
| 2.2.0 | 2026-10-21 | 10.7 months; six pre-releases between 2026-04-30 and 2026-10-20 |

The repository was pushed to the day I looked, and the docs already mention 2.3 features. Support
is community plus paid support from JUXT.

**What changed 2.1 to 2.2** (from the 2.2.0 notes). New: external sources (Postgres logical
replication, Kafka Connect), Arrow ADBC and Flight SQL, a **single leader per database** with a
separate replica log, garbage collection of superseded storage files, 15-minute block flush by
default, `finish-block` and `xt.*` admin tables, much wider Postgres-client compatibility. Breaking
or behaviour-changing for a new install: authentication model (`CREATE USER` and `!UserTable`
gone; `!SingleRootUser`, `XTDB_PASSWORD`, `!UserList`, OIDC), logging moved to Log4j2 with JSON
default, Docker paths (`xtdb.jar`, `config.yaml`), JDK 25 in the image, unknown tables and
columns now error, `GENERATE_SERIES` end-inclusive, `USER` reserved, Postgres operator precedence,
`UPDATE` skips identical versions, JSON output (decimals, error details).

**`SYSTEM_TIME` and the 2.2 floor.** The notes do **not** say 2.2 introduced settable system
time. What they say is that 2.2 enforces the rule backfill depends on: "an explicit `SYSTEM_TIME`
has to be later than the latest transaction's — if you're backfilling, give each transaction its
own" (#6086, fixed in 2.2.0-beta3 and shipped in 2.2.0; before that a system time equal to the
previous transaction's silently returned the earlier data). I did not find the release that first
accepted the `SYSTEM_TIME` option. The floor is therefore **2.2.0 stable** on three independent
grounds: that correctness fix, the authentication model this spike relies on (E7), and the
`version()` probe and `finish-block` endpoint used by the doctor.

**Upgrade and storage-format statements.** I found **no published guarantee** of storage-format
or log-format compatibility across minors, and no long-term-support line. What exists is
per-release "Upgrading" text:

- 2.0 to 2.1: "largely backwards-compatible", with breaking configuration changes (log, Kafka,
  storage, EDN config).
- 2.1 to 2.2: 2.2 writes a **new log format that 2.1.0 cannot read**. Stop-start upgrade is direct
  from 2.1.0; a rolling upgrade must go 2.1.0 to 2.1.1 to 2.2.0. 2.1.1 (released the same day) can
  read both formats so you can roll back, with caveats (Clojure `patch-docs` ignored, new SQL
  fails, delete the replica topic before rolling forward again).
- Within 2.2, pre-releases needed stepping-stone upgrades (rc0, rc1, beta2 via beta3 to rc2).
- Every minor so far has carried breaking configuration or SQL changes.

Consequence: pin the exact image digest and re-run the loader conformance kit on any minor
upgrade; do not use `latest` or `nightly`. Because the BeadLog is the source of truth, an upgrade
that fails to open the old data costs a reload, not data.

### E10. Licence: MPL-2.0 (not legal advice)

I read the upstream `LICENSE` (MPL-2.0 text; the README says "version 2 or, at your option, any
later version"; the image label `org.opencontainers.image.licenses` is `MPL-2.0`). MPL-2.0 is a
file-level copyleft: the obligations attach to **Covered Software** (§1.4) and to
**Modifications** (§1.10, files that change or contain Covered Software).

- **Optional adapter that only speaks pgwire.** The adapter contains no XTDB source file, links no
  XTDB code, and talks to a separate process over the Postgres protocol. It is neither Covered
  Software nor a Modification, and a "Larger Work" (§1.7, §3.3) concerns combining MPL files
  with other code in one work, which does not happen here. **No MPL obligation falls on the
  adapter's own code**, which can stay MIT like the rest of bh. Its dependencies keep their own
  terms (psycopg LGPL-3.0, transit-python2 Apache-2.0).
- **Running the upstream image locally**, or telling operators to pull it from `ghcr.io`: no
  distribution by us, so nothing to comply with.
- **Redistributing or bundling the image** (a release artefact, a vendored tarball, a mirror):
  that is distributing Covered Software in Executable Form, so §3.2(a) requires making the
  corresponding source available and telling recipients how to get it. Pointing at the upstream
  tag and revision `c6c19623` (and keeping the image's source label) is the practical way to do
  that. §3.4 forbids removing licence notices, so do not strip the image's labels or notices. As
  long as we do **not modify** any MPL file, there is nothing else (no copyleft reaching bh). The
  uberjar also bundles third-party components under other licences (I saw Apache-2.0 texts, the
  Unicode licence and JDK notices under `/opt/java/legal`); redistributing means carrying their
  notices, and the jar does not itself ship the MPL text.
- **Modifying XTDB** (patching its sources, a custom image that edits MPL files) triggers §3.1:
  those modified files must be published under MPL-2.0. Nothing here needs that.
- **Patent clauses.** §2.1 grants a patent licence from contributors and §5.2 ends it for a
  licensee who sues over the software; neither affects normal use.

Recommendation: do not bundle or redistribute the image from bh; document the digest and let the
operator pull it. If that changes, add a notices file and a source pointer, and get a real
review.

### E11. Deployment options

| Option | Fit | Notes |
|---|---|---|
| **Container, single node, `Local` log and storage on a bind-mounted real-disk directory** | **Recommended** | Image's default; everything E1 to E8 measured this way. Upstream calls it "standalone, non-production", which matches a rebuildable projection store |
| Same, systemd-managed `docker run`/Quadlet, or `podman` | Variant | Same image; podman not tested |
| `java -jar xtdb.jar` on the host under systemd | Possible | Needs a JDK 21+ (the host has none); loses the image's flags and pinning; more to maintain |
| Embedded in-process node (Clojure, Kotlin, Java) | No | bh is Python; in-process means a JVM sidecar anyway |
| Object storage (S3, Azure, GCS) and Kafka log | No | Multi-node scale-out; extra infrastructure for no benefit at 0.5 GB |
| `playground` or in-memory defaults | Tests only | Non-durable; useful for a conformance run that needs no disk |

Lifecycle ownership follows the Dolt precedent
([`dolt-server-mode-adr.md`](../design/dolt-server-mode-adr.md): "bd spawns and manages the
server; bh does not supervise it"): **bh does not start, stop or supervise XTDB**. The operator
does, and bh only connects, probes and reports.

## Verdict — **GO**

XTDB 2.2.0 is operationally acceptable as an **optional, off-by-default, operator-run, loopback
only, single-node container** on the factory host. The evidence: idle about 390 MiB; the full bh
loads peak at 0.55 to 0.9 GiB and fit a **2 GiB** cap with margin (1.5 GiB also clean); a
reload of about 6 minutes; no data loss or half-applied transaction across three `kill -9`s;
restore from a stopped copy verified by content digest; MPL-2.0 imposes nothing on a pgwire-only
adapter; psycopg works. The conditions are not optional, because each is a real hazard seen in
the run: password or loopback binding must be set (default is open), the stock healthcheck must be
replaced, a heap OOM leaves the node up but not ingesting, and psycopg is the only usable driver.
GO is for a rebuildable projection store, not for XTDB as the only copy of anything.

## Recommendation

**1. Deployment mode (for the operator runbook that the implementation molecule writes).**

```sh
docker run -d --name xtdb-beadlog --restart unless-stopped \
  --memory 2g --memory-swap 2g --user "$(id -u):$(id -g)" \
  -p 127.0.0.1:<port>:5432 \
  --env-file <operator-only file with XTDB_PASSWORD=...> -e XTDB_LOG_FORMAT=json \
  --health-cmd 'wget -q -O /dev/null http://127.0.0.1:8080/healthz/alive' \
  --health-interval 10s --health-start-period 30s \
  --log-opt max-size=10m --log-opt max-file=3 \
  -v <real-disk dir>:/var/lib/xtdb \
  ghcr.io/xtdb/xtdb@sha256:<pinned digest of the validated 2.2.x>
```

- Memory: `--memory 2g` with default JVM flags. 1.5 GiB is also clean. Do not go below 1 GiB;
  at 1 GiB add `JDK_JAVA_OPTIONS=-XX:MaxRAMPercentage=40`. Never raise the cap without bound: the
  heap is a fixed fraction of it.
- Data dir: a real-disk path, never `/tmp` (tmpfs on this host). The doctor should refuse a
  `tmpfs` or `ramfs` mount. Budget 0.5 GB per bh-scale store plus log growth.
- Keep the node **off unless needed**. A 20 s cold start and a 390 MiB idle node make on-demand
  start for a backfill or query session cheap, and match "every behaviour is off by default".
  Always-on is an operator choice, not a default.
- Loopback bind plus `XTDB_PASSWORD`. Never publish 3000 or 8080 off-host.
- Backup: stop, `cp -a` the data dir, start. Or rely on re-loading the `.bdlg`.
- Upgrade: pin the digest; before changing the pinned digest, copy the data dir, start the new
  image against a copy, run the loader conformance kit, then switch.

**2. Packaging: two packages, matching the two package classes
([package-class ADR](../design/package-class-library-vs-plugin-adr.md)).**

| Package | Class | Contains | Depends on |
|---|---|---|---|
| `packages/beadlog-xtdb` | Library (never imports `beadhive`) | The loader and reader: BeadLog events to the `bh-w56ts` layout (b) tables; as-of reads back to BeadLog; psycopg connection setup (`StrDumper`, transit dumper, `fallback_output_format='transit'`), single-type column coercion, `version()` gate, dangling-edge report | `beadlog` (stdlib-only), `psycopg[binary]>=3.2,<4`, `transit-python2==0.8.321` (or an in-package transit writer) |
| `packages/beadhive-beadlog-xtdb` | **Plugin** (`plugin.json`; imports only the allowed `beadhive` contract surfaces and `beadlog-xtdb`) | Manifest, config schema, `plugin beadlog-xtdb doctor` and `status`, binding of the loader into `bh backfill` through a new `beadlog.store` capability | `beadhive`, `beadlog-xtdb` |

- The ADR already says the XTDB loader is "its own package depending only on `beadlog`". The
  split keeps that true: the library is usable by other BeadLog tools without BeadHive; only the
  thin plugin carries bh wiring (config, doctor, capability binding), as
  `beadhive-tracker-github` does for `tracker.adapter`.
- **Opt-in the same way `tracker-github` is:** a public extra `beadhive[xtdb]` and a dev/CI
  dependency group, both naming the plugin; the root wheel installs without psycopg or transit;
  the plugin id is added to the built-in catalog only when the package lands; root resolves it
  lazily, after manifest selection, never by static import.
- **Manifest sketch.** Capability `beadlog.store` API 1 (name to be settled with `bh-65jen`).
  Namespace `plugins.beadlog-xtdb`, host-owned, **`enabled` default false**. Keys: `host`
  (default `127.0.0.1`; refuse a non-loopback value unless a second explicit key allows it),
  `port`, `database`, `user`, `minimum_version` (default 2.2.0), `maximum_version_exclusive`
  (default 2.3.0). Credential `xtdb-password` from an environment variable, never stored in
  config. Permission `network.xtdb.pgwire` (loopback). `docker` as an optional executable only
  for doctor hints.
- **Doctor and status** (read-only, no write grant): TCP connect; psycopg connect; parse
  `current_setting('xtdb.version')` against the floor and ceiling; `healthz/alive` and
  `healthz/started` by GET (not HEAD); data-dir filesystem is not tmpfs; auth mode (warn if a
  no-password connection succeeds); scan the container log for `OutOfMemoryError`, since a plain
  `SELECT` still works on a wedged node. A write-then-read probe belongs behind an explicit flag.
- Not in scope for the plugin: starting, stopping or upgrading XTDB (the operator owns it).

**3. Version floor and pin.** Floor `>= 2.2.0` (stable, not a pre-release); ceiling `< 2.3` until a
2.3 has passed the loader conformance kit. Pin an image digest; never `latest` or `nightly`.

**4. Follow-ups for the planner (suggestions; this spike files nothing).**

- Implementation beads for the two packages and the capability, after `bh-psu1b` and `bh-65jen`.
- A doctor check that probes with GET and scans for `OutOfMemoryError`, because the image's check
  lies and a heap OOM does not stop the process.
- A loader contract line: batch at least several hundred documents per transaction, since the log
  grows per transaction (E3), but remember `SYSTEM_TIME` backfill needs one transaction per
  distinct recorded time (`bh-3jp2g`); the two pull opposite ways and the loader needs a policy.
- A short upstream report for the HEAD healthcheck bug and the empty `ParameterDescription` for DML
  (psycopg-only), if the operator wants to send them. Nothing was reported from here.
- If a second driver ever matters, retest after XTDB fixes parameter description for DML.

**5. Open and not verified.** Host power-loss durability and the `Local` log's fsync policy; log
growth and compaction over weeks; running a copy during active writes; what `/healthz/*` reports
on a heap-OOM node; podman and a non-container JVM; Flight SQL/ADBC reads; 2.0 to 2.2 direct data
compatibility (irrelevant to a fresh 2.2 install).
