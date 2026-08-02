# Upgrading `bh`

Narrative upgrade notes for releases that change the shape of a user's on-disk state or CLI
surface — what you must run, what's safe to delete, and what must never be copied between
machines. For the mechanical per-commit list, see [CHANGELOG.md](../CHANGELOG.md); this file
exists for the releases where "read the changelog" isn't enough to act on.

## 0.6.0 → 0.7.0 — Factory HQ, the multi-host model, and the fleet/host config split

0.7.0's real subject is **multi-host**. Before this release, "the machine `bh` runs on" was
implicit and untracked; every host quietly assumed it was the only one. 0.7.0 makes the host a
first-class concept with its own identity, its own manifest, and a write fence — and Factory HQ
graduates from a local cache into the fleet's durable, remote-backed store. None of that is
visible from a version number, and it changes what's on disk enough that this is a guide, not a
changelog entry.

> **Breaking change, read this first:** `bh backup <dest>` is now `bh backup export [dest]`.
> See [§6](#6-breaking-change-bh-backup-is-now-bh-backup-export) below — this is the one thing
> in 0.7.0 that breaks existing muscle memory or a script.

**If you only ever run `bh` on one machine and never touch `bh hq`/`bh host`,** almost none of
this changes your day-to-day: `guard_primary` (§1) only refuses a write when a host *lease*
exists naming another host, and a factory that has never adopted anything has no lease at all —
single-host stays single-host, functionally unchanged. What still applies to you regardless:
the `bh backup export` rename (§6), and the fact that your first `bh hq init` will write real
disk (§5) the moment you decide to stand one up.

### 1. The multi-host model

A **host** is one `~/.beadhive` config home (usually one machine). 0.7.0 gives it four new
pieces of state, all covered in depth by
[`docs/design/multi-host-model-adr.md`](design/multi-host-model-adr.md):

- **Host identity — `~/.beadhive/host.yaml`.** A `host_id` (UUID) plus a cosmetic `label`,
  minted once by `bh config init` and **never regenerated, never synced, never copied to
  another host** — it is this machine's identity, not fleet truth. A hostname rename never
  changes it.
- **Per-host manifests in HQ — `hosts/<host_id>.yaml`.** Once a host registers
  (`bh host init` / `bh host provision`), its role (`primary-default` / `adopt-on-demand` /
  `worker`) and identity mechanism are recorded in Factory HQ, so `bh host list` can render the
  whole fleet's roster from one place.
- **The host lease + epoch fence.** Exclusive-write arbitration for a hive between hosts, split
  across two refs: a **lease** at `refs/bh/lease/<prefix>` in HQ (who *should* be primary —
  schedule, TTL, `bh host list`), and a **fence** at `refs/bh/epoch` beside the hive's own data
  (who *may* write — enforced atomically with the push). You manage this indirectly through
  `bh host adopt <hive>` / `bh host release <hive>` / `bh host packup`; you never touch either
  ref by hand.
- **`guard_primary()`.** The check every write verb (`bh work assign|claim|start|submit|merge`,
  `bh plan file`) now runs before mutating a hive: refuse if this host isn't the hive's leased
  primary. Reads (`ready`, `list`, `show`, `brief`, `sync`) are never gated. Critically: **an
  absent lease means "unconfigured", not "someone else's"** — until you `adopt` a hive from
  some host, every host may write it, exactly like before 0.7.0.

The practical upshot: multi-host is opt-in. Nothing forces exclusivity onto a single-machine
setup; it switches on the moment a *second* host adopts a hive you also work on.

### 2. Factory HQ

Factory HQ (`~/.beadhive/hq/`) is the fleet's durable, remote-backed store — full details in
[HQ.md](HQ.md). The verbs that matter for an upgrade:

| Command | Does |
|---|---|
| `bh hq init [--create] [--dry-run]` | Stand up (or idempotently re-wire) HQ: `bd`-init the local store (first run only), take a verified pre-push backup, scaffold `fleet.yaml`/`workspace.toml`/`hosts/`, then wire + push the remote. `--create` makes the remote as a private, empty repo when it doesn't exist yet. |
| `bh hq clone` | Bootstrap a **second** host with no local HQ: clone `main`, hydrate bead state from `refs/dolt/data`, register the `local/factory/hq` identity. Refuses if `~/.beadhive/hq` already exists — never clobbers. |
| `bh hq push [--dry-run]` | Publish HQ again after `init`: refresh the aggregate, commit any dirty `fleet.yaml` drift, push both the git half and the Dolt half. |
| `bh hq status` | Read-only ahead/behind for both halves against the wired remote. |
| `bh hq restore [--list] [--from DIR] [--level auto\|tar\|jsonl] [--dry-run] [--confirm]` | Restore HQ from a pre-push backup — `--level tar` replaces the Dolt store, `--level jsonl` upserts the portable export (works even with no readable store). |

The first `bh hq init` on a real fleet is not a cheap operation — see §5 before you run it.

### 3. The fleet/host config split

A flat `~/.beadhive/config.yaml` — the only shape that existed before 0.7.0 — mixed fleet-wide
truth (`orgs`, `dimensions`, `work.validate_cmd`, `managed_repos`, …) with host-local truth
(`worktrees.path`, `otel.*`, `work.identity`, `hq.remote`, …) in one file. 0.7.0 splits that
partition into two files that `config.load()` deep-merges at read time: `fleet.yaml` (inside
`~/.beadhive/hq/`, identical on every host) as the base, with the host's own reduced
`config.yaml` merged over it. Which key belongs to which side is *data*
(`config_partition.py`'s `FLEET_PREFIXES`/`HOST_PREFIXES`), not a branch you have to reason
about by hand. Full schema in
[CONFIGURATION.md — Fleet + host config](CONFIGURATION.md#fleet-host).

**This matters for exactly one host, and it's a one-time step:**

- **The host that FOUNDS a fleet** (the first one to run `bh hq init`) must also run
  `bh config split` — see [the walkthrough](#the-upgrade-end-to-end) below. `bh hq init` writes
  `fleet.yaml` from a
  snapshot of your *current* resolved config, but it never rewrites your own `config.yaml` to
  drop the now-duplicated fleet keys; left alone, the next `config.load()` sees the same
  fleet-classified keys on both sides and raises a `ConfigError` naming every offending key.
  `bh config split` is what actually reduces your file, and it's idempotent — a config with
  nothing fleet-shaped left in it is a clean no-op on re-run.
- **A host JOINING an existing fleet needs no manual split at all.** `bh hq clone` — and
  `bh host provision`, which calls it — reconciles the host config automatically: it drops the
  stale fleet-shaped keys your local template left behind (never merging them anywhere; the
  freshly cloned `fleet.yaml` is authoritative) and **reports what it dropped**. This is the
  direction that matters for almost every upgrading host, and it needs zero operator action.

Don't run `bh config split` on a joining host "just in case" — the two directions move keys
opposite ways (split *publishes* your leaves into `fleet.yaml`; clone's reconciliation *drops*
your stale copies), and a joining host has nothing of its own worth publishing.

### 4. Host lifecycle

New verbs to adopt/decommission a host, and — the part that's easy to get wrong — **two of them
sound almost identical and are not**:

| Verb | Scope | Does |
|---|---|---|
| `bh host provision --role <role> [--auto] [--dry-run]` | new host | The whole adoption path in one idempotent, resumable call: `config init` → `git workspace update` → resolve `hq.remote` → `hq clone` (auto-reconciling, per §3) → `host init` → per-hive `bead sync` → fix `.beads` permissions → verify. Every step probes before acting, so re-running against partial state is always safe. **Prerequisite:** `bh setup check` — see §10. |
| `bh host init --role <role>` | this host | Mint/write just this host's own manifest into HQ (`hosts/<host_id>.yaml`). What `provision` calls internally as one of its steps. |
| `bh host lease adopt <hive> [--force]` | this host, one hive | Become primary for `<hive>`: fence its remote, then lease it in HQ. |
| `bh host lease release <hive>` | this host, one hive | Yield this host's lease for `<hive>` (a tombstone; the epoch survives). |
| `bh host lease release --all` | this host, every hive | Release every lease this host currently holds — the "I'm switching machines" ritual. |
| **`bh host retire [--dry-run] [--backup] [--confirm] [--purge]`** | **HOST-LOCAL** | Decommission **THIS host only**: one SAFE/NEEDS_BACKUP/BLOCKED verdict across every hive, worktree, held lease, and HQ, then release leases → sync+push every hive → reclaim local clones/worktrees → deregister this host's manifest → push HQ. **Never touches `managed_repos`/fleet registration** — the fleet still has every hive after this runs, just not on this machine. |
| **`bh host rm <host_id> [--dry-run] [--confirm] [--force]`** | **FLEET-WIDE** roster entry | Unregister an *orphaned* manifest from HQ (e.g. a wiped-and-rebuilt host whose old entry never self-cleared). Registry-only — no clone, worktree, or history is touched. Requires `--confirm`; `--force` additionally bypasses the live-lease and recent-last-seen guards. |

And the hive-level counterparts — where the scope distinction is the single easiest thing to
get catastrophically wrong, because the verb names are so close:

| Verb | Scope | Does |
|---|---|---|
| **`bh hive rm <id>`** | **FLEET-WIDE** | Unregister a hive from `managed_repos` — shared fleet truth, so **every host loses this hive**, not just this one. Registry-only; leaves `.beads`/the clone intact. |
| **`bh hive retire <id> [--dry-run] [--backup] [--confirm] [--purge]`** | **FLEET-WIDE** | Guarded teardown: assess → backup-or-consent → worktree teardown → soft-archive the clone → unregister. The unregister step is the same fleet-wide `managed_repos` drop as `rm`, even though the clone/worktree teardown only touches this host. |
| **`bh hive reclaim <id> [--dry-run] [--backup] [--confirm] [--purge]`** | **HOST-LOCAL** | The *host-local-only* twin of `retire`: identical assess → backup-or-consent → worktree teardown → soft-archive, but **never unregisters** — `managed_repos` (and every other host's copy) is left untouched, so the hive stays registered for the fleet. Use this when only this host no longer wants a local copy. |

If you want to stop working on a hive from *this laptop* but keep it live for the rest of the
fleet, that's `bh hive reclaim` or `bh host retire` — never `bh hive rm`/`bh hive retire`, which
take it away from everyone.

### 5. Backups and disk

0.7.0 starts writing backups nobody asked for by name — they're a safety net, but they consume
real disk, and nothing pruned them before this release. Three independent roots, each with a
different owner and retention policy (full design:
[backup-retention-boundary-adr.md](design/backup-retention-boundary-adr.md)):

| # | Root | Written by | When | Default retention |
|---|---|---|---|---|
| 1 | `~/.beadhive/hq-backups/<date>/` | `bh hq init`, before HQ's first remote push | once, per remote-wiring event | keep newest **5** dated dirs (`backup.hq_keep`), auto-pruned right after each new verified backup |
| 2 | `<hive>/.beads/backup/` | `bd`'s own Dolt-native backup | periodic, `backup.interval` (bd's own timer) | rotate past **500 MB** (`backup.hive_cap_mb`), keep **3** generations (`backup.hive_rotate_keep`) — operator-invoked, not automatic |
| 3 | `~/.beadhive/backups/<provider>/<org>/<repo>/` | `bh backup export` | manual, ad hoc | overwritten each run — no history to prune |

**A real first `bh hq init` wrote 744 MB** (a 743,894,216-byte Dolt tarball plus a 5,504,318-byte
JSONL export) into `hq-backups/` on one run, with no pruning at the time. On a real, already
populated fleet, `bh backup usage` reported **1.4 GB across 21 backup roots** — mostly root #2,
which grows continuously with every commit until you rotate it. Don't let a full volume be how
you find out:

```sh
bh backup usage              # size + policy for all three roots, this host
bh backup usage --json       # machine-readable
bh backup reclaim --dry-run  # preview what a reclaim would free, all roots
bh backup reclaim --root hq --confirm            # prune HQ's dated dirs to backup.hq_keep
bh backup reclaim --root hive --confirm          # rotate the CURRENT hive's bd backup once over cap
bh config set backup.hq_keep 3                   # keep fewer HQ snapshots
bh config set backup.hive_cap_mb 200             # rotate a hive's bd backup sooner
```

`bh backup reclaim --root hive` needs `--confirm` to actually mutate (it rotates bd's live
backup destination via bd's own sanctioned lifecycle verbs, never chunk-store surgery) —
`--dry-run` previews with zero writes either way.

### 6. BREAKING CHANGE: `bh backup` is now `bh backup export`

Pre-0.7.0, `bh backup [dest]` was a bare command that wrote the JSONL interchange mirror.
0.7.0 needed `usage`/`reclaim` as proper subcommands of the same top-level name, and a
positional-argument-vs-subcommand parse conflict makes a hybrid "bare action, but also a
subcommand group" shape actively ambiguous — so `bh backup` is now a **group**, and the old
bare invocation is a subcommand:

```sh
# before 0.7.0
bh backup                 # exported the JSONL mirror
bh backup ./some/dest     # exported to an explicit destination

# 0.7.0 and later
bh backup export                 # same behavior, new name
bh backup export ./some/dest     # same behavior, new name
```

**If you have a script, alias, or cron job that calls bare `bh backup`, it now does nothing
useful** (`bh backup` with no subcommand just prints help) — update it to `bh backup export`.
This is pre-1.0 (`major_version_zero = true`), so a CLI rename is a MINOR version bump per this
project's own versioning convention, not something a major-version gate would have caught.

### 7. BREAKING CHANGE: `bh hive rm` now requires `--confirm`

`bh hive rm` drops the hive from `managed_repos` — **fleet truth**, so every host loses it, not
just the one running the command (see §4). Before 0.7.0 it took no flags at all and performed
that drop immediately: it printed a warning naming the consequence on the line *before* the
mutation, which announced the outcome at the moment you could no longer prevent it.

It now matches every sibling destructive verb:

```sh
bh hive rm <id> --dry-run    # preview; changes nothing
bh hive rm <id> --confirm    # perform the fleet-wide unregister
bh hive rm <id>              # refuses, exit 1
```

This closes an asymmetry rather than inventing a rule: `bh hive retire` runs the *identical*
`registry.unregister` as its final step and has always gated it behind `--confirm`. The same
fleet-wide drop was protected in one verb and unprotected in the other.

Both the refusal and `--dry-run` print the `bh hive add` invocation that would restore the
entry. `rm` is recoverable — but only while you still know the provider/org/repo/prefix/kind,
which is exactly what unregistering takes away.

**If you have a script calling `bh hive rm`, add `--confirm`.**

### 8. BREAKING CHANGE: `bh host` verb renames

Two renames land together, both inside the `bh host` group that is itself new in 0.7.0 — so
these only affect you if you were tracking pre-release builds.

**`bh host remove` is now `bh host rm`, and requires `--confirm`.**

`rm` is the decided spelling for "unregister one entity" across the whole CLI
(`bh hive rm`, `bh worktree rm` — see the verb model in
[`design/cli-mcp-naming-conventions-adr.md`](design/cli-mcp-naming-conventions-adr.md) §5b-i);
`remove` shipped against that convention and is corrected before it sets. It also picks up the
same `--dry-run` / `--confirm` gate every other teardown verb carries, and the old `--yes`
(self-removal) folds into `--confirm` — the two flags asked the same question at different
scopes:

```sh
bh host rm <host_id> --dry-run    # preview the plan; changes nothing
bh host rm <host_id> --confirm    # perform the fleet-wide unregister
bh host rm <host_id>              # refuses, exit 1
```

`--force` is unchanged and still separate: it bypasses the live-lease and recent-last-seen
guards, and `--confirm` never implies it.

**The lease verbs moved under `bh host lease`, and `packup` is gone.**

| Before | Now |
|---|---|
| `bh host adopt <hive>` | `bh host lease adopt <hive>` |
| `bh host release <hive>` | `bh host lease release <hive>` |
| `bh host packup` | `bh host lease release --all` |

Their object is a *lease* — a renewable, time-bounded claim over a hive — not the host. Flat,
`bh host release` sat next to `bh host retire` and read as its milder synonym when one is
reversible and the other terminal, and `bh host adopt <hive>` took a hive argument in a group
where every other verb takes a `host_id`. `packup` disappears because fan-out is spelled
`--all`, never its own verb name.

**All three old spellings keep working as hidden aliases**, so scripts do not break; they are
just off `--help`. Prefer the new forms in anything you write from here.

### 9. BEHAVIOR CHANGE: onboarding no longer installs the pre-push fence hook

`bh hive init` / `bh hive onboard` used to furnish a `pre-push` git hook in every hive — the
multi-host fence's early refusal. **They no longer install anything.** It is now opt-in:

```sh
bh hive hook install [HIVE_ID]     # install the fence shim for one hive
bh hive hook pre-push [HIVE_ID]    # the hook contract itself, for your own dispatcher
```

Two reasons, and the second is the one that matters.

**It was never the enforcement.** The fence's real backstop is the atomic
`--force-with-lease` epoch push (`refs/bh/epoch`, §1), which rejects a stale-epoch push
regardless of hooks *and* regardless of `git push --no-verify`. The hook is a local, fast-fail
refusal in front of it. Turning it off by default costs an early, legible error message — not
safety.

**The old default was already failing quietly.** The installer is deliberately
non-destructive: finding a `pre-push` it did not write, it skips and reports
`"skipped (custom hook present)"`. So every hive whose operator used *any* hook manager already
had no fence, while appearing to have one. Opt-in replaces a silent partial default with an
explicit choice.

If you want the early refusal, run `bh hive hook install` per hive. **Re-run it after the
first `bd dolt push`** on a fresh hive: with bd's embedded engine the push that carries
`refs/dolt/data` originates in a transport repo bd creates lazily, so before that there is
nothing to install into (the verb says so rather than failing).

Already-installed hooks keep working untouched — they are simply never created or refreshed
again unless you ask. If you use a hook manager (lefthook, husky, pre-commit), prefer wiring
`bh hive hook pre-push` as a job instead; it reads git's ref list on stdin and exits non-zero
to refuse, so any dispatcher can call it. See
[`design/hooks-as-functionality-adr.md`](design/hooks-as-functionality-adr.md).

### 10. `bh setup check` — a prerequisite `bh host provision` doesn't announce

Nearly every `bh` verb is gated behind a passing post-install dependency cache (`setup`,
`config`, and `doctor` are the only exemptions) — on a fresh host that has never run the check,
`bh host provision`, the recommended entry point for adopting a new host, refuses before doing
anything:

```text
✗ `bh host` requires setup — run `bh setup check` first.
  Skip with BH_SKIP_SETUP_CHECK=1 (debug bypass).
```

Running `bh setup check` once clears it (cached from then on), so it's a required first step,
not a dead end — but neither `provision`'s own 8-step plan (which starts one step later, at
`config init`) nor most written adoption sequences say so up front. This was found the hard way
during the 0.7.0 release-readiness pass and is tracked as a documentation/UX gap
([bh-1kzc](design/0.7.0-release-readiness.md#gap-1--bh-setup-check-is-an-undocumented-prerequisite),
still open) — until it's fixed in the tool itself, treat it as step 0 of every sequence below.

### 11. REQUIREMENT: your `bd` build must embed dolt >= 2.2.0

**This is the one to read if you run more than one host.** 0.7.0's multi-host model syncs bead
data with `bd dolt pull`. On a `bd` whose embedded dolt predates **v2.2.0**, that pull can hang
**indefinitely** on a large store — upstream [beads#4770](https://github.com/gastownhall/beads/issues/4770),
a quadratic `git cat-file` read. Measured here: 170s then killed, versus 3.2s on a fixed build.

**Every tagged `bd` release through v1.1.2 is affected.** v1.1.0, v1.1.1 and v1.1.2 all pin the
same dolt commit (`1bf533220ab0`, dated 2026-06-05) — 168 commits behind dolt v2.2.0
(2026-07-15). v1.1.2 shipped eleven days *after* the fix and did not pick it up. So a plain
`brew install beads` today gives you an affected build.

**Upgrading the standalone `dolt` CLI does not help.** dolt is statically compiled into `bd`
(a ~137 MB binary) and the CLI is never spawned. Verified by upgrading dolt 2.1.10 → 2.2.2 and
retesting: still hung.

Two escapes:

```sh
# 1. a HEAD build, until a tagged release carries the fix
brew unlink beads && brew install --HEAD beads

# 2. run bd against an external dolt sql-server >= 2.2.0
bd init --server --server-host 127.0.0.1 --server-port <port>
```

The second is the more durable answer: `bd` issues fetch/pull as `CALL DOLT_FETCH(...)` over
the database connection, so the **server's** dolt does the transport work — which takes the
dolt version out of `bd`'s release cadence entirely. Verified at the dolt layer: dolt 2.2.2
fetched the same remote in **11 seconds** where an affected embedded build hung past 240s.

`bh setup check` now warns when it detects an affected `bd`. It is a **warning, not a gate** —
the hang needs a large store to bite, so a small or new hive may never hit it, and blocking
setup over a probabilistic issue would be worse than the issue.

### The upgrade, end to end

**Once, on every host**, after installing the new `bh` version:

```sh
bh setup check     # clears the post-install dependency gate (§10) — do this first, always
bh config init     # idempotent: never touches an existing config.yaml, mints host.yaml if absent
```

**Then, exactly one path per host** — pick based on whether this host is starting a fleet or
joining one that already exists.

**Path A — this host FOUNDS the fleet** (you have no `hq.remote` anywhere yet, and no other
host has stood one up):

```sh
bh hq init --create          # stand up HQ; --create makes a private empty remote if needed
bh config split               # reduce this host's config.yaml now that fleet.yaml is real (§3)
bh host provision --role primary-default --auto   # finishes registration; earlier steps
                                                    # (config init, hq clone) are already done
                                                    # and report `skipped`, not repeated
```

**Path B — this host JOINS a fleet another host already founded** (`hq.remote` resolves to
someone else's HQ):

```sh
bh host provision --role worker --auto   # or --role adopt-on-demand for an intermittently-used
                                          # laptop; no manual `bh config split` needed (§3) —
                                          # `hq clone`'s own reconciliation handles it and
                                          # reports what it dropped
```

Either path, finish by checking you actually landed usable: `bh host provision`'s own final
step is a verify gate, but you can re-run it any time — `bh doctor` also folds in a fleet-health
section.

### What's safe to delete, and what must never be copied

0.7.0 doesn't change the answer for most of `~/.beadhive/` — the full durable / regenerable /
machine-local / artifact classification for every entry already lives in
[beadhive-home-layout-contract.md](design/beadhive-home-layout-contract.md); don't re-derive it
by hand. The two facts from that contract worth repeating here, because they're exactly what
0.7.0 makes newly relevant:

- **`~/.beadhive/host.yaml` is machine-local, by design, forever.** Minted once, never
  regenerated, and explicitly never synced or templated. **Do not copy it to another host, put
  it in dotfiles sync, or restore it from another machine's backup** — doing so would make two
  hosts share one `host_id`, which breaks the fencing/identity model `guard_primary` and the
  per-host HQ manifest both key off. If a host is wiped and rebuilt, it gets a *new* `host.yaml`
  (and its old HQ manifest becomes exactly the kind of orphan `bh host remove` exists to clean
  up).
- **`~/.beadhive/hq-backups/` and `<hive>/.beads/backup/` are artifacts, not sources of truth.**
  Safe to prune with `bh backup reclaim` (§5); never the *only* copy of anything durable.
  `~/.beadhive/hq/` itself, by contrast, **is** durable — a fresh host obtains it via
  `bh hq clone`, never by hand-copying another host's directory.

## See also

- [HQ.md](HQ.md) — Factory HQ in full: layout, naming, hub-vs-HQ, `push`/`status` mechanics.
- [CONFIGURATION.md — Fleet + host config](CONFIGURATION.md#fleet-host) — the full partition
  schema, `--scope fleet|host`, and `bh config split`'s exact behavior.
- [design/multi-host-model-adr.md](design/multi-host-model-adr.md) — the decision record for
  the host/lease/epoch model, including the amendment that split the lease from the fence.
- [design/backup-retention-boundary-adr.md](design/backup-retention-boundary-adr.md) — why
  three backup mechanisms exist and the retention policy behind each.
- [design/beadhive-home-layout-contract.md](design/beadhive-home-layout-contract.md) — every
  `~/.beadhive/` entry classified durable / regenerable / machine-local / artifact.
- [design/0.7.0-release-readiness.md](design/0.7.0-release-readiness.md) — the verified pass
  this guide's upgrade sequence is drawn from.
- [CHANGELOG.md](../CHANGELOG.md) — the mechanical, per-commit record.
