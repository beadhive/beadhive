# Configuration

Everything `bh` owns on a machine lives under **`~/.beadhive/`** (module: `config.py`).

## Locations & env vars

| Thing | Default | Override | Notes |
|---|---|---|---|
| home | `~/.beadhive/` | `BH_HOME` (legacy alias `WS_HOME`) | base for everything below |
| config | `~/.beadhive/config.yaml` | `BH_CONFIG` (legacy alias `WS_CONFIG`) | host-local config (this file) |
| Git-mode fleet config | `~/.beadhive/hq/fleet.yaml` | via `BH_HQ` (legacy alias `WS_HQ`) | Git compatibility source; SQL mode reads the selected committed central snapshot |
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

`config.load()` resolves one effective config from the selected fleet source and the
host-local `config.yaml`. In Git mode, the fleet source is `fleet.yaml` in the HQ store;
in `dolt-server` mode it is the committed shared SQL snapshot. The host file is deep-merged
over fleet data. Nested sections merge key-by-key, so a host setting `worktrees.path` keeps
the fleet's `worktrees.ephemeral`; scalars and lists are replaced wholesale.

Which keys belong to which side is **data**, not branching: `config_partition.py` owns the
fleet/host split (`FLEET_PREFIXES` / `HOST_PREFIXES`, longest match wins) plus
`FLEET_HOST_OVERRIDE_ALLOWLIST` — the explicit narrow list of fleet keys a host may still
override. It currently contains only `worktrees.ephemeral`, allowing a machine to retain
persistent worktrees when the fleet default is ephemeral.

| Situation | Behavior |
|---|---|
| fleet source and host file present | merged; host wins only on host keys + allowlisted fleet keys |
| host sets a non-allowlisted **fleet** key | `ConfigError` naming every offending key — never silently ignored, never silently applied |
| no fleet source (not attached to Git HQ or SQL) | host-only config, unchanged; Git mode warns when an HQ store exists but has no `fleet.yaml` |
| no `config.yaml` | fleet-only config |
| neither file | `FileNotFoundError` pointing at `bh config init` |

`load()` is the **read** path. `load_host()` is the **write** path: every read-modify-write
(`bh config set/unset`, the hive registry, `bh hive enable/disable`) loads through it, so
`save()` can never bake fleet-wide truth into a host's own file.

