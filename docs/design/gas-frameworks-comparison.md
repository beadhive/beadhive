# Three-Way Software-Factory Comparison — GasTown · GasCity · Beadery

*A design reference for understanding where Beadery overlaps, differs from, and can complement the
Gas\* frameworks. Focused on orchestration/process, roles, and naming conventions; tech stack is a
short section at the end.*

Sources: `gastownhall/gastown` and `gastownhall/gascity` design docs (fetched `main`, 2026-07-04);
Beadery's own `plugins/agf/skills/beadery-concepts/` bundle and `docs/` in this repo.

> **Command spelling:** Beadery's concept skill brands the CLI `bdry`; the live repo skills/docs
> use **`ws`** (rename in progress). This document uses the real `ws …` verb names.

---

## 1. TL;DR

All three are the **same substrate wearing three postures** — they are siblings, not competitors.

- **Shared DNA:** all three run on **Beads** (the `bd` CLI) over **Dolt**, store issue history on
  `refs/dolt/data`, model work as a **molecule** (epic + steps + dependency DAG), group with
  **convoys/batches**, isolate workers in **git worktrees**, serialize a merge onto a green line,
  and even share the Gas Town Mad-Max role vocabulary. They are **interoperable at the data layer**.
- **The axis of divergence is not "how autonomous" — it is *who fixes the runtime and the autonomy
  level*:**
  - **GasTown** ships an **opinionated always-on runtime** (tmux + daemon + scheduler + health
    patrol + escalation chain + federation). Roles are **hardcoded Go types**.
  - **GasCity** is the **platform generalization**: "zero hardcoded roles" — roles become
    **config** (packs/agents/formulas/orders); a v2 orchestrator runs a formula as a **graph
    across many agents outside your session**. Still an opinionated runtime, but roles are config.
  - **Beadery** makes the **runtime and autonomy level a dial the operator sets**. It is
    **harness-agnostic**: drives whatever harness is present (today a supervised Claude Code loop),
    designed to scale from **semi-supervised → full-autonomous** as *trust* grows — not a rejection
    of daemons, a deferral of that choice to the operator.
- **Seats are entity-agnostic** — a defining Beadery idea. A *role* is the archetype; a *seat* is a
  role instance a **human *or* an agent** can occupy. You can *be* the dispatcher, drop into the
  reviewer seat, or let agents fill everything. Gas\* seats are effectively agent slots; a human
  talks *to* the Mayor rather than *sitting as* a role.

**Posture at a glance:**

| | GasTown | GasCity | Beadery |
|---|---|---|---|
| Roles are… | hardcoded Go types | config (packs) | markdown seat defs + overrides |
| Runtime | opinionated (tmux daemon) | opinionated (orchestrator daemon) | **harness-agnostic (a dial)** |
| Autonomy | fixed by the daemon | fixed by the orchestrator | **trust dial: supervised → autonomous** |
| Seat occupant | agent | agent | **human *or* agent** |
| Control surface | Go CLI + tmux | Go CLI + tmux | Go/Python CLI **+ MCP server** |

**Punchline:** Beadery is the **harness-agnostic control layer with an autonomy dial**; Gas\* are
**opinionated always-on runtimes**. Because they share the beads/Dolt substrate, Beadery can *drive*
or *feed* a Gas\* runtime rather than reimplement it — turning the dial toward that runtime as trust
grows.

---

## 2. What all three share

They descend from the same design and remain compatible where it counts:

- **Beads over Dolt.** The atomic unit of work is a **bead**; state lives in a Dolt-backed store,
  history on `refs/dolt/data` — no central DB server required.
- **The molecule.** An epic + its child steps + their dependency DAG is the schedulable unit
  (Beadery also calls it a *swarm*; GasCity v2 compiles it to a *graph*).
- **Convoy / batch grouping.** Related beads are bundled and tracked as a unit.
- **Worktree-isolated workers.** Each worker gets its own branch + worktree sandbox.
- **Serialized merge onto a green line.** A single merge owner lands work `--no-ff`, preserving
  history, on an always-green integration branch — merging is distinct from releasing.
- **The Gas Town vocabulary.** Polecat, overseer, the Refinery, and friends appear (verbatim or as
  a deliberate compatibility layer) across all three.

The upshot: a Beadery rig's beads are *mechanically consumable* by a Gas\* runtime and vice-versa.
The rest of this document is about where the three diverge above that substrate.

---

## 3. The axis of divergence — who fixes the runtime & autonomy level

