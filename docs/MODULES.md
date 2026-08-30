# Beadhive modular architecture

Status: proposed architecture and workstream plan

Evidence date: 2026-08-30 UTC

Repository baseline: `fbc45370292175507d90952952d4458d2fa5a82e`
(`chore(merge): molecule bh-4bhs7`)

## Purpose

Beadhive is preparing its first stable configuration, CLI, MCP, API, plugin, lifecycle, and
telemetry contracts while its implementation is still predominantly a flat Python package. This
document defines the target internal module architecture and the migration sequence for reaching
it without changing product behavior in one large rewrite.

The intended result is a modular monolith with explicit internal boundaries:

- one canonical catalog for operations exposed through supported transports;
- application use cases and domain rules that do not depend on Typer, FastMCP, HTTP, Herdr,
  OpenTelemetry, process execution, or persistence implementations;
- typed plugin capability ports and lifecycle hook points;
- deterministic, versioned, checked-in schema artifacts;
- independently testable capability modules and plugins;
- smaller, evidence-backed test closures for ordinary changes; and
- boundaries that permit future package extraction without requiring separate distributions now.

This is a structural program. It preserves current behavior and public compatibility unless a
later implementation bead explicitly owns a contract change.

## Current structure and evidence

At the named baseline, Beadhive has three installed entry points:

- `bh = beadhive.cli:main`;
- `bh-mcp = beadhive.mcp:main`; and
- `beadhive-gateway = beadhive.remote_gateway_runtime:main`.

The production package contains approximately 177 flat top-level Python modules and 95,000 lines
of code. The test tree contains approximately 343 Python files and 123,000 lines. The principal
composition roots are `cli.py`, `mcp.py`, `host_daemon.py`, and `remote_gateway_runtime.py`.

The exact-tip RepoWise index records 771 files. Static dependency evidence shows six cyclic
strongly connected components spanning 85 production files; the largest contains 65 files,
including config, registry, plugins, runtime, engine, telemetry, work, and worktree concerns.
Moving files before introducing ports would therefore preserve the coupling under new paths.

High fan-in and static test-import blast radius are concentrated in shared registration and
configuration modules:

| File | Production import fan-in | Importing test files |
|---|---:|---:|
| `config.py` | 95 | 129 |
| `registry.py` | 66 | 48 |
| `run.py` | 51 | not separately measured |
| `cli.py` | 65 test dependents | 65 |
| `mcp.py` | 26 dependents | 23 |
| `config_schema.py` | 28 dependents | 19 |
| `otel.py` | 20 production imports | 19 |
| `plugins.py` | 19 dependents | 9 |

These are static graph relationships, not dynamic line coverage. The repository has a historical
coverage baseline, but the current RepoWise index has no verified fresh dynamic per-test coverage
map. Test-closure reductions must therefore be earned through boundary tests and measured impact
data rather than assumed from directory names.

### Existing foundations to preserve

The modularization program builds on, rather than repeats, existing work:

- `work.py`, `config.py`, and `worktree.py` have already been reduced behind compatibility
  facades. The supported import and monkeypatch seams documented in
  `docs/design/structural-quality-baseline.md` remain compatibility constraints.
- The operator has already decided that the operation catalog is canonical. The open
  **Catalog-derived surfaces** molecule (`bh-j4gbx`) owns the initial catalog, wire-schema, CLI,
  and MCP foundation.
- The merged **Release Herdr-managed exact-seat launches** molecule (`bh-4bhs7`) introduced
  provider-independent seat contracts, generic launch profiles, Herdr-specific launch profiles,
  and portable launch receipts.
- `host_daemon.py` already has ordered startup and shutdown phases plus composed lifespan
  components. Those semantics should inform the shared lifecycle model instead of being replaced
  by an unrelated event framework.
- The operator API already publishes a checked-in OpenAPI artifact. It should join the common
  schema release process without forcing every operation onto HTTP.

### The launch boundary as the first reference slice

The launch work merged in `bh-4bhs7` establishes the desired dependency direction:

- Beadhive core owns launch identity, workspace binding, seat policy, durable intent,
  idempotency, completion, and teardown decisions.
- A presentation adapter owns bounded external allocation effects.
- Core imports no Herdr implementation and carries no Herdr Space, tab, or pane fields in its
  provider-independent contracts.
