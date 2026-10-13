# Validation broker ADR — a bh-native broker by default; pueue and HyperQueue as optional adapters

**Status:** accepted as the initial direction (operator, 2026-10-13; decision bead `bh-5mdar.6`)
· **Date:** 2026-09-30 · **Decision bead:** `bh-5mdar.6` · **Supersedes:** nothing ·
**Amends:** nothing
**Related:** [validation-broker-proposal.md](validation-broker-proposal.md) (the design record),
[attested-green-adr.md](attested-green-adr.md) (unchanged by this decision), and the seven
bake-off spikes, whose docs live on their branches and not on `main`:
`bh-5mdar.1` (port + replay harness, branch `wt/bead/issue/bh-5mdar.1`),
`bh-5mdar.2` (per-key sizing), `bh-5mdar.3` (availability signals and brake),
`bh-5mdar.4` (HyperQueue prototype), `bh-5mdar.5` (pueue + packer prototype),
`bh-5mdar.9` (fair HyperQueue re-evaluation, which supersedes `bh-5mdar.4`'s verdict) and
`bh-5mdar.10` (group-aware packer against HyperQueue nightly on group latency). Each branch is
`wt/bead/issue/<bead>`.

Records what implements the validation broker, how the decision splits into a contract, an
enforcement layer and a scheduler, which packer policy is on by default, when an external
framework earns an adapter, the requirements every implementation must meet, the validations
still owed, and the amendments this proposes for the downstream molecules of workstream
`bh-alzfb`. The amendments are a **proposal**: no downstream bead is edited until the operator
confirms the verdict, after which `/bh:replan` applies them.

## Context

A full gate on this hive spends about 15 minutes running seven attest keys one after another, and
five uncoordinated validation controls can deadlock (hq-vbha, fixed separately as `bh-cesc7`) or
oversubscribe the host. The design record proposes one host-wide broker that admits a job only
when its reservation vector fits measured availability, behind a shared port, and names two
candidate runners, pueue and HyperQueue (HQ). The bake-off built both as throwaway prototypes and
replayed 24 real gate sets (190 key runs, 25,869 s of recorded key time) through one harness,
plus a synthetic staggered stream of 6 large and 30 small gates built to stress starvation
(`bh-5mdar.10`). The harness also has an **in-memory broker**: the same port and packer code with
no external runner. It started as a yardstick and ended as the strongest candidate.

### The objective: group latency

A gate unblocks only when its **last** key finishes. The scoring objective is therefore **group
latency**: the time from a gate's submit to its last key done, reported as **p50, p90 and MAX**.
MAX is the starvation check: the latency of the gate that was held back longest. Per-key queue
wait is secondary; a short key waiting is harmless if its gate's laggard is still running. All
replay figures are replay seconds at 50× (`--scale 0.02`) on a quiet host unless stated.

### What the evidence settled

- **The port holds.** Six operations (`submit`, `wait_grant`, `report`, `cancel`, `status`,
  `events`) and a typed job model express whole-reservation admission, priority and LPT order,
  nested sub-allocation, orphan reclaim, the brake and fallback (`bh-5mdar.1`, GO).
- **Running keys side by side pays.** stateful@16 + integration@8 + demos finished in 403 s
  against 803 s in sequence (2.0×) with zero failures over 15 key runs (`bh-5mdar.2`, GO).
- **Memory is the binding limit, not CPU.** One stateful@16 lane takes 7.6 GiB of
  non-reclaimable memory (64 % of the 11.9 GiB typically available) while using 13 of 32 cores.
  3.8 GiB of that is the fence's tmpfs scratch (`bh-5mdar.2` E2, E4).
- **The brake can be built on portable signals**, and the page-cache **file-refault rate** is the
  earliest memory signal on Linux. PSI memory barely moved on a host without swap
  (`bh-5mdar.3`, GO).
- **Heuristic packing beats CP-SAT** on every completion measure, at no dependency cost
  (`bh-5mdar.5` E7).
- **The packer, not the runner, sets group latency** (`bh-5mdar.10`, GO), and the runner added
  only overhead (below).

## Decision

**GO — a bh-native broker by default, on every platform.**

The broker is the group-aware packer plus a **bh-owned durable lease store** (SQLite, or
lock-guarded files), running as a supervised bh service from the library workspace package
`packages/beadhive-validation-broker`. It is `bh-5mdar.10`'s in-memory control made durable. No
external runner is installed by default. **pueue and HyperQueue become optional adapters behind
the same port**, added only when an [adoption trigger](#adoption-triggers) fires. If the broker
is missing or down, clients fall back to today's file-lock host slots with a loud warning.

### Why native

1. **The native broker was the best scheduler measured.** `bh-5mdar.10` E2, means of 2 reps:

   | Scenario | Implementation | Group p50 | p90 | **max** |
   |---|---|---|---|---|
   | burst | HQ nightly | 6,710 | 10,961 | 11,872 |
   | burst | pueue + group | 6,769 | 10,519 | 11,523 |
   | burst | **native (in-memory group)** | **5,857** | **9,515** | **10,469** |
   | solo | HQ nightly | 552 | 876 | 974 |
   | solo | pueue + group | 586 | 774 | 1,130 |
   | solo | **native (in-memory group)** | **485** | **574** | **972** |
   | stagger | HQ nightly | 1,211 | 2,287 | 2,424 |
   | stagger | pueue + group | **470** | 2,214 | 2,752 |
   | stagger | **native (in-memory group)** | 808 | **1,977** | **2,242** |

   It beat both frameworks on p90 and MAX in every scenario (solo MAX ties HQ at the stateful
   key's 972 s floor) and on p50 in burst and solo. Stagger p50 is the exception, and stagger
   medians swing 30–80 % between reps, so that cell is not significant
   ([Variance](#variance-and-model-limits)).
2. **The frameworks only added overhead.** With the same packer, pueue ties HQ nightly on burst
   p50 within about 1 % (6,769 against 6,710; reps 6,711/6,827 against 6,807/6,613). Moving the
   same policy from memory onto pueue cost +16 % burst p50 and +23 % stagger MAX, the pueue CLI
   tax (about 265 ms per call against HQ's 115 ms, `bh-5mdar.10` E2.3).
3. **Crash correctness came from bh's own lease files, for both frameworks.** After a SIGKILL, HQ
   restored still-running tasks as `waiting` (re-queued; they had to be cancelled) and pueue
   marked still-running tasks `Killed`. Both reached 0 over-admission, 0 fallback and 0 lost jobs
   only through a bh-owned lease directory and reconciliation (`bh-5mdar.9` E2, `bh-5mdar.5`
   E2.3, E6). The durable state that matters is already bh's to write.
4. **In lease mode the framework never runs the test.** The bh key runner stays the process
   parent, so stdout, stderr, exit codes, fallback and Attested Green are unchanged. That leaves a
   framework's core value, running and supervising the process, unused. pueue would hold
   placeholder holder tasks, a pattern that was never built or measured (`bh-5mdar.5` E2.6).
5. **Neither framework enforces resources.** HQ's resources are logical; pueue has none. bh still
   builds the resource model, the packer, the brake, nested sub-allocation and orphan reclaim for
   either backend (`bh-5mdar.4` recommendation 3, `bh-5mdar.5` E3).
6. **Supply-chain, pinning and upgrade cost.** An external runner is another pinned binary, a
   setup-managed install and license check, another supervised daemon, and a parse layer over
   output its authors call unstable (HQ) or broke in a major (pueue v4). HQ has **no usable tagged
   release**: every 0.26.x panics within 2 s on this workload (#1121), and #1135 is live on nightly
   and `main` (`bh-5mdar.9` E1, E6).
7. **pueue writes the caller's environment to disk.** It snapshots the submitting shell's whole
   environment into every task and persists it in `state.json`; an agent's API tokens landed
   there in a probe (`bh-5mdar.5` E2.4).
8. **Portability is unverified either way.** pueue claims macOS and Windows but only Linux ran;
   HQ ships Linux only. The native broker is stdlib plus psutil (BSD-3-Clause), which is the
   smallest portability surface of the three.

## Framing: three separable layers

The decision follows the split Borg made between a declarative job contract, kernel-level
isolation, and a scheduler. Each layer can change without the others.

### 1. The declarative contract is bh's

The port and job model (`bh-5mdar.1`'s `broker_port.py`) are what every hive and every key
conforms to: a **reservation vector** (`cpu_min`, `cpu_max`, `memory_mb`, named pools), a
**priority class**, a **group** (the gate it unblocks), an **expected duration** from history,
an optional **parent lease**, and the **grant env** (`BH_VALIDATION_LEASE`, `BH_GRANT_CPUS`,
`BH_GRANT_MEMORY_MB`, `BH_GRANT_<POOL>`). The fields are chosen to map one-to-one onto the two
most likely future backends, so adopting one is a mechanical adapter, not a redesign:

| bh field | HyperQueue | Kubernetes (Jobs + Kueue) |
|---|---|---|
| `cpu_min` / `cpu_max` | resource variants (`cpus` at max, then at min) | `requests.cpu` / `limits.cpu` |
| `memory_mb` (incl. tmpfs scratch) | a sum resource for memory | `requests.memory` = `limits.memory` |
| named pools (`dolt-servers`) | counted or indexed resources | extended resources, or Kueue flavours/quotas |
| priority class | `--priority` | `PriorityClass` / Kueue `WorkloadPriorityClass` |
| group | a job, with a group-rank priority | one Kueue workload (gang admission) |
| expected duration | not modelled (`--time-request` is a filter) | not modelled |
| parent lease | sub-allocation inside the task | sub-allocation inside the pod |
| grant env | task env from the allocated resources | downward API / env from requests |

Keep new fields inside that intersection unless there is a reason not to.

### 2. Enforcement is delegated to the kernel

The broker admits; the kernel bounds. Where available, each admitted job runs in a
`systemd-run --user --scope` transient scope under a validation slice with cgroup v2 CPU, memory
and IO weights and caps; elsewhere it runs under `nice`/`ionice`. `bh-5mdar.2` already used these
scopes as its ground truth for memory and CPU, `bh-o8769` plans the cgroup adapter, and
`bh-69j3d`'s stopgap config surface (nice level, ionice class, scope weights, emergency override)
is reused. This is the **bounded resource context**: a job cannot take more than it was granted,
and the slice cap holds with the broker stopped. Neither pueue nor HyperQueue provides it.
Enforcement stays best-effort per platform (design record requirement 5): a missing mechanism
degrades to plain `nice` and is reported by `bh doctor`, never a failed gate.

### 3. The single-host scheduler stays thin and bh-owned

About nine job types on one 32-core host is a small scheduling problem. That is why a few hundred
lines of policy (the harness's in-memory broker is about 275 lines; the packer pick p99 is at
most 1.5 ms, `bh-5mdar.10` E2.3) matched and then beat HQ's MILP scheduler. The scheduler owns
admission, ordering, grants and the brake, and nothing else.

### Long-lived versus run-and-done

- **Long-lived services** (databases, caches, warm workers) need placement, restart and held
  reservations. Admission latency barely matters; they start once.
- **Our gates are run-and-done batch**: bursty, group-structured, and short. That is a queueing
  problem where group latency is the objective, and it is where general frameworks fit worst:
  HQ's head-of-line behaviour held short keys 302 s behind blocked long ones (`bh-5mdar.9` E3).
- **One Borg-shaped long-lived resource exists already:** the Dolt server pool that keys borrow
  (`dolt-servers`). Today it is just a counted pool. If it grows into warm worktrees or
  long-lived fixtures that hold reservations while batch keys borrow around them, that is
  adoption trigger 4.

## Packer: the defaults that earned their keep

`packer=group` (`bh-5mdar.10`), with each knob named and tunable:

| Knob | Default | Why |
|---|---|---|
| laggard first | **on** | Within a group, longest expected key first; the laggard is the gate. |
| `laggard_flex` | **1 (on)** | Spare CPU to each group's longest remaining key, up to `cpu_max`, ahead of the headroom rule. The largest single gain: without it, burst lands near greedy (6,627 / 10,925 / 12,058, E1). Define the laggard per group as its longest key overall. |
| generalised EASY | **on** | Every admitted group's first blocked key holds a reservation; a backfill starts only if it delays none of them. It refused 1,355–2,193 harmful backfills per burst (E3). |
| priority classes | **strict** | Critical path > check/submit > merge/land > background; never mixed. |
| `headroom_cpu` | **8** | CPUs held back from flexible grants for keys other than a laggard. |
| group-major admission | **off** | Enable only when measured queue depth shows contention. It reorders waiting groups by `eff = queued work − aging × time waited`. |
| `aging` | **off** (4 when enabled) | Needed only with group-major admission. At 4 a waiting large group outranks new small ones after about 180–360 s; at 0 (pure SRPT) a large gate starved to 3,318 s (E1, E4). |

With group-major admission off, groups are admitted in arrival order within a class. The closest
measured configuration is `sticky_head=1` (arrival-order admission with flex and EASY): stagger
1,124 / 2,045 / 2,184 against 762 / 1,940 / 2,159 with `aging=4` (E1). So turning the knob on buys
about a third on stagger p50 and almost nothing on MAX, which is why it waits for evidence of
queue depth. `packer=greedy` stays selectable for one release as a fallback. Estimates come from
recorded durations padded by the measured profiles; an overrunning key is treated as ending now.

### Effect sizes

| Change | Effect on group latency | Evidence |
|---|---|---|
| Today's sequential gate → any broker | **≈ 2×** | Solo p50 ≈ 1,075 s (today's slots) → ≈ 545 s (greedy) in replay (`bh-5mdar.1` E5); real keys 803 s → 403 s (`bh-5mdar.2` E4) |
| Greedy → group-aware packer | **≈ 10–18 %** | In memory: burst p50 / p90 / max −18 / −17 / −15 %, solo p90 −11 %, stagger p90 / max −13 / −7 % (`bh-5mdar.10` E2) |
| Framework choice (HQ nightly vs pueue, same packer) | **≈ 0** | Burst p50 within ≈ 1 % (`bh-5mdar.10` E2) |

### Variance and model limits

- **Variance.** Every matrix cell is 2 reps. Burst is stable within ±2–5 % (for example HQ
  6,807/6,613, native group 5,710/6,003). Stagger medians swing 30–80 % between reps (pueue +
  group 337/603, HQ 1,039/1,383), so stagger p50 differences are not significant; stagger p90
  and MAX are steadier. One solo MAX outlier: pueue + greedy 1,991 against 1,040 in the other rep.
- **Model limits.** The replay runs `sleep`s, not tests. CPU contention is a formula (α 0.5
  slowdown below `cpu_max`), and memory contention is **not modelled**: reservations are
  admitted against a budget, but nothing slows or thrashes when memory runs short. The in-memory
  broker also pays no durable-store cost. These are why the owed validations below exist.

## Adoption triggers

Any **one** trigger justifies adding a framework adapter behind the port:

| # | Trigger | Why native stops being enough | Candidates |
|---|---|---|---|
| 1 | More than one host (remote workers, cloud burst, validation farm) | Placement across machines, worker discovery, host failure | HyperQueue (multi-node, MIT); Kubernetes Jobs + Kueue (queueing, quotas, gang admission; Apache-2.0); a Bazel Remote Execution backend if keys become cacheable actions |
| 2 | Heterogeneous capacity (macOS runners, GPUs, large-memory boxes) | Constraint-based placement across pools | as in #1 |
| 3 | Untrusted or multi-tenant workloads | Hard per-tenant isolation and quotas, not just admission | containers or systemd units with enforced limits; a cluster scheduler if also multi-host |
| 4 | Long-lived and batch work sharing capacity | One arbiter must see both reserved services and borrowers | a service + batch scheduler |
| 5 | Scale: hundreds of concurrent jobs, many hives, scheduler HA, preemption | Scheduler throughput and correctness become a product in themselves | HyperQueue or a cluster scheduler |
| 6 | Humans or other systems need the job ledger (UI, audit, integration) | A maintained queue with CLI, API and dashboards beats building one | pueue, HyperQueue, Kubernetes |

- **Today none is met:** one host, trusted code, tens of concurrent keys, and bh owns the job
  ledger through Beads and the validation run records.
- **The most likely first trigger is #1**, and HyperQueue is then the natural first adapter:
  multi-node, one static binary, native multi-resource requests.
- **Permissive licenses only** (MIT, Apache-2.0, BSD) still applies to any adapter. Some
  well-known schedulers are not permissive and drop out: Nomad is BUSL 1.1, Slurm GPL-2.0, Flux
  LGPL-3.0 (design record).

### The bar for a HyperQueue adapter

When trigger 1 or 5 fires, the HQ adapter becomes selectable for production only when **all** of
these hold (`bh-5mdar.9` condition for GO, item 4 revised by `bh-5mdar.10`):

1. The first tagged HyperQueue release that contains **PR #1123** (the #1121 `solver.rs:222`
   fix) **and a fix for #1135** (PR #1136 or its successor), pinned to that exact version and
   binary hash.
2. The bh conformance replay passes on that pinned build, on `bh-5mdar.2`'s measured profiles,
   including the HQ build probes ([Conformance kit](#conformance-kit)), and is re-run on every
   HQ upgrade.
3. **Topology:** one worker holding each host's budget, resized by a ballast task. Never
   differently resourced worker slices on one host (the #1135 trigger, and a fragmentation
   problem).
4. **Group-latency parity:** on the kit's burst, solo and stagger replays, HQ's group-latency
   p90 and MAX match the native broker's within the kit's tolerance.

Nightly or a pinned `main` build is acceptable for development and for running the kit only.

## Rejected options

| Option | Reason |
|---|---|
| **pueue + bh packer as the default** | Same packer, only overhead: +16 % burst p50 and +23 % stagger MAX over the native control from the ≈ 265 ms CLI; lease mode leaves pueue running holder tasks that were never measured; crash correctness still needs bh's lease files; it persists the caller's environment (tokens included) in `state.json`; one more pinned binary and daemon. Kept as an optional adapter for trigger 6. |
| **HyperQueue as the default** (any platform) | No tagged release runs this workload (#1121 in every 0.26.x, #1135 on `main`); Linux-only binaries; two daemons plus a holder per lease and about 490 HQ-specific glue lines; on group latency the native broker beats it. Kept as the likely first adapter for triggers 1 and 5, under [its bar](#the-bar-for-a-hyperqueue-adapter). |
| **HyperQueue v0.25.1** (the only release that runs) | Pre-MILP scheduler: a blocked high-priority task does not hold lower-priority work, so a ballast cannot shrink capacity. It also carries the #1135 indexing bug on the reporter's shape. |
| **HyperQueue nightly or pinned `main` in production** | Unreleased, unpinned by any tag, and #1135 is live on both. Development and the kit only. |
| **Both frameworks co-equal** | Two supported runners for no group-latency gain on one host. |
| **Neither (keep today's slots)** | Today's two FIFO slots over-admitted `dolt-servers` 18 times in the burst replay, deadlock on nested requests (hq-vbha), and run gates about 2× slower than side by side. |
| **`packer=greedy` as the default** | 10–18 % worse group latency than `packer=group` in memory. Selectable for one release as a fallback only. |
| **Pure SRPT group order (`aging=0`)** | Best stagger p50, but starves: a large gate finished last of 36, at 1.4× the worst gate under arrival order (`bh-5mdar.10` E4). |
| **OR-Tools CP-SAT packer** | Lost to greedy + EASY on every completion measure; the plan model hit its 100 ms limit on 359 of 379 decisions and held the admission lock long enough to delay an orphan reclaim; about 180 MB of wheels (`bh-5mdar.5` E7). Revisit only off the admission lock and only if it beats `packer=group` on the kit first. |
| **Resize-by-slices on HyperQueue** | The `bh-hetero` shape kills the nightly and `main` server within 3 s (#1135), and slices fragment capacity (`bh-5mdar.4` E7). |
| Dask, task-spooler, Slurm, Flux, Nomad, Ray, Snakemake, Celery-class queues, CI runners | Screened out in the design record (license, platform, runtime weight, no resource packing, or loss of warm state). Nothing in the bake-off reopened them; Kubernetes + Kueue joins the candidate list only under triggers 1–3. |

## Licenses

The default depends only on bh code and psutil. Every candidate adapter is permissive (design
record requirement 8):

| Component | License | Source of the confirmation |
|---|---|---|
| psutil | BSD-3-Clause | `bh-5mdar.5` E1 |
| HyperQueue (optional adapter) | **MIT** | `LICENSE` ("Copyright (c) 2021-present, Ada Böhm, Jakub Beranek"); GitHub SPDX `MIT` (`bh-5mdar.4` E1) |
| pueue (optional adapter) | **MIT OR Apache-2.0** | `Cargo.toml` at v4.0.4 (`license = "MIT OR Apache-2.0"`); repo ships `LICENSE.MIT` and `LICENSE.APACHE`. GitHub's API reports only Apache-2.0 because it detects one file (`bh-5mdar.5` E1) |
| Kubernetes, Kueue (trigger candidates) | Apache-2.0 | Project licenses; not evaluated in this bake-off |
| OR-Tools | Apache-2.0 | Not needed: CP-SAT is rejected |

An adapter's setup-managed license check must accept a dual `MIT OR Apache-2.0` expression; a
check keyed on GitHub's single detected license would misreport pueue.

## Platform coverage

- **Linux x86_64: verified** on this host (32 vCPU, 47 GiB, no swap, tmpfs `/tmp`, cgroup v2 with
  user delegation, PSI), for the native broker's harness and both framework prototypes.
- **macOS: UNVERIFIED.** The native broker needs only Python and psutil; the macOS
  memory-pressure signal (`kern.memorystatus_vm_pressure_level`) is documented but unmeasured;
  enforcement falls back to `nice` (no cgroups). A verification pass is owed before macOS is
  claimed. pueue claims parity; HQ has no macOS binary.
- **Windows: UNVERIFIED.** psutil's Windows `swap sin/sout` are always 0, and the key runner's
  POSIX assumptions have not been checked there.

## Requirements for the broker and any adapter

These come from the spikes. They belong in the broker core and the conformance kit, so an adapter
inherits them rather than reimplementing them.

### Leases, restart and outage

- **bh-owned durable leases.** Grant, owner (pid + start token), and exit code per lease live in
  the broker's store and are reconciled on start. With an adapter, the framework's view of
  "running at crash" is never trusted (Why native, point 3).
- **Reconcile on restart.** Re-count orphans that are still alive, cancel restored work whose
  client has moved on, and never re-run a task that was running at the crash.
- **Client grace for `wait_grant`.** `submit`, `wait_grant` and `report` retry
  `BrokerUnavailable` with backoff for a bounded grace (30 s real in the replays) before the client
  falls back. Without it, a 600 s outage pushed 108–135 jobs onto fallback slots and the broker
  over-admitted `dolt-servers` afterwards. A durable queue and the grace are both needed.
- **Fallback runs register with the broker.** Named pools such as `dolt-servers` are invisible to
  any sensor, so a fallback run the broker cannot see causes over-admission after recovery.
- **Lease mode.** The key runner stays the process parent. Nested requests carry
  `BH_VALIDATION_LEASE`, draw only from the parent's grant, and are never braked.

### Brake, enforced bh-side

The brake lives in the broker's admission loop. Neither pueue nor HyperQueue has a
priority-aware admission pause, so an adapter would still use bh's brake: stop force-starting
(pueue) or park/unpark queued jobs (HQ). Thresholds are `bh-5mdar.3` E3's defaults,
expressed as `max(absolute, fraction of RAM)` so they carry to other hosts, and tunable by the host
seat within supervisor bounds:

| Level | Enter when (any) | Action |
|---|---|---|
| AMBER | available < max(4 GiB, 8.5 % RAM); **file refaults ≥ 5,000/s**; PSI memory `some` avg10 ≥ 10 %; PSI cpu `some` ≥ 25 % (30 s); PSI io `some` ≥ 30 % (30 s) | Admit critical class only, at `cpu_min`. Pause background admission. |
| RED | available < max(2.5 GiB, 5 % RAM); **file refaults ≥ 50,000/s**; PSI memory `full` ≥ 10 %; PSI cpu `some` ≥ 50 % (30 s); PSI io `full` ≥ 20 % (30 s) | Stop all admission. SIGSTOP background work only. |
| KILL | available < max(1 GiB, 2 % RAM) for 10 s; PSI memory `full` ≥ 25 % for 10 s; any `oom_kill` increase | Kill the lowest-priority, youngest job whose memory covers the deficit; record **exit 75** (retryable), never a red verdict. One kill per 10 s. |

- **Refault rate is the early memory signal.** It crossed its threshold earlier and more sharply
  than anything else as the host approached thrash; psutil alone left the 16/8 edge at AMBER.
  Missing signals drop their rule, never the level.
- **Hysteresis:** escalate after 6 s (memory), 30 s (CPU/IO) or 10 s (KILL); de-escalate one level
  at a time after a 60 s clear with a 1–1.5 GiB memory margin and half-threshold rates.
- **CPU and IO thresholds stay marked "extrapolated"** until a host shows CPU thrash; none did.
- **SIGSTOP only background work.** Foreground keys run under the 900 s watchdog, and a paused
  suite keeps aging.
- **Kill with TERM first**, wait for the fence (about 1 s), then KILL, then delete the job's
  `bh-hermetic-*` scratch: after a SIGKILL, stateful's 3.8 GiB of tmpfs scratch stays pinned.
- **No start race.** The native broker decides and grants in one step under its own lock, so
  it admitted 0 jobs inside RED. pueue admitted 1 per burst because a `pueue start` was already
  in flight (`bh-5mdar.10` E2.5); a pueue adapter must re-check the brake after the start
  returns and write the grant only after that.

### Default reservations

From `key-profiles.measured.json` (artifact on branch `wt/bead/issue/bh-5mdar.2`)
(memory is cgroup anon+shmem at `cpu_max` with about 10 % headroom, **including tmpfs scratch**;
`cpu_max` is the measured knee, not the core count):

| Key | cpu min | cpu max | memory at max | `dolt-servers` | Expected wall |
|---|---|---|---|---|---|
| stateful | 8 | 16 | 8.5 GiB (≈ 4.4 GiB + 0.2 GiB/worker) | 2 | 332 s |
| integration | 4 | 8 | 2.5 GiB | 4 | 357 s |
| demos | 1 | 2 | 0.5 GiB | 0 | 161 s |
| root-composition | 4 | 8 | 1.0 GiB | 0 | 51 s |
| bd-cli | 8 | 16 | 2.2 GiB (≈ 0.1 GiB/worker) | 10 (one per worker) | 33 s |
| packages | 2 | 8 | 0.5 GiB | 0 | 23 s |
| architecture-contracts | 1 | 1 | 0.25 GiB | 0 | 102 s |
| docs | 1 | 2 | 0.25 GiB | 0 | 8 s |
| unit | 1 | 1 | 0.25 GiB | 0 | 2 s |

- **Admit against `min(budget, available − AMBER floor)`**, not RAM and not a static budget.
  Idle availability on this host ranged 6.4–12.9 GiB as other agents' tmpfs moved 4.3 GiB.
- **stateful's tmpfs scratch counts as memory.** Better, point the fence's `TMPDIR` scratch at a
  disk-backed directory where `/tmp` is a tmpfs; that frees 3.8 GiB per stateful lane.
- **stateful `cpu_min` 8, or scale the watchdog with the grant.** At 4 workers stateful took
  1,042 s, past the 900 s watchdog. Either keep the floor at 8 or export
  `BH_TEST_TIMEOUT_SECONDS` from the grant (expected CPU-seconds ÷ granted cores × 2).
- The design record's `dolt-servers: 8` for stateful is replaced by 2 (at most 2 seen); bd-cli
  needs a pool slot per worker.
- Expected durations come from each key's history; the table is the seed.
- The engine config key landed as `work.validation_broker.engine` (plus `budget`, `floor`,
  `packer`, `repoll_seconds`), not `work.validation.broker.engine`, because `work.validation` is
  already a string field (`bh-qrixp`).
- The grant never enters `cmd_hash` (Attested Green unchanged). stateful passed at 4, 8 and 16
  workers.

### Shared checkout and environment

- **Run `validation-evidence-refresh` before stateful.** stateful reads per-tree test-closure
  evidence that only the `architecture-contracts` key writes. Side by side, a fresh tree fails 56
  tests (`bh-5mdar.2` E5; the same race is `bh-cbz4j`). The broker's per-tree setup job (or
  `stateful-native` depending on the refresh) removes the hidden ordering the "no DAG" assumption
  missed. One setup job per tree also runs `uv sync` for every workspace package so no key's
  `uv sync --inexact` changes the shared `.venv` under a reader.
- **`architecture-contracts` needs a managed-hive checkout.** A shared scratch view must resolve
  through `registry.current_hive`, inside `$GIT_WORKSPACE` or the bh shadow root.
- **Grants through the lease, never through a runner's environment.** The native broker hands
  the grant to the client through its lease; the key starts from a clean, allow-listed
  environment. For adapters: pueue snapshots the caller's
  whole environment into every task and persists it in `state.json` (an agent's API tokens landed
  there in a probe), and freezes it at `add`. Call pueue with a minimal allow-listed env (`PATH`,
  `HOME`, `PUEUE_CONFIG_PATH`) and deliver the grant through a per-lease env file written just
  before start. HQ tasks inherit the worker's environment, so start `hq worker` with a scrubbed
  one. A clean environment matters on its own: a leaked `VIRTUAL_ENV` broke three
  keys in the sizing campaign.
- **Usage measurement:** read the cgroup scope's `cpu.stat` and `memory.peak` where the
  kernel enforcement layer exists; otherwise psutil PSS plus scratch-filesystem growth, with CPU
  labelled a lower bound. Never `os.wait4` rusage: it reads about 0 through the bubblewrap fence.

### Conformance kit

The native broker and every adapter pass the same kit, seeded from `bh-5mdar.1`'s harness and
ledger and run on `bh-5mdar.2`'s measured profiles, not the assumed ones:

- **Group-latency bounds** — the primary scheduling check. On the **burst** (24 gates at once,
  brake RED 600–900 s, two killed clients), **solo** (non-overlapping gates) and **stagger**
  (`bh-5mdar.10`'s 6 large + 30 small gates over 2,640 s, shipped as a fixture) replays, group
  latency **p90 and MAX** must stay within a stated tolerance of the reference: the native
  broker's own recorded run for regressions, and the native broker on the same host and scale
  for an adapter. The tolerance comes from the reference's rep-to-rep spread plus the adapter's
  measured per-call cost, and is recorded with the kit. Per-key wait is reported, not asserted.
- **Starvation bound** — on stagger, no gate exceeds the arrival-order reference
  (`sticky_head=1`) plus tolerance. With group-major admission and `aging=0` the check must fail,
  which proves it bites.
- **Safety on every replay** — zero over-admission on the backend-independent ledger, zero lost
  jobs.
- **crash** — broker (or adapter daemon) SIGKILLed mid-replay and restarted: zero lost, zero
  fallback within grace, zero over-admission after reconcile.
- **brake** — no grant inside a RED window, **including a start already in flight** (the pueue
  race above).
- **nested** — a request under `BH_VALIDATION_LEASE` is served from the parent and never
  host-queued; `GrantRefused` rather than hold-and-wait when it can never fit.
- **killed client** — a dead owner's lease and every nested lease under it are reclaimed.
- **HQ build probes** (HQ adapter only) — `bh-5mdar.9`'s
  `hq_build_probes.py` (artifact on `wt/bead/issue/bh-5mdar.9`) shapes: the #1121
  reduction, the ballast check (C held behind B), the #1135 `issue` trigger, and `bh-single` /
  `bh-homog` / `bh-hetero`, run against the pinned binary hash in CI.

## Owed validations

These are the broker core's **first tasks**, not blockers on this decision. Each can move a
default knob; none is expected to reopen the native-versus-framework choice.

1. **Real execution at 1×.** Run real gates side by side under the native broker against
   today's sequential run, and watch available memory and the page-cache refault rate against
   `bh-5mdar.3`'s brake thresholds. This closes the replay's missing memory model and confirms
   the ≈ 2× from `bh-5mdar.2` holds under the packer.
2. **Failure-aware replay.** Cancel a group's remaining keys when its first key goes red: 98 of
   the 186 recorded gate sets had a red key (`bh-5mdar.1` E1), so group latency to the first red
   may matter as much as to the last green.
3. **Request-dedup replay.** Merge identical (tree, key) requests across groups, so two lanes
   validating the same tree run each key once (relates to `bh-7s67j.4`).
4. **Estimate-error sensitivity.** Perturb expected durations and measure how laggard ordering
   and the backfill reservations degrade; set the padding from the result.
5. **The default knob set as its own run.** Laggard first + flex + generalised EASY with
   group-major admission off was measured only through its closest proxy (`sticky_head=1`).
   Replay it directly on burst, solo and stagger before shipping the defaults.

## Consequences

1. **bh owns the whole single-host broker.** Packer, lease store, reconciliation, brake, nested
   pools and outage retry are bh code; they were bh code in both framework prototypes too (about
   670 SLOC with pueue, `bh-5mdar.5` E8). No external daemon, binary pin or runner license check
   ships by default.
2. **The lease-mode caveat is resolved.** Natively, the key runner is the parent and the lease
   store is the queue, so no holder tasks are needed. The caveat returns only with a pueue
   adapter.
3. **Gate latency.** At 1×, keys run side by side as memory allows; the replay predicts about 2×
   against today's sequential gate and a further 10–18 % from the group packer. The 2× gain on
   this host depends on ambient memory until stateful's scratch moves to disk (owed validation 1).
4. **Enforcement becomes a first-class layer.** The cgroup/`systemd-run` scope work in
   `bh-o8769` is how a job is bounded, not an afterthought; it is still optional per platform.
5. **Adopting a framework later is an adapter, not a redesign**, because the contract maps onto
   HQ and Kubernetes requests field by field. The cost is re-running the kit.
6. **Nothing changes for Attested Green.** Keys stay opaque, verdicts stay keyed on (tree,
   `cmd_hash`), and the grant stays out of `cmd_hash`.

## Downstream amendments (proposed)

Not applied. After the operator confirms the verdict, `/bh:replan` amends each molecule; the
`blocks` edges from `bh-5mdar.6` are released by the lifecycle when this bead closes.

### `bh-fmuqq` — broker core (builds the native broker)

Operator decisions already on record: the broker is the library workspace package
`packages/beadhive-validation-broker` (package-class ADR, `bh-qo63b`); its Pants BUILD targets
and `attest:<key>` tags are added but **disabled** (`bh-ahm6x` keeps the Pants question open);
the admission seam is its own bead, `bh-upbfl`, which blocks `bh-fmuqq.4`.

| Issue | Action | Amendment |
|---|---|---|
| `bh-fmuqq.1` scaffold package + port | **Keep, amend** | Port and job model from `bh-5mdar.1`'s `broker_port.py`, fields kept inside the HQ / Kubernetes mapping; add durable lease types, `reconcile(leases)`, `wait_grant` grace semantics and the lease-mode contract. Dedicated vs shared attest key stays open as noted. |
| `bh-fmuqq.2` service + state store | **Keep, amend** | The native broker: a supervised bh service whose state store is the durable lease store (SQLite or lock-guarded files: grant, owner, rc per lease; queued jobs) with reconcile-on-restart. |
| (new) group-aware packer | **Add** (or fold into `bh-fmuqq.2`) | `packer=group` with laggard first, `laggard_flex=1`, generalised EASY, strict classes and `headroom_cpu=8` on; group-major admission and `aging` (4 when enabled) off; `packer=greedy` selectable for one release; memoised pick; padded estimates; overruns end now. |
| `bh-fmuqq.3` conformance kit | **Keep, amend** | As in [Conformance kit](#conformance-kit): group-latency p90/max bounds on burst/solo/stagger (stagger stream as a fixture), a starvation bound, safety, crash, brake start-race, nested and killed-client checks; HQ probes as an adapter-only suite. |
| `bh-fmuqq.4` route admission through the broker | **Keep, amend** | Swap in via the `bh-upbfl` port; fallback runs register with the broker; clean allow-listed grant environment. |
| (new) 1× real-execution check | **Add** | Owed validation 1. |
| (new) failure-aware and dedup replays | **Add** | Owed validations 2 and 3. |
| (new) estimate sensitivity + default-knob run | **Add** | Owed validations 4 and 5. |
| (new) per-tree setup job | **Add** (or fold into `bh-7s67j.3`) | One setup job per tree before any key: `uv sync` for all workspace packages and `validation-evidence-refresh`. Relates `bh-cbz4j`. |

### `bh-rpnfx` — becomes optional backend adapters behind the port

| Issue | Action | Amendment |
|---|---|---|
| epic | **Retitle, re-scope** | "Optional backend adapters behind the broker port", deferred behind the [adoption triggers](#adoption-triggers). Drop "pueue as default". |
| `bh-rpnfx.2` pueue + packer adapter | **Defer** | Only on trigger 6. If built: lease mode with holder tasks (measure them), grant written after a post-start brake re-check, minimal env to `pueue add`, batched `add`/`start`, completion from the lease store. |
| `bh-rpnfx.1` HyperQueue adapter | **Defer** | Only on trigger 1 or 5, and selectable only under [its bar](#the-bar-for-a-hyperqueue-adapter). One worker + ballast per host, lease mode, scrubbed worker env. |
| (new) HQ release-condition gate | **Add, keep** | Opens when a tagged HQ release contains PR #1123 and a #1135 fix and the probes + replay pass on its pinned hash. Blocks `bh-rpnfx.1`. Carries [Watch upstream](#watch-upstream). |
| `bh-rpnfx.3` setup install + license check | **Defer with the adapters** | Needed only once an adapter ships; accept `MIT OR Apache-2.0` expressions. |
| `bh-rpnfx.4` per-host selection | **Keep, amend** | Native broker by default everywhere, falling back to host slots; an adapter is selected only by host config once built. `bh doctor` reports the selection, platform verification status and enforcement support. |
| (new) macOS verification pass | **Add** | Run the kit, the sensor and the `nice` fallback on macOS; flip "UNVERIFIED" only on evidence. |

### `bh-7s67j` — gate integration (backend-neutral)

| Issue | Action | Amendment |
|---|---|---|
| `bh-7s67j.1` declare resources on keys | **Keep, amend** | Seed this hive's catalog from `key-profiles.measured.json`; memory includes tmpfs scratch; stateful `dolt-servers` 2, bd-cli one per worker; `memory_per_worker` scaling. |
| `bh-7s67j.2` export grant via runner profiles | **Keep, amend** | Export from a clean, allow-listed environment; floor stateful at `cpu_min` 8 or export `BH_TEST_TIMEOUT_SECONDS` scaled with the grant. |
| `bh-7s67j.3` run keys side by side | **Keep, amend** | Ordered by the core's `packer=group`; run the per-tree setup job first (`bh-cbz4j`); one shared checkout inside a managed-hive path. |
| `bh-7s67j.4` share per-key results across lanes | **Keep** | Informed by the dedup replay (owed validation 3). |
| (new) disk-backed fence scratch | **Add** | Point the fence's `TMPDIR` scratch at disk where `/tmp` is a tmpfs, freeing 3.8 GiB per stateful lane (`bh-5mdar.2` recommendation 3). |

### `bh-6a76l` — signals and controls (backend-neutral)

| Issue | Action | Amendment |
|---|---|---|
| `bh-6a76l.1` availability sensor | **Keep, amend** | 1 Hz psutil core; Linux adds `/proc/vmstat` refault/majfault/`oom_kill` deltas and PSI; macOS pressure level (unverified); present/absent/disabled per signal; admission budget `available − AMBER floor`. |
| `bh-6a76l.3` brake levels | **Keep, amend** | `bh-5mdar.3` E3 thresholds and hysteresis as defaults; refault rate as the early memory signal; SIGSTOP background only; TERM → wait → KILL → scratch cleanup; exit 75. |
| `bh-6a76l.2` stall/orphan/starvation | **Keep** | Orphan reclaim includes nested leases; reconcile after restart feeds it. |
| `bh-6a76l.4` CLI/API/MCP controls | **Keep** | Unchanged. |

### `bh-gxpna` — host validation seat (backend-neutral)

| Issue | Action | Amendment |
|---|---|---|
| `bh-gxpna.1` seat and bounds | **Keep, amend** | Bounds include the brake thresholds, the admission floor and the off-by-default packer knobs (the seat may enable group-major admission when queue depth shows contention). |
| `bh-gxpna.2` usage + right-sizing | **Keep, amend** | Usage from the cgroup scope where present, else PSS + scratch growth with CPU as a lower bound; never `wait4` rusage. |
| `bh-gxpna.3` AIMD capacity tuning | **Keep** | Unchanged. |

### `bh-o8769` — becomes the delegated enforcement layer

| Issue | Action | Amendment |
|---|---|---|
| epic | **Re-scope** | Layer 2 of the [framing](#2-enforcement-is-delegated-to-the-kernel): kernel-delegated bounds for every admitted job, optional per platform. |
| `bh-o8769.1` cgroup v2 adapter | **Keep, amend** | `systemd-run --user --scope` under a validation slice with CPU/memory/IO weights and caps from the grant; `nice`/`ionice` fallback; reuse `bh-69j3d`'s config surface and emergency override; the scope is also the preferred `report(usage)` source. Sequence with `bh-alzfb.3`. |
| `bh-o8769.2` doctor support matrix | **Keep, amend** | Report enforcement mechanism, signal availability and platform verification per host. |
| `bh-alzfb.3` / `bh-69j3d` | **Keep as is** | Operator split of 2026-09-29 stands: `bh-69j3d` is the pre-broker stopgap, `bh-alzfb.3` the broker-scoped policy after `bh-fmuqq.2`. |

## Watch upstream

Only relevant once trigger 1 or 5 is in sight. Track HyperQueue PR #1136 (or its successor
for #1135) and the next tag. On **any new HQ tag**, re-run `bh-5mdar.9`'s
`hq_build_probes.py` (on `wt/bead/issue/bh-5mdar.9`; the probe shapes) and
`bh-5mdar.10`'s `run_matrix.py` (on `wt/bead/issue/bh-5mdar.10`)
`--stage matrix` (group latency against the native packer) on a quiet host. Clean probes meet
bar items 1–2 and open the gate bead; the matrix answers item 4.

## v1 slice (bh-j9jqq)

The epic `bh-j9jqq` delivers the smallest useful cut of this ADR. Spike docs and artifacts cited
above live on their `wt/bead/issue/bh-5mdar.N` branches and are not on `main`.

**v1 delivers:**

| Bead | Delivers |
|---|---|
| `bh-uljpc` | The broker port, job model and `SlotsEngine` in `packages/beadhive-validation-broker`. |
| `bh-qrixp` | `LocalEngine`, in-process, selected by `work.validation_broker.engine` (`slots` or `local`, default `slots`; the section also holds budget, floor, packer, repoll_seconds) with the `BH_VALIDATION_BROKER_ENGINE` override. |
| `bh-9oc32` | Default per-key reservations (`validation_reservations.py`, override `work.attest.keys[].resources`) and the grant exported as `BH_GRANT_CPUS` to the `*-native` recipes. |
| `bh-mf1o4` | Broker-owned execution roots: exactly one bounded root per run holds both the checkout and the fence scratch, never straddling. A worktree outside every root is materialised into the chosen root. |
| `bh-qhrl6` | Pending attest keys run side by side through the broker. |

**Stays deferred to the existing molecules:**

| Bead | Deferred work |
|---|---|
| `bh-fmuqq.2` | Durable service and lease store with reconcile. |
| `bh-fmuqq.3` | Full conformance kit with replay bounds. |
| `bh-rpnfx` | Adapters. |
| `bh-6a76l` | Brake and signals. |
| `bh-gxpna` | Host seat. |
| `bh-o8769` | cgroups. |
| `bh-7s67j.4` | Cross-lane result sharing. |

The execution-roots model (`bh-mf1o4`) refines this ADR's "disk-backed fence scratch"
recommendation: rather than pointing only the scratch at disk, the broker picks one bounded root
per run that holds checkout and scratch together. As built:

- **Space is a counted pool.** Each root's bound is a `root:<path>` pool in the local engine's
  budget, clamped by the filesystem's measured free space through a `DiskSpace` port, so
  admission, release, EASY backfill and nested carving treat disk exactly like `dolt-servers`.
  Like memory, measured free space is not credited back with what running leases will still
  write: conservative, never optimistic. An unreadable root reads as full.
- **Order and pinning.** Roots are tried in configured order, so the operator states the
  preference (disk first saves the memory a tmpfs root costs). A gate is pinned to the root of
  its first admitted key while it has live keys, and only to a root whose bound fits every
  queued key, so `bh-qhrl6` can run a gate's keys in one shared checkout without stranding a
  larger key.
- **tmpfs is memory.** A `tmpfs` root adds the run's disk reservation to its memory reservation.
  A root declared `disk` that is RAM-backed is refused at selection (slots fallback, loudly).
- **The run directory.** `beadhive.validation_run_root.run_directory` holds the checkout
  (`git worktree add` of the exact tree, verified by tree id, unless the source already is a
  clean checkout of it inside the root), the `TMPDIR` the hermetic fence puts its scratch under,
  and the report drop zone. The fence itself is unchanged. The checkout's git object store stays
  in its source repository, read-only inside the fence. A killed client's directory, including
  a SIGKILLed fence's scratch, is reclaimed by the next run on that root through an owner marker.

**Current figures, by reference only:** the full gate is 1,448 s on `2c256557`, with stateful at
526 s and integration at 447 s, run sequentially. The ADR's own 2026-09-30 measurements are not
restated.
