# Validation broker — proposal and backend bake-off

**Status:** proposal, 2026-09-26. Not an ADR. The bake-off decision bead produces the ADR.
**Seat:** planning (groom → plan), with the operator.
**Workstream:** bh-alzfb (bake-off spikes bh-5mdar). Separate bug: bh-cesc7. See
[Filed beads](#filed-beads).

## Summary

A full gate on this hive takes about 15 minutes. Most of that time goes to seven attest keys
that run one after another, while the host's validation controls (host slots, attest keys,
worker counts, identity locks, the test watchdog) each size themselves without knowing about
the others. Two parallel checks can deadlock, and a third lane oversubscribes the host.

This proposal adds a **validation broker**: one host-wide queue that admits a validation job only
when its declared reservation fits the resources the host actually has free, orders jobs by
priority and by historical runtime, and hands each job its grant through an environment
contract that any test framework can consume. The broker sits behind one **shared broker
interface** so more than one concrete backend can implement it.

Research found two candidate backends worth building on instead of writing a scheduler from
scratch:

- **pueue + a small packer + psutil** — portable (Linux, macOS, Windows); pueue runs and records
  the jobs, and bh owns only the packing decision and the availability sensor.
- **HyperQueue** — Linux; native multi-resource reservations (sum pools, counted pools) and
  priorities, so most of the packer disappears.

A bake-off spike molecule builds both behind the shared interface and replays real gate runs
against them. Both licenses are permissive (MIT / Apache-2.0), which is a hard requirement. The
decision may keep both: pueue as the portable default and HyperQueue on large Linux hosts.

Kernel enforcement (cgroups) is an **optional adapter**, never a requirement. The broker must
work on any Linux distribution and on macOS.

## Problem, measured

### Where a full gate spends its time

`just check-all-native` runs its stages one after another, because `just` has no parallel
dependency execution. `selective_validation.run` also executes keys one at a time
(`src/beadhive/selective_validation.py`, the `for name in receipt.invalidated_keys` loop).
Median durations over 1,642 completed runs, recorded on bh-mfbk7:

| Key | Median |
|---|---|
| stateful | 347.8s |
| integration | 292.3s |
| demos | 152.8s |
| architecture-contracts | 61.2s |
| package | 45.3s |
| docs | 9.2s |
| unit | 4.0s |
| **Sum** | **≈ 913s (15.2 min)** |

The critical path of the current roadmap (bd serve owner → approve & bounce → API/CLI routing →
lifecycle handlers → CLI cutover) is a chain of beads that each pay two or three full gates.
Gate latency therefore turns directly into critical-path time.

### Five validation controls that don't coordinate

| Control | Where | Unit | Problem |
|---|---|---|---|
| Host validation slots | `validation_admission.host_slot`; `work.validation_slots` (2 here) | Fixed count of equal permits held by `flock` | Not re-entrant. `bh work check` takes a slot (`work_submission.py`, `impl_check`), then each key's runner calls `worktree.clean_checkout(..., reuse=False)`, which takes a second slot (`worktree_verify.py`, `impl_clean_checkout`). Two concurrent checks each hold one of two slots and wait forever. This is hq-vbha. |
| Attest keys | `selective_validation.run` | Per-key verdict | Keys run one at a time, and every key builds its own clean checkout. |
| Worker counts | `justfile`: `stateful_workers := "16"`, `integration_workers := "16"`, `PYTEST_XDIST_AUTO_NUM_WORKERS` default 16 | Per-command constant | Fixed regardless of what else runs. Two slots × 16 workers already uses all 32 cores of this host, so raising `BH_VALIDATION_SLOTS` to run a third lane oversubscribes it. |
| Identity lock | `validation_admission.identity_lock` | (hive, tree, command hash) | Works, but at whole-command granularity. Two lanes validating the same tree can't share one key's result mid-run. |
| Watchdog | `scripts/test-watchdog.py`, `BH_TEST_TIMEOUT_SECONDS` 900 | Wall clock | Time lost to contention counts as test time. bh-2p33n's eight deadline and connection failures under a 12-worker fan-out were one contention signature, not eight regressions. |

