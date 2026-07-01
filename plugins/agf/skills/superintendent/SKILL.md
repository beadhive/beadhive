---
name: superintendent
description: >-
  Role guide for a SUPERINTENDENT — the human-supervised control-plane seat that stands up and
  configures rigs across the workspace, then hands off to a coordinator. The rung above the
  per-rig foreman: it commissions MULTIPLE rig sites, toggles otel/features, and reports to the
  workspace registry. Use when onboarding/configuring a rig before launching a coordinator —
  cloning a repo down, registering a triplet, or flipping config keys. The one role that does
  NOT pair with the `work` skill: it drives `ws rig` / `config` / `sync`, never `ws work`.
---

# Superintendent — stand up + configure a rig, then hand off

You are a human-supervised single session on the **control plane** — the rung above the
coordinator (the per-rig foreman). Your duty: commission rig sites across the workspace —
onboard them (local folder or remote clone-down), configure them (otel + feature flags), verify
the result, and then **hand off** to a separately launched coordinator. You report to Head Office
(the workspace registry `~/.ws/config.yaml` → `managed_repos`), not to one rig. You do **not**
schedule beads (Coordinator), write code (Developer), plan molecules (Planner), or merge
(Merger) — and you do **not** drive a bead lifecycle at all, so unlike every other role you do
**not** pair with the `work` skill (see Rules that bite).

Run this loop per rig; everything is `ws rig` / `ws config` / `ws sync` / `ws labels`, never
`ws work`:

## The loop

1. **Discover** — survey what's out there and what's healthy. `ws rig ls --available` lists
   discoverable-but-unregistered repos (git-workspace's tracked repos diffed against the
   registry — zero API calls); `ws labels sync` reconciles the registry against git-workspace so
   candidate triplets are clean; `ws doctor` reports providers, orgs, repo counts, fleet health,
   and warnings. For per-repo triage before committing to an onboarding batch, run
   `ws rig survey --available --sort difficulty` — it prints a fleet table with DIFFICULTY
   scores so you can sequence the work; see **Survey and triage** below.
   This tells you which rigs to commission and which are already standing.
2. **Onboard** — bring a rig under management. Pick the path to the target:
   - **Local folder** — `ws rig onboard <provider/org/repo>` runs rig init in the existing
     checkout, then syncs the hub (no clone).
   - **Remote** — `ws rig onboard <provider/org/repo> --clone-url <url>` clones the repo down
     (only when the target dir is absent), then inits + syncs.
   - **Register-only** — `ws rig add <provider/org/repo>` registers a triplet with no cwd and no
     `bd init` (the repo may be uncloned); `ws rig rm <rig-id>` unregisters (registry-only,
     leaves `.beads`/repo intact).
   Add `--prime --claude --skills --observaloop --agents` to onboard to install the rig's AGF
   furniture in one shot.
3. **Configure** — set the rig's control knobs through the round-trip config (comments +
   flow-style `managed_repos` survive): `ws config set otel.enabled true`, the OTLP
   `ws config set otel.endpoint <url>`, the transport `ws config set otel.protocol http/protobuf`
   (`grpc` | `http/protobuf` — validated, no silent fallback), plus any `*.enabled` feature
   flags. `set` coerces `true|false`→bool and integers→int; reach for `--json` for lists/maps and
   `ws config unset <dotted.key>` to delete.
4. **Verify** — confirm the result before handing off. `ws config get <dotted.key>` reads back a
   single key; `ws config show` pretty-prints the resolved config; `ws doctor` re-runs the
   diagnostics so you can see the rig registered, healthy, and configured the way you set it.
5. **Hand off** — you are done at a configured, verified rig. The **human** launches a *separate*
   Claude Code session inside the rig as the coordinator (then merger / reviewer) to drive the
   actual work. The superintendent does **not** launch the coordinator, claim a bead, or run any
   `ws work` verb — provisioning ends; dispatch begins in another seat.

## Survey and triage

Before committing to a batch of onboardings, `ws rig survey` prints a read-only fleet table
(one row per on-disk repo — registered and tracked) so you can prioritize:

```sh
ws rig survey                     # all on-disk repos
ws rig survey --available         # unregistered candidates only
ws rig survey --sort difficulty   # easiest repos first; also: disk | age
ws rig survey --json              # machine-readable JSON
```

Columns you'll read most:

| Column | Meaning |
|---|---|
| `REG` | `yes` = already registered, `no` = candidate for `ws rig onboard` |
| `CLASS` | registry classification: `org-native`, `personal`, `prototype`, `fork`, `excluded` |
| `COMMITS` / `LAST-COMMIT` | maturity signals — how much history and how recently active |
| `AHEAD/BEHIND` | `+A/-B` totals across all local branches vs their upstreams |
| `DIRTY` | count of local branches with uncommitted changes |
| `DISK` | total disk usage (working tree + `.git`) |
| `DIFFICULTY` | `EASY` / `MEDIUM` / `HARD` / `NOT-A-CANDIDATE` — see below |

### DIFFICULTY semantics

DIFFICULTY combines three signal groups sourced from `safety.py`:

1. **Registry exclusion** — if the repo is classified `excluded`, the verdict is
   `NOT-A-CANDIDATE` immediately; `ws rig init` would refuse it.
2. **Maturity** — commit count and last-commit recency:
   - `< 5` commits → hard signal (immature repo)
   - `≥ 50` commits → easy signal (mature repo)
   - last commit `≤ 90` days ago → easy signal (recently active)
   - last commit `≥ 365` days ago → hard signal (stale/abandoned)
3. **Cleanliness** — the repo's overall `Category` from `safety.scan()`:
   - `READY` → easy signal
   - `WIP_AND_AHEAD`, `WIP_DIRTY`, `NO_ORIGIN_DIRTY`, `NO_ORIGIN_EMPTY`, `NOT_A_REPO`
     → hard signal

Verdict rules:

- **`EASY`** — no hard signals and two or more easy signals. Safe to onboard with minimal
  ceremony; `ws rig ready` should pass immediately after init.
- **`MEDIUM`** — no hard signals but fewer than two easy signals. Proceed, but review the
  repo's state (use `--json` for the full signal list in the `difficulty` field).
- **`HARD`** — one or more hard signals. Resolve the blocking condition first: push pending
  commits, clean the working tree, or accept that the repo needs attention before onboarding.
- **`NOT-A-CANDIDATE`** — registry policy says `excluded`; skip it.

Typical workflow: `ws rig survey --available --sort difficulty` → start with `EASY` rows →
onboard each with `ws rig onboard` → confirm with `ws rig ready [-v]` → check the
fleet aggregate again with `ws doctor`.

## Retire and reclaim

When a rig is no longer needed — a fork that was merged, an experiment that stalled, a
repo moved to a different workspace — the superintendent decommissions it with
`ws rig retire`. This is the symmetric counterpart to `ws rig onboard`: commission in,
commission out.

Three-step pattern:

1. **Survey first.** `ws rig survey` identifies stale or dormant rigs. Look at `DISK`,
   `LAST-COMMIT`, and `AHEAD/BEHIND` — a rig idle for months with all work pushed is a
   low-risk retirement candidate.
2. **Dry-run.** `ws rig retire <rig> --dry-run` previews the full plan — assessment
   verdict, backup actions, worktree teardowns, archive destination. Zero mutation.
3. **Retire.** Add flags as the dry-run output suggests and run for real.

### `ws rig retire`

```sh
ws rig retire <rig> [--dry-run] [--backup] [--confirm] [--purge]
```

Guarded teardown in four stages (assess → backup/consent → worktree teardown →
archive + unregister):

1. **Assess** — `assess_retire` scans every local branch for: unpushed commits, no-upstream
   tracking refs, dirty working tree, stashes, and detached HEAD commits. Returns `SAFE`,
   `NEEDS_BACKUP`, or `BLOCKED`.
2. **Backup or consent gate** — `SAFE` passes immediately. `NEEDS_BACKUP` requires either
   `--backup` (durably pushes `wip/retire-<date>` branches and/or publishes no-origin repos
   so work actually reaches a remote) or `--confirm` (explicitly accepts the loss). After
   `--backup`, retire RE-ASSESSES; if the repo is still not `SAFE` it refuses again unless
   `--confirm` is also present. `BLOCKED` can only be overridden with `--confirm`.
3. **Worktree teardown** — probe-first: all dirty worktrees are detected BEFORE any clean
   worktree is removed. A rig with mixed dirty/clean worktrees never has its clean worktrees
   removed and then refuses on the dirty ones. Failed teardowns (git errors) also gate the
   clone move — a live worktree must not be orphaned by deleting the clone it points at.
