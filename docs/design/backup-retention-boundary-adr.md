# Backup mechanisms: boundary + retention ADR (bh-cmqp.2)

> Status: **shipped.** Answers the design question bh-cmqp.2 opens with: does `bh` need three
> independent things writing backups, and if so, what is each one actually FOR? Then gives
> every surviving root an implemented (not merely documented) retention policy.

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
| 1 | `~/.beadhive/hq-backups/<date>/` | `hq._take_backup` (`bh`) | once, before HQ's first remote push (a one-way schema-migration decision) | a broken/lost HQ store at the single highest-stakes moment in its lifecycle | `bh hq restore` (bh-cmqp.1) |
| 2 | `<hive>/.beads/backup/` | `bd`'s own Dolt-native backup | periodic, `backup.interval` (bd's own daemon-less timer, driven by hive activity) | ordinary hardware/disk loss on a machine that's been actively used — off-machine recovery for the *live working database* | `bd backup restore` |
| 3 | JSONL mirror (`bh backup export`) | `bh backup` (operator-invoked) | manual, ad hoc | nothing automatically — it is a portable interchange snapshot for migration, handoff, or "let me have a copy of this before I do something risky by hand" | whatever the operator hands it to; **not** a `bh`/`bd` restore source |

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
