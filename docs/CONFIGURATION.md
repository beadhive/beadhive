# Configuration

Everything `bh` owns on a machine lives under **`~/.beadhive/`** (module: `config.py`).

## Locations & env vars

| Thing | Default | Override | Notes |
|---|---|---|---|
| home | `~/.beadhive/` | `BH_HOME` (legacy alias `WS_HOME`) | base for everything below |
| config | `~/.beadhive/config.yaml` | `BH_CONFIG` (legacy alias `WS_CONFIG`) | host-local config (this file) |
| fleet config | `~/.beadhive/hq/fleet.yaml` | via `BH_HQ` (legacy alias `WS_HQ`) | fleet-wide base layered *under* the host config — see [Fleet + host config](#fleet-host) |
| hub | `~/.beadhive/hub/` | `BH_HUB` (legacy alias `WS_HUB`) | cross-hive aggregation hub (built by `bh sync`) — [HUB](HUB.md) |
| cache | `~/.beadhive/cache/` | `BH_CACHE` (legacy alias `WS_CACHE`) | minimal-clone caches for uncloned hives |
| generated docs | `~/.beadhive/labels.md` | — | `bh label docs` output |
| dolt env | `~/.beadhive/.env` | — | [DOLT](DOLT.md) server secrets |
| dolt compose | `~/.beadhive/docker-compose.yml` | — | [DOLT](DOLT.md) |

`GIT_WORKSPACE` (defaults to `~/workspace`) is **git-workspace's** variable, shared — it's
the root directory (canonical HQ launch directory) from which `bh` derives `<group>/<account>/<repo>`
identity for all cloned hives during initial setup and beyond (the first segment is the repo-group
**path**, not necessarily the provider type — see [INTEGRATIONS.md](INTEGRATIONS.md#git-workspace)).
The integration-plane (and setup skill) set this variable to `~/workspace` if unset. It is not
`bh`-owned; it belongs to git-workspace.

## Scaffolding

```sh
bh config init          # write config.yaml, docker-compose.yml, .env.example into ~/.beadhive
bh config init --force  # overwrite existing
bh config path          # print the resolved config path
```

Templates ship inside the package (`src/beadhive/templates/`).

## Fleet + host config {#fleet-host}

`config.load()` resolves **one effective config** from two files: the fleet-wide base
(`fleet.yaml` in the HQ store — identical on every host) with the host-local `config.yaml`
deep-merged over it. Nested sections merge key-by-key, so a host setting `worktrees.path`
keeps the fleet's `worktrees.ephemeral`; scalars and lists are replaced wholesale.

Which keys belong to which side is **data**, not branching: `config_partition.py` owns the
fleet/host split (`FLEET_PREFIXES` / `HOST_PREFIXES`, longest match wins) plus
`FLEET_HOST_OVERRIDE_ALLOWLIST` — the explicit, currently-empty list of fleet keys a host may
still override.

| Situation | Behavior |
|---|---|
| both files present | merged; host wins only on host keys + allowlisted fleet keys |
| host sets a non-allowlisted **fleet** key | `ConfigError` naming every offending key — never silently ignored, never silently applied |
| no `fleet.yaml` (host has not cloned HQ) | host-only config, unchanged; `bh` warns once per invocation if an HQ store exists but has no `fleet.yaml` |
| no `config.yaml` | fleet-only config |
| neither file | `FileNotFoundError` pointing at `bh config init` |

`load()` is the **read** path. `load_host()` is the **write** path: every read-modify-write
(`bh config set/unset`, the hive registry, `bh hive enable/disable`) loads through it, so
`save()` can never bake fleet-wide truth into a host's own file.

`managed_repos` is one of those fleet-scoped keys: once a host is fleet-managed, the hive
registry (`bh hive init`/`add`/`rm`) writes it straight into the HQ working copy's
`fleet.yaml`, not this host's `config.yaml` — see
[HQ — Fleet writes after init](HQ.md#fleet-writes-after-init) for the local-only-write
caveat and the manual commit/push reconciliation step.

## `config.yaml` schema

```yaml
delimiter: ":"                       # label delimiter (provider:github, …)

# Recognized provider labels (git hosts — the auth/fetch mechanism, not a repo group's
# on-disk path; see INTEGRATIONS.md). A plain list — no codes.
# May be omitted entirely when the git-workspace integration is enabled (loaded from there).
providers: [github, gitlab, gitea]

# org (full name) -> {code, policy}.
#   code:   used in prefixes (ag-infra). If an org is absent, code falls back to
#           sanitize(name)[:2] and policy to personal — so most orgs need no entry.
#   policy: required = org-native repos MUST use "<code>-<repo>" (enforced at hive init)
#           personal = code is only a suggestion
orgs:
  agentguides: {code: ag, policy: required}

# Repos bh ignores entirely (labels sync skips, hive init refuses, doctor de-noises).
exclude:
  orgs: [SimplicityGuy, bcripe-xealth]
  repos: []                          # "<group>/<account>/<repo>" — matched on the repo-group
                                      # PATH, not the provider type (a "contrib" group with
                                      # provider=github excludes as "contrib/…", not "github/…")

# Non-identity label dimensions. open vs closed is decided by whether `values:` is present:
#   no values:    → open set (any value)
#   values: [...] → closed set (only those pass validation)
#   values: []    → closed but reserved (nothing valid yet — locks the dimension)
dimensions:
  component: {description: "Functional area (iac, runtime, docs)."}
  size:      {description: "Effort estimate.", values: [xs, s, m, l, xl]}
  tag:       {description: "Free-form workflow tag."}

# git-workspace is a required dep, not an optional toggle (see INTEGRATIONS.md) — no `enabled`
# flag; bh always reads whatever workspace*.toml it finds.
git_workspace:
  # path: ~/workspace/workspace.toml   # default: glob $GIT_WORKSPACE/workspace*.toml
  # hive_match: flexible                # how `bh -r <id> …` resolves: flexible | prefix | triplet

# Optional orca integration — registers git-workspace clones with orca (see INTEGRATIONS.md).
# Its own `enabled` flag is the only gate (disabled unless set — default false).
orca:
  enabled: false
  # data_path: ~/.config/orca/orca-data.json   # default: platform-aware (see INTEGRATIONS.md)
  # worktrees: false                           # opt in to orca-delegated worktree create/remove
  # worktrees:
  #   enabled: false
  #   fallback: false   # true = degrade to native git on delegation failure (default: hard fail)

# Optional agent-hitch launch integration (see INTEGRATIONS.md). No AND-gate on any other
# plugin; disabled unless the flag below is set (default false). `bh plugin hitch up <target>
# <profile>` only — never a change to bh's default launch path (`bh role`).
hitch:
  enabled: false
  # repo: ~/workspace/github/briancripe/agent-hitch   # required to actually launch
  # command: hitch                                     # override the hitch CLI command/path
  # root: ~/.beadhive/hitch   # persistent Config Directory root (ephemeral: false only)

# Optional local Dolt server (see DOLT.md).
dolt:
  backend: docker                      # colima | docker | podman | none

# Soft-archive graveyard settings (bh hive retire destination).
archive:
  dir: ~/workspace/.archived           # default: $GIT_WORKSPACE/.archived
  window_days: 30                      # default age threshold for `bh hive archive prune`

# Retention for the three backup roots — see docs/design/backup-retention-boundary-adr.md.
backup:
  hq_keep: 5            # dated dirs kept under ~/.beadhive/hq-backups/ (auto-pruned)
  hive_cap_mb: 500       # size (MB) past which `bh backup reclaim --root hive` rotates
  hive_rotate_keep: 3    # rotated .beads/backup.<ts>/ generations kept after a reclaim

# Multi-host policy — the HOST lease (host <-> hive), NOT bd's worker lease (worker <-> issue).
# See docs/design/multi-host-model-adr.md, Amendment 1 (§3 for these numbers, §5 for the
# host-lease vs worker-lease vocabulary split).
# FLEET-scoped, load-bearingly so: two hosts disagreeing about when a lease expires would
# disagree about who may write. Per-host variation comes from that host's `role` in
# hosts/<host_id>.yaml, which SCALES the ttl below — never a per-host override of these keys.
host:
  lease:
    renew_interval: 300                # seconds between renewals while workers are active
    ttl: 1800                          # seconds a lease survives unrenewed before it's takeable

# One entry per managed hive — maintained by `bh hive init` (add) + `bh label sync`.
#   kind: org-native | personal | prototype | fork ; forks add upstream: "owner/name"
#   provider: the repo-group PATH (not necessarily the provider type — see INTEGRATIONS.md);
#             the stored key name is unchanged for backward compatibility.
managed_repos:
  - {"provider": "github", "org": "agentguides", "repo": "infra", "prefix": "ag-infra", "kind": "org-native"}
```

### Notes on the file

- It's the **registry** — the single source of truth ([LABELS](LABELS.md), [HIVES](HIVES.md)).
- `bh` round-trips it with `ruamel.yaml`, preserving comments and the one-flow-mapping-per-line
  style of `managed_repos`, so `bh hive init` / `bh label sync` edits produce minimal diffs.
- There is **no `enforcement:` block** — enforcement is fixed behavior, not config
  ([LABELS](LABELS.md#enforcement)).
- Provider entries carry **no codes** (only org codes go in prefixes).

## `bh config` commands

| Command | Effect |
|---|---|
| `bh config init [--force]` | scaffold `~/.beadhive` from bundled templates |
| `bh config path` | print the resolved `config.yaml` path |
| `bh config show` | pretty-print the resolved config (doctor overview + extras, including per-key [provenance](#scope)) |
| `bh config get <key> [--scope fleet\|host]` | read a dotted config key |
| `bh config set <key> <value> [--json] [--scope fleet\|host]` | set a dotted config key (bool/int coercion) |
| `bh config unset <key> [--scope fleet\|host]` | delete a dotted config key |
| `bh config split [--dry-run]` | one-time migration: split a flat `config.yaml` into `fleet.yaml` + a reduced host config — see [Migration](#config-split) |

### `bh config get`

Reads a single dotted-path key from the resolved config. Booleans print as `true` or
`false`; scalars print verbatim; lists and maps print as compact JSON so the value round-trips
back through `bh config set --json`. Exits 1 (with a message on stderr) when the key is not
set.

```sh
bh config get otel.enabled        # → true
bh config get otel.protocol       # → grpc
bh config get dimensions          # → {"component": {...}, "size": {...}}
```

### `bh config set`

Sets a single dotted-path key and persists the config via the round-trip `ruamel.yaml` path
(comments and `managed_repos` flow style are preserved).

**Coercion rules (no `--json` flag):**

- `true` / `false` → `bool`
- All-digit string → `int`
- Anything else → `str`

Pass `--json` to parse the value as a JSON literal — required for lists and maps, and for
forcing a string `"true"` / `"true"` without coercion.

**Validation:** `otel.protocol` is validated against `grpc | http/protobuf` (error + no
write on mismatch). Any `*.enabled` key must receive a boolean (error otherwise), and a JSON
write to `work.routing.tiers` validates every model, bound, and endpoint before persisting.
Unknown config sections produce a warning but the write proceeds.

```sh
bh config set otel.enabled true
bh config set otel.endpoint http://localhost:4317
bh config set otel.protocol http/protobuf        # validated
bh config set work.max_commits 8
bh config set my.list '[1,2,3]' --json           # list via JSON
bh config set my.map '{"a":1}' --json            # map via JSON
```

### `bh config unset`

Deletes a dotted-path key from the config and persists. Exits 1 when the key is not set.
Useful for removing optional sections (`otel`, `dolt`, etc.) without hand-editing the file.

```sh
bh config unset otel.endpoint
bh config unset dolt              # removes the whole dolt section
```

### `--scope` — targeting a specific layer {#scope}

`get`/`set`/`unset` all take `--scope fleet|host`, which picks the layer directly instead of
the default merged/default view:

- `bh config get` defaults to the **merged view** (`config.load()`); `--scope host` reads only
  `~/.beadhive/config.yaml` as written (so a fleet-only key is invisible); `--scope fleet`
  reads only the HQ working copy's `fleet.yaml`.
- `bh config set` / `bh config unset` default to **`--scope host`** — the host's own file.
  `--scope fleet` writes/deletes in `fleet.yaml` instead (never committed or pushed by these
  commands — that's [`bh hq init`](HQ.md#hq-init)'s job).
- A **host**-scope `set` of a key that belongs to the fleet partition and isn't in
  `FLEET_HOST_OVERRIDE_ALLOWLIST` is refused immediately with the same `ConfigError` message
  `load()` would raise for it — not deferred to the next read.

```sh
bh config get work.validate_cmd --scope fleet    # read straight from fleet.yaml
bh config set orgs '{"agentguides": {"code": "ag"}}' --json --scope fleet  # fleet-wide write
bh config unset archive.window_days --scope host # host-only key — the default scope
```

`bh config show`'s **`# Provenance`** section (bh-e0y8.6) labels every resolved key `fleet`,
`host`, or `override` (present in both files — an allowlisted override, or an unclassified key
both sides happen to set) so a surprising merged value is traceable back to the file that set
it.

### `bh config split` — flat-config migration {#config-split}

A one-time, idempotent migration for any install that predates the fleet/host split: splits an
existing flat `config.yaml` (mixing fleet-wide and host-local keys, the pre-`bh-e0y8` shape)
into `fleet.yaml` + a reduced host `config.yaml`, leaf by leaf, via the SAME
`config_partition.partition_of` classification `load()` merges by — so the result reads back
identical to the config it replaced.

```sh
bh config split --dry-run   # preview both prospective files; writes nothing
bh config split             # perform the split
```

- **Idempotent** — a host with nothing left to split (already reduced to host-only keys) is a
  no-op.
- **Reversible** — the original `config.yaml` is copied to `config.yaml.bak` before anything is
  overwritten.
- **Merges, not replaces, on a second host** — the extracted fleet portion is deep-merged onto
  whatever `fleet.yaml` already exists (from a first host's earlier split), so migrating a
  second host doesn't discard the first host's fleet keys; on a key both sides set, the value
  from the host actually being split wins.
- **Never fired automatically** — unlike the home-directory migration, this is a deliberate,
  operator-invoked step (not wired into any `bh` invocation's best-effort migration hooks),
  since restructuring one file into two is a bigger, more visible change.

The control-plane role that drives these verbs (alongside `bh hive`) is documented in
[CONTROL-PLANE.md](CONTROL-PLANE.md).

## Archive section

The `archive` section controls where `bh hive retire` moves retired clones and when
`bh hive archive prune` considers them eligible for permanent deletion.

| Key | Default | Effect |
|---|---|---|
| `archive.dir` | `$GIT_WORKSPACE/.archived` | Root directory for soft-archived clones |
| `archive.window_days` | `30` | Default `--older-than` age threshold for `archive prune` |

```sh
bh config set archive.dir /mnt/cold/bh-archive   # relocate the graveyard
bh config set archive.window_days 60              # keep archives for 60 days before pruning
bh config get archive.window_days                 # read back → 60
```

Both keys are optional. When `archive.dir` is unset, clones are archived under
`$GIT_WORKSPACE/.archived`. When `archive.window_days` is unset, `archive prune` defaults
to a 30-day window. See [HIVES.md — bh hive archive](HIVES.md#bh-hive-archive) for the full
reclaim workflow.

## Backup section

Three independent backup roots exist — a one-way pre-push HQ snapshot, bd's own periodic
per-hive Dolt backup, and `bh backup`'s manual JSONL interchange mirror — each with a
different owner and a different retention policy. See
[docs/design/backup-retention-boundary-adr.md](design/backup-retention-boundary-adr.md) for
the full boundary/retention design; the `backup` section holds every root's tuning knobs.
Host-scoped: how much of *this host's* disk each root may keep is a machine-local choice, not
fleet policy.

| Key | Default | Effect |
|---|---|---|
| `backup.hq_keep` | `5` | Dated dirs kept under `~/.beadhive/hq-backups/`; pruned automatically right after `bh hq init` takes + verifies a new one. |
| `backup.hive_cap_mb` | `500` | Size (MB) past which `bh backup reclaim --root hive` rotates a hive's `.beads/backup/` (bd's own). Below the cap, reclaim is a no-op. |
| `backup.hive_rotate_keep` | `3` | Rotated `.beads/backup.<timestamp>/` generations kept after a `--root hive` reclaim. |

```sh
bh config set backup.hq_keep 3            # keep fewer HQ pre-push snapshots
bh config set backup.hive_cap_mb 200      # rotate a hive's bd backup sooner
bh backup usage                            # see current size + policy for all three roots
bh backup reclaim --root hive --confirm    # rotate the current hive's bd backup once over cap
```

The JSONL mirror (`bh backup export`) needs no key here — it overwrites a fixed per-hive
path each run, so there is no history to prune under the default destination (see the ADR).

## `claude:` section — seat agent distribution {#claude-section}

The `claude:` section controls how `bh hive init --claude` (and `bh hive onboard --claude`)
vends seat agents and role skills to a hive. All keys resolve per-hive
`entry.claude.<key>` > global `claude.<key>` > default.

| Key | Default | Values | Effect |
|---|---|---|---|
| `claude.source` | `plugin` | `plugin` \| `copy` | How to vend seat agents to hives. |
| `claude.plugin` | `agf` | string | Name of the Claude Code plugin to install. |
| `claude.marketplace` | `.` | string | Marketplace ref passed to `claude plugin marketplace add`. `.` means the repo root itself is the marketplace (works when `bh` is installed from this repo). Use an absolute path or URL for a standalone marketplace. |
| `claude.scope` | `user` | `user` \| `project` | Plugin install scope: `user` (persists across hives) or `project` (local `.claude/` only). |

### `source: plugin` (default)

`bh hive init --claude` runs:

```sh
claude plugin marketplace add <marketplace>
claude plugin install <plugin>@<marketplace> --scope <scope>
```

Seat agents are namespaced `agf:<seat>` and skills are bundled inside the plugin.  Hives do
**not** commit `.claude/agents/` files or a `skills/` directory — agents and skills live in
the user's plugin cache. A local `.claude/agents/<seat>.md` in any hive is a supported
override that outranks the plugin: `bh role <seat>` picks it up automatically.

`bh hive ready -v` passes the `skills` and `agents` checks when the `agf` plugin is installed,
even with no local files.

### `source: copy` (legacy / airgap)

`bh hive init --claude` copies agent defs to `.claude/agents/` and role skills to `skills/`
inside the hive. Works fully offline once the initial copy is done. `bh hive ready` falls back
to the local-files check.

### Local plugin development

The `bh` plugin lives in its own repo, [beadhive/claude-plugin](https://github.com/beadhive/claude-plugin).
When hacking on it, point marketplace at your local clone; `agents_src()` / `skills_src()`
resolve from the installed marketplace clone's plugin dir, so the local tree is always the
source of truth — no install step needed during development.

```yaml
# ~/.beadhive/config.yaml
claude:
  source: plugin        # install the agf plugin at onboard time
  plugin: agf
  marketplace: .        # '.' = the workspace repo root (resolved at install time)
  scope: user           # user-scope persists across all hives
```

## `work.routing` — model capability intent

`work.routing` describes which model routes can serve each complexity tier. It is fleet-wide
configuration and follows the usual precedence: a `managed_repos[*].work.routing` leaf overrides
the corresponding global `work.routing` leaf for that hive.

```yaml
work:
  routing:
    policy: loose                 # loose (default) | strict
    tiers:
      - model: openai/gpt-5-mini
        ceiling: MEDIUM           # omitted floor means SIMPLE
      - model: anthropic/claude-opus-4-1
        floor: COMPLEX
        ceiling: REASONING
        endpoint: primary-gateway # or https://gateway.example/v1
```

`model` is always written as `provider/model-name`. Beadhive validates that shape but does not
freeze a provider or model catalogue into config. `floor` and `ceiling` are inclusive and use the
ordered `SIMPLE | MEDIUM | COMPLEX | REASONING` vocabulary; omitting them means the lowest and
highest tier respectively. A floor above its ceiling is invalid.

`endpoint` is optional. It may be an HTTP(S) URL or an endpoint-profile reference (a profile name,
optionally written as `profile:name`). Omission unambiguously selects the configured role/harness
default. Credentials and TLS policy are resolved outside the tier entry; embedded URL credentials
are rejected. `policy` defaults to `loose`. At dispatch time the resolver intersects these ranges
with the bead's required complexity, optional canonical `model:` preference, role/harness, and
availability evidence. `loose` may choose the nearest available range with a warning; `strict`
blocks an unavailable or out-of-range preference and any group preference conflict.

`bh work schedule --json` and `beadhive://work/schedule/{epic}` expose a complete decision on
every group, singleton, and nested coordinator: `complexity`, `preferred_model`,
`selected_model`, `selection_reason`, `policy`, `availability_source`, and `warnings`. The old
`model` field remains temporarily as a deprecated alias of `selected_model` (and is `null` when
selection is blocked); consumers should migrate to `selected_model`.

## `work.dispatch` — collapsed dispatch

`work.dispatch.*` tunes how the root dispatcher dispatches a ready epic's beads: the default
**fanout** (one bead → one developer sub-agent → one worktree, parallel wall-time) or a
**collapsed** run (every ready bead worked sequentially by ONE collapsed `dispatcher @ batch` seat
in one shared `wt/batch/<epic>` worktree, merged once). Each key resolves per-hive
`entry.work.dispatch.<key>` > global `work.dispatch.<key>` > default (the `config.dispatch_*`
accessors in `src/beadhive/config.py`). Every value is **advisory** — dispatch config decides
grouping and seat only; it never claims or merges anything.

| Key | Default | Values | Effect |
|---|---|---|---|
| `work.dispatch.mode` | `fanout` | `fanout` \| `collapsed` \| `auto` | How to dispatch a ready epic; unknown values fall back to `fanout`. |
| `work.dispatch.max_depth` | `2` | `0` \| `1` \| `2` | How deep sub-agent dispatch may nest; out-of-range clamps to `2`. |
| `work.dispatch.max_beads_per_session` | `8` | int | Cap on beads a single collapsed session holds before it splits into chunked sessions. |
| `work.dispatch.auto_budget` | `8` | int | `size:`-weighted budget `auto` mode may absorb before it prefers fanout. |
| `work.dispatch.review_mode` | `self` | `self` \| `fresh` | Who resolves a dispatched bead's review gate (see below). |
| `work.dispatch.poll_interval` | `5.0` | float (s) | `local` tier: seconds between poll passes. Gate latency is bounded by this. |
| `work.dispatch.max_concurrency` | `2` | int ≥ 1 | `local` tier: seat processes in flight at once. In-process; resets on restart. Below 1 **clamps to 1** — there is no "unlimited" spelling. |
| `work.dispatch.max_run_seconds` | `1800.0` | float (s), `0` = off | `local` tier: per-run wall-time cap; an over-running seat is cancelled through the CANCEL ladder. `0` is the one "off" sentinel in this section. |
| `work.dispatch.terminate_grace` | `5.0` | float (s) | `local` tier: gap between the reaper's group SIGTERM and its group SIGKILL. |
| `work.dispatch.envelope_grace` | `3.0` | float (s) | `local` tier: how long the loop holds the child's stdout pipe waiting for the priced envelope **before** reaping. |
| `work.dispatch.seat_command` | `bh-{role}` | string | `local` tier: the seat binary template (shell-split, `{role}` substituted). |
| `work.dispatch.seat_bundle` | the bundle bh ships | path, or `-` | `local` tier: the seat's tool roster + permission mode, passed as `--bundle`. Unset resolves to `assets/seat-bundle.json`; `-` passes none, which leaves the seat **default-closed** (`plan` posture, every Bash call refused) and unable to complete a write action. A `--bundle` already present in `seat_command` wins. |

**The `local` runtime keys** (`bh work loop`, bh-c6dk.5) sit here rather than in a parallel
section because they are dispatch policy. All of them are **in-process and volatile by design**:
they describe *this loop process's* own children, so a restart resets them
([loop-ownership-and-execution-memory-adr.md](design/loop-ownership-and-execution-memory-adr.md)
Decision 2). A rolling token budget is deliberately NOT among them — it cannot live in beads and
v1 does not build it (deferred to `bh-3yoh`).

- **`mode`** — `collapsed` always collapses a ready epic into one collapsed `dispatcher @ batch` `Task`;
  `fanout` (the default) leaves the per-bead / per-group developer fan-out **unchanged**;
  `auto` decides per epic via `schedule.auto_should_collapse`. **Note:** `collapsed` mode
  requires the epic to be fully un-batched (no existing `batch:` labels on any child). A
  partially planner-batched epic will fail loudly during claim with "members span multiple
  batch groups" rather than silently mixing batch groups.
- **`max_depth`** — picks the collapsed seat and whether it has an escape valve: `0` (current
  session does the work, no `Task` — only coherent for a human on the developer seat), `1`
  (collapsed `dispatcher @ batch`, no `Task`, hard ceiling), `2` (adds `sub-dispatch:1`, the
  single-bead escape valve). See [AGF.md — Delegation depth spectrum](AGF.md#delegation-depth-spectrum--how-far-dispatch-nests).
- **`auto_budget`** — `auto` mode sums each candidate bead's `size:<xs..xl>` ordinal weight
  (`xs=1`, `s=2`, `m=3`, `l=4`, `xl=5`; an unlabeled or unrecognized size counts as `m`) and
  collapses the epic only when that total stays within budget **and** the set is single model
  tier / single review gate. Over budget or mixed ⇒ fanout.

### Planner hints vs. operator override — precedence

The planner authors **advisory** labels on beads (`size:`, `batch:`, `model:`, `gate:`). `model:`
is an optional open `<provider>/<model-name>` preference; the authoritative portable capability
route is the single closed `complexity:SIMPLE|MEDIUM|COMPLEX|REASONING` label compiled onto each
work bead. These
are consulted **only by `auto`** — as the cost signal (`size:` weights vs. `auto_budget`) and
the single-model-preference / single-gate guards. They are estimates, never a command.

An explicit operator `work.dispatch.mode` of `fanout` or `collapsed` **always wins**, regardless
of what the planner estimated:

- `mode: collapsed` collapses the epic even if the planner's `size:` weights would blow past
  `auto_budget` — the operator is vouching for cohesion in place of the algorithm
  (`plan_schedule(..., force_single_group=True)` bypasses the cohesion/size/model/gate guards).
- `mode: fanout` (the default) fans out even where `auto` would have collapsed — the planner's
  hints don't force a collapse the operator didn't ask for.

Only when `mode: auto` is set do the planner's hints actually steer the collapse decision.

### `review_mode` — who resolves the review gate

`work.dispatch.review_mode` (accessor `config.dispatch_review_mode`, default **`self`**) decides
who resolves a collapsed bead's review gate. Two modes ship:

- **`self`** (default) — the collapsed `dispatcher @ batch` seat is its own review authority and self-resolves
  each bead's gate in the same collapsed session (no second `Task`). This is legitimate because
  the collapsed session runs under a live human watching it. **Note (bh-e5kv):** `bh work approve`'s
  reviewer cross-seat policy (`work.dispatch.reviewer_cross_seat`) now defaults to `hard`, which
  BLOCKS that same-seat self-resolve unless a human distinct from the bead's author runs `approve`,
  or the policy is explicitly set to `advise` for a rig that knowingly relies on the live-human-
  watching assumption above — see [roles-rbac-matrix.md §3](design/roles-rbac-matrix.md#3-seat-vs-session-rbac-nuance).
- **`fresh`** — a separate reviewer `Task` with independent, fresh context resolves each bead's
  gate. Spawning that `Task` requires **depth 2** (`sub-dispatch:1`); depth 1 holds no
  `Task`, so a depth-1 + `fresh` pairing is a dispatcher misconfiguration to surface, not
  silently self-review.

**`paired` is deliberately NOT implemented.** It was scoped as a third mode (two seats sign off,
via a resumable reviewer session), but the fekf.10 spike
([docs/spikes/fekf-10-resumable-agent.md](spikes/fekf-10-resumable-agent.md)) concluded **NO-GO**
— no resumable-sub-agent mechanism is wired for Beadflow seats — and the implementation bead was
closed as not-planned. Selecting `review_mode: paired` does **not** silently no-op:
`config.dispatch_review_mode` normalizes it to `fresh` and emits a `review_mode_paired_fallback`
warning through the log pipeline, so the bead still gets an independent reviewer instead of an
unreviewed gate. Do not rely on `paired` as a working mode.

```sh
bh config set work.dispatch.mode collapsed        # force-collapse ready epics
bh config set work.dispatch.max_depth 1           # collapsed seat with no escape valve
bh config set work.dispatch.auto_budget 12        # let auto absorb a bigger epic
bh config set work.dispatch.review_mode fresh     # independent reviewer per bead (depth 2)
```

The dispatcher seat that reads these keys is documented in
[skills/dispatcher/SKILL.md](../skills/dispatcher/SKILL.md); the collapsed variants it dispatches
are `dispatcher @ batch` (depth 1) and `dispatcher @ batch` + `sub-dispatch:1` (depth 2).
