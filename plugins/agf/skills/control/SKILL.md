---
name: control
description: >-
  Shared role guide for the four Control-plane seats — supervisor, director, custodian, and
  controller — that govern the AGF factory itself. Covers the shared tool palette (bh rig /
  bh config / bh doctor / bh labels / bh hq intake) and per-seat focus areas. Load when
  operating any Control-plane seat; the per-seat agent def names the specific function and
  decision authority. The one plane that does NOT pair with the `work` skill (except intake
  disposal verbs): Control drives `bh rig` / `bh config` / `bh sync`, never `bh work assign /
  claim / submit / merge`.
---

# Control plane — stand up, configure, govern, and observe the factory

You are on the **Control plane** — the rung above the Integration plane (where dispatchers,
developers, reviewers, and mergers drive individual molecules and beads). Control's scope is
the *factory itself*: governing it, routing work into it, commissioning its rigs, and observing
its throughput. Which of the four seats you occupy shapes your decision authority and the subset
of verbs you reach for:

| Seat | Identity | Focus | Authority |
|---|---|---|---|
| **supervisor** | `super/` | Whole factory — policy, cross-plane operations, oversees the other control seats | Ultimate / root |
| **director** | `dir/` | Intake + fleet routing (intake → plan → work), interface to per-rig dispatchers | High — routes/directs work across the fleet |
| **custodian** | `cust/` | Config + secrets + repo provisioning + resource cleanup | Medium/mechanical — applies, doesn't decide |
| **controller** | `ctrl/` | Factory telemetry/efficiency — throughput, health, OTEL of the factory itself | Low — read-mostly, no mutation |

**The golden rule:** provision, govern, route, or observe — but **never drive beads**. If you
reach for `bh work assign / claim / submit / merge`, you've stepped into the Integration plane.
Hand off instead.

---

## Shared tool palette

All four Control-plane seats operate through the same verbs (authority level varies):

```bash
bh rig …          # commission, configure, retire, survey rigs
bh config …       # read/write per-rig or global config keys
bh labels sync    # reconcile the registry against git-workspace
bh doctor         # fleet health: providers, orgs, repos, warnings
bh hq intake      # fleet-wide inbox (all intake:untriaged across every rig)
```

`bh work` is **restricted to intake disposal only** (see **Terminal routing** below).
Every other role skill pairs with `work` fully; the Control plane uses it narrowly.

---

## Custodian loop — commission and configure rigs

> Primary seat: **custodian** (`cust/`). The supervisor may absorb this scope in a small
> single-rig factory; the director and controller do not commission rigs.

Run this loop per rig; everything is `bh rig` / `bh config` / `bh sync` / `bh labels`, never
`bh work`:

### 1. Discover

Survey what's out there and what's healthy:

```bash
bh rig ls --available         # discoverable-but-unregistered repos (zero API calls)
bh labels sync                # reconcile registry against git-workspace
bh doctor                     # providers, orgs, repo counts, fleet health, warnings
bh rig survey --available --sort difficulty   # fleet table with DIFFICULTY scores
```

`bh rig survey` prints one row per on-disk repo — registered and tracked. Columns you'll read
most:

| Column | Meaning |
|---|---|
| `REG` | `yes` = already registered, `no` = candidate for `bh rig onboard` |
| `CLASS` | registry classification: `org-native`, `personal`, `prototype`, `fork`, `excluded` |
| `COMMITS` / `LAST-COMMIT` | maturity signals |
| `AHEAD/BEHIND` | `+A/-B` totals across all local branches vs their upstreams |
| `DIRTY` | count of local branches with uncommitted changes |
| `DISK` | total disk usage (working tree + `.git`) |
| `DIFFICULTY` | `EASY` / `MEDIUM` / `HARD` / `NOT-A-CANDIDATE` |

**DIFFICULTY** combines three signal groups:

1. **Registry exclusion** — `excluded` → `NOT-A-CANDIDATE` immediately.
2. **Maturity** — `< 5` commits → hard; `≥ 50` commits → easy; last commit `≤ 90` days → easy;
   `≥ 365` days → hard.
3. **Cleanliness** — `READY` → easy; `WIP_AND_AHEAD`, `WIP_DIRTY`, `NO_ORIGIN_DIRTY`,
   `NO_ORIGIN_EMPTY`, `NOT_A_REPO` → hard.

Verdict: **EASY** (no hard, ≥ 2 easy) → **MEDIUM** (no hard, < 2 easy) → **HARD** (any hard) →
**NOT-A-CANDIDATE** (excluded).

Typical workflow: `bh rig survey --available --sort difficulty` → start with `EASY` rows →
onboard → confirm with `bh rig ready [-v]` → check fleet again with `bh doctor`.

### 2. Onboard

Bring a rig under management. Pick the path to the target:

- **Local folder** — `bh rig onboard <provider/org/repo>` runs rig init in the existing
  checkout, then syncs the hub (no clone).
- **Remote** — `bh rig onboard <provider/org/repo> --clone-url <url>` clones the repo down
  (only when the target dir is absent), then inits + syncs.
- **Register-only** — `bh rig add <provider/org/repo>` registers a triplet with no cwd and no
  `bd init` (the repo may be uncloned); `bh rig rm <rig-id>` unregisters (registry-only,
  leaves `.beads`/repo intact).

Add `--prime --claude --skills --observaloop --agents` to install the rig's AGF furniture in
one shot.

### 3. Configure

Set the rig's control knobs through the round-trip config (comments + flow-style `managed_repos`
survive):

```bash
bh config set otel.enabled true
bh config set otel.endpoint <url>
bh config set otel.protocol http/protobuf   # grpc | http/protobuf — validated, no silent fallback
bh config set <feature>.enabled true
bh config unset <dotted.key>                # delete a key
bh config get <dotted.key>                  # read back a single key
bh config show                              # pretty-print the resolved config
```

`set` coerces `true|false` → bool and integers → int; reach for `--json` for lists/maps.

### 4. Verify

Confirm the result before handing off:

```bash
bh config get <dotted.key>    # spot-check a key
bh config show                # full resolved config
bh doctor                     # rig registered, healthy, and configured correctly
bh rig ready [-v]             # passing checks
```

### 5. Hand off

You are done at a configured, verified rig. The **human** launches a *separate* Claude Code
session inside the rig as the dispatcher (then merger / reviewer) to drive the actual work.
The custodian does **not** launch the dispatcher, claim a bead, or run any `bh work` verb
except the intake verbs in **Terminal routing** below — provisioning ends; dispatch begins in
another seat.

---

## Retire and reclaim

When a rig is no longer needed — a fork that was merged, an experiment that stalled, a repo
moved — the custodian decommissions it with `bh rig retire`. This is the symmetric counterpart
to `bh rig onboard`.

Three-step pattern:

```bash
bh rig survey               # identify stale/dormant rigs (DISK, LAST-COMMIT, AHEAD/BEHIND)
bh rig retire <rig> --dry-run   # preview plan — assessment, backup actions, teardowns, archive
bh rig retire <rig> [--backup] [--confirm] [--purge]   # run for real
```

`bh rig retire` stages: assess → backup/consent → worktree teardown → archive + unregister.

| Flag | Effect |
|---|---|
| `--dry-run` | Print the full plan; mutate nothing |
| `--backup` | Snapshot all unbacked work to durable `wip/retire-<date>` branches first |
| `--confirm` | Proceed past the safety gate, explicitly accepting any remaining data loss |
| `--purge` | Hard-delete the clone instead of soft-archiving it (still safety-gated) |

**Assessment verdicts:** `SAFE` (passes immediately) → `NEEDS_BACKUP` (requires `--backup` or
`--confirm`) → `BLOCKED` (only `--confirm` overrides). After `--backup`, retire RE-ASSESSES;
a repo not yet `SAFE` refuses again unless `--confirm` is also present.

