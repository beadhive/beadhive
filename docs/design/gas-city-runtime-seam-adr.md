# Gas City runtime seam ADR — a registered `work.runtime`, outcome in bead metadata

**Status:** accepted and implemented (epic `bh-vjzki`); amended 2026-11-26 · **Date:** 2026-11-23 ·
**Supersedes:** the Mode 1 NO-GO verdict in
[bh-kp64s.1](../spikes/bh-kp64s.1-gc-runtime-adapter.md) (resolves decision bead `bh-kp64s.2`) ·
**Amends:** [work-runtime-tiers-adr.md](work-runtime-tiers-adr.md) (the "not a plugin framework"
clause of the `work.runtime` key) ·
**Related:** [gas-frameworks-comparison.md](gas-frameworks-comparison.md),
[bh-iaubc.1](../spikes/bh-iaubc.1-gc-beads-safety.md)
(shared-store hazards; reframed as loop ownership in Decision 7),
[bh-lko4x.1](../spikes/bh-lko4x.1-beadflow-gc-pack.md) (pack scope, unchanged),
[plugin-kernel-v1-adr.md](plugin-kernel-v1-adr.md), [roles-rbac-matrix.md](roles-rbac-matrix.md)

Lets Gas City (GC) act as the process host behind BeadHive's dispatcher loop by registering a
`Runtime` implementation. The BH loop still owns the bead lifecycle.

## Context

Spike `bh-kp64s.1` judged **Mode 1** ("the BH loop decides, GC hosts the seat sessions") to be
NO-GO. It found four blockers:

| # | Blocker (spike evidence) | Where it is fixed now |
|---|---|---|
| E11/E12 | Two reconcilers, GC's and BH's, both drive the seat process | Ownership split (Decision 4). GC owns process liveness, BH owns lifecycle. The conformance kit enforces the split. |
| E13 | GC's `SessionCreateBody` cannot take BH's per-bead argv | GC side. GC's runtime implementation maps `schedule()` arguments to its own session shape. |
| E14 | GC's subprocess provider sends stdout to `/dev/null`, so the SeatRun is lost | Outcome sink in bead metadata (Decision 2) |
| E15 | GC's exec provider is fire-and-forget | `observe()` reads the outcome record. It no longer waits on a child process (Decision 2). |

The GC team has since asked for the seam that removes those blockers. They asked for a registry
for `work.runtime` that works like the claim-authority registry, plus agreement on the
`schedule` / `observe` / `on_gate_resolved` semantics. They also asked for an outcome channel
for long-lived sessions that are not child processes, and for defined bounce and feedback points.
`src/beadhive/runtime/` already defines exactly that protocol. What's missing is the
registration mechanism, an outcome path that doesn't depend on a child process, and a real caller
for `on_gate_resolved`, which nothing invokes today.

The GC team also raised four smaller items: the claim CAS, a `dispatch status` KeyError, HQ
location, and start-refused recovery. Decision 5 covers them.

## Decisions

### 1. `work.runtime` becomes a name-keyed registry

`runtime/` gains `_RUNTIMES: dict[str, RuntimeFactory]`, `register_runtime(name, factory)`,
`available_runtimes()` and `get_runtime(cfg)`. It mirrors `claim_authority.py:455-490` exactly.
Lookup is by name. An unknown name raises `ValueError` listing the available names. The built-ins
`local`, `claude` and `temporal` register at import time.

**Amended 2026-11-26 (as implemented): entry-point loading.** A plugin becomes selectable in one
of two ways: it calls `register_runtime` itself, or it declares a `beadhive.runtimes` entry point
named after the runtime. On a registry miss, `get_runtime` imports **only** the entry point whose
name equals the requested `work.runtime`, and no other entry point in the group is imported.
The entry-point object is either a `register()` callable that calls `register_runtime` itself, or a
`(cfg, entry)` factory that bh registers under the entry-point name. A failure to load is a
distinct named error: not installed, import error, name mismatch or a non-callable object. So
nothing is *eagerly* auto-discovered, but a runtime package that is installed and declares the
entry point is found by name. Registering `local` through an entry point was considered and
deferred, because it would make the default runtime depend on installed package metadata. It is
a possible later cleanup.

