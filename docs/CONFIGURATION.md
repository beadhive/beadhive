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

### bd passthrough timeouts for long store transfers

`bh bd` runs ordinary bd commands under a 120s bound. Long store transfers (`dolt push`,
`dolt pull`, `dolt clone`, `bootstrap`, `backup sync`, `backup restore`) instead get the fsck
allowance plus a 60s transfer margin, never less than 120s:

| Variable | Default | Effect |
|---|---|---|
| `BEADS_FSCK_TIMEOUT` | unset | When set (`30m`, `1h30m`, `900s` or bare seconds), used as the fsck part, uncapped; total is this plus 60s. Unparseable values are ignored. |
| `BH_STATE_TIMEOUT_CAP` | `900` (seconds) | Cap on the store-sized fsck allowance (300s plus 60s per GiB of store) used when `BEADS_FSCK_TIMEOUT` is unset. Same duration syntax. |

When the bound fires the command exits 124 and the message names the timeout, its source and
these variables; for `dolt push` it also points at `bh hive sync remotes <hive> --push`.

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

### Validation broker layering {#validation-broker-layering}

`work.validation_broker` is resolved **per hive** (bh-m2a8r): the hive's
`managed_repos[*].work.validation_broker` overlay is read on top of the merged host/fleet
`work.validation_broker`. Ownership is split by what each field means:

| Field | Owner | Resolution |
|---|---|---|
| `engine`, `setup` | hive | hive over host/fleet; `BH_VALIDATION_BROKER_ENGINE` still wins for one invocation |
| `packer` | hive | hive over host/fleet, field by field |
| per-key reservations (`work.attest.keys[].resources`) | hive | hive over host/fleet (unchanged) |
| `budget.cpu`, `budget.memory`, `budget.pools` | host, hive may ask for less | hive over host/fleet, then **capped by the host's configured budget**: a nonzero host `cpu`, a host `memory`, and each host pool are ceilings. A hive asking for more (or `cpu: 0`, "every CPU", under a host cap) gets the host value. With no host `cpu` configured the ceiling is the host's CPU count (`os.cpu_count()`). A pool the host does not define is ignored: only the host adds pools |
| `floor`, `repoll_seconds`, `roots` | host | host/fleet only; a hive overlay is ignored |

This **differs from `work.validation_slots`**, which deliberately ignores hive overlays. Slots are
one host-wide capacity shared by every hive's gates, so a hive must not resize it. The broker's
engine, packer and per-key policy are properties of the project being validated (what it runs and
how its keys pack), so they follow the hive; what the host can grant is still a host property, so
the budget and floor never exceed what the host layer allows.

