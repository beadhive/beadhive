# Control plane — governing the factory (`bh rig` / `bh config` → registry)

The control plane governs the **factory itself**: it stands up and configures rig sites across the
workspace, routes work across the fleet, holds config + secrets, observes factory health, and
registers everything in the workspace registry (`~/.ws/config.yaml`). It runs in
**human-supervised sessions** — not inside a worktree, not alongside a dispatcher — and its seats do
**not** pair with the `work` skill (the one structural break from every other Beadflow role).

Its conceptual resources have different blast radii, so the plane is **four separable seats** on a
3-level orchestration spine (**supervisor → director → dispatcher**, where the dispatcher lives one
plane down in Integration):

| Seat | Identity | Owns (conceptual resource) | Decision authority | Aliases |
|---|---|---|---|---|
| **supervisor** | `super/` | the whole factory — cross-plane operations, policy; supervises the other control seats | ultimate / root | mayor · overseer |
| **director** | `dir/` | intake + work routing (intake→plan→work) + the interface to the per-rig dispatchers | high — routes/directs work across the fleet | — |
| **custodian** | `cust/` | config + secrets + repo provisioning + resource cleanup | medium / mechanical — applies, doesn't decide | administrator · caretaker |
| **controller** | `ctrl/` | factory telemetry/efficiency — throughput, health, OTEL of the factory itself | low — read-mostly, no mutation | the gauge |

**Distinctions.** *supervisor* governs and manages the control seats (org root); *director* is the
operations/traffic layer that routes work and talks to the per-rig *dispatchers* — it directs work,
holds no secrets, sets no policy; *custodian* is the only control seat touching **secret/key
material** (its own blast radius → its own identity) and does the mechanical commissioning below;
*controller* only reads. The **custodian** does the hands-on rig commissioning (the 5-step loop),
the **director** fields the fleet-wide intake inbox and routes it, and the **supervisor** owns
policy and launches the other three.

```text
discover → onboard → configure → verify → hand off      (the custodian's commissioning loop)
```

See also [AGF.md](AGF.md) for the overall flow and [PLANNING-PLANE.md](PLANNING-PLANE.md) for
the upstream planning stage.

## Head Office — the partitioned workspace registry

`~/.ws/config.yaml` is the single source of truth for every rig the control plane touches. Its
write authority is **partitioned across the four seats** — least-privilege, no seat holds the union:

| Registry region | Writer | Content |
|---|---|---|
| policy | **supervisor** | operating policy, org policies, cross-plane decisions |
| `managed_repos` / fleet membership | **director** | which rigs are in the fleet (`{provider, org, repo, prefix, kind}`) |
| per-rig config | **custodian** | rig knobs (`otel`, feature flags, prefix, work defaults), secrets/keys |
| — (read only) | **controller** | reads everything; writes only dashboards, never the registry |

- `orgs` — org → `{code, policy}` mapping that drives prefix derivation and enforcement (supervisor
  policy + director fleet membership).
- `dimensions` — label dimensions shared across all rigs.

`bh` round-trips this file with `ruamel.yaml`, preserving comments and the one-flow-mapping style of
`managed_repos`, so `bh rig init` / `bh config set` edits produce minimal diffs. `bh config show`
pretty-prints the current state; `bh doctor` re-runs diagnostics so you can confirm a rig is
registered, healthy, and configured before handing off. Never hand-edit the file — every mutation
lands through the round-trip `bh config` path.

## The supervisor collapse path

A small / single-rig factory does **not** need four seats. It runs just the **supervisor**, which
absorbs the director / custodian / controller scopes — one identity governing, routing,
commissioning, and observing. Split them into their own seats + identities as the factory grows and
the blast radii diverge (a fleet with real secrets and cross-org policy wants the custodian's
secret isolation and the director's routing as separate authorities). The full separation is
designed here so the collapse is a **deliberate merge into the supervisor**, not an accident.

## The commissioning loop (custodian)

### 1. Discover

Survey what is out there and what is healthy before acting.

- `bh rig ls --available` — diffs git-workspace's tracked repos (from `workspace-lock.toml`,
  zero API calls) against the registry to surface **candidate** repos to commission.