**Amended 2026-11-26 (as implemented): syntax at load, strict at use.** The config schema relaxes
`work.runtime` from `Literal[...]` to a string. Config **load** checks only the name's syntax
(letters, digits, `_`, `.`, `-`, starting alphanumeric). The registry is **not** consulted at
load, so a hive's runtime choice cannot break config load on a host that lacks the package that
provides it. The strict registry check, which also loads the entry point, runs where the value is
used: `get_runtime` (and so `bh work loop`), `bh config set`, `bh config validate`, `bh hive
ready` and `bh doctor`. The relaxation is the one contract-schema change. It amends the "not a
plugin framework" clause of the tiers ADR, because a registry is not a framework. When the
plugin kernel's runtime port lands, it will call `register_runtime`, so the two stay
forward-compatible.

The hive selects its own runtime: `managed_repos[].work.runtime` overrides the host value.

### 2. Outcome sink: a SeatRun record in bead metadata

The seat binaries (`bh-<role>`) gain `--outcome-sink {stdout,bead}`. The default is `stdout`, so
today's behavior is unchanged. With `bead`, the seat writes the same SeatRun JSON it prints
today to the bead metadata key `bh.outcome.<session_id>`, and stamps it with the seat identity.

A shared helper, `outcome.read(bead_id, session_id) -> RoleOutcome | None`, reads the record back
through the existing `seatrun.classify_run`, so the classification rules stay identical. A runtime
whose sessions are not child processes implements `observe()` as "outcome record present ⇒
classified outcome; absent ⇒ `running`". A record with a different `session_id` is ignored, so a
stale session cannot answer for a new one.

Invariants carried over unchanged:

- `observe()` is never authoritative about bead lifecycle, and bead state still wins.
- A bare exit 0 is still never "success".
- The record is append-only per session and read-only to BH.

Implementation notes (bh-im4dm):

- The flag is on the `bh role <seat> --bead <id> --outcome-sink bead --session-id <sid>` wrapper.
  The `bh-<seat>` binaries do not gain it, so GC launches seats through `bh role`.
- Records are write-once per session id. A retry or restart uses a fresh session id, or the
  first record stays final.
- The role wire operation gained optional `outcome_sink` and `session_id` in wire release 2.36.0.

### 3. `on_gate_resolved` gets a real caller and carries bounce and feedback

The protocol changes from `on_gate_resolved(gate_id: str)` to
`on_gate_resolved(resolution: GateResolution)`. The new type is
`GateResolution(gate_id, bead_id, verdict: approved|changes_requested, feedback_ref: str | None)`.

The loop's existing `coordination.gate_check` pass calls it once for each gate it observes
resolving. Delivery is at-least-once, and implementations must be idempotent. Latency stays
bounded by `work.dispatch.poll_interval`. No push doorbell is added, as the tiers ADR's
Limitation 1 already decided.

These are the **bounce and feedback points.** A runtime can use the hook to wind a session down,
or to keep a long-lived session warm and attach `feedback_ref` to it. Re-dispatch of
changes-requested work stays **BH-owned**. The loop's `resume-changes-requested` decision calls
`schedule()` again, and a runtime must never re-dispatch on its own.

### 4. Ownership split, enforced by a conformance kit

A registered runtime owns **process liveness only**. That covers spawning, restarting and
reaping its own sessions. It must not:

- claim, assign or change status on a bead;
- write any label;
- write any metadata key except through the outcome sink;
- resolve a gate.

BH owns claim, reclaim, gates, bounce and retry. A session restarted by GC is valid only if it
answers `observe()` for the **same** `session_id`. BH's reclaim treats a runtime handle as live
until `observe()` returns a terminal outcome or the lease expires.

**Optional capabilities (as implemented).** The `Runtime` protocol is `name` plus the three
operations `schedule`, `observe` and `on_gate_resolved`. Anything more is an optional capability,
detected per runtime and never assumed, so a runtime that implements only the three still passes
the kit:

- `cancel(handle)` stops a run: the loop's wall-time cap and a lease-loss shutdown use it. A
  runtime without it **degrades, it does not fail**. For a non-adoptable runtime the loop writes
  the cause and releases the claim, and the session itself is left to the runtime. For an
  adoptable one the loop warns once, keeps the bead in flight and leaves its claim to the lease,
  because releasing it would let the bead start a second session.
- `notify(handle, message)` delivers a one-line message to a live run (sibling notification).
- `close()` releases the runtime's own resources. It is called on every loop shutdown.
- `adoptable = True` says the runtime's sessions outlive the loop that scheduled them (below).

