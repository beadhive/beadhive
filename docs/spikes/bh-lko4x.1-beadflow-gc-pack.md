# Spike `bh-lko4x.1` — Beadflow AGF as a registry-ready Gas City pack

**Bead:** `bh-lko4x.1` · **Seat:** `dev/gc-pack` · **Type:** research-only (no product code)
**Feeds decision on:** `bh-lko4x.2` (Decide Beadflow Gas City pack scope). Related: decision
`bh-d75bi` (converge on packs, not runtime), runtime spike `bh-kp64s`, shared-Beads spike
`bh-iaubc`, agent-hitch investigation `ah-fo7r`.

## Question

Can Beadhive's Beadflow roles, skills, MCP wiring, and lifecycle conventions ship as a
**versioned, registry-publishable Gas City pack** while Beadhive keeps authority over planning,
approvals, review, merge, and setup? Where exactly does declarative pack content stop and a future
`bh plugin gc` (host integration, installation, runtime ownership) begin?

This spike does **not** ask:

- whether Gas City should run Beadhive seats or replace its loop. That is `bh-kp64s`.
- whether Gas City can share a Beadhive hive's Beads database. That is `bh-iaubc`.
- how to build a generic agent-hitch-to-Gas City emitter. That is agent-hitch's `ah-fo7r`.

It also authors no pack, publishes nothing, and does not install `gc`.

## Method

Read-only research on 2026-09-28. No `gc` binary installed, no registry account used, and no
Beads hive written except this bead's own lifecycle.

1. **Gas City v1.4.2** (tag `v1.4.2`, commit `d458216`), read from a local clone. Sources:
   pack spec and guides (`docs/reference/specs/pack-spec.md`,
   `docs/guides/{understanding-packs,shareable-packs,registry-showcase,configuring-an-agent,capabilities-for-coding-agent-users}.md`),
   formula v2 spec, Orders tutorial, `docs/reference/config.md`, and `system-packs.md`. Code
   sources: `cmd/gc/cmd_registry.go`, `internal/packregistry/`, `internal/materialize/`
   (skills, MCP), `internal/hooks/`, `internal/overlay/`, and the bundled `core` pack's Orders.
   Cited as `gascity@v1.4.2:path:line`.
2. **Public registry.** `github.com/gascity/registry` at `1122c721aa94`: README, `sources.toml`,
   `public/registry.toml`, publish validation, tier policy, and ownership docs. The first-party
   catalog `github.com/gastownhall/gascity-packs` at `bc0d35423058`: `gascity`, `bmad`, and
   `superpowers` packs. Both were shallow-cloned read-only and cited as `registry@…` and
   `gascity-packs@…`.
3. **Beadhive.** `docs/AGF.md`, `docs/WORK.md`, `docs/PLANNING-PLANE.md`,
   `docs/INTEGRATIONS.md`, `docs/PLUGIN-AUTHORING.md`, and
   `docs/design/gas-frameworks-comparison.md`. The shipped `bh` Claude Code plugin is
   `github.com/beadhive/claude-plugin` tag `v0.5.0` (`dfa8f518167a`): 11 seat agents, 20 skills,
   `hooks/hooks.json`, `.mcp.json`, `scripts/`, and `output-styles/`. It is cited as
   `claude-plugin@v0.5.0:beadhive/…`. Live CLI surface: `bh plugin --help`.
4. **Decision context.** `bh bd show` for `bh-lko4x`, `bh-lko4x.2`, and `bh-d75bi`, including its
   2026-08-11 correction and Amendment 1.
5. **agent-hitch** (`959eeeae1f4a`, read-only): bead `ah-fo7r`, `packs/beadhive/hitch.yaml`, and
   `src/agent_hitch/capabilities.py`.

## Evidence

### A. What a Gas City pack can carry

1. **Pack contents.** A pack is a directory with a required `pack.toml`. Well-known directories
   are `agents/`, `formulas/`, `orders/`, `skills/`, `mcp/`, `commands/`, `doctor/`,
   `template-fragments/`, `overlay/`, and `assets/`. Pack-level tables are `[[named_session]]`,
   `[[service]]`, `[providers.*]`, `[[patches.agent]]`, `[global]`, and `[[pricing]]`
   (`gascity@v1.4.2:docs/reference/specs/pack-spec.md:80-98`, `:117-132`). The format is
   versioned: `schema = 2`, and a loader rejects an unknown schema or unknown `pack.toml` keys
   (`pack-spec.md:62-64`, `:188-190`).