- The accepted transaction is `prepare -> adapter commit -> core commit/abort`.
- Teardown is durable, retryable, generation-fenced, and does not infer successful work
  completion merely from process exit.

The small `seat_contracts.py` module is cohesive and provider-independent. In contrast,
`herdr_plugin.py` is approximately 3,495 logical lines, has 20 outgoing dependencies, high graph
betweenness, and repeated recent fixes. The agent-launch domain should therefore move inward as a
reference capability module while Herdr transport, topology, CLI, and lifecycle details move
outward into an integration adapter.

## Architectural principles

1. **Capability cohesion over layer-only grouping.** Top-level modules represent reasons to
   change—work, planning, worktrees, agents, hives, config, and state. Layering is used inside a
   capability where it creates a real boundary.
2. **Ports before moves.** Break dependency cycles with typed inbound use cases and outbound
   ports before relocating implementation files.
3. **Core contracts do not know transports.** Domain and application code cannot import Typer,
   FastMCP, Starlette, Uvicorn, Herdr clients, OpenTelemetry SDKs, or terminal renderers.
4. **One behavior, projected surfaces.** The operation catalog describes stable application
   operations. CLI, MCP, and HTTP project those operations using transport-specific metadata.
5. **A catalog is not a service locator.** It binds known application handlers at a composition
   root; domain code never queries the catalog to find services.
6. **Typed capabilities over arbitrary callbacks.** A plugin that takes ownership of behavior
   implements an application port. Lifecycle subscribers observe or participate at declared,
   policy-governed phases.
7. **Released schemas are artifacts.** Runtime models may generate them, but checked-in,
   versioned artifacts with compatibility tests define the public contract.
8. **Compatibility facades are deliberate migration tools.** Existing import paths and supported
   test patch points remain forwarding facades until consumers migrate.
9. **No premature distribution split.** Modules remain packages in the existing distribution
   until dependency and test isolation demonstrate that separate packages would help.
10. **Test selection follows dependency evidence.** Directory-based selection is insufficient;
    shared contracts, reverse dependencies, and integration seams must participate in closure.

## Target dependency model

```text
CLI / MCP / operator API / gateway adapters
                    |
          transport projections
                    |
          canonical operation catalog
                    |
           application use cases
                    |
                domain rules
                    |
          declared outbound ports
                    |
 persistence / process / plugin / telemetry adapters
```

Plugin discovery and lifecycle are adjacent to, but not inside, the domain path:

```text
Plugin manifest ----> validation/discovery ----> capability providers
                                            `--> lifecycle subscribers

Capability provider ----implements----> application port
Lifecycle subscriber ---receives------> typed phase/context
```

Allowed dependency direction is inward:

```text
bootstrap -> transports/integrations -> kernel/application -> domain
                                      -> contracts
```

`domain` and `contracts` must not depend on bootstrap, transports, or integrations. Application
packages declare the outbound protocols they need; adapters implement those protocols. Only
bootstrap code selects concrete implementations and assembles registries.

## Proposed project structure

```text
src/beadhive/
  kernel/
    operations/
    plugins/
    lifecycle/
    telemetry/
    schemas/

  modules/
    config/
    hives/
    work/
    planning/
    worktrees/
    agents/
    state/

  adapters/
    cli/
    mcp/
    operator_api/
    gateway/
    persistence/
    telemetry/

  integrations/
    herdr/
    orca/
    observaloop/
    hitch/
    repowise/

  bootstrap/
    cli.py
    mcp.py
    host.py
    gateway.py

tests/
  unit/
    kernel/
    modules/
  contracts/
  adapters/
  plugins/
  integration/
  system/
