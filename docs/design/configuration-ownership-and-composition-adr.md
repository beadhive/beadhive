# Configuration ownership and fragment composition

Status: accepted

Date: 2026-08-31

Decision owners: configuration module workstream `bh-18hud`

## Context

Beadhive's configuration behavior is spread across the broad `beadhive.config` and
`beadhive.config_schema` surfaces, specialized typed getters, YAML persistence, migrations,
runtime environment overlays, fleet and host policy, secret handling, and hard-coded plugin
sections. This ADR freezes the current compatibility boundary before those responsibilities move
into an independently testable configuration module.

The extraction is behavior-preserving. Existing callers keep their import paths and supported
monkeypatch points until the migration ledger proves they have moved. Checked schemas, persisted
YAML, environment behavior, diagnostics, and refusal paths are public contracts rather than
implementation details.

## Decision

Configuration becomes one capability with separate inward-facing responsibilities:

1. public contract models and checked schema artifacts;
2. default construction and deterministic layer resolution;
3. fleet, host, runtime, and environment overlay policy;
4. YAML document persistence and round-trip preservation;
5. versioned migrations and future-version refusal;
6. secret references, redaction, and diagnostic classification; and
7. validated plugin fragment declarations composed under plugin-owned namespaces.

The allowed dependency direction is:

```text
CLI / bootstrap / plugins -> configuration application API -> typed contracts
                                      |
                                      v
                         storage / environment adapters
```

Consumers receive immutable typed settings or narrow read/write ports. They do not reach through
the module to YAML nodes, environment readers, migration internals, or plugin discovery.

## Ownership matrix

The current owner is named so that extraction does not turn a facade into a second source of
truth. The target owner is the package or port that dependent beads create.

| Concern | Current authority | Target authority and implementation owner |
| --- | --- | --- |
| Public schema and version | `config_schema.BeadhiveConfig`, `SCHEMA_VERSION`, and schema introspection | `modules/config` contract models plus checked artifact registry; `bh-18hud.2` |
| Typed models | `_Section` models in `config_schema.py` | immutable contracts under `modules/config`; `bh-18hud.2` |
| Defaults and aliases | Pydantic field declarations and `SettingsConfigDict` | the same canonical model metadata, never a copied table; `bh-18hud.2` |
| Fleet/host classification | `config_partition.py` | pure resolution policy owned by `modules/config`; `bh-18hud.3` |
| Fleet and host documents | `config_store.load`, `load_fleet`, `load_host`, and `deep_merge` | explicit document-reader ports and pure resolver; `bh-18hud.3` |
| Runtime overrides | the CLI/bootstrap operation that accepts the override | explicit, typed invocation input to the resolver; `bh-18hud.3` does not make it persistent |
| Environment overlays | `_Env`/`config_paths.env`, specialized getters, and Pydantic settings sources | an allowlisted environment-source adapter; `bh-18hud.3` |
| YAML persistence | `config_store.py` plus ruamel round-trip nodes | a YAML store implementing load/save/edit ports; `bh-18hud.3` |
| Migrations | `config_policy.py`, `config_split_migration.py`, and `home_migration.py` | versioned migration application service over the store port; `bh-18hud.3` |
| Secrets | `credentials.py` and plugin-manifest credential references | credential adapter outside config; config owns references and redaction classification only |
| Plugin namespace declaration | validated `PluginManifest.configuration_*` fields | manifest contract remains authoritative |
| Plugin fragment composition | not implemented; legacy sections are core model fields | deterministic config composer; `bh-18hud.4` |
| Compatibility facades | `beadhive.config` and `beadhive.config_schema` | forwarding-only ownership through `bh-18hud.5`, evidenced by `bh-18hud.6` |

## Layering and source order

There is no single unqualified “config wins” rule. Resolution has four explicit stages, in this
order, and provenance records the winning stage and source path:

