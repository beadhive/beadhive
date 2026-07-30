# Temporal control plane ADR — the `temporal` runtime tier's topology

**Status:** proposed · **Date:** 2026-07-29 · **Supersedes:** nothing ·
**Depends on:** [work-runtime-tiers-adr.md](work-runtime-tiers-adr.md) — this ADR designs tier 2
only; the seam, the role-binary contract, and the "beads owns lifecycle state" invariant are
decided there and are **not** re-litigated here.
**Related:** [roles-rbac-matrix.md](roles-rbac-matrix.md) (the seats being mapped),
[CONTROL-PLANE.md](../CONTROL-PLANE.md)

Maps AGF's planes and seats onto Temporal workflows, activities, task queues, and namespaces;
records how a non-deterministic scheduling policy coexists with deterministic replay; and lists
the parts of the existing design this topology changes.

---

## Context

The `temporal` tier exists for the case the `local` tier cannot serve: more than one machine, more
than one hive, work that must survive the orchestrator's own death, and an execution record good
enough to answer *why* a bead was retried. The question this ADR answers is not "should we use
Temporal" — it is "what maps onto what", because a wrong mapping here reproduces the seat model
badly and is expensive to undo.

The temptation to avoid is visible in Temporal's own AI cookbook, which models each LLM call as
an activity so the workflow drives the agent loop. **That shape is wrong for us**: our agent loop
lives inside the compiled baml-harness role binary. Adopting the cookbook's granularity means
slowly rewriting baml-harness as workflow code, and it puts non-deterministic reasoning inside the
layer that must replay deterministically.

---

## Decision 1 — the classification test

> **Needs a mailbox and a lifetime → Workflow. One-shot transform → Activity.**

Applied to the seat roster, this reproduces the plane model without further argument:

| Seat | Plane | Primitive | Why |
|---|---|---|---|
| **supervisor** | Control | long-lived workflow, `super:<factory>` | policy state, escalation terminus, supervises control seats |
| **director** | Control | long-lived workflow, `dir:<fleet>` | intake + telemetry + feedback signals; routes |
| **custodian** | Control | activities on an **isolated task queue** | mechanical; the only secret-bearing worker (Decision 5) |
| **controller** | Control | Temporal Schedule | read-only; largely subsumed (Decision 6) |
| **planner** | Planning | workflow, `plan:<idea>` | spans days, human Updates, must survive everything |
| **analyst** | Planning | **activity** | question → findings. Pure. Fan out with `gather`. |
| **dispatcher** | Integration | workflow, `disp:<epic>` | scope × mode are *inputs*, not distinct types |
| **developer** | Integration | workflow, `bead:<id>`, wrapping activities | needs cancel + replan signals and a real lifecycle |
| **reviewer** | Integration | **activity** *or* `wait_condition` | agent verdict = activity; `type:human` gate = signal |
| **merger** | Integration | **singleton** workflow, `merge:<hive>:<branch>` | Temporal guarantees one execution per workflow id |
| **warden** | Assurance | **activity** | a verdict function, called from three gate sites |
| **verifier** *(lens)* | Assurance | **activity** | same argument — confirms keeping it a lens, not a seat |
| **releaser** | Release | workflow, `rel:<hive>:<version>` | gated, long-lived |
| **contributor** | Contribution | workflow + hard human publish gate (Update) | isolated queue holds the external creds |
| **operator** | Delivery | workflow, reached via **Nexus** | deploy targets are a different blast radius (Decision 7) |

### The topology

```
SupervisorWorkflow                     id: super:<factory>    immortal, continue-as-new
│  policy · escalation terminus · launches control seats
│
├── DirectorWorkflow                   id: dir:<fleet>        signals: intake, telemetry, feedback
│   │
│   ├── PlannerWorkflow                id: plan:<idea>        human Updates, spans days
│   │   └── ⚙ analyst × N  (parallel, read-only, pure)
│   │
│   └── DispatcherWorkflow             id: disp:<epic>        scope×mode = INPUT, not a type
│       │
│       ├── fanout ──→ DeveloperWorkflow    id: bead:<id>     one per bead
│       │              ├── ⚙ run_role("developer")   ← baml-harness binary
│       │              ├── ⚙ warden                  ← security:* gate
│       │              └── review gate: ⚙ reviewer │ wait_condition(signal)  ← human
│       │
│       └── collapsed → ⚙ run_role("dispatcher") × N sequentially, one shared branch
│
├── MergerWorkflow      id: merge:<hive>:<branch>   singleton
├── ReleaserWorkflow    id: rel:<hive>:<version>
├── ContributorWorkflow id: contrib:<pr>            hard human publish gate
└── OperatorWorkflow    via Nexus → deploy namespace

⚙ CustodianWorker       own task queue, own credentials — secrets exist nowhere else
📊 ControllerWorkflow    Temporal Schedule, read-only export
```