2. **Agents.** Each agent is `agents/<name>/`, with an optional `agent.toml` and a prompt
   discovered as `prompt.template.md`. `agent.toml` covers `scope` (city/rig), `provider`,
   `option_defaults` (model and `permission_mode`), `args`, `work_query`, `sling_query`,
   session min/max caps, `suspended`, `attach`, `pre_start`, `session_setup`, `overlay_dir`, and
   fragment injection (`pack-spec.md:304-400`). **It has no per-agent tool allow-list field.**
   Model selection is portable through `option_defaults = { model = "sonnet" }`
   (`docs/guides/configuring-an-agent.md:75-90`). Imported agents are namespaced by binding:
   `beadflow.developer`, or `<rig>/beadflow.developer` for a rig import
   (`pack-spec.md:850-860`).
3. **Work routing is overridable.** `work_query` has a three-tier default: in-progress work
   assigned to this session, then ready work assigned to it, then ready work with
   `gc.routed_to=<agent>`. It is documented as overridable "to integrate with external task
   systems". `sling_query` defaults to `bd update {} --set-metadata gc.routed_to=…`
   (`docs/reference/config.md:113-114`).
4. **Skills are materialized, not translated.** Gas City symlinks pack-level `skills/<name>/`
   and role-level `agents/<role>/skills/<name>/` into each provider's skill sink
   (`.claude/skills`, `.codex/skills`, `.opencode/skills`, …)
   (`docs/guides/capabilities-for-coding-agent-users.md:45-61`;
   `internal/materialize/skills.go:60-66`). Agents that share a scope root share a sink.
   `inject_assigned_skills` only adds a prompt appendix saying which skills are "yours"
   (`config.md:130`). It does not restrict visibility.
5. **MCP.** Servers are declared as `mcp/<name>.toml` or `mcp/<name>.template.toml`, with stdio,
   http, or sse transport (`internal/materialize/mcp.go:20-29`, `:105-111`). The code projects
   them into provider-native files: Claude `.mcp.json`, Codex, OpenCode, and others
   (`internal/materialize/mcp_project.go:20-36`). The user guide still says "MCP is list-only
   today" (`capabilities-for-coding-agent-users.md:62-63`). This is doc drift, so check it live
   before relying on projection.
6. **Hooks have no pack directory.** Claude hooks are installed per city, from
   `<city>/.claude/settings.json` or `hooks/claude.json` merged over embedded defaults
   (`internal/hooks/hooks.go:395-402`). A pack can deliver `.claude/settings.json` through
   `overlay/`, which is JSON-merged rather than overwritten
   (`internal/overlay/merge.go:12-21`). There is no `${CLAUDE_PLUGIN_ROOT}` equivalent for hook
   script paths. Pack-directory templating (`{{.ConfigDir}}`) is specified for `[global]` and
   `pre_start`/`session_live` commands (`pack-spec.md:561-576`, `:968-970`), **not** for overlay
   JSON. This is unverified, and it matters.
7. **Commands and doctor checks.** `commands/<path>/run.sh` adds `gc` sub-commands.
   `doctor/<name>/run.sh`, with an optional `fix.sh`, adds `gc doctor` checks
   (`pack-spec.md:502-559`). `requires_gc` is parsed but **not enforced**
   (`pack-spec.md:70-74`), so a version floor must be a doctor check.
8. **Formulas.** Formulas compile to a workflow root bead, step beads, and orchestrator-owned
   control beads (check, retry, fanout), and the orchestrator drives them
   (`docs/reference/specs/formula-spec-v2.md:46-60`). **The gate `type` vocabulary is inert:**
   `human`, `gh:run`, `gh:pr`, `timer`, and `mail` are unvalidated comments, "no bundled watcher
   acts on them", and zero bundled formulas use `gate` (`formula-spec-v2.md:914-919`).
