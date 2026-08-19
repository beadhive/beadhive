# Read-path measurement: `bh doctor` per SOURCE, and today's fact-set size

Measurement record for **bh-13spb.1**, the numbers the read-path cache molecule (bh-13spb) is
supposed to be designed against. It is a snapshot, not a spec: every number below is stamped
with the host and fleet it was taken on so a later reading can be compared honestly rather than
averaged into it.

## Provenance of these numbers

| | |
|---|---|
| Date | 2026-08-19 |
| Host | `beadhive-factory`, Debian 13, Linux 6.12.100, x86_64, 24 cores, 47 GB RAM |
| bh | 0.12.2 (installed binary; the in-process harness ran this repo's `src/`, same code) |
| Fleet | **20 registered hives**, **15 local `.beads/` stores** (14 of them registered — `prototypes/briancripe/observaloop` is an unregistered second clone of the `obs` store), **30 git repos on disk** under `$GIT_WORKSPACE`, **131 bh worktrees** |
| hitch | enabled, **7** seat-aligned profiles present on this host |

The bead's brief says 21 hives; the dispatch note says 20/15. **Observed: 20 registered, 15
`.beads/` stores.** Use the observed numbers.

## Method

Two independent instruments, both read-only against the fleet:

1. **Per-source attribution** — `scripts/measure_doctor_sources.py` (throwaway, committed for
   reproducibility). It monkeypatches `subprocess.run` — the single seam every bh spawn funnels
   through, including the handful of deliberate `run.run` bypasses — to record argv, cwd and wall
   time, and monkeypatches `doctor._timed` to tag each record with the section executing at the
   time. It then calls `doctor.doctor_payload()` **in-process**, so its totals exclude CLI
   start-up (~1–2 s) that an external `bh doctor` also pays.
2. **Verb level, cold vs warm** — `scripts/bench_read_path.py` (bh-amq08), run from the
   registered clone.

"Cold" means bh's own JSON read-caches (`metadata.json`, `bd-schema-version.json` under
`config.cache_dir()`) were deleted first. It does not clear the OS page cache or dolt buffers.
"Warm" is the immediately following run.

Reproduce with:

```sh
# from the registered clone, NOT a bh work worktree
uv run python3 scripts/measure_doctor_sources.py > /tmp/sources.json
just bench-read-path
```

---

## 1. Verb level, cold vs warm

```
VERB                        COLD      WARM       GAP
hive list                  0.93s     1.37s    -0.45s
hive status --json         1.25s     1.11s     0.14s
bd export (bh)             2.52s     2.64s    -0.12s
hive ready                26.76s    21.59s     5.17s
doctor                     62.23s    45.83s    16.40s
```

Reproduces the dispatch-note baseline within noise (doctor 62.6/47.5 → 62.2/45.8; hive ready
25.9/21.5 → 26.8/21.6). `bench_read_path.py` flags `hive ready` as **FAILED** — that label is a
false alarm here: `hive ready` legitimately exits 1 when one of its checks fails on this hive,
which it does. The timing is real (the in-process run below measured the same 20–21 s).

## 2. `bh doctor` attributed to SOURCES — warm

Warm run: **45.97 s wall, 317 subprocess spawns, 40.25 s (87.6 %) inside those spawns.** Two
back-to-back runs measured 45.97 s and 45.52 s — reproducible to ~1 %.

Sources costing >0.1 s. A "source" is the reading, independent of which hive it was taken
against: `bd config get beads.role` in 15 hives is **one source read 15 times**, which is the
number this molecule needs.

