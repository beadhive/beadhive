# Spike `bh-kp64s.1` — can Gas City be a Beadhive work runtime with one owner per concern?

**Bead:** `bh-kp64s.1` · **Seat:** dev/gc-runtime · **Type:** research-only (no product code)
**Feeds decision on:** `bh-kp64s.2` (Decide Gas City runtime adapter feasibility), which is also
blocked by `bh-iaubc.2` (shared Beads state safety). Revisits blocker 1 of `bh-d75bi`.

> Upstream sources are cited as `gascity@v1.4.2:<path>:<line>` against a read-only clone of tag
> `v1.4.2`. Beadhive sources are cited at this branch's base (`308a2e33`, post-`bh-sy36q`
> cutover). Nothing was installed, no `gc` binary was run, and no hive was written. Evidence is
> therefore documentary and source-read, not experimental. Each row says which.

## Question

Gas City (GC) v1.4.2 could be integrated in two ways:

- **Mode 1, hosting only.** Beadhive keeps dispatch and bead lifecycle, and GC supplies only
  process/session hosting for the exact `bh-<seat>` role-binary contract (work-runtime-tiers ADR
  Amendment 2 §1).
- **Mode 2, delegated molecule.** Beadhive hands one kicked-off molecule's dispatch to GC's
  formula controller, and Beadhive's own loop does not claim or schedule that work.

For each mode, the spike asks whether it can keep **exactly one owner** for each of these
concerns:

- claims;
- desired-versus-running reconciliation;
- session start and recovery;
- workspace and worktree ownership;
- structured `SeatRun` capture;
- cancellation;
- the host/plugin provisioning boundary.

This also settles whether `bh-d75bi`'s "two reconcilers over one bead store" can be dissolved.
The outcome is that both modes work, one works, or neither is safe.

This spike does **not** ask whether GC may share Beadhive's managed hive database. That question
belongs to sibling spike `bh-iaubc.1`, which is running in parallel. Where a mode depends on the
answer, this document says so and does not guess. It also does not design the AGF Gas City pack
(`bh-lko4x`), and it does not propose anything to build before the `bh-sy36q` cutover.

## Method

1. **Beadhive contracts read at this base:**
   - the runtime seam: `src/beadhive/runtime.py`, `work.runtime` in
     `src/beadhive/modules/config/contracts.py`, and
     [work-runtime-tiers-adr.md](../design/work-runtime-tiers-adr.md) Decision 1 and Amendment 2;
   - the `local` tier: `src/beadhive/localloop.py` (pass, claim, spawn, cancel, and orphan
     reaping);
   - the dispatch, molecule, and local-loop seams that `bh-sy36q.5` moved into beadhive-core:
     `packages/beadhive-core/src/beadhive_core/dispatch.py` and `src/beadhive/dispatch_state.py`;
   - `SeatRun`: `src/beadhive/seatrun.py` and `src/beadhive/role_process.py`;
   - worktree slots: `packages/beadhive-plugins/src/beadhive_plugins/worktree_slots.py` and the
     [manager/binding ADR](../design/bh-mr9tk.2-worktree-manager-herdr-binding-adr.md);
   - the Herdr precedent:
     [lifecycle-owned-agent-herdr-space-contract.md](../design/lifecycle-owned-agent-herdr-space-contract.md)
     and [seat-process-ownership-north-star.md](../design/seat-process-ownership-north-star.md);
   - the [plugin kernel ADR](../design/plugin-kernel-v1-adr.md),
     [loop-ownership ADR](../design/loop-ownership-and-execution-memory-adr.md), and
     [gas-frameworks-comparison.md](../design/gas-frameworks-comparison.md) (refreshed
     2026-09-27).
2. **Beads read in full:**
   - `bh-kp64s`, `bh-kp64s.1`, and `bh-kp64s.2`, including its NOTES that name the `bh-sy36q.5`
     seams and `bh-ym56t`;
   - `bh-iaubc.1`, and `bh-tfapu` for its status;
   - `bh-d75bi`, including the 2026-08-11 CORRECTION and Amendment 1 (the review-gate boundary).