`workflow_id = bead:<id>` makes re-dispatching a bead a free no-op: Temporal rejects a duplicate
running execution for the same id. That is the whole idempotency story for dispatch.

---

## Decision 2 — non-determinism lives in activities; the scheduling loop is deterministic code

Temporal's constraint is that workflow code must be **replayable**, not that decisions must be
**predictable**. An activity's result is recorded on first execution and replayed thereafter, so a
non-deterministic decision becomes a deterministic fact. An LLM may therefore drive scheduling:

```python
@workflow.defn
class EpicWorkflow:
    @workflow.run
    async def run(self, epic: str, state: EpicState):
        while not state.done:
            d = await workflow.execute_activity(decide_next, state, ...)  # LLM — recorded
            match d.action:                                    # deterministic code, free sequence
                case "dispatch": state = await self._dispatch(d.beads, state)
                case "replan":   state = await workflow.execute_activity(replan, d.rationale, ...)
                case "escalate": state = state.with_answer(await self._ask_parent(d))
                case "done":     break
            if workflow.info().get_current_history_length() > 10_000:
                workflow.continue_as_new(epic, state)
```

**The one real constraint: the action vocabulary is a closed enum.** The policy chooses among
`dispatch | replan | escalate | done`; it never emits code or an unbounded action. The *sequence*
is free and never has to be reproducible. Adding a fifth action later is a replay-compatibility
change guarded by `workflow.patched()`.

Determinism footguns that pass tests and fail on replay: use `workflow.now()`,
`workflow.uuid4()`, `workflow.random()` — never the stdlib equivalents. All `bd` and `git` calls
go in activities, without exception.

---

## Decision 3 — escalation is the parent chain

`escalate.py` currently routes flat to HQ and its docstring defers "up-chain auto-routing" as a
smart-target change. Under this topology the parent chain **is** the routing table: an unhandled
failure propagates developer → dispatcher → director → supervisor, and each level decides
handle-or-propagate. That is OTP escalation with no new mechanism.

HQ intake becomes the terminus and the human-visible mirror rather than the only hop. `bh escalate`
is unchanged and continues to fire independently — see work-runtime-tiers Decision 4 on why the
machine channel and the human channel stay separate.

---

## Decision 4 — interrupts reach agents through heartbeats, and only through heartbeats

Signals address workflows; **an activity cannot be signalled**. The only interrupt path is:

```
signal / cancel → workflow → activity_handle.cancel() → activity's next heartbeat raises
  CancelledError → SIGTERM the role binary → binary checkpoints (submit WIP, bd unclaim)
  → activity re-raises
```

Two operational requirements follow:

- **Heartbeating is mandatory, not hygiene.** Without `activity.heartbeat()` and a
  `heartbeat_timeout`, an activity is uninterruptible and a wedged agent hangs until
  `start_to_close_timeout`, which for agent work is hours. Heartbeat every ~10s from the subprocess
  supervisor loop.
- **`activity.heartbeat(details)` is the resume checkpoint.** On retry,
  `activity.info().heartbeat_details` returns the last payload. Heartbeat the bead's current phase
  so a restarted agent resumes there rather than from zero. This is what keeps restart cost
  bounded — the precondition that makes "let it crash" affordable when a crash costs tokens.

For a human interrupt that must return a value, use a **Workflow Update** (synchronous, durable),
not a Signal. The `type:human` bead gate remains the in-band record.

Set **`ParentClosePolicy = REQUEST_CANCEL`** on child workflows. The default (`TERMINATE`) kills
children with beads still claimed and worktrees dirty — the "abandoned bead" failure mode. With
`REQUEST_CANCEL` children take the path above and unclaim themselves; pair it with a cleanup
activity in the bead workflow's `finally`.

---

## Decision 5 — task queue is the RBAC boundary

A worker polls one task queue with one identity's credentials. Scheduling `provision_repo` on the
`custodian` queue means only a custodian worker — the sole process holding key material — can
execute it, regardless of what any agent definition says.