| Source (external reading) | calls | total | consumed by |
|---|---:|---:|---|
| `bd config get beads.role --json` | 15 | 7.09 s | `beads_role` |
| `bd config get sync.remote --json` | 15 | 7.00 s | `node_id` |
| `bd config get issue_prefix --json` | 14 | 3.72 s | `prefix_mismatches` |
| `bd sql -q "SELECT MAX(version) … schema_migrations"` | 9 | 2.41 s | `warnings` |
| `hitch profile preflight <seat>` (7 distinct seats) | 7 | 12.70 s | `seats` |
| `bd dolt status --json` | 15 | 1.84 s | `warnings` |
| `bd --version` | 12 | 1.30 s | `warnings` |
| `bd config get node_id --json` | 1 | 0.26 s | `node_id` |
| `bd show <container-bead> --json` | 11 | 2.16 s | `molecules` |
| `git rev-parse --show-toplevel` | 34 | 0.19 s | `dispatch` |
| `git rev-parse HEAD` | 28 | 0.18 s | `metadata_rollup` |
| `git rev-parse --verify --quiet <ref>` | 28 | 0.17 s | `dispatch` (14), `warnings` (14) |
| `git for-each-ref` (branch scan) | 20 | 0.12 s | `molecules` |
| `git for-each-ref` (signature scan) | 15 | 0.10 s | `warnings` |
| everything else (<0.1 s each) | 91 | 0.76 s | — |

**Rolled up by tool — this is the shape of the problem:**

| tool | spawns | total | per spawn |
|---|---:|---:|---:|
| `bd` | 93 | 25.90 s | **278 ms** |
| `hitch` | 7 | 12.70 s | **1 814 ms** |
| `git` | 186 | 1.15 s | **6.2 ms** |
| `systemctl` | 28 | 0.27 s | 9.6 ms |
| `dolt`, `ps`, `pgrep` | 3 | 0.22 s | — |

### The correction this table makes to bh-i6e5g

bh-i6e5g says doctor's cost "is not computation — it is roughly 60 subprocess spawns". The
count is off (317 warm, 714 cold) and, more importantly, **spawn overhead is not the mechanism.**
`git` is spawned twice as often as everything else combined and costs 1.15 s total; a `git`
spawn is 6 ms. The cost is that `bd` costs ~278 ms per invocation and `hitch` ~1.8 s. Of `bd`'s
278 ms, ~108 ms is bare process start-up (`bd --version`, 12 samples) and the remaining ~170 ms
is the dolt read behind it.

Practically: **"make fewer subprocess calls" is the right prescription, but only for `bd` and
`hitch`.** Coarsening `git` calls would buy nothing measurable.

### `warnings` — the section nobody had diagnosed

bh-i6e5g left `warnings` (6.58 s / 11.1 %) explicitly undiagnosed. Attributed here (warm,
6.59 s over 111 spawns):

| source | calls | total |
|---|---:|---:|
| `bd sql … schema_migrations` (schema-version probe) | 9 | 2.41 s |
| `bd dolt status --json` | 15 | 1.84 s |
| `bd --version` | 12 | 1.30 s |
| `bd dolt remote list --json` | 1 | 0.14 s |
| `dolt --data-dir … sql -q …` | 1 | 0.12 s |
| ~72 git/pgrep calls (for-each-ref, rev-parse, cat-file, ls-files, rev-list, config) | 72 | ~0.42 s |

It is **the same pattern after all** — 37 `bd` spawns are 5.8 s of its 6.6 s — but a different
fan-out: a per-hive dolt-health/schema probe rather than a config-key read. Notably, 12 of the
37 are a bare `bd --version` repeated verbatim (see §3).

### Cold adds two more sources

Cold run: **60.29 s wall, 714 spawns, 51.4 s (85 %) in subprocesses.** New at the top:

| source | calls | total | section | why cold only |
|---|---:|---:|---|---|
| `bd init --prefix schemaprobe<hex> --non-interactive` | 1 | **4.96 s** | `warnings` | populates `bd-schema-version.json`; it creates a throwaway bd repo in a temp dir |
| `bd dolt remote list --json` | 15 | 2.34 s | `metadata_rollup` (14), `warnings` (1) | `metadata.json` miss |
| `bd dolt status --json` | 29 | 3.54 s | `metadata_rollup` (14), `warnings` (15) | `metadata.json` miss |
| `git ls-remote origin` | 1 | 0.90 s | `metadata_rollup` | network |
| ~250 extra `git` calls (rev-list, status, log, ls-remote) | ~250 | ~1.7 s | `metadata_rollup` | `metadata.json` miss |

`metadata_rollup` goes 0.20 s warm → 10.81 s cold; `warnings` 6.59 → 11.65 s. That is the whole
16.4 s cold/warm gap, and it is already cache-shaped — `metadata.json` and
`bd-schema-version.json` are prototypes of exactly the layer this epic proposes.