3. **Gas City v1.4.2 docs read:**
   - the RPP reference (`docs/reference/exec-session-provider.md`) and the herdr provider
     reference;
   - the controller, session, health-patrol (reconciler), dispatch (sling), and molecule
     architecture docs;
   - formula spec v2 (control dispatch, drain, finalize), the convoy-first drain design, and
     `engdocs/drain-fanout.md`;
   - the CLI reference (`gc session`, `gc runtime`) and the OpenAPI spec (`SessionCreateBody`).
4. **Gas City v1.4.2 source read to confirm the docs:**
   - the subprocess and exec providers (`internal/runtime/{subprocess,exec}`);
   - the session-create handler (`internal/api/handler_session_create.go`) and the option schema
     (`internal/config/{provider,options}.go`);
   - the claim path (`internal/beads/bdstore.go`) and the sling reassign path
     (`internal/sling/`);
   - workflow finalize (`internal/dispatch/runtime.go`) and the core-pack formulas.
5. **Checks that were not run, and why.** No GC process was started, because the constraints
   forbid installing `gc`. This leaves three things unmeasured:
   - the reconciler's exact treatment of a crashed `one_shot` session;
   - the latency of drain dependency wiring;
   - the end-to-end timing of the subprocess provider's SIGTERM-to-SIGKILL grace.

   Each is marked **documented** or **source-read** below, never **measured**.

## Evidence

### A. What Beadhive requires of any runtime

- **E1.** **The state machine stays in Beads.** Beads owns lifecycle state, and the runtime owns
  only process scheduling. "No runtime-only lifecycle state… NEVER authoritative about whether a
  bead is claimed, blocked, approved, or done"
  ([work-runtime-tiers-adr.md:99-102](../design/work-runtime-tiers-adr.md)). Retry counts are
  derived by counting event beads, never stored ([loop-ownership
  ADR:107](../design/loop-ownership-and-execution-memory-adr.md)).
- **E2.** **The seat contract is exact and per bead.** Every seat is launched as `bh-<seat>
  --workspace <path> --bead <id> --instructions <file|-> --session_id <uuid>`, with `SeatRun` JSON
  on stdout as the source of truth
  ([work-runtime-tiers-adr.md:438-463](../design/work-runtime-tiers-adr.md)). The seam that carries
  it is `Runtime.schedule/observe/on_gate_resolved` (`src/beadhive/runtime.py:72-118`).
- **E3.** **The CANCEL ladder belongs to whoever holds the pipes.** That owner must outlive the
  child, hold its stdin and stdout, and **never send SIGINT**, because a SIGINT-cancelled run exits
  0, which collides with `0 = done`
  ([work-runtime-tiers-adr.md:583-602](../design/work-runtime-tiers-adr.md)). `localloop` enforces
  this with `FORBIDDEN_SIGNALS` (`src/beadhive/localloop.py:118-124`). It spawns each seat with
  `start_new_session=True` and drains stdout from the moment of spawn (`localloop.py:394-432`).
- **E4.** **Orphans are reaped, not adopted.** "An orphaned seat is REAPED, NOT ADOPTED… the pipes
  died with the old parent" (`localloop.py:1071-1082`). Recovery re-dispatches a fresh turn against
  the same worktree, because the branch is the checkpoint
  ([work-runtime-tiers-adr.md:450-452](../design/work-runtime-tiers-adr.md)).
- **E5.** **The local loop is a reconciler already, and part of its sweep is hive-wide.** Each pass
  runs `gate check -> reclaim -> lease -> heartbeat -> caps -> harvest -> decide -> dispatch`
  (`localloop.py:1-12`). Step 2 calls `coordination.reclaim(self.hive_dir, actor=…)` with **no
  label, assignee, or id filter** (`localloop.py:1666`). It therefore reverts every stale lease this
  replica granted, including leases on beads outside the loop's molecule. The wrapper already
  accepts `exclude_label` (`beadhive_bd_cli/coordination.py:357-361`); the loop does not pass it.
- **E6.** **The claim is molecule-bounded only when the caller asks.** The loop claims through `bh
  work next --json --epic <epic>` (`localloop.py:1313-1357`). The `--epic` bound is opt-in
  (`src/beadhive/work.py:312`), so a hive-wide `bh work next` or a manual `bh work claim` can take
  any ready bead.