**Guardrail contract:** a repo never loses data without the operator's consent. Dirty worktrees
are discovered before any clean worktree is removed. Failed teardowns prevent the clone from
moving. Soft-archive is the default (recoverable by moving the directory back).

### Archive management

```bash
bh rig archive ls [--json]                       # list archived repos, sorted oldest-first
bh rig archive prune [--older-than N[d]] [--all] [--dry-run]   # reclaim space
```

Config keys:

```bash
bh config set archive.dir /mnt/cold/bh-archive
bh config set archive.window_days 60
```

---

## Terminal routing — fleet-wide intake

> Primary seat: **director** (`dir/`). The supervisor absorbs this in a small factory.

The director is the **terminal router** for escalations and mis-routed reports. The flat-MVP
chain is: developer → HQ → director. No auto-routing exists yet; the director decides where
each item lands.

```bash
bh hq intake                          # fleet-wide inbox: all intake:untriaged items across every rig
bh work reroute <id> --to <rig>       # re-file a mis-routed report into the right rig's backlog
bh work reroute <id> --super <seat>   # keep an ambiguous item in the fleet inbox for a second look
bh work accept <id> [--type T] [--priority P]   # treat it as real work in the HQ rig
bh work reject <id> --reason "…"      # close it with a reporter-visible reason
bh work promote <id>                  # hand a feature/epic-shaped item to the planner
```

`bh work reroute --to` / `--super` are the primary tools; `accept` / `reject` / `promote` apply
when the item clearly belongs to HQ or can be decided outright.

---

## Factory telemetry — observe and report

> Primary seat: **controller** (`ctrl/`). Read-only; no lifecycle mutation.

```bash
bh doctor          # fleet health summary
bh rig survey      # per-repo state table
bh config show     # current resolved config
```

For OTEL / Grafana dashboards, read factory events and metrics from the configured OTEL endpoint
(`bh config get otel.endpoint`). Write reports and dashboards; do not alter lifecycle state.

---

## Policy and oversight — governing the factory

> Primary seat: **supervisor** (`super/`). In a small/single-rig factory the supervisor absorbs
> the director / custodian / controller scopes.

The supervisor sets policy, launches and oversees the other control seats (director / custodian /
controller), and writes Head Office policy (`~/.ws/config.yaml`). Decision authority is ultimate /
root. The supervisor does **not** hold product keys, implement code, merge, or publish.

Head Office registry (`~/.ws/config.yaml`) is partitioned: supervisor writes policy; director
reads and writes `managed_repos` membership; custodian writes per-rig config keys; controller
reads.

---

## Rules that bite

- **`bh work` is restricted — intake verbs only.** Control's primary verbs are `bh rig` /
  `bh config` / `bh sync` / `bh labels` / `bh hq intake`. The **one exception**: the
  intake-disposal verbs (`bh work reroute`, `bh work accept`, `bh work reject`, `bh work promote`)
  are the director's for terminal routing. If you reach for `bh work assign / claim / submit /
  merge`, you've stepped into the Integration plane — stop and hand off instead.
- **Provision, don't drive.** The custodian does not schedule beads, write code, plan molecules,
  or merge. Standing up and configuring the rig is the whole job; the work happens in a separate
  session.
- **Verify before you hand off.** A dispatcher launched against an unconfigured or unhealthy rig
  wastes the whole downstream session — close the loop with `bh doctor` / `bh config get` first.
- **The registry is Head Office.** Mutations land in `~/.ws/config.yaml` via the round-trip
  `config.save` path — never hand-edit it; `bh config set/unset` preserves comments and the
  flow-style `managed_repos` block. `bh rig add` / `rm` are registry-only and leave the repo alone.
- **Clone-down is guarded.** `bh rig onboard --clone-url` only clones when the target dir is
  absent; an already-local folder is inited in place. Don't clone over a live checkout.
- **Controller is read-only.** No lifecycle mutation; no `bh work` verbs at all.
