# Design — Workspace-metadata cache

Status: **design / spike**. This document is the contract that the
downstream beads implement directly:

- **.2** — build the cache component (`beadhive.metadata`: data model + storage + read/refresh).
- **.3** — migrate `doctor` Fleet Health / Disk Usage + `hive survey` to read from the cache.
- **.4** — event invalidation + background reload on mutating ops.

It is grounded in a real profile of `_section_fleet_health` (`doctor.py:259-310`) at 90 repos
and in the shape of the two consumers today: `doctor.py` and `survey.py`.

---

## 1. Why — the problem in one profile

`bh doctor` (and `bh hive survey`) recompute the whole fleet's repo state from scratch on every
invocation. The dominant consumer is Fleet Health (`doctor._section_fleet_health`,
`doctor.py:280-301`), which for **every** git repo under `<provider>/<org>/<repo>` calls:

- `safety.scan(path)` (`safety.py:299`) — ~8 `git` subprocess spawns + one full disk walk, and
- `safety.last_commit_age_days(path)` (`safety.py:435`) — one more `git log -1`.

### Profiling breakdown (real code path, 90-repo fleet)

Measured with `scripts/profile_fleet_health.py` (throwaway harness, this bead) driving the real
`beadhive.safety` functions over all **89** on-disk `github/<org>/<repo>` git repos under
`$GIT_WORKSPACE` (= `/Users/brian/workspace`) — the exact universe `doctor._scan`
(`doctor.py:36-56`) feeds into Fleet Health. Total disk measured: **40.1 GB**. The disk walk's
internal `_measure_disk_usage` was neutralized during the scan-timing pass so the git-call
bucket is isolated cleanly (subtracting a separately-timed walk is confounded by OS page-cache
warming — the first naive attempt mis-attributed the git calls to ~0 ms).

| bucket | per-repo | fleet (×90) | share of work |
|---|---:|---:|---:|
| `_measure_disk_usage` os.walk (working tree + `.git`) | **183 ms** (cold ~206 ms) | ~16.5 s | **~62%** |
| `safety.scan` git subprocess calls (~8 spawns + N branch diffs) | **106 ms** | ~9.5 s | **~36%** |
| `last_commit_age_days` (`git log -1 --format=%ct`) | **8 ms** | ~0.7 s | **~3%** |
| **effective per-repo (scan + one walk + age)** | **~296 ms** | **~26.6 s** | 100% |

Headline: **one `bh doctor` spends ~27 s (warm) / ~30 s (cold) just in Fleet Health**, and
**~62% of that is the `os.walk` disk-sizing**, ~36% is `git` process-spawn overhead (the git
calls are individually trivial — the cost is ~13 ms/spawn × ~8 spawns/repo), and only ~3% is
commit-age.

Distribution is heavily skewed by a few large working trees (build artifacts / `node_modules`):
`untui` alone walks in **5.5 s**; the top 8 repos account for a large fraction of the disk time.
This matters for invalidation design (below): the repos you actively work in are often the fat
ones.

### The hive double-scan (acceptance point 5)

`doctor()` walks the same expensive `safety.scan` **twice** for every registered hive:

- **Disk Usage (by hive)** — `doctor.py:360-368` calls `safety.scan(path)` for each
  `managed_repos` entry (for `disk_bytes`).
- **Fleet Health** — `doctor.py:280-301` calls `safety.scan(path)` again for **every** git repo
  on disk, which is a superset of the registered hives.

So each registered hive pays the full walk **twice per `bh doctor`** (with `R` hives registered
that is `R + 90` scans where only `90` are distinct). A cache keyed by the `provider/org/repo`
triplet **inherently removes this double-scan**: both sections read the same cached entry, so
each repo is measured at most once per refresh — a structural win that holds even with a TTL of
zero.

---

## 2. Cache API surface (bead .2)

New module **`beadhive.metadata`** (single small file, mirrors the `safety` / `survey` style). Pure
read/compute/store — no rendering, no `typer`.