Placing all three on the axis that actually separates them:

- **Hardcoded → config → harness-agnostic.** GasTown bakes roles into Go types and encodes
  architecture in the filesystem (`~/gt/mayor/`, path-derived identity). GasCity's key move is
  "roles are examples, not platform law" — every role becomes user config (a **pack**). Beadery's
  key move is one step further out: it does not own the runtime at all. Seats are markdown
  definitions the harness instantiates; the *same molecule* runs under a supervised Claude Code
  loop today or a more autonomous harness later without re-planning.
- **Autonomy as a fixed choice vs a trust dial.** In Gas\* the daemon/orchestrator *is* the
  autonomy level. Beadery treats autonomy as a **dial the operator turns as trust grows** —
  semi-supervised, with a human resolving gates, all the way to hands-off. This is not
  anti-daemon; it is a deferral of the daemon choice to the operator.
- **Entity-agnostic seats.** A role's duties are defined independent of who performs them, so a
  **human or an agent** can fill any seat. This is the mechanism *behind* the dial: at low trust a
  person sits as overseer/reviewer; at high trust agents fill every seat.
- **Scheduler models differ** (detailed in §6): GasTown = capacity/back-pressure daemon; GasCity =
  no central scheduler (ordering emerges from dependency edges); Beadery = a **planning-time**
  grouping function under explicit guards.

Beadery additionally structures governance into **explicit planes** — *control* (commission rigs),
*planning* (idea → gated molecule), *integration* (execute), plus *release* and *contribution* on
the roadmap. **Planes divvy up both the role types and the bead lifecycle**, and each hands off to
the next without stepping into its role.

---

## 4. Roles — side by side

| Concept | GasTown | GasCity | Beadery |
|---|---|---|---|
| Planner / dispatcher | **Mayor** (unified) | `mayor` (pack agent) | **split:** `planner` (cartographer) + `dispatcher` (overseer) |
| Ephemeral worker | **Polecat** | polecat = scalable/transient pool | `developer` (polecat) — per-bead ephemeral |
| Persistent worker | **Crew** | persistent named agent | — (none) |
| Merge owner | **Refinery** | formula / pack step | `merger` (the Refinery) |
| Watchdog | **Deacon** (+ **Boot**) | orchestrator health patrol (config) | — (seat-holder watches; patrol is a dial-up) |
| Observer / lead | **Witness** (observe-only) | events + waits | `reviewer` (gate-resolving) — partial overlap |
| Relay | **Dog** | exec order (no LLM) | — |
| Final escalation | **Overseer** (human) | — | human operator |
| Commissioner | `gt rig add` | `gc rig add` | **control plane** (supervisor · director · custodian · controller) |
| Research | — | — | `analyst` (read-only sub-agent) |
| Collapsed-epic driver | (Mountain-Eater) | pool / formula | `dispatcher @ batch` (collapsed) |

**Structural choices worth calling out:**

- **Beadery splits the Mayor** into a `planner` (cartographer, planning plane) and a `dispatcher`
  (overseer, integration plane). Gas\* keep the Mayor a single unified role. The split falls
  straight out of Beadery's plane separation — planning is a distinct session with its own gates.
- **Beadery makes `reviewer` first-class and gate-resolving.** GasTown's **Witness** is deliberately
  observe-only — *"you NEVER implement code directly… the Witness literally cannot edit files"* and
  it does **not** gate completion. GasCity has no reviewer role at all; review is a formula gate.
  Beadery's reviewer actually walks the branch and resolves or bounces the gate.
- **Beadery has a distinct control plane** — four seats (supervisor · director · custodian ·
  controller) for commissioning *and* automated workspace/rig management (Head Office registry,
  `ws sync`, rig kinds; custodian provisions, director routes the fleet). Gas\* fold a thinner
  version into the `rig add` CLI verb.
- **Beadery has no built-in watchdog daemon (yet).** At supervised dial settings the seat-holder
  (human or agent) is the patrol; GasTown's Deacon/Boot health patrol is the *dial-up option* for
  unattended runs, not a rejected concept.
- **Beadery has no persistent worker pool (crew).** Every `developer` is per-bead ephemeral. Its
  **collapsed `dispatcher`** (`dispatcher @ batch` — one agent works a whole epic in one worktree,
  merged once) is a middle ground Gas\* express via pools/formulas.

---

## 5. Naming / vocabulary — side by side

