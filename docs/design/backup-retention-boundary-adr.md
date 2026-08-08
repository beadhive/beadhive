# Backup mechanisms: boundary + retention ADR (bh-cmqp.2, amended by bh-5009a)

> Status: **shipped.** Answers the design question bh-cmqp.2 opens with: does `bh` need three
> independent things writing backups, and if so, what is each one actually FOR? Then gives
> every surviving root an implemented (not merely documented) retention policy.
>
> **Amended by bh-5009a** (2026-08-08): a FOURTH mechanism — `bh hive migrate-storage`'s
> pre-migration backup — turned out to be writing outside this contract entirely, and the
> three roots that were inside it disagreed on location, hive addressing, and time format.
> The amendment adds root #4, consolidates all four under `$BH_HOME/backups/`, and gives every
> dated set a `manifest.json`. The boundary reasoning below is unchanged — it was right, and
> root #2's immovability in particular is restated rather than relitigated. See
> [the amendment](#amendment-bh-5009a-one-root-four-categories) at the end.

## The question

Standing up a real HQ (`bh hq init --create`, 2026-08-01) surfaced three independent
mechanisms writing backups, none aware of the others, none pruning:

1. `hq._take_backup` → `~/.beadhive/hq-backups/<YYYY-MM-DD>/` — fires once per remote-wiring
   event. One run: 744 MB (`hq-embeddeddolt.tar.gz` 743,894,216B + `hq-issues.jsonl`
   5,504,318B).
2. bd's own Dolt-native backup → `<hive>/.beads/backup/` — a real Dolt/noms content-addressed
   store, synced on `backup.interval` (default 15m when a git remote exists). 171 `.darc`
   table files / 185 MB in this hive alone, growing with every commit.
3. `bh backup` → a JSONL mirror, previously defaulting to `./backup` **relative to cwd**.

"Keep all three" was the leading hypothesis going in (bh-cmqp's own epic description calls
them out as having genuinely different jobs), and it holds up: each protects a different
failure and none of the other two can stand in for it. What did NOT hold up is "no boundary,
no retention" — that part this ADR fixes.

## The boundary

| # | root | writer | trigger | protects against | consumed by |
|---|---|---|---|---|---|
| 1 | `$BH_HOME/backups/hq/<instant>/` | `hq._take_backup` (`bh`) | once, before HQ's first remote push (a one-way schema-migration decision) | a broken/lost HQ store at the single highest-stakes moment in its lifecycle | `bh hq restore` (bh-cmqp.1) |
| 2 | `<hive>/.beads/backup/` | `bd`'s own Dolt-native backup | periodic, `backup.interval` (bd's own daemon-less timer, driven by hive activity) | ordinary hardware/disk loss on a machine that's been actively used — off-machine recovery for the *live working database* | `bd backup restore` |
| 3 | `$BH_HOME/backups/mirrors/<triplet>/` (`bh backup export`) | `bh backup` (operator-invoked) | manual, ad hoc | nothing automatically — it is a portable interchange snapshot for migration, handoff, or "let me have a copy of this before I do something risky by hand" | whatever the operator hands it to; **not** a `bh`/`bd` restore source |
| 4 | `$BH_HOME/backups/migrate/<triplet>/<instant>/` | `storage_migrate.take_backup` (`bh`) | once per hive, immediately before `bh hive migrate-storage` touches anything | a storage-mode migration that loses or corrupts a hive's corpus — the *only* copy taken from the live embedded store before the mechanism runs | `bd backup restore` (the set's `dolt-native/`); `bd import` (its `issues.jsonl` floor) |

**Why all three, not fewer:**

- **#1 is a ONE-WAY-DECISION snapshot, not a schedule.** It exists because giving HQ's Dolt
  store a remote is irreversible in a specific way (bh-e0y8.2's finding): once pushed, a
  schema migration becomes a fleet-wide decision. #2's periodic timer doesn't help here —
  the moment that matters is the wiring event itself, and #1 is the only mechanism pinned to
  it. It is also the only level with a matching **restore** path (bh-cmqp.1) — the whole
  reason it captures a full-fidelity tarball, not just the JSONL floor.
- **#2 is bd's own concern, not `bh`'s.** `bh` doesn't own the write path (bd's interval
  timer fires independent of any `bh` command running) or the on-disk format (a real Dolt
  chunk store — `manifest`, `LOCK`, `oldgen/`, content-addressed `.darc` table files, the
  exact shape of `.dolt/noms/`). Folding this into #1 or #3 would mean `bh` re-implementing a
  database backup engine; not doing that is the actual "consolidation" decision here — the
  three mechanisms are consolidated in the sense of getting one shared *contract*
  (boundary + retention, this doc, `backup.py`), not in the sense of merging code paths that
  have no business sharing one.
