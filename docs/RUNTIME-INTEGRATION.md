# Runtime integration guide — Gas City as a `work.runtime`

How a process host such as Gas City (GC) plugs into BeadHive's dispatcher loop, how it proves it
does so correctly, and what to do when it cannot. Decisions and rationale live in the
[Gas City runtime seam ADR](design/gas-city-runtime-seam-adr.md); this guide is the how-to.
Reference detail for each piece is in [WORK.md](WORK.md#the-runtime-seam).

## Choosing a mode

There are two supported integration modes, and the conformance kit makes the choice (ADR
Decision 6).

```text
GC runtime passes beadhive.runtime.conformance?
  ├─ yes → Mode 1: declare a beadhive.runtimes entry point "gascity"; hive sets work.runtime: gascity
  └─ no  → which kit check failed?
        ├─ GC-fixable (argv mapping, outcome sink, idempotence) → fix and re-run
        └─ structural (lifecycle writes, session_id lost on restart) → Mode 2
```

| | Mode 1: BH loop, GC process host | Mode 2: delegated molecule, GC formula controller |
|---|---|---|
| Who picks and claims work | BH loop | GC, for members labelled `dispatch-owner:gc` |
| Who hosts the process | GC runtime behind `schedule()` / `observe()` | GC session running one `bh role` item wrapper |
| Outcome path | outcome sink in bead metadata | wrapper stdout `SeatRun` |
| Gates, bounce, merge | BH | BH (a bounce returns the member to BH) |
| Hive loop mode | `work.dispatch.loop: bh` | `work.dispatch.loop: gc` |

Mode 1 is the default target. Mode 2 is the fallback when the kit reports a structural failure.
BH's side of the stand-down is built: `work.dispatch.loop: bh | gc` and the `dispatch-owner:gc`
member label are read by one predicate (see
[`work.dispatch.loop`](CONFIGURATION.md#workdispatchloop--who-owns-the-dispatcher-loop)). The rest
of the Mode 2 BH-side work, the `bh plugin gc` integration, is not filed. It is planned on demand
if a GC runtime lands on the Mode 2 side of the tree, so a Mode 1 success leaves nothing
speculative behind.

## Mode 1: plug a runtime into the loop

`bh work loop` drives every seat through the hive's `work.runtime`: it resolves the runtime once
at start, calls `schedule()` for each dispatch, `observe()` for each in-flight seat and
`on_gate_resolved()` for each gate it sees resolve. A runtime that cannot drive the loop fails the
loop start with a `runtime_unavailable` line and never falls back to another runtime. That covers
an unregistered name, `temporal` (not implemented), `claude` (the Task-tool fanout, which has
nothing for the loop to schedule) and a factory that returns something that is not a `Runtime`.
`--seat-binary` and `--baml-required` configure the `local` runtime's launch, so a plugin runtime
refuses them (`runtime_conflict`) rather than dropping them.

### From `pip install` to a first loop pass

1. **Install the package** that provides the runtime, into the environment of the `bh` that is on
   `PATH`: the loop claims each bead through a `bh work next` subprocess found on `PATH`, so a
   plugin installed anywhere else is invisible to it. For example
   `uv tool install --with gc-runtime beadhive` (or `pip install gc-runtime` into that same
   environment), where `gc-runtime` stands for your package.
2. **Declare the entry point** in that package's `pyproject.toml`. The entry-point *name* is the
   `work.runtime` value it provides:

   ```toml
   [project.entry-points."beadhive.runtimes"]
   gascity = "gc_runtime.runtime:register"
   ```

   The object is either a `register()` callable that takes no required arguments and calls
   `register_runtime("gascity", factory)` itself, or a `(cfg, entry)` factory that bh registers
   under the entry-point name:

   ```python
   from beadhive.runtime import register_runtime

   def make_gascity(cfg, entry):          # the loaded config and hive entry, either may be None
       return GasCityRuntime(cfg, entry)  # your Runtime implementation

   def register():
       register_runtime("gascity", make_gascity)
   ```

   A package may also call `register_runtime` at import, but then something must import it
   first. The entry point is what makes the runtime selectable by name.
3. **Select it per hive** (or for the host):

   Add a `work:` block to the hive's **existing** `managed_repos` entry; do not add a new entry,
   because an entry without its `provider`/`org`/`repo` triplet is malformed:

   ```yaml
   managed_repos:
     - provider: github      # your existing entry's triplet, unchanged
       org: acme
       repo: tool
       prefix: tl
       work:               # <- the only addition
         runtime: gascity  # this hive only; `work.runtime` at the top level sets the host default
   ```

   `bh config set work.runtime gascity` is the host-level form.
4. **Check it resolves.** Run `bh hive ready` from inside the hive checkout (it takes no hive
   argument). It reports `work.runtime` as missing, naming the runtime
   and the entry-point group, when it does not.
5. **Run the conformance kit** statically, then with the loop probe (see below). Both need
   nothing from BeadHive's own fixtures.
6. **Run a first loop pass:** `bh work loop <epic>`. The first pass reclaims, adopts any live
   sessions an earlier loop left (adoptable runtimes only), then dispatches.

### How `bh` finds the runtime

`register_runtime(name, factory, replace=False)` raises on a second registration of a name unless
`replace=True`. Nothing is imported eagerly: when a name is not registered, `get_runtime` imports
**only** the `beadhive.runtimes` entry point whose name equals `work.runtime`, and no other entry
point in the group. A failure names the runtime and the group and is one of: not installed,
import error, name mismatch (the `register()` ran but did not register that name), or not
callable. The built-ins `local`, `claude` and `temporal` are registered by `bh` itself. `local`
is deliberately not an entry point, so the default runtime never depends on installed package
metadata.

**Syntax at load, strict at use.** Config load checks only that `work.runtime` is a well-formed
name (letters, digits, `_`, `.`, `-`, starting alphanumeric). It does **not** consult the
registry, so a hive that selects `gascity` still loads on a host that lacks the package. The
registry check, which loads the entry point, runs where the value is used: `bh work loop`,
`bh config set`, `bh config validate`, `bh hive ready` and `bh doctor`. An unregistered name
fails closed there, with the list of registered names.

> **The published schema still lists only built-ins.** The published config schema (contract v1
> line) enumerates only `claude`, `local` and `temporal` for `work.runtime`, and its description
> of the key is stale (it says the registry is checked when the config loads). A plugin name such
> as `gascity` is accepted by `bh` itself, but an external validator of the published schema
> rejects it until the next contract major. That follow-up is tracked as `bh-esnpc`. It is a
> next-major item, not a blocker for Mode 1.

### The `Runtime` protocol

A `name` attribute and three operations, plus optional capabilities
([WORK.md](WORK.md#the-runtime-seam)):

- `schedule(bead_id, role, *, workspace, instructions, session_id, model=None, decision=None)
  -> RoleHandle` starts the `bh-<role>` seat for the bead. GC maps these arguments to its own
  session shape.
- `observe(handle) -> RoleOutcome` reports what is known now: `running`, `done`, `blocked`,
  `handoff` or `failed`. It is a status read, never a wait, and never authoritative about bead
  lifecycle: bead state wins.
- `on_gate_resolved(resolution: GateResolution)` is called for each review gate the loop sees
  resolve, with `GateResolution(gate_id, bead_id, verdict, feedback_ref)`. `verdict` is `approved`
  or `changes_requested`, and `feedback_ref` is set only for a bounce. Delivery is at-least-once,
  so the hook must be idempotent. It can wind a session down, or keep one warm and attach the
  feedback. It must not re-dispatch: the loop's `resume-changes-requested` decision calls
  `schedule()` again.

Optional capabilities are detected per runtime, so a runtime that implements only the three
operations is valid and passes the kit:

- `cancel(handle) -> RoleOutcome | None` stops a run (the wall-time cap, a lease-loss shutdown).
  Without it the loop degrades instead of failing. A non-adoptable runtime gets the cause written
  and its claim released, with the session left to the runtime. An adoptable runtime's bead stays
  in flight and its claim is left to the lease, with one warning, so the bead is never started
  twice.
- `notify(handle, message) -> bool` delivers a one-line message to a live run.
- `close()` releases the runtime's own resources. The loop calls it on **every** shutdown, so an
  adoptable runtime's `close()` must not stop its sessions.
- `adoptable = True` opts in to restart adoption, below.

If `observe()` or `cancel()` raises, the loop logs it and retries, rather than crashing the pass.
The limit, `LocalLoop(runtime_error_limit=...)`, defaults to 3 and counts consecutive raising
*calls* for a bead, not passes. The third raising `observe()` answers `failed`. After the limit
on `cancel`, an adoptable runtime's cap is treated as unenforceable and is not retried.

### Restart adoption (adoptable runtimes)

A runtime whose sessions outlive the loop sets `adoptable = True`. The loop (never the runtime)
then writes a `bh.runtime.handle` document on the bead after each `schedule()` and removes it
when the turn is harvested. A restarted loop reclaims, then adopts: it calls `observe()` once for
each `in_progress` bead it owns whose handle names the runtime, and a `running` answer puts the
bead back in flight with no second `schedule()`. Details are in
[WORK.md](WORK.md#persisted-runtime-handles-and-restart-adoption--bhruntimehandle). What a
runtime author needs to know:

- **A plain stop leaves the sessions running.** Do not end them in `close()`.
- **Adoption runs after reclaim**, so a bead `bd reclaim` has already reverted is dispatched
  again under a fresh session id, and the old session's late outcome record never answers for it.
- **Lease expiry wins, and it means the claim's `lease_expires_at` timestamp.** `bd reclaim`
  reverts a claim only after a grace window past expiry (its `--older-than` flag, a fixed
  10 minutes by default), so expiry and "reverted" are not the same moment.
- **An expired lease is never adopted, even inside that grace window.** A restarted loop checks
  `lease_expires_at` before it calls `observe()`. A session whose claim has expired is not
  observed, adopted, heartbeated or harvested, and its claim is not released. The loop logs
  `session_not_adopted_lease_expired` and leaves the bead to `bd reclaim`. Once reclaimed, the
  bead is dispatched again under a fresh session id. Expect that session to be left running in
  your runtime, and expect its late outcome record to be ignored.
- **Accepted gaps.** A session whose handle write failed (or whose loop crashed between
  `schedule()` and the write) is not adopted and can run alongside a re-dispatch. A loop that
  stops for good leaves its sessions to lease expiry. An adopted run's cause reason reads
  `exit None`, because an outcome-sink record carries no exit code.
- **No operator reclaim verb yet.** Nothing stops a manual reclaim of a bead with a live session.
  `bh-zaxga` adds `work.reclaim` with `--force`; until then, lease expiry is the protection.

### Foreground `bh role` always runs locally

Everything the loop dispatches goes through `runtime.schedule` and `runtime.observe`. The one
accepted exception is the foreground `bh role` (one seat in the operator's terminal, with live
forwarding, signals and the outcome writer), which spawns its child directly. It runs locally even
when `work.runtime` names a plugin runtime. A guard test pins exactly those two spawn callers.

### Ownership: process liveness only

A registered runtime owns spawning, restarting and reaping its own sessions, and nothing else. It
must not claim, assign or change the status of a bead, write a label, write a metadata key except
through the outcome sink, or resolve a gate. A session GC restarts is valid only if it answers
`observe()` for the **same** `session_id`. With `work.dispatch.loop: bh`, you must turn off GC's own
orphan sweep and gate sweep, and keep its formula dispatch off, for hive beads.

### The outcome sink

A GC session is not a child process of BH, and GC's subprocess provider discards stdout, so the
seat's `SeatRun` cannot be read off a pipe. The outcome sink writes it to the bead instead. Launch
the seat like this:

```sh
bh role developer --bead <id> --outcome-sink bead --session-id <session_id>
```

The reader is `beadhive.outcome.observe(handle)`: the classified record when present, `running`
when absent. The record schema, classification and stale-session rules are in
[WORK.md](WORK.md#outcome-sink-in-bead-metadata--bh-role---outcome-sink-bead). Things GC must
know:

- **The flag is on the `bh role` wrapper, not on the `bh-<seat>` binary.** The seat binaries do
  not gain `--outcome-sink`, so GC must launch seats through `bh role`, never the seat binary
  directly.
- **Session ids are write-once.** The record lives at `bh.outcome.<session_id>` and an existing
  key is never overwritten. A retry or restart of a turn needs a **fresh session id**, otherwise
  the first record (possibly a synthesized failed one) stays final. This is the one exception to
  the same-`session_id` rule above: it applies to a restarted *live* session answering
  `observe()`, not to re-running a finished turn.
- **`--outcome-sink bead` implies a foreground headless run.** It refuses `-d` and the hitch
  backend. The writer stamps the seat worktree's identity, and falls back to `$BH_DEV`, the git
  user, then `$USER` when no profile is stamped.
- **Wire release 2.36.0** added the optional `outcome_sink` and `session_id` parameters to the
  role wire operation. A GC client driving `bh role` over the wire needs a bh at or past that
  release.

### Host requirements

- **A third-party `close()` must not stop sessions of an adoptable runtime.** See the restart
  adoption notes above.
- **Minimum `bd` version: 1.3.0.** `bd update --claim` is an atomic, actor-idempotent
  compare-and-set from 1.3.0, and BH treats a non-zero `--claim` exit as a clean "lost the race",
  so a second driver such as GC may claim from the same store. An older `bd` is refused by name.
  An unparseable version (a dev build) is allowed.
- **HQ outside `BH_HOME`.** A runtime that keeps HQ elsewhere sets `hq.path` (or `BH_HQ`, which
  wins). See [CONFIGURATION.md](CONFIGURATION.md) for what follows it.

## Run the conformance kit

Passing the kit is the GO condition for documenting a GC runtime as supported. The runtime itself
lives in GC's repository; the kit ships in `beadhive.runtime.conformance` and needs no BeadHive
fixture. Run it in two steps. The **static run** calls the runtime directly. From the command
line, with a factory `package.module:callable` that takes a `RuntimeEnv` (not the `(cfg, entry)`
registry factory) and returns a `Runtime`:

```sh
python -m beadhive.runtime.conformance gc_runtime.conformance:make_runtime
```

Then repeat it with the **loop probe**, which drives one turn through BeadHive's real dispatcher
loop. A Mode 1 GO should be earned with both:

```sh
python -m beadhive.runtime.conformance gc_runtime.conformance:make_runtime --loop
```

The same probe is `run_conformance(factory, loop=True)` from Python, or `loop_probe = True` on a
`RuntimeConformanceSuite` subclass under pytest (implement `make_runtime(self, env)`). Exit status
is 0 only when every check passed. `--timeout` bounds each check (default 60 seconds). See
[WORK.md](WORK.md#the-conformance-kit--beadhiveruntimeconformance).
`env.seat_command` is a `work.dispatch.seat_command` template for a stand-in seat that the runtime
must launch with the settled `bh-<role>` argv.

### Reading the verdict

The runner prints a PASS or FAIL line per check, then one verdict:

| Verdict | Meaning | Next step |
|---|---|---|
| `GO` | Every check passed. | Mode 1: register the runtime and set `work.runtime`. |
| `NOT YET` | One or more checks failed, and every failure is GC-fixable. | Fix the runtime and re-run. |
| `INCONCLUSIVE` | At least one failure is **BH-side** (`bh-loop`: BeadHive's own loop mishandled a turn the runtime ran correctly) and none is structural. GC-fixable failures may be present too. | The BH-side failure is not the runtime's to fix: report it to BeadHive and re-run. Fix any GC-fixable failures as for `NOT YET`. |
| `NO-GO` | At least one structural check failed (`same-session-id` or `no-lifecycle-writes`). | **This routes GC to Mode 2.** |

Each failure names the ADR invariant it broke and its fix class: GC-fixable, structural or
BH-side. A structural failure wins over a BH-side one, so a run with both reads `NO-GO`. The six
checks and the loop probe's invariants, with their classes, are tabulated in
[WORK.md](WORK.md#the-conformance-kit--beadhiveruntimeconformance). The
outcome sink round trip is not one of them: it exercises BH's own writer and reader, never the
runtime, so it is tested in BH's suite and does not affect the verdict. A crash inside
the runtime or its factory is reported as a GC-fixable failure, so it reads as `NOT YET`, never
`NO-GO`.

### Which hive the verdict came from

By default, when `bd` is on `PATH`, the kit runs against a `BdHive`: a real scratch beads store
whose snapshots read status, assignee, labels, metadata and gates from `bd` itself. That is what
lets `no-lifecycle-writes` catch a runtime that runs `bd update --claim`, `bd label add` or
`bd gate resolve`.

**The loop probe needs `bd` on `PATH`.** It seeds a scratch bd store and, while it runs, points
`BH_HOME` and `BH_CONFIG` at a throwaway config that reads beads over the `cli` route and unsets
the shared-server environment variables, then restores them all afterwards. On a `FileHive` it
fails as `bh-loop`, and the pytest suite skips it.

Without `bd`, the static kit falls back to a JSON `FileHive`, and a clean run prints `GO (FileHive:
bd-level lifecycle writes NOT exercised)`. **A GO on FileHive does not exercise bd-level
lifecycle writes**: a runtime never touches the hive's JSON files, so the check that separates
Mode 1 from Mode 2 cannot fail there. Do not treat that verdict as the Mode 1 GO. Re-run where
`bd` is available. To run over your own store, implement the `DisposableHive` protocol.

## Mode 2: the fallback

On a `NO-GO`, GC works the hive's molecule as a formula controller instead (ADR Decision 6):

- BH hands a whole molecule's ready members to GC. A `dispatch-owner:gc` label on a member makes
  BH stand down: the picker, `bh work next` and the loop's reclaim skip that member. Lifecycle
  verbs (`bh work claim`, `submit`, `review`, `merge`, gate resolution) still work on it.
- GC runs exactly one BH command per member, a `bh role` item wrapper, as a one-shot session. The
  wrapper holds the pipes and emits `SeatRun` on stdout, so no outcome sink is needed.
- GC works the hive's beads directly in the shared store, or `tracks` them from its own city. The
  hive declares `work.dispatch.loop: gc` so BH stands down from the concerns GC's loop owns.
- Ownership returns to BH at `submit`. Review, merge and gates stay BH's, and a bounce ends
  delegation for that member.

## Further reading

- [Gas City runtime seam ADR](design/gas-city-runtime-seam-adr.md): decisions, the Mode 2
  contract, the loop-ownership table.
- [Runtime tiers ADR](design/work-runtime-tiers-adr.md): the invariants every tier preserves.
- [Three-way comparison](design/gas-frameworks-comparison.md): where Gas City and BeadHive
  overlap.
- [bh-kp64s.1 spike](spikes/bh-kp64s.1-gc-runtime-adapter.md): the Mode 1 NO-GO this seam
  resolves.