| Concept | GasTown | GasCity | Beadery |
|---|---|---|---|
| Cross-repo top | **Town** (`~/gt`) | **City** (dir + `.gc/`) | **Factory HQ / Head Office / hub** (`~/.ws`) |
| Per-repo | **Rig** | **Rig** | **Rig** (Dolt in `.beads/`) |
| Work unit | **Bead** | **Bead** | **Bead** |
| Epic + steps + DAG | **Molecule** | Molecule (v1) / **graph** (v2) | **Molecule** / **swarm** |
| Epic-of-epics | Epic | Epic | **Workstream** |
| Batch / group | **Convoy** | **Convoy** | **batch group** / collapsed epic |
| Ephemeral item | **Wisp** | **Wisp** (TTL GC) | — (worktree-scoped) |
| Template | **Formula → Protomolecule** | **Formula** | plan spec → filed molecule |
| Config unit | town / rig config | **Pack** + `city.toml` | rig config + `.claude/agents/` overrides |
| Dispatch verb | **sling** | **sling** | `ws work assign` / `claim` |
| Automation | **Plugin** (md + TOML) | **Order** (exec/formula) + **Trigger** | dispatch config (`work.dispatch.*`) |
| Federation | **Wasteland** (DoltHub) | — | — |
| Session refresh | **handoff / seance** | session / provider | (Claude Code compaction) |

Two notes: (1) beware **two senses of "hook"** in GasTown — a *lifecycle* hook (session event) and
a *Hook* (the pinned work-queue bead); they are unrelated. (2) Beadery's reuse of the Gas Town
nicknames (polecat, overseer, the Refinery, the cartographer, the pit crew) is a **deliberate
compatibility bridge**, not coincidence — it keeps the two ecosystems legible to each other.

---

## 6. Orchestration & process — side by side

| Stage | GasTown | GasCity | Beadery |
|---|---|---|---|
| **Core loop** | MEOW: Mayor decomposes → convoy → sling → monitor | write a formula → orchestrator runs it as a graph outside your session | one-terminal today: dispatcher finds ready beads → dispatch → watch gates → serialize merge |
| **Dispatch** | `gt sling` + capacity-controlled **scheduler daemon** | `gc sling` → compiled **control-bead graph** (check/retry/fan-out/tally/drain) | `ws work assign`/`claim` with **fanout / collapsed / auto** modes |
| **Scheduling** | back-pressure daemon (`scheduler.max_polecats`, batch size/heartbeat, **circuit breaker** at 3 failures) | **no central scheduler** — ordering emerges from blocking `needs` edges | **planning-time** `ws work schedule`: forms groups (child epics / planner batches / private chains / singletons) under **four guards** (cohesion, size cap, single model tier, single review gate) |
| **Escalation** | 3-tier severity-routed chain (see §7) | waits + formula gates + health patrol | flat MVP: developer → HQ → director (see §7) |
| **Merge** | **Refinery**, Bors-style **batch-then-bisect** | pack / formula merge step | **`merger` seat**: serialized merge-slot, `--no-ff`, never-squash, tiered retention; rebased-retry on trivial divergence |

The scheduling contrast is the sharpest: GasTown schedules for **capacity** (don't spawn N polecats
and exhaust rate limits), GasCity doesn't centrally schedule at all (the **graph** self-orders via
dependencies), and Beadery schedules at **plan time** for **cohesion and blast-radius** (a batch
fails as a unit, so keep groups small, single-tier, single-gate).

---

## 7. Escalation hierarchy / chain of responsibility

| Aspect | GasTown | GasCity | Beadery |
|---|---|---|---|
| Model | **Rich 3-tier chain** | Thin — gates + patrol | **Flat MVP, explicit** |
| Chain | agent `gt escalate -s <SEV>` → **Deacon** (t1) → **Mayor** (t2) → **Overseer**/human (t3) | formula `[steps.gate]` (`human`/`mail`) + `gc wait` + orchestrator **health patrol** (restart/backoff/reconcile) | **developer → HQ → director** (the **terminal router**); *"no auto-routing exists yet"* |
| Severity routing | **Yes** — P0 (bead+mail+email+SMS) / P1 / P2 | — | — (flat; a dial-up borrow) |
| Auto-re-escalate | **Yes** — unacked ~4h bumps severity to a max | — | — (a dial-up borrow) |
| Entry / verbs | `gt escalate`; each tier resolves → re-sling or forward up | mail bead (`type:message`), gate resolution | `ws escalate '<msg>'` (developer, fire-and-forget → `intake:untriaged` + `origin:escalation`); dispatcher bounces up with `ws work reroute <id> --super <seat>`; merger **aborts + escalates** on unresolvable conflict (never drops work) |