- **#4 is the strongest artifact `bh` writes, and it is pinned to a different one-way moment
  than #1.** It takes BOTH formats (JSONL floor + Dolt-native full fidelity), because the
  Dolt-native level is the only one that restores into a DIFFERENT engine mode than it was
  taken from — which is exactly what a storage migration is. #1 can't stand in for it (wrong
  moment, wrong hive — #1 is HQ-only), #2 can't (bd's timer fires on its own schedule, not on
  the migration), and #3 can't (JSONL is a floor, not a restore source). bh-5009a added it to
  this table; before that it was writing 28 MB per hive to a root nothing reported and nothing
  pruned.
- **#3 is the only one aimed at a human on the other end.** JSONL is the interchange format
  (migration, cross-tool, "let me look at this in a text editor"), not a recovery format —
  restoring HQ from JSONL already has a dedicated, better path (`bh hq restore --level
  jsonl`, which is level #1's JSONL floor, not this one). #3 existing lets an operator take a
  point-in-time export without needing to understand Dolt at all.

## Retention, per root

Three different policies, matched to who owns each write path.

### #1 — HQ pre-push backup: automatic keep-N

`bh` owns this write path end to end (`hq._take_backup`), so retention runs automatically,
synchronously, immediately after a NEW backup is taken and verified — the same call site that
already refuses to push on an unverified backup (`hq._wire_remote`). Older dated directories
beyond the newest N are removed; **N is never allowed to go below 1** (a `keep=0` config value
is clamped up, not honored — retention must never leave zero restorable backups after taking
a brand new one).

- Config: `backup.hq_keep` (default **5**). Host-scoped — how much of *this host's* disk an
  operator wants reserved for insurance is a machine-local tuning knob, not fleet policy
  (`config_partition.py`).
- Manual escape hatch: `bh backup reclaim --root hq [--dry-run]` re-applies the same keep-N
  policy on demand (lowering `backup.hq_keep` after the fact, or reclaiming ahead of the next
  wiring event).
- Never touches restore's contract: `bh hq restore --list` only ever needs the newest good
  set, and pruning strictly keeps the newest N — restore's minimum retained set (documented in
  `hq_restore.py`: "the newest set that holds a restorable level") is always inside what's
  kept.

### #2 — bd's own per-hive backup: operator-invoked rotate + keep-N generations