### Storage

- Path: **`$WS_CACHE/metadata.json`** — i.e. `config.cache_dir() / "metadata.json"`
  (`config.py:42-44`; `$WS_CACHE` → default `~/.ws/cache`). Reuses the existing cache dir that
  already holds minimal-clone bead caches; no new env var.
- Format: a single JSON object, atomically written (write temp + `os.replace`) so a concurrent
  reader never sees a half-file.
- Absent / unparseable / wrong `version` file ⇒ treated as an **empty** cache (cold start), never
  an error.

### Data model

```jsonc
{
  "version": 1,
  "last_updated": "2026-06-30T18:22:04Z",   // coarse stamp; whole-file freshness backstop
  "workspace_root": "/Users/brian/workspace", // guard: root moved ⇒ whole cache is stale
  "repos": {
    "github/briancripe/untui": {
      // --- freshness fingerprint (cheap to recompute; see §3) ---
      "git_head": "9f3ac21…",        // `git rev-parse HEAD` (empty for no-commit repos)
      "git_mtime": 1719772800.0,     // mtime of <repo>/.git (refs/index churn signal)
      "measured_at": "2026-06-30T18:22:03Z",

      // --- payload consumed by doctor + survey (mirrors safety.ScanResult + age/maturity) ---
      "category": "READY",           // safety.Category
      "has_origin": true,
      "stash_count": 0,
      "disk_bytes": 41235120,        // the expensive number — the whole reason for the cache
      "commit_count": 214,           // safety._maturity_commit_count
      "age_days": 3.2,               // safety.last_commit_age_days (null ⇒ inf/no commits)
      "last_commit": "2026-06-27",   // survey._last_commit_date (YYYY-MM-DD, null ⇒ none)
      "branches": [
        {"name": "main", "ahead": 0, "behind": 0, "has_upstream": true, "dirty": false}
      ],
      "worktrees": ["…"]
    }
    // … one entry per provider/org/repo triplet …
  }
}
```

The `repos.<key>` payload is exactly the union of what the two consumers pull today, so `.3` can
delete their inline `safety.scan` / `_last_commit_date` calls and read the record. `difficulty`
is **not** stored — it is a pure, cheap derivation over these primitives
(`safety.difficulty`, `safety.py:1106`), so consumers recompute it on read (keeps the model
small and avoids stale-verdict drift when thresholds change).

### Functions

```python
# ---- types ----
@dataclass RepoMetadata:      # one repos.<key> record (fields above)
@dataclass MetadataCache:     # {version, last_updated, workspace_root, repos: dict[str, RepoMetadata]}

# ---- read ----
def load(cfg=None) -> MetadataCache
    """Parse $WS_CACHE/metadata.json. Missing/invalid ⇒ empty cache. Never raises."""

def get(cfg, key: str) -> RepoMetadata | None
    """One repo's record, or None if absent. Freshness is the caller's call (see is_stale)."""

# ---- compute ----
def measure(path: str | Path) -> RepoMetadata
    """Compute ONE record from scratch (the expensive path: safety.scan + age + maturity).
    This is the single choke point that owns the walk + git calls."""

def fingerprint(path: str | Path) -> tuple[str, float]
    """Cheap (git_head, git_mtime) for a repo WITHOUT walking the tree — the staleness probe."""

def is_stale(entry: RepoMetadata, path, *, ttl: float | None) -> bool
    """True if the entry's fingerprint no longer matches OR (ttl set and measured_at older)."""

# ---- refresh / write ----
def refresh(cfg, keys: Iterable[str] | None = None, *, root=None) -> MetadataCache
    """Recompute `keys` (or the full on-disk fleet when None), merge into the loaded cache,
    stamp last_updated, atomically store, return the new cache. This is what a cold read and a
    background reload both call."""

def store(cfg, cache: MetadataCache) -> None
    """Atomic write of the whole file."""

# ---- read-through convenience for consumers (.3) ----
def read_fleet(cfg, keys: list[str], *, ttl: float | None, on_miss: str = "compute")
        -> dict[str, RepoMetadata]
    """Return records for `keys`. Fresh cached entries are served as-is; stale/missing entries
    are either computed inline (on_miss='compute', blocking) or returned absent + queued for
    background refresh (on_miss='stale', serve-stale-while-revalidate). This is the seam the
    timeout discussion (§5) turns on."""

# ---- invalidation (bead .4) ----
def invalidate(cfg, key: str) -> None          # per-repo: drop one entry, atomically
def invalidate_all(cfg) -> None                # coarse: clear repos + reset last_updated
```

