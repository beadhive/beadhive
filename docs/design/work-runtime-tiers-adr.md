# Work-runtime tiers ADR — beads is the state machine, the runtime is only the scheduler

**Status:** proposed, **amended in place twice, 2026-07-30** · **Date:** 2026-07-29 ·
**Supersedes:** nothing · **Amends:** no other ADR —
[Amendment 1](#amendment-1--the-contract-is-baml-harnesss-already-and-authority-bakes-into-the-binary)
and [Amendment 2](#amendment-2--the-settled-contract-provider-bakes-the-branch-is-the-checkpoint)
below amend *this* one.
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
>
> **Then read [Amendment 2](#amendment-2--the-settled-contract-provider-bakes-the-branch-is-the-checkpoint)
> before acting on Amendment 1's contract block.** Amendment 1 was the *proposal under test*; spike
> molecule `bh-878p` / epic `bh-a7so` has now tested it across five artifacts and returned **GO**,
> so Amendment 2 is the settled contract. Four lines of that block changed: **`--provider` moves
> from runtime to baked** (it is an authority argument), **`RESUME --resume <session_id>` is
> replaced by `RECOVERY`** — re-dispatch a fresh turn against the same worktree, because the branch
> is the checkpoint and resume costs 1.30× a fresh run — **`--session_id` becomes required on
> create**, and a new **`CANCEL`** ladder is added. The `EXIT 0/10/11` row survives as the target
> but is **unbuilt**: until it lands, exit `0` means "go read stdout", never "succeeded".
> **Decisions 1 and 2 survived all five spikes and stand as filed.**

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

> **Resolved** — [Amendment 2](#amendment-2--the-settled-contract-provider-bakes-the-branch-is-the-checkpoint).
> The verdict this amendment was pending is in: **GO**, with four lines of the contract block below
> changed. This section is retained for the reasoning it carries — particularly "why bake the
> bundle" and the exit-codes-AND-stdout correction, both of which the spikes confirmed — not as the
> settled shape.

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

> **Amended** — [Amendment 2 §1](#1-the-settled-contract).
> `--provider` moves out of the optional-runtime group into `BAKED AT BUILD`; the `RESUME` row is
> replaced by `RECOVERY`; `--session_id` is added as required-on-create; a `CANCEL` row is added.
> `EXIT`, `STDOUT` and `INVARIANT` survive as written, with `EXIT`'s taxonomy flagged unbuilt.

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

> **Answered** — [Amendment 2](#amendment-2--the-settled-contract-provider-bakes-the-branch-is-the-checkpoint).
> All three items below were measured. (1) wire format: **GO**, six priced deltas
> ([`bh-a7so.1`](../spikes/bh-a7so.1-harness-wire-format.md)). (2) checkpoint/resume: the premise
> holds but the named mechanism does not — the branch is the checkpoint
> ([`bh-a7so.2`](../spikes/bh-a7so.2-checkpoint-resume.md), partly superseded by
> [`bh-a7so.7`](../spikes/bh-a7so.7-graceful-interrupt.md)). (3) codex: it cannot express the
> roster, so `--provider` bakes ([`bh-a7so.3`](../spikes/bh-a7so.3-codex-provider.md), verified
> empirically by [`bh-a7so.8`](../spikes/bh-a7so.8-codex-empirical.md)).

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

---

## Amendment 2 — the settled contract: provider bakes, the branch is the checkpoint

**Recorded 2026-07-30**, amending this ADR in place. Amends **Amendment 1**'s contract block and
answers its *"What is still unvalidated"* list; through them, **Decisions 3 and 4**.
**Decisions 1 and 2 stand as filed** — see §0, which is a result, not an omission.
**Status:** accepted. `bh-a7so.4` closed **GO**.

Driven by spike molecule `bh-878p` / epic `bh-a7so`, whose five artifacts are in-tree and carry
every measurement cited here:

| Spike | Artifact | Verdict |
|---|---|---|
| `bh-a7so.1` | [harness wire format](../spikes/bh-a7so.1-harness-wire-format.md) | **GO** — six priced deltas |
| `bh-a7so.2` | [checkpoint + resume](../spikes/bh-a7so.2-checkpoint-resume.md) | NO-GO — **partly superseded**, see §7 |
| `bh-a7so.3` | [codex provider](../spikes/bh-a7so.3-codex-provider.md) | NO-GO — `--provider` is an authority argument |
| `bh-a7so.7` | [graceful interrupt](../spikes/bh-a7so.7-graceful-interrupt.md) | **GO** — cooperative cancel works today |
| `bh-a7so.8` | [codex, empirical](../spikes/bh-a7so.8-codex-empirical.md) | **GO** — baking works; necessary, not sufficient |

Both NO-GOs are **scoped and understood, not blocking**: `.3` rejects one clause of the contract
(`--provider` stays runtime) and names the replacement; `.2` rejects one mechanism (`--resume`) and
its own evidence names the thing that actually bounds restart cost. Neither names a constraint that
rules the contract out. Hence **GO**.

### 0. Decisions 1 and 2 survived all five spikes

Stated affirmatively because it is a finding, not a default.

**Decision 1 — beads owns lifecycle state; the runtime owns process scheduling only.**
Reinforced, not merely untouched. `bh-a7so.1` §4 drove a real seat turn and independently verified
that `bd` state moved *through ordinary `bh work` verbs the seat ran itself* — no runtime-only
state anywhere in the path. `bh-a7so.2` §11 measured the failure side: when a holder dies the bead
stays `in_progress` and **`bd reclaim` is the only recovery** (lease TTL 5 min), i.e. the reaper is
in beads, exactly where Decision 1 puts it. And `bh-a7so.2` §8/§12 found the restart bound comes
from *committed git history in the worktree* — the branch is the checkpoint — which is Decision 1's
invariant showing up as a cost number rather than as a rule.

**Decision 2 — three tiers behind one config key.** Unchanged. `bh-a7so.7` §14 designed sibling
notification on interrupt in both tiers and found **the same shape in each, differing only in
transport** (a parent-workflow signal in `temporal`, the existing poll loop's in-flight map in
`local`) — the "one set of semantics across tiers" claim reasoned through rather than assumed.
*Reasoned*, not measured: `bh-a7so.7` §14 is explicitly a design sketch, and no tier was built or
run in any spike.
One caveat, and it is a build requirement rather than a change to the decision: the `local` tier
sketch says `asyncio.TaskGroup` "cancellation propagates down to children", and that is true of the
*task* tree but not the *process* tree — see §5 and `bh-c6dk.5`.

### 1. The settled contract

```text
bh-<seat> --workspace <path> --bead <id> --instructions <file|->
          --session_id <uuid> [--model <tier>]

BAKED AT BUILD  PROVIDER · permissions · permission_mode · mcp_config · plugin_dirs
                · packs digest · seat prompt (including the interrupt protocol and
                  the commit-after-every-step invariant)
STDOUT          SeatRun JSON — the rich channel. `outcome.status` is the source of
                truth whenever stdout parses.
EXIT            0 done · 10 blocked · 11 handoff · anything else = did not complete
                (stdout may be absent).  TARGET, UNBUILT TODAY — until it lands,
                exit 0 means "go read stdout", never "succeeded".
RECOVERY        re-dispatch a fresh turn against the same worktree; the branch is
                the checkpoint. `--resume_session` stays on the binary as an
                optional continuation affordance, NOT the checkpoint primitive.
CANCEL          1. cooperative — write the wrap-up instruction to the seat's stdin
                   (--input-format stream-json); the seat finishes its in-flight
                   tool call, commits `wip: interrupted`, emits INTERRUPT_ACK,
                   exits 0 with a subtype:success envelope
                2. hard — {"type":"control_request","request":{"subtype":"interrupt"}}
                3. signal — SIGTERM to the `claude` process; NEVER SIGINT
                in all three: the scheduler MUST outlive the child and hold the read
                end of the pipe, or the priced envelope goes nowhere
INVARIANT       re-run against an already-advanced bead is a no-op (observed; held
                by agent judgment, unenforced)
```

`SeatRun` / `RoleOutcome` become the contract as they stand. `bh-a7so.1` §4–§5 confirmed stdout
carries exactly one line of well-formed `SeatRun` JSON whenever the process completes, and stderr
stays empty on a completed run (it carries BAML tracebacks only when no `SeatRun` was produced at
all). `bh-a7so.7` §10–§11 cleared the budget fields: `usage` is exact and `cost_usd` is a pure
function of it (agreement to ~0.02 % against list pricing), so both are safe bases for a budget.

### 2. `--provider` bakes — and baking is necessary, not sufficient

Amendment 1 pre-committed to this test in its *"What is still unvalidated"* item 3 — "if codex
cannot express the same allow/ask/deny roster, a runtime `--provider` switch would silently weaken
a baked boundary and **`--provider` must bake too**". It found otherwise.

> **Attribution note.** Earlier drafts of this section, and `bh-a7so.3`, quoted the pre-commitment
> as "`--provider`/`--model` stay runtime … UNLESS spike 3 finds otherwise" and cited it to this
> ADR. That sentence is from the **`bh-a7so` epic's design field**, not from Amendment 1 — the
> ADR's own wording is the item 3 text quoted above. Same commitment, different document; the
> citation was wrong, the argument was not.

Codex has no mechanism equivalent to `ToolRules{allow, ask, deny}` carried as one payload
(`bh-a7so.3` §10–§11). Authority is split across four separately-vocabularied mechanisms —
permission profiles / `sandbox_mode` (fs `read`/`write`/`deny`, net `allow`/`deny`, **no third
`ask` outcome anywhere**), `approval_policy` (a global-or-5-category knob for *when* to pause, and
in non-interactive `exec` answered by an LLM reviewer rather than the operator's roster),
experimental shell-prefix-only `execpolicy` `.rules`, and a fourth MCP-specific approval
vocabulary. `bh-a7so.8` §9 sharpened it further: permission profiles and `sandbox_mode` are
**mutually exclusive**, and `.rules` layers *outside* the sandbox boundary rather than overlapping
it. So a runtime `--provider claude-code` → `--provider codex` switch against an otherwise
unchanged baked bundle would silently swap which differently-shaped engine interprets the audited
roster — worst case falling back to whatever ambient `~/.codex/config.toml` the runtime happens to
supply. **`--provider` is an authority argument and bakes alongside `permissions` /
`permission_mode` / `mcp_config` / `plugin_dirs`.** `--model` / `--tier` are untouched by this
finding and stay runtime, as the roles matrix requires.

`bh-a7so.8` then tested the proposed remedy for real, with the binary installed
(`codex-cli 0.146.0`), and split it into the two experiments it actually is:

- **Config override: validated.** A pointed `CODEX_HOME` + `--profile` deterministically overrides
  ambient permission config, *including under an adversarial same-name profile collision* — the
  seat's own layered file wins (§4). Deny genuinely denies in a real non-interactive agent run, and
  allow allows (§8). Pass `--profile <seat>` **together with an explicit `-P <profile>`** rather
  than relying on the file's own `default_permissions`, and consider `--ignore-user-config` as
  defense in depth.
- **Auth: an unbudgeted cost.** `auth.json` lives *inside* `CODEX_HOME`, so a baked `CODEX_HOME`
  that carries no credentials cannot authenticate — reproduced as a real 401 (§5). Provisioning
  credentials into it is sensitive enough that a generic security control blocked the first
  mechanical attempt (§6). **Credential provisioning is a first-class, reviewed design step, not
  directory scaffolding**: either copy/symlink `auth.json` per baked seat as a secrets-handling
  step (rotation, revocation-on-rebuild, who can read the image), or set
  `cli_auth_credentials_store = keyring` once per machine so auth stops being `CODEX_HOME`-local
  (documented, not live-tested).

So: baking `--provider` is **necessary and not sufficient**. It is still GO, because both
remedies are named and sourced and neither is ruled out by any constraint.

### 3. `RESUME` becomes `RECOVERY` — the branch is the checkpoint, not the session

`bh-a7so.2` measured what nobody had. Against **byte-identical** starting worktrees and the same
task, resuming cost **1.30× a fresh turn** (1.382 and 1.224 over two independent pairs, same
direction both times): a resumed turn replays the dead conversation *including its dead ends*, for
roughly +50 % cache-read tokens, while a fresh turn simply reads `git log`, sees what is committed,
and does the rest. A recovery primitive more expensive than no recovery primitive is not the
checkpoint.

The premise Amendment 1 rested on nevertheless **holds**: restarting after losing 5 of 7 committed
steps cost 0.38–0.42 of a full fresh run (§8). What bounds it is commit granularity in the worktree
(§12) — which is why **commit-after-every-step is baked into the seat prompt** in §1. It is role
behavior, and Amendment 1 already puts role behavior in the binary.

`--resume_session` remains a working, tested affordance on the binary (`bh-a7so.2` §6: resumes in
place after both SIGTERM and SIGKILL, same session id back, does not redo completed steps). It is
demoted from *contract* to *option*.

### 4. `--session_id` is required on create — with a corrected justification

Required on create. `bh-a7so.2` §5 established the mechanism: a caller-minted uuid is honored by
`claude` and echoed back in `SeatRun.session_id`, so identity can be known before the process
starts.

**The justification changed, and the change matters.** `bh-a7so.2` argued a killed run is otherwise
anonymous, recoverable only by slugifying a cwd and scraping `~/.claude/projects/*.jsonl` by mtime.
`bh-a7so.7` retired that: hold the pipe and `session_id` arrives in the abort envelope (§4), and
under stream-json it is on stdout in the `system/init` line about 2 s in, before any work (§9). A
run is attributable from the moment it starts, killed or not.

The surviving reason is different and stronger: **the scheduler needs a stable key before spawn** —
for the `workflow_id` in `temporal`, for the `{bead_id → (proc, stdin, pgid)}` map the `local` loop
must hold to cancel and to notify siblings (§5), and for span/audit correlation from t=0 rather
than from the first envelope. Recording the corrected justification is the point: the same line of
contract, resting on evidence that was not withdrawn.

### 5. `CANCEL` — a three-rung ladder, and it belongs to the scheduler

Amendment 1's contract had no way to stop a running seat. `bh-a7so.7` measured three ways, all on
the documented CLI surface, with no change to `claude` and no new provider capability:

| Rung | Mechanism | Ack | Envelope | Exit | Tree at exit |
|---|---|---|---|---|---|
| 1 cooperative | wrap-up instruction over stream-json stdin | +1.10 s | +38.0 s | 0 | **clean, work committed** |
| 2 hard | `control_request` `{"subtype":"interrupt"}` | **+0.03 s** | +0.09 s | 1 | dirty (tracked) |
| 3 signal | SIGTERM to the `claude` process | — | +0.63 s | 143 | untracked only |

Each rung is strictly faster and strictly less graceful, and **every rung returns a priced
envelope**. The cooperative rung cost 1.32× a hard kill (n=1 against a four-run mean) and bought a
clean tree, a `wip: interrupted` commit, a structured `INTERRUPT_ACK`, and a `subtype: success`
envelope instead of an error to reconstruct.

Four requirements follow, and all four are contract, not implementation detail:

1. **Never SIGINT.** A SIGINT-cancelled run exits **`0`** (§4), colliding head-on with this
   contract's `0 = done`. SIGTERM is identical on envelope, latency, transcript marker and shutdown
   time, and exits `143`, which lands correctly in `anything else`. Use SIGTERM.
2. **The scheduler must hold the pipe and outlive the child.** This is the correction to
   `bh-a7so.2` in §7. Signal the `claude` process, read the envelope, *then* reap the group.
   0.63 s of patience is the entire difference between a priced cancel and a silent one.
3. **The wrap-up protocol is baked into the seat prompt.** `bh-a7so.7` §7 recorded a seat correctly
   flagging an ad-hoc mid-run "the scheduler says stop" message as prompt-injection-shaped, and
   complying *only* because committing is reversible. A cooperative cancel must therefore be a
   trigger for pre-agreed, baked behavior — never a novel instruction the seat has to judge. Rung 2
   stays precisely because it is out-of-band and cannot be declined.
4. **The cancellation channel cannot live in BAML.** `baml.sys.exec`'s `ProcessOptions.stdin` is a
   static string fixed before launch, and `exec` returns a `ShellOutput` for an *already-finished*
   process — no live write handle, no incremental read (§13). `run_resolved_seat` **structurally
   cannot** hold a bidirectional stream-json channel no matter how `seat_argv` is edited. This does
   not block anything; it **relocates ownership**: the harness supplies argv and typing, and the
   supervisor that spawns `claude` and keeps its pipes — `asyncio` in `local`, the activity worker
   in `temporal` — owns cancellation and, by the same argument, sibling notification (§14).

One blocker sits between this ladder and working code, and it is two lines in this org's own repo,
not an upstream limitation: `harness.baml:94` declares `ClaudeResult.result` required, so the abort
envelope fails to deserialize, and `harness.baml:334` panics on `is_error`, discarding
`session_id` / `total_cost_usd` / `usage` a second time even if it parsed. Filed against
baml-harness rather than fixed here.

### 6. `SeatRun` / `RoleOutcome` is the contract — with the deltas priced

`bh-a7so.1` returned GO on the wire format and priced six deltas, listed below. Two of them share a
root cause and
they share a root cause:

- **`--bead` does not exist as an input.** `RoleOutcome.bead_id` is model-echoed prose that nothing
  cross-checks. The round-trip check Amendment 1 wants ("did the agent work the bead it was
  handed?") is **not buildable until `--bead` lands** — there is nothing to check against today.
- **The `0/10/11` taxonomy is unbuilt.** Today exit is a 2-value signal: `0` whenever a `SeatRun`
  came back *whatever its status* (both observed `blocked` results exited 0), `1` whenever BAML
  threw before producing one — a typo'd `--workspace` and an unimplemented `--provider` are
  indistinguishable from outside without string-matching a traceback.
- **`--workspace` is not validated as anything** — it is the literal OS process `cwd`; a bad path
  surfaces as a raw `ENOENT` on the `claude` spawn rather than a typed result.
- **Authority is 100 % runtime today**, carried by `--bundle <path>` alone. `bh-a7so.1` §8 handed
  the shipped binary a hand-written bundle granting `Bash(*)` and it accepted it — the scenario
  Amendment 1's "why bake the bundle" describes is not hypothetical.
- **The resume flags differ in shape** (`--resume_session` / `--session_id` / `--fork_session`) and
  need reconciling against §1.
- **Packaging currency is unmanaged.** `dist/` was found stale against its own committed source
  twice, and rebuilt mid-session by a concurrent process in a shared checkout. A scheduler
  dispatching a packaged binary needs a rebuild-on-deploy step or a version check; neither exists.
  (The codex mirror-image, `bh-a7so.8` §2: the *cask metadata* was stale against the *binary* —
  trust `codex --version` / `codex doctor`, not the package manager.)

The scoping consequence, from `bh-a7so.1`: `--workspace` validation and the exit taxonomy live in
the same thin CLI wrapper layer as `harness.baml:94` / `:334`, so those are plausibly **one unit of
work rather than four**. `bh-c6dk.2` carries that.

### 7. Where `bh-a7so.2` was superseded, and where it still governs

Recorded explicitly because averaging the two spikes would encode withdrawn evidence into a
permanent contract. `bh-a7so.7` **supersedes** `bh-a7so.2` on two specific points, with
measurements, and says so:

1. **"A killed run emits zero bytes" does not generalise.** It was an artifact of killing the
   process holding the *read* end of the pipe at the same instant as the writer — true in both
   scopes `bh-a7so.2` tested, and in neither case a property of `claude`. Signal the child alone,
   or hold the pipe from outside its group, and the same CLI emits a 1347 B envelope 0.63–0.67 s
   later carrying `session_id`, `total_cost_usd`, full `usage`, and a machine-readable
   `terminal_reason`.
2. **"`SeatRun.usage` under-reports by 35–40 %" is retired.** The gap was a *transcript*
   double-count: `~/.claude/projects/<slug>/<sid>.jsonl` writes one line per content block, so one
   API response logs as two `assistant` lines sharing a `message.id` and a `usage` block. Summing
   per line double-counts. Deduplicating by `message.id` reproduces the envelope **to the token on
   both axes**. `usage` was never broken, and `cost_usd` is derivable from it.

What `bh-a7so.2` still **governs**, unretracted and load-bearing:

- **`proc.terminate()` orphans a live agent** (§3) — signal-independent, and still the single most
  important result in the molecule. The ADR's own `local`-tier cancellation signals the harness
  binary only; the `claude` grandchild reparents to init, **runs the entire task to completion**,
  keeps committing to the worktree for minutes after the supervisor believed it had cancelled, and
  spends about a full run's tokens into a pipe nobody holds. `bh-a7so.7` §2 saw the identical
  reparenting shape under a *group* SIGINT. This is a correctness requirement on `bh-c6dk.5`:
  `start_new_session=True` + `os.killpg`, SIGTERM-then-SIGKILL escalation, never SIGINT — and,
  per §5, own the pipe and read the envelope *before* reaping.
- **Resume costs 1.30× fresh** (§7) and the restart bound is 0.38–0.42 of a full run (§8) — the
  numbers behind §3 above.
- **Nothing releases a claim; `bd reclaim` is the recovery** (§11), lease TTL 5 min.
- **The worktree is recoverable because the seat commits per step** (§12) — the bound is a function
  of commit granularity and nothing else.

The two spikes compose into one requirement rather than a compromise: **kill the process group, but
signal the `claude` process and read its envelope first.**

### 8. What the molecule could not settle

Carried forward honestly rather than estimated:

1. **Any provider but `claude-code` for the runtime behaviors.** §3–§5's checkpoint, resume and
   cancel results are Claude Code CLI behaviors this contract *inherits*, exactly as
   `provider.baml`'s "someone else's permission engine enforces and we merely configure it" warns.
   `codex` is `implemented == false` and has promised none of them.
2. **`cli_auth_credentials_store = keyring`** — the clean fix for §2's auth cost is documented, not
   live-tested; testing it needs a real login flow.
3. **The adversarial cooperative cancel** — whether a seat refuses a *destructive* wrap-up
   instruction was not run. It complied in the one measured case explicitly because committing is
   reversible.
4. **Lifecycle under cancel** — all runs used scratch git repos and throwaway beads. Whether an
   interrupted seat can be driven to `bh work submit`, and whether a cooperatively cancelled seat
   should release its own claim, are untested.
5. **The harness's 900 s `timeout_ms`** — never exercised by any spike.
6. **Distribution.** Every cost figure is a single sample per mechanism, one model, one task shape,
   one machine, one CLI version. The *mechanisms* replicated across runs; the *numbers* did not.

### Consequences of Amendment 2

- **A seat binary is compiled for exactly one provider.** Switching providers means rebuilding —
  the same discipline already applied to the permission roster. Seat-image provisioning grows a
  reviewed credential step for any non-`claude-code` provider.
- **The scheduler, not the harness, owns cancellation and sibling notification.** Both tiers gain a
  supervisor that holds the child's stdin/stdout and outlives it. `bh-c6dk.5` (local) and
  `bh-c6dk.4` (temporal) carry this; `bh-c6dk.2` carries the contract line.
- **`bh-c6dk.2` is rescoped** to the §1 contract: `--provider` baked, `RECOVERY` in place of
  `RESUME`, `--session_id` required on create, the `CANCEL` ladder, and the four CLI-wrapper deltas
  (`--bead`, exit taxonomy, `--workspace` validation, envelope survival) treated as one unit of
  work.
- **`bh-c6dk.5` gains a hard correctness requirement** — `start_new_session=True` + `os.killpg`,
  SIGTERM-then-SIGKILL, never SIGINT, pipe held and envelope read before the reap. The
  `asyncio.TaskGroup` sketch in Decision 2 supervises the task tree, not the process tree.
- **Two follow-ons are filed against `beadhive/baml-harness`, not fixed here** — the
  `harness.baml:94` / `:334` envelope discard, and a durable record that the cancellation channel
  cannot live in BAML so nobody rediscovers it by trying.
- **The `EXIT` row is aspirational until the wrapper work lands.** Every caller — including the
  `local` poll loop — must treat exit `0` as "go read stdout", never as "succeeded". This is the
  one place where building against the contract as written, today, would be wrong.