`bh` does not own this write path (bd's own interval timer does) and will not reach into a
live Dolt chunk store and delete individual `.darc` files — `bd backup remove --help` itself
documents that removing the *destination* does not delete the *data*, a explicit signal from
the vendor that touching backup contents behind the operator's back is out of bounds. Instead,
`bh backup reclaim --root hive [--confirm]` performs a **rotate**, using only bd's own
sanctioned lifecycle verbs plus a plain filesystem rename (no chunk-store surgery):

1. `bd backup remove` — unregisters the destination (data untouched, per bd's own contract).
2. `mv <hive>/.beads/backup <hive>/.beads/backup.<timestamp>/` — the SAME "move aside, never
   delete outright" idiom `hq_restore._apply_tar` already uses for the live store it replaces.
3. `bd backup init <hive>/.beads/backup` + `bd backup sync` — a fresh, empty destination at
   the canonical path; the new sync writes only the CURRENT live state, not the accumulated
   backup history, so the destination shrinks back to roughly the live database's size.
4. Keep-N over the rotated generations (`backup.<timestamp>/`) — the ones from step 2 accumulate
   like #1's dated directories and get the identical newest-N-survives prune.

This only runs when explicitly invoked (`bh backup reclaim --root hive`), gated by a size cap
so routine runs are a no-op, and requires `--confirm` for the real (non-dry-run) rotate —
matching `hq_restore`'s `--dry-run` default / `--confirm`-to-mutate convention.

- Config: `backup.hive_cap_mb` (default **500**) — rotate only fires past this size.
  `backup.hive_rotate_keep` (default **3**) — generations kept after rotating.
- `bh backup usage` always reports current size regardless of the cap, so an operator can see
  growth coming before it matters.

### #3 — JSONL mirror: keep-1, by construction

No pruning code exists for this root because none is needed: `bh backup export` always writes
`issues.jsonl` to the SAME fixed per-hive path and overwrites it — there is no history to
prune under the default destination. An operator who passes an explicit timestamped `dest`
has opted into managing that copy themselves (same posture the layout contract gives
`retros/`: durable to the *operator*, outside `bh`'s contract) — `bh` will still report its
size in `bh backup usage` for visibility, but does not invent a retention scheme for a
directory the operator explicitly named.

The one real bug here — `./backup` defaulting relative to cwd, so *where* it lands depended on
which subdirectory of a hive the operator happened to be standing in (the same failure class
as bh-mw97's `hq.remote`) — is fixed by resolving a fixed per-hive default instead:
`~/.beadhive/backups/<provider>/<org>/<repo>/`, keyed off the SAME cwd→hive identity
resolution `registry.current_hive`/`worktree.cwd_identity` already use elsewhere (not raw
`Path.cwd()` string matching). A hive `bh` can't identify at all (no git-workspace/worktree
identity resolvable) still gets a stable answer — the git top-level directory's own name —
so a subdirectory never changes the answer even in that fallback case.

## Operator surface

`bh backup` (Typer group, `src/beadhive/cli.py`):

- `bh backup export [dest]` — the JSONL mirror (was the bare `bh backup [dest]`; renamed
  because `usage`/`reclaim` needed the top-level name as a proper subcommand group — a
  positional-argument-vs-subcommand parse conflict with Click makes a hybrid
  default-action-plus-subcommands shape actively ambiguous, confirmed empirically before
  choosing this over it). Pre-1.0 (`major_version_zero = true`), so a CLI rename is a MINOR
  bump, not a breaking-major one.
- `bh backup usage [--json]` — size (and policy) for all three roots: HQ's dated directories,
  every registered hive's `.beads/backup/`, and the current hive's mirror export. The "see
  backup disk usage" half of the acceptance bar.
- `bh backup reclaim [--root hq|hive|all] [--hive ID] [--dry-run] [--confirm]` — applies the
  policy above; `--dry-run` previews with zero mutation (default output-mode for `--root
  hive`'s rotate, matching `hq_restore`); reports bytes reclaimed. The "reclaim backup disk
  usage" half.

## Out of scope

- **Automating #2's rotate.** Deliberately operator-invoked, not hooked into bd's own sync
  timer — `bh` doesn't own that timer and firing an unregister/rotate/reinit dance from
  inside it would race bd's own in-flight sync. `bh doctor`/`bh backup usage` surface the size
  so an operator notices before it's urgent, the same posture as the layout contract's
  "reports, never auto-fixes" stance on the `wt/`/`worktrees/` drift.
- **A history feature for the JSONL mirror.** Not asked for by the bead, and inventing
  accumulation (and then a retention scheme for it) where the current design has none would be
  new complexity solving a problem nobody has yet — an explicit `dest` remains the escape
  hatch for an operator who does want a series of dated exports.
- **Fleet-wide `bh backup reclaim --all-hives`.** `usage` already sweeps every registered
  hive (read-only); `reclaim --root hive` stays scoped to one hive at a time (default: cwd's)
  to keep a destructive-ish rotate's blast radius small and deliberate, matching `hq_restore`
  requiring an explicit target rather than "restore whichever one looks newest, fleet-wide."

---

## Amendment (bh-5009a): one root, four categories

### What was wrong

`bh hive migrate-storage` produced two backup-shaped artifacts, and NEITHER was part of the
contract above:

1. `~/.beadhive/storage-migrate-backups/<hive>/<stamp>/` — the verified JSONL + Dolt-native pair
   taken before anything destructive. Correctly OUTSIDE the repo, but invisible to
   `bh backup usage` and with no retention policy. nvhack's migration alone wrote 28,268,281 B;
   nothing pruned it.
2. `<hive>/.beads/embeddeddolt.pre-migrate-<stamp>/` — the moved-aside original store, left
   INSIDE the operator's repo. 46 MB for nvhack; ~330 MB for the beadhive hive.

The inconsistency is the tell: the same operation wrote its verified backup outside the repo and
left its moved-aside store inside it. Same purpose, two policies.

Meanwhile the three roots this ADR *did* define disagreed on three independent axes — location
(`hq-backups/` vs `backups/` vs `storage-migrate-backups/`), hive addressing (`<provider>/<org>/
<repo>` vs a flattened sanitized `github-briancripe-nvidia-hackathon`), and time format
(`2026-08-08` vs none vs `2026-08-08T165333Z`). Root #1's path was hardcoded rather than resolved
through `config.home()`, so it was the one root a `$BH_HOME` override could not move.

### The layout

```text
$BH_HOME/backups/
├── hq/
│   └── 2026-08-08T165333Z/
│       ├── manifest.json
│       ├── issues.jsonl
│       └── dolt-native/            (or embeddeddolt.tar.gz for an embedded-mode HQ)
├── mirrors/
│   └── github/beadhive/beadhive/
│       └── issues.jsonl            ← keep-1, overwritten each run
└── migrate/
    └── github/briancripe/nvidia-hackathon/
        └── 2026-08-08T165333Z/
            ├── manifest.json
            ├── dolt-native/        ← full fidelity, bd-restorable
            └── issues.jsonl        ← interchange floor
```

- **One root, category → hive → instant.** Category first also removes a namespace collision:
  `hq`/`mirrors`/`migrate` can never be mistaken for a provider, whereas the old mirror root was
  keyed by provider directly under `backups/`.
- **Triplet hive addressing everywhere.** Mirrors `$GIT_WORKSPACE`, needs no sanitization, and
  avoids a second addressing scheme.
- **One time format** — `%Y-%m-%dT%H%M%SZ`, lexically sortable, so retention is a sort-and-slice
  in every root. Root #1 previously used a date-only name, so two wiring events on one day landed
  in the same directory and the keep-N window counted 1 where the operator had taken 2.
- **No redundant filename prefixes.** `nvhack-issues.jsonl` → `issues.jsonl`, `hq-dolt-native/` →
  `dolt-native/`. The path already names the hive, and a prefixed filename goes stale on a prefix
  rename while the path-based one does not.

`hq/` is a subject where `mirrors`/`migrate` are kinds, and HQ will legitimately appear twice once
HQ itself migrates (`hq/<instant>/` for pre-push, `migrate/local/factory/hq/<instant>/` for its
storage migration). Renaming it to `prepush/` was considered and rejected: `manifest.json`'s
`kind` field disambiguates at zero path cost. Recorded so the next reader doesn't "fix" it.

### `manifest.json`

None of the roots had one. Consequence: nothing on disk said whether a backup VERIFIED, what it
was taken BEFORE, or which `bh` wrote it — `bh backup usage` had to stat directories and infer,
and a restore had to trust the operator's memory.

```json
{ "kind": "migrate", "hive": "github/briancripe/nvidia-hackathon", "prefix": "nvhack",
  "taken_at": "2026-08-08T16:53:33Z", "bh_version": "0.8.7",
  "source_dolt_mode": "embedded", "target_dolt_mode": "server",
  "issue_count": 434, "verified": true,
  "artifacts": { "dolt-native": 28268281, "issues.jsonl": 554601 } }
```

Both the triplet AND the prefix are recorded, deliberately: a triplet moves when a hive's
provider/org/repo changes (bh-l9h56 / bh-484xb are about exactly that) and a prefix moves when the
prefix changes, so neither is stable alone. Recording both at least makes an orphaned set
self-identifying. An absent manifest means **unknown**, never *unverified* — every set already on
disk predates this file.

### Retention, per root (amended)

| root | policy | config |
|---|---|---|
| #1 HQ pre-push | automatic keep-N after each verified new set | `backup.hq_keep` (**3**, was 5) |
| #2 bd's own per-hive | operator-invoked rotate + keep-N generations — **unchanged, see below** | `backup.hive_cap_mb`, `backup.hive_rotate_keep` |
| #3 JSONL mirror | keep-1 by construction | — |
| #4 pre-migration | automatic keep-N **per hive** after each verified migration | `backup.migrate_keep` (3) |
| — | total-across-all-roots warning in `bh backup usage` | `backup.total_warn_mb` (2048; 0 disables) |

**#4's keep-N is per hive, not across the root**, or a fleet migration would let one hive's three
sets evict another hive's only one.

**`backup.hq_keep` 5 → 3 plus a size warning.** An HQ set is roughly the size of HQ's own store
(~138 MB post-GC on the reference host), so five is most of a gigabyte held against a
once-per-lifetime event. The alternative considered was **pruning the pre-push set once the push
succeeds and the remote exists** — rejected. This ADR's own reasoning for root #1 is that giving
HQ's store a remote is a one-way schema-migration decision; after a successful push the REMOTE
holds the post-migration state, so a migration that corrupted something has already propagated the
corruption. "Remote exists" proves the data transferred, not that it is correct, and pruning would
destroy the only clean rollback point. keep-3 plus a warning gets the disk relief without that
trade.

**Root #2 still cannot move, and this is not up for relitigation.** `bh` owns neither its write
path (bd's interval timer) nor its format; `bd backup remove --help` documents that removing the
destination does not delete the data — an explicit signal from the vendor that touching backup
contents behind the operator's back is out of bounds. The original ADR calls not re-implementing a
database backup engine "the actual consolidation decision here", and bh-5009a consolidates the
CONTRACT (locations, retention, operator surface), not the code paths.

### The in-repo pre-migrate store: pruned, not archived

Measured on nvhack's real post-migration artifacts:

```text
.beads/embeddeddolt.pre-migrate-<stamp>/  = 46 MB total
  noms/              27 MB   ← the actual data
  git-remote-cache/  19 MB   ← transport cache, derived junk
  stats/ tmp/ temptf/        ← scratch
backups/migrate/<triplet>/<instant>/dolt-native/ = 27 MB  (same data, cleaner)
```

The moved-aside store's `noms/` is the SAME SIZE as the migrate set's `dolt-native/`. Archiving it
wholesale would store ~19 MB of derived transport cache per hive to preserve data the migrate set
already holds in bd-restorable form — ~100 MB of waste across the remaining fleet. Its one unique
property is an IN-PLACE rollback (rename it back), and that expires the moment `verify_migration`
passes.

So: **rename aside, then remove — strictly after verification** — with `--keep-pre-migrate` for an
operator who wants the rollback window. A side effect worth naming: deleting has no
filesystem-boundary problem, so the cross-filesystem `mv` concern dissolves entirely.

This is what retires bh-xsv3's auto-commit. That fix appended
`embeddeddolt.pre-migrate-*/` to a furnished hive's tracked `.beads/.gitignore` **and committed
it** — because otherwise the kept store surfaced as hundreds of MB of untracked files, one
`git add -A` from being committed. With the store gone by default there is nothing to ignore. The
gitignore write survives, narrowed to `--keep-pre-migrate`; the auto-commit does not. A storage
migration has no business authoring commits in the operator's repo — it reports the edit and lets
them decide.

### Relocating what already exists

Both a read-both-locations period AND a one-time relocation, because either alone is
insufficient:

- **Read-both** (`backup.legacy_roots`, `hq_restore.list_backups`, `backup._hq_backup_dirs`) is
  the safety net. A backup taken before this amendment is still the only pre-push copy of that
  host's HQ, and a relocation that silently stopped seeing it would be precisely the failure this
  contract exists to prevent. Keep-N spans both roots, so the legacy locations drain on their own
  as their sets age out. The legacy `hq-` artifact filenames are accepted on the read side too.
- **`bh backup migrate-layout`** (`--dry-run` default, `--confirm` to apply) is the tidy-up. Same
  filesystem: a rename, atomic, no second copy on disk. Across filesystems: copy, verify the
  copy's size, and only then remove the source — `EXDEV` is detected explicitly rather than
  papered over by an unconditional `shutil.move`. A destination that already exists is reported,
  never merged over. A legacy migrate directory no registered hive claims lands under
  `migrate/_unresolved/` rather than being guessed at or discarded — an orphan from a retired or
  renamed hive is still somebody's only pre-migration backup.

### Operator surface (amended)

- `bh backup usage [--json]` — now reports all four roots, plus any leftover in-repo pre-migrate
  store, plus a `legacy …` row per pre-relocation root that still holds something, plus the
  total-size warning. `--json` gained an envelope (`{roots, total_bytes, warning}`).
- `bh backup reclaim --root hq|hive|migrate|all` — `migrate` prunes the pre-migration sets to
  `backup.migrate_keep` and, with `--confirm`, removes leftover in-repo pre-migrate stores. The
  in-repo half is `--confirm`-gated where the sets are not: it deletes inside the operator's own
  working tree, a different blast radius from pruning `bh`'s own artifact root.
- `bh backup migrate-layout [--dry-run|--confirm]` — the one-time relocation above.
- `bh hive migrate-storage --keep-pre-migrate` — opt back into the in-place rollback window.