`measure()` is the **only** place that runs the walk + git plumbing, so every read path and the
background reloader share one implementation and one attribution point.

---

## 3. Invalidation contract (coarse vs per-repo)

**Decision: per-repo entries with a per-repo fingerprint probe, a coarse TTL backstop, and
per-repo event invalidation on mutating ops.** Coarse-only is rejected.

### Why not coarse-only

A single mutating op (`bh work merge`, retire, `backup_unpushed`, a worktree add/remove) touches
**one** repo. A coarse "the whole cache is stale" model would discard 89 still-valid entries and
force a full ~27 s re-walk on the next `bh doctor` after *any* change anywhere. Per-repo keeps 89
entries warm and re-walks only the one that actually changed. Given the cost skew (§1), the repo
you just worked in is frequently the expensive one — but that is exactly one walk, not ninety.

### Three invalidation tiers (in precedence order)

1. **Per-repo event invalidation (authoritative, bead .4).** Mutating ops call
   `metadata.invalidate(cfg, key)` for the affected triplet, which drops that entry and (if
   background reload is on) queues a `refresh(cfg, [key])`. The op→key map `.4` must wire:

   | mutating op | site | invalidates |
   |---|---|---|
   | `bh work merge` / molecule land | `worktree_merge.py` / `work.py` | the hive's key |
   | `backup_unpushed` (push WIP / publish) | `safety.backup_unpushed` (`safety.py:912`) | that repo's key |
   | retire (delete/backup) | `retire.py` | that repo's key |
   | worktree add / remove | `worktree.py` | the owning repo's key (branch/worktree churn) |
   | `bh hive register` / repos-sync | `hive.py` / `registry` | new/removed key (or `invalidate_all` on provider-set change) |

2. **Per-repo fingerprint probe (cheap self-heal).** On read, `is_stale` compares the stored
   `(git_head, git_mtime)` against a fresh `fingerprint()` — no walk. This catches changes made
   **outside** `bh` (a manual `git commit`, `git fetch`, an editor writing files that changed
   `.git/index`). A mismatch ⇒ that one entry is recomputed. **Caveat (see §4):** the fingerprint
   detects *git-state* change, not pure working-tree-size change; a build that adds 2 GB of
   untracked artifacts without touching `.git` will not move `git_mtime`, so `disk_bytes` can
   lag until the TTL backstop or an event fires. This is an accepted trade — walking to detect
   whether you need to walk defeats the cache.

3. **Coarse TTL backstop.** `last_updated` + a TTL (config `metadata.ttl`, default **300 s** for
   interactive freshness; `0` = always-fresh/bypass, negative = never-expire) bounds staleness
   for anything tiers 1–2 miss. `workspace_root` mismatch is a hard coarse-invalidate (root moved
   ⇒ every path is wrong).

### Consistency / concurrency

All writes go through `store()` (temp + `os.replace`, whole-file). Concurrent `bh` processes may
both refresh and last-writer-wins; entries are independent and idempotent (recomputing a repo
yields the same record), so a lost update just costs a redundant walk, never corruption. No lock
file in v1.

---

## 4. Residual cost the cache cannot hide (acceptance point 3)

The cache turns "**always** ~27 s" into "**~27 s once per change, then ~ms**." What it cannot
hide:

- **Cold start.** First-ever run, a fresh clone, CI, or after `invalidate_all` (root move /
  provider-set change) has nothing to serve — the full ~27–30 s fleet walk still happens. The
  cache only accelerates the *second and later* reads.
- **The walk on an invalidated entry is irreducible.** When a repo is event- or fingerprint-
  invalidated, recomputing it re-pays the full `_measure_disk_usage` walk (~180 ms typical, up to
  **5.5 s** for a big tree like `untui`). Event invalidation means the repo you just merged/backed-
  up is precisely the one now cold — so the interactive `bh doctor` right after a merge still eats
  that one repo's walk unless the refresh is backgrounded (bead .4).
- **`os.walk` is O(files), not O(repos).** The cost is dominated by large working trees
  (`node_modules`, build output). No caching strategy shrinks the walk itself; only *not walking*
  (serving a cached number) avoids it. A future optimization orthogonal to this cache would be a
  cheaper sizer (e.g. `du`, or trusting `git count-objects` + skipping gitignored trees), but that
  is a `safety._measure_disk_usage` change, not a cache concern.
- **Background reload cost isn't eliminated, only relocated** off the interactive path — it still
  consumes CPU/IO, just not in front of the operator.

Net: the cache is a **latency** win for the interactive/steady-state path, not a **throughput**
win for the underlying measurement. The residual is "one walk per changed repo, moved off the
critical path."

---

## 5. Parallel-sections / `WS_DOCTOR_TIMEOUT` follow-up (acceptance point 4)

Context: a previously-deferred idea was to run `doctor`'s sections in parallel and render a
bounded per-section "(timeout)" placeholder governed by a new `WS_DOCTOR_TIMEOUT` env var
(default ~5 s), so a slow Fleet Health section can't hang the whole report.

**Verdict: the cache makes the parallel-sections + `WS_DOCTOR_TIMEOUT` mechanism unnecessary —
do NOT build it.** Reasoning:

- Fleet Health is the only section that is slow, and it is slow *only* because of the walk. Once
  it reads from a warm cache it renders in ~ms, so there is nothing left for a per-section timeout
  to protect against in steady state, and parallelizing ~ms sections buys nothing.
- The one case a timeout would help — a **cold/stale** cache on the interactive path — is better
  solved by the mechanism bead **.4** already introduces: **serve-stale-while-revalidate**.
  `read_fleet(..., on_miss="stale")` renders immediately from whatever is cached (or a
  `(measuring…)` placeholder for missing entries) and kicks a background `refresh`. That gives the
  *same* "never hang the report" guarantee as a timeout, but with no arbitrary cutoff, no half-
  measured numbers, and it fills in on the next invocation.
- A hard per-section timeout also has a correctness smell: it would print *partial* fleet totals
  (some repos measured, some timed out) that silently under-count dirty/reclaimable stats —
  worse than an honest "refreshing" marker.

**Recommendation on follow-up beads:**

- **Drop** the parallel-sections work and the `WS_DOCTOR_TIMEOUT` env var. Fold their intent into
  bead **.4**'s background-reload / serve-stale design (already in scope).
- **Optional, small** follow-up (only if operators want a blocking guarantee): a `bh doctor
  --fresh` flag that forces `on_miss="compute"` (blocking full refresh) for scripting/CI, plus a
  one-line `cache: last refreshed <ago>, N entries stale (refreshing)` status line so the staleness
  is visible. This is a ~½-day UX bead, **not** the parallel-sections/timeout machinery, and is
  not required for .2–.4 to land.

---

## Appendix — profiling harness

`scripts/profile_fleet_health.py` (throwaway, committed for reproducibility). Run:

```sh
GIT_WORKSPACE=/path/to/workspace uv run python scripts/profile_fleet_health.py
```

It times the three buckets over every `github/<org>/<repo>` git repo, neutralizing the scan's
internal disk walk during the git-call timing pass to isolate the buckets. Numbers in §1 are from
a warm-cache run at 89 repos / 40.1 GB on macOS; cold disk-walk cost runs ~10–15% higher.