### This host, for scale

32 cores (x86_64), 47 GB memory, of which about 15 GB was available with 19 GB shared (tmpfs
caches) when measured on 2026-09-26. Memory may be the tighter constraint, not CPU. This host is
one example: the design must hold on other Linux distributions, other sizes, and macOS.

## Requirements

1. **Job model.** A job is an opaque command plus a **reservation vector** (at least X cpu, Y
   memory, Z of named pools such as `dolt-servers`), a **priority**, an **expected duration**
   taken from history, and a **group** (the gate it helps unblock). No DAG between jobs is
   needed: a gate's keys have no ordering between them.
2. **Admission against actual availability.** A job starts only when its full reservation fits
   what the host has free right now, not just a static budget.
3. **Packing objective.** Maximize validation throughput, and finish the group that unblocks the
   most urgent gate first, without tripping the overload brake.
4. **Deadlock-free by construction.** No hold-and-wait, and nested requests reuse the parent's
   grant.
5. **Portable.** Linux distributions and macOS. Enforcement is best-effort per platform, and
   `bh doctor` states what a host supports.
6. **Failsafe.** The broker must keep working when bh, Dolt or HQ is broken. If the broker
   itself is down, validation still runs, degraded to today's slots.
7. **Controllable by an agent.** Every signal and lever is available as CLI and JSON, as an
   API, and as MCP tools, so an agent loop can tune the host.
8. **Permissive licenses only** (MIT, Apache-2.0, BSD) for anything the broker depends on.
9. **Attested Green is unchanged.** Keys stay opaque, the five fail-closed rules hold, verdicts
   stay keyed on (tree, command hash), the landing boundary still requires the exact tree, and
   a carried verdict never chains. The broker changes when and alongside what a key runs, never
   whether its verdict counts.

## Architecture

### Layers

| Layer | Job | Portable? | Source |
|---|---|---|---|
| Runner | Durable queue, launch, logs, exit codes, restart survival | Required | A backend (pueue or HyperQueue) |
| Packer | Decides which queued jobs start now | Required | A greedy loop in bh (pueue backend) or HyperQueue's own scheduler |
| Availability sensor | Free memory, CPU, disk, pressure | Required | `psutil` everywhere. Optional extras: Linux PSI (`/proc/pressure/*`), macOS `kern.memorystatus_vm_pressure_level`. |
| Enforcement | Caps what a running job can actually use | Optional | None by default. A Linux cgroup v2 / systemd-scope adapter where available. |

### Processes

```text
 supervisor policy ──► hard bounds (HQ)                          slow loop: minutes–hours
                          │
 host agent seat ──CLI/API/MCP──► set-points within bounds (reason, TTL, audited)
                          │
                          ▼
 ┌─ bh-validation-broker ── own package · own service unit · own state ─┐
 │  shared broker interface → backend: pueue+packer | HyperQueue        │  fast loop: ~1s
 │  admission · priority/LPT order · brake · stall/orphan detection     │
 └───────────────────────────────────────────────────────────────────────┘
          ▲ submit / grant / release                    │ samples host + job usage
          │                                             ▼
   bh work check / submit / merge / release attest  ──► key command, with the grant
   (clients)                                              in its environment
```

- **The broker is its own process.** It ships as a separate workspace package with its own
  service unit (systemd user unit on Linux, launchd agent on macOS, following the host daemon's
  existing supervisor backends) and its own state store. It does **not** depend on Dolt, HQ, or
  the root `beadhive` package, and the bh version under test does not upgrade it. A broken bh
  candidate cannot take down its own scheduler.
- **The host daemon relays it.** The existing host daemon (`/api/v1/*` on 127.0.0.1:8420) is
  not the failsafe: it reports ready only when HQ, Dolt, bead-state and run-journals are up
  (`REQUIRED_FACTORY_DEPENDENCIES`). It exposes `/api/v1/validation/*` as an authenticated relay
  for remote and agent readers, while local clients talk to the broker directly.
