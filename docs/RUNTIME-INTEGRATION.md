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
  ├─ yes → Mode 1: register_runtime("gascity", …); hive sets work.runtime: gascity
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

## Mode 1: register a runtime

A plugin registers a factory by name. Nothing is auto-discovered.

```python
from beadhive.runtime import register_runtime

def make_gascity(cfg, entry):          # the loaded config and hive entry, either may be None
    return GasCityRuntime(cfg, entry)  # your Runtime implementation

register_runtime("gascity", make_gascity)
```

`register_runtime(name, factory, replace=False)` raises on a second registration of a name unless
`replace=True`. Then the hive selects it:

```yaml
work:
  runtime: gascity
```

`bh` validates `work.runtime` against the registry when the config loads and in `bh config set`,
and an unregistered name fails closed with the list of available names.

> **The published schema still lists only built-ins.** The published config schema (contract v1
> line) enumerates only `claude`, `local` and `temporal` for `work.runtime`. A plugin name such as
> `gascity` is accepted by `bh` itself, but an external validator of the published schema rejects
> it until the next contract major. That follow-up is tracked as `bh-esnpc`.

### The `Runtime` protocol

Three operations, and nothing else ([WORK.md](WORK.md#the-runtime-seam)):

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

### Ownership: process liveness only

A registered runtime owns spawning, restarting and reaping its own sessions, and nothing else. It
must not claim, assign or change the status of a bead, write a label, write a metadata key except
through the outcome sink, or resolve a gate. A session GC restarts is valid only if it answers
`observe()` for the **same** `session_id`. With `work.dispatch.loop: bh`, GC's own orphan sweep,
gate sweep and formula dispatch stay off for hive beads.

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

- **Minimum `bd` version: 1.3.0.** `bd update --claim` is an atomic, actor-idempotent
  compare-and-set from 1.3.0, and BH treats a non-zero `--claim` exit as a clean "lost the race",
  so a second driver such as GC may claim from the same store. An older `bd` is refused by name.
  An unparseable version (a dev build) is allowed.
- **HQ outside `BH_HOME`.** A runtime that keeps HQ elsewhere sets `hq.path` (or `BH_HQ`, which
  wins). See [CONFIGURATION.md](CONFIGURATION.md) for what follows it.

## Run the conformance kit

Passing the kit is the GO condition for documenting a GC runtime as supported. The runtime itself
lives in GC's repository; the kit ships in `beadhive.runtime.conformance` and needs no BeadHive
fixture. From the command line, with a factory `package.module:callable` that takes a
`RuntimeEnv` (not the `(cfg, entry)` registry factory) and returns a `Runtime`:

```sh
python -m beadhive.runtime.conformance gc_runtime.conformance:make_runtime
```

Exit status is 0 only on GO. `--timeout` bounds each check (default 60 seconds). The same suite
runs under pytest by subclassing `RuntimeConformanceSuite` and implementing
`make_runtime(self, env)`; see [WORK.md](WORK.md#the-conformance-kit--beadhiveruntimeconformance).
`env.seat_command` is a `work.dispatch.seat_command` template for a stand-in seat that the runtime
must launch with the settled `bh-<role>` argv.

### Reading the verdict

The runner prints a PASS or FAIL line per check, then one verdict:

| Verdict | Meaning | Next step |
|---|---|---|
| `GO` | Every check passed. | Mode 1: register the runtime and set `work.runtime`. |
| `NOT YET` | One or more checks failed, and every failure is GC-fixable. | Fix the runtime and re-run. |
| `NO-GO` | At least one structural check failed (`same-session-id` or `no-lifecycle-writes`). | **This routes GC to Mode 2.** |

Each failure names the ADR invariant it broke and its fix class. The six checks and their
classes are tabulated in [WORK.md](WORK.md#the-conformance-kit--beadhiveruntimeconformance). The
outcome sink round trip is not one of them: it exercises BH's own writer and reader, never the
runtime, so it is tested in BH's suite and does not affect the verdict. A crash inside
the runtime or its factory is reported as a GC-fixable failure, so it reads as `NOT YET`, never
`NO-GO`.

### Which hive the verdict came from

By default, when `bd` is on `PATH`, the kit runs against a `BdHive`: a real scratch beads store
whose snapshots read status, assignee, labels, metadata and gates from `bd` itself. That is what
lets `no-lifecycle-writes` catch a runtime that runs `bd update --claim`, `bd label add` or
`bd gate resolve`.

Without `bd`, the kit falls back to a JSON `FileHive`, and a clean run prints `GO (FileHive:
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