## 3. Readings repeated verbatim inside ONE run

Identical argv **and** identical cwd, occurring more than once in a single `doctor`:

| | warm | cold |
|---|---:|---:|
| redundant calls (calls beyond the first) | 54 | 96 |
| wall time they cost | **1.46 s** | **3.29 s** |
| share of the run | 3.2 % | 5.5 % |

Almost all of it is two items:

| repeated reading | times | cost | sections |
|---|---:|---:|---|
| `bd --version` | 12 | 1.30 s | `warnings` (12×, same cwd) |
| `bd dolt status --json` per hive (14 hives × 2) | 2 each | ~1.8 s cold | `metadata_rollup` + `warnings` |
| `git config --global --get-regexp …` | 4 each | 0.05 s | `group_auth` |
| `git rev-parse --show-toplevel` / `cat-file -p` / `rev-parse --verify` | 2 each | ~0.10 s | `dispatch` + `warnings` |

**Verdict on the per-run memo — this is the inconvenient half.** A memo beneath the persistent
cache is worth **1.5 s of 46 s warm (3 %) / 3.3 s of 60 s cold (5 %)**, and 1.3 s of that is a
single call — `bd --version` invoked twelve times with the same cwd, which is a one-line
`@lru_cache` at its call site, not a cache layer. The memo is *real but small*; do not size a
layer for it. If you want it, take `bd --version` and the `bd dolt status` double-read
(`metadata_rollup` and `warnings` both ask each hive the same question) and stop there.

The expensive redundancy is **not** verbatim repetition. It is **three separate full passes over
the same 15 hives reading one config key each** — 44 `bd config get` spawns, **17.81 s warm
(39 % of the run)**, for three keys that live in the same two files per hive. A memo keyed on
argv cannot collapse those, because the argv differs. Only a **coarser source** ("this hive's
config", read once) can. That is bh-i6e5g's point and the measurement supports it strongly.

For scale, reading those files directly instead:

```
15 × .beads/config.yaml, plain read:   1.57 ms
15 × .beads/config.yaml, yaml.safe_load: 44.55 ms
```

**44 ms against 17.81 s — a factor of ~400.** (Caveat: `issue_prefix` lives in the dolt `config`
table, not in `config.yaml`, so a pure-file read covers `sync.remote` and `beads.role` but the
prefix needs either one dolt read per hive or bd growing a multi-key read. `store_locator`'s
"pure file op, no subprocess" discipline is the model for the two that can be.)

## 4. `bh hive ready`: 3.82 s → 21 s, resolved

The epic recorded 3.82 s; it measures 21.6 s warm / 26.8 s cold. bh-ls1ks guessed it rides the
same hitch hook that costs doctor 13 s. **Half right, and the other half is a source class
nobody in this molecule has named yet.**

In-process run of `hive_ready.run_check()` (20.0 s wall, only **10 subprocess spawns**,
12.37 s of them), with a `cProfile` pass over the same call (21.8 s total):

| | cumulative | mechanism |
|---|---:|---|
| `hitch_plugin._readiness` → `seat_reports` | **12.44 s** | 7 sequential `hitch profile preflight` spawns — confirms bh-ls1ks |
| `hive_ready._observaloop_checks` | **8.67 s** | 3 asyncio MCP client sessions: `is_available` 4.09 s, `visualizer_status` 2.32 s, `endpoint_for` 2.27 s; includes a 1.21 s `import fastmcp` |
| everything else | ~0.3 s | 10 spawns total, 2 of them git |

`hitch` + observaloop is ~97 % of `hive ready`. **Confirmed for bh-ls1ks; and the observaloop
probe is a finding of its own** — 8.7 s of network/MCP round-trips that appear in **no**
subprocess accounting, because they are not subprocesses. `doctor` does not pay it (its
`observability` section measures 0.00 s), which is why the doctor-side analysis never saw it.

**Consequence for the layer contract (.2): a "source" is not "a subprocess".** Any source
interface that assumes argv-shaped readings will silently fail to cover the single largest
non-hitch cost in `hive ready`.

## 5. Fact-set size today

Counted with `bd -C <hive> sql -q "SELECT COUNT(*) …"` against each local `.beads/` store —
read-only SQL, one query per hive. `bd history` was **not** re-derived per bead: the `events`
table is bd's own transition log (`created`, `claimed`, `closed`, `status_changed`, `reopened`,
`label_added/removed`, `dependency_added/removed`, `updated`, `commented`) and is the same
information at 1 query instead of ~6 700 subprocesses.