- **Clients stay the parents of their jobs** where the backend allows it, so stdout, stderr and
  exit codes keep flowing as they do today.

### Shared broker interface

One port, several backends. Sketch:

```text
submit(job: JobSpec) -> JobHandle          # reservation, priority, expected duration, group,
                                           # parent lease (optional), command, cwd, env
wait_grant(handle) -> Grant                # blocks; Grant = the amounts actually granted
report(handle, outcome, usage)             # exit code, wall time, peak memory, cpu seconds
cancel(handle, reason)
status() -> BrokerStatus                   # budget, in use, queue, brake level, signals
events() -> stream                         # submitted, admitted, finished, braked, stalled
```

A **conformance kit** exercises every backend identically: over-admission never happens, a
nested request draws from its parent, a killed client frees its reservation, a brake stops
admission, and replayed gate sets finish in the expected order.

### Grant contract (framework-generic)

Keys stay opaque commands. bh never rewrites `-n 16`. The broker exports the grant, and a small
**runner profile** maps it onto the framework's own worker setting:

| Profile | What it sets |
|---|---|
| (all) | `BH_GRANT_CPUS`, `BH_GRANT_MEMORY_MB`, `BH_GRANT_<POOL>` |
| pytest-xdist | `PYTEST_XDIST_AUTO_NUM_WORKERS` (with `-n auto`) |
| pants | `PANTS_PROCESS_EXECUTION_LOCAL_PARALLELISM` |
| cargo | `CARGO_BUILD_JOBS` |
| go | `GOFLAGS=-p=N` |
| make / just | `MAKEFLAGS=-jN` |
| jest, vitest, turbo | `${BH_GRANT_CPUS}` in the key's own command (`--maxWorkers`, `--concurrency`) |

Runner profiles are plugin data through a new kernel capability, `validation.runner`. pytest-xdist
and make are built in, beadhive-pants ships the Pants profile, and the Turborepo build plugin
(bh-mwj8n) ships its own.

The grant **never enters `cmd_hash`**: a verdict is about the tree and the command, not the
worker count. A suite that passes with 16 workers and fails with 4 has an ordering or timing
bug. The run record keeps the grant so that failure can be traced.

### Key resource declaration

Two optional fields on each attest key:

```yaml
attest:
  keys:
    - name: stateful
      cmd: just attest-stateful
      resources: {cpu: {min: 4, max: 16}, memory_gb: 6, dolt-servers: 8}
      runner: pytest-xdist
    - name: docs
      cmd: just attest-docs
      resources: {cpu: 1}
```

A key that declares nothing behaves as it does today: it takes the run's whole share and runs
alone. Nothing changes until a hive adds profiles.

### Admission and packing

- **Whole reservation or nothing.** A job is admitted only when its minimum reservation fits
  `min(budget, measured availability) − reservations held by others`. It never holds part of a
  reservation while waiting.
- **Nested requests.** The lease id travels in `BH_VALIDATION_LEASE`. A child (a key runner or a
  clean checkout) sub-allocates from its parent's grant and never goes back to the host queue,
  which removes the hq-vbha double hold by design.
- **Order.** By priority class (critical path > check/submit > merge/land > background), then
  by group urgency. Within a group, longest expected duration first (LPT), which approximates
  the shortest time for the whole group to finish. Expected duration and peak memory come from
  the job's own history, not its declaration.
- **Flexible grants.** A job gets between its minimum and maximum, with the maximum capped at
  the measured knee of its speedup curve, so no cores sit idle on a job that can't use them.
- **Reservation for the head of the queue**, so small jobs can't starve a large one.
- **Background fill.** Idle capacity goes to background work (for example bh-ydpjg's
  preemptive version-set validation), which never blocks foreground admission.
- **Optional solver.** If greedy LPT proves too weak for deep queues, OR-Tools CP-SAT models
  the same problem directly: one `AddCumulative` constraint per resource, minimizing either
  makespan or the weighted completion time of each gate's group. Snakemake's scheduler uses the
  same shape (an ILP through PuLP with a greedy fallback). The solver would run in the broker's
  own environment because of its dependency weight.

