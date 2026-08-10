# Loop ownership and execution memory ADR — what runs the loop unattended, and what it may remember

**Status:** accepted · **Date:** 2026-08-10 · **Supersedes:** nothing ·
**Amends:** [work-runtime-tiers-adr.md](work-runtime-tiers-adr.md) **Decision 1**, by drawing the
boundary that decision left open — its invariant says a runtime "MAY keep a richer *execution*
record (retry counts, timings, the parent chain, why something was retried)" without ever saying
where that permission stops. This ADR draws the line, and in v1 draws it at **zero**.
**Related:** [roles-rbac-matrix.md](roles-rbac-matrix.md) (the seats, §2.1 and §2.2),
[temporal-control-plane-adr.md](temporal-control-plane-adr.md) (tier 2's topology, untouched here),
[cli-mcp-naming-conventions-adr.md](cli-mcp-naming-conventions-adr.md) (the surface conventions the
delivery beads validate against)

> **Scope guard.** This ADR settles four questions and no others: the **process model** of an
> unattended dispatcher, the **execution-memory boundary**, the **alternatives rejected for this
> role**, and **which seat owns which loop**. It does **not** re-decide the tier seam
> (work-runtime-tiers Decision 1/2), the role-binary contract (Amendment 2 §1), or the Temporal
> topology. Those were tested by spike molecule `bh-878p` / epic `bh-a7so` and **stand as filed**.
> Nothing below should be read as reopening them.

Filed by epic `bh-e7r9q` (headless dispatch on an executor host), bead `bh-e7r9q.1`. Follow-on to
epic `bh-c6dk`, whose design field was amended the same day with the boundary this record
formalises.

---

## Context

The `local` runtime tier gives the lifecycle a scheduler. It does not yet give it a **host**. Today
the loop still advances because a human invoked a Claude Code harness and is sitting in front of
it; the moment they close the terminal, nothing claims, nothing checks a gate, nothing reclaims a
dead lease.

Turning that into an always-on process on an executor host forces four questions that the tiers ADR
could leave open while a human was always in the room:

1. Is an unattended dispatcher a long-lived process, or something that sleeps and wakes?
2. What may it hold in memory across a restart — and where does that state live?
3. Beads already ships a formula/wisp mechanism that *looks* like a dispatch loop. Should the loop
   be one?
4. Which **seat** owns the loop, and which seat starts it?

Question 2 is the load-bearing one, because the answer to it is what keeps the tiers
interchangeable. Question 4 turned out to expose a real ambiguity in the roles matrix, which this
ADR records as open rather than quietly resolving.

---

## Decision 1 — always-on supervised process, not sleep/wake

**The dispatcher is a supervised long-lived process on an `executor` host.** It is started by the
host's own supervision backend (systemd / launchd / a container runtime, selected by a config key),
it polls, and if it dies the supervisor restarts it. It does not suspend itself, it does not
serialise a continuation, and there is no "wake me when X" primitive to build.

The sleep/wake framing is what makes durable-execution engines expensive, because a process that
sleeps must be able to *resume* — which means its in-flight state has to be written down somewhere
a future process can read. An always-on process has no such obligation. It only has to survive a
**restart**, and a restart is allowed to start from nothing.

That is affordable here for one reason: **beads is the state.** work-runtime-tiers Decision 1
already requires it — gates resolved in beads, leases held in beads, the merge slot in beads, in
every tier — so a freshly started loop re-derives its entire world from `bd ready` plus open gates
plus `bd reclaim`. The tiers ADR says as much of the `local` tier ("kill the loop, restart it, and
it re-derives everything"); this decision is that property promoted from a nice remark about a code
sketch to the **process model** the host deployment is designed around.

So "durable memory" for this dispatcher is scoped to **surviving a restart, not to sleeping** — and
almost nothing needs to survive, which is what Decision 2 makes precise.

The restart cost is bounded by evidence already in the tree, not by hope: `bh-a7so.2` §8 measured
restart after losing 5 of 7 committed steps at **0.38–0.42 of a full fresh run**, and §12 found the
bound is a function of commit granularity in the worktree and nothing else — the branch is the
checkpoint. Let-it-crash is priced.

### What this decision does not license

A restart is cheap for the *loop*. It is not free for the *children*: `bh-a7so.2` §3 found that
signalling only the harness binary orphans the `claude` grandchild, which reparents to init and
runs the whole task to completion into a pipe nobody holds. Supervised restart therefore assumes
the process-group discipline already required of `bh-c6dk.5` (`start_new_session=True` +
`os.killpg`, SIGTERM-then-SIGKILL, never SIGINT, envelope read before the reap). This ADR inherits
that requirement; it does not restate or relax it.

---

## Decision 2 — the execution-memory boundary, and the v1 carve-out is zero

work-runtime-tiers Decision 1 permits a runtime to keep "a richer execution record". Read
literally, that permission is unbounded, and an unattended dispatcher is exactly the consumer that
would take it — retry counts, bounce history, stall reasons, a token window. This decision bounds
it.

### The line

| Kind of memory | Where it lives | Survives restart? |
|---|---|---|
| Failure cause, bounce history, stall reason | **beads** — closed state-dimension values written with `bd set-state --reason` | yes (git-synced, visible from any host) |
| Retry / bounce **counts** | **derived** by counting event beads at read time; never stored | n/a — recomputed |
| Concurrency cap, per-run wall-time cap, the in-flight `{bead_id → (proc, stdin, pgid)}` map | **in process** | **no, on purpose** |
| Rolling token-budget window | **nowhere in v1** — deferred, see below | n/a |

**=> v1 persists nothing outside beads.** work-runtime-tiers Decision 1's invariant ships whole
rather than with an exception carved out of it on first contact with a real deployment.

### Why beads is sufficient for failure history — measured, not assumed

`bd set-state <id> <dim>=<value> --reason "..."` atomically does two things: it **creates an event
bead** recording the change (the source of truth) and it writes a `<dim>:<value>` **label** (a
fast-lookup cache). That is an append-only log plus a materialised projection — the same shape a
durable-execution engine uses — assembled from primitives that already ship.

Measured on this hive, 2026-08-10:

| Measurement | Value |
|---|---|
| Beads that are already state-change event beads | **627 of 1941** (32% of the corpus) |
| `review -> changes-requested` events | **37**, across **29** distinct beads |
| Beads carrying **two** such events | **8** |

Those 8 are the whole argument. "Has this bead been bounced, and how many times" is not a feature
to be designed — it is a query that returns correct answers over live data **today**, which is why
the loop-breaker can be built on derived counts instead of on a stored counter.

### Why `metadata` was rejected for failure history

`metadata` is **last-write-wins current state**, i.e. a projection. It can tell you what the last
failure was; it structurally cannot tell you that there were three. Storing a count there
reintroduces every problem the event log removes: something to go stale, a reconcile rule to write,
a concurrent writer to clobber it.

And the projection is not even the thing that is missing — `bd set-state` already writes one, as
the `<dim>:<value>` label cache. So `metadata` would be a *second, weaker* projection sitting
alongside the one that ships, while the history that `metadata` cannot hold is exactly what the
event beads already hold. It is rejected as redundant on the half it can do and incapable on the
half that matters.

### Write on failure, not on attempt

Event beads are permanent and this hive has **no compaction tier** — `bd compact` / `bd flatten`
are forbidden until `bh-3vs6c` lands. Recording every dispatch attempt would accelerate the
fastest-growing bead class in the corpus (see the 32% above) for no gain a retry count needs.
Bounces, stalls and escalations only. That bounds volume to what a human would plausibly read
later, and is sufficient for every count the loop derives.

### Why the token window cannot live in beads at all

Four independent reasons, any one of which is disqualifying:

- It is **host/account-scoped**, not bead-scoped. There is no bead it belongs to.
- It is **read before every spawn**, i.e. on the hot path of the loop, not at state transitions.
- It needs a **TTL** — a rolling window expires; bead state does not.
- Its value is **unbounded** (a running token total), and a closed state dimension cannot express
  an unbounded value.

### The carve-out is zero in v1 — a decision with a date and a trigger

**Operator decision, 2026-08-10:** v1 does **not** build the token-budget governor. The local tier
ships a **concurrency cap** and a **per-run wall-time cap**, both in-process, both correctly dying
with the loop. Token-window enforcement **defers to `bh-3yoh`**, and its **trigger is the second
hive dispatching unattended** — with one hive there is nothing to arbitrate between. For the same
reason and at the same trigger, v1 ships **no director loop**; the per-epic loop takes kicked-off
epics in `bd ready` order.

**R3 is therefore knowingly half-met in v1.** That is recorded here as a deliberate, dated choice
with a named trigger, not as an omission someone later has to discover from a gap in the code. The
budget data itself is not the obstacle — `bh-a7so.7` §10–§11 cleared `usage` as exact and `cost_usd`
as a pure function of it, so both are safe bases for a budget whenever one is built.

> **The first durable runtime state will arrive with the budget governor.** When it does, crossing
> this line is an **amendment to this ADR with its own justification** — not an allowance already
> granted by work-runtime-tiers Decision 1's "richer execution record" clause, and not something a
> delivery bead may do in passing.

### Open prerequisite — the gate-instrumentation gap

A discrepancy that was a curiosity while a human watched the loop is load-bearing the moment a
count is derived rather than stored. Measured on this hive:

| Rows | Count |
|---|---|
| `issue_type='gate'` | **453** |
| …with a **created** event | **406** |
| …with a **closed** event | **232** |

If events can be silently dropped, a derived count **under-counts**. The failure direction is
therefore specific and worth stating plainly: the loop-breaker fires **late or never — never
early**. A dispatcher that never gives up is a worse failure than one that gives up too soon,
because it spends tokens forever with nobody watching.

Spike **`bh-gj0v9.2`** owns classifying this as defect-or-by-design. It is an **open prerequisite
for the escalation path**, not a nice-to-have, and this ADR is accepted with it outstanding
precisely so that the dependency is on the record rather than in someone's head.

---

## Decision 3 — the formula/wisp path is rejected for the dispatch loop, with a named later use

Beads ships a molecule template mechanism — formulas (`bd mol pour` / `wisp` / `squash` / `burn`)
whose wisp phase is explicitly "spawn creates wisps, execution happens, squash compresses the trace
into an outcome (digest)". That reads like a dispatch loop, and it is not one. It is rejected for
this role, and the reasons are recorded so the question is not reopened from the wrong premise.

**1. A formula cannot execute — this is the decisive fact.** A formula Step has **no
`action` / `script` / `command` field**. A formula can only *materialise beads that track a run*;
it cannot *run* anything. So a formula could never be the loop. At best it would be a **second
description** of `work_next.py`'s priority table — one in a template, one in code, both claiming to
say what runs next, free to drift, with nothing that fails when they disagree. Anyone who reopens
this decision believing a formula could "drive" dispatch is starting from a premise the tool does
not support.

**2. `bd mol squash` grows the permanent corpus every pass.** Squash **promotes the ephemeral
children to persistent** (clearing the Wisp flag) *and* writes a **permanent digest issue**. A loop
that squashes once per pass therefore adds rows forever — in a repo where `bd compact` and
`bd flatten` are forbidden until `bh-3vs6c` lands, and where event beads are already 32% of the
corpus and the fastest-growing class. This is the same argument as "write on failure, not on
attempt" in Decision 2, applied to a different mechanism.

**3. Wisps are host-local and not git-synced.** A dispatch loop's state would then be invisible
from the operator's laptop — which is precisely the property Decision 2 buys by keeping failure
history in beads.

**4. The path has zero live rows.** `bd mol wisp list` returns **"No wisps found" in every hive on
this machine.** Building the unattended dispatcher — the thing that has to work with nobody
watching — on a code path with no production traffic anywhere would be a poor first user.

### The named later use — the per-pass trace

Rejecting the mechanism for *this* role is not rejecting the mechanism. There is a good fit for it,
and naming it now is the point of this subsection:

**A wisp molecule per dispatch pass, as the trace.** The pass spawns a wisp, the wisp records what
was considered and what was dispatched, and then:

- **clean pass → `bd mol burn`** — discarded, nothing added to the corpus;
- **escalation → `bd mol squash`** — the trace is condensed into a permanent digest, exactly where
  a human will later want to read what the loop was thinking.

That is burn-by-default with squash-on-interest, which turns objection 2 from a cost into the
feature. But it is **observability, and observability should follow a working loop** — you cannot
usefully trace a decision procedure that does not exist yet. It is deliberately not in v1.

---

## Decision 4 — which seat owns which loop

A review of [roles-rbac-matrix.md](roles-rbac-matrix.md) against the autonomy design found the
common shorthand "the director runs the root loop" to be **wrong**, and found one genuine ambiguity
that must be settled before a second hive forces it.

### The root dispatcher is not the director

The matrix is unambiguous on this. §2.2's dispatcher-variant table, **row 1**, reads:

| Legacy name | Dispatcher variant | Scope (branch) | Implements? (Edit/Write) | Task |
|---|---|---|---|---|
| coordinator (root) | dispatcher @ main · **fanout** | integration main line | no | yes — fans out to developers |

The root loop is **`dispatcher @ main · fanout`** — Integration plane, `disp/`, on the integration
main line, holding **Task** and **no Edit/Write**. Its retired name is **"coordinator (root)"**
(§5: `coordinator` (`coord/`) → `dispatcher` (`disp/`)), which is where the confusion comes from:
"coordinator" sounds like a control seat and is not one.

The **director** is a different plane and a different job. §2.1's Control-plane table gives it
resource scope *"Intake + work routing (intake→plan→work) + interface to the per-rig dispatchers"*,
and the paragraph under that table is explicit: the director *"is the operations/traffic layer that
routes work and talks to the per-rig dispatchers — it directs work, **holds no secrets, sets no
policy**"*. §4's `director` row repeats it as a permission: *"write fleet/`managed_repos`,
route/direct work, launch dispatchers; **not** hold secrets, set policy, implement/merge"*.

Two consequences follow directly from those rows:

- **The director decides and launches; it never drives.** It opens no branch, assigns no bead,
  resolves no gate, holds no key. Every one of those is another seat's row in §4 — assignment and
  provisioning are the `dispatcher` row, gate verdicts are the `reviewer` row, keys are the
  `custodian` row (§2.1: custodian is *"the only control seat touching secret/key material"*).
- **Hive maintenance is the custodian's, not the director's.** §4's custodian row scopes it
  precisely: *"create/register repos, write config, manage secrets, cleanup"* — and it explicitly
  excludes routing, just as the director row explicitly excludes secrets. An unattended factory
  will want config repair, worktree pruning and key rotation; none of that may be bolted onto the
  director loop, because the director row says it holds no keys.

### `dispatcher @ main` does not go vestigial under autonomy

If every molecule gets a per-epic dispatcher, it is fair to ask what the root fanout dispatcher is
still *for*. It keeps a real charter: **the loose beads that belong to no molecule** — standalone
bugs, chores, intake items accepted straight to the backlog. Those are `bd ready` work with no epic
container and therefore no per-epic loop to pick them up. `dispatcher @ main · fanout` is the seat
whose scope is the integration main line, so they are its. Stating this now prevents the seat from
being quietly deleted as redundant and the loose beads from becoming nobody's.

### OPEN — who launches a dispatcher

**This ADR records this as open. It does not resolve it.**

Two rows in the matrix can each plausibly claim it:

- §4's **director** row lists **"launch dispatchers"** among its permissions.
- §2.2's **`dispatcher @ main · fanout`** row holds **Task** and is described as *"fans out to
  developers"* — and it is the integration root, so a dispatcher launched under it is structurally
  a sub-dispatch.

Nothing in the document says which one launches a per-epic dispatcher, and §2.1's spine
(*"supervisor → director → dispatcher, where dispatcher lives one plane down in Integration"*)
is consistent with both readings. Under human operation this never bit, because a person decided
per session and the answer never had to be written down. **Unattended, it must be settled**: two
seats that both believe they may start a loop is either a double-launch or a launch that never
happens.

v1 sidesteps it rather than settling it — with no director loop and one hive, the only launcher is
the operator. The question comes due at the **same trigger as the budget governor: the second hive
dispatching unattended.** It is recorded here so it is settled by a decision rather than discovered
by a duplicate dispatcher.

### The supervisor collapse path already answers "multi-hive supervisor vs direct-to-human"

A related question — whether an escalation from an unattended loop goes to a supervising seat or
straight to a person — needs no new decision. §2.1's **collapse path** answers it: *"a small/
single-rig factory runs just the supervisor, absorbing the director/custodian/controller scopes;
split them into their own seats + identities as the factory grows"*. In a single-hive factory the
supervisor **absorbs director/custodian/controller and IS the human.** Escalation to the supervisor
and escalation to a person are the same act today, and become different acts by the same growth
event that splits the seats — no separate design step, no fork in the escalation path.

---

## Constraints this ADR records for later work

Not decisions of this ADR; findings that bound what the delivery beads may assume.

**Only `claude-code` is implemented.** In baml-harness's `provider.baml`, `claude-code` is the sole
provider with `implemented: true`; `codex` is declared `implemented: false`, and that is **pinned by
an assertion test** rather than left as a comment. Every checkpoint, recovery and cancel behavior in
work-runtime-tiers Amendment 2 §3–§5 is a Claude Code CLI behavior the contract *inherits*.

**`--provider` bakes, so multi-provider is a per-provider seat build.** Settled by Amendment 2 §2 —
it is an authority argument. The consequence for this molecule is scheduling, not capability:
building a second provider is work in **baml-harness**, a different hive, and is deliberately not
in `bh-e7r9q`.

**Headless dispatch does not wait on Codex.** This is the reason the two are separable at all. The
loop spawns a role binary and reads `SeatRun` JSON off its stdout; it cannot tell what is inside.
So the executor host can be built now, off the human-invoked harness, with `claude-code` under
every seat binary.

---

## Consequences

- **work-runtime-tiers Decision 1's invariant ships whole.** v1 persists nothing outside beads, so
  the seam is not weakened by its first real deployment. The clause that permitted "a richer
  execution record" is now bounded rather than open-ended.
- **Retry logic is a read, not a write.** The loop-breaker counts event beads at decision time.
  There is no counter to initialise, migrate, reconcile or clobber — and no schema change.
- **The in-process caps are deliberately volatile.** A restart resets concurrency and wall-time
  accounting. That is correct: both describe *this process's* children, and this process no longer
  has any.
- **`bh-gj0v9.2` is a prerequisite, not a follow-up.** The escalation path cannot be trusted until
  the gate-event gap is classified, because a derived count under-counts silently.
- **The token-budget governor is the next boundary crossing**, and it is an amendment to this ADR.
  Its trigger is the second hive dispatching unattended (`bh-3yoh`).
- **"Who launches a dispatcher" is a live open question with an owner-shaped hole in it**, due at
  the same trigger.

## Limitations

1. **R3 is half-met.** Concurrency and wall-time are capped; spend is not. A single runaway epic can
   still burn a token budget inside the wall-time cap.
2. **Derived counts are only as good as the event stream**, which is exactly what the 453/406/232
   gap puts in question. Under-count is the failure direction.
3. **One hive, one host.** No cross-hive arbitration and no director loop in v1, by decision. The
   per-epic loop takes kicked-off epics in `bd ready` order and calls that scheduling.
4. **The seat/loop split is a design record, not an enforcement.** §4's enforcement column marks
   most of these boundaries `soft` (seat-typing + tool scoping in the agent def). Nothing fails
   closed if an unattended loop acts outside its row.
5. **No trace in v1.** Diagnosing why the loop chose what it chose means reading process logs; the
   wisp-based per-pass trace in Decision 3 is named, not built.