### Entities

| entity | count | source |
|---|---:|---|
| registered hives | 20 | `bh hive list` |
| local `.beads/` stores | 15 (14 registered) | filesystem |
| git repos on disk | 30 | `find -name .git` under `$GIT_WORKSPACE` |
| bh worktrees | 131 | `bh worktree list` |
| **beads (issues)** | **6 673** | `SELECT COUNT(*) FROM issues`, 14 dolt stores + 14 lines in hermes' `issues.jsonl` |
| dependencies | 6 820 | `dependencies` |
| labels | 14 410 | `labels` |
| comments | 86 | `comments` |
| on-disk `.beads/` bytes | 749 MB (436 MB of it `bh`) | `du -sh` |

Per hive (`prototypes/briancripe/observaloop` excluded as a duplicate store;
`agentguides/hermes-plugin` runs bd in embedded mode where `bd sql` is unsupported — counted
from `issues.jsonl`):

```
HIVE                                   ISSUES  EVENTS   DEPS  LABELS  COMMENTS  DOLT_COMMITS
github/beadhive/beadhive                 3108    5041   3186    7039        32          7752
github/briancripe/agent-hitch             780     106    869    1403         4          2224
github/beadhive/beadhive-ui               630    1656    607    1209        10          2513
github/briancripe/observaloop             446    2138    471    1052        13          1285
github/briancripe/nvidia-hackathon        435      31    443     895         2          1069
github/beadhive/infra                     298     348    294     510        11           840
github/beadhive/baml-harness              294     751    314     440         4          1313
github/agentguides/runtime                267    1672    200     962         0           694
github/beadhive/claude-plugin             187     127    192     423         3           590
github/agentguides/infra                  165       0    203     348         7           363
github/beadhive/beadhive-app               53     236     41      88         0           282
github/agentguides/hermes-plugin           14       —      —       —         —             —
github/agentguides/claude-plugin            7      49      0      35         0            34
github/briancripe/agentic-git-flow          3       0      0       6         0            33
TOTAL                                    6673   12155   6820   14410        86         18992
```

### Events

**12 155 rows in the `events` tables fleet-wide.** Distribution (bh hive, 5 041 rows):

```
label_added 1993 · updated 999 · created 939 · closed 480 · label_removed 241
claimed 178 · dependency_added 130 · status_changed 68 · dependency_removed 10
commented 2 · reopened 1
```

Lifecycle transitions proper (`created`/`claimed`/`closed`/`status_changed`/`reopened`) are
**1 666 of 5 041 = 33 %**; label churn alone is 44 %.

**A caveat the contract bead must not miss:** every one of the bh hive's 5 041 event rows is
dated 2026-08, while its oldest issue is 2026-06-29. The `events` table is **not** a complete
archive — either event capture began recently or older rows are gone. `dolt_log` (18 992 commits
fleet-wide, 7 752 in bh) is the fuller record of *when writes happened*, but it is
commit-granular, not transition-typed. Neither is `bd history`, which is the diff of consecutive
versions and is what CLAUDE.md forbids destroying. **If .2's event layer wants complete history
it must read the dolt version history, not the `events` table.**

### Trajectory

Issues created per month:

| hive | 2026-06 | 2026-07 | 2026-08 (19 d) |
|---|---:|---:|---:|
| `bh` | 92 | 1 054 | **1 962** |
| `bhui` | — | 253 | 377 |
| `ah` | — | 523 | 257 |
| `obs` | 131 | 232 | 83 |

The `bh` hive alone is at **~103 issues/day** in August, up from ~34/day in July — a 3× monthly
growth that shows no sign of a knee. Fleet-wide the corpus went 0 → 6 673 beads in **51 days**
(~130/day). Events in the bh hive: **2 157 in the last 7 days (~308/day)**; extrapolated
fleet-wide from the same ratio, on the order of **600–700 events/day**.