1. Load the fleet document, then the host document. `config_store.deep_merge(fleet, host)` is a
   recursive mapping merge and replaces scalar/list leaves. A host value may supersede a fleet
   value only when `config_partition.partition_of(path) == HOST` or the path is in
   `FLEET_HOST_OVERRIDE_ALLOWLIST`. That allowlist is empty at this decision. Merely repeating a
   fleet-only value in the host document is still a forbidden override and raises `ConfigError`.
2. Resolve contextual per-hive values. `config.layered` is exactly
   `entry[section][key] > merged global config[section][key] > declared default`. A contextual
   entry does not become persisted host or fleet data.
3. Apply a declared environment overlay. For the typed model the order is
   `BH_* environment > already-merged YAML constructor data > dotenv > file-secret source > model
   default`. Specialized getters retain their documented aliases; for example `harness_name` is
   `BH_HARNESS > per-hive entry > merged global config > "claude"`. `BH_WORKTREES` remains a
   transport-only path alias and is filtered out of the structured Pydantic source.
4. Apply an explicit invocation override only at a command/application boundary that declares
   one. It is highest for that invocation, is never written back implicitly, and cannot bypass a
   fleet-only, security, privilege, or schema-version refusal.

Defaults fill absence only. Empty-string and sentinel normalization remain field-specific typed
policy; a generic merge must not reinterpret them. The resolver returns typed provenance such as
`default`, `fleet`, `host`, `hive`, `environment`, or `runtime`, plus the source key. It never
stores the value itself in provenance.

### Unknown, future, and round-trip behavior

The current compatibility surfaces deliberately have different jobs and must not be collapsed:

- raw ruamel load/save/edit preserves mapping order, comments, flow style, permissions, and
  unknown content. `bh config set` warns but writes an unknown top-level section; this is the
  forward-compatible editor contract in
  `test_config_dotted.py::test_unknown_top_level_section_warns_not_rejects`;
- typed validation is strict. `BeadhiveConfig` forbids unknown top-level and nested members, and
  `config_validate.validate_config` reports them as errors with schema-derived suggestions; and
- a schema version newer than `SCHEMA_VERSION` is an error at every boundary that claims a typed,
  validated view. Such a document may be loaded only as an opaque round-trip document; it is not
  resolved, migrated, composed, or mutated by the new module. The existing proof is
  `test_config_validate.py::test_newer_schema_version_is_an_error`.

Load and validation therefore remain separate ports. Unknown core keys never become active typed
settings merely because the round-trip store retained them. A failed validation or migration
must leave the source bytes unchanged. Atomic writes retain current file mode and use fsync plus
same-directory replacement; dotted edits retain the per-file thread/process transaction lock.

## Compatibility and migration ledger

The `beadhive.config` and `beadhive.config_schema` modules remain deliberate forwarding facades.
Removal requires consumer inventory, replacement contract coverage, no supported monkeypatch
dependents, and a separately reviewed compatibility decision.