`managed_repos` is fleet-scoped. The hive registry (`bh hive init`/`add`/`rm`) writes it through
the selected backend, not this host's config. In Git mode, the working copy remains a Git
publication responsibility; in SQL mode, the selected committed SQL store is authoritative.
See [HQ](HQ.md#fleet-writes-after-init) for Git behavior and the
[migration runbook](design/dolt-hq-config-migration-runbook.md) for a guarded mode transition.

## HQ configuration authority {#hq-configuration-authority}

The `hq` section configures the fleet **configuration** backend and its separate runtime
authority bindings. It is distinct from the local HQ Beads database, the local Dolt engine
(`dolt.backend`), and `beads.engine`. A host with `hq.mode: dolt-server` needs explicit
non-secret endpoint, TLS and broker-reference metadata. Credentials are resolved from the
configured secret broker and never belong in this file. See the
[frame fleet membership guide](FRAME-FLEET-MEMBERSHIP.md) for a redacted example and readiness
boundaries.

| Config key | Purpose |
|---|---|
| `hq.mode` | `git` compatibility mode or `dolt-server`; selects fleet config only. |
| `hq.remote` | Git HQ repository remote. It is not the SQL server address. |
| `hq.beadyard_id` | HOST-local canonical HQ UUID pin; normal reads never create it. |
| `hq.authority_anchor` | HOST-local path to the protected operator authority-anchor file. |
| `hq.admission_policy` | Currently `manual`; frame admission remains an explicit protected action. |
| `hq.sql.enabled` | Selects SQL only when true and the reader/trust binding validates. |
| `hq.sql.reader` | Required SQL configuration reader connection. |
| `hq.sql.publisher` | Optional separately credentialed fleet-config publisher connection. |
| `hq.sql.runtime` | Optional frame runtime connection; cannot coexist in one HOST binding with `observer` or `authority_writer`. |
| `hq.sql.observer` | Optional protected observer/read connection; mutually exclusive with a runtime binding on that HOST. |
| `hq.sql.authority_writer` | Optional protected authority publication connection; mutually exclusive with a runtime binding on that HOST. |
| `hq.sql.runtime_backend_identity`, `runtime_generation`, `runtime_initial_revision` | Separate runtime-authority backend pins. |
| `hq.sql.runtime_floor_path`, `runtime_operator_public_key` | Host-local runtime replay-floor path and operator public-key reference used to verify signed runtime authority. |
| `hq.sql.floor_path`, `backend_identity`, `generation`, `minimum_sequence`, `initial_revision` | Reader-side trust floor, backend/generation and initial revision pin. |
| `hq.sql.cache_ttl` | Bounded config cache age; it never waives revision or revocation checks. |
| `hq.sql.liveness` | `receiver` (default, unchanged behavior) or `signed`. `signed` reads this frame's newest verified signed heartbeat from its own inbox at read time, treats hive-lease expiry as an advisory hint, and skips receiver renewal on write verbs. Tombstones, foreign holders, epoch fences and admission are unchanged; adopt, release and failover still need a receiver. |
| `BH_HQ_SQL_LIVENESS` (env) | Process-level override for `hq.sql.liveness`: `receiver` or `signed`. When set it wins over the config key, so a newer bh can run signed without adding the key to a HOST file an older bh (strict schema) also reads. Any other value is an error, not a fallback. |
| `BH_HQ_AUTHORITY_ENFORCE` (env) | **UNSUPPORTED, dev/prototype only.** `true` (default) or `false`. `false` makes this host's `bh` processes skip HQ runtime-authority enforcement (expiry, config binding, cordon/admission/release/caps predicates), so the frame is self-asserted. Per host and per process environment; no fleet publish or `host.yaml` key. The operator must unset it on every executor before the three new executors are admitted. Any other value is an error. See [HQ — disabling authority enforcement](HQ.md#unsupported-disabling-hq-authority-enforcement). |
| `BH_FRAME_HEARTBEAT` (env) | **Transitional** (retired with the heartbeat/receiver model, bh-qlgmm). `required` (default; unset or empty also means required) or `advisory`; any other value is an error. `advisory` keeps publishing and verifying heartbeats but stops a stale or missing beat from failing the `authenticated_fresh_heartbeat` eligibility predicate (a `waived` warning is printed to stderr); every other predicate, including the lease-derived ones, still fails closed. It is a **single-executor-frame escape only**: it must be cleared on every frame before any additional executor frame is admitted. Per process environment; no `host.yaml` key or fleet publish, so older readers sharing the HOST file are unaffected. Retire it in gated steps: (A1) the frame's eligibility reads session rows, or the decoupled heartbeat sender has run with no lapse for one full-gate soak window; (A2) unset `BH_FRAME_HEARTBEAT` in every unit, shim and profile on the frame and restart them; (A3) confirm `bh host eligible --hive <prefix> --json` stays eligible through a full gate with no `waived` warnings in the journal; (A4) only then remove the code. Never delete the code first. Rollback before A4: set `advisory` again (single-frame only). See [rotation window](design/frame-release-upgrade-runbook.md) for what it does not cover. |
| `hq.sql.<role>.*` | Per role (`reader`, `publisher`, `runtime`, `observer`, `authority_writer`): `host`, `port`, `database`, `user`, `tls_mode`, `server_name`, `ca_file`, `credential.config_path/profile/key`, and `connect_timeout`, `read_timeout`, `write_timeout`, `operation_timeout`. |
| `BH_HQ_OPERATOR_SETTINGS` (env) | Path to a JSON/YAML file with an `hq.sql` section (`reader`, `authority_writer`, `runtime: null`, pins, optional `authority_max_duration_s`, optional `inbox_retention_s`) that supplies the operator binding for `bh hq authority renew\|grant\|observe\|bind-beadyard\|status\|check\|prune-inbox` and `bh host release-upgrade plan\|apply\|check` instead of this host's `host.yaml`. Refused when it binds `runtime` or lacks `authority_writer`. Shape and laptop renew procedure: [HQ](HQ.md#authority-laptop-renew). |
| `BH_HQ_INBOX_RETENTION` (env) | Retention margin for `bh hq authority prune-inbox` (duration such as `1h`, `2d`; default `1h`). A signed-mode heartbeat row is pruned only once its signed `renewTime` + lease + 30 s skew + this margin has passed, and the newest verified beat is always kept. `hq.sql.inbox_retention_s` in the `BH_HQ_OPERATOR_SETTINGS` file wins over it. Operator-side only; nothing prunes unless the operator runs the verb. See [HQ](HQ.md#signed-mode-inbox-retention). |
| `BH_HQ_AUTHORITY_MIN_REMAINING` (env) | Floor for `bh hq authority check` (duration, default `0`: fail only when expired or config-unbound). |
| `BH_HQ_AUTHORITY_WARN_WITHIN` (env) | Lead time for authority-expiry warnings (duration such as `6h`, `2d`; default `24h`). Environment-only by design: a fleet key would move the HQ head and a host key would break older readers. See [HQ](HQ.md#authority-expiry). |
| `managed_repos[].frame_policy.config_revision` | Required policy revision label for a frame-managed hive. |
| `managed_repos[].frame_policy.requires` | Optional requirements: `isolation`, `trust_zone`, `arch`, `harness`, `harnesses`, and positive `max_sessions`. |
| `managed_repos[].frame_policy.evict_after_s` | Required positive finite interval used by protected takeover policy. |

Each HOST-local `hq.sql.<role>.tls_mode` independently accepts `required` (the default)
or `disabled`. `required` verifies the configured CA and matching `server_name`, requires
TLS 1.2 or newer and native MySQL `CLIENT_SSL` advertisement before authentication, and
never falls back to plaintext after a TLS failure. Existing bindings retain that behavior
when the setting is omitted.

`disabled` is an explicit operator choice for a plaintext MySQL listener, including a raw
Caddy layer4 TCP proxy. It does not require `ca_file` or `server_name`. Credentials and SQL
traffic can be exposed on the network; the proxy alone provides no TLS protection. Keep
this choice in HOST configuration and select it separately for each intended principal.
Endpoint validation, fnox broker retrieval, driver pinning, deadlines, redacted errors,
reconnect refusal, and signed runtime authority/admission checks still apply. Runtime
cross-database transactions require matching endpoint and transport policy for their
config and runtime bindings so a plaintext runtime connection cannot bypass a required
config-reader TLS policy. See the [migration examples](design/dolt-hq-config-migration-runbook.md#host-local-sql-transport).

The SQL credential broker requires **fnox 1.36.0**. It searches the fixed administrator
directories `/run/current-system/sw/bin`, `/usr/local/bin`, and `/usr/bin`, plus
`/opt/homebrew/bin` on macOS. On macOS it also supports `mise install fnox@1.36.0` in
the [default mise installation directory](https://mise.jdx.dev/directories.html):
`~/.local/share/mise/installs/fnox/1.36.0/fnox`. This removes the need for a manual
`/usr/local/bin/fnox` symlink. The account database determines the home directory;
`HOME`, `PATH`, mise/XDG directory overrides, repository configuration, and mise shims
do not select the broker. The account home and every installation path component must
be owned by the account or root, have no group/other write permission, and contain no
symlinks. The binary must be a regular executable file. Custom mise layouts require
an administrator installation in one of the fixed directories. Version checking and
credential retrieval retain the shared deadline, noninteractive invocation, bounded
output, and redacted failures.

`bh config schema --json` lists the public config rows. The current authoritative generated
package schema is
[`config-v1.schema.json`](../src/beadhive/schemas/contracts/v2.0.0/artifacts/config-v1.schema.json).
Settings `schema_version: 1`, SQL storage metadata v3, signed Git carrier v2, frame heartbeat
domain/API versions and the versioned `docs/schemas/wire` snapshots are separate contracts;
an added config key does not itself require a wire-schema bump.

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
# FLEET-scoped. Per-host variation comes from that host's `role` in hosts/<host_id>.yaml, which
# SCALES the ttl below — never a per-host override of these keys. Since 0.23.0 (bh-12hev) the
# lease's expiry is a failover HINT only: it never stops the holder writing, and write verbs no
# longer renew it (see docs/design/hive-writer-partitioning-adr.md §4).
host:
  lease:
    renew_interval: 300                # `bh host list` shows "expiring" within this of the hint
    ttl: 1800                          # seconds until the expiry hint; past it a lease is takeable

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
| `bh config set <key> <value> [--json] [--scope fleet\|host]` | set a dotted config key (bool/int coercion); `hives.<id>.<key>` writes one managed hive's fleet-owned override |
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
bh config set hives.bh.work.validation_bypass true --scope fleet  # emergency: this hive only
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
- `worktrees.ephemeral` is the sole allowlisted exception, so a host can opt into persistent
  worktrees with `bh config set worktrees.ephemeral false --scope host` without changing the
  fleet policy. This is one-way: a host cannot set it to `true` when the fleet requires
  persistent worktrees.

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

## Disk-pressure alerts

`alerts.disk_free_floor_mb` is the free-space floor for the filesystem mounted at `/` (default
10,240 MB). `alerts.worktree_filesystem_free_floor_mb` independently sets the floor for the
filesystem containing `worktrees.path` or the ephemeral worktree root (also default 10,240 MB).
Set either value to `0` to disable that alert. When the worktree-specific key is absent, it
inherits `disk_free_floor_mb`, so existing configs keep their previous configured threshold
while the alert now identifies the constrained filesystem correctly. `alerts.worktree_cap_mb`
(default 5,120 MB) continues to limit each hive's total managed-worktree footprint.

Add the worktree-specific value to a config when the two filesystems need different floors:

```yaml
alerts:
  disk_free_floor_mb: 10240
  worktree_filesystem_free_floor_mb: 4096
```

No rewrite is required for existing `disk_free_floor_mb` settings. The doctor payload retains
`worktree_disk_usage.disk_free_bytes` as a compatibility alias for the worktree-root reading;
new consumers should read `worktree_filesystem.free_bytes` and
`host_root_filesystem.free_bytes`.

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
bh backup reclaim --root cache --dry-run   # split superseded caches from only-copy caches
bh backup reclaim --root cache --confirm   # remove only caches superseded by local .beads
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
`selected_model`, `selection_reason`, `policy`, `availability_source`, and `warnings`. The
pre-existing `model` field remains temporarily as a deprecated alias of `selected_model` (and is
`null` when selection is blocked); consumers should migrate to `selected_model`.

See [Complexity-first routing](COMPLEXITY-ROUTING.md) for availability limitations, strict/loose
decision behavior, migration recovery, and the complete public decision schema.

## `work.beads` — Beads route {#work-beads}

`work.beads.route` decides whether the cohorts cut over to `beadhive-core` (assign, claim,
resume, abandon, submit's bead-state half, next, ready --json, schedule, `bh plan file`, and the
local loop's reads) may take their `bd` CLI route when no capable Beads service session opens.
Resolves per-hive `entry.work.beads.route` > global `work.beads.route` > `api`.

| Key | Default | Values | Effect |
|---|---|---|---|
| `work.beads.route` | `api` | `api` \| `api+cli-fallback` \| `cli` | `api`: fail closed, naming `bh host beads start --hive <hive>` and this key. `api+cli-fallback`: select the `bd` route before execution when no capable session opens. `cli`: always the `bd` route. An unknown value is refused (exit 2), never defaulted. |

See [BEADS-SERVICE.md](BEADS-SERVICE.md) for the full behavior table.
`bh work approve` / `bounce` are outside this key and always fail closed without a service.

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

The dispatcher seat reads these keys; the collapsed variants it dispatches are
`dispatcher @ batch` (depth 1) and `dispatcher @ batch` + `sub-dispatch:1` (depth 2).