**Handle persistence and adoption (as implemented).** For an `adoptable` runtime only, the *loop*
(never the runtime) writes one bead metadata document, `bh.runtime.handle`, after each
`schedule()`. Harvesting or cancelling the turn removes it. A restarted loop looks at the
molecule's `in_progress` beads claimed by its own actor whose handle names the same runtime, and
calls `observe()` once on each. `running` puts the bead back in flight with no second
`schedule()`. A terminal answer is harvested like any finished run. A plain shutdown (SIGTERM, a
restart) leaves an adoptable runtime's sessions alone, so an adoptable runtime's `close()` must
not end its sessions. `LocalRuntime` is not adoptable and gets no handle. The field list, the
removal rules and the no-double-cause guard are in
[WORK.md](../WORK.md#persisted-runtime-handles-and-restart-adoption--bhruntimehandle).

- **Adoption runs after reclaim, not before.** The `bh-fwm82` design text said adoption runs
  before reclaim. It runs after. Adopting first would put a bead whose lease has expired back in
  flight, and then reclaim would revert it underneath the loop. That leaves a stale in-flight entry
  and a possible double dispatch.
- **"Lease expiry wins" means the claim's `lease_expires_at` timestamp.** (Operator decision,
  2026-11-26.) It does **not** mean "whatever `bd reclaim` has reverted". A restarted loop must
  not adopt or heartbeat a claim whose `lease_expires_at` has passed. `bd reclaim` reverts a claim
  only after a grace window past expiry (about 10 minutes at the default 5-minute TTL), so the
  two readings differ inside that window.
- **Known gap, fixed in a follow-up.** The current code does **not** yet check
  `lease_expires_at` when it adopts. It relies on `bd reclaim` having reverted the claim, so a
  restarted loop adopts, and heartbeats, an expired lease while it is still inside reclaim's grace
  window. The behaviour ADR Decision 4 requires is the timestamp rule above. A follow-up bead
  makes the loop enforce it.
- **Accepted gaps.** A session whose handle write failed, or whose loop crashed between
  `schedule()` and the write, is not adopted. The loop logs the failed write and carries on, and
  the session can run alongside a re-dispatch of the same bead. A loop that is stopped for good
  leaves its sessions to lease expiry.
- **Accepted: an adopted run's cause reads `exit None`.** A run read from an outcome-sink record
  has no process to read an exit code from (Decision 2: records carry no exit code), so the cause
  reason of an adopted, metadata-backed run says `exit None`.
- **Transient-error policy.** A runtime whose `observe()` or `cancel()` raises is logged and
  retried instead of crashing the pass. The limit, `runtime_error_limit` (default 3), counts
  consecutive raising *calls* for one bead, not passes. The third raising `observe()` answers
  `failed`. Once an adoptable runtime's wall-time cap is known to be unenforceable (no `cancel`, or
  `cancel` raised up to the limit) it is warned about once and not retried. This applies to
  `LocalRuntime` too, which is a behaviour change: a raising `LocalRuntime.observe` used to crash
  the pass.
- **`runtime_handle.reclaim_refusal` has no caller.** There is no operator reclaim verb yet that
  refuses a bead with a live handle. A follow-up adds `work.reclaim` with `--force`, and until it
  lands, lease expiry is the only protection.

**One spawn path, with one accepted exception.** Everything the loop dispatches goes through
`runtime.schedule` and `runtime.observe`, and `LocalRuntime.schedule` is the only place the
dispatch tier starts a seat. The accepted exception is `role_process._run_foreground`, the
foreground `bh role`: one seat in the operator's terminal, with live output forwarding, signals
and the outcome writer, which spawns its child directly. It is the inside of one seat, not a
second scheduler. A guard test pins exactly these two callers. A foreground `bh role` therefore
always runs locally, even when `work.runtime` names a plugin runtime.

`beadhive.runtime.conformance` ships a contract suite that a third-party runtime runs against a
disposable hive. It covers:

- the shape of `schedule()`;
- `observe()` transitions;
- outcome-sink round-trip and stale-session rejection;
- `on_gate_resolved` idempotence;
- that the runtime makes no lifecycle writes. The suite diffs bead state around a full turn.

**Passing the kit is the GO condition** for any GC runtime being documented as supported. The GC
runtime itself lives in GC's repository, not in BeadHive.

**Loop probe (as implemented).** The checks above call the runtime directly. An opt-in loop probe
(`--loop`, `run_conformance(factory, loop=True)` or `loop_probe = True` on the pytest suite) also
drives one turn through BeadHive's real dispatcher loop against a scratch bd store. A Mode 1 GO
should be earned with the loop probe on, on a real bd store. A probe failure that is in
BeadHive's own loop is the **BH-side** fix class: it is BeadHive's to fix, not the runtime's, and
it makes the verdict `INCONCLUSIVE` instead of `NOT YET`. A structural failure still wins, so the
verdict is `NO-GO`.

