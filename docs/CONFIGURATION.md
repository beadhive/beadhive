# Configuration

Everything `ws` owns on a machine lives under **`~/.ws/`** (module: `config.py`).

## Locations & env vars

| Thing | Default | Override | Notes |
|---|---|---|---|
| home | `~/.ws/` | `WS_HOME` | base for everything below |
| config | `~/.ws/config.yaml` | `WS_CONFIG` | the registry (this file) |
| hub | `~/.ws/hub/` | `WS_HUB` | aggregated cross-rig beads DB ([HUB](HUB.md)) |
| cache | `~/.ws/cache/` | `WS_CACHE` | minimal-clone caches for uncloned rigs |
| generated docs | `~/.ws/labels.md` | — | `ws labels docs` output |
| dolt env | `~/.ws/.env` | — | [DOLT](DOLT.md) server secrets |
| dolt compose | `~/.ws/docker-compose.yml` | — | [DOLT](DOLT.md) |

`GIT_WORKSPACE` (defaults to `~/workspace`) is **git-workspace's** variable, shared — it's
the root `ws` derives `<provider>/<org>/<repo>` identity from. It is not `ws`-owned.

## Scaffolding

```sh
ws config init          # write config.yaml, docker-compose.yml, .env.example into ~/.ws
ws config init --force  # overwrite existing
ws config path          # print the resolved config path
```

Templates ship inside the package (`src/ws/templates/`).

## `config.yaml` schema

```yaml
delimiter: ":"                       # label delimiter (provider:github, …)

# Recognized provider labels (git hosts). A plain list — no codes.
# May be omitted entirely when the git-workspace integration is enabled (loaded from there).
providers: [github, gitlab, gitea]

# org (full name) -> {code, policy}.
#   code:   used in prefixes (ag-infra). If an org is absent, code falls back to
#           sanitize(name)[:2] and policy to personal — so most orgs need no entry.
#   policy: required = org-native repos MUST use "<code>-<repo>" (enforced at rig init)
#           personal = code is only a suggestion
orgs:
  agentguides: {code: ag, policy: required}

# Repos ws ignores entirely (labels sync skips, rig init refuses, doctor de-noises).
exclude:
  orgs: [SimplicityGuy, bcripe-xealth]
  repos: []                          # "provider/org/repo"

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
  # rig_match: flexible                 # how `ws -r <id> …` resolves: flexible | prefix | triplet

# Optional local Dolt server (see DOLT.md).
dolt:
  backend: docker                      # colima | docker | podman | none

# Soft-archive graveyard settings (ws rig retire destination).
archive:
  dir: ~/workspace/.archived           # default: $GIT_WORKSPACE/.archived
  window_days: 30                      # default age threshold for `ws rig archive prune`

# One entry per managed rig — maintained by `ws rig init` (add) + `ws labels sync`.
#   kind: org-native | personal | prototype | fork ; forks add upstream: "owner/name"
managed_repos:
  - {"provider": "github", "org": "agentguides", "repo": "infra", "prefix": "ag-infra", "kind": "org-native"}
```

### Notes on the file

- It's the **registry** — the single source of truth ([LABELS](LABELS.md), [RIGS](RIGS.md)).
- `ws` round-trips it with `ruamel.yaml`, preserving comments and the one-flow-mapping-per-line
  style of `managed_repos`, so `ws rig init` / `ws labels sync` edits produce minimal diffs.
