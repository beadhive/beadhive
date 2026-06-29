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
