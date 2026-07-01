# Spike fekf.10 — Resumable-agent feasibility for `review_mode: paired`

**Bead:** · **Seat:** crew/dev-spike · **Type:** research-only (no product code)
**Feeds decision on:** (`review_mode: paired` implementation)

## Question

The operator proposal for `review_mode: paired` wants to keep **one reviewer sub-agent session
alive for a whole epic** and hand off to it **turn-by-turn** as beads complete — via a
resume-style call rather than a fresh `Task` spawn each time — so it **accumulates a running
conversation history** across the epic, paired with a similarly persistent developer session
sharing one worktree/branch (closer to how a human reviews a PR series than N isolated one-shot
reviews).

Concretely: **can a `Task`-spawned AGF sub-agent (reviewer or developer seat) be resumed
turn-by-turn with retained conversation history across multiple bead hand-offs, or can it only be
invoked as one-shot calls?**

Critically distinguished from — and NOT the same as:

- **`ws work resume <id>`** — re-attaches a *worktree* on the bead branch (git/worktree state).
  This is durable **branch** state, not conversation memory.
- **Resuming a sub-agent's conversation/context** — the actual subject of this spike.

## Method

Grepped the rig for any sub-agent dispatch/resume primitive and any consumer of the paired
review mode: `.claude/agents/*.md` (seat tool grants), `skills/**/SKILL.md`
(coordinator / epic-coordinator / reviewer / developer / work), `src/ws/**` (config + dispatch),
`docs/*.md`, and `tests/**`. Searched for `SendMessage`, `send_message`, `resume[_-]agent`,
`continue[_-]agent`, `agent[_-]id`, `agent[_-]handle`, `session id`, `conversation history`,
`review_mode`, `paired`, `fresh`, and the `Task` tool contract as expressed by seat `tools:` lists.

## Evidence

### 1. The only sub-agent primitive in the rig is `Task` — a spawn, never a re-entry

The coordinator seat's entire tool grant (`.claude/agents/coordinator.md:9`):

> `tools: Task, Bash, Read, Grep, Glob, Skill`

There is exactly one sub-agent dispatch verb — `Task` — and every reference in the rig treats it
as a **fresh spawn that returns only a final text result**. There is no tool named `SendMessage`,
no `resume`/`continue`-agent verb, and no agent id/handle returned or accepted anywhere. A repo-wide
grep for `sendmessage|send_message|resume[_-]?agent|continue[_-]?agent|agent[_-]?handle|agent[_-]?id`
across `.claude/`, `skills/`, and `docs/` returned **zero hits**.

### 2. Coordinator loop is spawn-per-bead; continuity is git/bead state, not agent memory

`skills/coordinator/SKILL.md` dispatch loop (quoted):

```text
5. Fan out developers in parallel — launch one `Task` per independent ready bead or group…
   The sub-agent ends at `submit` and reports back its branch + sha.
6. Watch gates … changes-requested → relaunch a `developer` Task (same crew/<name>)
   that runs `ws work resume <id> --as <crew>`, addresses the feedback, and resubmits.
```

On a changes-requested bounce the coordinator **relaunches a Task** — a new spawn — and continuity
is re-established from *durable ws state*: the `wt/bead/<id>` branch (`ws work resume` re-attaches a
**fresh worktree** on that branch), the beads ledger, and the review gate's feedback. Nothing carries
the prior sub-agent's conversation. The `work` skill states this explicitly for `resume`:

> `ws work resume <id>` — After changes-requested: re-attach a **fresh worktree** on the bead branch,
> print feedback, re-assert the claim.

This is exactly the (a)-vs-(b) distinction the bead flags: `resume` restores **branch state**, not
conversation history.

### 3. `review_mode: paired` is a parsed config value with ZERO code consumers

`src/ws/config.py:775` defines the accessor:

```python
def dispatch_review_mode(cfg, entry):
    """Who reviews a dispatched bead: self … | fresh … | paired (two seats sign off).
    Config key `work.dispatch.review_mode`, default self. Unknown values fall back to self."""
    mode = str(dispatch_value(cfg, entry, "review_mode", "self"))
    return mode if mode in ("self", "fresh", "paired") else "self"
```

