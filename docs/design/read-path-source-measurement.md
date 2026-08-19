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

```text
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

```text
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

```text
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

```text
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

## 8. Re-measured after bh-i6e5g (2026-08-19)

The config-read cluster of §2 is gone. `sync.remote` now comes off `.beads/config.yaml`
(`bd.sync_remote`, a plain file read) and `beads.role` off `git config --get` with the process cwd
pinned to the hive (`bd.beads_role` — bh-s08me's scoping, kept and covered by a real-git test);
`bd --version` is memoized for the process. Same instrument as §1/§2 (in-process
`doctor.doctor_payload()`, chdir'd to the registered clone), same host, runs interleaved
before/after to absorb host noise:

| section | before (2 runs) | after (2 runs) |
|---|---:|---:|
| `node_id` | 8.40 s / 9.44 s | **0.47 s / 0.41 s** |
| `beads_role` | 7.89 s / 8.90 s | **0.12 s / 0.11 s** |
| `prefix_mismatches` | 7.72 s / 5.36 s | 5.81 s / 4.86 s (unchanged by design) |
| `warnings` | 9.04 s / 7.17 s | 6.25 s / 5.57 s (the `bd --version` memo) |
| **whole payload** | **42.11 s / 39.66 s** | **32.92 s / 29.03 s** |

**~10 s off a ~41 s warm run (≈25 %), and 44 `bd config get` spawns became 14.** The two
retargeted sections went 16.3–18.3 s → 0.5 s combined; the memo took ~1.4 s more out of
`warnings`. `bd --version` fell 12 spawns → 1.

Not changed, and why:

- **`prefix_mismatches` still spawns one `bd` per hive.** `issue_prefix` lives in the dolt
  `config` TABLE, not in any file — `.beads/config.yaml` does not carry it and `metadata.json`
  records engine/database only. One `bd` read per hive is the floor until the cache layer covers
  it; that is bh-13spb's job, not a source-shape one.
- **The rest of `warnings`** (`bd sql … schema_migrations`, `bd dolt status`) is the same fan-out
  shape with different keys and is tracked under bh-b5v4y.
- **`molecules`** is unchanged: its `git for-each-ref` calls are 0.12 s for 20 spawns (§2), so
  there is nothing to win there; its real cost is 11 `bd show` calls, also bh-b5v4y.
- `seats` swings 2.8–16.3 s run to run here (hitch), independent of this change — bh-ls1ks.

`just bench-read-path` was NOT the instrument for the "after" column: it shells the *installed*
`bh`, which does not carry this change, so it can only reproduce the "before" numbers until this
lands.

## 9. `warnings`' `bd dolt status` / `bd sql schema_migrations` probes parallelized (bh-ti7ws)

bh-3qo60 (§8's note) parallelized `prefix_mismatches`; this bead does the identical treatment for
the other half of `warnings` §8 named as unchanged: `_bd_schema_skew_warnings`'s per-hive
`safety._bd_dolt_mode` (`bd dolt status`) and `hive_schema.refresh` (`bd sql … schema_migrations`)
now fan out across a `ThreadPoolExecutor` instead of running strictly sequentially across the 15
hives, consuming `pool.map`'s results positionally to keep the reported warnings in registry
order.

Same instrument as §8 (`scripts/measure_doctor_sources.py`, in-process `doctor.doctor_payload()`),
3 runs each side, interleaved (after → before, immediately back to back) on this host:

**Caution on this host's load** — measured at load average 4.0–6.2 (5-minute), with several
long-lived `dolt sql-server` processes present; well below the ~20 the bead flagged as possible
but still not a clean/idle host, so the spread below is real host noise, not a controlled
benchmark:

| run | before (`git stash` off) | after |
|---|---:|---:|
| 1 | 5.46 s | 1.89 s |
| 2 | 5.42 s | 1.82 s |
| 3 | 5.60 s | 1.79 s |

`warnings`: **5.42–5.60 s → 1.79–1.89 s** (baseline 5.53 s confirmed within noise; ~3.0x faster,
consistent across all 3 runs). The `bd sql schema_migrations` (9 calls) and `bd dolt status` (15
calls) spawns still total ~5.8 s of subprocess time each run (`by_source` in the raw JSON), but
now overlap inside the pool instead of summing serially into wall time.

### The write path (`hive_schema.refresh` → `hive_schema.save`)

Each hive writes its OWN manifest file (`hives/<provider>/<org>/<repo>.yaml`, distinct paths per
hive), so there is no file-level write race to worry about — 15 concurrent writers never target
the same file. The one piece of shared, mutable state is `hive_schema.py`'s module-level
`ruamel.yaml.YAML()` instance, used by both `save` (dump) and `load`/`try_load` (load), now inside
the same pool. That is the exact class of bug bh-3qo60 found in `config.py`'s equivalent
singleton (measured there: 147/200 failures at 16 threads unguarded, 0/200 with a lock) — guarded
here the same way, with a `threading.Lock` around every `_yaml.load`/`_yaml.dump` call in
`hive_schema.py` (the other four modules bh-3qo60 flagged with the same shape, `bh-vb5nd`, are
left alone).

`bd._STRICT_READS` (a `ContextVar`, invisible inside a `ThreadPoolExecutor` worker — the hazard
bh-3qo60's review caught) does not apply here: none of `_bd_dolt_mode` / `probe_raw_schema_version`
/ `_local_bd_version_string` goes through `bd.py`'s `run`/`json` wrapper (the only place that
reads `_STRICT_READS`) — they all call `run.run`/`subprocess.run` directly. Confirmed by reading
every call in the pooled path, not assumed.

Not changed: the git-spawn loops in `_data_warnings` (`git ls-files`, `_local_commits_while_not_primary`)
— measured elsewhere at ~6 ms/call, the wrong target per bh-b5v4y.

## 10. The 4.96 s scratch `bd init` (bh-j68p3): minting is unavoidable

Worked bh-j68p3's own question list, most-saving first, on this same host
(beadhive-factory, load average 1.5–4.3 over 24 cores at measurement time — idle-ish, not a
clean bench):

**1. Can `LatestVersion()` be read without minting a repo? No CLI surface reports it.** Checked
every candidate:

- `bd version --json`'s `schema_version` — hardcoded `1` (confirmed: same value as the
  module docstring's `bd dolt status --json` decoy; it is the JSON-envelope version, not a
  migration count).
- `bd migrate --inspect` — the module docstring's other decoy (`GetLocalMetadata(ctx,
  "bd_version")`, a release string); measured here against a live server-mode store it
  printed a blank `Schema Version:` with a bogus mismatch warning, which is itself further
  evidence this surface is not meant to answer the question.
- The 9x `bd sql … schema_migrations` calls `warnings` already makes (§2/§9) answer a
  **different question by construction**: each is `MAX(version)` for one EXISTING hive's
  store, which can legitimately lag the binary's `LatestVersion()` (a store that hasn't been
  migrated up yet). Reusing the max observed across hives as a stand-in would be right only
  by coincidence (on this host today, every checked store happens to be fully migrated to
  v62, matching the binary) and silently wrong the day a hive lags — the kind of fragile
  workaround bh-j68p3 explicitly said not to build. **Not redundant; nothing removed here.**

**2. Does the cache key already make this once-per-bd-upgrade rather than once-per-cache-clear?
Yes, already.** `local_bd_schema_version` keys `bd-schema-version.json` on `bd --version`'s
exact string (`dolt_health.py`, `_local_bd_version_string`/`local_bd_schema_version`), not on
anything cache-clear-shaped. A `brew upgrade beads` (or a Nix rebuild) naturally invalidates it;
clearing `config.cache_dir()` without a binary change re-mints once and re-caches under the same
key. This is the "cheaper win" bh-j68p3 named as the fallback — it was already the design, not a
gap. Covered by the pre-existing `test_local_bd_schema_version_caches_by_bd_version_string` /
`test_local_bd_schema_version_reprobes_after_a_bd_upgrade`.

**3. Is 4.96 s the right price for `bd init`? Yes — it's dolt migration replay, not incidental
scaffolding.** Isolated measurement, 3 samples per condition, same host/load window:

| variant | run 1 | run 2 | run 3 |
|---|---:|---:|---:|
| `bd init --prefix … --non-interactive` (as it was) | 4.77 s | 4.89 s | 4.79 s |
| + `--skip-agents --skip-hooks` | 4.76 s | 4.90 s | 4.90 s |
| + `--skip-agents --skip-hooks --quiet` | — | 4.97 s | 4.92 s |
| `git init` pre-run (removes the "init a git repo" step too) | — | — | 4.71 s |
| bare `dolt init` (no bd, no migrations) for comparison | 0.09 s | — | — |

AGENTS.md generation, git-hooks install, Cursor scaffolding, output formatting, and even git
repo initialization are NOT the cost — every variant lands in the same ~4.7–4.9 s band, while a
bare `dolt init` (no schema migrations applied) is 90 ms. The gap is bd replaying every
migration the binary knows against a fresh embedded store — the actual `LatestVersion()` proof,
by construction, and there is no cheaper way to get it from the outside.

**What changed: nothing in `_scratch_probe_local_version`'s `bd init` invocation.** The table
above shows `--skip-agents --skip-hooks` measured free on wall time, and an initial pass added
them anyway for the filesystem-write side effect. Reviewed back out: they buy zero measured
time against a directory that's `rm -rf`'d moments later, and some `bd` builds reject an
unknown flag outright (`Error: unknown flag: --skip-bogus`), which would silently blank the
entire schema-skew section on such a build (`doctor._bd_schema_skew_warnings`'s `local.version
is None` short-circuit) rather than just failing loud. Zero upside, real downside — not worth
it. This bead's only code change is the failure-detail plumbing that lives in bh-j50yv's
commit; §10 itself is a documented negative result.

**Full `bh doctor` cold/warm re-measurement was not run via `just bench-read-path` here**: that
script drives the `bh` binary on `$PATH` (the machine's globally `uv tool install`ed 0.12.2),
not this worktree's modified `src/`, and re-installing the global `bh` mid-flight on a shared
host with other concurrent seats was judged the wrong risk to take for a change already shown,
at the isolated-call level, not to move the number. The isolated measurement above (same
subprocess, same host, same load window) is the honest substitute: it shows the target call is
unchanged at ~4.7–4.9 s either way, which is expected since §10.3 established that time is
migration replay, not something `--skip-agents`/`--skip-hooks` ever touched. Net effect on `bh
doctor` cold wall-time from this bead: **~0 s** — there is, in the end, no functional code
change here (the flags were added, then reviewed back out; see "What changed" above), so a
before/after wall-time delta is vacuously zero. The 4.96 s line item stands as a measured,
unavoidable cost of learning `LatestVersion()` the only way bd's CLI surface allows. It has no
bearing on the separate bh-j50yv fix in this same molecule — that one corrects how a probe's
FAILURE DETAIL is reported when a real error shares a subprocess with a harmless `Warning:`
line (see `_probe_failure`'s docstring in `dolt_health.py`), not whether the probe treats a
`Warning:` as a failure — measurement there (bh-j50yv's own review) found `bd` already exits 0
on a warning-only stderr, so that was never the mechanism.

### §10.1 — Re-verdict at 30% (bh-zzoek, 2026-08-19): still unavoidable

`docs/BH_DATA_PIPELINE.md` §4's "What has no owner" carries the full re-check (same host, same
line item, now worth ~26–30% of cold `bh doctor` rather than ~8% — the denominator shrank, not
this cost). Summary against §10's own four questions, re-asked from scratch against `bd version
1.1.0 (dev)` and post-`bh-gy7bc` code rather than inherited: (1) the cache invalidates on a bd
upgrade only, confirmed; (2) `bh doctor` only needs the value when there's a hive to compare
against — a real gap (the probe fired even with zero local checkouts) fixed in
`doctor._bd_schema_skew_warnings`, though it doesn't move this repo's own number (20/20
registered hives have local checkouts); (3) bd's CLI surface as of 1.1.0 still has no way to
learn `LatestVersion()` without a real database — `bd migrate --inspect`, `bd info --schema`,
and `bd version --json` all checked fresh, none answer it; (4) filed as a bd-side ask,
`bh-m8dki`, rather than merely noted. The isolated `bd init` cost re-measured at 5.08–5.13 s
over 3 runs, same band as §10's 4.7–4.9 s.

---

## §11 — Two pipeline shapes, and the first cross-hive bulk read (bh-1qxjn, bh-0gvs3, bh-7fen2)

**Measured 2026-08-19 on beadhive-factory**, 20 registered hives / 15 local `.beads/` stores,
`bh doctor -v` from source in a batch worktree, load average 2.5–3.4. Three warm runs each side.

### The shapes

Before this work, every cross-hive read invented its own concurrency: five hand-rolled
`ThreadPoolExecutor` blocks with three different worker bounds. `beadhive/fleet.py` now holds
**two** shapes and the rule for choosing between them, so a sixth dataset has something to be
added to instead of inventing a seventh:

- **Shape A — bulk cross-hive read** (`fleet.sql` / `fleet.sql_rows`). ONE `bd sql` against the
  shared Dolt server, reading every hive's database by qualified name. Cost does NOT scale with
  fleet size. Sound only for a stored column or a server-side view.
- **Shape B — bounded per-hive fan-out** (`fleet.fanout`). N calls under a worker cap. Cost
  still scales with fleet size, divided by the cap. Required whenever A is unsound, and for
  anything that is not a bead-store read at all.

### The classification rule, and why it is the load-bearing half

bd is not a thin wrapper over its tables. `bd ready` is the clear case: readiness is computed
from status plus the dependency graph plus defer dates by bd's own resolver, not read from a
column. Hand-writing that as SQL yields a number that looks right and then DRIFTS every time bd
changes upstream, with nothing to catch it. So every dataset is classified BEFORE any SQL is
written — stored column / server-side view / derived-in-Go-do-not-reimplement — and the third
class stays on shape B.

### What was classified, and where each landed

| Dataset | Reads | Class | Shape |
|---|---|---|---|
| `warnings` schema version | `MAX(version) FROM schema_migrations` | stored table; the per-hive path ALREADY used `bd sql`, so there is no bd-side derivation to reimplement | **A** |
| `warnings` dolt mode | `bd dolt status` | bd's own report on a store, not a row the server serves | B |
| `molecules` epic status | `bd show <epic>` | full bead record through bd | B |
| `prefix_mismatches` | `bd config get issue_prefix` | **reclassified `bh-a8sox`**: `SELECT value FROM config WHERE key = 'issue_prefix'`, a stored row with no bd-side layering for THIS key — see §15 | **A** |
| `seats` | hitch preflights | not a bead-store read at all | B |
| federation status / remote assess | per-hive engine + network | not a bead-store read at all | B |

### Numbers

**Shape A, isolated (`dolt_health.bulk_schema_versions`, live against the real fleet):**
**14 server-mode hives answered in ONE query, 0.267 s.** The per-hive path it replaces is one
`bd sql` spawn each at ~280 ms — 3.9 s sequential, and roughly 0.6–1.0 s even pooled at 16
workers. Embedded-mode hives are absent from the map by construction and fall back to shape B
per hive; a query failure returns `{}` so the whole pass falls back.

**Section-level effect of shape A: inside the noise, and that is the honest result.** `warnings`
measures ~1.8–2.0 s before and after. bh-ti7ws had already pooled these probes, so what shape A
removes was no longer the section's binding cost — the remainder is `_bd_dolt_mode` (0.21 s
pooled), `local_bd_schema_version` (0.11 s), and the section's other unrelated checks. The value
delivered here is **structural, not a wall-time win at today's fleet size**: N spawns became 1,
so this dataset's cost stops growing as hives are added. Claiming a section win the measurement
does not show would be the wrong record to leave.

**`molecules` (shape B, bh-7fen2), same-host A/B:**

| | main | batch branch |
|---|---|---|
| `molecules` | 2.66 / 2.68 / 2.70 s | 1.01 / 1.03 / 1.07 s |
| `bh doctor` total | 11.07 / 11.08 / 11.78 s | 9.86 / 9.81 / 10.17 s |

The section's cost was 11 `bd show` calls (2.16 s) behind 20 `git for-each-ref` (0.12 s);
both stages now fan out.

**A drift question closed:** `molecules` was recorded at 2.61 s early in the session and 3.01 s
later, and it was unclear whether that was a regression. Unmodified `main` re-measures at
**2.66–2.70 s across three consecutive runs** — the 3.01 s reading was host load. Noise, not a
regression.

### Behavioural verification

The `warnings` section renders **byte-for-byte identically** to `main` on the real fleet, which
is what a read-path change is allowed to move: how the reading is obtained, never what is
reported.

### What this does NOT do

`fleet.fanout` does not bound the child processes its workers spawn. Stopping the WAIT on a
future does not reap the process behind it — it would report a timeout while the real child ran
on. Bounding stays at `run.bounded`; PDEATHSIG in a threaded pool remains **bh-0tjqd's** call,
since `preexec_fn` is unsafe in a multi-threaded process and a pool is exactly what makes bh
multi-threaded. `fleet.py`'s docstring names that seam rather than leaving it implicit.

### §11.1 — The baseline these documents are measured against (bh-k5ogw)

`bh-5sizy` (read-path cache pipeline) and `bh-b5v4y` (the read-path FLOOR) both state their
acceptance against a **45.8 s** warm `bh doctor`. That host no longer exists. The current
baseline, on beadhive-factory, is the table in §11: **9.8–10.2 s warm**, load average 2.5–3.4.

**`metadata_rollup` is BIMODAL, and no per-section table taken this session shows it.**
`metadata.read_fleet` runs under a TTL: **193 ms** when its cache is fresh, **2.5–4.3 s** when
the TTL has lapsed. When it fires it is the largest single section — larger than `seats`. Every
timing table collected during this work happened to catch it warm, which is exactly how a
section this size stays invisible. Anything reasoning about "the top section" must account for
which mode it was in.

The re-read `bh-k5ogw` asks for is not arithmetic. `bh-5sizy` premises that CACHING is the
lever; `bh-b5v4y` already established that the floor is inside `bd` and `hitch` startup, which a
cache defers to the next cold read rather than removes. At ~10 s warm the binding constraint is
`bd`'s ~278 ms fixed startup, not any one section — and §11 adds a second answer the epic was
not designed around: a cross-hive dataset that qualifies for shape A stops scaling with fleet
size without any cache at all. Whoever kicks off `bh-5sizy` answers the premise question first.

---

## §12 — The git inventory: 44% of the spawns are bd's, not bh's (bh-z31lc)

Measured 2026-08-19 on beadhive-factory at `main` `bef62ce`, 20 registered hives / 15 local
stores. Method: a shim on `$PATH` logging every git invocation's argv, cwd and PARENT PROCESS,
then exec'ing the real git.

### The premise this bead opened with was wrong in both directions

|  | filed (from bh-b5v4y) | measured now |
|---|---:|---:|
| spawns per `bh doctor` | 186 | **278** |
| per spawn | 6.2 ms | 5.6 ms (50 clean samples, no shim) |
| total git | ~1.15 s | ~1.56 s |
| share of a warm run | 1.15 / 45.8 = 2.5% | **1.56 / 9.5 ≈ 16%** |

The denominator shrank 4x while the count grew 1.5x. git went from a rounding error to about a
sixth of a warm run without anyone adding git calls on purpose. `bh hive ready` contributes 3
spawns; this is entirely `bh doctor`.

### 44% of them are spawned by bd, inside its own startup

| parent process | spawns |
|---|---:|
| `bh` | 156 (56%) |
| `.bd-wrapped` | **122 (44%)** |

What bd asks git before doing any work, with no `-C` (orienting itself in cwd): 41x
`rev-parse --git-dir --git-common-dir --show-toplevel`, 26x `config user.name`, plus per-hive
`-C <hive> rev-parse --show-toplevel` / `--git-dir --git-common-dir`.

**Every bd invocation forks git at least twice before answering anything.** bh cannot remove
these by restructuring bh. They are the first CAUSE anyone has attached to bh-b5v4y's
"why does bd cost 278 ms to start" — recorded on that bead, which is where they belong.

### bh's own 156: mostly distinct work, 28% waste

156 spawns / 113 distinct (question, directory) pairs / **43 redundant**. Most of bh's git
calls are one-per-hive asking a DIFFERENT question — this hive's lease ref, that hive's
`beads.role`, the other's release tags. That is real work, not duplication.

### What was fixed, and what was measured and deliberately not fixed

**FIXED — 18 spawns, `bh` side 156 → 138 (278 → 260 total):**

- `identity.workspace_identity` (29 → 15, one per directory): `lru_cache`. It forks
  `rev-parse --show-toplevel` for a fact that cannot change while a verb runs — bh never
  `os.chdir`s, every verb threads `cwd=` instead.
- `gitauth._get_regexp` (8 → 2, one per pattern): `lru_cache`. Reads the user's GLOBAL git
  config, a process-wide read-only fact with no in-process writer. Returns a tuple, because an
  `lru_cache` hands every caller the same object.

That is ~0.10 s — **below `bh doctor`'s run-to-run noise, and it is not claimed as a wall-time
win.** The value is that the count stops growing: both were called once per hive by
construction, so both scaled with the fleet.

A hazard this introduced and closed in the same change: a process-lifetime `lru_cache` leaks
across tests, where a process runs hundreds of verbs with different fake gits. `conftest.py`'s
autouse `_clear_git_fact_caches` resets both per test. This is not hypothetical — two
`test_gitauth` tests passed alone and failed in file order before the fixture existed.

**MEASURED AND NOT FIXED — the duplicate lease probe, 14 spawns / ~0.08 s.**
`refs/bh/lease/<prefix>` is read twice per prefix because two independent doctor sections each
need it (traced: `dispatch_status.compute_status_all` and `doctor._data_warnings`, both through
`guard.primary_state` → `host_lease.read_cached`). Deduplicating means caching a ref that has an
in-process WRITER (lease adoption), so it needs an invalidation contract across that write path.
That is real machinery for 78 ms, and a stale lease cache would make a fresh primary look like a
follower to its own guard. Left alone deliberately; the number is here so nobody has to
re-measure to make the same call.

### Options rejected, with reasons, so they stay closed

- **`git-workspace run` / `gita`** — both broadcast ONE command across every repo. bh's
  per-hive calls ask DIFFERENT questions per hive, so a broadcast answers none of them, and
  neither touches the 44% bd spawns. git-workspace is already a required dependency, so there
  is no adoption cost being avoided either — it simply does not fit the shape.
- **A different git implementation or build** — at 5.6 ms/spawn there is nothing to reclaim,
  and it trades a universally-present system binary for a fleet-wide dependency.
- **Reading refs without git** — REAL, and measured rather than assumed: a plain stdlib read of
  `.git/refs` + `packed-refs` returns the IDENTICAL answer in **0.21 ms vs git's 6.23 ms (30x)**,
  with no `pygit2` and no `dulwich`. Across bh's ref and config questions that is ~0.64 s. Not
  recommended today: a hand-rolled ref reader must handle packed-refs, `.git` as a FILE for
  worktrees (this repo has 60+), per-worktree refs, symbolic refs, and concurrent updates.
  Getting any of those wrong makes doctor report a wrong branch state, which is worse than
  slow. Revisit only if git's share grows again.

---

## §13 — Shape H, and the audit of every process-lifetime memo (bh-w49zv, bh-gy7bc)

§11 named two shapes for a cross-hive dataset and **both were per-hive**. An author holding a
fact that does not vary by hive was told to pick A or B, and both are wrong for such a value.
That omission had a measured consequence: `bd --version` — which takes no hive, resolves no cwd,
and cannot differ between iterations — was forked **15 times per `bh doctor` run**.

### The third shape

- **Shape H — a host-global fact.** Resolved ONCE per process. The cheapest correct code for one
  has no cache in it at all: hoist the value out of the loop and pass it in
  (`hive_schema.refresh_with_detail(probed=…)` is the worked example). `fleet.once` is the
  fallback for when the value is consumed several call levels below the pool boundary and
  threading it would change signatures that exist for other reasons.

The choosing rule now asks **"does this fact vary by hive at all?" FIRST**, before the A-vs-B
classification, because that is the question whose omission caused the defect.

### Why `functools.cache` could not be the answer

`functools.cache` / `lru_cache` have no stampede protection. Measured:

```text
@cache under a 15-way pool: underlying function ran 15 times (want 1)
@cache called sequentially : underlying function ran  1 times
```

This is exactly how the memo was defeated. `bh-i6e5g` added `@cache` to
`_local_bd_version_string` against a **sequential** loop and measured 12 spawns → 1. `bh-ti7ws`
then made that loop concurrent, all 15 workers missed together, and the docstring went on
claiming "the second answer is the first one" for as long as it was false. **Making fan-out the
house pattern is what armed the trap**, so the guard lives next to the shape: `fleet.once`, a
double-checked lock that does not cache a raised exception (a cached failure would turn one
transient probe failure into a permanently wrong answer for the process's life).

A test pins the reason `fleet.once` exists by asserting that the same pool test **fails** against
`functools.cache` — if the stdlib ever grows stampede protection, that test tells us to delete
`once`.

### A second bug in the same function, found while fixing the first

The old `@cache` was keyed on a `timeout=` keyword, and the two callers passed **different**
bounds (15 s from `hive_schema`, 30 s from `local_bd_schema_version`). One host-global question
therefore had two cache entries, so `bd --version` ran at least twice **even without a pool**.
`fleet.once` is zero-argument on purpose: if a value varies by anything, it is not shape H.

### Result

| | before | after |
|---|---:|---:|
| `bd --version` per `bh doctor` | 15 | **2** |
| total `bd` invocations (warm) | 57 | **47** |

The residual 2 is not a leak: one is the schema-version read through `fleet.once` (verified as
exactly **1** by an in-process trace), and one is `deps.probe_one`'s dependency detection, a
separate call site with a different purpose that `run.py`'s own routing inventory deliberately
keeps unrouted.

**No wall-clock claim.** The 15 forks overlapped inside the pool, so removing 13 of them returns
process count and host load, not seconds.

### The audit: every process-lifetime memo under `src/`

Three exist. Each is classified for pool reachability, because "believed safe" is what this bead
exists to stop trusting:

| memo | shape | pool-reachable? | verdict |
|---|---|---|---|
| `dolt_health._local_bd_version_string` | H | **YES** — `fleet.fanout` → `_probe` → `hive_schema.refresh_with_detail` | **was defeated**; now `fleet.once` |
| `identity.workspace_identity` | H per directory | No — only `bd.create` / `bd.import_`, both write paths | `@cache` retained; re-audit if a read path calls it |
| `gitauth._get_regexp` | H | No — `doctor`'s `group_auth` section, serial | `@cache` retained; re-audit if it moves into a pool |

The two retained ones were fixed by `bh-z31lc` **per-site rather than from a rule**, which is
precisely the pattern this bead exists to replace. They are correct today and their safety is
now written down rather than assumed.

### Dataset classification: A / B / H

| dataset | shape | why |
|---|---|---|
| `bd --version` | **H** | host-global; the defect above |
| `git config --global --get-regexp` ×8 | **H** | host-global; already memoized (`bh-z31lc`) |
| local bd schema version | **H** | host-global; file-cached, correct |
| `bd config get issue_prefix` ×14 | **A** | reclassified — a stored `config` row, not layered; see §15 (`bh-a8sox`) |
| `bd dolt status` ×15 | B | genuinely per-hive |
| `bd show <epic>` ×10 | **A** | retargeted — `issues.status` is a stored column (`bh-xi0m1`, §14) |
| `channels.scan` release tags ×15 | B | genuinely per-hive — **each repo's OWN tags. CHECKED and ruled out; do not re-derive.** |
| `systemctl is-enabled/is-active` ×28 | B | genuinely per-hive UNIT NAMES — see below |

### The systemctl question, answered

`systemctl` **does** accept multiple units per invocation — its own man page documents
`is-enabled UNIT...` and `is-active PATTERN...`, printing one state line per unit. So bh's 28
single-unit calls could be 2.

**Not done here, and the reason is a finding of its own.** On this host those 28 calls measure at
~9.6 ms each *because they fail immediately*: `systemctl --user` cannot reach a session bus
(`DBUS_SESSION_BUS_ADDRESS` and `XDG_RUNTIME_DIR` unset in this environment), so every one
returns without doing work. Batching a call that is currently a no-op would optimise a
measurement artefact. The real question — what these cost on a host where the user bus works,
and whether bh should be probing units it cannot reach at all — needs measuring somewhere the
call actually functions. Recorded rather than guessed at.

---

## §14 — `molecules` stage 2 retargeted to shape A (bh-xi0m1)

§11 left `doctor._orphan_container_branches`' second stage (`bd show <epic>` ×10, one per
container branch) on shape B, noting it fanned out but not retargeted. It uses exactly ONE
field of the full bead record it fetches: `bead.get("status") == "closed"`.

### Classification, done rather than assumed

`bh bd sql -q "DESCRIBE issues" --json` against a real hive on this host:

```json
{"Field": "status", "Type": "varchar(32)", "Default": "'open'", "Extra": "", "Key": "MUL"}
```

`Extra: ""` — no generated/virtual expression — so `status` is a plain, indexed stored column
bd's own `close` mutates directly, not a value bd's Go resolver derives at read time (contrast
`bd ready`, §11's example of a derivation that must stay shape B). `fleet.py`'s own module
docstring already names `issues.status` as the canonical shape-A stored column. Shape A applies
with no reimplementation risk.

### Shape

`doctor._bulk_epic_closed`: one cross-database `SELECT '<db>' AS db, id, status FROM
<db>.issues WHERE id IN (...)` per server-mode hive's database, `UNION ALL`'d into a single `bd
sql` call — same transport as `dolt_health.bulk_schema_versions`, keyed by `(hive_dir, branch)`
so `_orphan_container_branches`'s per-item worker can thread the bulk answer straight in
(`bulk.get(...)`), falling back to `bd show` only for whatever the bulk pass left unanswered —
the same `probed=` shape `hive_schema.refresh_with_detail` uses.

**Partial by construction, same contract as `bulk_schema_versions`:** embedded-mode hives have
no database on the shared server to qualify (`bd sql` refuses them outright) and are excluded
before the query is built; so is any hive whose `recorded_server_database` is absent or fails
the `sanitize_database_name` round-trip. Both fall back to the per-hive `bd show` path,
exercised by `test_orphan_embedded_mode_hive_falls_back_to_per_hive_bd_show` (asserts the bulk
`fleet.sql` seam is never called for an embedded-mode hive) rather than assumed.

**Stage 1 stays shape B, unchanged:** the container-branch list comes from `git for-each-ref`
per hive — local git, not a bead-store read at all, so no bulk query covers it (fleet.py's rule:
shape A is for reads the shared Dolt server can answer).

### Numbers — same host/fleet as §11 (20 registered hives, 15 `.beads/` stores)

In-process `doctor._data_molecules(cfg)`, 3 warm runs each, from the registered clone
(`/home/bees/workspace/github/beadhive/beadhive`), `main` (before) vs this branch's `src/`
loaded via `PYTHONPATH` against the same cwd/fleet (after) — same method §11 used for the A/B
comparison:

| | main (10 `bd show`) | after (1 `bd sql`) |
|---|---:|---:|
| `molecules` (`_data_molecules`) | 1.03 / 1.13 / 1.14 s | 0.37 / 0.39 / 0.40 s |

`fleet.sql` called exactly **1** time (traced), replacing what was up to 10 `bd show` spawns.
Output is identical both sides: 2 orphaned branches, same `(prefix, branch)` pairs.

**State the two claims separately, per this bead's own instruction:**

- **Wall-clock:** real, on this fleet — ~1.0 s → ~0.4 s for this section. Not the headline
  number `bh doctor` users notice (§11.1: the ~10 s warm floor is `bd`/`hitch` startup), but a
  measured, not-inside-the-noise win, unlike §11's `schema_migrations` case which WAS noise at
  its section's scale.
- **Fleet-size scaling (the separate, structural argument):** the real reason to do this — 10
  invocations become 1 and stop scaling with the number of open molecule branches across the
  fleet, exactly as §11's shape-A conversions do. This holds regardless of what the wall-clock
  number happens to be on any one host.

### Behavioural verification

`test_orphan_uses_bulk_path_for_a_server_mode_hive_no_bd_show_needed` seeds NO `fakebd` bead
record and asserts the branch still reports orphaned — if the code silently fell back to
`bd show` for a bulk-eligible hive, `bd` would report the bead unknown and the test would fail.
`test_bulk_epic_closed_reads_every_hive_in_one_call` asserts exactly one `fleet.sql` call.
Existing `test_orphan_lists_closed_epic_branch_not_open` / `test_orphan_empty_when_no_mol_branches`
pass unchanged (their fixture hives have no recorded server database, so they exercise the
shape-B fallback exactly as before this bead).

---

## §15 — `bd config get issue_prefix` retested: it IS shape A (bh-a8sox)

§11 and §14 both classified config reads as shape B on the reasoning "bd config resolution is
layered (global/project/local), not a plain row" (bh-0gvs3), applied to config reads AS A
CLASS. That reasoning was never tested against `issue_prefix` specifically — and by the time of
this bead's own read-path measurement, `bd config get issue_prefix` had become the single most
expensive bd verb on the read path: **14 invocations at ~565 ms each, 7.91 s of summed process
time**, warm and cold alike.

### What the value MEANS — read from bd's own source before any SQL was written

Traced `bd config get issue_prefix` through `cmd/bd/config.go`'s `configGetCmd`:

1. `issue_prefix` is **not** a YAML-only key — `internal/config.IsYamlOnlyKey("issue_prefix")`
   is asserted `false` by bd's own test table (`internal/config/yaml_config_test.go`).
2. It is **not** one of the command's two special-cased derived values (`backup.enabled`,
   which reports an auto-detected effective value with a source label, and `beads.role`, read
   from git config) — neither branch matches `issue_prefix`.
3. It falls straight through to `store.GetConfig(ctx, key)`, which for the sql-server-backed
   store resolves: `DoltStore.GetConfig` (`internal/storage/dolt/config.go`) →
   `issueops.GetConfigInTx` (`internal/storage/issueops/config_metadata.go`) →

   ```go
   func GetConfigInTx(ctx context.Context, tx DBTX, key string) (string, error) {
       var value string
       err := tx.QueryRowContext(ctx, "SELECT value FROM config WHERE `key` = ?", key).Scan(&value)
       ...
   }
   ```

   That is the WHOLE implementation. No merge of a project/global/local tier, no derivation
   layered on top of the stored value — it is `SELECT value FROM config WHERE key = ?` inside a
   read transaction. The only transformation anywhere near this key is on the WRITE side
   (`SetConfigInTx` / `configSQLRepositoryImpl.SetConfig` both strip a trailing hyphen before the
   `REPLACE INTO config` that stores it), so what `GetConfigInTx` returns is exactly what a
   direct `SELECT` against the `config` table would return — nothing to reimplement, nothing to
   drift out from under a hand-rolled equivalent.

### Classification

**Stored row in the per-database `config` table → shape A applies.** The "config resolution is
layered" premise does hold for *some* config-adjacent reads bd exposes — `backup.enabled`'s
effective value, `GetInfraTypes`'s YAML-then-default fallback, `GetCustomTypes`'s table-then-
config-then-YAML union — but none of that is `issue_prefix`. Applying a class-wide rule to every
member of the class without checking each one is exactly the gap this bead exists to close.

### The `hub_bulk.DENY_TABLES` objection, engaged directly

`hub_bulk.py` lists `config` in `DENY_TABLES` with the docstring: "**NEVER COPY** —
identity/bookkeeping. Each database's OWN identity; copying one INTO the aggregate corrupts
it." That is a real constraint, and it is not the same claim as "never read this table":

- `DENY_TABLES` governs `hub_bulk`'s `INSERT ... SELECT` copy path — moving ROWS from one
  hive's database into a DIFFERENT (aggregate) database's table. Copying hive A's
  `issue_prefix` row into hive B's `config` table would silently overwrite B's own identity —
  that is the corruption the deny-list prevents, and it is real.
- `dolt_health.bulk_issue_prefixes` (this bead) does the opposite of a copy: it runs a
  read-only `SELECT ... FROM <db>.config WHERE key = 'issue_prefix'` against EACH database by
  its own qualified name, in a `UNION ALL`, and returns each row labelled by the database it
  came from. Nothing is written, and nothing crosses a database boundary except the read-only
  connection itself — the same shape `bulk_schema_versions` already uses for
  `schema_migrations`, another table not in `DENY_TABLES`'s copy sense but also never copied by
  this module.

So `DENY_TABLES` and this change are answering different questions and do not conflict:
"never COPY `config` between databases" (write-path identity rule) is orthogonal to
"a cross-database read of `config` is sound" (read-path classification, this bead's question).

### What changed

`dolt_health.bulk_issue_prefixes` (new, mirrors `bulk_schema_versions`): one `bd sql` running a
`SELECT '<db>' AS db, value FROM <db>.config WHERE key = 'issue_prefix'` per database, joined
with `UNION ALL`, against every server-mode hive with a `store_locator.recorded_server_database`
— never `server_database`, which falls back to a guess and would let one un-migrated hive's
guessed name fail the whole union (same reasoning `warnings`' schema-version caller already
documents). `doctor._data_prefix_mismatches` now tries the bulk read first and falls back to the
existing per-hive `bd config get issue_prefix` (`fleet.fanout`) for whatever the bulk read
didn't cover — embedded-mode hives (no database on the shared server to qualify) and any
server-mode hive the bulk query didn't answer for. Input order is still preserved end to end.
Tests: `tests/test_dolt_health.py` (`bulk_issue_prefixes`'s own unit coverage, same shape as
`bulk_schema_versions`'s) and `tests/test_doctor.py` (the bulk-hit path, the bulk-miss fallback,
and the embedded-mode-never-offered-to-bulk path, each asserted directly).

### Numbers

Measured 2026-08-19 on beadhive-factory, 14 registered hives / 14 local `.beads` stores (13
server-mode with a recorded database, 1 embedded), `bh doctor -v`, three warm runs each side.

| | before (shape B, 14 spawns) | after (shape A + 1 fallback spawn) |
|---|---:|---:|
| `prefix_mismatches` section | 1.47 / 2.56 / 2.27 s | 0.73 / 0.70 / 0.70 s |
| `bh doctor` total | 12.7 / 16.1 / 17.8 s | 11.2 / 10.8 / 11.3 s |

**This IS a measured wall-clock win at today's fleet size** — unlike §11's `schema_migrations`
result, where `bh-ti7ws` had already pooled the per-hive fallback so shape A's removal fell
inside the noise. Here the per-hive path (`fleet.fanout` over 14 sequential-cost `bd config
get` spawns, ~565 ms each) was still the section's binding cost, so replacing 13 of those 14
spawns with one query removes real wall time: roughly 2.1 s → 0.7 s, a ~3x section-level
speedup. The single embedded-mode hive still costs one `bd config get` spawn under the
fallback.

**The fleet-size-scaling argument is separate from that wall-clock number, and is stated
separately on purpose (the sizing note this bead's brief asked for):** the wall-clock win
above is specific to today's 14-hive fleet and this host's load; what does NOT depend on
either is that the read count for server-mode hives dropped from N to 1 — this dataset's cost
stops growing as hives are added, independent of whatever the per-spawn cost happens to be on
a given day. The `~565 ms`/spawn figure this bead opened with is itself evidence that per-spawn
cost is not stable across measurements (§2/§8/§11 all show the same command's per-spawn cost
moving with host load) — the scaling argument is the part of this result that is not
sensitive to that noise.

### Verification

`_data_prefix_mismatches`'s existing suite (order-preservation under concurrency, skip-on-
unreadable, skip-on-unparseable, end-to-end section rendering) passes unmodified against the
new code path — on this host every registered hive has no `dolt_server_database` recorded in
its fixture-constructed metadata (the `prefix_hive` fixture creates a bare `.beads/` dir with
no metadata.json), which `store_locator.recorded_server_database` correctly reports as `""`,
so the bulk map comes back empty and every existing test exercises the SAME fallback path it
always did — behavior is unchanged for a hive with no recorded server database, exactly as
`bulk_schema_versions`' own caller depends on. Three new tests exercise the bulk-hit, bulk-miss,
and embedded-mode paths explicitly (`test_data_prefix_mismatches_uses_bulk_read_for_
server_mode_hives`, `..._falls_back_per_hive_when_bulk_omits_a_hive`,
`..._embedded_hive_skips_bulk_and_uses_fallback`).

---

## §16 — `metadata.read_fleet`'s miss path retargeted to shape B (bh-f6w4d)

`fleet.py`'s own docstring calls out `metadata.read_fleet` by name as "the only large per-repo
loop on the read path that never went through these shapes." True on inspection: `refresh()`
(the function every miss — `read_fleet`'s `on_miss="compute"` path, plus the background
reloader `_spawn_reload` — eventually calls) was a plain `for key in target: repos[key] =
measure(...)`. This bead's primary deliverable was attribution of the 10.66 s cold cost
(`docs/BH_DATA_PIPELINE.md`'s new "`metadata_rollup`'s 10.66 s MISS attributed" section carries
the per-bucket numbers); this entry records the one shape change that followed from it.

### Classification, not assumed

Per §11's own choosing rule: does the fact vary by repo? Yes (every field of `RepoMetadata` is
per-repo) — not shape H. Is it a bead-store read with a server-side equivalent? No — `measure()`
is `os.walk` + `git` subprocess calls against the filesystem; `bd sql` has no view over
arbitrary on-disk repos it doesn't manage. Shape A is unsound by construction. That leaves shape
B, `fleet.fanout`, the same bounded per-repo fan-out `molecules` and `seats` already use.

### The measurement this bead's brief asked for: is parallelizing worth it, given it might be I/O-bound

Not assumed — measured, `metadata.refresh()` over this host's real 21-repo fleet, 3 interleaved
trials per configuration:

| | serial (before) | `fleet.fanout`, workers=8 | `fleet.fanout`, workers=16 (after) |
|---|---:|---:|---:|
| `metadata.refresh()`, real fleet | 9.28 s median | 8.27 s median | 8.28 s median |

**Answer: yes, but modestly, and it saturates well before the pool's default cap.** Workers=16
(what `fleet.fanout`'s `MAX_WORKERS` default gives every caller that doesn't override it) buys
nothing over workers=8 on this fleet — the work is bound by something that stops scaling past
~8 concurrent repos on this host (disk contention and/or `git` process-spawn rate, not
identified further; not this bead's scope to chase). The ~11% wall-clock gain is smaller than
§14's `molecules` (~3x) or §15's `prefix_mismatches` (~3x) shape conversions, because those
replaced N spawns with ONE `bd sql` call — an algorithmic reduction — while this conversion only
overlaps N calls that still all have to happen; §11's own framing (bulk-vs-fanout) predicts
exactly this difference in kind between "replace N with 1" and "run N concurrently instead of
serially."

**Stated separately, per house standard:** the wall-clock number above is this host's 21-repo
fleet today. The structural argument is independent of it — the miss path's cost was `O(n)`
serial and is now `O(n / min(8, n))`, so a larger fleet (`docs/METADATA-CACHE.md` §1's 90-repo
profile, or any fleet with more registered hives than this factory host) gains proportionally
more in absolute seconds from the same relative speedup, and a fleet smaller than 8 repos gains
nothing measurable at all (the pool cap never binds).

### What this did NOT touch

`measure()`'s own internals — the `os.walk`, the ~7 `safety.scan` git spawns, `fingerprint`,
`_maturity_commit_count`, the two commit-date git-log calls — are unchanged; parallelizing the
per-repo loop overlaps those calls across repos, it doesn't make any one of them cheaper. The
freshness contract (`is_stale`'s `(git_head, git_mtime)` fingerprint, `store()`'s atomic write,
event/coarse invalidation) is untouched — `fleet.fanout` only changes execution order and
concurrency of the calls into `measure()`, not what any of them compute or persist.
`docs/BH_DATA_PIPELINE.md`'s companion section carries the four-angle verdict (walk-question,
shape, cold-is-not-rare, `on_miss='stale'`) and the `bh-5sizy`/`bh-b5v4y` relationship.
