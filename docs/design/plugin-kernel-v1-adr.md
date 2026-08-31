# ADR: Plugin kernel v1 contracts

> Status: **decided** (2026-08-31). Decision bead: `bh-qw9oi.1`.

## Context and boundary

The current [`beadhive.plugins`](../../src/beadhive/plugins.py) registry combines a Typer app,
an enablement predicate, and nullable callbacks in one dataclass. That seam preserved built-in
integration behavior, but it does not express which integration owns an application capability,
which one merely observes lifecycle work, or which command is only a presentation of behavior
owned elsewhere. Adding more nullable callables would make those distinctions weaker.

The v1 boundary is a plugin kernel contract. Declarative manifests and typed public ports point
inward; integration implementations and transport adapters point toward those contracts; the
bootstrap layer discovers, validates, selects, binds, and starts concrete implementations.
Manifest loading must not import an integration, start a process, read a secret, or mutate a
global registry. This bead ratifies and publishes that contract. Later `bh-qw9oi` beads own the
runtime migration and the temporary `beadhive.plugins` compatibility facade.

The published JSON Schema is
`urn:beadhive:wire-schema:plugin-manifest:1` in wire release `1.3.0`. It is the named,
language-neutral port for manifest producers and consumers. The schema, ADR, release metadata,
and conformance fixtures are independently testable without constructing the CLI or an
integration. Runtime capability protocols and subscribers are concrete implementations behind
that contract; they are deliberately not stored as Python references in JSON.

## Decision summary

| Concern | PluginManifest v1 decision |
| --- | --- |
| Identity | `plugin_id` is a lowercase stable local ID; `plugin_version` is strict three-part SemVer. IDs are independent of Python import paths and distribution names. |
| Compatibility | `compatibility.beadhive` and `compatibility.plugin_kernel` are explicit half-open ranges: `[minimum_inclusive, maximum_exclusive)`. |
| Capability | `capabilities.provides` declares owned, versioned capability IDs. It contains no handler, adapter, or service instance. |
| Lifecycle | `lifecycle.subscriptions` declares typed event observation policy, ordering, timeout, criticality, idempotency, retry, and compensation. It contains no callback. |
| Configuration | The plugin owns exactly `plugins.<plugin_id>`; core owns discovery and owner selection. Legacy namespaces are explicit compatibility aliases. |
| Presentation | `presentation.cli` projects a declared capability to a command path. A projection neither owns the capability nor subscribes to lifecycle. |
| Security | Permissions, executable basenames/ranges, and credential lookup references are declared. Secret values and executable command lines are invalid manifest members. |

## 1. Identity, compatibility, and deterministic artifacts

`manifest_version` is the manifest contract major and is exactly `1`. `plugin_version` versions
one plugin's implementation and metadata. The two compatibility ranges answer different
questions:

- `compatibility.beadhive` constrains the Beadhive application release that may load the plugin;
- `compatibility.plugin_kernel` constrains the plugin-kernel protocol implementation that may
  validate and bind it.

All range endpoints are release SemVer values without prerelease or build suffixes. At load time,
the host version must be greater than or equal to `minimum_inclusive` and less than
`maximum_exclusive`; the minimum must be strictly lower than the maximum. JSON Schema validates
the endpoint syntax and shape. The loader performs the ordered comparison and reports the failed
range before importing implementation code. An incompatible manifest is unavailable, never
partially loaded.

The schema follows the `bh-j4gbx` wire-artifact policy: a stable URN independent of repository
paths, contract major `1`, an immutable SemVer release directory, registration in `index.json`
and `release.json`, shared conformance fixtures, and same-major compatibility checks. Its checked
bytes have an executable digest assertion. A changed schema is therefore an intentional reviewed
artifact change, not serialization-order drift. A compatible v1 addition is published in a new
wire release; a breaking manifest change requires artifact and manifest major 2.

Manifest arrays have semantic identities (`capability.id`, subscription `id`, credential `id`,
permission `id`, and executable `name`). The loader rejects duplicate semantic identities even
when the full JSON objects differ. Diagnostic and generated output sort plugins by `plugin_id`
and nested declarations by semantic identity. Discovery order, mapping insertion order, and
filesystem enumeration never determine rendered bytes or runtime ownership.

## 2. Capability ownership, lifecycle observation, and CLI presentation differ

These are three independent relationships:

| Relationship | Declares | Runtime binding | Does not imply |
| --- | --- | --- | --- |
| Capability ownership | `capabilities.provides[].id` plus `api_version` | One selected provider implements the named application port. | Lifecycle participation or a CLI command. |
| Lifecycle observation | A subscription ID and event with execution policy | Bootstrap binds the ID to a typed subscriber accepting that event's typed context. | Ownership of the event, the affected capability, or the external effect being observed. |
| CLI presentation | A command path referencing a capability ID | The CLI adapter invokes the selected capability port. | A second behavior implementation, provider priority, or lifecycle ordering. |

Capability IDs are lowercase dotted names matching
`^[a-z][a-z0-9-]*(\.[a-z][a-z0-9-]*)+$`. They name behavior, not products, commands, modules,
classes, or transports. The capability's `api_version` changes only when its typed port breaks.
A plugin manifest can be valid while providing no capability, for example a lifecycle-only
observer, but every CLI projection must reference a capability declared by that same plugin in
v1. Cross-plugin presentation delegation is deferred because it would obscure ownership.

The canonical operation catalog remains authoritative for public operation and transport shape.
Plugin CLI declarations are input to the later composition/projection step; they do not create a
parallel operation executor. Bootstrap binds a selected provider to known application ports and
then gives the CLI adapter an explicit binding. Domain and application code do not query the
manifest catalog for a service.

## 3. Capability conflicts fail closed

At most one enabled plugin owns a capability ID and API major in one process. Selection follows
this order:

1. discard manifests that are invalid, incompatible, unavailable on the host, or disabled;
2. use an explicit core-owned selection in `plugin_kernel.capability_owners`, when present;
3. otherwise accept the sole remaining provider; and
4. if multiple providers remain, fail startup/readiness for that capability with all conflicting
   plugin IDs and the selection key needed to resolve it.

There is no last-discovered-wins rule, manifest priority, implicit built-in preference, or
lexicographic winner. An explicit selection must name a discovered, compatible provider that
actually declares the capability; stale or misspelled selections fail validation. Optional
capability conflicts make that capability unavailable and visible in readiness. A conflict for a
capability required by the requested operation blocks that operation. Core startup is blocked
only when the conflicted capability is required for core startup.

Duplicate `plugin_id` values are a discovery error before capability resolution, even if one
copy would otherwise be disabled. Duplicate subscription IDs within a plugin, CLI command paths
after projection, configuration namespaces, permission IDs, executable names, or credential IDs
are likewise errors rather than overwrite opportunities.

## 4. Discovery is deterministic and side-effect free

V1 has two ordered sources:

1. built-in manifest resources from a static Beadhive-owned list; and
2. the reserved Python entry-point group `beadhive.plugins.v1`, sorted by entry-point name and
   distribution identity.

The loader always supports the source abstraction and provenance record. Loading arbitrary
third-party entry points is not required to be enabled in the first v1 implementation. When it is
enabled, an entry point yields manifest bytes or a resource reference only; discovery does not
construct a plugin object. The implementation entry point is resolved only after schema,
compatibility, enablement, conflict, permission, executable, and credential-reference validation.

The loader does not recursively scan repositories, `$PATH`, operator home directories, config
values, or a hosted registry. Built-in status is loader provenance, not a self-asserted privilege
inside a manifest. Built-ins and third parties pass the same manifest validation. Discovery
diagnostics retain source and distribution provenance, but provenance does not alter ownership
selection unless the operator explicitly configures it.

## 5. Lifecycle subscriptions are typed policy, not callbacks

A lifecycle event has a kernel-owned dotted ID and one typed immutable context contract. A
manifest subscription names the event and declares:

- a plugin-local globally qualified subscription ID;
- integer `order`, with ties resolved by `plugin_id` then subscription ID;
- a positive timeout;
- `best-effort` or `blocking` criticality;
- whether idempotency is required;
- bounded attempts and backoff; and
- whether a named compensation action is required.

Bootstrap explicitly binds the subscription ID to an implementation of that event's subscriber
protocol. The context exposes only the event contract and declared ports; it is not a registry,
container, or arbitrary mapping of services. A missing binding, wrong context protocol, retry on
a non-idempotent subscriber, `required` compensation without an action ID, or an action ID when
compensation is `none` is a load-time error.

Best-effort observers preserve the existing warn-and-continue behavior: timeout or failure is
recorded and later observers continue. Blocking subscribers fail the owning lifecycle phase.
External effects that must participate in a transaction use an explicitly typed saga/compensation
port and blocking policy; an observer does not acquire ownership merely by running early. Retries
occur only where idempotency is required and proven by the subscriber contract. Shutdown ordering
is supplied by the lifecycle event definition, not inferred by reversing a global callback list.

## 6. Configuration and sensitive prerequisite ownership