- **E7.** **The post-cutover seams only read.** `DispatchCommands` does molecule-progress, swarm,
  event-row, and ready-poll reads. "Process scheduling and role execution stay root-supplied runtime
  capabilities — spawning a seat, the CANCEL ladder, process-group reaping — none of that is touched
  here" (`beadhive_core/dispatch.py:30-32`, `:152-166`). The composition adapter re-derives every
  answer with no cache (`dispatch_state.py:20-22`). A delegation filter belongs on the
  `work.dispatch.poll` read, and a hosting adapter belongs behind `Runtime` and `spawn_seat`.
  Neither lives in pre-cutover argv code.
- **E8.** **A `WorkspaceBinding` is presentation only.** Its port is `bind(handle) -> handle` and
  `release(handle)`: "presentation grouping for one presenter", with release before the manager's
  remove (`worktree_slots.py:65-78`). Exactly one `WorktreeManager` owns mechanics per hive, and
  Beadhive policy computes every path and branch ([bh-mr9tk.2
  ADR:121-130](../design/bh-mr9tk.2-worktree-manager-herdr-binding-adr.md)). The slot has no start,
  stop, observe, or signal verbs.
- **E9.** **Herdr, the only external process host Beadhive accepts, is passive.** Under the accepted
  Herdr contract, core owns launch identity, completion, and teardown, and the adapter owns only
  "bounded allocation effects". Herdr IDs are "staleable pointers", and "process exit, idle, `done`…
  never mint lifecycle completion"
  ([lifecycle-owned-agent-herdr-space-contract.md:50-65](../design/lifecycle-owned-agent-herdr-space-contract.md)).
  When Herdr loses its server, the agents die with it, and Beadhive relaunches generation `N+1`
  (`:147-153`). Herdr has no desired-state reconciler of its own, which is why that contract can
  give Beadhive sole ownership.
- **E10.** **The plugin kernel binds capabilities and does not install anything.** A plugin owns
  `plugins.<id>` config and declares executables by basename and version range only
  ([plugin-kernel-v1-adr.md:175-177](../design/plugin-kernel-v1-adr.md)). "Installation never
  implies enablement, and enablement never authorizes installation", and `bh-h441d` owns
  provisioning (`:193-198`). At most one enabled plugin owns a capability, and conflicts fail closed
  (`:94-101`). Runtime port implementations and dynamic loading "remain downstream work"
  (`:245-247`). `work.runtime` is a core `Literal["claude", "local", "temporal"]` whose own
  description says it is "not a plugin framework" (`contracts.py:793-804`). The Nix toolchain
  installs neither `gc`, `tmux`, `jq`, nor `flock`
  ([gas-frameworks-comparison.md:395-400](../design/gas-frameworks-comparison.md)).

### B. What Gas City's session layer actually offers (Mode 1 inputs)

- **E11.** **Every GC session is owned by GC's reconciler.** The controller "periodically reconciles
  running agents against the desired config"
  (`gascity@v1.4.2:engdocs/architecture/controller.md:10-16`). Each tick runs
  `reconcileSessionBeads()` to "make session beads and running sessions match the desired list"
  (`:114-117`). The state table is start / skip / **drain or close when not desired** / **drain +
  restart when drifted** (`engdocs/architecture/health-patrol.md:128-137`). "True orphans are killed
  immediately" (`:154-162`). *Documented.*
- **E12.** **An ad-hoc session is still a desired-state object.** `gc session new` waits "for the
  reconciler to start the session", and `gc session kill` is described as "Force-kill session
  runtime (reconciler restarts)" (`gascity@v1.4.2:docs/reference/cli.md:3739`, `:3889`). Crash-loop
  quarantine uses `max_restarts`, which defaults to 5, and "0 = unlimited" (`controller.md:266`). No
  setting means "never restart". Tracker state is in memory only (`controller.md:350-354`).
  *Documented.*
- **E13.** **GC cannot be handed the exact seat argv.** `SessionCreateBody` has
  `additionalProperties: false` and accepts only `alias`, `async`, `kind`, `message`, `name`,
  `options`, `project_id`, and `title` (`gascity@v1.4.2:docs/reference/schema/openapi.json:7132`).
  The work dir comes from the template's create context, and the command comes from the resolved
  provider (`internal/api/handler_session_create.go:135-157`). `options` are `"select" only`, with a
  fixed list of `Choices` (`internal/config/provider.go:18-23`), and any value outside it is
  rejected (`internal/config/options.go:125-133`). Per-bead `--workspace`, `--bead`, and
  `--session_id` therefore cannot be expressed. The only free-form per-session input is prompt text
  (`message`). *Source-read.*