### Overload brake

Signals are portable first (`psutil`: available memory, CPU utilization, disk free on scratch
and tmpfs), with platform extras where they exist. Levels use hysteresis so they don't flap.
Thresholds are set-points the agent loop tunes.

| Level | Example signals | Action |
|---|---|---|
| Green | Available memory above floor, scratch disk above floor, pressure low | Admit normally |
| Amber | Memory pressure sustained above threshold (PSI `some`, or macOS pressure level "warn"), or CPU saturated | Admit only critical work, at minimum grants. Pause background admission. |
| Red | Available memory below floor, or scratch/tmpfs below floor, or PSI `full` | Stop all admission. Pause the lowest-priority background job where the backend or platform supports it. |
| Kill | Red sustained past a set-point | Kill the lowest-priority job and record **exit 75** (unknown, retryable), never a red verdict |

Where the optional cgroup adapter is present, a slice-level memory cap is a backstop that
doesn't depend on the broker at all. Pausing only applies to background work, because a paused
suite with wall-clock timeouts would fail when resumed.

### Deadlock, stall and orphan detection

- **Prevented by design:** whole-reservation admission, nested sub-allocation, and a fixed lock
  order (identity lock, then broker grant), which `worktree_verify` already documents.
- **Detected anyway:**
  1. A wait-for graph across grants and waiters. A cycle is a bug: the youngest waiter gets a
     retryable failure and an event is raised.
  2. **Stall:** a grant is held but the job's process tree has used close to zero CPU and
     started no new processes for N minutes. This is the exact hq-vbha symptom. The broker raises
     a `stalled` event, and policy decides whether to kill.
  3. **Orphans:** the holder process is gone but its reservation remains. It is reclaimed.
  4. **Starvation:** the head of the queue has waited past a threshold. Its reservation grows.

### Controls for agents and operators

- **Read:** `bh validation status | queue | leases | history --key K | events --follow |
  profiles --suggest`, all with `--json`, mirrored over the broker socket, the daemon relay and
  MCP tools.
- **Write:** `budget set`, `brake thresholds | engage | release`, `lease cancel`, `priority
  set`, `profile override <key>`, `drain` / `resume`.
- **Every write** needs a reason, is journaled, expires by default, and is clamped to the
  supervisor's bounds. If the agent dies, set-points expire back to defaults. A bad agent can't
  exceed the bounds.

### The host agent loop

A slow loop (minutes to hours), never in the admission path. Its objective is completed
validation work-seconds per wall hour, weighted by priority, with the brake staying green.

- **Capacity tuning (AIMD).** Raise admitted capacity a little while utilization is below
  target and the brake stays green. Cut it in half on each brake trip.
- **Right-sizing.** Per key and command hash, compare measured peak memory, effective cores
  (cpu seconds ÷ wall seconds), throttle events and duration against the declared profile:
  under-declared (throttled or over memory), over-declared (p95 use under half the grant over N
  runs), or outdated (the scaling knee moved). The output is a host-local override with an
  expiry, or a proposed bead or config change for the key's profile. A persistent change goes
  through review.
- **Reports:** queue latency by class, brake trips, wasted work (killed or re-run keys).

**Seat.** A new host-scoped seat whose only write authority is these levers, bounded by
supervisor policy. The existing `controller` seat stays read-only: its contract is "observe,
don't steer".

### Failure behavior

| What fails | What happens |
|---|---|
| The broker | Clients fall back to today's file-lock slots and warn loudly. |
| The agent | The broker keeps the last set-points, which expire back to defaults. |
| bh itself | The broker is a separate package and unit, and the bh candidate doesn't upgrade it. |
| Dolt or HQ | The broker keeps its own state; it never reads Dolt or HQ. |
| Memory pressure | The brake stops admission. The optional cgroup adapter caps the slice. |

## Research: frameworks considered

