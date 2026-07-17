# Configuration

Everything `bh` owns on a machine lives under **`~/.ws/`** (module: `config.py`).

## Locations & env vars

| Thing | Default | Override | Notes |
|---|---|---|---|
| home | `~/.ws/` | `WS_HOME` | base for everything below |
| config | `~/.ws/config.yaml` | `WS_CONFIG` | the registry (this file) |
| hub | `~/.ws/hub/` | `WS_HUB` | cross-hive aggregation hub (built by `bh sync`) — [HUB](HUB.md) |
| cache | `~/.ws/cache/` | `WS_CACHE` | minimal-clone caches for uncloned hives |
| generated docs | `~/.ws/labels.md` | — | `bh label docs` output |
| dolt env | `~/.ws/.env` | — | [DOLT](DOLT.md) server secrets |
| dolt compose | `~/.ws/docker-compose.yml` | — | [DOLT](DOLT.md) |

`GIT_WORKSPACE` (defaults to `~/workspace`) is **git-workspace's** variable, shared — it's
the root directory (canonical HQ launch directory) from which `bh` derives `<group>/<account>/<repo>`
identity for all cloned hives during initial setup and beyond (the first segment is the repo-group
**path**, not necessarily the provider type — see [INTEGRATIONS.md](INTEGRATIONS.md#git-workspace)).
The integration-plane (and setup skill) set this variable to `~/workspace` if unset. It is not
`bh`-owned; it belongs to git-workspace.

## Scaffolding

```sh
bh config init          # write config.yaml, docker-compose.yml, .env.example into ~/.ws
bh config init --force  # overwrite existing
bh config path          # print the resolved config path
```

Templates ship inside the package (`src/beadhive/templates/`).

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

# Optional git-workspace integration (see INTEGRATIONS.md).
git_workspace:
  enabled: true
  # path: ~/workspace/workspace.toml   # default: glob $GIT_WORKSPACE/workspace*.toml
  # hive_match: flexible                # how `bh -r <id> …` resolves: flexible | prefix | triplet

# Optional orca integration — registers git-workspace clones with orca (see INTEGRATIONS.md).
# Gated on git_workspace.enabled; disabled unless the flag below is set (default false).
orca:
  enabled: false
  # data_path: ~/.config/orca/orca-data.json   # default: platform-aware (see INTEGRATIONS.md)
  # worktrees: false                           # opt in to orca-delegated worktree create/remove
  # worktrees:
  #   enabled: false
  #   fallback: false   # true = degrade to native git on delegation failure (default: hard fail)

# Optional local Dolt server (see DOLT.md).
dolt:
  backend: docker                      # colima | docker | podman | none

# Soft-archive graveyard settings (bh hive retire destination).
archive:
  dir: ~/workspace/.archived           # default: $GIT_WORKSPACE/.archived
  window_days: 30                      # default age threshold for `bh hive archive prune`

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
| `bh config init [--force]` | scaffold `~/.ws` from bundled templates |
| `bh config path` | print the resolved `config.yaml` path |
| `bh config show` | pretty-print the resolved config (doctor overview + extras) |
| `bh config get <key>` | read a dotted config key |
| `bh config set <key> <value> [--json]` | set a dotted config key (bool/int coercion) |
| `bh config unset <key>` | delete a dotted config key |

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
write on mismatch). Any `*.enabled` key must receive a boolean (error otherwise). Unknown
config sections produce a warning but the write proceeds.

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
# ~/.ws/config.yaml
claude:
  source: plugin        # install the agf plugin at onboard time
  plugin: agf
  marketplace: .        # '.' = the workspace repo root (resolved at install time)
  scope: user           # user-scope persists across all hives
```

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

The planner authors **advisory** labels on beads (`size:`, `batch:`, `model:`, `gate:`). These
are consulted **only by `auto`** — as the cost signal (`size:` weights vs. `auto_budget`) and
the single-tier / single-gate guards. They are estimates, never a command.

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
  the collapsed session runs under a live human watching it.
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