- `bh labels sync` — reconcile the registry against git-workspace so candidate triplets are clean.
- `bh doctor` — report providers, orgs, repo counts, fleet health, and any warnings (controller's
  read view feeds this).
- `bh rig survey` — per-repo fleet table with DIFFICULTY scores for deeper triage; run
  `bh rig survey --available --sort difficulty` to see unregistered candidates ranked by onboarding
  effort. See [RIGS.md — bh rig survey](RIGS.md#bh-rig-survey) for column meanings.

### 2. Onboard

Bring a rig under management. Pick the path:

- **Local folder** — `bh rig onboard <provider/org/repo>` runs rig init in the existing checkout,
  then syncs the hub (no clone needed).
- **Remote** — `bh rig onboard <provider/org/repo> --clone-url <url>` clones the repo down (only
  when the target directory is absent), then inits and syncs.
- **Register-only** — `bh rig add <provider/org/repo>` registers a triplet with no `cwd` and no
  `bd init`. `bh rig rm <rig-id>` unregisters (registry-only; leaves `.beads` and the repo intact).

Registering / removing fleet membership is the **director's** write to `managed_repos`; the clone +
`bd init` + config scaffolding is the **custodian's** mechanical work. Onboarding is
zero-footprint by default; add `--claude --skills --observaloop --agents` (the tracked ones
imply `--furnish` — owner-only) to `bh rig onboard` to furnish the rig in one shot.

### 3. Configure

Set the rig's control knobs through the round-trip config (custodian's per-rig region):

```sh
bh config set otel.enabled true
bh config set otel.endpoint http://localhost:4317
bh config set otel.protocol grpc          # grpc | http/protobuf — validated
bh config set work.review_gate gh:run     # any *.enabled key requires a bool
bh config set my.key '[1,2,3]' --json     # lists/maps via JSON
bh config unset otel.endpoint             # remove a key
```

`set` coerces `true|false` → bool and all-digit strings → int; pass `--json` for lists and maps.
`otel.protocol` is validated against `grpc | http/protobuf` — no silent fallback. Any `*.enabled`
key must be a boolean (error otherwise).

### 4. Verify

Confirm the result before handing off:

```sh
bh config get otel.enabled    # read back a single key; bools print as true/false
bh config show                # pretty-print the full resolved config
bh doctor                     # full diagnostics — rig registered, healthy, and configured
```

Close the loop here. A dispatcher launched against an unconfigured or unhealthy rig wastes the
whole downstream session.

### 5. Hand off

You are done at a configured, verified rig. The **human** launches a separate Claude Code session
inside the rig as the **dispatcher** (then merger / reviewer) to drive the actual work. The control
plane does **not** launch the dispatcher, claim a bead, or run any `bh work` verb — provisioning
ends here; dispatch begins on the Integration plane.

### 6. Retire (when needed)

When a rig is no longer needed — a merged fork, a stalled experiment, a repo moved elsewhere —
decommission it with `bh rig retire` (custodian). This reverses onboarding:

```sh
bh rig survey                               # confirm the candidate (look at LAST-COMMIT, AHEAD/BEHIND)
bh rig retire <rig> --dry-run              # preview the full plan; zero mutation
bh rig retire <rig> --backup               # durably push wip branches, then retire
bh rig retire <rig> --backup --confirm     # backup + accept any remainder
```