### Relaxing the requirements

The first iteration of this design assumed a DAG scheduler (setup nodes for clean checkouts and
fixture pools, key runs depending on them) and kernel enforcement through cgroup v2 and systemd
scopes. Two operator constraints changed it:

1. **Portability.** This host has cgroup v2 with cpu, memory and pids delegated to the user,
   PSI, and systemd 257, and `systemd-run --user --scope` works. Other hosts won't. Building on
   cgroups would force a compatibility matrix across Linux distributions and exclude macOS. So
   enforcement became an optional adapter, and admission became the portable core.
2. **No DAG is needed.** A gate's keys have no ordering between them. What matters is "at least
   X of Y reserved before start" plus priority ordering from historical runtime. That opens the
   field to simple queues combined with a packing rule.

### Candidates

| Option | Platforms | Reservations | Priority and duration | Interface | License | Verdict |
|---|---|---|---|---|---|---|
| **HyperQueue** | Linux | Native. Workers declare sum pools (for example memory) and indexed or counted pools; a task starts only when every requested amount is free. Resources are logical, not enforced. | `--priority` (signed 32-bit), `--time-limit`; `--time-request` only checks worker lifetime | CLI with `--output-mode json` (documented as unstable), Python API, one static binary | MIT | **Bake-off** |
| **pueue** + bh packer | Linux, macOS, Windows | None: per-group parallel counts only | Stashed tasks, force-starting specific tasks, `--after` | CLI with JSON `status` and `log`, callback hooks | MIT / Apache-2.0 | **Bake-off** |
| Dask distributed (local scheduler and worker) | Linux, macOS, Windows | Abstract worker resources; a task waits until they are free. Not physically enforced. Fixed when the worker starts. | Priorities | Python clients from several processes; tasks wrap subprocesses | BSD-3 | Fallback if a pip-only runner is required |
| task-spooler (`ts`) | Linux, macOS, BSD | One dimension: `-N` slots per job out of `-S` total | "Urgent" only | C CLI | GPL-2.0 | Excluded (license, one dimension) |
| Slurm (single node) | Linux | Yes, including "licenses" for shared counted pools | Backfill using time limits, the closest built-in match to the goal | CLI, Python bindings | GPL-2.0 | Excluded (license, Linux only, operating weight) |
| Flux (single-user instance) | Linux | Yes, graph-based (Fluxion) | Backfill, priorities | CLI, Python API; `flux start` runs unprivileged | LGPL-3.0 | Excluded (license, Linux only) |
| Nomad (single node) | Linux, macOS, Windows | cpu and memory bin packing; batch jobs wait for capacity. No named pools for `raw_exec`. | Job priority | HTTP API, Python client | BSL 1.1 | Excluded (license) |
| Ray | Linux, macOS, Windows | Custom resources, logical only | Limited | Python; multi-process runtime | Apache-2.0 | Excluded (runtime weight) |
| Snakemake | Linux, macOS | Resource-aware packing (ILP through PuLP, greedy fallback) | — | Workflow files, one invocation at a time | MIT | Borrow the algorithm, not the tool |
| Celery, Hatchet, procrastinate | Varies | Concurrency caps only | Retries, priorities | Python; broker or Postgres | Varies | Excluded (no resource packing) |
| CI runners (GitHub self-hosted, Buildkite) | All | Runner count | Queue UI and history | Agents | Varies | Excluded (fresh checkout per job loses warm state) |
| OR-Tools CP-SAT (solver, not a runner) | Linux, macOS, Windows | `AddCumulative` per resource | Makespan or weighted completion objectives | Python | Apache-2.0 | Optional packer for the pueue backend |

### Why two backends

- **pueue** handles the daemon, durable queue, process lifecycle and logs on every platform bh
  supports. bh maintains only the packing decision and the availability sensor, roughly 300–500
  lines. Risk: pueue describes itself as not designed to be a heavy-duty programmable scheduler,
  and its reservation model is ours to build.