- There is **no `enforcement:` block** — enforcement is fixed behavior, not config
  ([LABELS](LABELS.md#enforcement)).
- Provider entries carry **no codes** (only org codes go in prefixes).

## `ws config` commands

| Command | Effect |
|---|---|
| `ws config init [--force]` | scaffold `~/.ws` from bundled templates |
| `ws config path` | print the resolved `config.yaml` path |
| `ws config show` | pretty-print the resolved config (doctor overview + extras) |
| `ws config get <key>` | read a dotted config key |
| `ws config set <key> <value> [--json]` | set a dotted config key (bool/int coercion) |
| `ws config unset <key>` | delete a dotted config key |

### `ws config get`

Reads a single dotted-path key from the resolved config. Booleans print as `true` or
`false`; scalars print verbatim; lists and maps print as compact JSON so the value round-trips
back through `ws config set --json`. Exits 1 (with a message on stderr) when the key is not
set.

```sh
ws config get otel.enabled        # → true
ws config get otel.protocol       # → grpc
ws config get dimensions          # → {"component": {...}, "size": {...}}
```

### `ws config set`

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
ws config set otel.enabled true
ws config set otel.endpoint http://localhost:4317
ws config set otel.protocol http/protobuf        # validated
ws config set work.max_commits 8
ws config set my.list '[1,2,3]' --json           # list via JSON
ws config set my.map '{"a":1}' --json            # map via JSON
```

### `ws config unset`

Deletes a dotted-path key from the config and persists. Exits 1 when the key is not set.
Useful for removing optional sections (`otel`, `dolt`, etc.) without hand-editing the file.

```sh
ws config unset otel.endpoint
ws config unset dolt              # removes the whole dolt section
```

The control-plane role that drives these verbs (alongside `ws rig`) is documented in
[CONTROL-PLANE.md](CONTROL-PLANE.md).

## Archive section

The `archive` section controls where `ws rig retire` moves retired clones and when
`ws rig archive prune` considers them eligible for permanent deletion.

| Key | Default | Effect |
|---|---|---|
| `archive.dir` | `$GIT_WORKSPACE/.archived` | Root directory for soft-archived clones |
| `archive.window_days` | `30` | Default `--older-than` age threshold for `archive prune` |

```sh
ws config set archive.dir /mnt/cold/ws-archive   # relocate the graveyard
ws config set archive.window_days 60              # keep archives for 60 days before pruning
ws config get archive.window_days                 # read back → 60
```

Both keys are optional. When `archive.dir` is unset, clones are archived under
`$GIT_WORKSPACE/.archived`. When `archive.window_days` is unset, `archive prune` defaults
to a 30-day window. See [RIGS.md — ws rig archive](RIGS.md#ws-rig-archive) for the full
reclaim workflow.

## `work.dispatch` — collapsed dispatch

`work.dispatch.*` tunes how the root coordinator dispatches a ready epic's beads: the default
**fanout** (one bead → one developer sub-agent → one worktree, parallel wall-time) or a
**collapsed** run (every ready bead worked sequentially by ONE epic-coordinator seat in one
shared `wt/batch/<epic>` worktree, merged once). Each key resolves per-rig
`entry.work.dispatch.<key>` > global `work.dispatch.<key>` > default (the `config.dispatch_*`
accessors in `src/ws/config.py`). Every value is **advisory** — dispatch config decides
grouping and seat only; it never claims or merges anything.

| Key | Default | Values | Effect |
|---|---|---|---|
| `work.dispatch.mode` | `fanout` | `fanout` \| `collapsed` \| `auto` | How to dispatch a ready epic; unknown values fall back to `fanout`. |
| `work.dispatch.max_depth` | `2` | `0` \| `1` \| `2` | How deep sub-agent dispatch may nest; out-of-range clamps to `2`. |
| `work.dispatch.max_beads_per_session` | `8` | int | Cap on beads a single collapsed session holds before it splits into chunked sessions. |
| `work.dispatch.auto_budget` | `8` | int | `size:`-weighted budget `auto` mode may absorb before it prefers fanout. |
| `work.dispatch.review_mode` | `self` | `self` \| `fresh` | Who resolves a dispatched bead's review gate (see below). |

- **`mode`** — `collapsed` always collapses a ready epic into one epic-coordinator `Task`;
  `fanout` (the default) leaves the per-bead / per-group developer fan-out **unchanged**;
  `auto` decides per epic via `schedule.auto_should_collapse`.
- **`max_depth`** — picks the collapsed seat and whether it has an escape valve: `0` (current
  session does the work, no `Task` — only coherent for a human on the developer seat), `1`
  (`epic-coordinator`, no `Task`, hard ceiling), `2` (`epic-coordinator-deep`, adds the
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

- **`self`** (default) — the epic-coordinator seat is its own review authority and self-resolves
  each bead's gate in the same collapsed session (no second `Task`). This is legitimate because
  the collapsed session runs under a live human watching it.
- **`fresh`** — a separate reviewer `Task` with independent, fresh context resolves each bead's
  gate. Spawning that `Task` requires **depth 2** (`epic-coordinator-deep`); depth 1 holds no
  `Task`, so a depth-1 + `fresh` pairing is a coordinator misconfiguration to surface, not
  silently self-review.

**`paired` is deliberately NOT implemented.** It was scoped as a third mode (two seats sign off,
via a resumable reviewer session), but the fekf.10 spike
([docs/spikes/fekf-10-resumable-agent.md](spikes/fekf-10-resumable-agent.md)) concluded **NO-GO**
— no resumable-sub-agent mechanism is wired for AGF seats — and the implementation bead was
closed as not-planned. Selecting `review_mode: paired` does **not** silently no-op:
`config.dispatch_review_mode` normalizes it to `fresh` and emits a `review_mode_paired_fallback`
warning through the log pipeline, so the bead still gets an independent reviewer instead of an
unreviewed gate. Do not rely on `paired` as a working mode.

```sh
ws config set work.dispatch.mode collapsed        # force-collapse ready epics
ws config set work.dispatch.max_depth 1           # collapsed seat with no escape valve
ws config set work.dispatch.auto_budget 12        # let auto absorb a bigger epic
ws config set work.dispatch.review_mode fresh     # independent reviewer per bead (depth 2)
```

The coordinator seat that reads these keys is documented in
[skills/coordinator/SKILL.md](../skills/coordinator/SKILL.md); the collapsed seats it dispatches
are `epic-coordinator` / `epic-coordinator-deep`.