`uv run python -m beadhive.validation_broker_engine [--hive H] [--json]` prints the effective
settings for a hive and the source of each field: `hive`, `global` (merged host/fleet), `default`,
`host cap` (a hive request the host limit lowered) or `BH_VALIDATION_BROKER_ENGINE`.

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
| `hq.authority_mode` | HOST-only, frame-local HQ authority mode: `signed`, `trusted` or `inherit` (default). `inherit` follows the learned fleet default (`hq.default_authority_mode`), which is `signed` until the operator sets it. `trusted` is an opt-in, frame-local waiver (see `BH_HQ_AUTHORITY_MODE`; no signature, expiry or head binding, cordon and content still apply); doctor reports it as one WARN. Never settable at fleet scope. Also read from the `hq` section of the operator settings file. |
| `hq.authority_expiry_recheck_s` | HOST-only. Seconds (non-negative number, default `300`) that a command reuses its last read of the HQ authority status for the expiry warning. `0` re-reads on every command. The first read of a process uses the default; the value is learned from the host config that read already loads. |
| `hq.admission_policy` | Currently `manual`; frame admission remains an explicit protected action in a signed fleet. In a trusted fleet (`hq.default_authority_mode: trusted`) registering frames are open-admitted instead (see below). |
| `hq.default_authority_mode` | FLEET default HQ authority mode for frames whose `hq.authority_mode` is `inherit`: `signed` (default; the key is absent) or `trusted`. Set it only with `bh hq authority mode-trusted` / `mode-signed` (`bh hq authority mode` shows it). Raising it to `trusted` needs the operator key once: frames learn it only from an operator-authorised read (on SQL HQ the authority re-signed against the new config head - the key is frame-relevant, so a key-less edit fences signed frames instead of downgrading them; on Git HQ the operator-signed config commit). Lowering it to `signed` always takes effect. The learned value is kept per host in `$BH_HOME/state/hq-fleet-authority-mode.json`. A trusted fleet also open-admits registering frames: `bh hq authority join` (run automatically on registration where the frame holds HQ authority-write access) records the frame's holder identity, instance and runtime key as an active incarnation with no grant, enrollment or operator key; cordon, drain and retire still apply. A frame pinned `hq.authority_mode: signed` refuses to work in a trusted fleet with an actionable message. **Mixed versions:** 0.23.x rejects the key (strict fleet schema) - upgrade every frame and operator host to 0.24.0 before `mode-trusted`; `mode-signed` removes the key again. **Trust delta:** in a trusted fleet anything that can reach HQ with write access and register can join. |
| `hq.sql.enabled` | Selects SQL only when true and the reader/trust binding validates. |
| `hq.sql.reader` | Required SQL configuration reader connection. |
| `hq.sql.publisher` | Optional separately credentialed fleet-config publisher connection. |
| `hq.sql.runtime` | Optional frame runtime connection; cannot coexist in one HOST binding with `observer` or `authority_writer`. |
| `hq.sql.observer` | Optional protected observer/read connection; mutually exclusive with a runtime binding on that HOST. |
| `hq.sql.authority_writer` | Optional protected authority publication connection; mutually exclusive with a runtime binding on that HOST. |
| `hq.sql.runtime_backend_identity`, `runtime_generation`, `runtime_initial_revision` | Separate runtime-authority backend pins. |
| `hq.sql.runtime_floor_path`, `runtime_operator_public_key` | Host-local runtime replay-floor path and operator public-key reference used to verify signed runtime authority. |
| `hq.sql.floor_path`, `backend_identity`, `generation`, `minimum_sequence`, `initial_revision` | Reader-side trust floor, backend/generation and initial revision pin. |
| `hq.sql.cache_ttl` | Bounded config cache age; it never waives revision or revocation checks. On a trusted frame it is also the maximum age of the cached verified snapshot a `bh bd` command may use while the HQ config reader is briefly unreachable ([HQ](HQ.md#config-read-retry)). |
| `hq.sql.liveness` | **Deprecated from 0.23.0** (kept so a HOST file shared with an older `bh` stays valid; removed with the receiver, not before). `receiver` (default, unchanged behavior) or `signed`. `signed` reads this frame's newest verified signed heartbeat from its own inbox at read time, treats hive-lease expiry as an advisory hint, and skips receiver renewal on write verbs. Tombstones, foreign holders, epoch fences and admission are unchanged. From 0.22.8, adopt, renew and release also need no receiver: hive leases are accepted from the frame's signed proposals at the hive's `refs/bh/epoch` (see `BH_HQ_SQL_HIVE_LEASE`). **0.23.0 supersedes both per hive and per incarnation, by data, with no key:** (a) on a hive whose `hq_live_hive_leases` row the director wrote, placement owns the lease row and the 0.22.8 proposal resolver is dropped for that hive; (b) on a frame whose `_session`/`_evidence` tables exist, liveness is read from them, and this key only chooses the fallback. Both revert to this key's behavior when the data is absent. See [HQ](HQ.md#hive-placement-by-the-director-dolt-server-hq-023) and [HQ session rows](HQ.md#session-rows). |
| `BH_HQ_SQL_LIVENESS` (env) | Process-level override for `hq.sql.liveness`: `receiver` or `signed`. When set it wins over the config key, so a newer bh can run signed without adding the key to a HOST file an older bh (strict schema) also reads. Any other value is an error, not a fallback. |
| `BH_HQ_SQL_HIVE_LEASE` (env) | Hive-lease acceptance under signed liveness: `proposal` (default) or `receiver`. `proposal` resolves the holder from this frame's verified signed proposals whose epoch equals the hive's current `refs/bh/epoch` fence; ties and conflicts fail closed. `receiver` restores 0.22.7 behavior, where only the receiver's protected row counts. Ignored in `receiver` liveness mode, and for any hive whose placement row the director wrote (0.23.0 drops the proposal resolver for it, so a frame's own proposal on that hive is refused); any other value is an error. See [the 0.22.8 release note](releases/signed-hive-lease-0.22.8.md). |
| `BH_HQ_AUTHORITY_MODE` (env) | Process-level override for `hq.authority_mode`: `signed`, `trusted` or `inherit`. Resolution order: this env var > deprecated `BH_HQ_AUTHORITY_ENFORCE` > `hq.authority_mode` (host config, or the operator settings file when `BH_HQ_OPERATOR_SETTINGS` is set) > `inherit` (= the learned fleet default, else `signed`). An invalid value fails closed to `signed` with an error. |
| `BH_HQ_AUTHORITY_ENFORCE` (env) | **Deprecated; use `BH_HQ_AUTHORITY_MODE=trusted`.** `true` (default) or `false`. `false` resolves to `trusted` with a deprecation warning and keeps its **old content waiver**: unlike `trusted` it also ignores cordon, admission state, release pins and caps, so the frame is self-asserted. UNSUPPORTED, dev/prototype only; to be removed (`bh-ihckx`, 0.24.1). Per host and per process environment; no fleet publish or `host.yaml` key. Any other value is an error. See [HQ - deprecated switch](HQ.md#unsupported-disabling-hq-authority-enforcement). |
| `BH_FRAME_HEARTBEAT` (env) | **Transitional** (retired with the heartbeat/receiver model, bh-qlgmm). `required` (default; unset or empty also means required) or `advisory`; any other value is an error. `advisory` keeps publishing and verifying heartbeats but stops a stale or missing beat from failing the `authenticated_fresh_heartbeat` eligibility predicate (a `waived` warning is printed to stderr); every other predicate, including the lease-derived ones, still fails closed. On a frame whose incarnation is data-switched to session rows (0.23.0) it is logged as ignored and has no effect. It is a **single-executor-frame escape only**: it must be cleared on every frame before any additional executor frame is admitted. Per process environment; no `host.yaml` key or fleet publish, so older readers sharing the HOST file are unaffected. Retire it in gated steps: (A1) the frame's eligibility reads session rows, or the decoupled heartbeat sender has run with no lapse for one full-gate soak window; (A2) unset `BH_FRAME_HEARTBEAT` in every unit, shim and profile on the frame and restart them; (A3) confirm `bh host eligible --hive <prefix> --json` stays eligible through a full gate with no `waived` warnings in the journal; (A4) only then remove the code. Never delete the code first. Rollback before A4: set `advisory` again (single-frame only). See [rotation window](design/frame-release-upgrade-runbook.md) for what it does not cover. |
| `hq.sql.<role>.*` | Per role (`reader`, `publisher`, `runtime`, `observer`, `authority_writer`): `host`, `port`, `database`, `user`, `tls_mode`, `server_name`, `ca_file`, `credential.config_path/profile/key`, and `connect_timeout`, `read_timeout`, `write_timeout`, `operation_timeout`. |
| `BH_HQ_OPERATOR_SETTINGS` (env) | Path to a JSON/YAML file with an `hq.sql` section (`reader`, `authority_writer`, `runtime: null`, pins, optional `authority_max_duration_s` (ceiling for opt-in `--duration`; default unlimited), optional `authority_mode` under `hq` (`trusted` lets key-less operator commands publish), optional `inbox_retention_s`, and for the director `placement_writer`) that supplies the operator binding for `bh hq authority rebind\|grant\|observe\|bind-beadyard\|status\|check\|prune-inbox`, `bh host release-upgrade plan\|apply\|check` and the frame lifecycle verbs `bh host admit\|cordon\|drain\|park\|resume\|quarantine\|frame-retire\|emergency-admit\|emergency-revoke` (wire 2.9.0, bh-siluj; needed on an operator host that is also a frame) instead of this host's `host.yaml`. From 0.23.0 the `--operator-settings <file>` flag on the same verbs takes precedence over this env var. Refused when it binds `runtime` or lacks `authority_writer`. `bh hq placement` and the host daemon's failover loop read the same file for `hq.sql.placement_writer` (see below). Shape and laptop renew procedure: [HQ](HQ.md#authority-laptop-renew). |
| `BH_HQ_INBOX_RETENTION` (env) | Retention margin for `bh hq authority prune-inbox` (duration such as `1h`, `2d`; default `1h`). A signed-mode heartbeat row is pruned only once its signed `renewTime` + lease + 30 s skew + this margin has passed, and the newest verified beat is always kept. `hq.sql.inbox_retention_s` in the `BH_HQ_OPERATOR_SETTINGS` file wins over it. Operator-side only; nothing prunes unless the operator runs the verb. See [HQ](HQ.md#signed-mode-inbox-retention). |
| `BH_HQ_AUTHORITY_MIN_REMAINING` (env) | Floor for `bh hq authority check` (overridden by `--min-remaining`, 0.23.0+; duration, default `0`: fail only when expired or config-unbound). |
| `BH_HQ_AUTHORITY_WARN_WITHIN` (env) | Lead time for authority-expiry warnings (only for an authority opted into a finite expiry with `--duration`; a default non-expiring authority never warns) (duration such as `6h`, `2d`; default `24h`). Environment-only by design: a fleet key would move the HQ head and a host key would break older readers. See [HQ](HQ.md#authority-expiry). |
| `host.forward.enabled` | Opt-in per frame (default `false`): bh's bd for a cut-over hive forwards claim, create and close to the hive's current primary. See [FORWARD-WRITE-PATH.md](FORWARD-WRITE-PATH.md). |
| `host.forward.endpoints.<frame>.*` | The hive server of primary frame `<frame>`: same fields as `hq.sql.<role>.*` (`host`, `port`, `database`, this frame's `user`, `tls_mode`, `server_name`, `ca_file`, `credential`). `tls_mode: required` needs `ca_file` and `server_name`. |
| `host.forward.serve.enabled` | Opt-in per frame (default `false`): this frame's hive server takes forwarders; `bh doctor` reports its watched globals, read-only root and grant conformance. |
| `host.forward.serve.operators` | Logins that are not forwarders (default `root`, `watchdog`; Dolt's built-ins always). Every other account must hold the table-scoped forwarder shape. |
| `host.forward.serve.require_tls` | Forwarder accounts are created, and must stay, `REQUIRE SSL` (default `true`). |
| `host.forward.serve.quiesce_before_reset` | Kill forwarder sessions before a divert reset (default `true`). |
| `host.forward.serve.watched`, `unwatched` | Overrides, additions and removals for the watched globals (default `dolt_force_transaction_commit=0`, `dolt_transaction_commit=0`, `read_only=0`, `max_connections=100`). |
| `host.forward.serve.root_path` | The hive server's `DOLT_ROOT_PATH`, checked read-only by `bh doctor` (default empty: not checked). |
| `hq.sql.placement_writer` | The **director credential** (0.23.0): same fields as `hq.sql.<role>.*`. Read **only from the operator settings file** (`BH_HQ_OPERATOR_SETTINGS` or `--operator-settings`), never from `host.yaml` or fleet config; the file is refused when it also binds `hq.sql.runtime`. It holds `UPDATE` on `hq_live_hive_leases` and `INSERT/UPDATE/DELETE` on `hq_live_failover_policy`, no other write right. Used by `bh hq placement` and by the host daemon's failover loop. No setup step creates it; provision it by hand: [Provision the director account](design/hq-placement-runbook.md#provision-the-director-account). |
| `hq.sql.release_catalog` | The **release catalog writer** (bh-vfrem.4): same fields as `hq.sql.<role>.*`. Read **only from the operator settings file**, never from `host.yaml` or fleet config; refused beside `hq.sql.runtime`. Holds `INSERT` on `hq_release_catalog` (plus `EXECUTE` on `dolt_add`/`dolt_commit`) and no other write right. Used by `bh hq release-catalog import\|verify\|revoke`; see [HQ](HQ.md#release-catalog). |
| `hq.release_catalog.auto_import` | HOST-local opt-in (default `false`): the host daemon imports each new PyPI release into the signed release catalog on a timer, with the same verification as `bh hq release-catalog import`. Needs `dolt-server` HQ, a settings file with `hq.sql.release_catalog` and `beadhive[release-verify]`; the daemon refuses to start otherwise. |
| `hq.release_catalog.interval_seconds` | Seconds between PyPI listings while `auto_import` is on (default `3600`, range `(0, 86400]`). |
| `hq.release_catalog.operator_settings` | Absolute path of the settings file carrying `hq.sql.release_catalog` (else `BH_HQ_OPERATOR_SETTINGS`). |
| `hq.release_catalog.trusted_root` | Absolute path of a pinned Sigstore `trusted_root.json` for offline verification (default empty: refresh the TUF root online). |
| `host.daemon.failover.enabled` | Opt-in (default `false`): the host daemon runs the director's unattended failover loop. Needs `dolt-server` HQ and a settings file with `placement_writer`; the daemon refuses to start otherwise. |
| `host.daemon.failover.interval_seconds` | Seconds between observations (default `60`, range `(0, 3600]`). Keep it well under `failover_after / 2`: a longer gap resets the observed window. |
| `host.daemon.failover.max_primary_spread` | Integer `>= 1` (default `2`): `bh doctor` warns when the busiest executor holds more hive primaries than the least busy by more than this. Invalid values are refused, never clamped. |
| `host.daemon.failover.operator_settings` | Absolute path of the settings file carrying `placement_writer` (else `BH_HQ_OPERATOR_SETTINGS`). |
| `failover_after`, `executor_floor` (**not config keys**) | Failover thresholds are per role and per hive and live in HQ **data**, in the `hq_live_failover_policy` table beside the placement row (`scope`, `setting`, `seconds`; scope is a hive prefix or the fleet default `*`). They are never `host.yaml` or fleet-config keys. Defaults: `executor` 3600 s (60 min), `transient` 1800 s (30 min); a viewer is never placed. The executor floor (setting `executor_floor`, fleet scope only) defaults to 2700 s (45 min). Set them with `bh hq placement policy` ([runbook, section 6](design/hq-placement-runbook.md)). Validation is on write and on load and a bad value is refused, never clamped: below the hard bounds (bd lease TTL plus reclaim grace, 15 min; `2 x (session TTL + sync interval)`, 10 min on `dolt-server` HQ) is refused, an `executor` value below the floor is refused, and a floor below a hard bound is refused. A floor from the hard bound up to 35.4 min (the measured false-stale stretch) is accepted with a warning. A row refused on load falls back to the fleet row, then the code default. |
| `BH_FORWARD_QUIESCE` (env) | `off` disables the forwarder-session kill before a divert reset. |
| `BH_FORWARD_PASSWORD` (env) | A forwarder's password when no fnox `credential` is configured. |
| `managed_repos[].frame_policy.config_revision` | Required policy revision label for a frame-managed hive. |
| `managed_repos[].frame_policy.requires` | Optional requirements: `isolation`, `trust_zone`, `arch`, `harness`, `harnesses`, and positive `max_sessions`. |
| `execution_hives` (host document) | Optional list of hive prefixes the frame may execute (default: unset, no restriction). Frame `capabilities.isolation` also accepts `workstation` beside `kvm`, `microvm` and `container`. |
| `managed_repos[].frame_policy.evict_after_s` | Required positive finite interval used by protected takeover policy. |
| `managed_repos[].dolt_remote` | Optional HQ-declared desired Dolt remote for the hive (`dolt-remote-descriptor/v1`, [ADR](design/hq-dolt-remote-descriptor-adr.md)): `url` (required; `s3://bucket/path`, `aws://[table:bucket]/db`, `file:///abs`, `git+ssh://…`), `name` (default `origin`), `credentials` (host profile label; empty = ambient AWS chain). Absent keeps today's Git-origin behaviour. URLs with credentials, unknown S3 query keys or a location another hive already declares are refused with a value-free, path-specific error. Applied only by `bh hive remote --reconcile` and onboarding; see the [runbook](design/hq-dolt-remotes-runbook.md). |
| `dolt.remote_credentials.<label>` | HOST-only credential source for descriptors naming `<label>`: `source` = `ambient` (default), `instance-role`, `aws-profile` (+ `aws_profile`) or `fnox` (+ `access_key_id`, `secret_access_key`, optional `session_token` fnox refs), plus optional `s3_checksums: when_required` (off by default; sets `AWS_RESPONSE_CHECKSUM_VALIDATION` and `AWS_REQUEST_CHECKSUM_CALCULATION` to `when_required` for every dolt/bd env using the profile and in the rendered server credentials file; required for multipart objects on Garage, see docs/TWO-STORES.md). Lookups only; values are injected into the one bd child that needs them and never printed, logged or stored. |

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

### Proposed keys (not shipped) {#hq-proposed-keys}

The [trust profiles and capabilities ADR](design/hq-trust-profiles-and-capabilities-adr.md)
(2026-10-09, status proposed) names keys that do not exist yet and are listed here only so the
table above is not read as the whole intended surface: `hq.trust_profile` (fleet: `open`,
`trusted`, `verified`, `attested`; `verified` equals today's defaults), `host.roles` (a set
composed from `viewer`, `planner`, `operator`, `transient`, `executor`, `director`; `host.role`
stays as a one-element alias), `hq.placement_mode` (`director`, `self`), `hive.write_policy`
(`fenced`, `forward`, `open`), `hq.tenure` and `hq.tenure_refresh`, `hive.adopt_existing`, and
the `hive.sync.*` batch auto-commit, auto-push and auto-pull settings. Setting any of them on a
0.24.0 host is a schema error.

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
# lease's expiry never gates a write: it is a failover/liveness HINT only (see
# docs/design/hive-writer-partitioning-adr.md §4). Legacy hives (no in-data bh_writer) still
# renew it best-effort from write verbs and the dispatch loop, as a liveness hint until M8
# placement — a failed renewal is logged, never refused. Cut-over hives don't renew it.
host:
  lease:
    renew_interval: 300                # legacy hives: renew when within this of the expiry hint
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
- Re-registering a hive (`bh hive init`, a prefix repair) rewrites only the keys `register`
  owns (`provider`, `org`, `repo`, `prefix`, `kind`, `upstream`, `furnish`, `contribution`);
  every other key on the entry — per-hive overrides, `frame_policy`, `stores` — is carried.

### `managed_repos[*].stores` — internal and public bead stores {#stores}

Optional, and absent by default: an entry without `stores` is one store at the checkout's
`.beads`, exactly as before. Declaring it splits the repository into a private, operational
`internal` store and an optional `public` projection store
([two-store-scope-adr.md](design/two-store-scope-adr.md)):

```yaml
managed_repos:
  - {provider: github, org: beadhive, repo: beadhive, prefix: bh,
     stores: {internal: {remote: beadhive-private},
              public: {prefix: bhpub, path: ~/.beadhive/stores/github/beadhive/beadhive/public,
                       remote: git-origin, source: github-issues}},
     dolt_remote: {name: beadhive-private, url: "s3://<bucket>/internal/beadhive"}}
```

| Key | Default | Rule |
|---|---|---|
| `prefix` | internal: the hive `prefix` | public: required; unique across every managed store |
| `path` | internal: the checkout's `.beads` | public: required, absolute or `~`, unique fleet-wide |
| `remote` | internal: the hive `dolt_remote.name` when one is declared; public: its nested `dolt_remote.name` | the name of the store's Dolt remote descriptor, never a URL: internal names the hive's `dolt_remote` (`bh-b1scz`), public names its own nested one; `git-origin` = the code origin's `refs/dolt/data`; internal may not be `git-origin` when a public store exists |
| `dolt_remote` | none | public only: the store's own `dolt-remote-descriptor/v1` (`{url, name, credentials}`, same rules as `managed_repos[].dolt_remote`); registered on the public store after it is minted |
| `authority` | internal `local`, public `remote` | internal must be `local`, public must be `remote` |
| `sync` | `local`: `push-pull`; `remote`: `pull-only` | `local` allows `push-pull`/`none`; `remote` allows `pull-only`/`none` |
| `source` | none | public only: `github-issues` |
| `dispatch` | internal yes, public no | a public store can never be operational |
| `adapter` | none (unbound) | public only, and requires `source`: the plugin id of the tracker adapter serving the store; known ids: `tracker-github` (serves `github-issues`). Fleet-owned intent only: it never enables a plugin, a read or a write ([tracker-adapter-plugin-adr.md](design/tracker-adapter-plugin-adr.md)) |

`bh config validate` rejects a `stores` block without `internal`, duplicate prefixes, paths or
remote names across the fleet, every invalid authority/sync combination, an internal `remote`
that is not the hive's `dolt_remote`, a public `remote` with no descriptor of its own, and two
descriptors (hive-level or nested) sharing one Dolt remote location.

`adapter` is checked at load from the document alone: an unknown id, a value that is not a
plugin id (rejected without being repeated), an adapter without `source`, an adapter on the
internal store, and an adapter serving another source are errors. The projection never reads it.
**Do not declare `adapter` yet:** a host whose binary predates the key cannot resolve any store of
that hive, so the key stays unused until the schema has rotated to every host (`bh-uf986`).
Validation errors of the stores block never repeat an input value.

A public store with `source: github-issues` and `sync: pull-only` is filled only on demand by
`bh hive remote sync <hive> --store <prefix> --pull-only` (formerly `bh hive project-issues`) — a
pull-only `bd github sync` into that
store, with the credential taken from the environment (`GITHUB_TOKEN` / `GH_TOKEN` / `gh auth`)
and never written to config ([github-issue-projection.md](design/github-issue-projection.md)).

The public store is synced only on demand by `bh hive remote sync <hive> --store <tracker> [--pull-only]
[--dry-run] [--json]` (pull, then fast-forward push; `--force-with-lease <sha> --confirm` is the
guarded one-time replacement). `bh hive project-issues` is its deprecated `--pull-only` alias.

A pull leaves the rows it imported in the store's Dolt working set: `bd github sync` does not commit
them, and only the push leg's `dolt commit` records them. Every pull report (human and `--json`)
therefore carries `uncommitted` (the rows `bd diff HEAD WORKING` finds, `0` when clean, `null` when
bd could not be read), `committed` and `warnings`; a non-zero count prints a `warning:` line and
never changes the exit code. `tracker_sync.commit_after_pull` (host-owned, **default `false`**)
commits the pulled rows with a descriptive `dolt commit` after a successful pull; with it off the
behaviour is unchanged apart from the report.

| Key (under `tracker_sync`) | Default | Meaning |
|---|---|---|
| `commit_after_pull` | `false` | strict boolean: after a successful pull (identity verified), `bd dolt commit` the working set; a failed commit is reported as a problem (exit 1) |
| `overlap_repull` | `false` | strict boolean: after bd's incremental pull, re-pull (`--issues=<numbers>`) every issue the hive's bound tracker adapter reports since bh's own inclusive checkpoint, so an edit bd's end-of-sync `last_sync` skipped still lands ([github-issue-projection.md](design/github-issue-projection.md), bh-clqa5). Needs `plugins.tracker-github` with `issues.read` and the store's `adapter` binding; without them the sync refuses. A failed listing or re-pull is a problem (exit 1) and leaves the checkpoint where it was |

Running a partitioned hive — layout, bootstrap modes, explicit public sync and publication,
diagnostics, recovery, and the separate rollout gate for a live hive — is in the operator guide
[TWO-STORES.md](TWO-STORES.md).

### `plugins` and `plugin_kernel` {#plugins}

`plugins.<plugin_id>` is the canonical, plugin-owned namespace (the legacy top-level `hitch`,
`orca`, `repowise`, `herdr` and `observaloop` sections remain its compatibility aliases). Built-in
fragments are validated against their published `plugin-config-<id>-v1` schema; an unknown plugin
id is kept as opaque, inactive data. `plugin_kernel` is core-owned discovery policy:
`enabled.<plugin_id>` (a boolean override; `false` always wins), `capability_owners`
(`capability.id@MAJOR` to plugin id) and `allow_external_entry_points` (default `false`). Both
are **host-owned**: a fleet push cannot switch a plugin on. Both are absent by default.

`plugins.tracker-github` configures the GitHub tracker adapter. Every switch is off and every
boolean is strict (`"false"` is invalid, not truthy). There is no `repo` key: owner and
repository come from the bound hive's `provider`/`org`/`repo`. No key holds a credential, a
command or an approval; credentials stay ambient. The adapter's import package ships inside the
`beadhive` wheel; the optional `beadhive[github]` extra adds its one third-party dependency,
`githubkit` (`uv tool install 'beadhive[otel,github]'`, or `uv sync --extra github` in a
checkout). Installing the extra enables nothing, `import beadhive` never imports `githubkit`, and
enabling the plugin without the extra refuses naming `beadhive[github]`.

| Key (under `plugins.tracker-github`) | Default | Meaning |
|---|---|---|
| `enabled` | `false` | the plugin's own gate; off means no owner, no port and no network |
| `base_url` | unset (public API) | GitHub Enterprise Server prefix: https, no userinfo, query or fragment |
| `credential.sources` | `[env, gh]` | credential source names in precedence order; empty means anonymous |
| `credential.per_invocation_cache` | `false` | keep the first token resolved in memory for the rest of ONE command instead of deriving it again for every GitHub operation (one `gh auth token`, about 70 ms, each; bh-25fvb). Opt-in, and the one exception to the never-cached credential: the token lives only in that command's token source, is never written or logged, and a token rotated or revoked while the command runs is not picked up until the next command. Applies to the intake verbs, `bh plugin tracker-github` probes and the tracker overlap re-pull; never to the host daemon's discovery timer, which keeps its adapters for the daemon's whole life and derives the token per operation whatever this key says. The key governs only the token: a command's GitHub client and connection pool are reused for the whole command whatever this key says (the token is still resolved per operation, and sent per request, when the key is off; the daemon builds a client per operation), and the org's issue-field definitions are read once per command, not once per `issue.fields` write (re-read once when a field or option is missing from them or GitHub answers a `404`/`422`; bh-vy7dx, bh-n0h15). New in 0.35.5 (bh-25fvb): an older host rejects the key, so set it only once every host runs that release (the rotation order in [UPGRADING](UPGRADING.md#0354--0355--outbox-reopen-faster-batches-no-delivery-on-a-duplicate-of)) |
| `read.issues.enabled` / `read.comments.enabled` | `false` | enable `issues.read` / `comments.read` |
| `read.labels.enabled` | `false` | enable `labels.read`: read the repository's label definitions (name, color, description). A read, so no grant; new (bh-la587), see the rotation note below |
| `read.include_closed` | `true` | scope within a read, not a feature switch |
| `read.page_size` | unset | listing page size, 1 to 100 |
| `http_cache.enabled` | `false` | conditional requests for the id-reconcile pass |
| `timeout_seconds` | `30` | seconds before a connect or read is abandoned; a number in the range `(0, 300]` (an integer or a float): zero, negative, boolean, non-numeric, `inf`/`nan` and any value above 300 are refused at config load, by `bh config validate` and by `bh config set` (nothing is written), never clamped, and the refusal does not repeat the value. There is no way to disable the timeout, and 300 s is a hard maximum rather than a default: an effectively unbounded timeout would reopen that hole, and one hung request would stall `bh work discover`, the host-daemon discovery timer and the outbox sender for that tracker binding. GitHub API calls do not legitimately need longer, and `retry.max_total_wait_seconds` already bounds the waits between attempts. The adapter's transport enforces the same bound from the same constant |
| `retry.max_retries` / `retry.max_single_wait_seconds` / `retry.max_total_wait_seconds` | `3` / `60` / `120` | the retry budget; single wait at most the total |
| `write.comment.enabled` / `write.labels.enabled` | `false` | enable `comment.create` / `labels.change`: necessary, never sufficient (each write needs its own operator-approved grant) |
| `write.label_definitions.enabled` | `false` | enable `labels.define`: create or update the repository's label definitions. Repository-scoped, necessary and never sufficient (each use needs an operator-approved single-use grant); new (bh-la587), see the rotation note below |
| `write.labels.owned_prefixes` | `[]` | labels Beadhive owns; empty means no label write is possible, and `write.labels.enabled: true` with an empty list is refused at config load |
| `write.issue_close.enabled` | `false` | enable `issue.close`: close a bound issue with its reason (`completed`, `not_planned` or `duplicate`) as `PATCH /issues/{n}` with only `state` and `state_reason`, never the labels. Issue-scoped, necessary and never sufficient (each close needs its own operator-approved single-use grant). It is also the outbox's close gate: with it off no close is drafted, approved or sent (`close_disabled`); there is no separate intake key. New in 0.35.3 (bh-pr5ll), see the rotation note below |
| `write.issue_reopen.enabled` | `false` | enable `issue.reopen`: reopen a closed bound issue on the operator's say (`bh work outbox reopen`), the inverse of `issue.close`, as `PATCH /issues/{n}` with only `state: open`, never the labels. Issue-scoped, necessary and never sufficient (each reopen needs its own operator-approved single-use grant; the close's grant never reopens). It is also the outbox's reopen gate: with it off no reopen is drafted, approved or sent (`reopen_disabled`); there is no separate intake key. New in 0.35.5 (bh-ih484), see the rotation note below |
| `write.issue_type.enabled` | `false` | enable `issue.type`: read each issue's type (`TrackerIssue.issue_type`; unread while off) and set it to a type the organization defines as `PATCH /issues/{n}` with only `type`, never the labels. Issue-scoped, necessary and never sufficient (each write needs its own operator-approved single-use grant). It is also the outbox's `issue.type` gate (`type_disabled`); there is no separate intake key. New in 0.35.3 (bh-poqy0), see the rotation note below |
| `write.issue_fields.enabled` | `false` | enable `issue.fields`: read each issue's field values (`TrackerIssue.fields`; unread while off) and set named ones: one `GET /orgs/{org}/issue-fields` resolves the field id and checks the option, then `POST /issues/{n}/issue-field-values` adds only those values (every other field and the labels are kept). Needs the write role; a classic token needs `read:org`, and a fine-grained token or GitHub App needs the organization permission "Issue Fields" (read) for the field listing and the repository permission "Issues" (write) for the POST. Issue-scoped, necessary and never sufficient (each write needs its own grant). It is also the outbox's `issue.fields` gate (`fields_disabled`). New in 0.35.3 (bh-poqy0), see the rotation note below |

The adapter ships the read path and its writers, `comment.create`, `labels.change` (owned
prefixes only), `labels.define`, `issue.close` (the state and its reason only), `issue.reopen` (the
state only), `issue.type` and `issue.fields` (the type, or the named field values, only). Each write
flag binds its writer and nothing else, and each write still needs its own operator-approved
single-use grant (no key can supply one). A read or write whose flag is off is refused before any
request (a disabled writer has no method on the bound object).
`read.include_closed: false` narrows issue listings but never the full id snapshot used to detect
deletions (a closed issue still exists). Retries wait on the adapter's own budget
above, never on githubkit's built-in retry.

As with `adapter`, nothing in a shared config should set these keys until the schema has rotated
to every host (`bh-uf986`).

`write.issue_close.enabled` is new in 0.35.3 (bh-pr5ll), with the `issue.close` tracker operation
and the outbox's `issue.close` operation kind. It follows the same 0.35.0 rotation order: an older
host rejects the key, and a host older than 0.35.3 cannot decode an `intake.outbox` document that
holds a close (it fails closed on it), so **every host that reads the hive's outbox runs 0.35.3
before the key is set**. A close rides on a triage reply (`fixed` closes as `completed`, `reject`
as `not_planned`, `duplicate-of` as `duplicate`; `needs-info` never closes), is approved with
`bh work outbox approve <id> --op close:<digest>` and sent with `bh work outbox send <id> --kind
close`, and only after that record's comment and every other comment of the report are settled
and the report's latest labels record is sent (`close_not_ready` otherwise, before any request).
`bh work outbox backfill-close [--dry-run]` drafts the close for reports already closed whose
issue is still open, drafting the missing reply too when the report never got one. A report closed
at completion with delivery `merged` or `released` (it tracked work) whose issue is still open is
recovered too (bh-iyog8): the close (reason `completed`) is attached to its latest reply or
milestone record that carries a live comment and no close, whether that comment is already sent or
not, and with no such record the completion record (the `merged` or `released` wording) is drafted
with the close. A report with delivery `pending`, an unlinked report and a closed issue are skipped
with a reason; a re-run drafts nothing twice, and nothing is approved or sent. The probe reports
`issue.close` as `no` below the triage role. A `duplicate-of` close also names the canonical
report's issue (bh-iamru): the PATCH adds `duplicate_issue_id` (the canonical's numeric issue id,
read with one GET before the write), and the close's digest shows `duplicate_of: <url>`. It needs
no other key; a canonical with no issue in the same repository closes as a plain duplicate.
GitHub's issue dependencies and sub-issues have no key: bh does not write them (no bead edge means
the same thing).

`write.issue_reopen.enabled` is new in 0.35.5 (bh-ih484), with the `issue.reopen` tracker operation,
the outbox's `issue.reopen` operation kind and the `bh work outbox reopen` verb, and follows the
same rotation order: an older host rejects the key and fails closed on an `intake.outbox` document
holding a reopen, so **every host that reads the hive's outbox runs that release before the key is
set**. A reopen is **only ever the operator's explicit act**: `bh work outbox reopen <report>
[--text T | --reply-file F] [--dry-run] [--json]` drafts, for ONE report whose bound issue reads
closed (closed by anyone), a reply carrying the reopen; `bh work triage apply` and `bh work wake`
never draft one. With the key off it refuses (`reopen_disabled`); it also refuses an issue that is
not closed (`reopen_not_needed`) and a second reopen while one is pending for the issue
(`reopen_pending`: send it, or discard it, first). `--dry-run` makes every check and prints the
planned reopen and reply, writing nothing. The reopen is sent **before** its reply: the reply's
comment is refused (`reopen_not_ready`, before any request) until its reopen is sent or retired,
and the reopen itself is refused while another record of the same issue still holds an unsent close
(send or discard that close first, or it would close the issue again). Per report the send order,
and the `send-approved` batch order, is reopen, comment, labels, close; **only `send-approved`
guarantees that order across separate records** (a question drafted on its own for the still-closed
issue is a separate record and is not held back by a pending reopen, so send the reopen first or
use the batch). Discarding the reopen retires its reply with it, and so does closing the report
afterwards (`reject`, `reroute`): a pending reopen of a closed report is superseded. Nothing is
approved or sent by the verb: approve it with `bh work outbox approve <id> --op reopen:<digest>`
(and `--op comment:<digest>`), send it with `bh work outbox send-approved` (or `bh work outbox send
<id> --kind reopen`); the probe reports `issue.reopen` as `no`
below the triage role. With the key unset nothing is read, drafted, approved or sent.

`write.issue_type.enabled` and `write.issue_fields.enabled` are new in 0.35.3 (bh-poqy0), with the
`issue.type` and `issue.fields` tracker operations and outbox operation kinds, and follow the same
rotation order: **every host that reads the hive's outbox runs 0.35.3 before either key is set**
(an older host rejects the keys and fails closed on an `intake.outbox` document holding either
operation). With a key on, `bh work labels sync` (and the `labels.on_transition` refresh) also
projects the bead's type onto the issue type and its priority onto the `Priority` field through
`intake.external.labels.issue_type` / `priority_field` below, from the tracked work and falling
back to the report. A difference is reported like label drift and drafted on the issue's same
`labels` record, so `labels review` / `labels approve --batch` approve it with the labels, each
operation with its own digest and grant; send it with `bh work outbox send <id> --kind type` or
`--kind fields`. The labels are unchanged: the `bh:type/*` and `bh:priority/*` labels are still
projected. The probe reports both as `no` below the write role, and `issue.fields` as `no` for a
classic token without `read:org`.

`read.labels.enabled` and `write.label_definitions.enabled` are new (bh-la587) and were added with
the `labels.read` and `labels.define` tracker operations. `BeadhiveConfig` forbids unknown keys, so
**an older host rejects both keys: upgrade every host before setting either one** (the `bh-tfko9`
rotation pattern); the same holds for the `intake.external.labels` section below. Both default
off, and `bh plugin tracker-github status` lists the two operations as disabled rows until set.
When `intake.external.labels.enabled` is on and either label write flag is on, the projection
`prefix` must be covered by `write.labels.owned_prefixes`, or the config is refused at load
naming both keys.

`plugins.beadlog-xtdb` configures the opt-in XTDB BeadLog store
([beadlog-format-adr.md](design/beadlog-format-adr.md) amendment A6): with it enabled,
`bh backfill apply|export --store xtdb` reach the operator's node through the `beadlog.store`
capability, and `bh plugin beadlog-xtdb status|doctor` report on it. It is **host-owned** and off
by default. bh never starts, stops or upgrades the node. No key holds the password: it is the
node's `XTDB_PASSWORD`, resolved from the host's fnox secret broker through `credential`, and a
`dsn` that embeds one is refused. The plugin's import packages ship inside the `beadhive` wheel;
the optional `beadhive[xtdb]` extra adds its one third-party dependency, `psycopg` (LGPL-3.0;
`uv tool install 'beadhive[xtdb]'`, or `uv sync --extra xtdb` in a checkout). Installing the
extra enables nothing, `import beadhive` never imports psycopg, and enabling the plugin without
the extra refuses naming `beadhive[xtdb]`.

| Key (under `plugins.beadlog-xtdb`) | Default | Meaning |
|---|---|---|
| `enabled` | `false` | the plugin's own gate (strict boolean) |
| `dsn` | `host=127.0.0.1 port=5432 user=xtdb dbname=xtdb` | libpq string or `postgresql://` URI of the loopback pgwire port; never a password |
| `credential.config_path` / `credential.profile` / `credential.key` | unset | fnox lookup of the node's `XTDB_PASSWORD`; unset means no password (doctor reports it) |
| `image` | the validated XTDB 2.2.0 digest | digest-pinned image the operator runs (`<repo>@sha256:<64 hex>`; a tag is refused; doctor warns on another digest) |
| `data_dir` | unset | absolute host path mounted at `/var/lib/xtdb`, for doctor's check (`tmpfs`/`ramfs` refused) |
| `health_url` | `http://127.0.0.1:8080/healthz/alive` | loopback healthz URL, probed with `GET` (the stock image `HEALTHCHECK` sends `HEAD`, answered `405`) |

### `intake.external` {#intake-external}

`intake.external` configures external issue intake
([external-intake-module-adr.md](design/external-intake-module-adr.md) section 15). It is
**host-owned** (a fleet push cannot switch it on), absent by default, and every switch is off.
No key binds a tracker (the store binding does that), holds a credential, a grant or an approval,
and no key can enable or pre-approve a tracker write.

What the keys below produce for a report, from import to the close of its issue, is in
[Report lifecycle and end states](INTAKE-LIFECYCLE.md).

| Key (under `intake.external`) | Default | Meaning |
|---|---|---|
| `enabled` | `false` | enable `bh work import <issue-url>` and `bh work discover`; off means the intake port is the null one and they refuse with `intake_disabled`. They also need `plugins.tracker-github.enabled` (and the hive's `stores.public.adapter` binding and `plugins.tracker-github.read.issues.enabled`): without the plugin on no tracker adapter is built and they refuse naming it. The token comes from `plugins.tracker-github.credential.sources`, read per operation and never stored (unless `credential.per_invocation_cache` opts into keeping it in memory for one command) |
| `default_type` | `bug` | issue type of an imported report: `bug`, `feature` or `chore` |
| `discovery.enabled` | `false` | run the opt-in host-daemon discovery timer (needs `enabled` too) |
| `discovery.interval_seconds` | unset | seconds between timer runs; **required** when `discovery.enabled` is true (there is no default cadence) |
| `discovery.full_reconcile_every` | `24` | every Nth timer run is a full pass (all issues including closed, then the id reconcile); the rest resume from the checkpoint |
| `completion_policy` | `close-on-merge` | which milestone completes a linked report: `close-on-merge`, or `close-on-release` (the report stays open and held, `delivery:merged`, until a release is verified) |
| `milestones.draft_on_merge` | `false` | draft a `merged` progress notice when linked work merges; the completion milestone always drafts its record (with an `issue.close`) regardless |
| `milestones.draft_on_release` | `false` | draft a `released` progress notice when `bh work delivery release <tag>` verifies a release; the completion milestone always drafts its record (with an `issue.close`) regardless |
| `milestones.append_release_to_closed` | `false` | also draft the `released` notice (comment only) for a report that is already closed |
| `milestones.catch_up_on_link` | `false` | when `bh work link` (or `accept --work`) links a report to work that already merged (never a `duplicate-of`, whose delivery is its canonical report's), give it the milestones it missed from the merge (and verified release) another tracking report recorded: `delivery:merged` / `released`, a record where drafting is on, then the completion policy. Off, such a report stays `delivery:pending` |
| `milestones.draft_on_start` | `false` | draft a `started` record for every report that tracks work (still `delivery:pending`) when that work is claimed for the first time (`bh work claim`, `claim --group`, `start`, `next`). Keyed on the work: a re-claim, a resume after a bounce or a claim after an abandon drafts nothing. A failure only warns; the claim never fails, and off the claim reads nothing extra |
| `milestones.draft_on_blocked` | `false` | allow `bh work delivery blocked <work> --reason <public text>` to draft a `blocked` record (fixed wording plus the reason) for every report that tracks the work and is still `delivery:pending`. Off refuses naming this key |
| `milestones.draft_on_deprioritised` | `false` | the same for `bh work delivery deprioritised <work> --reason <public text>` and a `deprioritised` record |
| `outbox.enabled` | `false` | allow outbox records to be drafted and approved; milestones draft nothing without it |
| `outbox.labels_enabled` | `false` | allow `labels.change` operations to be approved and sent (`bh work outbox approve --op labels:...` and `send --kind labels`); needs `outbox.enabled` too, and is separate from the comment sender and from the `tracker-github` labels capability (`write.labels`), which must also be on. Off refuses with `labels_disabled` before any tracker request. Only `approve` and `send` are gated by this flag: `render`, `discard` and `retry --kind labels` are local state moves that make no tracker request, so they are deliberately not blocked. A retried label operation still cannot reach GitHub without a new `approve` and `send`, and both of those are gated |
| `outbox.mode` | `supervised` | the only accepted value: every send needs an operator's per-record approval |
| `outbox.batch_recheck_seconds` | `0` | how long a one-process batch (`bh work outbox send-approved`, `bh work labels approve --batch`) reuses its primary-host check and epoch read before taking them again (bh-pc9rf). `0` (the default) re-checks on every hold of the lock, as a single `send` does; a positive value is an opt-in. Since bh-25fvb a batch takes ONE hold of the per-binding lock per report (not one per operation) and sends or approves that report's operations under it; every operation still spends its own single-use grant and writes its own write-ahead and receipt, and only the host check is shared. The check itself costs about 1.9 s per hold on the factory (13 s before), and `bh work labels sync` drafts inside the same batch scope. The factory sets `300` once every host runs 0.35.3 (an older host rejects the key) |
| `outbox.send_writes_per_minute` | `80` | the most outbox write requests (`bh work outbox send` and `send-approved`) started in any minute, counted across every command and hive on this host in the host-level write ledger (bh-eqtxv). The unit is one GitHub write request: a `labels.change` is one `POST` plus one `DELETE` per removed label, and a rate-limit resend counts again. `1` to `80`; `80` is GitHub's documented limit of 80 content-generating requests a minute and the hard upper bound (a larger value is refused at validation, never clamped). There is no unpaced mode. See [Outbox write pacing](#outbox-write-pacing-bh-eqtxv) |
| `outbox.send_writes_per_hour` | `500` | the most outbox write requests started in any rolling hour on this host, on the same terms. A batch whose next operation does not fit stops with `stopped: hourly_budget` (the rest stay approved; re-run once the hour frees), and a single `send` is refused `hourly_budget`. `1` to `500`; `500` is GitHub's documented hourly limit and the hard upper bound |
| `comments.enabled` | `false` | ingest bound reports' issue comments: `bh work thread ingest` (needs `plugins.tracker-github.enabled` and `plugins.tracker-github.read.comments.enabled`), `thread show` and `thread considered`. Reads only; off refuses naming this key |
| `comments.deletion_confirmations` | `2` | complete comment-id snapshots that must agree a comment is gone before it is tombstoned |
| `comments.purge_deleted_bodies` | `true` | drop a tombstoned comment's latest body (its digest is kept; not erasure) |
| `comments.max_body_chars` | unset | optional cap on a stored comment body (the digest is of the full body) |
| `needs_info.enabled` | `false` | allow `bh work needs-info <report> --ask <text>` (draft the question as a supervised reply and move a bound report to `intake:needs-info`; also needs `outbox.enabled` and `comments.enabled`) and `bh work wake <report>` (back to `intake:untriaged` once its ingested thread holds a reply from the reporter). Nothing is sent: the question posts only after an operator approves it. Off refuses naming this key |
| `needs_info.sweep` | `false` | allow `bh work wake --all` over every waiting report; needs `needs_info.enabled` too |
| `labels.enabled` | `false` | enable the registry-derived GitHub label projection (new, bh-la587): `bh work labels sync` and `bh work labels define` (bh-v3fe7). The section only shapes the label catalog: it reads and writes nothing itself, and every label write still needs `plugins.tracker-github.write.labels.enabled` / `write.label_definitions.enabled` and an operator-approved single-use grant. `sync` and `define` read through `plugins.tracker-github.read.labels.enabled` (and `read.issues`); a sync that drafts also needs `outbox.enabled` and `outbox.labels_enabled`. Off refuses naming this key |
| `labels.prefix` | `bh:` | the namespace prefix of every projected label; non-empty and trimmed. When `labels.enabled` and a tracker label write flag are on it must be covered by an entry of `plugins.tracker-github.write.labels.owned_prefixes`, else the config is refused naming `intake.external.labels.prefix` |
| `labels.project` | `[intake, delivery, progress, type, priority, component]` | the registry dimensions projected as labels (unique; any other name is refused naming `intake.external.labels.project`) |
| `labels.aliases` | `{}` | dimension to display group, replacing the dimension name in a projected label; a key that is not a projected-dimension name is refused |
| `labels.colors` | `{}` | dimension to label color: exactly six hex digits, no `#`; a bad color or an unknown dimension is refused naming `intake.external.labels.colors` |
| `labels.overrides` | `{}` | `dimension/value` to `{name, description, color}` (each optional; `color` is six hex digits) overriding one label; a key without a known dimension is refused naming `intake.external.labels.overrides` |
| `labels.issue_type.map` | `{bug: Bug, feature: Feature, task: Task, chore: Task}` | bead type (case-insensitive) to the issue type name the organization defines, set beside the labels while `plugins.tracker-github.write.issue_type.enabled` is on. Setting it replaces the whole map; a bead type it does not name is skipped with a note, never guessed (drop `chore` to leave chores untyped). Keys and values are non-empty and trimmed. New in 0.35.3 (bh-poqy0) |
| `labels.priority_field.name` | `Priority` | the single-select issue field the bead priority is written to (while `write.issue_fields.enabled` is on) |
| `labels.priority_field.map` | `{P0: Urgent, P1: High, P2: Medium, P3: Low, P4: Low}` | bead priority (`P0` to `P4`, any case) to the field's option name; setting it replaces the whole map, an unmapped priority is skipped with a note, and a key that is not a priority is refused naming `intake.external.labels.priority_field.map`. New in 0.35.3 (bh-poqy0) |
| `labels.on_transition` | `false` | reflect a state change onto the labels when it happens (only acts with `labels.enabled`): every disposition (`accept`, `reject`, `reroute`, `promote`), `needs-info`, `wake`, `link` / `unlink` and the milestone triggers also draft or refresh the report's ONE `labels` outbox record from stored state, with no tracker request. It only drafts (it needs `outbox.enabled` and `outbox.labels_enabled` to draft anything) and never fails the transition |

`bh work import` additionally needs the hive's public store to bind a tracker adapter and the
plugin's `read.issues.enabled` ([tracker-adapter-plugin-adr.md](design/tracker-adapter-plugin-adr.md));
it lands the issue in `intake:untriaged` with `origin:github`, held, and never writes to the
tracker. As with `adapter`, nothing in a shared config should set these keys until the schema has
rotated to every host (`bh-tfko9`).

`bh work discover [--full]` and the daemon timer run the same pull-only discovery through the
import upsert: an incremental pass resumes from its checkpoint (a file in the hive's private
`.bh/intake/checkpoints`, the tracker's own mark, never the local clock) and a re-run changes
nothing; `--full` re-pulls every issue including closed ones and marks reports whose issue is gone
`source_missing` in their snapshot (it never deletes or closes a report). The timer needs both
`enabled` and `discovery.enabled`, a chosen `interval_seconds` and a wired tracker adapter (the
daemon refuses to start otherwise); it never writes a tracker or the public store.

### Where a batch spends its time: the `timings` block (bh-17g6b)

`bh work labels sync`, `bh work labels approve --batch`, `bh work outbox send-approved` and
`bh work outbox send` add a `timings` object to their `--json` output (and only there: the human
output, the stored outbox record and its receipt are unchanged, so no wire shape changes). It is
measured, not modeled, from one monotonic clock; a run on the factory records its own profile.

```json
"timings": {
  "schema": 1,
  "clock": "monotonic",
  "startup": {"source": "interpreter", "ms": 3010.2, "interpreter_to_entry_ms": 40.1,
              "entry_to_first_work_ms": 2970.1},
  "total_ms": 91234.5,
  "unaccounted_ms": 812.3,
  "phases": {"config_load": {"count": 1, "ms": 1403.9}, "...": {"count": 0, "ms": 0.0}},
  "holds": [{"binding": "github:acme/widget", "ms": 2410.0, "phases": {"host_check": {"count": 1, "ms": 1850.0}}}],
  "holds_omitted": 0
}
```

- `startup`: process start to the verb's first work. `interpreter_to_entry_ms` is the
  interpreter's age when `bh` entered its CLI (Linux, from the kernel's start time; `null`
  elsewhere), `entry_to_first_work_ms` the import cost of the command tree up to the verb, and
  `ms` their sum. `source` is `interpreter` when both are known and `entry` when only the second
  is; when neither is known (an injected clock) `source` is `entry` and the three `ms` fields are
  `null`.
- `total_ms`: the verb's first work to its output. `unaccounted_ms` is `total_ms` less every
  phase below, the time nothing claims.
- `phases`: always all of these keys, `count` occurrences and their `ms` total (a phase's time is
  its own: a phase started inside another is taken out of the outer one, so the phases add up):

  | phase | what it measures |
  |---|---|
  | `config_load` | the effective configuration load (the SQL read) |
  | `wiring` | the plugin binding: the tracker adapter, identity and marker codec, before any request |
  | `host_check` | the primary-host check and epoch read of an intake-lock hold, when it was evaluated |
  | `host_check_reused` | holds of a batch that reused an earlier check (`outbox.batch_recheck_seconds`); count only |
  | `intake_lock` | taking the per-binding `flock` |
  | `store_open` | choosing and opening the store route (the Beads service session) |
  | `store_read`, `store_write` | one round trip each to the report store (a `bd` call or a service request); session-cached reads make none |
  | `github_token` | resolving the credential (`gh auth token`), once per GitHub operation |
  | `github_client` | building (or, with the per-command client, fetching) the HTTP client: once per GitHub operation, or once per command when the adapter is command-scoped (bh-vy7dx) |
  | `github_request` | each HTTP request (a retry counts again); the org's issue-field definitions are read once per command (bh-n0h15), so an `issue.fields` write is one request after the first |
  | `pacer_wait` | the outbox write pacer waiting for its spacing or for a full minute to free (`outbox.send_writes_per_minute`, bh-eqtxv); `outbox send` and `send-approved` only |

- `holds`: one entry per hold of the intake lock, that is per report, not per outbox operation (a
  batch takes one per report and sends or approves that report's operations under it; a single
  send is one). A hold that was refused (not the primary host, or busy) is listed too, with what it
  spent before refusing. Each has the hold's own `ms` and
  the non-empty phases inside it, so a batch shows what each report cost. At most 500 are listed;
  `holds_omitted` counts the rest.

The collector is installed for the command and removed with it, and the seams that feed it (the
intake lock, the report store's routes and the tracker adapter's transport) do nothing when it
is absent, so a command that is not measuring, and every daemon iteration, pays a context
lookup at each seam (about four per hold).
The `-m perf` model (`tests/test_intake_outbox_batch_perf.py`) reads its latencies from the
recorded profile `tests/fixtures/intake_batch_profile.json`: put a real batch's numbers there.

### Outbox write pacing (bh-eqtxv)

`bh work outbox send` and `send-approved` pace every GitHub **write request** they make, not every
operation, through one host-level **write ledger** shared by every command and hive on the host:
at most `outbox.send_writes_per_minute` (default 80) starts in any minute and
`outbox.send_writes_per_hour` (default 500) in any rolling hour. Pacing is on by default and has
no off switch; the keys can only lower it. Sends stay serial, one report at a time.

- **The ledger:** `$XDG_STATE_HOME/beadhive/github-writes/<api host>.ledger` (falling back to
  `~/.local/state/...`; `<api host>` is `api.github.com`, or the GHES host of
  `plugins.tracker-github.base_url`), one wall-clock entry per request, and its permanent lock file
  `<api host>.ledger.lock`. Both are created on first use. Deleting the ledger forgets the last
  hour; both are safe to delete while no send runs. A ledger that cannot be read is reported once
  on stderr and counted as a full minute (one minute's wait), never as empty.
- **Before each operation's write-ahead** the batch checks its stop, then the operation's expected
  requests must fit in the hour (1, or `1 + removals` for a labels change with additions), and it
  waits for the spacing (about 0.75 s at 80 a minute) and for room in the minute. **Every request
  is recorded in the ledger as it starts**, so the limits hold for the requests GitHub receives,
  however long the write-ahead takes. A request is never refused mid-operation: over the hour it
  is recorded and the batch stops after it. A ledger that cannot be written stops the batch with
  `error`.
- **The bounds, exactly** (derived from the code and each pinned by a test):

  - **Minute: at most 80 request starts in any 60 s on the host, strictly**, across every command
    and hive: each request start is one locked ledger step that waits while the last 60 s hold 80.
    Two exceptions: a **forward step of the wall clock** makes the ledger's entries look older, so
    in the minute after the step **at most 80 extra (100% over) may start, once per step** (a step
    back only delays); and a ledger that **stops being writable mid-operation** lets that one
    operation's remaining requests go unrecorded (at most its own requests, below), then the batch
    stops with `error`.
  - **Hour: at most 500 request starts in any rolling hour**, except that a request inside an
    operation is never refused. **One command alone** goes over by at most its in-flight
    operation's **unexpected** requests (its expected ones are checked before the write-ahead):
    3 rate-limit resends per request plus, for `issue.fields`, one retry on stale field ids with
    its own 3 resends, so **at most 7 extra (1.4% over) for `issue.fields`, 3 × (1 + removals) for
    `labels.change`, 3 (0.6%) for every other kind**. **k commands racing** on the shared ledger
    can each also start their own in-flight operation's requests past the same last free slot, so
    **at most the sum of their in-flight operations' requests plus those extras** (two other
    one-request operations: 2 extra, 0.4%). Every request over the hour is recorded and stops its
    command with `hourly_budget`.
- **Stops** (`stopped` in the batch JSON is a reason code, its text in `stopped_detail`):
  `not_primary`; `rate_limited` (the first rate-limit answer GitHub gives, including
  `retry-after: 0`; the request in flight finishes its own retries); `hourly_budget`;
  `interrupted` (the first `SIGINT`: the operation in flight finishes and writes its outcome,
  a second `SIGINT` aborts as before); `error` (an unexpected failure in one operation; the
  command exits non-zero). Every operation not started is listed in `failed` with the stop
  reason and stays `approved` with its grant, so re-running `send-approved` continues.
- **Output:** the batch JSON gains `"pacing": {"writes_per_minute", "writes_per_hour",
  "requests", "waited_seconds", "budget_frees_at"}` (the last is the epoch second the hour frees
  enough after an `hourly_budget` stop, else `null`), and the `timings` block a `pacer_wait`
  phase.
- **What it does not cover:** other hosts (the ledger is host-local), bh's own non-outbox GitHub
  writes (label definitions), `gh` and the web interface on the same account. The stop on the
  first rate limit is the backstop for those.

The milestone and outbox keys only ever **draft**: a merge or a verified release leaves a `drafted`
record for an operator to render, approve and send; no key sends, approves or mints a grant, and a
tracker write still needs the adapter's own write flag. With every default, merge and release
behave exactly as before. `outbox.labels_enabled` is a new key: `BeadhiveConfig` forbids unknown
keys, so nothing in a shared config should set it until the schema has rotated to every host
(`bh-tfko9`).

The `bh work outbox` verbs that reach the tracker (`draft`, `approve`, `send`, `reconcile`,
`discard`, `retry`, and `show <id>`'s dry run) build it from the same binding as import: with
`enabled`, `outbox.enabled` and `plugins.tracker-github.enabled` all on, the tracker is the hive's
bound adapter, the own-message identity is the bound `owner/repo` plus this hive's install id (a
random id minted once into the private `.bh/intake/install-id`; never configuration), the marker
line is the adapter's own codec, and the owned label prefixes are
`plugins.tracker-github.write.labels.owned_prefixes` (only while `write.labels.enabled`). Any
switch off refuses naming it. Without a tracker, `bh work outbox show <id>` prints the stored
draft, its event and its operations' states instead of the dry run (no digest, so nothing can be
approved from it). Nothing here posts: a send still needs the adapter's write flag and an
operator's per-operation approval minting a single-use grant.

The `comments.*` keys are new (bh-o9rep): as above, nothing in a shared config should set them
until the schema has rotated to every host (`bh-tfko9`).

The `needs_info.*` and `milestones.catch_up_on_link` keys are new (bh-61xkc), and so is the
`intake:needs-info` value they write: an older host rejects both the keys (`extra=forbid`) and the
label (`bh label validate`), so nothing in a shared config should set them, and no hive should use
the value, until the release has rotated to every host (`bh-hmtjn`, the `bh-tfko9` pattern). A
report on `intake:needs-info` keeps its `intake-hold:` gate (never ready, never claimable), leaves
the untriaged queue, and every triage disposition fields it as it fields an untriaged report.

The `milestones.draft_on_start`, `.draft_on_blocked` and `.draft_on_deprioritised` keys are new
(bh-dev42), and so are the outbox events `started`, `blocked` and `deprioritised` and the
`notified` values of the same names: an older host rejects the keys (`extra=forbid`), cannot decode
a report's `intake.outbox` holding such a record, and rejects the labels (`bh label validate`), so
nothing in a shared config should set them until the release has rotated to every host
(`bh-hmtjn`, the `bh-tfko9` pattern). The three only draft, in the `milestone` group (a newer one
supersedes an unsent older one), only while a report's delivery is `pending`, never move a
dimension and never complete a report. The `blocked` / `deprioritised` reason is the one free text
a milestone carries: the leak check holds a record whose reason names a bead id, branch or path.

The `labels.*` keys are new (bh-la587): an older host rejects the whole `intake.external.labels`
section (`extra=forbid`), so nothing in a shared config should set them until the release has
rotated to every host (the `bh-tfko9` pattern). They are host-owned and plain data: no key holds a
credential, a grant or an approval. The issue label changes they draft are the new `labels`
outbox record type, which an older host cannot decode: run `bh work labels sync` only once every
outbox-reading host runs 0.35.0 ([UPGRADING.md](UPGRADING.md)).

The `bh work labels` verbs (bh-v3fe7, wire 2.22.0) read these keys: `sync [<report>|--all]
[--include-closed] [--dry-run] [--prune-unmapped]` drafts at most one `labels` record per issue
(one issue listing and one `labels.read` per run; never sent), `define [--dry-run] [--prune]
[--approve <digest>]` renders the repository's definitions plan with a digest and, from an
operator seat, applies it with one single-use grant per tracker write (noted in the private
`.bh/intake/audit.log`), `review` renders the batch of drafted label changes with its digest, and
`approve --batch <digest>` (operator seats) mints one grant per issue; a send is still
`bh work outbox send <id> --kind labels`. `bh label docs` adds a "Projected GitHub labels" table
while `labels.enabled` is on.

The `bh work triage` verbs (bh-v3fe7, wire 2.22.0) add no key. `review --pending` needs
`comments.enabled` and the tracker's `read.comments.enabled` (a packet without its full thread is
refused); `propose`, `check` and `apply --reviewed` need only `enabled`. `apply` drafts a reply or a
question through the outbox (`outbox.enabled`) and asks through `needs-info` (`comments.enabled`),
so with either off the report that needs it fails and nothing is written for it; it refreshes the
touched reports' labels while `labels.enabled` is on. The pre-apply checks read the hive checkout's
git (ancestry, tags, changed paths; read-only) against `work.integration_branch`, and the fixing
commits from each closed work bead's `git.commits` linkage. Since 0.35.3 (bh-iamru) they also
resolve every `#N` a `reporter_text` names against the bound repository, with ONE issue listing per
run and only when a `#N` appears: this needs the tracker plugin with `read.issues.enabled`, and
without it such a text fails as `mention_unverified` (no new key).

Since 0.35.2 a proposal goes stale only when a comment changed after its `thread_seq` (recording a
triage read with `thread considered` no longer counts), `apply` records the analysis's read through
`thread_seq` before it asks a needs-info question, an owner target that is itself a report fails the
checks (`owner_not_work`), and a reject's close reason is cut to the 255 characters Beads stores
(the whole text stays in the reply and the `intake` state change). A report a failed close left
open on `intake:rejected` is finished by the next `apply` or `bh work reject`, which only close it.

### `backfill.judge` {#backfill-judge}

`backfill.judge` configures the opt-in judge that backfill reconcile asks about its fuzzy
residual ([backfill-platform-adr.md](design/backfill-platform-adr.md) section 4). The section is
**host-owned** (a fleet push cannot switch it on: the judge runs a host command that may spend
API credit) and absent by default, and every default is off: with `enabled: false` the
deterministic lexical engine answers alone and no subprocess is ever spawned. When enabled,
`command` receives one `bh-judge/1` JSON request on stdin and must print one `bh-judge/1`
response (schema:
[`bh-judge-v1.schema.json`](schemas/bh-judge-v1.schema.json)). A timeout, a non-zero exit, or
malformed or wrong-version output falls back to the deterministic engine, and the plan records
the fallback. Verdicts are recorded in the plan and never asked again at apply time.

| Key (under `backfill.judge`) | Default | Meaning |
|---|---|---|
| `enabled` | `false` | ask `command` when the deterministic engine abstains |
| `command` | `bh-judge-haiku` | the judge command line; the bundled reference needs `beadlog-sources[haiku]` and reads `ANTHROPIC_API_KEY` from its environment only (never config, never logged) |
| `timeout_seconds` | `30` | seconds one call may run before it falls back |
| `model` | `claude-haiku-5-5` | the model named in every request |
| `auto_accept` | `{}` | judgment family (for reconcile, `reconcile.match`) → confidence `0..1` at or above which a judged verdict needs no review; empty means every judged verdict is reviewed |
| `max_calls_per_run` | `100` | judge calls one run may make; past it the judge abstains |
| `cost_per_call_usd` | `null` | operator-estimated USD per call; only reported by `bh backfill judge setup`, null reports nothing |
| `credential` | empty | host-local fnox lookup (`config_path`, `profile`, `key`) for `ANTHROPIC_API_KEY`; a reference only, never the value. Empty: the key must already be in the environment |

`bh backfill judge setup [--dry-run] [--no-install] [--enable] [--fnox-config P --fnox-profile N
--fnox-key K]` enables the judge in one step: it verifies (or installs) `beadlog-sources[haiku]`,
resolves `ANTHROPIC_API_KEY` through the host's fnox broker (the same seam as HQ's SQL transport)
or the environment, and makes one `bh-judge/1` probe call that reports model and latency; an
offline or rejected probe is a failure, not a fallback. The config changes only with `--enable`
and only after every step passed (it then writes `backfill.judge.enabled` and, when given, the
fnox reference). The key is never printed, logged or stored; `--dry-run` lists the steps and
changes nothing. When `credential` is set, backfill runs pass the resolved key to the judge
command's environment only.

The `beadlog` CLI (no BeadHive needed; see [BeadLog CLI](BACKFILL.md#beadlog-cli); installed by
`beadlog-sources` and by the `bh` wheel) takes the same keys
as `--judge-<key>` flags (`--judge-auto-accept family=threshold,...`) and `BEADLOG_JUDGE_<KEY>`
env vars; flags win over env, env over defaults. Pointing `command` at another `bh-judge/1`
speaker, such as Jev, needs no code change. `backfill` is a new section: `BeadhiveConfig` forbids
unknown keys, so nothing in a shared config should set it until the schema has rotated to every
host (`bh-tfko9`).

### `backfill.timeline.gapfill` {#backfill-timeline-gapfill}

`backfill.timeline.gapfill` configures the opt-in gap fill of `bh backfill export --history`:
snapshot-level history from `dolt_history_issues` for the beads that have no `created` row in the
events table ([BACKFILL.md](BACKFILL.md#gap-fill) has the method, the caveats and the measured
cost table). It is **off by default**, **host-owned** (part of `backfill`: a fleet push cannot
switch it on) and read-only: every query is a bounded `SELECT`. With `enabled: false` nothing
extra runs and the archive and its `bdlg.history` annotation are exactly what they were.

| Key (under `backfill.timeline.gapfill`) | Default | Meaning |
|---|---|---|
| `enabled` | `false` | run the gap fill when exporting with `--history` |
| `batch_size` | `25` | beads per history scan (>= 1). Each scan walks every store commit (~9 s on `bh`) and costs ~1.3 s and ~3 MB of CSV per bead, so the whole `bh` gap takes about an hour at any size >= 25 |
| `timeout_seconds` | `180` | seconds one gap-fill query may run before the run stops; the cursor `<out>.gapfill.json` lets the next run resume |

`backfill.timeline` is a new subsection and `BeadhiveConfig` forbids unknown keys, so nothing in a
shared config should set it until the schema has rotated to every host (`bh-tfko9`).

### `backfill.coverage` {#backfill-coverage}

`backfill.coverage` configures the link-coverage metrics of `bh backfill coverage`
([BACKFILL.md](BACKFILL.md#coverage) has the three ratios and the cache). It is **off by
default** and **host-owned** (part of `backfill`). The command itself always works when run by
hand; `enabled` governs the unattended part: whether an applied backfill pass refreshes the cached
state. The refresh is read-only toward the hive (one bd export, one `git log`, one doc walk) and
writes only the cache file; a failure warns and never fails the apply.

| Key (under `backfill.coverage`) | Default | Meaning |
|---|---|---|
| `enabled` | `false` | rewrite the cached state after every applied backfill pass, and show the cached ratios in `bh doctor` and `bh hive ready` (advisory) |
| `code_types` | `[bug, chore, feature, task]` | bead types that count as code-bearing in the closed-beads-with-commits ratio; `event`, `gate` and `molecule` never count, whatever is listed |
| `doc_roots` | `[docs]` | directories whose `*.md` files (recursively) are the set of docs measured (at least one) |
| `example_limit` | `5` | top unlinked examples kept per ratio (0 keeps none) |
| `warn_below` | `0.60` | advisory threshold, per ratio (0 to 1): a cached ratio below it renders a warning in `bh doctor` and `bh hive ready`, never a failure |

`code_types` and `doc_roots` are part of the cache's inputs hash, so changing either marks the
cache stale until the next refresh.

### `backfill.continuous` {#backfill-continuous}

`backfill.continuous` keeps linkage and the lifecycle timeline current with small incremental
runs ([BACKFILL.md](BACKFILL.md#incremental-and-continuous-backfill) has the cursors, the scope
of a run and its bounds). It is **off by default** and **host-owned** (part of `backfill`). A run
is best effort: any failure warns on stderr and never fails the merge or sync that triggered it.

| Key (under `backfill.continuous`) | Default | Meaning |
|---|---|---|
| `enabled` | `false` | run the incremental passes after the configured triggers |
| `passes` | `[commits, timeline]` | the incremental passes, in order: `commits` (`git.commits` appends) and `timeline` (`metadata.lifecycle.*`) |
| `triggers` | `[post-merge]` | what starts a run: `post-merge` (after `bh work merge` / `finish` lands on the integration branch) and/or `sync` (after `bh hive sync` pulls a hive; needs a cursor from one `bh backfill commits --since-cursor` run) |

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

### bd's own routing keys (`routing.mode`, `routing.default`) {#bd-routing-keys}

These live in each hive's `.beads/config.yaml`, not in bh config. Every bh-managed hive
declares `routing.mode: explicit` and `routing.default: .` so bd's actor-based `auto`
heuristic is never the operative rule. With `work.beads.explicit_routing: true` (off by
default; per-hive or global) `bh hive onboard` sets them when absent and `bh doctor` lists
hives where they are unset or differ. Fix one with
`bh -r <hive> bd config set routing.mode explicit` (and `routing.default .`).
Why: [beads-multi-repo-adoption-adr.md](design/beads-multi-repo-adoption-adr.md) §3; the
routing rule bh uses instead is in
[beads-methodology-divergence-adr.md](design/beads-methodology-divergence-adr.md).

## `work.gate` — gate model and phase profiles

| Key | Default | Values | Effect |
|---|---|---|---|
| `work.gate.model` | `legacy` | `legacy` \| `keys`, per hive or global | `keys` evaluates every selective boundary (including post-land, union and the push-main miss path) per key through its phase profile, with the runtime catalog drift check and a fallback to the phase's `work.validate` command on drift. Rollback: set `legacy`. |
| `work.gate.profiles.<phase>` | unset (`default`, itself `all`) | `all`, a subset name, or a key list | Keys a phase requires under `model: keys`. Phases: `check`, `submit`, `review`, `merge`, `merge-main`, `molecule`, `postland`, `union`, `release`, `push-main`, plus `default`. Every phase accepts project-defined named subsets or key lists; projects own their strictness and policy. Rollback: remove the entry. |
| `work.gate.subsets.<name>` | none | list of key names | Named key subsets a profile may reference. Rollback: remove the entry. |
| `work.gate.carry.high_water` | unset (on under `keys`, off under `legacy`) | bool | Discover carry roots beyond the receipt base. Rollback: `false` restores single-hop sourcing. |
| `work.gate.carry.max_roots` | `3` | int, `0` or more | Newest same-command green runs tried as extra carry roots per key. Rollback: `0` disables that scan. |

Worked example (the operator-approved bh profiles) and the per-field rollback table:
[ATTEST-KEYS.md](ATTEST-KEYS.md#gate-settings-reference).

## `work.attest.version_bump` — carry the parent's verdicts through a version-only bump

A release bump commit changes only the changelog plus the bh version value in `pyproject.toml`,
`uv.lock` and `server.json`, yet every behaviour key selects those files. This opt-in policy lets
the bump reuse the parent's verdicts for the keys you name. Unlike `work.attest.trivial` (a policy
skip that leaves the revision not fully attested), a carry is evidence: it is written to the
ledger as a **carried** verdict (kind `carried`, `source_tree` = the parent tree, and the full
receipt including the judge record), and `attested_green` and `release` accept it like any other
carried key. Keys not named still run (unit, docs and packages observe the version string).

| Key | Default | Values | Effect |
|---|---|---|---|
| `work.attest.version_bump.enabled` | `false` | bool, per hive or global | Master switch. Rollback: `false`. |
| `work.attest.version_bump.keys` | `[]` | list of key names | Behaviour keys whose parent verdict is carried (e.g. `integration`, `stateful`, `demos`, `architecture-contracts`, `root-composition`). Empty disables the policy. |
| `work.attest.version_bump.paths` | `CHANGELOG.md`, `pyproject.toml`, `uv.lock`, `server.json` | fnmatch globs | The path judge: every changed path must match. |

Two judges must agree (the path globs AND a content judge that parses old/new file content:
`[project].version` only; the root package `version` in `uv.lock` only; `version` fields only in
`server.json`; any change to `CHANGELOG.md`), HEAD must have exactly one parent whose tree is the
base, and each carried key needs a qualifying green run at that parent tree. Anything else (a
dependency change, an extra or edited file, unparsable content, a merge commit, a missing or red
parent verdict) takes the normal route. Invalid config degrades to off.

To enable it on a hive (operator decision), in the hive's `work.attest` config:

```toml
[work.attest.version_bump]
enabled = true
keys = ["integration", "stateful", "demos", "architecture-contracts", "root-composition"]
```

## `work.attest.drift_check` — runtime catalog drift guard

HQ `work.attest.keys` is the single catalog source; the drift check guards it against the repo's
recipes at run time.

| Key | Default | Values | Effect |
|---|---|---|---|
| `work.attest.drift_check` | unset | non-blank command | An opaque command run at the evaluated revision's checkout with the live catalog (`name`, `cmd`, `enabled`) as JSON on stdin. Exit 0 is no drift; anything else is drift. Under `work.gate.model: keys`, unset also counts as drift. Rollback: unset it (and use `model: legacy`). |
| `work.attest.drift_check_timeout_seconds` | `60` | float, greater than 0 | A run longer than this counts as drift. |

See [ATTEST-KEYS.md](ATTEST-KEYS.md#runtime-catalog-drift-check).

## `work.gate.push` — per-key acceptance at the push hook

| Key | Default | Values | Effect |
|---|---|---|---|
| `work.gate.push.accept_attested_green` | `false` | bool, per hive or global | Opt in: the `push-main` pre-push hook also accepts the exact pushed tree when every active attest key is current or carried (`selective_validation.attested_green`), printing each key's state. Under `model: keys` a miss runs only the absent keys. An absent or red key, drift or any error falls through to the aggregate lookup and then the full gate; off is byte-identical to before. Rollback: `false`. See [ATTEST-KEYS.md](ATTEST-KEYS.md#accepting-per-key-proof-at-the-push-hook). |

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

## Dolt/bd trigger canary version ranges {#fence-canary-ranges}

The hive write guard's shape depends on Dolt and bd trigger behaviour (ADR condition 9). The
canary (`just fence-canary`) is proved against a **range** of each tool, not an exact version. Both
are test-harness settings read from the process environment; they are not `config.yaml` keys.

| Env var | Default | Notes |
|---|---|---|
| `BH_FENCE_CANARY_DOLT_RANGE` | `>=2.3.5,<2.4` | PEP 440 specifier set for the Dolt version (parsed with `packaging.specifiers`). |
| `BH_FENCE_CANARY_BD_RANGE` | `>=1.3.0,<1.4` | PEP 440 specifier set for the bd version. |

A set-but-empty or malformed value is refused with an error naming the variable; it is never
ignored, clamped, or replaced by the default. The integration canary passes when the installed
`dolt`/`bd` satisfy their range and prints the exact versions it proved. The fast-gate tripwire
(`tests/test_fence_trigger_canary_pin.py`) fails only when a pin in `flake.nix` or
`docker/toolchain-metadata.json` falls outside its range; widen the range deliberately after
`just fence-canary` is green on the new binary. A bump inside the range needs no test edit.