- **HyperQueue** reserves multi-dimensional resources natively, so bh maintains only priority
  mapping, the brake, and resizing when availability changes. Risk: Linux only, JSON output is
  marked unstable, and worker resources are fixed at start.
- Both sit behind the same broker interface, so keeping both (portable default plus a Linux
  option for large hosts) costs one more adapter, not a second design.

## Bake-off plan

A spike molecule builds both backends behind the shared interface as throwaway prototypes (kept
as `docs/spikes/artifacts/<bead>/` patches, not product code, following the Pants prototype
precedent) and replays real gate key sets from the validation records.

**Scoring criteria:**

1. Glue code bh would maintain (lines, moving parts).
2. Never admits more than fits, across replayed contention.
3. Time for a gate group to finish, compared with running its keys one after another.
4. Recovery after killing the backend daemon mid-run.
5. How the brake hooks in (pausing admission, resizing).
6. JSON and API stability; upgrade churn.
7. Platform coverage. macOS is recorded as **unverified** for now and tested later.
8. License (must be permissive).

Measurements that decide sizing regardless of backend: per-key peak memory and effective cores
on this host (is memory or CPU the real limit?), whether stateful, integration and demos running
side by side beat running them in sequence without reintroducing the bh-2p33n contention, and
whether keys can share one clean checkout inside the hermetic fence.

## Workstream

The broker is filed as a workstream: the bake-off spike molecule first, then backend-agnostic
molecules gated behind its decision. The planning seat normally files implementation molecules
only after a spike verdict. Here the operator asked for the whole deliverable up front, so the
later molecules are scoped to the shared interface (they hold whichever backend wins, or both),
stay kickoff-pending, depend on the decision bead, and are amended through replan when the
verdict lands.

| Molecule | Epic | Scope |
|---|---|---|
| Bake-off spikes | bh-5mdar | Interface and replay harness, HyperQueue prototype, pueue + packer prototype, sizing measurements, signals, decision |
| Broker core | bh-fmuqq | Package, service unit, state store, shared interface, conformance kit, fallback to slots |
| Backend adapters | bh-rpnfx | The chosen backend(s), setup-managed install and license check, per-host selection |
| Gate integration | bh-7s67j | Key resource fields, grant contract and runner profiles, running selected keys side by side, per-key result sharing, failure policy |
| Signals and controls | bh-6a76l | Availability sensor, brake levels, stall and orphan detection, CLI/API/MCP controls, daemon relay |
| Host agent seat | bh-gxpna | Seat definition and bounds, right-sizing, capacity tuning |
| Enforcement adapters | bh-o8769 | Optional Linux cgroup v2 adapter, `bh doctor` support matrix |