In Mode 1 the BH loop is the dispatcher, so GC's own loop features stay off for hive beads. That
covers GC's orphan sweep, gate sweep and formula dispatch. Decision 7 makes this explicit, so it
is a declared configuration and not a convention someone has to remember.

### 5. Adjacent fixes requested by the GC team

- **Claim CAS.** On the host, bd 1.3.0 `--claim` already says it is atomic and idempotent for
  the same actor. BH conforms as follows:
  - A non-zero `--claim` exit is a clean "lost the race", not an error.
  - `claim_won` stays as an idempotence check.
  - The `_guard_not_other` pre-check stays advisory.
  - The stale "not a compare-and-swap" comments and docs are corrected.
  - A minimum bd version check is added, pinned to the release whose atomic claim is verified.
  - A real-service race test proves two drivers cannot both win.
- **`bh host dispatch status` KeyError 'provider'.** The cause is a fleet entry with no identity
  triplet hitting an unguarded `entry['provider']` index. The fix:
  - an `entry_triplet()` helper that raises a named error;
  - `compute_status_all` reports broken hives instead of silently dropping them;
  - a regression test.
- **HQ outside `BH_HOME`.** `$BH_HQ` already relocates HQ, through `config.hq_dir()` and
  `named_home`. The fix adds an equivalent `hq.path` config key and moves the four HQ-adjacent
  paths hard-wired under `BH_HOME` (HQ backups, fleet state, beadyard, the reclaim fallback).
- **Start-refused recovery.** The command is `bh host beads start`, not `bead`. The
  `ServiceUnavailable` refusal gains a remediation ladder (`status → stop → start`), and
  BEADS-SERVICE.md gets a recovery section. We will get the exact reproduction from the GC team.

### 6. Mode 2 is the documented fallback, and GC chooses with the kit

There are two supported integration modes. The GC team decides between them by running the
conformance kit against its runtime.

```text
GC runtime passes beadhive.runtime.conformance?
  ├─ yes → Mode 1: register_runtime("gascity", …) or a beadhive.runtimes entry point;
  │        hive sets work.runtime: gascity
  └─ no  → which kit check failed?
        ├─ fixable on the GC side (argv mapping, outcome sink, idempotence) → fix and re-run
        └─ structural (GC must reconcile bead lifecycle, can't preserve session_id
           across restart, can't keep sweeps off hive beads)  → Mode 2
```

**Mode 1: BH loop, GC process host.** This is the default target, defined by Decisions 1–4. The
BH loop picks work, claims, gates and bounces. GC only hosts sessions behind `schedule()` and
`observe()`. Each bead runs one seat turn. The work stays on the hive's beads, and the GC city has
no copy of it.

**Mode 2: delegated molecule, GC formula controller.** This is the conditional GO from
`bh-kp64s.1` (Recommendations 2–5), restated here so both modes sit in one document:

- **Hand-off.** BH hands a whole molecule's ready members to GC. A `dispatch-owner:gc` label on a
  member makes BH stand down: BH's poll, claim and reclaim all skip that member.
- **Single entry point.** GC runs exactly one BH command per member, a `bh role` item wrapper,
  as a one-shot session. The wrapper holds the pipes and emits SeatRun on stdout, so no outcome
  sink is needed.
- **One copy of the work.** GC works the hive's beads directly in the shared store, or `tracks`
  them from its own city. Either topology is allowed. What matters is the declared loop mode
  (Decision 7): the hive is set to `work.dispatch.loop: gc`, so BH stands down from the concerns
  GC's loop owns.
- **Returning ownership.** Ownership returns to BH at `submit`. Review, merge and gates stay BH's.
  A bounce (changes requested) ends delegation for that member, and BH re-dispatches it.
- **Open blockers.** Only the BH-side stand-down work in the table below. `bh-iaubc.2` is
  resolved: a shared store is acceptable once the loop mode declares who owns what.

| Concern | Mode 1 | Mode 2 |
|---|---|---|
| Who picks and claims work | BH loop | GC formula controller, for `dispatch-owner:gc` members |
| Who hosts the process | GC runtime | GC session running the `bh role` wrapper |
| Outcome path | outcome sink in bead metadata (Decision 2) | wrapper stdout SeatRun |
| Gates, bounce, merge | BH | BH. A bounce returns the member to BH. |
| BH-side work | this molecule | stand-down label in poll/claim/reclaim, `bh role` wrapper, `bh plugin gc` |
| Hive loop mode (Decision 7) | `work.dispatch.loop: bh` | `work.dispatch.loop: gc` |
| Blocked on | conformance kit (this molecule) | BH-side stand-down work (replanned on demand) |

