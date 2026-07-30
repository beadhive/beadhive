# Work-runtime tiers ADR — beads is the state machine, the runtime is only the scheduler

**Status:** proposed, **amended in place 2026-07-30** · **Date:** 2026-07-29 ·
**Supersedes:** nothing · **Amends:** no other ADR —
[Amendment 1](#amendment-1--the-contract-is-baml-harnesss-already-and-authority-bakes-into-the-binary)
below amends *this* one.
**Related:** [temporal-control-plane-adr.md](temporal-control-plane-adr.md) (tier 2's topology),
[bead-backend-abstraction.md](bead-backend-abstraction.md) (the `beads.engine` seam this mirrors),
[roles-rbac-matrix.md](roles-rbac-matrix.md) (the seats being scheduled)

Establishes the seam between *what is true about a bead* (beads) and *when a process wakes up to
act on it* (the runtime), and defines three runtime tiers that share one set of semantics.

> **Read [Amendment 1](#amendment-1--the-contract-is-baml-harnesss-already-and-authority-bakes-into-the-binary)
> before acting on Decisions 3 and 4.** Both were written before beadhive/baml-harness was read.
> The harness already returns a typed `SeatRun`/`RoleOutcome` that covers most of what Decision 4
> proposed to invent, and its bundle carries authority that Decision 3 left as a runtime argument.
> The amendment revises the contract in Decision 3, corrects Decision 4's either/or framing of exit
> codes, and records what remains unvalidated. Everything in Decisions 1 and 2 stands as filed.

---

## Context

AGF's lifecycle is driven from a terminal today: a human (or a Claude Code sub-agent tree) runs
`bh work` verbs, and the loop advances because somebody is sitting there. That works for one
person at one machine and stops working the moment nobody is watching.

The obvious fix — adopt a durable orchestration engine — has a failure mode we want to avoid
up front: making the engine a hard dependency. A solo developer on a free tier should not need
a Temporal server to run a bead to green, and a human on a laptop should be able to approve a
review gate whether or not any orchestrator is running.

### Evidence 1 — beads is already a weak durable-execution engine

An audit of the `bd` surface against Temporal's primitives found near-complete coverage of the
*semantics*, and exactly one gap:

| Durable-execution primitive | beads, today |
|---|---|
| blocking wait / signal | **`bd gate`** — types `human` · `timer` · `gh:run` · `gh:pr` · `bead`, with `create` / `add-waiter` / `check` / `resolve` |
| exclusive mutex with queue | **`bd merge-slot`** — `<prefix>-merge-slot` bead (`gt:slot`), `metadata.holder` + priority-ordered `metadata.waiters` |
| worker liveness heartbeat | **`bd heartbeat`** — refresh the lease on an issue held `in_progress` |
| liveness timeout → recovery | **`bd reclaim`** — revert stale-lease `in_progress` back to ready |
| event history | **`bd set-state`** (creates an event + updates the label) over Dolt history |
| child/parent execution tree | epic → children |
| scheduled wake-up | `bd gate --type=timer` |
| **process wake-up** | **absent.** `bd hooks` manages *git* hooks only; beads is pull-only, never push. |

Beads has the vocabulary. What it cannot do is *start a process* when state changes.

### Evidence 2 — that single gap is the only thing a runtime supplies

If the state machine already lives in beads, an orchestrator is not being asked to model the
lifecycle. It is being asked one much smaller question: **which processes should be running right
now, and what happens when one dies?** That is a scheduler, not a workflow engine, and it admits
implementations of wildly different weight.

### Evidence 3 — the repo already has this exact seam, once

`engine.py` established the pattern for swapping a bead backend, and deliberately framed its own
scope:

> *"Modeled on dolt.py's container-backend dispatch: a config key (`beads.engine`) selects a thin
> implementation, not a plugin framework."*

The runtime axis wants the same shape and the same restraint. Reusing it costs a config key and
buys consistency; inventing a second, richer extension mechanism for the same class of decision
would be the drift `engine.py` was written to avoid.

---

## Decision 1 — beads owns lifecycle state; the runtime owns process scheduling only

The seam is stated as an invariant, because it is the only thing that keeps the tiers
interchangeable:

> **No runtime-only lifecycle state.** Gates are resolved in beads. Leases are held in beads. The
> merge slot lives in beads. In every tier. A runtime MAY keep a richer *execution* record (retry
> counts, timings, the parent chain, why something was retried) but is NEVER authoritative about
> whether a bead is claimed, blocked, approved, or done.

Two consequences follow directly, and both are requirements, not side effects:

1. **`bh work approve` works with no runtime running.** A human resolves a gate against beads;
   whatever runtime is active observes the resolution on its next check. Human-in-the-loop never
   depends on infrastructure being up.
2. **Tiers are switchable mid-flight.** Because no tier holds authoritative state, stopping a
   local loop and starting Temporal workers against the same hive is a restart, not a migration.

### Rejected — a bespoke mailbox / event bus for agent wake-up

Considered and dropped: adding a lightweight broker (NATS, a `bd watch` subcommand, a socket) so
processes are pushed rather than polled. `bd gate` **is** a durable, addressable, blocking wait
with a waiter list — a mailbox with a slower doorbell. The only thing a broker adds is latency
reduction, and Temporal's own workers poll their task queues. Polling is not the compromise it
looks like; a second piece of infrastructure to avoid it would be.

Revisit if gate latency (bounded by poll interval) becomes a real complaint rather than an
aesthetic one.

---

## Decision 2 — three tiers behind one config key

```yaml
work:
  runtime: claude | local | temporal    # default: local
```

| Tier | Wake-up mechanism | Infra | Target |
|---|---|---|---|
| `claude` | Task-tool sub-agents (today's dispatcher fanout) | none | one human, one session; harness-bound |
| `local` | poll loop + `asyncio.create_subprocess_exec` | none | **solo / small team — the harness-agnostic default** |
| `temporal` | workers polling task queues | Temporal server | fleet, multi-hive, multi-machine |

`claude` is documented as a tier because it already exists and is what most sessions run today;
it is not a new implementation. It is the only tier bound to a specific harness, which is
precisely why it cannot be the default.

### The `local` tier

The whole scheduler, modulo error handling:

```python
async def loop(hive, interval=5):
    async with asyncio.TaskGroup() as tg:          # structured concurrency == supervision
        while True:
            bd("gate", "check")                    # timer / gh:run / gh:pr / bead gates self-resolve
            bd("reclaim")                          # dead-worker recovery
            for bead in bd_json("ready"):
                if bead.id not in running:
                    running[bead.id] = tg.create_task(run_role(seat_for(bead), bead.id))
            await asyncio.sleep(interval)
```

`asyncio.TaskGroup` is the supervision tree: cancellation propagates down to children, exceptions
propagate up, and a failing sibling cancels the group. Restart-with-backoff (`one_for_one`) is a
`try` inside the task plus `bd unclaim`.

This tier is more durable than its size suggests, because beads *is* the state — kill the loop,
restart it, and it re-derives everything from `bd ready` + open gates. What it lacks is listed
under Limitations.

---

## Decision 3 — the role-binary contract is the tier boundary

Every tier schedules the same thing:

```text
bh-<role> --bead <id> --instructions <file>   →   exit code + side effects in beads and git
```

- **Idempotent on re-run.** A restarted role re-reads bead state and continues; it never assumes
  it is the first attempt.
- **No return channel except bead state and git.** Nothing is handed back in-process, so the same
  binary is schedulable by a poll loop, a Temporal activity, or a Task-tool sub-agent without
  modification.
- **Exit-code taxonomy** — the escalation channel (see Decision 4).

This contract, not the orchestrator, is what makes the design harness-agnostic. It is also the
interop surface for any future non-BAML harness.

---

## Decision 4 — failure vs judgment, expressed as exit codes

Blind-retrying an agent that decided it could not proceed is wrong: same prompt, same context,
same outcome, double the spend. So the contract distinguishes infrastructure failure from
agent judgment:

| Exit | Meaning | Tier behavior |
|---|---|---|
| `0` | done; bead advanced | proceed |
| `10` | blocked — needs a decision | write `$BH_ESCALATION_FILE`; **do not retry**; escalate |
| `11` | fatal — do not retry | escalate |
| other | transient (network, rate limit, OOM) | retry with backoff |

Semantic outcomes ("acceptance criteria contradict each other", "the plan is wrong", "I gave up")
are **results**, not errors — they are inputs to the next scheduling decision, not retry triggers.

`bh escalate` continues to fire independently as the human-visible record into HQ intake. The exit
code is the machine channel (the scheduler reacts now); the HQ bead is the human channel (someone
reads it later). Two consumers, deliberately not collapsed.

---

## Consequences

- **`bd`'s coordination surface becomes load-bearing** — `gate`, `merge-slot`, `heartbeat`, and
  `reclaim` move from lightly-used features to the contract every runtime depends on. They need
  test coverage proportional to that.
- **The commercial seam lands on scale, not on layer.** Tiers `claude` and `local` are a complete,
  honest product for one machine; the value of `temporal` is fleet operation. Nothing has to be
  withheld from the open surface for that to hold.
- **The `local` tier must ship first.** Building Temporal first would let runtime-only state leak
  in before there is a second consumer to catch it. A seam with one implementation is a guess.

## Limitations

1. **Gate latency is bounded by poll interval** in `local` (and by task-queue poll in `temporal`).
   Sub-second reaction is not on offer in any tier.
2. **`local` is one machine.** No fan-out across workers, no cross-host scheduling. `bd reclaim`
   plus the multi-host lease/fence model ([multi-host-model-adr.md](multi-host-model-adr.md)) is
   what keeps a second machine from double-claiming, not the runtime.
3. **`local` has no retry policy and no execution history.** It records *that* state changed, never
   *why* it was retried. Diagnosing a flapping bead means reading agent logs.
4. **No backpressure or budget governance in `local`.** Concurrent epics can saturate the machine
   and the token budget with nothing to arbitrate between them.
5. **`claude` cannot be made harness-agnostic** and is documented, not developed. It stays as-is.
6. **Three tiers means three code paths to keep honest.** The invariant in Decision 1 is the only
   thing preventing drift, and it is a convention until something tests it — a conformance suite
   that runs the same molecule through each tier is the enforcement, and it is not free.

---

## Amendment 1 — the contract is baml-harness's already, and authority bakes into the binary

**Date:** 2026-07-30 · **Amends:** Decisions 3 and 4 of this ADR ·
**Status:** proposed, pending the verdict on spike molecule `bh-878p` / epic `bh-a7so`

Decisions 3 and 4 were written without reading `beadhive/baml-harness`. That was a mistake in
sequencing rather than in reasoning: the contract they specify is largely already built, in a
better shape, and one of its two halves belongs to the binary rather than to the invocation.

### What is already there

`dist/bh-developer` and `dist/bh-dispatcher` compile and run today. The typed return covers most of
what Decision 4 proposed to invent:

```text
SeatRun    { outcome: RoleOutcome, session_id, cost_usd, usage, packs }
RoleOutcome{ status: "done"|"blocked"|"handoff", summary, bead_id?, next_action? }
```

| Decision 3/4 specified | baml-harness already has | Disposition |
|---|---|---|
| exit codes `0/10/11` as the taxonomy | `RoleOutcome.status` | typed result is better — **it becomes the source of truth** |
| `$BH_ESCALATION_FILE` | `RoleOutcome.summary` + `next_action` | drop ours |
| `--instructions <file>` carrying the prompt | `--bundle` — prompt **+ permissions + permission_mode + mcp_config + plugin_dirs** | bundle is richer; see baking below |
| (not specified) | `SeatRun.session_id`, declared the resume token | adopt — it is the checkpoint primitive both tiers need |
| (listed as missing from `local`) | `cost_usd`, `usage` | budget data arrives for free |
| (not specified) | `packs`, digest-pinned | provenance; its doc comment says the field exists "so a caller wrapping the binary in a span can attribute which config actually ran" |

That last row is the tell: the harness was designed to be orchestrated. It is not a component the
runtime adapts to — it is the other half of the runtime.

Current argv is `--task --workspace --bundle --provider`. Note `--workspace`, which Decision 3
omitted and which the scheduler must supply as the bead's worktree path.

### The revised contract

```text
bh-<seat> --workspace <path> --bead <id> --instructions <file|->
          [--provider <kind>] [--model <tier>] [--resume <session_id>]

BAKED AT BUILD  seat prompt · permissions · permission_mode · mcp_config
                · plugin_dirs · packs digest
STDOUT          SeatRun JSON
EXIT            0 done · 10 blocked · 11 handoff · anything else = did not complete
                (stdout may be absent)
RESUME          --resume <session_id>
INVARIANT       re-run against an already-advanced bead is a no-op
```

**Why bake the bundle — authority, not tidiness.** If `--bundle` stays a runtime flag, anything
that can spawn the process can hand it a bundle granting `Bash(*)`. Baking makes the binary itself
the authority boundary, which is the same idea as task-queue-per-seat in
[temporal-control-plane-adr.md](temporal-control-plane-adr.md) Decision 5, one layer down and
available to *every* tier rather than only to `temporal`. It also fixes the `packs` digest at build
time, so provenance becomes a property of the artifact rather than of the invocation.

The bundle's fields split by **threat model**, not convenience:

| Field | Governs | Runtime-overridable? |
|---|---|---|
| `permissions`, `permission_mode` | authority | **never** |
| `mcp_config`, `plugin_dirs` | authority (tool reach) | **never** |
| seat `instructions` | role behavior | no — rebuild |

The baked prompt is the **role**; `--instructions` is the **task**. They compose. `--bead` is
first-class rather than embedded in prose because `RoleOutcome.bead_id` already echoes back, so
making it an input makes the round-trip checkable — did the agent work the bead it was handed? The
scheduler also needs it for `workflow_id`, claim, and audit without parsing prose.

### Correction to Decision 4 — exit codes AND stdout, not either/or

Decision 4 framed the taxonomy as *the* channel; the first draft of this amendment then framed the
typed result as *replacing* it. Both are wrong. A scheduler must react to a process that died
before writing stdout. So:

- **exit code** — the degraded-mode signal, always present
- **`SeatRun` on stdout** — the rich channel
- **`status`** — the source of truth whenever stdout parses

Decision 4's underlying distinction is unaffected and still holds: *failure* (infrastructure —
retry with backoff) versus *judgment* (`blocked` / `handoff` — a result, never a retry trigger).
`bh escalate` likewise still fires independently as the human-visible HQ intake record; the machine
channel and the human channel stay separate.

### Consequences accepted deliberately

1. **`resolve_seat_from`'s ingestion paths move from runtime to build time.** The bundle / plugin /
   hitch sources still exist; they run when a seat is compiled rather than when it is invoked. The
   runtime gets simpler and nothing is lost.
2. **Baking does not couple the harness to a producer**, so long as beadhive compiles its own seat
   binaries from its own bundle. baml-harness still depends on nothing upstream; the independence
   its ADRs assert stays at the source level, which is where they put it.

### What is still unvalidated

Filed as spike molecule `bh-878p` / epic `bh-a7so`, which `bh-c6dk.2` now depends on:

1. **Wire format** — how `SeatRun` actually reaches a caller, and whether today's exit code
   reflects `status` at all.
2. **Checkpoint and resume** — the load-bearing one. For CLI providers the agentic loop runs inside
   the `claude` child, and provider.baml is explicit that "someone else's permission engine
   enforces and we merely configure it," so checkpoint-on-kill is that child's behavior, not the
   harness's. Whether `--resume <session_id>` costs materially less than a fresh run is **measured
   by nobody**. The premise that restart cost is bounded — which is what makes let-it-crash
   affordable when a crash costs tokens — rests entirely on this.
3. **codex** — declared in provider.baml with `executes_tools_locally == true` and
   `implemented == false`. Implementing it is a build, not a probe. It can also invalidate this
   amendment: permissions are enforced by the provider's own engine (`boundary_enforced_by`), so if
   codex cannot express the same allow/ask/deny roster, a runtime `--provider` switch would
   silently weaken a baked boundary and **`--provider` must bake too**.

Until `bh-a7so.4` closes, this amendment is the proposal under test, not the settled contract.
