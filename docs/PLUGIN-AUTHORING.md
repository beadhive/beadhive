# PluginManifest v1 authoring and isolation

Beadhive plugins extend application capabilities without turning the manifest catalog into a
service locator. A plugin publishes data, bootstrap binds concrete implementations to named
ports, lifecycle delivery uses kernel-owned events, and transports project those ports into CLI
or MCP surfaces. The normative contract and rationale live in
[`plugin-kernel-v1-adr.md`](design/plugin-kernel-v1-adr.md).

The executable, deliberately non-shipped example is
[`sample-plugin-v1.json`](../tests/harness/sample-plugin-v1.json), with its provider and observer
in [`sample_plugin_v1.py`](../tests/harness/sample_plugin_v1.py) and the complete discovery,
disablement, binding, timeout, and failure matrix in
[`test_sample_plugin_authoring.py`](../tests/contracts/test_sample_plugin_authoring.py).

## Manifest fields

`manifest_version` is exactly `1`; `plugin_id` is a stable lowercase local ID; and
`plugin_version` is strict three-part SemVer. Both Beadhive and plugin-kernel compatibility are
half-open ranges. Keep their meanings separate: an application release and a kernel protocol can
evolve independently.

`capabilities.provides` declares dotted application capability IDs and API majors. It never
contains Python objects. `lifecycle.subscriptions` names a kernel event, stable plugin-qualified
subscription ID, order, timeout, criticality, idempotency, retry, and compensation policy.
`presentation.cli` maps a capability to a command path; it is presentation metadata, not the
implementation and not capability ownership.

`security.permissions`, `security.executables`, and `security.credentials` declare prerequisites.
Executable entries contain only a basename, requirement flag, and version range. Credential
entries contain lookup source names, never values. Unknown members—including tokens, passwords,
argv, shell fragments, and installer commands—fail closed.

## Configuration

Core owns `plugin_kernel.enabled`, `plugin_kernel.capability_owners`, and the external-entry-point
policy. A plugin owns one subtree, `plugins.<plugin_id>`, named by `configuration.namespace`.
Point `schema_artifact` at a namespaced, versioned schema URN. Existing top-level keys belong in
`legacy_namespaces` only while a compatibility adapter remains; conflicting canonical and legacy
values must be rejected rather than resolved by precedence.

Every accepted config form is validated and immutably snapshotted before a manifest source is
read. In particular, booleans are exact booleans—strings such as `"false"` are invalid—and mutable
caller mappings cannot change discovery after validation.

## Capability ports and composition

Define a narrow, runtime-checkable `Protocol` for each capability. Bootstrap supplies a
`CapabilityKey`, the finite `DiscoveryResult`, and explicit `ProviderBinding` objects to
`bind_application_port`, then injects the returned port into the application consumer. Domain and
application code do not query a plugin registry. Missing, duplicate, or structurally wrong
bindings block only the composition that requires that capability.

## Lifecycle policy and failure isolation

Use only IDs in `EVENTS_BY_ID` and the matching immutable context type. Same-phase order is
`policy.order`, plugin ID, then subscription ID. Best-effort failure or timeout is recorded and
later observers continue. Blocking subscribers fail their owning phase; retries require declared
idempotency; compensation is explicit and runs in reverse completion order.

An invalid, disabled, incompatible, or crashing optional plugin cannot stop core read-only
startup. A critical failure blocks only when its blocking lifecycle phase or capability is
required by that composition. Do not catch a blocking `LifecycleDeliveryError` and silently
downgrade it. Conversely, do not promote an observer failure into a process-wide startup failure.

## CLI projection, telemetry, and redaction

CLI mounting is an outer transport adapter. Keep Typer/FastMCP/Starlette out of the kernel and do
not store a Typer app in `PluginManifest`. Operation-catalog projection remains the authority for
public command and transport shape.

Diagnostics may include plugin ID, capability ID, stable error code, declared source kind, and
distribution identity. Logs, spans, receipts, and manifest artifacts must never include resolved
credential values. Redact config fragments before telemetry, and keep optional OpenTelemetry SDK
imports outside the kernel contract package.

## Testing and compatibility gates

Run the common plugin conformance kit with a fresh subject factory. Each case must receive new
manifest/capability mappings and provider instances. Validate the checked manifest through the
real discovery parser, assert exact declared-to-bound capability equality, and exercise failure
policy separately from adapter integration.

Useful repository commands are:

```text
just test-closure kernel.plugins
just test-plugin <plugin-id>
just test-contracts
just architecture-check
just check
```

Built-in artifacts live under `src/beadhive/kernel/plugins/manifests/`. Their test reconciles IDs,
runtime-core modules, and external executable declarations with `plugin_runtime_catalog.py`.
Discovery does not import that delivery catalog and the catalog contains no Nix attributes or
installer commands. Nix/host package ownership remains with `bh-h441d`.

`git-workspace` is the required-tool exception: it remains an unconditional `deps.py` row and an
explicit plugin-shaped CLI mount. It is not discoverable, disableable, or conflict-resolved as an
optional plugin. The larger Herdr lifecycle extraction is also outside this migration; its current
registration metadata is manifested without transferring that ownership.

## Legacy facade deprecation and removal ledger

| Compatibility surface | Current consumers | Successor | Owner | Removal trigger |
| --- | --- | --- | --- | --- |
| `beadhive.plugins.Plugin` nullable callback dataclass | Built-in `PLUGIN` declarations and downstream integrations constructing the old type | checked manifests, typed capability ports, and `SubscriberBinding` | `bh-qw9oi.4` compatibility facade | all built-ins and known external consumers construct bootstrap bindings directly; compatibility tests pass without instantiating `Plugin`; then remove in a separately reviewed major-compatible deprecation change |
| `beadhive.plugins.registry()` and its lazy integration imports | CLI transport projection plus the facade's typed projection factories | declared built-in manifest source plus explicit bootstrap adapter catalog | `bh-qw9oi.4` compatibility facade | no production caller imports `registry`; CLI consumes operation projection; lazy-import SCC is absent from the architecture checker |
| top-level integration config aliases (`orca`, `hitch`, `herdr`, `observaloop`, `repowise`) | persisted pre-v1 host configs | `plugins.<plugin_id>` | each integration owner | canonical config has shipped for at least 60 days and 30 representative changes with zero alias-related escapes; conflicts already fail closed before removal |

Until those triggers are met, the facade is an owned compatibility boundary, not a second plugin
kernel. It builds one immutable manifest-discovery composition for each host action: canonical
`plugin_kernel.enabled` and the legacy predicate must both permit execution, capability selection
governs every projection, and runtime subscriber IDs, events, and policies come verbatim from the
selected manifest. CLI construction snapshots canonical policy once; a disabled optional plugin
is not mounted, while a pre-setup host with no config retains the legacy static command inventory.
The required `git-workspace` dependency remains mounted outside this optional-plugin snapshot. New
plugins must not add nullable fields, registry queries, or new top-level config keys.

Worktree creation and onboarding likewise compose once at their actual `(config, registry entry)`
action boundary: enumeration, enablement, capability delivery, and before/after observers all
consume that same immutable selection. A mutable legacy predicate or manifest source therefore
cannot split one action into contradictory plugin states.