The deadlock bug (hq-vbha's nested slot hold) is filed separately as bh-cesc7 and doesn't wait
for any of this. The filed beads are the record of each molecule; the specs used to file them
are not kept (bh-g1kog).

## Relationships

- **bh-zyyoz** (native path-selected keys) restores the key catalog that gate integration
  schedules. Suggested amendment: implement its "full gate records per-key receipts" as "the
  full gate runs every key", so the broker and receipts share one path.
- **bh-i6gua** (selection unification) stays frozen until after the core cutover. The broker
  changes how keys run, not which keys run; it only adds `resources` and `runner` to the key
  schema, which that spike carries forward.
- **bh-1c04h** (test isolation fence): sharing one clean checkout between keys depends on the
  per-phase scratch checkout in bh-cx9me.
- **bh-vo5ey** (warm verify worktrees) fits the broker's preference for reusing warm state.
- **bh-484xb** (per-host realization): the budget lives in host config, with supervisor bounds
  in HQ.
- **bh-j5saf** and **bh-t0ha** are superseded by gate integration.
- **bh-ydpjg** (preemptive version-set validation) is the first background-class consumer.
- **bh-2p33n**'s fixed stateful ceiling becomes that key's declared maximum and `dolt-servers`
  pool.

## Open questions

1. Is memory or CPU the binding constraint on this host? (Sizing spike.)
2. Can keys share one clean checkout under the fence, or does each need its own scratch view?
3. How should the broker's state store and service unit be installed on hosts without systemd
   user units or launchd agents?
4. Should the broker eventually schedule other heavy local work (builds, Repowise indexing), or
   stay validation-only?

## Filed beads

Filed 2026-09-26. All molecules are kickoff-pending and pass `bh plan verify`.

- **bh-alzfb** — Workstream: validation broker (container). Closeout: bh-alzfb.1, which waits
  on every child molecule.
- **bh-5mdar** — Bake-off spikes:
  - bh-5mdar.1 shared broker interface and replay harness (root)
  - bh-5mdar.2 per-key sizing and side-by-side contention (root)
  - bh-5mdar.3 portable availability signals and brake thresholds (root)
  - bh-5mdar.4 HyperQueue prototype (after .1)
  - bh-5mdar.5 pueue + packer prototype (after .1)
  - bh-5mdar.6 DECISION (after .1–.5)
- **bh-fmuqq** — Broker core. Waits on bh-5mdar.6 and on the package ADR bh-qdezo.1.
- **bh-rpnfx** — Backend adapters. Waits on bh-5mdar.6 and bh-fmuqq.
- **bh-7s67j** — Gate integration. Waits on bh-5mdar.6, bh-fmuqq and bh-zyyoz. Supersedes
  bh-j5saf and bh-t0ha (closed onto bh-7s67j.3).
- **bh-6a76l** — Signals and controls. Waits on bh-5mdar.6 and bh-fmuqq.
- **bh-gxpna** — Host agent seat. Waits on bh-5mdar.6 and bh-6a76l.
- **bh-o8769** — Enforcement adapters. Waits on bh-5mdar.6 and bh-fmuqq.
- **bh-cesc7** — Bug, separate: parallel `bh work check` deadlocks on the nested host-slot hold.
  Related to bh-fmuqq.4.

## Sources

- HyperQueue: [documentation](https://it4innovations.github.io/hyperqueue/stable/),
  [resources](https://it4innovations.github.io/hyperqueue/stable/jobs/resources/),
  [jobs, priority and time limits](https://it4innovations.github.io/hyperqueue/stable/jobs/jobs/),
  [output mode](https://it4innovations.github.io/hyperqueue/stable/cli/output-mode/),
  [repository](https://github.com/It4innovations/hyperqueue)
- pueue: [repository](https://github.com/Nukesor/pueue)
- Dask: [worker resources](https://distributed.dask.org/en/stable/resources.html),
  [scheduling](https://docs.dask.org/en/stable/scheduling.html)
- task-spooler: [justanhduc/task-spooler](https://github.com/justanhduc/task-spooler),
  [original](https://viric.name/soft/ts/)
- Flux: [starting an instance](https://flux-framework.readthedocs.io/projects/flux-core/en/stable/guide/start.html),
  [flux-core](https://github.com/flux-framework/flux-core),
  [Fluxion](https://github.com/flux-framework/flux-sched)
- Nomad: [raw_exec driver](https://developer.hashicorp.com/nomad/docs/deploy/task-driver/raw_exec)
- Ray: [application-level scheduling with custom resources](https://rise.cs.berkeley.edu/blog/ray-scheduling)
- Snakemake: [scheduler source](https://snakemake.readthedocs.io/en/v7.2.1/_modules/snakemake/scheduler.html)
- OR-Tools: [CP-SAT Python reference](https://developers.google.com/optimization/reference/python/sat/python/cp_model),
  [scheduling guide](https://github.com/farao-community/or-tools/blob/stable/ortools/sat/doc/scheduling.md)
- In-repo: `src/beadhive/validation_admission.py`, `src/beadhive/work_submission.py`,
  `src/beadhive/worktree_verify.py`, `src/beadhive/selective_validation.py`, `justfile`,
  `src/beadhive/daemon_supervisor.py`, `src/beadhive/daemon_platform.py`, bh-mfbk7, bh-xg1r9,
  bh-2p33n, bh-zyyoz.