**Reading:** GasTown has by far the most developed chain — severity classes with distinct
notification fan-out, and automatic re-escalation of unacknowledged items. Beadery's is a
**deliberate flat MVP** with a clean **terminal-router** seat (the director). GasCity folds
escalation into gates + health-patrol rather than naming a chain. The natural Beadery borrow is
GasTown's *severity routing* + *auto-re-escalate*, dialed up as autonomy grows.

---

## 8. Cross-rig report & intake routing

**Do Gas\* have an equivalent? — No first-class equivalent; only approximations.** This is a
Beadery differentiator.

**Beadery (a first-class, named flow):**

- **Report into any owned rig:** `ws report` (cross-rig channel) lands an item as
  `intake:untriaged` + `origin:report`. The director can report to *any* rig; other seats
  **escalate up to HQ** rather than crossing rig boundaries directly.
- **One source-agnostic queue, channel = `origin`:** `report` | `github` | `import` all land in the
  single `intake:untriaged` queue — membership *is* the state.
- **Director intake inbox → triage → fan to 0..N rigs:** `ws hq intake` (fleet-wide,
  aggregated across the hub) is the inbox; typed disposition verbs route each item:
  - `ws work reroute <id> --to <rig>` — re-file into the right rig
  - `ws work reroute <id> --super <seat>` — hold in the fleet inbox for a second look
  - `ws work accept <id> [--type T] [--priority P]` — HQ owns it → backlog
  - `ws work reject <id> --reason "…"` — close with a reporter-visible reason
  - `ws work promote <id>` — hand a feature/epic-shaped item to the planner (`intake:promoted`)
- **Per-rig intake queue for prioritization:** `ws work intake` (this rig), with `bd
  find-duplicates` surfacing dupes so a colliding request isn't triaged as new.
- **MCP surface:** `ws://work/intake`, `ws://work/intake/dupes`, `ws://hq/intake`.

**Gas\* approximations:**

- **GasTown** — no `intake:untriaged` triage queue. Cross-rig work is **Mayor-mediated** via
  `routes.jsonl` (prefix → rig) + the **mail** system + convoy distribution; **Wasteland** is the
  cross-*town* task marketplace (post/claim/reputation). Routing exists; a *triage inbox* does not.
- **GasCity** — **mail** beads (`type:message`) + **orders/triggers** + sling routing by `needs`
  edges; rig isolation by bead-ID prefix. No director-style inbox that fans one intake to
  0..N rigs.

**Reading:** Beadery's report → HQ-inbox → triage → (0..N rigs) → per-rig-queue pipeline, with
dedup and typed disposition, is **more explicit than either Gas\* framework**. It is a strength to
lean into — and a candidate to expose as a *service other factories consume*.

---

## 9. Tooling & ergonomics — git/gh, MCP, safe ops, guided setup

**Do Gas\* have equivalents?** Headline: **Gas\* are Go CLIs driving agents over tmux/hooks and
expose no MCP server; Beadery is MCP-native with typed tools + subscription resources.** That is the
biggest ergonomic gap.

