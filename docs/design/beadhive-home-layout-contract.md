# `~/.beadhive/` layout contract (bh-cmqp.3)

> Status: **shipped.** Classifies every top-level entry `bh` writes under `config.home()`
> (`~/.beadhive/` by default, `$BH_HOME` overridable), resolves the `wt/` vs `worktrees/`
> drift some hosts have accumulated, and gives `bh doctor` a check that keeps this doc honest
> instead of a claim nobody verifies.

## Problem

`~/.beadhive/` has accumulated a flat mix of durable stores, regenerable caches, per-machine
state, and one-off artifacts, with no stated contract about which is which:

```text
hq/                  hq-backups/          hub/                 cache/
wt/                  worktrees/           retros/              hitch/
backups/             config.yaml          config.yaml.bak      host.yaml
labels.md            docker-compose.yml   docker-compose.otel.yml   .env
.env.example         setup-state.json
```

Nothing on disk distinguishes "delete this any time" from "this is the only copy." Two
concrete costs:

- **An operator can't tell what's safe to clean up.** Is `cache/` regenerable? Is
  `worktrees/` still live, or a leftover? Guessing wrong either loses state or leaves cruft
  forever.
- **`host.yaml` sits in the same undifferentiated directory as `hq/`, and the two could not
  be more different.** `host.yaml` is machine-local *by design* — minted once, "never
  regenerated... never synced or templated" (`host.py`'s own docstring) — while `hq/` is a
  full git clone, fully replicated to every other host that adopts it. That distinction is
  currently invisible on disk; this doc (and the doctor check below) is most of the point.

## The four classifications

| class | means | if lost |
|---|---|---|
| **durable** | The only copy, or the copy of record. A fresh host must obtain it. | Real data loss, or a manual re-clone/re-setup. |
| **regenerable** | Derived from durable state; a `bh` verb rebuilds it. | Re-run the verb; nothing is lost. |
| **machine-local** | Correct to differ per host *by design* — identity, secrets, or a local path/tuning choice. Not merely "not yet synced." | Loses that host's identity/config, not shared truth; re-mint or re-configure. |
| **artifact** | A byproduct (backup, template render, migration leftover) — neither the source of truth nor rebuilt on demand by a normal verb. | Usually nothing; some are intentionally kept for recoverability, some are safe to prune. |

## The table

Every entry `bh` itself writes at the top level of `config.home()`, classified. "Accessor"
is the `config.py` (or module-local) function that resolves its path — `doctor._data_layout`
(below) walks these same accessors, so this table and the code cannot silently diverge.