### What that means for sizing (.2's promotion triggers)

- **Entities are small and will stay small.** 20 hives, 30 repos, 131 worktrees. Even at 10× that
  is thousands of rows, not millions. A file-backed entity store is sized for **years**.
- **Beads are medium and growing fast.** 6 673 today; at the current 130/day the fleet passes
  **50 000 in one year** and the `bh` hive alone passes 40 000. A single JSON file per hive is
  fine today (3 108 rows) and is uncomfortable at 40 000 — a per-hive file with an index, or
  sqlite, is the honest one-year answer. **Promotion trigger: beads per hive, not fleet total.**
- **Events are the only thing with a real growth problem.** ~600–700/day fleet-wide and rising is
  ~230 000/year. Append-only file is fine; anything that rewrites the whole file on each append
  is not. **A file-backed event store must be append-only, or sized for a month.**
- **Bytes are already large.** 749 MB of `.beads/` for 6 673 beads — 112 KB per bead of dolt
  storage. The cache should hold *derived facts*, not copies of stores.

## 6. Does the measurement support the epic's premise?

The epic (bh-13spb) assumes ingest is where the time goes and caching is the lever.
bh-i6e5g argues that is half right. **The numbers support bh-i6e5g. Stated plainly:**

1. **Ingest is where the time goes — yes, overwhelmingly.** 87.6 % of a warm doctor and 85 % of a
   cold one is spent inside external readings. There is essentially no computation to optimize.
2. **A cache makes a repeat read fast — yes.** This is already demonstrated, not hypothesized:
   `metadata.json` + `bd-schema-version.json` are today's cache and they are worth **16.4 s of
   62 s (26 %)**. A fact cache covering the rest could plausibly take a warm `doctor` under 5 s.
3. **A cache does NOT fix the cold path — correct, and this is the load-bearing finding.** After
   every bh cache is warm, `doctor` still costs **45.8 s**, of which **40.2 s is `bd` and `hitch`
   subprocesses** that no cache in bh removes on a first read. The remaining lever is *fewer and
   coarser source calls* — 45 `bd config get` spawns collapsing to one config read per hive
   (17.8 s → ~0.05 s for two of the three keys), and 7 sequential `hitch` spawns run concurrently
   (12.7 s → ~2.4 s). Those two changes alone are worth **~28 s of the 45.8 s warm floor** and
   they are *independent of the cache*.
4. **The per-run memo is the cheapest win only in the sense that it is trivial — it is not a big
   one.** 1.5 s warm / 3.3 s cold, mostly one repeated `bd --version`.

So: build the cache, but do not let the cache justify leaving the source shape alone. The right
order is bh-i6e5g and bh-ls1ks **first or alongside** — coarsen the sources, then cache the
coarse ones. Caching the current shape means building a cache with 45 config-key sources per
refresh where 15 hive-config sources would do, and .4's adoption would inherit that.

## 7. What is still missing

The numbers above are sufficient to choose cadences and promotion triggers in .2. These are not
covered and .2 should not pretend otherwise:

- **Only one host.** Every number is `beadhive-factory`. A laptop with 4 cores and slower disk
  will shift the `bd`/`hitch` per-call costs, which is exactly where the time is. Cadences chosen
  from these numbers should be expressed as *ratios* (e.g. "refresh if older than N × the measured
  read cost"), not as absolute seconds.
- **No invalidation-frequency data.** We know how many facts exist and how fast they grow; we have
  not measured how often an existing fact *becomes stale* between reads. That is what actually
  decides a TTL. The `events` timestamps could answer it, but the events table's incomplete
  history (§5) makes any answer from it suspect today.
- **Other verbs are unattributed.** Only `doctor` and `hive ready` were taken apart. `hive list`
  (1.1 s), `hive status` (1.1 s) and `bd export` (2.6 s) are cheap enough to ignore for now, but
  no verb outside those five was measured at all.
- **The `events` table's history gap is unexplained** and should be understood before .2 designs
  the event layer on top of it. Do not resolve it by compacting or flattening anything.