`bh rig retire` enforces the guardrail contract: **a repo never loses data without the operator's
consent**. It assesses every local branch, refuses on `NEEDS_BACKUP` unless `--backup` or
`--confirm` is given, re-assesses after backup, gates on dirty worktrees and failed teardowns, then
soft-archives the clone (reversible by default; `--purge` to hard-delete). See
[RIGS.md — bh rig retire](RIGS.md#bh-rig-retire) for the full orchestration and guardrail details.

Use `bh rig archive ls` to inspect the graveyard and `bh rig archive prune` to reclaim disk space
after the retention window has passed (default 30 days, controlled by `archive.window_days`).

## Fleet routing (director)

The **director** owns intake + fleet-wide work routing and the interface to the per-rig
dispatchers. The fleet-wide intake inbox is `bh hq intake` (untriaged intake aggregated across every
rig); typed disposition verbs route each item to the right rig (`bh work reroute <id> --to <rig>`),
hold it for a second look (`--super <seat>`), accept/reject, or promote it to a planner. The
director directs work — it holds no secrets and sets no policy. See docs/AGF.md's intake
vocabulary and escalation chain for the source-agnostic queue mechanics.

## Command surface

| Verb | Does |
|---|---|
| `bh rig ls` | list registered rigs |
| `bh rig ls --available` | list discoverable-but-unregistered candidate repos (zero API calls) |
| `bh rig survey [--available] [--sort disk\|age\|difficulty] [--json]` | read-only fleet table: one row per on-disk repo with classification, commits, disk, and DIFFICULTY |
| `bh rig add <provider/org/repo>` | register a rig from a triplet (no cwd, no `bd init`) |
| `bh rig rm <rig-id>` | unregister a rig (registry-only; leaves `.beads`/repo intact) |
| `bh rig onboard <provider/org/repo>` | end-to-end onboard: clone if absent, init, sync hub |
| `bh rig init` | initialize beads in the current repo and register it |
| `bh rig ready [-v]` | read-only rig readiness check for the current rig |
| `bh config get <key>` | read a dotted config key (bools as `true`/`false`) |
| `bh config set <key> <value> [--json]` | set a dotted config key (bool/int coercion) |
| `bh config unset <key>` | delete a dotted config key |
| `bh config show` | pretty-print the resolved config |
| `bh config path` | print the resolved `config.yaml` path |
| `bh config init [--force]` | scaffold `~/.ws` from bundled templates |
| `bh labels sync` | reconcile registry vs git-workspace |
| `bh doctor` | full diagnostics: providers, orgs, repo counts, warnings |
| `bh rig retire <rig> [--dry-run] [--backup] [--confirm] [--purge]` | guarded teardown: assess → backup/consent → worktree teardown → archive + unregister |
| `bh rig archive ls [--json]` | list archived clones with age and size |
| `bh rig archive prune [--older-than N[d]] [--all] [--dry-run]` | reclaim disk from the archive graveyard |

## MCP tools (control plane)

When structured I/O is the advantage — a CI agent onboarding many rigs, or an automated config
loop — these MCP tools wrap the same logic:

| Tool | Does |
|---|---|
| `config_set` | delta-apply one dotted key; validation problems returned as `ok: false` |
| `rig_add` | register a triplet; returns `{prefix, kind, registered}` |
| `rig_onboard` | end-to-end onboard; returns `{cloned, registered, prefix, synced, warnings[]}` |
| `rigs_status` | `{candidates[], collisions[], violations[], rigs[]}` — full health view |
| `rigs_available` | `{candidates[], registered[]}` — lighter, for discovery only |

`config_get`, `rig rm`, `bh sync`, and `bh doctor` remain CLI-only (no structured-I/O advantage, or
destructive). See [MCP.md](MCP.md) for the full tool reference.

## Skills and agents

- **`Skill: supervisor` / `director` / `custodian` / `controller`** — the four human-supervised
  control-plane seats. Load the seat's skill for its scope: the supervisor governs and launches the
  other control seats; the director routes the fleet; the custodian runs the commissioning loop
  (discover → onboard → configure → verify → hand off) and holds config + secrets; the controller
  reads factory telemetry. None pair with `bh work`. A single-rig factory loads only the
  **supervisor**, which absorbs the other three.
- Each control seat also runs as a role mode: `bh role <seat>` (or `claude --agent <seat>`) launches
  the full role without a manual skill load.