9. **Orders.** An order pairs a trigger (`cooldown`, `cron`, `condition`, `event`, or `manual`)
   with a formula routed to a pool, or with an `exec` script run on the orchestrator. An exec
   script gets no agent, no LLM, and `PACK_DIR` in its environment
   (`docs/tutorials/07-orders.md:7-11`, `:147-158`, `:202-225`). The bundled `core` pack's
   `gate-sweep` order closes **timer and GitHub gates** every 30 s through
   `gc bd gate check --type=gh --escalate`
   (`internal/bootstrap/packs/core/orders/gate-sweep.toml:1-8`;
   `…/assets/scripts/gate-sweep.sh:28-29`). `prune-branches` deletes only `gc/*` branches
   (`…/assets/scripts/prune-branches.sh:2`, `:27`).

### B. Precedent: methodology packs in the registry

1. The public catalog already lists process methodologies as packs, each "as a Gas City build
   factory": `bmad`, `compound-engineering`, and `superpowers`
   (`registry@1122c721aa94:public/registry.toml:4-19`, `:38-53`;
   `gascity-packs@bc0d35423058:bmad/README.md:1-9`). Beadflow would be a peer of these.
2. Their "hard approval gates" are **prompt-enforced**. Superpowers' design-approval step tells
   the agent to read `approval_mode` and "otherwise default to `autonomous`". In `interactive`
   mode the agent parks its own session and mails a human
   (`gascity-packs@bc0d35423058:superpowers/assets/workflows/superpowers-brainstorming/{target}.confirm-design-approval.md:1-8`,
   `:49`; `superpowers/README.md:14-15`). Nothing in the state machine blocks readiness.
3. Integration in the base `gascity` pack ends at `push` / `open_pr` flags on a publish stage
   (`gascity-packs@bc0d35423058:gascity/README.md:146`). There is no serialized local
   `--no-ff` merge, merge slot, or container-branch ladder.

### C. Registry mechanics: source, pinning, publishing

1. **Durable coordinates.** An import stores `source` plus an optional `version`. The source is
   a GitHub `tree/<ref>/<path>` URL. The version is a semver range (`^0.1`) or an exact pin
   (`sha:<40-hex>`). The exact resolved state lives in `packs.lock`, and registry handles such
   as `main:gascity` must never be persisted
   (`docs/guides/understanding-packs.md:91-111`, `:367-389`; `pack-spec.md:258-270`).
   `gc import install` and `gc import check` own resolution. Registry commands are discovery
   only (`understanding-packs.md:292-307`).
2. **The default catalog is one TOML file.** The `main` registry is
   `raw.githubusercontent.com/gastownhall/gascity-packs/main/registry.toml`
   (`internal/packregistry/config.go:20-25`). The hosted registry aggregates listed sources.
   The first listed source wins a name collision, and `trusted = true` yields tier `maintained`
   while everything else is `community`
   (`registry@1122c721aa94:sources.toml:10-17`; `shared/catalogPolicy.ts:1-13`).
   Each release records `version`, `ref`, full `commit`, and a `sha256` content `hash`
   (`public/registry.toml:14-19`). The hash covers the pack subtree's tracked files, modes, and
   contents at that commit (`gascity@v1.4.2:internal/packregistry/content_hash.go:17-19`).
3. **Publishing** runs `gc pack registry publish <pack-root>` against
   `https://registry.gascity.com` (`cmd/gc/cmd_registry.go:26`, `:120-160`). It requires:
   - a clean tree whose `HEAD` matches its upstream branch on a GitHub remote
     (`cmd_registry.go:300-330`, `:392`, `:530`);
   - a name and version taken from `[pack]` or flags;
   - auth from a token, a session, GitHub Actions OIDC, or a Gasworks login.

   The command POSTs to `/api/publish-requests` (`cmd_registry.go:756`). Server-side validation
   checks that the pack name matches and recomputes the hash with `gc pack release hash`
   (`registry@1122c721aa94:server/publish-validation.ts:26-50`, `:68-92`). **Staff review gates
   every release.** Auto-approval exists but is off in production. OIDC proves the repository,
   not branch policy (`docs/verify-pack-ownership.md:40-54`). Ownership verification uses the
   Gas City Registry Verifier GitHub App (`verify-pack-ownership.md:1-38`).