| entry | class | accessor | notes |
|---|---|---|---|
| `hq/` | durable | `config.hq_dir()` | The Factory HQ store: a git repo + `.beads/`, fully replicated to every host that adopts it. A fresh host obtains this via `bh hq clone`. |
| `hub/` | regenerable | `config.hub_dir()` | Legacy pre-HQ aggregation store. `bh sync` rebuilds it from each hive; `hub.py`'s `_aggregation_target` already prefers `hq/` once one is registered. |
| `cache/` | regenerable | `config.cache_dir()` | Minimal-clone caches for hives that aren't locally checked out. `bh sync` / hub hydration re-fetch on demand. |
| `hitch/` | machine-local | `config.hitch_config_dir_root()` | Holds Claude Code's OAuth session state (`.claude.json`) — "nothing regenerates" it (the function's own docstring). Not durable in the shared sense: it's *this host's* login, not fleet truth. |
| `wt/` (or wherever `worktrees.path` points) | machine-local | `config.worktrees_root()` | Persistent worktree checkouts, only relevant when `worktrees.ephemeral: false`. Not "regenerable" in the low-stakes sense — a worktree can hold uncommitted work — but it is also never synced; treat it like other host-local working state. |
| `worktrees/` | **legacy — see Migration** | — | The *old* default `worktrees_root()` fallback (`config.home() / "worktrees"`, still literally in `config.py`) from before a host set an explicit `worktrees.path`. Not a distinct class of its own; it's drift, addressed below. |
| `hq-backups/` | artifact | `hq._backup_root()` | Pre-push backup tarballs (`bh hq push`'s three-level backup). Recoverability insurance, not a source of truth; auto-pruned to `backup.hq_keep` dated dirs right after each new one is taken and verified (bh-cmqp.2 — see [backup-retention-boundary-adr.md](backup-retention-boundary-adr.md)); `bh backup usage`/`reclaim --root hq` cover the manual case. |
| `backups/` | artifact | `backup.mirror_root()` | `bh backup export`'s JSONL interchange mirror, one `<provider>/<org>/<repo>/issues.jsonl` per hive. Overwritten each run — no history to prune under the default path (bh-cmqp.2). |
| `retros/` | durable, but **not bh-managed** | — (no code reference at all) | Human-authored retro notes living alongside `bh`'s home by operator convention. `bh` never reads or writes this directory — it is durable to the *operator*, out of `bh`'s contract entirely. |
| `config.yaml` | machine-local | `config.config_path()` | Post-`bh config split` (bh-e0y8.7), this holds only HOST-partition leaves (`worktrees.path`, `otel.*`, `work.identity`, `hq.remote`, …) — see `config_partition.py`. FLEET-partition truth lives in `fleet.yaml` *inside* `hq/`, which **is** replicated. `config.yaml` itself never is. |
| `config.yaml.bak` | artifact | `config_split_migration.BACKUP_SUFFIX` | One-time pre-split backup, taken once by `bh config split` and **left indefinitely by design** (its own docstring: "the original left recoverable"). Decision: keep leaving it — it's the one-time undo for a one-time, non-idempotent-looking operation, and it's tiny. Do not auto-delete it. |
| `host.yaml` | machine-local | `host.path()` | This machine's stable identity (`host_id` UUID + a cosmetic `label`). Minted once by `bh config init`, "never regenerated... never synced or templated" (`host.py` docstring) — the canonical machine-local file this whole doc exists to distinguish from `hq/`. |
| `labels.md` | regenerable | `config.docs_path()` | `registry.docs()` output (`bh hive docs` / labels report). Rebuilt from `config.yaml` on demand. |
| `docker-compose.yml`, `docker-compose.otel.yml` | artifact | `config.compose_file()`, `config.otel_compose_file()` | Template renders written by `bh config init`; regenerate with `--force`. |
| `.env` | machine-local | `config.env_file()` | Secrets (tokens, etc.) for the compose stack. Never synced, never templated after the initial copy. |
| `.env.example` | artifact | (copied from `config.template("env.example")` by `bh config init`) | Reference copy of the template; not read by anything, safe to overwrite/delete. |
| `setup-state.json` | regenerable | `setup.setup_state_path()` | `bh setup check`'s dependency-probe cache. Deleting it just forces one re-probe. |

**What a fresh host must obtain** (durable, not locally re-derivable): `hq/` (via `bh hq
clone`), plus whatever `retros/`-equivalent notes the operator personally keeps — nothing
else in the table. **Everything else** a fresh host either mints locally
(`host.yaml`, `config.yaml` via `bh config init`), rebuilds on first use (`hub/`, `cache/`,
`labels.md`, `setup-state.json`), or accumulates as a byproduct of normal operation
(`hq-backups/`, `.env.example`, `docker-compose*.yml`). This is also why `host.yaml` and
`hq/` "look the same" on disk today (two directories/files sitting side by side) despite
being opposite ends of the durable/machine-local axis — the table above is the missing
label; `bh doctor`'s layout check (below) is what keeps a host from silently drifting from
it.

## Migration: `wt/` vs `worktrees/` drift

**Root cause.** `config.worktrees_root()`'s own fallback, unconditionally, is
`config.home() / "worktrees"` — that's the path a host gets if it never sets
`worktrees.path` explicitly. A host that starts on the default, then *later* sets an
explicit `worktrees.path` (to `~/.beadhive/wt`, or anywhere else) leaves whatever
accumulated under the old default orphaned: nothing in `bh` ever cleans it up, because
nothing in `bh` ever *notices* — the two directories are just two more paths on disk, one
of which happens to no longer be read.

**Resolution.** Two directories with the same job is a bug, not a supported shape.
`worktrees.path` (or `$BH_WORKTREES`) — not the historical default name — is authoritative;
the legacy default is unconditionally cold once an explicit override exists.

**For an affected host:**

1. Confirm which is live: `bh config get worktrees.path` (or `$BH_WORKTREES`) names the
   active root; `bh doctor` also reports the legacy root explicitly when this drift exists
   (see below).
2. Confirm the legacy root (`~/.beadhive/worktrees`) is safe to remove: it holds nothing
   `bh` still reads once an explicit `worktrees.path` is set, but a worktree can hold
   uncommitted work, so check it isn't harboring an in-flight checkout before deleting:
   `find ~/.beadhive/worktrees -mindepth 4 -maxdepth 4 -type d` lists any triplet leaves
   still present.
3. If empty (the common case — see below): `rmdir` it, or just leave it; `bh doctor` will
   keep flagging it as drift until it's gone, but nothing else depends on its absence.
4. If non-empty: move any live worktree under it to the active root by hand (or let it
   finish/get abandoned normally, then remove the empty parent), then remove the directory.

No `bh` verb performs this migration automatically — worktree directories can hold
uncommitted work, so a destructive move-or-delete stays an operator decision, not a
default-path best-effort like `home_migration.py`'s directory move. What `bh` *does* do
going forward is **detect and report** the drift (next section), so it can't go unnoticed
the way it did before this doc existed.

## `bh doctor`: layout drift reporting

`bh doctor` already reports config drift, untracked repos, and stale worktree sandbox
grants in its Warnings section (`doctor._data_warnings`) — the natural home for this too,
rather than a new report nobody reads. Two checks, both pure `_data_layout(cfg)`:

1. **Unclassified top-level entry.** Every accessor in the table above is walked
   (`hq_dir()`, `hub_dir()`, `cache_dir()`, `hitch_config_dir_root()`, and
   `worktrees_root()` when persistent) alongside the fixed filenames
   (`config.yaml`, `host.yaml`, `labels.md`, …). Anything at the top level of
   `config.home()` that matches none of them is reported — a name this doc doesn't account
   for is exactly the kind of drift this contract exists to catch, whether that's a future
   `bh` feature that grew a new home-directory entry without updating this doc, or manual
   clutter.
2. **The `wt/`-vs-`worktrees/` drift specifically.** `config.home() / "worktrees"` existing
   on disk while the *active* `worktrees_root()` resolves somewhere else gets its own,
   more actionable warning (pointing at the Migration section above) instead of the generic
   unclassified-entry message.

Both checks are informational — `bh doctor` always exits 0, matching every other warning it
already surfaces. See `doctor._KNOWN_HOME_ENTRIES`, `doctor._configurable_home_entries`,
`doctor._legacy_worktrees_root`, and `doctor._data_layout` in `src/beadhive/doctor.py`.

## Out of scope

- **Automatic migration/cleanup.** `bh doctor` reports; it never deletes or moves state on
  disk. Same posture as its existing "stale sandbox grant" and "N local commits while not
  primary" warnings — surfaced, not auto-fixed.
- **A `bh home` (or similar) CLI surface for this.** Nothing here needs a new verb yet;
  `bh doctor` plus this doc's manual steps are enough until real multi-host operation
  (`multi-host-model-adr.md`) needs more.
- **Deleting `config.yaml.bak` automatically.** Decided above: left indefinitely, by design,
  matching its own docstring's stated intent.
