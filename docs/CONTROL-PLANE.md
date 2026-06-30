# Control plane — `ws rig` / `ws config` (workspace → registry)

The control plane is the **commissioning stage** of AGF: a human-supervised session stands up
and configures rig sites across the workspace, registers them in the workspace registry
(`~/.ws/config.yaml`), and then hands off to a coordinator.

```text
discover → onboard → configure → verify → hand off
```

It runs in a **human-supervised session** — not inside a worktree, not alongside a coordinator.
The superintendent role is the commissioning agent; it does not schedule beads, write code, plan
molecules, or merge. Uniquely among AGF roles, the superintendent does **not** pair with the
`work` skill — its verbs are `ws rig` / `ws config` / `ws sync` / `ws labels`.

> **Head Office is the workspace registry.** Every mutation lands in `~/.ws/config.yaml` via
> the round-trip `ws config` path — never hand-edit the file. `ws rig add`/`rm` are
> registry-only; `ws rig onboard`/`init` reach into the checkout. The plane name is
> **control-plane** (renamable to workspace-plane in a future revision).

See also [AGF.md](AGF.md) for the overall flow and [PLANNING-PLANE.md](PLANNING-PLANE.md) for
the upstream planning stage.

## Head Office — the workspace registry

`~/.ws/config.yaml` is the single source of truth for every rig the superintendent touches:

- `managed_repos` — one entry per registered rig: `{provider, org, repo, prefix, kind}`.
- `orgs` — org → `{code, policy}` mapping that drives prefix derivation and enforcement.
- `dimensions` — label dimensions shared across all rigs.

`ws` round-trips this file with `ruamel.yaml`, preserving comments and the one-flow-mapping
style of `managed_repos`, so `ws rig init` / `ws config set` edits produce minimal diffs.
`ws config show` pretty-prints the current state; `ws doctor` re-runs diagnostics so you can
confirm a rig is registered, healthy, and configured before handing off.

## The 5-step loop

### 1. Discover

Survey what is out there and what is healthy before acting.

- `ws rig ls --available` — diffs git-workspace's tracked repos (from `workspace-lock.toml`,
  zero API calls) against the registry to surface **candidate** repos you could commission.
- `ws labels sync` — reconcile the registry against git-workspace so candidate triplets are
  clean.
- `ws doctor` — report providers, orgs, repo counts, fleet health, and any warnings.
- `ws rig survey` — per-repo fleet table with DIFFICULTY scores for deeper triage; run
  `ws rig survey --available --sort difficulty` to see unregistered candidates ranked by
  onboarding effort before committing to a batch. See
  [RIGS.md — ws rig survey](RIGS.md#ws-rig-survey) for column meanings and DIFFICULTY
  semantics.

This tells you which rigs to commission and which are already standing.

### 2. Onboard

Bring a rig under management. Pick the path:

- **Local folder** — `ws rig onboard <provider/org/repo>` runs rig init in the existing
  checkout, then syncs the hub (no clone needed).
- **Remote** — `ws rig onboard <provider/org/repo> --clone-url <url>` clones the repo down
  (only when the target directory is absent), then inits and syncs.
- **Register-only** — `ws rig add <provider/org/repo>` registers a triplet with no `cwd` and
  no `bd init` (the repo may be uncloned). `ws rig rm <rig-id>` unregisters (registry-only;
  leaves `.beads` and the repo intact).

Add `--prime --claude --skills --observaloop --agents` to `ws rig onboard` to install the
rig's AGF furniture in one shot.

### 3. Configure

Set the rig's control knobs through the round-trip config:

```sh
ws config set otel.enabled true
ws config set otel.endpoint http://localhost:4317
ws config set otel.protocol grpc          # grpc | http/protobuf — validated
ws config set work.review_gate gh:run     # any *.enabled key requires a bool
ws config set my.key '[1,2,3]' --json    # lists/maps via JSON
ws config unset otel.endpoint             # remove a key
```

`set` coerces `true|false` → bool and all-digit strings → int; pass `--json` for lists and
maps. `otel.protocol` is validated against `grpc | http/protobuf` — no silent fallback.
Any `*.enabled` key must be a boolean (error otherwise).

### 4. Verify

Confirm the result before handing off:

```sh
ws config get otel.enabled    # read back a single key; bools print as true/false
ws config show                # pretty-print the full resolved config
ws doctor                     # full diagnostics — rig registered, healthy, and configured
```

Close the loop here. A coordinator launched against an unconfigured or unhealthy rig wastes
the whole downstream session.

### 5. Hand off

You are done at a configured, verified rig. The **human** launches a separate Claude Code
session inside the rig as the coordinator (then merger / reviewer) to drive the actual work.
The superintendent does **not** launch the coordinator, claim a bead, or run any `ws work`
verb — provisioning ends here; dispatch begins in another seat.

## Command surface

| Verb | Does |
|---|---|
| `ws rig ls` | list registered rigs |
| `ws rig ls --available` | list discoverable-but-unregistered candidate repos (zero API calls) |
| `ws rig survey [--available] [--sort disk\|age\|difficulty] [--json]` | read-only fleet table: one row per on-disk repo with classification, commits, disk, and DIFFICULTY |
| `ws rig add <provider/org/repo>` | register a rig from a triplet (no cwd, no `bd init`) |
| `ws rig rm <rig-id>` | unregister a rig (registry-only; leaves `.beads`/repo intact) |
| `ws rig onboard <provider/org/repo>` | end-to-end onboard: clone if absent, init, sync hub |
| `ws rig init` | initialize beads in the current repo and register it |
| `ws rig ready [-v]` | read-only AGF readiness check for the current rig |
| `ws config get <key>` | read a dotted config key (bools as `true`/`false`) |
| `ws config set <key> <value> [--json]` | set a dotted config key (bool/int coercion) |
| `ws config unset <key>` | delete a dotted config key |
| `ws config show` | pretty-print the resolved config |
| `ws config path` | print the resolved `config.yaml` path |
| `ws config init [--force]` | scaffold `~/.ws` from bundled templates |
| `ws labels sync` | reconcile registry vs git-workspace |
| `ws doctor` | full diagnostics: providers, orgs, repo counts, warnings |

## MCP tools (control plane)

When structured I/O is the advantage — a CI agent onboarding many rigs, or an automated
config loop — these MCP tools wrap the same logic:

| Tool | Does |
|---|---|
| `config_set` | delta-apply one dotted key; validation problems returned as `ok: false` |
| `rig_add` | register a triplet; returns `{prefix, kind, registered}` |
| `rig_onboard` | end-to-end onboard; returns `{cloned, registered, prefix, synced, warnings[]}` |
| `rigs_status` | `{candidates[], collisions[], violations[], rigs[]}` — full health view |
| `rigs_available` | `{candidates[], registered[]}` — lighter, for discovery only |

`config_get`, `rig rm`, `ws sync`, and `ws doctor` remain CLI-only (no structured-I/O
advantage, or destructive). See [MCP.md](MCP.md) for the full tool reference.

## Skills and agents

- **`Skill: superintendent`** (`skills/superintendent/SKILL.md`) — the human-supervised role:
  the 5-step loop, when to use each onboard path, how to configure and verify a rig, and the
  rules that bite (especially: no `ws work`). Load this before any control-plane session.
  The superintendent also runs as a role mode: `ws role superintendent` (or
  `claude --agent superintendent`) launches the full role without a manual skill load.