| Capability | Beadery (`ws` / MCP) | GasTown | GasCity |
|---|---|---|---|
| **Review a bead's history pre-merge** | `ws work show <id>` + MCP `ws://work/show/{id}` (base commit, `max_commits`, flagged commits); `submit` rejects noisy history | sandbox branch/worktree per step; Refinery inspects at merge | pack/formula step |
| **Safe rebase / squash, no data loss** | `ws work refine` → **backup branch** `wt/bead/<id>.refine-<ts>` + **byte-identical gate** (`git diff --quiet backup tip`); aborts & restores on conflict/gate-fail. `--autosquash`/`--plan`/`--since`/`--dry-run`. MCP `work_refine` | data lifecycle DECAY → COMPACT → FLATTEN (rebase/squash) | — |
| **Safe merge / conflict handling** | merger has **no Edit/Write** → abort + escalate; `--no-ff`, never-squash; **rebased-retry** onto integration tip; staleness backstop escalates rather than rewriting a shared land | Refinery **batch-then-bisect** (Bors-style) | pack/formula merge step |
| **Clean workspace, no data loss** | `ws worktree status` → **7-state classification** (SAFE = closed + ancestor + clean, conservative conjunction; always fresh metadata) → `ws worktree prune` removes **only SAFE**; `ws rig retire`/`archive`/`archive prune` = soft-archive default (reversible), `--confirm`/`--purge` safety gate — *"a repo never loses data without the operator's consent."* MCP `ws://worktrees` | worktree cleanup exists; no SAFE-guarded prune surfaced | — |
| **Fleet-wide stats over MCP** | `ws doctor` (Fleet Health: dirty/unpushed repos, reclaimable disk; Disk Usage by rig) + MCP `ws://doctor`, `ws://rigs/status`, `ws://rigs/survey`, `ws://plans`, `ws://work/ready`, `ws://work/schedule/{epic}`; metadata-cache perf work | `gt status`/`gt doctor` (CLI, no MCP) | `gc` CLI + events JSONL (no MCP) |
| **Config via MCP for complex schemas** | MCP `config_set` (**delta-apply**, one dotted key, `type: json\|string` coercion, validation returns `{ok,problems}` writing nothing on invalid) + resources `ws://config`, `ws://config/{key}`; `resources/updated` invalidation | `gt config` (CLI) | `gc config` mostly inspect/explain; TOML + generated JSON schemas (`genschema`) — **no MCP tools** |
| **Bead ops via MCP** | `plan_check`, `plan_file` (typed spec → epic, `dry_run`), `bd_create` (bulk typed issues) | `bd`/`gt` CLI | `bd` CLI |
| **Auto-labeling controls** | `ws labels validate\|sync\|report\|allowed\|docs` — enforce-by-default linter (non-zero on violation, `--advisory`); `provider:/org:/repo:` triplet auto-applied by `ws bd create`; MCP `ws://labels/validation` | labels used (e.g. `mountain`) but no registry linter surfaced | labels used; no registry linter surfaced |
| **Guided / agentic setup** | the **control plane** *is* agent-driven onboarding: discover (`ws rig ls --available`, `ws doctor`, `ws rig survey --sort difficulty`) → `rig_onboard` MCP tool (clone/register/`prime`/`claude`/`skills`) → verify → hand off | `gt install` + `gt rig add` (clones, sets up workers) + **`gt doctor --fix`** auto-repair — CLI wizard, not agentic | `gc init` + `gc rig add`; progressive capability **levels 0–8** — CLI, not agentic |

**Reading:**

- **MCP-native structured control has no Gas\* equivalent** — typed config/bead/plan/label tools +
  always-fresh subscription resources, versus Go-CLI + tmux keystroke injection. This is Beadery's
  strongest ergonomic moat.
- **No-data-loss is enforced mechanically** (byte-identical refine gate, SAFE-only prune,
  consent-gated archive) — more explicit than GasTown's decay/compact/flatten.
- **Setup is agentic** via the control plane, where Gas\* ship CLI wizards. The one thing to
  *borrow*: GasTown's **`gt doctor --fix`** auto-repair (Beadery's `ws doctor` reports; the
  custodian acts).

---

## 10. Where Beadery already leads / differentiates

Five strengths to lean into, each contrasted with Gas\*:

1. **Entity-agnostic seats.** Role = archetype; seat = an instance a **human or agent** occupies.
   The operator can *be* the overseer, drop into the reviewer seat, or let agents fill everything —
   a graduated-trust model Gas\* don't express (their seats are agent slots; humans talk *to* the
   Mayor). This is the mechanism behind the autonomy dial.
2. **Harness-agnostic runtime + autonomy dial.** Beadery abstracts the runtime away and scales from
   semi-supervised → full-autonomous as trust grows, instead of shipping one always-on daemon. The
   same molecule can run under a supervised loop today and a more autonomous harness later without
   re-planning.
3. **Opinionated git-history model.** Lossless integration history (merge `--no-ff` at the
   boundary, **never squash there**), **tiered retention** (squash only local checkpoints in
   `refine`, worker-side, *before* submit), a serialized **merge-slot**, and **integration ≠
   release**. A stronger, more explicit stance than GasTown's decay/compact/flatten or GasCity's
   pack-defined merge behavior.