| Existing surface | Compatibility requirement and successor | Migration owner and removal gate |
| --- | --- | --- |
| `config` constants/errors (`BINARY_*`, scope/provenance names, `ConfigError`, `KNOWN_SECTIONS`) | names remain importable; canonical facts forward from contracts/policy | `bh-18hud.5`; remove only after no production or supported external import and an explicit deprecation decision |
| `config` paths/assets (`home`, `config_path`, `fleet_path`, `*_dir`, templates/assets) | byte-compatible results; forward to injected path/source ports | `bh-18hud.5`; all callers migrated and path patch-point tests green |
| `config` storage/layers (`load_host`, `load_fleet`, `load`, `save`, `save_fleet`, provenance/reconcile helpers) | old-module collaborator lookup remains patchable; result/error/YAML behavior unchanged | `bh-18hud.3` implements, `bh-18hud.5` migrates; no old-name patches/callers before removal |
| `config` dotted editing (`get_value`, `set_value`, `unset_value`, coercion/validation) | payload keys, warning/error levels, scope, and transaction behavior remain stable | `bh-18hud.3` implements; CLI/MCP and facade contract tests must use successor before removal |
| `config` typed getters installed from services/work/release modules | all existing names and call signatures remain on the facade; dynamic binding continues to honor `config.load` and other patches | `bh-18hud.5`; capability-batched caller inventory is empty and reverse-dependent closures pass |
| `config_schema` public types (`BeadhiveConfig`, `ManagedRepoEntry`, `RoutingTierConfig`, `ReleaseConfig`, other `_Section` subclasses) | forwarding imports preserve type/validation identity | `bh-18hud.2`; all known callers use canonical contracts before facade removal |
| `config_schema` metadata APIs (`SCHEMA_VERSION`, `iter_schema_fields`, `known_keys`, `suggest_key`, `field_default`, `literal_choices`) | deterministic results derive from canonical models and aliases | `bh-18hud.2`; official schema/legacy-row drift tests and `bh-1h9h` reconciliation pass |
| legacy plugin keys (`orca`, `observaloop`, `hitch`, `herdr`, `repowise`) | read as aliases of `plugins.<plugin_id>`; equivalent dual values collapse, disagreement refuses | `bh-18hud.4`; removal additionally needs the plugin-kernel 60-day/30-change/zero-escape gate |

Supported monkeypatch seams are compatibility, not an invitation to add more globals. At minimum
the move preserves lookup through `beadhive.config` for `home`, `config_path`, `fleet_path`,
`load_host`, `load_fleet`, `_reject_fleet_overrides`, `load`, `save`, `save_fleet`, `_yaml`, and
typed getters patched by their consuming capability. The typed service modules remain explicitly
bound to the facade until consumers migrate. Executable anchors are:

- `tests/test_structural_facade_contracts.py::test_config_load_facade_executes_layer_patch_points_and_preserves_precedence`;
- `tests/test_config_boundaries.py::test_facade_uses_named_path_and_store_modules`;
- `tests/test_config_boundaries.py::test_typed_services_are_explicitly_bound_to_the_patchable_facade`;
- `tests/test_config_boundaries.py::test_atomic_save_failure_preserves_original_bytes`; and
- the config CLI/MCP, schema, partition, fleet-merge, scope, dotted-edit, and reverse-dependent
  closures recorded by `bh-18hud.6`.

A facade may be deleted only after `bh-18hud.5` produces an exact empty caller/patch ledger,
`bh-18hud.6` proves the replacement closure and import graph, the compatibility window above has
elapsed where applicable, and a separate reviewed change authorizes deletion. Directory movement
or a successful internal test suite alone is never a removal gate.

## Plugin fragment composition

This ADR extends, and does not replace, `plugin-kernel-v1-adr.md`:

- a manifest owns exactly `plugins.<plugin_id>`; the namespace must equal its validated stable
  `plugin_id`, and prefix overlap is invalid;
- `configuration.schema_artifact` identifies a checked, immutable fragment schema whose artifact
  major is the fragment contract major. The fragment schema, defaults, and examples are published
  without importing plugin runtime code;
- discovery validates manifests first. Publication inventory is deterministic over all valid,
  compatible discovered manifests. Runtime activation then selects only installed/available and
  enabled plugins. Both orders are canonical-namespace byte order; order grants no precedence
  because namespaces are disjoint; and
- provenance for every composed field includes plugin ID/version, manifest source provenance,
  schema artifact ID and digest, fragment major, persisted spelling (canonical or legacy), and
  configuration source layer. It contains no value.

Conflict and failure policy is fail closed:

| Case | Decision |
| --- | --- |
| Duplicate plugin ID, canonical namespace, or namespace prefix | discovery/composition error; no winner and no last-discovered precedence |
| Missing/unregistered schema artifact for a known configured plugin | validation error for that subtree; preserve bytes, do not activate |
| Unsupported fragment major or incompatible manifest/plugin/kernel range | plugin unavailable with a version diagnostic; preserve bytes, do not activate |
| Invalid fragment default or configured value | validation error naming owner/path/code without printing the value |
| Canonical and legacy values are structurally equivalent | project one canonical immutable snapshot and record both source paths |
| Canonical and legacy values disagree | conflict error; neither spelling wins and the plugin does not activate |
| Known but disabled plugin | validate when its artifact is available, preserve config, mark inactive, perform no effects |
| Known but unavailable/uninstalled plugin | preserve config and report unavailable; do not import runtime or execute it |
| Unknown `plugins.<id>` namespace | preserve as opaque inactive data with a diagnostic; never expose it as typed or executable config |
| Plugin attempts a core key, another namespace, or secret value in its manifest | reject the manifest before source access or activation |

Enablement remains core policy in `plugin_kernel.enabled`; a plugin cannot self-enable through its
fragment. During legacy migration, the canonical enablement decision and the integration's
existing predicate must both permit execution. Disabling or uninstalling a plugin never deletes
its persisted subtree. Garbage collection requires a separate explicit operation that names the
namespace and proves the plugin is absent; it is not part of load, validation, or discovery.

## Secret-safe diagnostics

Config and plugin manifests contain credential references, never credential values. Lookup and
authentication remain owned by `credentials.py` or another injected credential adapter at point
of use. Environment-variable names, keyring service names, OAuth profile names, and file-reference
paths may be declared only where the contract allows them; their contents may not enter the typed
config snapshot.

Diagnostics may contain a stable problem code, redacted dotted path, contract owner, source layer,
schema/fragment version, plugin ID, artifact/provenance identity, expected type, and presence
boolean. They must not contain:

- resolved tokens, passwords, headers, credential files, keychain results, or environment values;
- raw YAML subtrees or a value's `repr` when the field is secret-classified;
- manifest bytes, exception strings, subprocess output, or validation input that can embed a
  secret; or
- telemetry attributes derived from a secret value, even when hashed.

Redaction is schema/credential-classification driven and happens before formatting, logging,
telemetry, or aggregation. Nested and plugin-contributed fields inherit classification from their
fragment schema. Unknown opaque plugin data is never rendered. Tests use canary values and assert
their absence from human output, JSON diagnostics, logs, and telemetry.

## Reconciliation and deferrals

`bh-1h9h — Config validation should derive from the schema, not restate it` is open at this ADR's
source baseline. It remains the sole owner of schema-derived `config.KNOWN_SECTIONS` and alias
policy; this ADR does not implement or copy that fix. When it lands:

1. `bh-18hud.2 — Extract canonical config models and deterministic JSON Schema artifacts` consumes
   the landed derivation by moving the authoritative models/alias metadata inward and forwarding
   the derived section inventory through `beadhive.config`;
2. `bh-18hud.4 — Compose namespaced plugin configuration fragments from manifests` consumes that
   inventory to distinguish core, canonical plugin, legacy alias, and unknown namespaces; and
3. `bh-18hud.5 — Migrate config consumers behind typed settings and preserve compatibility
   facades` retains the old warning/write contract until its compatibility ledger authorizes a
   change.

If `bh-1h9h` is still unlanded when `bh-18hud.2` starts, that bead is blocked at the
`KNOWN_SECTIONS`/alias movement: it must replan or integrate the owning change, not independently
derive a competing list. Its exact regression proofs must include the currently missed
`git_workspace`, `orca`, and `hitch` sections plus accepted aliases and the existing unknown-write
warning.

The remaining implementation is deliberately split:

- `bh-18hud.2` owns models, defaults/aliases, official JSON Schema, deterministic artifact
  publication, and the legacy schema CLI projection;
- `bh-18hud.3 — Extract configuration resolution, overlay policy, and persistence ports` owns the
  pure resolver, typed provenance, environment/runtime inputs, YAML ports, migrations, atomicity,
  thread safety, future-version refusal, and secret-safe resolution diagnostics;