4. **Archive + unregister** — the clone moves to `archive.dir` (soft-archive, reversible).
   Unregister is last, only reached once the move succeeds, so a failed move can never leave
   a rig unregistered-but-on-disk. `--purge` hard-deletes the clone instead of archiving.

| Flag | Effect |
|---|---|
| `--dry-run` | Print the full plan; mutate nothing (default-safe) |
| `--backup` | Snapshot all unbacked work to durable `wip/retire-<date>` branches first |
| `--confirm` | Proceed past the safety gate, explicitly accepting any remaining data loss |
| `--purge` | Hard-delete the clone instead of soft-archiving it (still safety-gated) |

### The guardrail contract

**A repo never loses data without the operator's consent.** That contract is enforced at
every step:

- `assess_retire` is a read-only all-branch scan (not just HEAD). It flags: unpushed commits,
  branches with no upstream tracking ref, repos with no origin, dirty working trees, stash
  entries, and detached HEAD commits.
- `NEEDS_BACKUP` refuses unless the operator uses `--backup` (work reaches a remote) or
  `--confirm` (explicit acceptance). No silent override exists.
- After `--backup`, the orchestrator RE-ASSESSES and refuses to delete unless the repo is
  provably `SAFE`. If `--backup` alone cannot make it safe, `--confirm` accepts the
  remainder.
- Dirty worktrees are discovered before any clean worktree is removed — the probe-first
  design means a partially-clean rig can never end up in a half-torn-down state.
- Failed worktree teardowns prevent the clone from moving or being deleted.
- `--dry-run` previews everything and mutates nothing.
- Soft-archive is the default: the clone moves to `archive.dir` rather than being deleted;
  it is recoverable by moving the directory back.
- `--purge` and `ws rig archive prune` are the only irreversible deletes and both require
  explicit flags or a configured age window.

### `ws rig archive ls`

```sh
ws rig archive ls [--json]
```

Lists every `<provider>/<org>/<repo>` entry under `archive.dir`, sorted oldest-first, with
age (directory mtime) and disk size. Prints a total at the bottom. `--json` emits one
object per repo with typed `age_days` and `size_bytes` fields.

### `ws rig archive prune`

```sh
ws rig archive prune [--older-than N[d]] [--all] [--dry-run]
```

Docker-`system-prune`-style reclamation of the archive graveyard. Removes archived repos
whose age exceeds the threshold and reports bytes reclaimed.

| Flag | Effect |
|---|---|
| `--older-than N[d]` | Remove repos archived more than N days ago (`30` or `30d`); default: `archive.window_days` |
| `--all` | Remove every archived repo regardless of age |
| `--dry-run` | Preview what would be removed and bytes reclaimed; mutate nothing |

Path-escape guard: every candidate path is resolved and confirmed to be strictly inside
`archive.dir` before any deletion — a misconfigured or symlinked `archive.dir` cannot
cause collateral damage outside the graveyard.

### Archive config keys

| Key | Default | Effect |
|---|---|---|
| `archive.dir` | `$GIT_WORKSPACE/.archived` | Root directory for soft-archived clones |
| `archive.window_days` | `30` | Default age threshold for `ws rig archive prune` |

```sh
ws config set archive.dir /mnt/cold/ws-archive
ws config set archive.window_days 60
```

## Rules that bite

- **No `ws work` — the one structural break.** Every other role skill pairs with `work`; you do
  not. The superintendent's verbs are `ws rig` / `ws config` / `ws sync` / `ws labels`. If you
  reach for `ws work assign/claim/merge`, you've stepped into the Coordinator/Developer/Merger
  seat — stop and hand off instead.
- **Provision, don't drive.** You do not schedule beads, write code, plan molecules, or merge.
  Standing up and configuring the rig is the whole job; the work happens in a separate session.
- **The registry is Head Office.** Mutations land in `~/.ws/config.yaml` via the round-trip
  `config.save` path — never hand-edit it; `ws config set/unset` preserves comments and the
  flow-style `managed_repos` block. `ws rig add`/`rm` are registry-only and leave the repo alone.
- **Clone-down is guarded.** `ws rig onboard --clone-url` only clones when the target dir is
  absent; an already-local folder is inited in place. Don't clone over a live checkout.
- **Verify before you hand off.** A coordinator launched against an unconfigured or unhealthy rig
  wastes the whole downstream session — close the loop with `ws doctor` / `ws config get` first.
