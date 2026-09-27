# Three-Way Software-Factory Comparison — GasTown · GasCity · Beadhive

*A design reference for understanding where Beadhive overlaps, differs from, and can complement the
Gas\* frameworks. Focused on orchestration/process, roles, and naming conventions; tech stack is a
short section at the end.*

Sources: `gastownhall/gastown` and `gastownhall/gascity` design docs (initial comparison fetched
2026-07-04; Gas City revalidated against v1.4.2 and Beads v1.3.0 on 2026-09-27); Beadhive's own
`plugins/agf/skills/beadhive-concepts/` bundle and `docs/` in this repo.

> **Command spelling:** Beadhive's concept skill brands the CLI `bdry`; the live repo skills/docs
> use **`bh`** (rename in progress). This document uses the real `bh …` verb names.
>
> **Current baseline (2026-09-27):** Gas City v1.4.2 is the latest stable release and is explicitly
> tested with Beads v1.3.0.
> Its current controller, formula graph, and Orders make it a more direct bead-work execution and
> orchestration candidate than the July comparison described. This supports investigating Gas City
> as an optional Beadhive runtime; it does not establish that `gc` can safely share Beadhive's
> managed hive database or lifecycle. See [the release notes](https://github.com/gastownhall/gascity/releases/tag/v1.4.2),
> [v1.4.2 installation guide](https://github.com/gastownhall/gascity/blob/v1.4.2/docs/getting-started/installation.md),
> and [formula / pack guide](https://github.com/gastownhall/gascity/blob/main/docs/guides/understanding-packs.md).

---

## 1. TL;DR

All three share Beads concepts and Gas Town lineage, but their runtimes and lifecycle controls are
not interchangeable. The useful framing is **complementary systems with meaningful overlap**.

- **Shared DNA:** all three use the **Beads** work model and Gas Town vocabulary. Gas City v1.4.2
  and Beadhive both target Beads 1.3.0, but that version match is only a compatibility starting
  point: Gas City initializes its own rig stores and controller lifecycle, while Beadhive manages a
  per-hive `bd serve`, leases, gates, and integration policy. Shared data semantics do not by
  themselves make the same live hive safe to use from both systems.
- **The axis of divergence is not "how autonomous" — it is *who fixes the runtime and the autonomy
  level*:**
  - **GasTown** ships an **opinionated always-on runtime** (tmux + daemon + scheduler + health
    patrol + escalation chain + federation). Roles are **hardcoded Go types**.
  - **GasCity** is the **platform generalization**: roles and workflows are config (packs, agents,
    formulas, Orders). Its controller reconciles agent sessions, dispatches scheduled/event-driven
    Orders, and runs formula graphs across agents outside the operator's session.
  - **Beadhive** separates work lifecycle and governance from a swappable role scheduler. The local
    process loop is implemented; Claude Task-tool fanout is documented, and Temporal is not yet
    implemented. Gas City is not currently a Beadhive runtime option. Beadhive still owns the
    bead lifecycle, gates, review, merge, and host setup around whichever runtime executes work.
- **Seats are entity-agnostic** — a defining Beadhive idea. A *role* is the archetype; a *seat* is a
  role instance a **human *or* an agent** can occupy. You can *be* the dispatcher, drop into the
  reviewer seat, or let agents fill everything. Gas\* seats are effectively agent slots; a human
  talks *to* the Mayor rather than *sitting as* a role.

**Posture at a glance:**

| | GasTown | GasCity | Beadhive |
|---|---|---|---|
| Roles are… | hardcoded Go types | config (packs) | markdown seat defs + overrides |
| Runtime | opinionated (tmux daemon) | controller + session providers + formula/Order dispatch | thin scheduler seam; local process loop implemented |
| Autonomy | fixed by the daemon | configured in city/packs and formulas | **operator-controlled, with explicit Beadhive lifecycle gates** |
| Seat occupant | agent | agent | **human *or* agent** |
| Control surface | Go CLI + tmux | Go CLI + tmux | Go/Python CLI **+ MCP server** |

**Punchline:** Gas City now looks like a credible optional execution/orchestration target for Beadhive
bead work, and Beadhive's AGF roles are a strong candidate for a standalone Gas City pack. Both
paths need adapters and lifecycle boundaries; Beadhive remains the stronger owner of setup,
planning, approvals, integration governance, and managed hive infrastructure.

---

## 2. What all three share

They descend from the same design and remain compatible where it counts:

- **Beads work model.** The atomic unit is a **bead** and each system can express linked work as a
  molecule / dependency graph. Gas City v1.4.2 pairs with Beads 1.3.0; Beadhive also runs Beads
  1.3.0, but uses a managed server-mode database and lifecycle services.
- **The molecule.** An epic + its child steps + their dependency DAG is the schedulable unit
  (Beadhive also calls it a *swarm*; GasCity v2 compiles it to a *graph*).
- **Convoy / batch grouping.** Related beads are bundled and tracked as a unit.
- **Worktree-isolated workers.** Each worker gets its own branch + worktree sandbox.
- **Serialized merge onto a green line.** A single merge owner lands work `--no-ff`, preserving
  history, on an always-green integration branch — merging is distinct from releasing.
- **The Gas Town vocabulary.** Polecat, overseer, the Refinery, and friends appear (verbatim or as
  a deliberate compatibility layer) across all three.

The upshot: shared Beads versions make interoperability plausible, not automatic. Gas City's rig
initialization, database ownership, gates, worktree paths, and direct `bd` operations need to be
checked against Beadhive's managed per-hive services before pointing both systems at one hive.

---

## 3. The axis of divergence — who fixes the runtime & autonomy level

Placing all three on the axis that actually separates them:

- **Hardcoded → config → scheduler seam.** GasTown bakes roles into Go types and encodes
  architecture in the filesystem (`~/gt/mayor/`, path-derived identity). GasCity's key move is
  "roles are examples, not platform law" — roles and workflows become user config (a **pack**).
  Beadhive's `Runtime` is a narrow scheduler seam that launches a Beadhive role binary; Beadhive
  still owns bead lifecycle and governance. The seam can host a future GC adapter, but that adapter
  is not implemented today.
- **Autonomy as configuration and explicit governance.** Gas City configures controller behavior,
  pools, formulas, and order triggers. Beadhive makes its human gates, role permissions, and
  integration lifecycle explicit, while its runtime seam chooses how role processes are scheduled.
- **Entity-agnostic seats.** A role's duties are defined independent of who performs them, so a
  **human or an agent** can fill any seat. This is the mechanism *behind* the dial: at low trust a
  person sits as overseer/reviewer; at high trust agents fill every seat.
- **Scheduler models differ** (detailed in §6): GasTown centers capacity/back-pressure; Gas City
  has a controller that reconciles sessions and dispatches formula graphs and scheduled/event
  Orders; Beadhive separates planning-time grouping from its role-process scheduler and bead-owned
  lifecycle.

Beadhive additionally structures governance into **explicit planes** — *control* (commission rigs),
*planning* (idea → gated molecule), *integration* (execute), plus *release* and *contribution* on
the roadmap. **Planes divvy up both the role types and the bead lifecycle**, and each hands off to
the next without stepping into its role.

---

## 4. Roles — side by side

| Concept | GasTown | GasCity | Beadhive |
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

- **Beadhive splits the Mayor** into a `planner` (cartographer, planning plane) and a `dispatcher`
  (overseer, integration plane). Gas\* keep the Mayor a single unified role. The split falls
  straight out of Beadhive's plane separation — planning is a distinct session with its own gates.
- **Beadhive makes `reviewer` first-class and gate-resolving.** GasTown's **Witness** is deliberately
  observe-only — *"you NEVER implement code directly… the Witness literally cannot edit files"* and
  it does **not** gate completion. GasCity has no reviewer role at all; review is a formula gate.
  Beadhive's reviewer actually walks the branch and resolves or bounces the gate.
- **Beadhive has a distinct control plane** — four seats (supervisor · director · custodian ·
  controller) for commissioning *and* automated workspace/rig management (Head Office registry,
  `bh sync`, rig kinds; custodian provisions, director routes the fleet). Gas\* fold a thinner
  version into the `rig add` CLI verb.
- **Beadhive has no built-in watchdog daemon (yet).** At supervised dial settings the seat-holder
  (human or agent) is the patrol; GasTown's Deacon/Boot health patrol is the *dial-up option* for
  unattended runs, not a rejected concept.
- **Beadhive has no persistent worker pool (crew).** Every `developer` is per-bead ephemeral. Its
  **collapsed `dispatcher`** (`dispatcher @ batch` — one agent works a whole epic in one worktree,
  merged once) is a middle ground Gas\* express via pools/formulas.

---

## 5. Naming / vocabulary — side by side

| Concept | GasTown | GasCity | Beadhive |
|---|---|---|---|
| Cross-repo top | **Town** (`~/gt`) | **City** (dir + `.gc/`) | **Factory HQ / Head Office / hub** (`~/.beadhive`) |
| Per-repo | **Rig** | **Rig** | **Rig** (Dolt in `.beads/`) |
| Work unit | **Bead** | **Bead** | **Bead** |
| Epic + steps + DAG | **Molecule** | Molecule (v1) / **graph** (v2) | **Molecule** / **swarm** |
| Epic-of-epics | Epic | Epic | **Workstream** |
| Batch / group | **Convoy** | **Convoy** | **batch group** / collapsed epic |
| Ephemeral item | **Wisp** | **Wisp** (TTL GC) | — (worktree-scoped) |
| Template | **Formula → Protomolecule** | **Formula** | plan spec → filed molecule |
| Config unit | town / rig config | **Pack** + `city.toml` | rig config + `.claude/agents/` overrides |
| Dispatch verb | **sling** | **sling** | `bh work assign` / `claim` |
| Automation | **Plugin** (md + TOML) | **Order** (exec/formula) + **Trigger** | dispatch config (`work.dispatch.*`) |
| Federation | **Wasteland** (DoltHub) | — | — |
| Session refresh | **handoff / seance** | session / provider | (Claude Code compaction) |

Two notes: (1) beware **two senses of "hook"** in GasTown — a *lifecycle* hook (session event) and
a *Hook* (the pinned work-queue bead); they are unrelated. (2) Beadhive's reuse of the Gas Town
nicknames (polecat, overseer, the Refinery, the cartographer, the pit crew) is a **deliberate
compatibility bridge**, not coincidence — it keeps the two ecosystems legible to each other.

---

## 6. Orchestration & process — side by side

| Stage | GasTown | GasCity | Beadhive |
|---|---|---|---|
| **Core loop** | MEOW: Mayor decomposes → convoy → sling → monitor | write a formula → controller runs it as a graph outside your session | plan/gate beads → selected `work.runtime` schedules role binaries → watch lifecycle → serialize merge |
| **Dispatch** | `gt sling` + capacity-controlled **scheduler daemon** | `gc sling` routes bead work; formula v2 runs graph fan-out/retry/tally/drain, with Orders for scheduled/event dispatch | `bh work assign`/`claim` with **fanout / collapsed / auto** modes |
| **Scheduling** | back-pressure daemon (`scheduler.max_polecats`, batch size/heartbeat, **circuit breaker** at 3 failures) | controller reconciles agent pools; formula graphs dispatch dependency-ready work; Orders add cron/cooldown/condition/event triggers | planning-time `bh work schedule` forms groups under guards; a separate thin runtime seam schedules Beadhive role binaries |
| **Escalation** | 3-tier severity-routed chain (see §7) | waits + formula gates + health patrol | flat MVP: developer → HQ → director (see §7) |
| **Merge** | **Refinery**, Bors-style **batch-then-bisect** | pack / formula merge step | **`merger` seat**: serialized merge-slot, `--no-ff`, never-squash, tiered retention; rebased-retry on trivial divergence |

The scheduling contrast is now about **which layer owns each decision**: GasTown emphasizes capacity
and back-pressure; Gas City runs an always-on controller that reconciles sessions and dispatches
formula graphs and Orders; Beadhive groups work at plan time for cohesion and blast-radius, then
uses a separate runtime seam for role-process scheduling while `bd` remains authoritative for
claims, gates, approvals, and completion.

---

## 7. Escalation hierarchy / chain of responsibility

| Aspect | GasTown | GasCity | Beadhive |
|---|---|---|---|
| Model | **Rich 3-tier chain** | Thin — gates + patrol | **Flat MVP, explicit** |
| Chain | agent `gt escalate -s <SEV>` → **Deacon** (t1) → **Mayor** (t2) → **Overseer**/human (t3) | formula `[steps.gate]` (`human`/`mail`) + `gc wait` + orchestrator **health patrol** (restart/backoff/reconcile) | **developer → HQ → director** (the **terminal router**); *"no auto-routing exists yet"* |
| Severity routing | **Yes** — P0 (bead+mail+email+SMS) / P1 / P2 | — | — (flat; a dial-up borrow) |
| Auto-re-escalate | **Yes** — unacked ~4h bumps severity to a max | — | — (a dial-up borrow) |
| Entry / verbs | `gt escalate`; each tier resolves → re-sling or forward up | mail bead (`type:message`), gate resolution | `bh escalate '<msg>'` (developer, fire-and-forget → `intake:untriaged` + `origin:escalation`); dispatcher bounces up with `bh work reroute <id> --super <seat>`; merger **aborts + escalates** on unresolvable conflict (never drops work) |

**Reading:** GasTown has by far the most developed chain — severity classes with distinct
notification fan-out, and automatic re-escalation of unacknowledged items. Beadhive's is a
**deliberate flat MVP** with a clean **terminal-router** seat (the director). GasCity folds
escalation into gates + health-patrol rather than naming a chain. The natural Beadhive borrow is
GasTown's *severity routing* + *auto-re-escalate*, dialed up as autonomy grows.

---

## 8. Cross-rig report & intake routing

**Do Gas\* have an equivalent? — No first-class equivalent; only approximations.** This is a
Beadhive differentiator.

**Beadhive (a first-class, named flow):**

- **Report into any owned rig:** `bh report` (cross-rig channel) lands an item as
  `intake:untriaged` + `origin:report`. The director can report to *any* rig; other seats
  **escalate up to HQ** rather than crossing rig boundaries directly.
- **One source-agnostic queue, channel = `origin`:** `report` | `github` | `import` all land in the
  single `intake:untriaged` queue — membership *is* the state.
- **Director intake inbox → triage → fan to 0..N rigs:** `bh hub intake` (fleet-wide,
  aggregated across the hub) is the inbox; typed disposition verbs route each item:
  - `bh work reroute <id> --to <rig>` — re-file into the right rig
  - `bh work reroute <id> --super <seat>` — hold in the fleet inbox for a second look
  - `bh work accept <id> [--type T] [--priority P]` — HQ owns it → backlog
  - `bh work reject <id> --reason "…"` — close with a reporter-visible reason
  - `bh work promote <id>` — hand a feature/epic-shaped item to the planner (`intake:promoted`)
- **Per-rig intake queue for prioritization:** `bh work intake` (this rig), with `bd
  find-duplicates` surfacing dupes so a colliding request isn't triaged as new.
- **MCP surface:** `beadhive://work/intake`, `beadhive://work/intake/dupes`, `beadhive://hq/intake`.

**Gas\* approximations:**

- **GasTown** — no `intake:untriaged` triage queue. Cross-rig work is **Mayor-mediated** via
  `routes.jsonl` (prefix → rig) + the **mail** system + convoy distribution; **Wasteland** is the
  cross-*town* task marketplace (post/claim/reputation). Routing exists; a *triage inbox* does not.
- **GasCity** — **mail** beads (`type:message`) + **orders/triggers** + sling routing by `needs`
  edges; rig isolation by bead-ID prefix. No director-style inbox that fans one intake to
  0..N rigs.

**Reading:** Beadhive's report → HQ-inbox → triage → (0..N rigs) → per-rig-queue pipeline, with
dedup and typed disposition, is **more explicit than either Gas\* framework**. It is a strength to
lean into — and a candidate to expose as a *service other factories consume*.

---

## 9. Tooling & ergonomics — git/gh, MCP, safe ops, guided setup

**Do Gas\* have equivalents?** Beadhive remains MCP-native with typed tools and subscription
resources. Gas City has expanded beyond a CLI: its supervisor exposes an HTTP API, and v1.4.0 added
a run-centered dashboard/API. The current Gas City docs still do not show a Beadhive-equivalent MCP
tool/resource surface, and its UI is centered on city/runtime state rather than Beadhive's fleet,
setup, and planning lifecycle.

| Capability | Beadhive (`bh` / MCP) | GasTown | GasCity |
|---|---|---|---|
| **Review a bead's history pre-merge** | `bh work show <id>` + MCP `beadhive://work/show/{id}` (base commit, `max_commits`, flagged commits); `submit` rejects noisy history | sandbox branch/worktree per step; Refinery inspects at merge | pack/formula step |
| **Safe rebase / squash, no data loss** | `bh work refine` → **backup branch** `wt/bead/<id>.refine-<ts>` + **byte-identical gate** (`git diff --quiet backup tip`); aborts & restores on conflict/gate-fail. `--autosquash`/`--plan`/`--since`/`--dry-run`. MCP `work_refine` | data lifecycle DECAY → COMPACT → FLATTEN (rebase/squash) | — |
| **Safe merge / conflict handling** | merger has **no Edit/Write** → abort + escalate; `--no-ff`, never-squash; **rebased-retry** onto integration tip; staleness backstop escalates rather than rewriting a shared land | Refinery **batch-then-bisect** (Bors-style) | pack/formula merge step |
| **Clean workspace, no data loss** | `bh worktree status` → **7-state classification** (SAFE = closed + ancestor + clean, conservative conjunction; always fresh metadata) → `bh worktree prune` removes **only SAFE**; `bh rig retire`/`archive`/`archive prune` = soft-archive default (reversible), `--confirm`/`--purge` safety gate — *"a repo never loses data without the operator's consent."* MCP `beadhive://worktrees` | worktree cleanup exists; no SAFE-guarded prune surfaced | — |
| **Fleet-wide stats over MCP** | `bh doctor` (Fleet Health: dirty/unpushed repos, reclaimable disk; Disk Usage by rig) + MCP `beadhive://doctor`, `beadhive://rigs/status`, `beadhive://rigs/survey`, `beadhive://plans`, `beadhive://work/ready`, `beadhive://work/schedule/{epic}`; metadata-cache perf work | `gt status`/`gt doctor` (CLI, no MCP) | supervisor API + run-centered dashboard for city sessions, runs, beads, mail, events, and health; `gc` CLI remains available |
| **Config via MCP for complex schemas** | MCP `config_set` (**delta-apply**, one dotted key, `type: json\|string` coercion, validation returns `{ok,problems}` writing nothing on invalid) + resources `beadhive://config`, `beadhive://config/{key}`; `resources/updated` invalidation | `gt config` (CLI) | `gc config` + TOML and generated JSON schemas; supervisor API handles operational surfaces, but no equivalent typed Beadhive MCP surface is documented |
| **Bead ops via MCP** | `plan_check`, `plan_file` (typed spec → epic, `dry_run`), `bd_create` (bulk typed issues) | `bd`/`gt` CLI | `bd` CLI |
| **Auto-labeling controls** | `bh label validate\|sync\|report\|allowed\|docs` — enforce-by-default linter (non-zero on violation, `--advisory`); `provider:/org:/repo:` triplet auto-applied by `bh bd create`; MCP `beadhive://label/validation` | labels used (e.g. `mountain`) but no registry linter surfaced | labels used; no registry linter surfaced |
| **Guided / agentic setup** | the **control plane** *is* agent-driven onboarding: discover (`bh rig ls --available`, `bh doctor`, `bh rig survey --sort difficulty`) → `rig_onboard` MCP tool (clone/register/`prime`/`claude`/`skills`) → verify → hand off | `gt install` + `gt rig add` (clones, sets up workers) + **`gt doctor --fix`** auto-repair — CLI wizard, not agentic | `gc init` + `gc rig add`; CLI setup with supervisor and pack configuration |

**Reading:**

- **MCP-native structured control has no documented Gas\* equivalent** — Beadhive exposes typed
  config/bead/plan/label tools and subscription resources. Gas City now also has a supervisor API
  and dashboard, so the comparison is no longer "MCP versus CLI only"; the surfaces serve different
  control and lifecycle needs.
- **No-data-loss is enforced mechanically** (byte-identical refine gate, SAFE-only prune,
  consent-gated archive) — more explicit than GasTown's decay/compact/flatten.
- **Setup is agentic** via the control plane, where Gas\* ship CLI wizards. The one thing to
  *borrow*: GasTown's **`gt doctor --fix`** auto-repair (Beadhive's `bh doctor` reports; the
  custodian acts).

---

## 10. Where Beadhive already leads / differentiates

Five strengths to lean into, each contrasted with Gas\*:

1. **Entity-agnostic seats.** Role = archetype; seat = an instance a **human or agent** occupies.
   The operator can *be* the overseer, drop into the reviewer seat, or let agents fill everything —
   a graduated-trust model Gas\* don't express (their seats are agent slots; humans talk *to* the
   Mayor). This is the mechanism behind the autonomy dial.
2. **A swappable runtime seam with Beadhive-owned lifecycle.** The local process loop is the
   implemented default; Claude Task-tool fanout is documented, and Temporal remains unimplemented.
   The runtime seam schedules `bh-<seat>` binaries while Beads stays authoritative for claims,
   gates, approvals, and completion. Gas City could add an external execution option after an
   adapter proves the role contract and single-owner dispatch boundary.
3. **Opinionated git-history model.** Lossless integration history (merge `--no-ff` at the
   boundary, **never squash there**), **tiered retention** (squash only local checkpoints in
   `refine`, worker-side, *before* submit), a serialized **merge-slot**, and **integration ≠
   release**. A stronger, more explicit stance than GasTown's decay/compact/flatten or GasCity's
   pack-defined merge behavior.
4. **External output quality via the Contribution plane** *(roadmap)* — a dedicated `contributor`
   seat + repo **dossier** (target's CONTRIBUTING/PR-template/DCO + mined conventions), an automated
   **provenance scrub** that hard-blocks factory metadata from leaving, and a **human-only
   `bh work pr` publication gate**. Gas\* have no equivalent quality/provenance boundary for
   contributing *outside* the town/city.
5. **Automated workspace & rig management.** The **control plane** + **Head Office**
   registry + `bh sync` (blobless minimal-clone cache of `refs/dolt/data`) + **rig kinds**
   (org-native / personal / prototype / fork / external) is a more developed commissioning +
   fleet-of-repos story than Gas\*'s `rig add`.

---

## 11. Where Beadhive can improve / borrow

Adopt what pays; the autonomy dial means these are **dial-up options**, not rejected non-goals.

- **A lightweight escalation ladder.** Beadhive has bounce + `abandon` + a flat terminal-router
  chain but no severity/auto-re-escalate like GasTown's `gt escalate`. Worth a *thin* version (a
  severity label + a re-surface timer) that works whether a human or agent resolves it — a natural
  fit at higher dial settings.
- **Autonomous "grind" durability from Mountain-Eater** — *"the label IS the state, the epic IS the
  thread."* As Beadhive turns the dial toward autonomy, borrowing *state-in-labels* durability
  (skip-after-N-failures as a label) hardens long unattended runs.
- **Config-over-hardcode direction from GasCity.** Beadhive's seats are already markdown agent defs
  with `.claude/agents/` overrides (softer than GasTown's Go types). A Beadhive-native general pack
  system can wait; the immediate investigation is a Gas City pack as a deployment format for the
  existing AGF roles and skills.
- **Health/liveness + session continuity for higher dial settings.** GasTown's health patrol and
  explicit `handoff`/`seance` are what an unattended Beadhive run would need — the concrete build to
  reach the full-autonomous end of the dial (not needed at the supervised end).
- **`bh doctor --fix` auto-repair.** GasTown's `gt doctor --fix` auto-repairs; Beadhive's `bh doctor`
  reports and the custodian acts. A guarded `--fix` (idempotent, dry-run-first) is a low-risk
  ergonomic borrow.

---

## 12. Where Beadhive complements Gas\* (rather than competes)

- **Version alignment makes a bridge worth testing.** Gas City v1.4.2 explicitly restores
  compatibility with Beads 1.3.0, the version Beadhive currently uses. This does not prove safe
  shared-database operation: Gas City manages city/rig-scoped bead stores, while Beadhive supervises
  one server-mode store per hive and enforces lease, gate, and writer policy. In adopted hives,
  direct `bd` operations can bypass Beadhive's managed lifecycle. Keep one database owner until the
  shared-state spike establishes a safe boundary.
- **Gas City can be explored as either an execution substrate or a delegated dispatcher.** One
  model keeps Beadhive's loop and lifecycle authority while GC only hosts the `bh-<seat>` role
  process; another delegates a molecule to GC's formula controller while Beadhive stands down from
  scheduling that work. The runtime spike compares both and requires exactly one owner for claims,
  session reconciliation, and dispatch. Any Beadhive runtime adapter must also preserve structured
  `SeatRun` capture, cancellation/recovery, and worktree ownership; GC's session-provider interface
  is not the same thing as Beadhive's scheduler protocol.
- **A versioned AGF pack is a separate, promising deliverable.** Map Beadhive roles and skills onto
  Gas City agents, formulas, Orders, commands, and pack config, then publish a pinned release through
  the registry. Publish requests are versioned and reviewed before release; imports can then pin an
  exact version. Keep Beadhive `bh work` operations and approval/review/merge policy as the lifecycle
  authority. A pack supplies city configuration; it does not install Gas City or implement a Beadhive
  runtime adapter.
- **Beadhive remains the stronger infrastructure and governance layer.** Its control plane,
  workspace/hive provisioning, managed `bd serve`, planning gates, intake, and integration controls
  complement Gas City's agent/session and bead-work orchestration. Formula-driven dispatcher loops
  can follow once the runtime and shared-state boundary is settled. UI integration is a later,
  separate question: Gas City already has a run-centered dashboard/API, while a Beadhive integration
  would need to connect to that surface without moving Beadhive setup or planning authority into GC.
  Frame Bridge is a bounded projection/refresh surface, not a general runtime endpoint.

---

## 13. Tech stack (minor)

| | GasTown | GasCity | Beadhive |
|---|---|---|---|
| Language | Go | Go | Python (`bh`) + Claude Code plugin |
| CLI | `gt` | `gc` + `bd` | `bh` (branded `bdry`) + `bd` |
| Runtime | tmux + daemon | controller + tmux/subprocess/exec/ACP/Kubernetes/herdr providers | `work.runtime` scheduler seam; local implemented, Claude documented, Temporal unimplemented |
| Roles defined as | Go types | packs (agents, prompts, formulas, Orders) | markdown agent defs (`agf:<seat>`) + skills |
| Issue store | **Beads / Dolt** | Beads 1.3.0 / Dolt by default; file provider optional | Beads 1.3.0 with managed server-mode service per hive |
| Control surface | Go CLI | Go CLI + supervisor HTTP/API + run-centered dashboard | CLI **+ FastMCP server** (tools + resources) |
| Observability | OTEL **log records**, `run.id` correlation | events JSONL + run-centered dashboard/API | OTEL via `bh[otel]` (observaloop) |
| Config | town/rig config | `pack.toml` + `city.toml` (TOML) | `~/.beadhive/config.yaml` + per-rig config |

**Install and dependency fit:** Gas City's [v1.4.2 install guide](https://github.com/gastownhall/gascity/blob/v1.4.2/docs/getting-started/installation.md)
offers Homebrew (recommended),
direct macOS/Linux release archives, or a source build with Go 1.26+; direct archives and source
builds require manual dependency installation. The guide lists `tmux`, `jq`, `git`, Dolt 2.1.0+,
Beads `bd` (1.0.4 minimum; 1.3.0 tested), and `flock`; `gh` is optional. The Quickstart documents a
file-backed provider that skips `bd`/Dolt/`flock`, but that is not the shared Beadhive hive path.
Gas City's v1.4.2 `deps.env` pins Dolt 2.1.7 and Beads 1.3.0 for its CI contracts.

Beadhive's managed Nix toolchain currently pins `bd`, Dolt, `gh`, `git-workspace`, `git`, `uv`, and
`just`; it does not install `gc`, `tmux`, `jq`, or `flock`. This is host provisioning work, not
something the current `bh plugin` kernel can install. The plugin ADR has a manifest/capability
contract, but dynamic third-party loading, package installation, config migration, and runtime port
implementations remain downstream work. See [Beadhive install](../../INSTALL.md),
[plugin kernel ADR](plugin-kernel-v1-adr.md), and [Gas City install guide](https://github.com/gastownhall/gascity/blob/v1.4.2/docs/getting-started/installation.md).

**The interop anchor:** common Beads versions and concepts make a bridge possible; explicit adapter
and ownership contracts are what will make it safe. Beadhive can keep planning, gates, provisioning,
and merge governance while a Gas City runtime executes selected bead work.

---

## 14. Version-specific update and recommended deeper dives

### What changed in Gas City v1.4.2

- The [v1.4.2 release](https://github.com/gastownhall/gascity/releases/tag/v1.4.2), released 2026-09-18,
  explicitly targets Beads 1.3.0. It fixes fresh managed workspace initialization and documents the
  coordinated schema migration required for existing 1.2.2 databases. This is a real version match
  with Beadhive's current Beads client, not evidence that both supervisors may own one hive.
- The current [controller architecture](https://github.com/gastownhall/gascity/blob/main/engdocs/architecture/controller.md)
  reconciles configured sessions and pools, watches pack/city config, dispatches Orders, and handles
  shutdown. Formula graphs execute dependency-aware work; [Orders](https://github.com/gastownhall/gascity/blob/main/engdocs/architecture/orders.md)
  add scheduled and event-triggered actions. The prior description of Gas City as having "no central
  scheduler" is obsolete.
- The scope expansion includes operator surfaces: Gas City [v1.4.0 added a run-centered dashboard
  and API](https://github.com/gastownhall/gascity/releases/tag/v1.4.0) for runs, sessions, beads,
  mail, and health. A separate [dashboard development repository](https://github.com/gastownhall/gascity-dashboard)
  is building the next UI against the supervisor API, with an explicit plan to fold it back into
  Gas City. That creates future overlap with Beadhive's UI/control surface, but the two projects
  currently manage different lifecycles and scopes.
- The [pack model](https://github.com/gastownhall/gascity/blob/main/docs/guides/understanding-packs.md)
  covers agents, formulas, Orders, commands, skills, MCP/config, and imports. Registry publication is
  supported through `gc pack registry publish`; imports can pin an exact version. A registry pack is
  feasible as an independent artifact from a future Beadhive plugin. The [public registry](https://registry.gascity.com/)
  and [registry implementation](https://github.com/gascity/registry) support direct, versioned
  publish requests from source repositories, followed by staff review.
- This updates the evidence behind Beadhive's earlier decision bead `bh-d75bi`, which kept Gas City
  as a preserved option and rejected runtime convergence at that time. That bead names dual
  reconciliation and writer authority as the primary blockers. v1.4.2 resolves a Beads-version
  mismatch; it does not remove those architecture questions or approve replacing Beadhive's loop.
  There is also an existing agent-hitch pack-emitter investigation (`ah-fo7r`); coordinate with it
  before implementation. This Beadhive spike is scoped to mapping AGF roles/skills, registry release,
  and the `bh plugin` boundary.
- Beadhive should host any first runtime experiment at the host/per-hive boundary. Do not assume a
  Gas City city inside each Bead Frame or worktree is the right unit: GC initializes per-rig state and
  owns a controller lifecycle, while Beadhive's Frame Bridge exposes a limited projection and
  refresh command rather than a general process/runtime port.

### Recommended spike molecules (filed; kickoff remains pending)

Each epic has one research task and one dependent decision task. The evidence task is blocked by a
human kickoff gate; no implementation work has been approved or filed ahead of these verdicts.

| Spike | Epic / swarm IDs | Evidence / decision beads | Question |
|---|---|---|---|
| **Gas City execution integration** | Epic `bh-kp64s` · swarm `bh-wyaf6` | `bh-kp64s.1` · `bh-kp64s.2` | Should Beadhive keep its loop and use GC for session hosting, or delegate a molecule to GC's formula controller? Can either mode preserve the Beadhive role-binary contract with one dispatch/lifecycle owner, safe cancellation/recovery, and clear worktree ownership? What must `bh plugin gc` and host provisioning add? |
| **Shared Beads safety** | Epic `bh-iaubc` · swarm `bh-19q3a` | `bh-iaubc.1` · `bh-iaubc.2` | Can GC v1.4.2 / Beads 1.3.0 safely operate on Beadhive's managed per-hive database, gates, leases, and writer controls, or must it use a separate store? |
| **Beadflow AGF pack** | Epic `bh-lko4x` · swarm `bh-i3wg6` | `bh-lko4x.1` · `bh-lko4x.2` | How do Beadhive roles, skills, and lifecycle conventions map to a versioned Gas City pack, and which functions stay with Beadhive or require a `bh plugin` adapter? |

The first two spikes settle whether Gas City is a viable Beadhive execution option and which system
owns each dispatch scope. The pack spike can proceed independently as a config-mapping study, with
coordination against `ah-fo7r`. Deeper formula/dispatcher automation and UI integration stay out of
scope until these boundaries are understood.