### D. What Beadhive ships today, and what carries Beadflow's authority

1. **The plugin surface** (`claude-plugin@v0.5.0:beadhive/`):
   - **Seats.** 11 seat defs. Frontmatter sets `tools:`, `skills:` preloads, and `model:`. For
     example, merger has `tools: Bash, Read, Grep, Glob, Skill`, with no Edit/Write
     (`agents/merger.md:7-9`), and developer adds Edit/Write (`agents/developer.md:9-11`).
   - **Skills.** 20 skills. `plan`, `replan`, `groom`, and `setup` are slash-invocable.
   - **MCP.** `.mcp.json` registers the `bh-mcp` stdio server (`.mcp.json:1-8`).
   - **Hooks.** `hooks/hooks.json` wires SessionStart preflight and hive context, plus
     PreToolUse `bd-steer` and `approve-readonly`, all through `${CLAUDE_PLUGIN_ROOT}/scripts`
     (`hooks/hooks.json:4-46`).
   - **Output styles.** 3 output styles.
2. **Lifecycle state lives in beads, and `bh` is the only lifecycle writer.** Gates are
   `bd gate`, leases are `bd heartbeat`, and the merge slot is `bd merge-slot`. A runtime "is
   NEVER authoritative about whether a bead is claimed, blocked, approved, or done", and
   `bh work approve` works with no runtime running (`docs/WORK.md:461-470`). Kickoff is a
   `bd gate --type=human` blocking each root (`docs/PLANNING-PLANE.md:284`, `:306`). Merge
   belongs to a separate seat under `bd merge-slot` (`docs/WORK.md:40`). Claims carry an epoch
   token checked against the host lease (`docs/WORK.md:1028-1032`). PR-governed landing
   resolves a `gh:pr` gate inside `bh work land` and writes a squash-proof close reason
   (`docs/WORK.md:38`, `:817`).
3. **Tenets that carry authority:** separate planes and seats, lossless `--no-ff` history, and
   the rule that merging is not releasing (`docs/AGF.md:25-38`). The seat roster is at
   `docs/AGF.md:286`.
4. **`bh plugin` precedent.** `hitch` is off by default, acts only inside explicit verbs, reports
   readiness `na` when disabled, and fails loudly without falling back
   (`docs/INTEGRATIONS.md:199-278`). Plugins declare executables and credentials only as
   prerequisites (`docs/PLUGIN-AUTHORING.md:28-31`).
5. **agent-hitch already has a neutral Beadhive source pack.** `packs/beadhive/hitch.yaml`
   vendors the plugin's agents, skills, commands, MCP, and permissions. It **excludes** hooks,
   output-styles, and scripts as a design task
   (`agent-hitch@959eeeae1f4a:packs/beadhive/hitch.yaml:19-35`), and it declares
   `beadhive.lifecycle.{plan,dispatch,work,review,merge}` (`:53-59`). `ah-fo7r` asks whether
   Gas City fits `TARGET_CAPABILITIES` (`src/agent_hitch/capabilities.py:102`) and requires a
   comparison before any emitter.

### E. Mapping

The table maps each Beadflow concept to its Gas City primitive. "Delegates" means the pack only
tells the agent to call `bh`. The pack does not represent the concept.