- **E14.** **The subprocess provider discards stdout and interrupts with SIGINT.** Output goes to
  `/dev/null`: `cmd.Stdout = nullFile; cmd.Stderr = nullFile`
  (`gascity@v1.4.2:internal/runtime/subprocess/subprocess.go:155-156`). The seat runs under `sh -c`
  in its own group (`Setpgid: true`, `:140-141`), and `Stop` sends SIGTERM then SIGKILL
  (`:220-241`), which is good. However, `Interrupt` sends **SIGINT to the process group**
  (`:243-251`). The controller's graceful shutdown always sends Interrupt before Stop
  (`controller.md:31-33`, `:186-189`). `Peek` is unsupported (`subprocess.go:362-366`).
  *Source-read.*
- **E15.** **The exec provider (RPP) is fire-and-forget and runs in the opposite direction.** GC
  calls `<script> <op> <name>`, and "the exec provider itself is fire-and-forget — it calls `script
  start` and returns immediately" (`gascity@v1.4.2:docs/reference/exec-session-provider.md:65`,
  `:180-182`). This is a GC → provider contract. Beadhive could author the script, but then a
  Beadhive process has to keep the pipes (E3), and GC's reconciler still decides when to call
  `start` and `stop` (E11). `interrupt` is whatever the script does
  (`internal/runtime/exec/exec.go:590-595`), so the SIGINT problem in E14 is avoidable only in this
  provider. *Documented and source-read.* This confirms the `bh-d75bi` correction: exec is
  first-class, but it gives no supervision.
- **E16.** **GC lists Herdr as a peer provider.** Herdr is "an opt-in alternative to tmux… one tab
  per agent" and is selected city-wide (`gascity@v1.4.2:docs/reference/herdr-provider.md:5-10`).
  Under GC, then, Herdr is a *session provider below GC's reconciler*. Under Beadhive it is a
  *presenter and passive launch host below Beadhive's lifecycle* (E9). The overlap with `bh-ym56t`
  is real, but the owners are opposite.

### C. What GC's formula controller offers (Mode 2 inputs)

- **E17.** **The GC orchestrator executes control beads, and agents execute only work beads.** A v2
  workflow is a root bead, step beads, and control beads. "The orchestrator executes every control
  bead… No agent participates in control execution. Agents execute only plain work beads"
  (`gascity@v1.4.2:docs/reference/specs/formula-spec-v2.md:53-72`). The loop-ownership ADR's
  Decision 3 rejected *Beads* formulas because they cannot execute ([loop-ownership
  ADR:212-218](../design/loop-ownership-and-execution-memory-adr.md)). GC v2 does execute, so that
  part of the rejection does not carry over. Its corpus-growth argument (`:220-225`) still applies,
  to the beads GC writes (E24).
- **E18.** **Drain maps a molecule's members onto per-member item runs and follows member
  dependencies.** `[steps.drain]` "scatters the input convoy into one-member unit convoys and runs
  an item formula per unit". It has `member_access = read | exclusive`, `max_units` up to 100, and
  `on_item_failure` (`formula-spec-v2.md:761-777`). "New drains materialize members in dependency
  order", and dependencies on beads outside the manifest "represent genuine waits"
  (`gascity@v1.4.2:engdocs/drain-fanout.md:117-122`). The best-effort window noted at `:124-131`
  applies only to legacy manifests. *Documented.*
- **E19.** **Convoy-first graph.v2 does not close or restamp the source bead.** The design says: "Do
  not stamp graph.v2 workflow roots with `gc.source_bead_id`… Do not auto-close the input convoy or
  the original source bead"
  (`gascity@v1.4.2:engdocs/design/convoy-first-formulas-and-drain-v0.md:61-70`). Membership uses the
  canonical `tracks` relation (`:129-130`). The source-chain closure that finalize performs only
  walks `gc.source_bead_id` (`internal/dispatch/runtime.go:820-829`, `:875-884`). A Beadhive member
  that is tracked but not stamped is therefore not closed by GC. *Documented and source-read.*
- **E20.** **Some GC paths would take over Beadhive lifecycle.** Each must be excluded by
  construction:
  - The default sling query is `bd update {} --assignee=<name>` or `--label=pool:<name>`
    (`engdocs/architecture/dispatch.md:24-28`).
  - Auto-convoy sets the routed bead's `ParentID` to the convoy, which re-parents it
    (`dispatch.md:107-109`).
  - `--reassign` "clear[s] any existing assignee and reopen[s] it if a prior actor left it
    in_progress" (`internal/sling/sling_core.go:131-142`).
  - Orphan release "reopens the work" assigned to a dead session (`health-patrol.md:189-191`).
- **E21.** **GC claims are ordinary Beads leases.** GC workers claim with `bd update <id> --claim`
  (`gascity@v1.4.2:internal/beads/bdstore.go:1372-1378`). A GC-held lease is exactly the kind that
  the unfiltered reclaim in E5 would revert.
- **E22.** **GC formulas create worktrees of their own when authored to.** The core pack's
  `mol-scoped-work` runs `git worktree add "$WORKTREE_PATH" --detach` under
  `$(pwd)/worktrees/<bead>` and stamps `metadata.work_dir`
  (`gascity@v1.4.2:internal/bootstrap/packs/core/formulas/mol-scoped-work.toml:99-104`). This
  violates E8's single-manager rule unless the item formula never does it.
- **E23.** **Sessions can run a bounded job and exit.** `lifecycle = "one_shot"` means "the command
  is expected to do bounded work and exit cleanly" (`internal/config/config.go:3064-3067`).
  `mol-do-work` asks the agent to `gc bd heartbeat` the work bead
  (`internal/bootstrap/packs/core/formulas/mol-do-work.toml:47`). *Source-read.*
- **E24.** **GC writes beads to the store it is pointed at.** Start-up runs `ensureBeadsProvider()`
  ("start dolt server if bd backend") and `initAllRigBeads()` (per-rig `.beads/` databases)
  (`controller.md:69-71`). Running work writes convoy, workflow-root, step, control, session, and
  order-tracking beads (E17 and E11), and wisp GC purges closed molecules (`controller.md:119-121`).
  **Whether any of this may target Beadhive's managed per-hive `bd serve` is `bh-iaubc.1`'s
  question, not this one.**

### D. Status updates to `bh-d75bi`'s premises

- **E25.** **The write fence works again.** `bh-d75bi` Amendment 1's contract 3 says "`bh-tfapu` says
  that fence is currently INOPERABLE". `bh-tfapu` has since **closed** (merged
  2026-09-13). The fence is no longer the blocker. Whether a second writer is safe is still
  `bh-iaubc`'s question.

### Mode comparison, row by row

| Concern | Mode 1 — Beadhive loop + GC hosts sessions | Mode 2 — GC controller owns a delegated molecule |
|---|---|---|
| **Claims** | Beadhive (`bh work next --epic`, E6). GC does not claim Beadhive beads, but it needs a configured agent template per seat (E13). | Two different beads, one owner each. GC claims its *own* step bead (`gc hook --claim`, E21). A Beadhive-owned wrapper inside the item run claims the *member* bead through `bh work claim`. Beadhive's loop must stand down; today nothing stops it (E5, E6). |
| **Desired vs running** | **Two reconcilers over one seat process.** Beadhive has its in-flight map, orphan reaping, and wall-time cap (E4, E5). GC has start-missing, kill-orphan, restart-crashed, idle, and drift (E11, E12). Neither can be switched off for these sessions (E12). | GC's session reconciler alone, over sessions GC's formula created. Beadhive's loop has no in-flight entries for the molecule. |
| **Session start / recovery** | Conflicting. GC restarts on its own crash policy, with counts held in memory (E12). Beadhive reaps, classifies, and re-dispatches, with counts derived from event beads (E1, E4). Retries become invisible to Beadhive's derived counts. | GC starts and restarts the item session. Each attempt runs the wrapper, which re-claims idempotently as a stable actor and re-dispatches a fresh seat turn against the same worktree, because the branch is the checkpoint (E4). The wrapper writes the attempt and cause event beads, so derived counts stay truthful. |
| **Workspace / worktree** | GC fixes the session work dir from the template (E13). The seat's `--workspace` cannot be passed. | Beadhive's `WorktreeManager`, via the wrapper's `bh work claim` (E8). The item formula must never create a worktree (E22). |
| **Structured `SeatRun`** | **Lost.** Subprocess output goes to `/dev/null` (E14), exec is fire-and-forget (E15), and tmux offers scrollback scraping at best. `classify_run` has nothing to read. | Kept. The wrapper spawns the seat through the existing `role_process`/`spawn_seat` discipline (pgid, pipe held, run journal) and classifies stdout-first. It then writes the outcome to the Beadhive bead (verbs, `record_cause`) and to the GC step bead (`gc.outcome`). |
| **Cancellation** | **Unsafe by default.** Interrupt is SIGINT to the group (E14), the opposite of E3. Rungs 1 and 2 need stream-json stdin, and GC offers only `nudge` text. | Two tiers, nested rather than peers. GC ends the session (close/stop). The wrapper translates SIGINT and SIGTERM into the Beadhive ladder (SIGTERM, never SIGINT) on the seat's own group. An owner-death watchdog (`process_watchdog.py`) reaps the seat if GC SIGKILLs the wrapper. |
| **`WorkspaceBinding` fit (roadmap note)** | **No.** Hosting is process ownership, and the binding slot has no process verbs (E8). The precedent that does host processes, Herdr (E9), works because it has no reconciler. GC does (E11). | Not applicable. GC's dashboard could at most be a read-only presenter of delegated runs. |
| **Host / plugin boundary** | A fourth `work.runtime` value would be a core `Literal` change, not a plugin (E10). | Not a runtime tier: the runtime stands down for the molecule. `bh plugin gc` can own the capability, config, readiness, and presentation. It cannot own `gc` installation (`bh-h441d`) or start the GC supervisor (a host service). (E10) |
| **Shared-store dependency** | Session beads are written wherever GC's store is (E24). | **Hard dependency:** convoy, workflow, step, and control beads must `tracks` Beadhive members (E19, E24). This depends on `bh-iaubc.1`/`.2`. |

## Verdict — **GO**

**GO for Mode 2 only, and only on condition. Mode 1 is NO-GO.**

**Mode 1 is not safe.** GC has no hosting-only surface. Every GC session is a desired-state object
that GC's own reconciler starts, restarts, and kills (E11, E12), so Beadhive's loop and GC would
be two reconcilers over the same seat process. That is `bh-d75bi` blocker 1, moved from the bead
store to the session layer, and here it cannot be partitioned away. Beyond that, GC cannot receive
the exact per-bead argv (E13), subprocess drops `SeatRun` stdout (E14), and interrupt is SIGINT
(E14). GC session hosting also cannot be expressed as another `WorkspaceBinding` (E8, E9).

**Mode 2 can be single-owner.** Ownership is partitioned by lifecycle state and by bead kind,
which is `bh-d75bi` Amendment 1's shape. GC's own mechanisms support the partition: the
orchestrator owns control beads (E17), drain follows member dependencies (E18), and convoy-first
graph.v2 never closes or restamps the source bead (E19). The concrete enablers are:

- a Beadhive stand-down marker that every claim path, and the loop's reclaim, honors (E5, E6);
- a Beadhive wrapper as the GC item command, which keeps claim, `SeatRun`, CANCEL, and worktree
  on Beadhive's side (E3, E4, E8);
- keeping four named GC paths away from Beadhive beads (E20, E22).

Under Mode 2, each concern has exactly one owner:

| Concern | Owner |
|---|---|
| Dispatch: which member runs when, concurrency, item retry | **GC controller** (drain + control dispatcher), for the delegated molecule, from kickoff to submit |
| Session reconciliation: start, restart, and stop of the hosting session | **GC controller** (session reconciler) |
| Bead lifecycle: member claim, causes and event beads, submit, review, bounce, merge, close | **Beadhive**, through `bh work` verbs over Beads. GC writes lifecycle fields only on its own convoy, workflow, step, control, and session beads. |

**Condition.** Mode 2 writes GC beads that must track Beadhive members (E24). It therefore cannot
be implemented until `bh-iaubc.2` names a store topology in which that is safe. If `bh-iaubc`
finds that GC must use a separate store, and cross-store `tracks` membership is unsupported, this
GO lapses to NO-GO. It is not re-decided here.

## Recommendation

1. **Close Mode 1 as NO-GO in `bh-kp64s.2`.** Keep `work.runtime` at `claude | local | temporal`.
   Mode 2 is not a fourth tier, so `bh-d75bi`'s "keep the Literal open" condition is honored
   without adding a value. The useful part of Mode 1 is RPP's shape as Beadhive's own
   seat-substrate protocol, which belongs to the existing harvest bead `bh-e7rju` (Beadhive calling
   RPP-shaped executables directly, never through `gc`). Do not treat it as a GC runtime. For
   `bh-ym56t`, record that GC-hosted Herdr is a provider below GC's reconciler and so is not a
   Beadhive presenter (E16).
2. **Record Mode 2's single-owner model in `bh-kp64s.2` as conditional GO**, blocked on
   `bh-iaubc.2`. Ownership transfers only at gates Beadhive already has:
   - **kickoff → GC.** The molecule is marked delegated.
   - **submit → Beadhive.** The seat's `bh work submit` opens the review gate, the member leaves
     ready, and GC's step closes `pass`.
   - **A bounce ends delegation for that member.** Beadhive's loop owns `resume`. GC is never
     asked to re-dispatch a changes-requested bead, so no bead ever has two dispatch owners at
     once.
3. **The adapter boundary is narrow: three seams, all post-cutover.**
   - **Stand-down (Beadhive core).** Add a closed label dimension for dispatch ownership, such as
     `dispatch-owner:gc` (name to settle in replan). Honor it in:
     - the `work.dispatch.poll` read (`DispatchCommands.poll_ready` and `dispatch_state`);
     - `bh work next` and `claim`, which refuse any actor other than the delegated identity;
     - the local loop's decide step;
     - loop step 2's reclaim (`--exclude-label`).

     Add a `bh doctor` row that lists delegated molecules.
   - **Item wrapper (Beadhive, a `bh role` subcommand).** This is the only command GC runs. It:
     - takes one member id and a stable molecule-scoped actor;
     - runs `bh work claim` (worktree via the one `WorktreeManager`);
     - spawns the seat through `spawn_seat`/`role_process` with the run journal, and heartbeats it;
     - runs `classify_run`;
     - writes the Beadhive outcome (verbs, `record_cause`) and the GC step `gc.outcome`, failing
       closed;
     - translates SIGINT and SIGTERM into the CANCEL ladder, never SIGINT;
     - runs under an owner-death watchdog.
   - **`bh plugin gc` (plugin kernel v1).** Build it only after the kernel's runtime-port
     migration lands (E10). Its manifest declares:
     - executables `gc >=1.4.2,<1.5.0` and `bd`;
     - config `plugins.gc` (city, rig, supervisor URL);
     - one capability for molecule dispatch delegation;
     - CLI presentation for `status`, `delegate`, and `release`, where `release` refuses while
       GC-held work is in flight.

     It installs nothing (`bh-h441d` owns `gc`, `tmux`, `jq`, and `flock` provisioning) and
     never starts the GC supervisor.
4. **The GC-side artifact is config, coordinated with `bh-lko4x`:**
   - an agent template with `lifecycle = "one_shot"` whose command is the item wrapper;
   - a graph.v2 drain formula targeting an **explicit** convoy (`tracks`, `--no-convoy`), with
     `member_access = "read"`, an explicit `max_units`, and `on_item_failure = "continue"`.

   It must never use `--reassign`, auto-convoy, `gc.source_bead_id` stamping, or worktree-creating
   steps on Beadhive beads (E20, E22).
5. **First implementation slice**, filed only after `bh-iaubc.2` returns GO and after the
   `bh-sy36q` cutover. It covers the stand-down marker, the item wrapper, and a hermetic
   conformance suite against a fake GC, with no `gc` install. The suite must prove four things:
   - no Beadhive claim or reclaim path touches a delegated bead;
   - the wrapper classifies and records `blocked`, `handoff`, and failure without trusting exit 0;
   - a SIGKILLed wrapper leaves no live seat;
   - a re-run after a crash re-claims idempotently and costs a fresh turn, not a second claim.

   `bh plugin gc` and the pack follow as the second slice. The opt-in end-to-end probe against a
   disposable city is gated on `bh-iaubc`'s store answer.
6. **Measurements to take in that probe, which this spike could not make:**
   - how GC's reconciler treats a non-zero exit from a `one_shot` session (restart count before
     quarantine) (E12, E23);
   - the drain dependency-wiring latency for separate-context items (E18);
   - whether the GC session's SIGTERM grace outlives the wrapper's cooperative-cancel rung (E14).