- `bh-18hud.4` owns fragment schemas, deterministic composition, conflicts, enablement,
  unavailable/unknown retention, and legacy plugin aliases;
- `bh-18hud.5` owns capability-batched caller migration and the exact remaining facade ledger; and
- `bh-18hud.6 — Prove isolated config tests, compatibility, and reduced blast radius` owns final
  isolation, graph/fan-in, coverage, timing, closure, and full-gate evidence.

This ADR does not authorize physical extraction, schema publication changes, runtime composition,
caller rewrites, removal of `config`/`config_schema`, removal of legacy aliases, deletion of
unknown YAML, weakening future-version refusal, secret exposure, or plugin-namespace cleanup.
Each happens only in its named bead and behind the gates above.

## Boundary assessment and alternatives

The configuration capability scores **13/14** against the modularization boundary rubric:

| Signal | Score | Evidence |
| --- | ---: | --- |
| Cohesive responsibility | 2 | one typed configuration lifecycle from declaration through resolution and persistence |
| Coupling pressure | 1 | current facade has high fan-in, so compatibility forwarding is required during migration |
| Side-effect isolation | 2 | YAML, environment, and credential access fit explicit adapters around a pure resolver |
| Port clarity | 2 | model registry, document store, resolver, migration, and fragment composer are narrow ports |
| Replacement seam | 2 | memory documents and synthetic environment/fragment sources can replace production adapters |
| Dependency direction | 2 | consumers depend inward on contracts; adapters and plugins cannot be imported by core policy |
| Independent closure | 2 | direct config tests and declared reverse-dependent closures can run without plugin runtimes |

A store-only extraction was rejected because it would leave precedence, schema, and migration
policy duplicated across the facade and callers. A plugin-only config package was rejected
because fragment composition is an extension of the same core namespace and provenance rules.
Keeping the flat modules permanently was rejected because their fan-in and patch-driven service
installation prevent an independently testable boundary. Publishing plugin fragments as new
nullable core-model fields was rejected because it transfers plugin ownership into core and
requires core releases for plugin evolution. Discovery priority or last-writer-wins conflict
resolution was rejected because filesystem/install order cannot confer configuration authority.

## Validation contract for the implementation

The cadence is strict because configuration is the repository's highest-fan-in boundary and the
decision includes persistence, secrets, public schema, and plugin activation. Each implementation
transition runs its direct contract tests and reverse-dependent closure; every bead still runs
the repository's configured full gate.

This ADR is anchored at source revision `5e4a76700e85b4bdda97640eeec5c2bbac632b46`.
Current executable evidence includes:

- `tests/test_config_partition.py` and `tests/test_config_fleet_merge.py` for classification,
  forbidden overrides, deep merge, source immutability, and missing-layer behavior;
- `tests/test_config_schema.py`, `tests/test_config_validate.py`, and
  `tests/test_config_schema_drift.py` for defaults, environment precedence, unknown keys, schema
  version refusal, writer/schema agreement, and schema CLI behavior;
- `tests/test_config_dotted.py`, `tests/test_config_scope.py`,
  `tests/test_config_split_migration.py`, and `tests/test_config_boundaries.py` for warning/write,
  YAML round trips, atomicity, locking, scoped edits, provenance, and facade delegation; and
- `tests/contracts/test_plugin_discovery_contract.py`,
  `tests/contracts/test_builtin_plugin_manifests.py`, and plugin-kernel unit tests for canonical
  namespaces, legacy aliases, schema artifact identities, deterministic discovery, and fail-closed
  manifest validation.

## Consequences

The configuration module has one-way dependencies, deterministic composition, explicit
provenance, and a credible isolated test boundary. Compatibility costs remain visible in the
facades and migration ledger instead of being hidden in file moves. Later beads can move one
responsibility at a time while comparing behavior with this contract.