```

The names describe the target ownership model, not a requirement to move every existing file.
Small, stable modules may remain at compatibility paths while delegating to these packages.

## Structure details

### `kernel/operations`

Owns the canonical declaration and execution mechanics for exposed application operations:

- `OperationSpec` and stable operation IDs;
- the operation catalog and deterministic iteration/order;
- handler binding at composition time;
- command/query and side-effect classification;
- privilege and availability policy;
- idempotency, timeout, and cancellation metadata;
- request and result contract references;
- supported transport projections; and
- the common operation executor used for telemetry, error mapping, and lifecycle envelopes.

An operation entry should contain application meaning, not Typer or FastMCP objects. It should
describe approximately:

- stable ID and contract version;
- summary and deprecation metadata;
- typed request and result contracts;
- command versus query classification;
- side-effect and privilege class;
- application handler binding;
- availability/precondition requirements;
- idempotency and timeout policy;
- telemetry name and redaction policy; and
- the set of allowed transport projections.

The catalog remains data a generator or projection adapter can read. It must not become a
runtime domain dispatcher queried from arbitrary modules.

### `kernel/plugins`

Separates plugin declaration, capability implementation, lifecycle subscription, and transport
presentation. It owns:

- `PluginManifest` and manifest validation;
- Beadhive/plugin API compatibility evaluation;
- built-in and package-entry-point discovery;
- capability identifiers and typed provider registration;
- duplicate/conflict resolution;
- plugin enablement and configuration binding;
- lifecycle subscription declarations; and
- a plugin conformance test kit.

The current frozen `Plugin` dataclass mixes a Typer application, enablement, and eight nullable
callables. It should remain as a compatibility facade while built-ins migrate to manifests and
typed providers.

A plugin manifest v1 should declare:

- manifest schema version;
- stable plugin ID, display name, and plugin version;
- supported Beadhive API range;
- contributed capability IDs;
- configuration-schema references;
- contributed operation IDs;
- lifecycle subscriptions;
- required executables, network access, filesystem access, or credentials;
- telemetry namespace and redaction classification; and
- Python distribution and entry-point metadata.

The manifest contains declarations, never live callables. Discovery may begin with built-in
manifests and later admit external entry points behind compatibility and policy checks.

### `kernel/lifecycle`

Owns typed lifecycle phases and their delivery semantics. It does not introduce a global event
bus. Three kinds of extension are distinct:

1. **Host lifecycle components**: configure/start/ready/drain/stop phases, informed by the
   existing host-daemon startup and shutdown ordering.
2. **Capability strategies**: typed ports such as `WorktreeProvisioner` or `AgentLauncher` that
   may take ownership of one operation.
3. **Lifecycle observers/participants**: typed notifications around hive, worktree, launch, and
   plugin events.

Every hook declares or inherits policy for:

- ordering and dependency constraints;
- maximum duration and cancellation;
- critical versus best-effort failure handling;
- idempotency/retry expectations;
- whether it may reject a prepare phase;
- whether it requires compensation; and
- whether its result becomes durable evidence.

Best-effort observers may preserve the current warn-and-continue behavior. A component that owns
an external side effect uses an explicit prepare/commit/abort or saga contract instead.

Initial lifecycle families should include:

- plugin discovered/validated/configured/started/ready/stopping/stopped;
- hive onboarding/onboarded/retiring/retired;
- worktree prepare/creating/created/removing/removed;
- agent-launch prepare/adapter-commit/core-commit/abort;
- host startup/readiness/drain/shutdown; and
- telemetry flush as the final shutdown phase.

### `kernel/telemetry`

Defines semantic telemetry without importing the OpenTelemetry SDK. It owns:

- `TelemetrySink` or equivalent outbound port;
- stable semantic event names;
- an event-envelope contract;
- correlation and causation fields;
- redaction and bounded-cardinality policy;
- no-op and test-recording implementations; and
- operation-executor instrumentation semantics.

An event envelope v1 should include the event and schema versions, operation/correlation/trace
IDs, timestamp, actor or seat where appropriate, hive/bead identifiers where permitted, outcome,
duration, bounded error classification, plugin ID where relevant, and explicitly classified
attributes. Secrets, prompts, raw environment values, unrestricted paths, and arbitrary plugin
payloads are excluded.

The OpenTelemetry adapter belongs under `adapters/telemetry`; domain and application tests use a
recording sink without an exporter, network, thread, or process dependency.

### `kernel/schemas`

Owns the registry and deterministic build of public schema artifacts:

- artifact identity and version;
- source model or generator;
- canonical serialization and ordering;
- compatibility classification;
- checked-in destination;
- drift and backward-compatibility tests; and
- release inventory generation.

Schema generation must be reproducible and run without starting transports or contacting
external services.

### `modules/config`

Owns configuration models, defaults, resolution, validation, storage ports, migrations, and
plugin-contributed fragments. It must distinguish:

- public configuration schema;
- host/fleet/runtime resolution;
- round-trip YAML persistence;
- environment and command-line overlays;
- secret references rather than secret values; and
- plugin configuration namespaces.

`config.py` and `config_schema.py` remain compatibility surfaces during migration. This module is
an early priority because config has the repository's largest production and test fan-in.

### `modules/hives`

Owns hive identity and application use cases for onboarding, readiness, retirement, and
registration. It declares outbound ports for registries, repository/workspace realization,
dependency probes, and optional lifecycle subscribers. Plugin details do not appear in hive
domain objects.

### `modules/work`

Owns bead-workflow policy and use cases: assignment, claim, scheduling, validation, submission,
review, approval, merge, resume, and abandonment. It depends on explicit ports for bead storage,
worktrees, execution, validation evidence, and identity. Existing `beadhive.work` facade behavior
and patch points remain stable until consumers migrate.

### `modules/planning`

Owns molecule validation, decomposition contracts, dependency-DAG policy, filing, kickoff gates,
verification, and repair. It may consume work read models but does not own dispatch execution.
Plan, report, and triage cycles should be broken with request/result contracts rather than mutual
module imports.

### `modules/worktrees`

Owns workspace binding, branch/worktree identity, creation/removal policy, inventory, status,
cleanup, and merge-related worktree mechanics. It declares a `WorktreeProvisioner` port so native
Git and plugin-mediated provisioning implement the same contract. Existing facade and test patch
points remain supported.

### `modules/agents`

Owns provider-independent seat and launch semantics:

- versioned seat contracts and digests;
- seat/bead policy;
- generic launch profiles and resolved profiles;
- workspace binding;
- prepared launch, receipt, abort, and teardown contracts;
- launch generation and idempotency rules; and
- `AgentLauncher` and process-observation ports.

Provider argument construction may be an adapter strategy, but Herdr Space/tab/pane identity and
client calls do not belong here. The merged launch work makes this the first reference module.

### `modules/state`

Owns durable validation records, state-stream contracts, activity/read projections, and query
models shared by transports. It does not turn the command path into CQRS infrastructure: read
projections are introduced only where existing consumers need replay, aggregation, or independent
availability.

### `adapters/cli`

Projects eligible operations into Typer groups and commands. It owns command paths, aliases,
flags, prompts, terminal rendering, exit codes, and the existing human-output compatibility
contract. An operation may be CLI-only when its privilege or interactivity policy requires it.

Generated registration does not require generated command implementation. Projection adapters
bind generated declarations to application handlers and preserve byte-stable help/output where
the compatibility contract requires it.

### `adapters/mcp`

Projects allowlisted operations into FastMCP tools and resources. It owns MCP descriptions,
resource URIs, notifications, MCP error mapping, and server construction. Privileged operations
are excluded through an allowlist derived from catalog policy, never a denylist maintained in
the transport.

Composite MCP tools must be explicitly declared over catalog operations. They must not bypass the
application layer or silently invent a second operation namespace.

### `adapters/operator_api` and `adapters/gateway`

The operator API projects appropriate operations and read models into authenticated HTTP/OpenAPI.
The remote gateway retains its explicit wire/version boundary. Neither transport is required to
expose every catalog operation. Streaming, session, and authentication concerns remain transport
or host-runtime responsibilities.

### `adapters/persistence`

Contains concrete Dolt, filesystem, YAML, Git, and process-facing implementations of application
ports. Storage DTOs are translated at the adapter boundary rather than leaking command output or
database row shapes into domain rules.

### `integrations/*`

Each optional external integration owns its manifest, config fragment, clients, capability
providers, lifecycle subscribers, CLI additions that cannot be catalog projections, and
integration-specific tests.

`integrations/herdr` should be split into cohesive pieces such as client/transport, topology and
identity mapping, launch capability, lifecycle/teardown, CLI presentation, and manifest. It
implements the provider-independent agent-launch port and returns portable receipts; it does not
own Beadhive seat or work-completion policy.

### `bootstrap/*`

These are the only composition roots. They load configuration, discover approved plugins, select
adapters, bind handlers into the operation catalog, register transport projections, and manage
process lifecycle. Importing domain or application modules must not start a server, discover
plugins, inspect the filesystem, or initialize telemetry.

## Canonical operation and transport projection model

The operation catalog is canonical for operation identity and shape; each transport owns its
presentation details.

| Concern | Canonical owner |
|---|---|
| Operation ID, meaning, request/result schema | operation catalog |
| Application behavior | capability application handler |
| CLI path, aliases, options, prompts, rendering | CLI projection |
| MCP tool/resource name, URI, annotations | MCP projection |
| HTTP path, method, status/auth mapping | operator API projection |
| Privilege and projection eligibility | catalog policy, enforced by projection |
| Cross-cutting execution telemetry/error envelope | operation executor |

Some existing surface commands may be deliberately excluded from the catalog when they are
transport mechanics rather than application operations. Every exclusion must be explicit and
tested so an incomplete migration cannot masquerade as full coverage.

## Schema release inventory

The first official schema release should deterministically generate and check in:

1. Beadhive configuration JSON Schema v1.
2. Plugin manifest JSON Schema v1.
3. Namespaced plugin configuration-fragment schemas.
4. Canonical operation catalog schema and catalog artifact v1.
5. CLI command-structure schema and generated command inventory v1.
6. MCP tool/resource schema and generated surface inventory v1.
7. Operator API OpenAPI v1.
8. Remote gateway wire schema v1.
9. Lifecycle and telemetry event-envelope schemas v1.
10. Seat-contract, launch-profile, workspace-binding, prepared-launch, commit-result,
    launch-receipt, and abort-receipt schemas v1.

Each artifact needs:

- a stable identifier independent of its repository path;
- an explicit version and compatibility rules;
- deterministic canonical JSON serialization;
- a golden-file drift test;
- a breaking-change detector or explicit major-version override;
- examples or conformance fixtures; and
- inclusion in a generated release inventory.

Additive optional fields are normally compatible. Removing or renaming fields, narrowing accepted
values, changing requiredness, changing operation identity, or changing closed unions is breaking
unless the artifact's major version changes. Human CLI output remains a separate compatibility
surface and is not silently changed by schema generation.

## Testing architecture

The current root `tests/conftest.py` contains many autouse fixtures that initialize config,
identity, storage, validation, telemetry, and runtime concerns. That prevents a plugin test from
proving independence from core infrastructure.

The target test topology is:

```text
tests/
  unit/kernel/                  # pure registries, policies, lifecycle, schemas
  unit/modules/<module>/        # module-local domain/application tests
  contracts/                    # ports, public DTOs, schema compatibility
  adapters/<adapter>/           # transport/persistence behavior
  plugins/<plugin>/             # isolated plugin suites + conformance kit
  integration/                  # explicit multi-module/adaptor boundaries
  system/                       # installed CLI/server/full lifecycle
```

Rules:

- Root fixtures are minimal and side-effect free.
- Module and plugin fixtures live with their test subtree and are not autouse globally.
- Domain/application unit tests use in-memory fakes or recording ports, not process mocks spread
  across unrelated modules.
- Every outbound port has reusable contract tests run against important adapters.
- Every plugin runs the common manifest, capability, lifecycle, failure, timeout, and redaction
  conformance suite.
- Transport snapshots protect CLI help, command paths, MCP names/URIs, and released schemas.
- Import-boundary tests reject inward dependencies on transports and integrations.
- Compatibility-facade tests protect old imports and documented monkeypatch seams.

### Test selection and CI progression

The program should not remove the full correctness gate merely because files were moved. CI
selection evolves in verified stages:

1. **Initially:** retain the current full merge/release gate while adding module targets and
   dependency-boundary checks.
2. **During migration:** ordinary commits run changed-module tests, port/schema contracts,
   reverse-dependent tests, and RepoWise/static impacted tests; merge still runs the full suite.
3. **After closure proof:** merge runs affected modules, shared contracts, compatibility tests,
   and a bounded integration/system smoke set. Full cross-module and cross-plugin matrices run on
   a schedule and for releases.
4. **Regression fallback:** any escaped dependency or missed regression expands the relevant
   closure until the boundary or selector is corrected.

Graduating from one stage requires fresh dynamic coverage/impact evidence, stable import-boundary
enforcement, and a measured history showing that selected gates catch relevant failures.

## Compatibility and migration method

Every extraction follows the same incremental loop:

1. Record current public imports, patch points, output/schema behavior, callers, and tests.
2. Add characterization and executable boundary tests.
3. Define inbound use-case and outbound port contracts at the current path.
4. Move one cohesive behavior slice behind the contract.
5. Leave the old module as a forwarding facade where compatibility requires it.
6. Move fixtures and tests into the new module closure.
7. Add or tighten an import-boundary rule.
8. Measure graph fan-in, cycles, health, coverage, and impacted tests at the exact commit.
9. Repeat until the facade contains only intentional compatibility behavior.

A change is not a successful modularization merely because a large file became several files.
Success requires one-way dependencies, a narrower test closure, explicit contracts, and
independent construction in tests.

## Design alternatives scored

Scores are 1 (poor) through 5 (strong). Migration safety scores favor incremental compatibility
over large flag-day moves.

| Design | Capability cohesion | Enforceable dependencies | Test isolation | Migration safety | Future extraction | Total |
|---|---:|---:|---:|---:|---:|---:|
| Hybrid capability modules plus small kernels | 5 | 5 | 5 | 4 | 5 | **24/25** |
| Layer-only `domain/application/adapters` packages | 3 | 4 | 3 | 4 | 4 | 18/25 |
| Plugin-first central kernel | 3 | 3 | 4 | 3 | 4 | 17/25 |
| Separate Python distributions immediately | 5 | 5 | 5 | 1 | 2 | 18/25 |

### Recommended: hybrid capability modules plus small kernels

This approach follows business reasons to change while centralizing only contracts that are truly
cross-cutting. It supports vertical test slices and future extraction without forcing every
capability through a single god package. Its main risk is inconsistency between module interiors;
dependency rules, templates, and architecture tests mitigate that risk.

### Alternative: layer-only packages

A pure `domain/`, `application/`, and `adapters/` split makes dependency direction visually clear,
but each layer would become another broad shared namespace. A change to work behavior would still
span repository-wide layer packages, and independent module test closure would remain weak.

### Alternative: plugin-first central kernel

Centering the architecture on plugins makes integration isolation attractive, but most Beadhive
behavior is not optional integration behavior. It risks turning the plugin registry into a
service locator and forcing core capabilities through extension mechanisms designed for edges.

### Alternative: separate distributions now

Separate packages provide the strongest physical isolation but impose release, versioning,
dependency, editable-install, and contributor overhead before the internal contracts have proven
stable. Internal packages can become distributions later with much lower risk once the boundaries
have measurable independence.

## Revised workstream sequencing

The work should be delivered as multiple dependent epics, not one repository-wide rewrite. Each
epic must leave `main` compatible and independently reviewable.

### Epic 0 — completed provenance: exact-seat launch contracts

**Release Herdr-managed exact-seat launches for Beadhive agents (`bh-4bhs7`)** is closed and
merged. It is not reopened. Its provider-independent contracts and prepare/commit/abort lifecycle
are inputs to the workstream, while its large Herdr adapter identifies the first extraction seam.

### Epic 1 — operation and schema kernel

Replan the open **Catalog-derived surfaces (`bh-j4gbx`)** molecule as the foundation:

- ratify dependency and catalog rules in an ADR;
- establish the schema artifact registry and compatibility gate;
- declare the canonical operation catalog;
- preserve and extend current JSON envelopes;
- derive CLI and MCP registration incrementally; and
- include launch, plugin-manifest, lifecycle, telemetry, API, and gateway artifacts in the overall
  schema inventory without forcing all their implementation into this epic.

This epic must keep the catalog as declarative data over application handlers, not a runtime
service locator.

### Epic 2 — architecture enforcement and test harness

Build the module boundary infrastructure required by every later extraction:

- dependency/import rules;
- minimal root fixtures and module-local fixture conventions;
- port and plugin conformance test kits;
- schema drift test harness;
- compatibility-facade test conventions;
- module test commands and impact mapping; and
- exact-tip structural/coverage evidence ledger.

Existing test-isolation and land-time attestation work must be reconciled rather than duplicated.

### Epic 3 — plugin kernel and lifecycle model

Replace the mixed plugin registry with:

- manifest v1 and compatibility validation;
- built-in discovery and future package-entry-point seam;
- typed capability registration;
- typed lifecycle subscriptions with explicit failure semantics;
- host lifecycle alignment; and
- compatibility adapters for existing plugins.

Dynamic third-party loading is not required for the first slice; the manifest and host contracts
must nevertheless make it possible without redesign.

### Epic 4 — agent module and Herdr reference extraction

Use the merged launch work as the first complete vertical module:

- move seat/launch/workspace/receipt policy into `modules/agents`;
- define the `AgentLauncher` and observation/teardown ports;
- split Herdr client, topology, launch, lifecycle, and CLI responsibilities;
- implement the Herdr manifest/capability adapter;
- publish conformance fixtures for the versioned launch transaction; and
- prove the Herdr plugin tests run without core storage/runtime fixtures.

This epic depends on the plugin/lifecycle kernel and the shared testing harness.

### Epic 5 — configuration module and plugin schema fragments

Extract configuration models, resolution, validation, stores, and schema generation behind the
existing facade. Replace hard-coded plugin top-level sections with namespaced manifest-declared
fragments. Reconcile the existing schema-derived config-validation molecule before filing
overlapping implementation.

This is the highest fan-in extraction and should occur only after boundary enforcement is active.

### Epic 6 — capability-module migration

Migrate remaining capabilities in dependency-aware slices:

1. hives and readiness/onboarding lifecycle;
2. worktrees and the provisioning port;
3. work lifecycle and execution/validation ports;
4. planning and its report/triage contracts; and
5. state/read projections and validation records.

Each slice breaks cycles before moving files, preserves compatibility facades, and demonstrates a
narrower test closure. Mutation-heavy areas wait for overlapping live bug/feature work to land.

### Epic 7 — transport composition completion

Complete projection of eligible operations across CLI, MCP, operator API, and gateway boundaries:

- shrink entry-point files to composition roots;
- remove manual duplicate registration;
- publish deterministic surface inventories;
- enforce privilege allowlists and transport exclusions;
- maintain human-output and stdio/server compatibility; and
- document the supported extension surface.

This builds on `bh-j4gbx`; it does not replace that epic's initial CLI/MCP work.

### Epic 8 — telemetry readiness and official schema release

Finish the semantic telemetry port and OpenTelemetry adapter, instrument the common operation and
lifecycle execution paths, enforce redaction/cardinality policy, and publish the complete v1
schema bundle with compatibility reports and release notes.

### Epic 9 — selective-test closure graduation

Use accumulated import, coverage, impacted-test, and escaped-regression evidence to graduate CI:

- certify module and plugin closures;
- enable affected-module merge gates;
- retain shared contract and system smoke tests;
- schedule full cross-module/plugin suites; and
- define automatic fallback when selection confidence degrades.

This is deliberately last. Faster CI is an outcome of real modular boundaries, not a path-filter
promise made before those boundaries exist.

## Workstream dependency overview

```text
bh-4bhs7 (merged reference)
          |
          v
bh-j4gbx operation/schema kernel
          |
          v
architecture enforcement + test harness
          |
          v
plugin kernel + lifecycle
       /       \
      v         v
agents/Herdr   config/plugin schemas
       \       /
        v     v
  capability-module migration
          |
          v
 transport composition completion
          |
          v
 telemetry + official schema release
          |
          v
 selective-test closure graduation
```

Some implementation inside adjacent epics may proceed in parallel after their shared dependency
is complete, but their integration order remains explicit. Cross-epic dependencies must be
recorded in beads so a dispatcher cannot treat the workstream as unrelated backlog.

## Success criteria

The program is complete when:

- domain/application packages have no imports from transport or integration packages;
- the operation catalog is the single declaration of exposed operation identity and shape;
- CLI, MCP, API, and gateway surface inventories are deterministic and drift-tested;
- every optional integration has a versioned manifest and isolated conformance suite;
- lifecycle hooks have typed context and explicit ordering/failure semantics;
- configuration and all official contract families publish versioned schema artifacts;
- Herdr implements an agent-launch port without leaking Herdr identity into core contracts;
- composition roots contain wiring rather than domain behavior;
- compatibility facades have an explicit consumer/removal ledger;
- the large cyclic component is materially reduced and no new forbidden cycles are introduced;
- plugins and capability modules can run unit/contract tests without root runtime fixtures; and
- selected CI closures are backed by current dependency and coverage evidence, with full-suite
  release protection retained.

## Explicit non-goals

- Splitting Beadhive into microservices.
- Publishing every internal module as a separate Python distribution.
- Exposing every operation on every transport.
- Introducing a global event bus or generalized CQRS framework.
- Moving product behavior into the operation catalog.
- Allowing arbitrary plugin callbacks to bypass application policies.
- Rewriting all compatibility imports in one release.
- Claiming that path-based tests alone make the full suite unnecessary.