Core owns `plugin_kernel`, including discovery policy, enablement, capability-provider selection,
and kernel diagnostics. A plugin owns exactly `plugins.<plugin_id>` and may publish a schema URN
for that subtree. It must not read or write another plugin's subtree or core's selection state.
Prefix overlap is invalid. New top-level plugin-specific keys are forbidden.

Existing top-level integration keys remain temporary compatibility inputs and appear in
`configuration.legacy_namespaces`. The config adapter maps them into the canonical namespace and
reports conflicts if both spellings disagree; the canonical spelling wins only when values are
equivalent. Listing a legacy namespace documents compatibility, not ownership, and does not let a
new plugin claim an existing key. Removal belongs to the migration bead that inventories callers
and updates the compatibility ledger.

`security.permissions` declares stable permission IDs, whether they are required, and why.
`security.executables` declares only an executable basename, a required flag, and a half-open
version range. It cannot contain argv, shell, installer, path override, or environment mutation.
`security.credentials` declares only an ID, requirement, purpose, and lookup source names such as
an environment-variable name, keyring record, OAuth profile, or file reference. Every nested
object is closed, so `value`, `token`, `password`, `secret`, and other undeclared material are
rejected rather than ignored. Runtime diagnostics may report presence and source kind but must
not copy resolved values back into a manifest, receipt, log, or wire artifact.

## 7. Required-tool and install-catalog reconciliation

`git-workspace` remains the explicit required-tool exception established by `bh-hsus.4`. It is
an unconditional `deps.py` row with `required=ALWAYS`, not an optional PluginManifest provider.
Its `bh plugin git-workspace`-shaped CLI and readiness line remain explicitly mounted through
`gitworkspace_plugin.py`. This ADR does not give the generic plugin kernel authority to gate,
disable, discover, install, or conflict-resolve it. Only the owner of that required-tool policy
may migrate it; until then, later plugin-kernel beads must preserve the exception and its tests.

The host-runtime install catalog planned by `bh-h441d` and PluginManifest answer different
questions. The install catalog owns package attrs, platform availability, required/optional kind,
verification, dependencies, license identity, and vendor/manual delivery. PluginManifest owns
runtime compatibility, capability binding, lifecycle participation, presentation, config, and
sensitive prerequisite declarations. Installation never implies enablement, and enablement never
authorizes installation.

They reconcile on stable IDs:

- an optional install-catalog plugin ID equals `plugin_id`;
- catalog capability IDs equal `capabilities.provides[].id` rather than inventing package IDs;
- executable prerequisites and version ranges in the manifest must be satisfiable by the selected
  catalog entry's required/core closure or its documented external prerequisite; and
- a plugin already shipped in `runtime-core` may have zero additional packages while retaining
  the same plugin and capability IDs.

Mismatch is catalog/manifest drift and fails host provisioning validation before a profile is
switched. The install catalog must not embed PluginManifest credential declarations or values,
and PluginManifest must not duplicate Nix attrs, delivery instructions, license policy, profile
generation, rollback, or package-manager commands. `bh-h441d` remains the owner of provisioning
and install lifecycle.

## Rejected designs

### Arbitrary callback bags

Adding more optional `Callable[..., Any]` fields to the registry leaves contexts, ordering,
failure policy, ownership, idempotency, and compensation implicit. It also makes manifest data
language-specific. V1 uses typed ports/subscribers plus declarative binding IDs.

### A global event bus

A process-wide publish/subscribe bus makes ordering and failure behavior ambient, lets unrelated
code emit lifecycle events, and hides reverse dependencies. V1 lifecycle dispatch belongs to an
explicit bootstrap-constructed coordinator over a finite event catalog and typed contexts.

### Registry as service locator

Looking up handlers, repositories, clients, plugin instances, or adapters by string from
application code reverses the intended dependency direction and defeats isolated tests. The
registry/catalog contains descriptions and validated bindings only. Bootstrap injects selected
ports into consumers; application code never queries the registry for behavior.

## Consequences and non-goals

- Manifest validation, compatibility, discovery diagnostics, and conformance fixtures can run
  without importing Typer, starting external tools, or assembling higher layers.
- Capability providers are replaceable behind named ports; lifecycle observers and CLI adapters
  remain consumers rather than alternate owners.
- Conflict and discovery results are reproducible across machines and filesystem order.
- Security prerequisites are auditable without turning release artifacts into secret stores or
  command-execution payloads.
- The current registry, built-in enablement semantics, CLI commands, and lifecycle behavior do
  not change in this bead. Dynamic third-party loading, package installation, config migration,
  and runtime port implementations remain downstream work bounded by this ADR.