But `dispatch_review_mode` is **never called** anywhere in `src/` — its only references are its own
definition and `tests/test_config_work.py` (which asserts parsing/fallback only). The value is
surfaced solely as **prose instructions** to a human/agent in `skills/epic-coordinator/SKILL.md:62-64`:

```text
3. Self-resolve the review gate — under the default work.dispatch.review_mode: self you are
   your own reviewer… When the root coordinator overrides review_mode to `fresh` (a separate
   reviewer seat) or `paired` (two seats sign off), do not self-resolve — leave the gate for
   the configured reviewer.
```

So even the *review-seat dispatch* that `paired` presupposes is not wired: there is no code path that
reads the mode and spawns (let alone resumes) a reviewer sub-agent. `fresh` and `paired` today mean
"a human coordinator manually launches a reviewer `Task` and/or approves the gate" — each such launch
is itself a one-shot spawn.

### 4. The reviewer seat is one-shot by construction

`.claude/agents/reviewer.md` grants `tools: Bash, Read, Grep, Glob, Skill` and drives one verb
`ws work review <id>`; its output is a **gate decision** (`approve` / `changes-requested`). It holds
no `Task` and no cross-invocation handle — nothing about the seat retains or re-enters prior context.

### 5. Rig-level acknowledgement that long-running agent sessions are not a thing yet

`src/ws/templates/config.example.yaml:107`:

> No resume of abandoned long-running tasks yet — agents are expected to dispose of worktrees promptly.

The design intent is explicitly **ephemeral, disposable** agent sessions with continuity carried by
git/worktree + beads, not by a persistent agent process.

## Verdict — **NO-GO**

The proposal as specified (one persistent reviewer sub-agent, resumed turn-by-turn with retained
conversation history across an epic, paired with a persistent developer session) is **not feasible**
on the plumbing that exists, at **both** layers the bead asks us to separate:

- **Harness layer:** the only sub-agent primitive available to any AGF seat is the `Task` spawn.
  Within all evidence in this rig, `Task` is one-shot — it returns a final text result, exposes **no
  stable agent id/handle**, and there is **no `SendMessage` / resume / continue-agent** verb to
  re-enter an existing sub-agent with its prior context. A "turn-by-turn resume with retained history"
  primitive is simply absent.
- **AGF `ws` seats layer:** even setting the harness aside, the seats are **not wired** to attempt it.
  `review_mode: paired` is a parsed-but-unconsumed config value; no code dispatches a persistent (or
  even ephemeral) reviewer from it. All cross-bead continuity in AGF is deliberately carried by
  **durable ws state** — the `wt/bead/<id>` branch, the beads ledger, and review-gate feedback — with
  each hand-off re-hydrated by a **fresh** `Task` spawn + `ws work resume` (which re-attaches a
  worktree, i.e. branch state, explicitly **not** conversation memory).

### Concrete blocker

No resumable-sub-agent mechanism exists. Specifically missing: (1) a `Task`-return **agent
id/handle**, and (2) a **re-entry verb** (`SendMessage`/resume-agent) that continues an existing
sub-agent's conversation. Absent both, "one reviewer session alive for the whole epic accumulating a
running history" cannot be built — you can only spawn N independent one-shot reviewer calls, each with
empty context.

## Recommendation for bead .11 (`review_mode: paired`)

**Do not implement `paired` as a persistent-conversation reviewer.** Recommend **closing .11
without implementation** (or re-scoping it), because its premise — a resumable reviewer session — has
no supporting mechanism.

If the *value* behind the proposal (epic-level review continuity, PR-series feel) is still wanted,
re-scope to what the existing stateless primitives already support, e.g.:

- **Molecule-level review** — the reviewer already reviews the whole `mol/<epic>` at once
  (`ws work review <epic>` is molecule-aware and prints **every** child's acceptance). One reviewer
  `Task` at epic-end sees the accumulated change set in a single fresh spawn — no persistence needed.
- **State-carried context** — if a running review narrative is desired, persist it as **durable
  ws/bead state** (gate comments / a review log on the branch) each fresh reviewer spawn reads —
  matching how AGF already carries developer continuity across `resume`.

Both fit the "ephemeral agents, durable state" architecture the rig is built on; neither needs the
resumable-agent primitive that does not exist. Revisit `paired` only if/when the harness grows a
first-class resumable-sub-agent handle.
