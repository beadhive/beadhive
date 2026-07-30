# Work-runtime tiers ADR — beads is the state machine, the runtime is only the scheduler

**Status:** proposed · **Date:** 2026-07-29 · **Supersedes:** nothing ·
**Related:** [temporal-control-plane-adr.md](temporal-control-plane-adr.md) (tier 2's topology),
[bead-backend-abstraction.md](bead-backend-abstraction.md) (the `beads.engine` seam this mirrors),
[roles-rbac-matrix.md](roles-rbac-matrix.md) (the seats being scheduled)

Establishes the seam between *what is true about a bead* (beads) and *when a process wakes up to
act on it* (the runtime), and defines three runtime tiers that share one set of semantics.

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

```
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