| Beadflow concept | Gas City primitive | Fit | Notes / authority |
|---|---|---|---|
| Seat def body (system prompt) | `agents/<seat>/prompt.template.md` | direct | 11 seats map to 11 agent dirs, namespaced as `beadflow.<seat>` |
| `model:` frontmatter | `agent.toml` `option_defaults.model` | direct | Portable label (A2) |
| `tools:` allow-list (merger/reviewer/warden without Edit/Write) | none portable; provider `args` or overlay `permissions` | **lossy** | A safety property, not styling. Merger must abort and escalate, never hand-edit a conflict (D1, A2) |
| `skills:` preload per seat | `agents/<seat>/skills/` + `inject_assigned_skills` | partial | Visibility is shared per sink. The preload becomes a prompt appendix, not scoping (A4) |
| 20 skills (`SKILL.md` + references) | pack `skills/<name>/` | direct | Files are placed, not translated. Slash-invocation is a harness behavior |
| `bh-mcp` server | `mcp/bh.toml` (stdio, `command = "bh-mcp"`) | direct* | *Projection exists in code but the guide says list-only (A5) |
| SessionStart / PreToolUse hooks | `overlay/.claude/settings.json` (JSON-merged) | **lossy** | No plugin-root path variable (A6). Only viable if hook logic moves into `bh` subcommands on `PATH` |
| Output styles | none (fold into prompts) | none | Claude-only, like hitch's exclusion (D5) |
| Slash skills `plan`/`replan`/`groom` | skills; optionally `commands/` (`gc beadflow …` → `bh …`) | partial | Commands are operator shell verbs, not agent slash commands |
| Human-interactive seats (planner, supervisor, reviewer) | `[[named_session]]` `mode = "on_demand"`, `attach = true` | partial | Session hosting is a runtime decision (`bh-kp64s`) |
| Worker seats (developer, dispatcher, merger) | agents with `work_query`/`sling_query` → `bh` | **delegates** | Claim, seat-typing, identity, and signing stay in `bh work claim`. Letting the GC reconciler spawn on demand is runtime ownership (`bh-kp64s`) |
| Planning plane (ideate → file, `bh plan file`) | none (skill + prompt) | **delegates** | GC treats planning as external. No Beadflow formula should compile molecules |
| Kickoff / review / human gates | none enforceable (formula gates inert, B2 prompt-parking) | **cannot represent** | Stays `bd gate --type=human` resolved by `bh plan approve` / `bh work approve` (D2) |
| Lease + epoch fence | none | **cannot represent** | Host lease, epoch CAS, and `guard_primary` are `bh` internals (D2) |
| Worktree per bead, `wt/bead/<type>/<id>`, `integration_base` climb | none (GC uses `gc/*` branches, `work_dir`) | **delegates** | `bh work claim` / `bh worktree path` inside the session |
| Refine, submit history guard, attested-green validation | none | **delegates** | `bh work refine/check/submit` |
| Merge slot, serialized `--no-ff` merge, `finish` ladder | none (publish = push/PR, B3) | **cannot represent** | Merger seat runs `bh work merge` under `bd merge-slot` |
| PR landing (`gh:pr` gate → `bh work land`) | `core` `gate-sweep` also closes gh gates | **conflicts** | A shared store would let GC close the gate without the close reason (A9) → `bh-iaubc` |
| Spike loop (`tag:spike`/`tag:decision`, template) | none needed | direct (convention) | Labels + docs. Replan is a planner skill, not a formula |
| Control plane (onboard, HQ, managed `bd serve`, secrets) | `gc init`/`gc rig add`; doctor checks | **cannot represent** | Pack may *check* `bh hive ready`, never provision |
| Periodic hygiene (`bh doctor`, intake sweep) | `orders/*.toml` exec, `manual` or `cooldown` | possible, defer | Read-only exec orders are safe. Anything that dispatches is a second reconciler (`bh-d75bi` blocker 1) |
| Version floor for `bh`/`bd`/`gc` | `doctor/<name>/run.sh` | direct | `requires_gc` not enforced (A7) |
| Beadflow lifecycle as formulas (`mol-*`) | `formulas/` | **defer** | Formula steps become orchestrator-owned beads with retry and close semantics. That moves lifecycle authority to GC |

## Verdict — **GO**

A registry-ready Beadflow pack is feasible **as a thin, delegating seat-and-skill pack**. Seat
prompts, skills, the `bh-mcp` server, and doctor checks all have direct schema-2 primitives.
Registry publication is a clean, reviewed, commit-and-hash-pinned path (C1–C3). Methodology packs
are established precedent in the catalog (B1).

Authority survives only because the pack **delegates** every lifecycle write to `bh`. Five
concepts cannot be represented in Gas City without moving authority there:

- state-machine human gates (A8, B2);
- the lease and epoch fence;
- the merge slot and `--no-ff` merge ladder (B3);
- seat-typed claim, identity, and signing;
- control-plane setup.