The BH-side Mode 2 work is **not filed** with this molecule. If GC reports a structural kit
failure, it is filed through `/bh:replan` on this epic, so a Mode 1 success leaves no speculative
beads behind.

### 7. The hive declares which loop it runs, and BH says what it is not managing

Sharing a beads store with GC is **not unsafe in itself**. The hazards found in `bh-iaubc.1` were
two loops each assuming they own the same concern: GC's orphan sweep reverting BH claims, GC's gate
sweep resolving BH gates, and two reapers. The fix is to **declare an owner**, not to ban sharing.

A new hive config key, `work.dispatch.loop: bh | gc`, defaults to `bh`. Each concern has exactly
one owner per mode:

| Concern | `loop: bh` (Mode 1, or plain BH) | `loop: gc` (GC is the dispatcher loop) |
|---|---|---|
| Picking ready work and dispatch passes | BH loop | GC, and BH does not run a dispatch pass |
| Dispatcher lease | BH | GC, and BH does not take the lease |
| Reclaim / orphan sweep of in-flight claims | BH | GC, and BH does not reclaim GC-held claims |
| Process hosting | registered `work.runtime` (may be GC) | GC |
| Claim atomicity | bd `--claim` (both loops use it) | bd `--claim` |
| Submit, review gates, merge, bounce | BH | BH (sessions call `bh work` verbs) |
| Gate sweep | BH | BH, and GC's gate sweep must stay off for hive gates |

With `loop: gc`, BH does three things:

- **Status.** `bh host dispatch status` reports a distinct state, `delegated (loop: gc)`, which
  says plainly that BH is not managing dispatch, the lease or reclaim for that hive. It is not
  shown as `not_enabled` or `running`.
- **Refusals.** Loop-owning verbs refuse with a message naming the declared owner. These are
  `bh host dispatch enable` (the service install), `bh host dispatch run`, `bh work loop` and
  `bh work next`. There is no operator reclaim verb to refuse yet (see Decision 4).
- **Unchanged verbs.** Lifecycle verbs keep working exactly as before. These are claim, submit,
  review, merge and gates.

The per-member `dispatch-owner:gc` label from Mode 2 is the **molecule-scoped** form of the same
declaration. The hive key is the **hive-scoped** form. Both are read by the same stand-down
check.

## Not decided here (future work)

- **Reserved seat names as runtime identities.** BH does not enforce seat-name reservation today.
  Revisit alongside the RBAC matrix and claim-record signing if a runtime needs its own seat type.
- **Plugin-kernel discovery** of runtimes through the `beadhive.plugins.v1` entry points. This
  is decided, not open: runtimes are discovered through their own `beadhive.runtimes` entry-point
  group (Decision 1), and the plugin kernel is not used for it. Nothing in the kernel is waiting
  on this. If the kernel's runtime port lands, it will call `register_runtime`.
- **An operator reclaim verb.** A follow-up adds `work.reclaim` with `--force`, using
  `runtime_handle.reclaim_refusal` (Decision 4).
- **The published schema's `work.runtime` enum.** The published config-v1 schema still lists only
  `claude`, `local` and `temporal`, and its description of `work.runtime` is stale (it says the
  registry is checked at load). A closed enum cannot be removed within a contract major, so the
  fix, `bh-esnpc`, is a next-contract-major item. It does not block Mode 1: `bh` itself accepts a
  plugin name, and only an external validator of the published schema rejects it.

## Consequences

- GC becomes a supported dispatcher-loop process host as soon as its runtime passes the kit. If
  it fails for structural reasons, the documented fallback is Mode 2.
- `work.runtime` accepts any well-formed name at load, and an unregistered name still fails
  closed where the value is used (`bh work loop`, `bh config set`, `bh hive ready`, `bh doctor`).
- Known gaps, each with a follow-up: an expired lease can be adopted inside `bd reclaim`'s grace
  window, there is no operator reclaim verb, and the published schema description is stale.
- `work.dispatch.loop` makes "who runs the loop" explicit. Under `gc`, BH reports itself as
  delegated and refuses loop-owning verbs.
- `bh-kp64s.2` closes with this ADR. `gas-frameworks-comparison.md:44` ("not currently a runtime
  option") is updated.