This converts most of the RBAC matrix's `soft` rows to infrastructure-enforced:

| Seat | Matrix enforcement today | Under task-queue isolation |
|---|---|---|
| supervisor, director | soft (org root / seat-typing) | hard — queue-scoped |
| **custodian** | soft (secret isolation by convention) | **hard — secrets exist only in that worker** |
| planner, dispatcher (`assign`) | soft | hard — queue-scoped |
| merger | soft | hard — queue-scoped |
| contributor | already hard (publish guard) | hard, plus creds isolated to its own worker |

This is the single largest structural improvement the tier buys, and it is a deployment topology
rather than new code.

---

## Decision 6 — what this changes in the existing design

1. **The merge slot stays in beads.** An earlier draft of this design proposed deleting
   `bd merge-slot` in favor of Temporal's workflow-id singleton. That is wrong under the tier
   invariant: the slot must work in `local` and `claude`, so **`bd merge-slot` remains the source
   of truth**. Temporal's singleton is a redundant second lock at the runtime layer — free and
   harmless, but not a replacement.
2. **Warden's "cross-cutting" awkwardness resolves.** The roles matrix flags Assurance as
   "deliberately breaking the one-plane-one-handoff tenet." It only breaks it because a pure
   verdict function was modelled as a seat. As an activity, being invoked at pre-merge, pre-cut,
   and pre-publish is unremarkable. The same argument independently confirms keeping **verifier**
   a lens.
3. **Controller shrinks.** Temporal supplies throughput, latency, failure rates, per-seat cost, and
   full history natively. The seat reduces to "export to Grafana + write dashboards" — a Schedule,
   not a seat exercising judgment. It is not retired (the `local` tier still needs it) but it stops
   being a design problem here.
4. **Dispatcher stays one workflow type.** scope × mode as input matches the matrix's "one seat,
   `dispatcher`, with scope+mode as dispatch metadata" exactly. No `epic-coordinator-deep` analog
   reappears at the runtime layer.

---

## Decision 7 — cross-hive is Nexus, not A2A

For hive → hive where both hives are ours: **Temporal Nexus** — namespace per hive, versioned
endpoints, caller-namespace allowlist, durable across the boundary. This also carries the
blast-radius story: a hive cannot reach another hive's task queues.

**A2A** earns a place only when a counterparty's agent is not ours. Until then it is a second
protocol carrying the same payloads with weaker guarantees. **ADK is explicitly rejected** — it
occupies the same slot as baml-harness (an in-process agent framework); the transferable half of
the widely-cited ADK + A2A + Temporal pattern is the second and third terms.

---

## Consequences

- **Sequencing: `DeveloperWorkflow` and `MergerWorkflow` first.** They are leaves, they have the
  sharpest payoff, and they exercise the role-binary contract without touching the control plane.
  `DispatcherWorkflow` second. Supervisor / director last — they are low-value until there is more
  than one hive, and by then the signal set will be known rather than guessed.
- **Namespace-per-hive is a later config change, not a rewrite** — single namespace is correct
  until Nexus is needed.
- **The topology is not a moat.** It is roughly 1500 lines of glue reproducible from public
  material. The defensible parts are `decide_next`'s quality (prompts + eval suite) and operating
  the thing for other people. Temporal server is MIT-licensed; "we use Temporal" differentiates
  nothing.

## Limitations

1. **Long epics will hit history limits.** `continue_as_new` is required, not optional, and
   deciding what carries across the boundary is real design work per workflow.
2. **Workflow versioning is permanent overhead.** Every change to a workflow's branch structure
   while executions are in flight needs `workflow.patched()` or Worker Versioning. This is the
   ongoing tax of the tier.
3. **Gate observation costs a polling activity.** Because beads stays authoritative (tier
   invariant), a Temporal workflow waiting on a `type:human` gate blocks on a heartbeating
   activity that polls `bd gate show`. Correct, but not free, and it is the seam most likely to
   tempt someone into caching gate state in Temporal — which would break the tier invariant.
4. **Operational surface is real.** Server, workers per task queue, namespace provisioning,
   credential distribution. This is precisely why it is tier 2 and not the default.
5. **`decide_next` is unspecified here.** This ADR fixes the *shape* (closed action enum, activity
   boundary) and says nothing about policy quality, which is the part that actually determines
   whether autonomous epic scheduling works.