4. **External output quality via the Contribution plane** *(roadmap)* — a dedicated `contributor`
   seat + repo **dossier** (target's CONTRIBUTING/PR-template/DCO + mined conventions), an automated
   **provenance scrub** that hard-blocks factory metadata from leaving, and a **human-only
   `ws work pr` publication gate**. Gas\* have no equivalent quality/provenance boundary for
   contributing *outside* the town/city.
5. **Automated workspace & rig management.** The **control plane** + **Head Office**
   registry + `ws sync` (blobless minimal-clone cache of `refs/dolt/data`) + **rig kinds**
   (org-native / personal / prototype / fork / external) is a more developed commissioning +
   fleet-of-repos story than Gas\*'s `rig add`.

---

## 11. Where Beadery can improve / borrow

Adopt what pays; the autonomy dial means these are **dial-up options**, not rejected non-goals.

- **A lightweight escalation ladder.** Beadery has bounce + `abandon` + a flat terminal-router
  chain but no severity/auto-re-escalate like GasTown's `gt escalate`. Worth a *thin* version (a
  severity label + a re-surface timer) that works whether a human or agent resolves it — a natural
  fit at higher dial settings.
- **Autonomous "grind" durability from Mountain-Eater** — *"the label IS the state, the epic IS the
  thread."* As Beadery turns the dial toward autonomy, borrowing *state-in-labels* durability
  (skip-after-N-failures as a label) hardens long unattended runs.
- **Config-over-hardcode direction from GasCity.** Beadery's seats are already markdown agent defs
  with `.claude/agents/` overrides (softer than GasTown's Go types) — validated direction. A fuller
  pack/type system is worth watching *if* a second role-set appears (YAGNI until then).
- **Health/liveness + session continuity for higher dial settings.** GasTown's health patrol and
  explicit `handoff`/`seance` are what an unattended Beadery run would need — the concrete build to
  reach the full-autonomous end of the dial (not needed at the supervised end).
- **`ws doctor --fix` auto-repair.** GasTown's `gt doctor --fix` auto-repairs; Beadery's `ws doctor`
  reports and the custodian acts. A guarded `--fix` (idempotent, dry-run-first) is a low-risk
  ergonomic borrow.

---

## 12. Where Beadery complements Gas\* (rather than competes)

- **Data-layer interop is the anchor.** All three read/write beads on `refs/dolt/data`; a Beadery
  rig *is* a consumable GasCity rig (isolation by bead-ID prefix / triplet labels). No translation
  layer needed.
- **Beadery plans + gates; a Gas\* runtime can execute the fleet.** Beadery's planning plane
  (planner + analyst, two gates) produces exactly the gated molecule/graph GasCity's v2 orchestrator
  wants to fan out. Being harness-agnostic, Beadery can *drive* that runtime as its autonomy dial
  turns up — the Gas\* daemon becomes **one harness Beadery targets, not a competitor**.
- **Beadery's governance is what Gas\* underspecify** — explicit plane separation (roles *and*
  lifecycle), entity-agnostic seats, a first-class reviewer, the opinionated git-history model, the
  Contribution/external-quality plane, and **control-plane** commissioning. Position Beadery as the
  **graduated-trust control layer** over whichever runtime executes.
- **Beadery's seat model could ship *as a GasCity pack*.** GasCity explicitly says "roles are
  examples, not platform law" and packs are the unit of config. The shared Gas Town nicknames are
  already a deliberate bridge, so publishing the plane-aligned seat model as a pack is a small step.

---

## 13. Tech stack (minor)

| | GasTown | GasCity | Beadery |
|---|---|---|---|
| Language | Go | Go | Python (`ws`) + Claude Code plugin |
| CLI | `gt` | `gc` + `bd` | `ws` (branded `bdry`) + `bd` |
| Runtime | tmux + daemon | tmux/subprocess/exec/k8s + supervisor | **harness-agnostic** (Claude Code today) |
| Roles defined as | Go types | TOML packs (`agent.toml` + prompt template) | markdown agent defs (`agf:<seat>`) + skills |
| Issue store | **Beads / Dolt** (`refs/dolt/data`) | **Beads / Dolt** | **Beads / Dolt** |
| Control surface | Go CLI | Go CLI + generated JSON schemas | CLI **+ FastMCP server** (tools + resources) |
| Observability | OTEL **log records**, `run.id` correlation | events JSONL (`.gc/events.jsonl`) | OTEL via `ws[otel,mcp]` (observaloop) |
| Config | town/rig config | `pack.toml` + `city.toml` (TOML) | `~/.ws/config.yaml` + per-rig config |

**The interop anchor:** all three sit on **Beads + Dolt with history on `refs/dolt/data`**. That
shared substrate is precisely why Beadery need not compete with Gas\* on execution — it can plan,
gate, and govern *above* the same data the Gas\* runtimes execute *below*.