Two further concepts are lossy: per-seat tool scoping and plugin-root hooks.

Encoding Beadflow's lifecycle as formulas or dispatching Orders is **NO-GO for now**. It would make
Gas City's orchestrator a second lifecycle owner, which is `bh-d75bi` blocker 1.

## Recommendation

**For `bh-lko4x.2` (decision).** Close as **GO** on this boundary: the pack is declarative content
that delegates to `bh`, and `bh` stays the only lifecycle writer. Re-enter planning for pack
authoring with formulas, dispatching Orders, and UI **deferred**.

**Pack content (declarative, registry-published):**

- `pack.toml` with `name = "beadflow"`, `schema = 2`, and a `version` that tracks the
  `claude-plugin` release. It has no `[imports]` beyond what doctor checks need.
- `agents/<seat>/` for the 11 seats:
  - prompt from the seat-def body;
  - `option_defaults.model` from `model:`;
  - `scope = "rig"` for integration seats and `"city"` for control seats;
  - the human-interactive seats as `on_demand` named sessions.

  Default worker `work_query`/`sling_query` to a **non-spawning** posture until `bh-kp64s`
  rules on runtime ownership.
- `skills/` holds the 20 skills unchanged, and `mcp/bh.toml` wires `bh-mcp` over stdio.
- `doctor/` checks the `bh`, `bd`, and `bh-mcp` floors, the `gc` version, and that the rig is a
  `bh hive ready` hive (read-only).
- Optionally, `commands/` thin wrappers (`gc beadflow status` → `bh work ready`) and one
  `template-fragments/` AGF hint.
- Illustrative shape only, not authored here:

  ```toml
  # agents/merger/agent.toml (illustrative)
  scope = "rig"
  option_defaults = { model = "sonnet" }
  ```

**Required integration work:**

1. **Move hook logic into `bh`.** Put preflight, hive context, bd-steer, and approve-readonly
   behind `bh` subcommands so that the plugin, the pack's `overlay/.claude/settings.json`, and
   OpenCode all call the same thing on `PATH`. This removes the `${CLAUDE_PLUGIN_ROOT}`
   dependency (A6).
2. **Express tool scoping per provider.** Add provider `args` or overlay `permissions` for
   merger, reviewer, and warden, and document that tool scoping is provider-specific (lossy
   row). A conformance test must show that the Claude provider still denies Edit/Write to the
   merger.
3. **Single-source the content.** Generate the pack from `claude-plugin` (or from agent-hitch's
   `packs/beadhive`) rather than hand-forking it. Settle the generator with `ah-fo7r`. This spike
   supplies the Beadflow-specific semantic mapping. `ah-fo7r` owns format translation and the
   emit-vs-import direction.
4. **Publish from a protected CI job.** Use a GitHub Actions OIDC job on a release tag. It needs a
   clean tree with `HEAD` at upstream (C3), expects staff review for each release, and needs
   ownership verified through the Verifier App. List the pack as `community` tier unless Gas City
   adds the source as trusted. Recommend consumers pin `version = "sha:<commit>"` or an exact
   semver release, not `^`.

**Proposed `bh plugin gc` boundary** (host integration, a Beadhive-side PluginManifest v1 plugin
that is off by default and follows the `hitch` precedent):

- **In scope:**
  - readiness (`gc` present and at a known version; silent `na` when disabled);
  - writing or checking the city's pinned `[imports.beadflow]` and running
    `gc import install`/`gc import check`;
  - mapping a rig to a hive and its path;
  - reporting in `bh hive ready`/`bh doctor`.
- **Out of scope, or owned by other beads:**
  - installing `gc` (the operator does that);
  - publishing the pack (CI does that);
  - session hosting and dispatch (`bh-kp64s`);
  - shared-store policy, including `gate-sweep` closing `gh:pr` gates and other `core` Orders
    (`bh-iaubc`);
  - any UI integration.

**Deferred.** Beadflow formulas (`mol-*` lifecycle), dispatching Orders, GC dashboard/UI
integration, and `requires_gc` enforcement (upstream). Revisit formulas only if `bh-kp64s` makes
Gas City's orchestrator the dispatch owner for some scope. Even then, gates and merge stay `bh`.
