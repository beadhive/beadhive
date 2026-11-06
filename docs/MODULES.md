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

At the named historical baseline, Beadhive had three installed entry points:

- `bh = beadhive.cli:main`;
- `bh-mcp = beadhive.mcp:main`; and
- `beadhive-gateway = beadhive.remote_gateway_runtime:main`.

That list is immutable evidence for revision `739349806ead27c94282219b1befb796ba73b583`,
not the current command surface. Current source installs
`beadhive-frame-bridge = beadhive.bootstrap.frame_bridge:main` for the core-owned per-frame
process. It installs no `beadhive-gateway` console command or other legacy alias. The
`beadhive-gateway` name now belongs to the multi-frame Beadhive Gateway authored in the sibling
repository; core's Frame Bridge projects one host daemon into the Gateway-owned wire contract.

The Transport composition closeout (`bh-3qkmk.5`) routes the four current console scripts
through `beadhive.bootstrap.{cli,mcp,host,frame_bridge}:main`. The historical modules remain
compatibility/runtime adapters behind exact exception-ledger edges until their patch inventories
permit physical relocation; reusable production code has no dependency back into bootstrap.

At that historical baseline, the production package contains approximately 177 flat top-level
Python modules and 95,000 lines of code. The test tree contains approximately 343 Python files and
123,000 lines. Its principal composition roots are `cli.py`, `mcp.py`, `host_daemon.py`, and
`remote_gateway_runtime.py`.

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

### Landed `packages/*` boundary

The historical recommendation below to defer distribution splits has one deliberate landed
extension: in-repo build plugins can live as separate distributions in `packages/*`.
`src/beadhive` still contains core contracts, policy, and composition roots; it does not
statically import package implementations. The uv workspace installs package distributions
editably, while each package has its own `pyproject.toml`, `src/`, `tests/`, `justfile`, and
three recursive BUILD files. Start with [`packages/_template`](../packages/_template) and
follow [PLUGIN-AUTHORING.md](PLUGIN-AUTHORING.md) for the full authoring procedure.

The enforced direction is `packages/*` to public `beadhive.kernel.*.contracts`,
`beadhive.modules.*.contracts`, or `beadhive.testing`; `src/beadhive` to package code is a
lazy bootstrap import after plugin manifest selection. `scripts/check_package_imports.py`
checks both sides. First-party manifest JSON in `src/beadhive/kernel/plugins/manifests/`
names the plugin and capabilities; `bootstrap/impact.py` and `bootstrap/build_verify.py`
provide selected runtime bindings. Package tests run in Pants sandboxes under the shared
`packages` attest key, separate from the root native pytest partition.

`beadhive-pants` is the first example. It implements `build.impact` and `build.verify`, owns
the `bh plugin pants` command tree, and keeps legacy `scripts/pants_*.py` paths as shims.
This extraction is limited to build plugins; the capability modules under
`src/beadhive/modules/` remain inside the core distribution.

*2026-10-30 amendment (planned, workstream `bh-pu6n6`):* the backfill platform adds three
library packages rather than a `src/beadhive/modules/backfill` module: `beadlog`,
`beadlog-sources` and `beadhive-backfill`. Root keeps only the bd loader, `bh backfill` CLI,
config and health wiring. See [`backfill-platform-adr.md`](design/backfill-platform-adr.md).
The three packages are scaffolded (`bh-lycrj`): `beadlog` carries the format domain (ids, merge,
fold, diff, manifest); the other two carry contracts only so far.

> **2026-09-27 amendment.** The direction above (`packages/*` depends on root; root imports
> package code only lazily) describes **plugin packages** only. A second class, **library
> packages** (`beadhive-plugins`, `beadhive-worktrees`, `beadhive-beads-client`, and the
> already-landed `beadhive-core`), inverts it: a library package never imports `beadhive` at
> all, and root (and other packages, including plugin packages) may import it statically
> through its public `__all__` surface. `packages/_template` is a library package by default
> (no manifest); adding a `plugin.json` and catalog entry is the one step that makes a copy a
> plugin package instead. `scripts/check_package_imports.py` now enforces both classes by
> manifest presence. See
> [`package-class-library-vs-plugin-adr.md`](design/package-class-library-vs-plugin-adr.md)
> for the full decision, its 2026-09-27 amendment (`bh-xh8ku.1`), including the enforcement
> rule and why the lazy-import rule applies only to plugin packages.

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
   *2026-09-27 amendment:* this demonstration has now been made for two modules — the plugin
   capability/lifecycle contracts and the worktree seam — which extract into the **library**
   package class (`beadhive-plugins`, `beadhive-worktrees`) defined in
   [`package-class-library-vs-plugin-adr.md`](design/package-class-library-vs-plugin-adr.md).
   The principle still holds as a default; it is not overturned, only rebutted for these two
   named cases ahead of the API-first replacement (`bh-bwnys`, `bh-sy36q`).
   *2026-10-30 amendment:* rebutted a third time, for the backfill platform. It ships as the
   library packages `beadlog` (format; stdlib only), `beadlog-sources` (BeadHive-free
   producers) and `beadhive-backfill` (hive apply envelope and policy passes), because the
   format exists for external bead-store loaders and the producers are useful without
   BeadHive. See [`backfill-platform-adr.md`](design/backfill-platform-adr.md) (workstream
   `bh-pu6n6`).
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

Plugin authors use [PluginManifest v1 authoring and isolation](PLUGIN-AUTHORING.md) for the
checked artifact, typed capability, lifecycle, redaction, testing, and deprecation contract.

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
2. **Capability strategies**: typed ports such as the `worktree.manager` slot or `AgentLauncher`
   that own one operation, selected explicitly (never raced in registry order).
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

The implemented public boundary is `beadhive.modules.hives.HiveLifecycleService`, with typed
request/result contracts and `HiveRegistry`, `WorkspaceRealizer`, `DependencyProbe`, and
`LifecyclePublisher` ports. Current CLI and MCP commands share the production composition in
`beadhive.hive_services`; legacy imports and monkeypatch seams remain governed by
`docs/design/hives-compatibility-removal-ledger.md`.

### `modules/work`

Owns build-system-agnostic impact resolution for the Attested Green validation ledger: the
`AttestKey` / `ImpactRequest` / `ImpactReceipt` domain, the `ImpactResolver` / `ImpactBackend` /
`TreeDiffPort` ports, and the fail-closed resolver policy (`contracts/impact_resolution.py`).

Bead-workflow policy no longer lives here. The beadhive-core cutover (bh-sy36q.6) deleted the
callback-shaped `WorkLifecycleService`, its `BeadStore` / `ExecutionPort` /
`ValidationEvidenceStore` / `IdentityProvider` / `WorkNotifier` ports, its request/result DTOs, and
the `beadhive.work_services` composition: every one was a pass-through over the shell's own
`impl_*` functions. Assignment, claim, resume, abandon, ready/claim-next, schedule, dispatch reads,
approve and bounce are `beadhive_core` handlers (`packages/beadhive-core`) composed at
`beadhive.beads_routing`; check, submit, review and merge are called directly by the
`beadhive.work` facade.

### `modules/planning` (retired)

Deleted in the beadhive-core cutover (bh-sy36q.6). Molecule filing and the dependency-DAG policy
(`MoleculeGraph`) are `beadhive_core.planning`, composed by `beadhive.plan_filing`; validation,
verification, kickoff approval and repair are plain functions in `beadhive.plan`. The
`module.planning` test closure is declared `absent`.

### `modules/tracker`

Owns the tracker-neutral **bead tracker adapter port** (`tracker-adapter-port-adr.md` section 3):
`beadhive.modules.tracker.contracts` (records, the error taxonomy, the required `TrackerReader` +
`TrackerIdentity` = `TrackerAdapter` Protocol and the optional `CommentWriter`, `LabelWriter` and
`IssueResolver`; standard library only, API version 1) and `application` (`iterate`,
`snapshot_ids`). It has no domain layer and no state. It is a **leaf**: the import checker forbids
it any other module's `contracts` or `application` (G3), forbids `httpx` in any `contracts` layer
(G1) and forbids tracker SDKs (`githubkit`, `gidgethub`, `github`) in every root layer (G2).
Concrete adapters live in plugin packages. Consumers test against
`beadhive.testing.tracker.MemoryTracker`, and every adapter must pass
`beadhive.testing.tracker.TRACKER_CASES`. Module-internal tests are `tests/unit/modules/tracker`
and `tests/unit/testing/test_tracker_conformance_kit.py`.

### `modules/beadlog_store`

Owns the **`beadlog.store` capability, API 1** ([beadlog-format-adr.md](design/beadlog-format-adr.md)
amendment A6): `beadhive.modules.beadlog_store.contracts` (the capability id and version, the
`BeadlogStore` and `BeadlogStoreLoader` Protocols, `StoreSnapshot`, `PasswordSource` and the
`BeadlogStoreError` / `BeadlogStoreUnavailable` refusals; standard library only). It is the seam
through which `bh backfill apply|export --store <name>` reaches a BeadLog store other than the
built-in bd store. It has no domain, application or state and imports no other module. The
composition root is `beadhive.bootstrap.beadlog_store` (lazy, after manifest selection); the only
provider is the opt-in `beadhive-beadlog-xtdb` plugin package (`beadhive[xtdb]`).

### `modules/intake`

Owns **external intake**: the one shared intake queue, the intake and origin vocabulary and, in
later beads, import, link, the conversation model and the outbox (`external-intake-module-adr.md`).
The consumer port is `beadhive.modules.intake.contracts.IntakePort`; `IntakeService` is its one
implementation and `NullIntake` is bound when intake is off (`bind_intake`). Intake's own outbound
ports are `ReportStore` (the bead store holding report beads), `CheckpointStore` and `LabelPolicy`.
`domain` is standard library only and holds the records, `RefusalReason` / `IntakeRefused`, the
vocabulary (`INTAKE_DIMENSIONS`, moved out of `beadhive.state`, which stays a facade that builds
`STATE_DIMENSIONS` from it) and pure rules; `application` also owns intake's own pager over the
tracker port's contracts, because intake may not import `tracker.application` (G5).

The import checker enforces the direction: intake may import only the tracker port's contracts
(`modules.tracker.contracts`), never `modules.work.*` or `modules.planning.*` (G6, the
consumer-exclusion table), never a legacy module, adapter or transport. Gap G7 is a module-local
AST test: no legacy file except the composition root `intake_services.py` imports
`modules.intake.domain` or `.application`.

`bh report`, `bh escalate` and the triage verbs (`bh work intake|accept|reject|reroute|promote`)
now run through the port. `beadhive.adapters.intake.BdReportStore` implements `ReportStore` over
injected `beadhive-bd-cli` routes (it never imports the package or builds argv);
`ApiReportStore` is the same store served from the hive's supervised Beads service
(`beadhive.intake_beads`, bh-9o12j) under `work.beads.route` exactly as the work verbs are: reads,
type/priority/assignee updates, tracking edges, the close and the three metadata documents go to
the service (the documents by compare-and-set), while the creates (the service cannot record
`created_by`), the gates, `bd set-state` and `bd find-duplicates` stay on the CLI, and the
`external_ref` lookup confirms an index miss on the CLI because the service has no such filter.
A command that needs only the CLI (`bh report`) never opens a session. The binding and
last issue snapshot of a bound report live in the flat top-level metadata key `intake.source`, the
issue URL in the bead's native `external_ref`. `beadhive.intake_services` is the legacy
composition root: it binds the store to the hive's operational store (`store_target.operational_store`
only, never a public store), supplies the label policy (`validate.bead_violations`) and renders the
module's refusals as the legacy `(exit, error, message)` tuples. `report.py` keeps hive resolution,
clone-on-demand and the push; `triage.py` is a thin facade; reroute to another hive is an
orchestration in the root (`IntakeService.reroute_elsewhere` over the source store plus the
caller's filing into the target). `report` no longer imports `triage`, which retired the owned
legacy cycle `cycle-edge-036`. The import, link and discovery operations, the outbox and the
`intake.external` config keys arrive in later beads, all off by default.

The conversation model (`bh-fptob`) is `domain/thread.py` (pure rules over intake-owned records:
`(id, revision, digest)` upserts, a local `seq`, tombstones only from complete id snapshots,
layered own-message recognition, the `considered_rev` triage watermark) and
`application/threads.py` (`ThreadService`, the only tracker-typed code of the model). A bound
report's thread lives in the flat metadata key `intake.thread`, rewritten whole in one store write
together with its per-issue checkpoint, under the per-binding `IntakeLock` and the store's `seq`
re-check (`ReportStore.read_thread` / `write_thread`; `THREAD_MOVED` when the document moved).
`IntakeService` delegates the thread operations to an optional `ThreadService` and refuses them
with `INTAKE_DISABLED` without one, so comment ingestion is off by default. Purging a tombstoned
body removes it from the current document only; under the archive rule earlier versions remain in
store history, so purging is not erasure. Its tests run on `MemoryTracker` with explicit `at=`
(`tests/unit/modules/intake/test_intake_threads.py` is the `bh-fptob` plan T1 to T19,
`test_intake_thread_rules.py` drives the rules directly).

Import (`bh-2ef4g`) is `application/importer.py` (`IssueImporter`, behind
`IntakeService.import_issue`): it resolves the URL through the tracker's optional `IssueResolver`
(distinct refusals R3 to R7) or pages `list_issues` (one `NOT_FOUND_IN_PROJECT`), keys on the
canonical URL in the operational store only, creates a held report in `intake:untriaged` with
`origin:github`, and re-imports as a snapshot update plus repair of any missing piece. It reads the
tracker and never writes it. `intake_services.import_issue` composes it; the switch
`intake.external.enabled` is host-owned and off, and the tracker is built only when
`plugins.tracker-github.enabled` is also true (`intake_services.process_adapters`, `bh-kzs0b`);
otherwise the verb refuses with `intake_disabled` naming both keys. Its lock is the root's
`FlockIntakeLock` (below), shared with the outbox sender: a mutating import takes `guard_primary`
and the per-binding `flock`.

Link (`bh-v2rr6`, I5) is `application/linker.py` (`LinkService`, behind `IntakeService.link`,
`unlink`, `work_progress` and `dispose(accept)` with `work` / `new_work`): a bound report `tracks`
a work bead of the same operational store (several reports to one work), decided by the pure rules
in `domain/link.py` before any write; a bound report is never a target (D-a) and no edge crosses
stores. A link writes `delivery:pending`; an unlink keeps the value (`bd set-state` cannot clear
one) and records an `unlinked` fact in the report's `intake.outbox` document. Work progress is
derived on read from the tracked bead. `intake_services.link_report` / `accept_to_work` compose it
under the root lock, off unless `intake.external.enabled` and without a tracker; `bh work link` and
`bh work accept --work/--new-work` are registered in the operation catalog (wire 2.16.0, bh-73td2).

Discovery (`bh-df64b`, I6) is `application/discovery.py` (`IssueDiscoverer`, behind
`IntakeService.discover`): opt-in, pull-only, and the same upsert as import
(`IssueImporter.upsert`), so a discovered report is exactly an imported one. An incremental pass
pages `list_issues(since=<checkpoint>, include_closed=True)` and stores the tracker's own mark
through `CheckpointStore` only after a page is durably upserted; a full pass (`--full`, or every
`full_reconcile_every`-th timer run) re-pulls everything including closed issues and, on a COMPLETE
id snapshot only, marks a vanished issue's report `source_missing` in its snapshot (it never
deletes or closes a report). The root file-backs the checkpoint under `.bh/intake/checkpoints`
(`beadhive.adapters.intake.FileCheckpointStore`; `MemoryCheckpointStore` for tests) and takes the
same `FlockIntakeLock` as import. `intake_services.discover` composes it and the opt-in timer is the
root module `intake_discovery.py` (the `release_catalog_auto_import` pattern, wired in
`host_daemon.serve`): `intake.external.discovery.enabled` and `interval_seconds` are host-owned and
off, and enabling it without a wired adapter refuses startup. `bh work discover` and `bh work import`
are registered in the operation catalog (wire 2.16.0, bh-73td2).

The outbox (`bh-6wj9y`) is pure so far: `domain/outbox.py` holds the communication records (keyed
by an opaque `ob1_` id and a stable `DraftKey`, rendering only their `PublicFacts` and message
text) and the per-operation machine O1 to O19, `domain/leaks.py` the leak check,
`domain/delivery.py` the `delivery`/`notified` rules and `domain/outbox_document.py` the
`intake.outbox` codec.
`application/outbox.py` only translates to and from the tracker port (`WriteGrant`, receipts,
capabilities, which errors prove nothing was written); the sender is `application/sender.py`
(below), and `OutboxPolicy()` is off. The `Sanitizer` and `IdSource` ports are in the
contracts; `beadhive.adapters.intake.ScrubSanitizer` implements the first over the shared
`scrub()` until `bh-o2mjz`, and with no sanitizer every operation is held. Tests:
`tests/unit/modules/intake/test_intake_outbox_*.py` (the `bh-6wj9y` R14 cases at the domain level,
X9 on `MemoryTracker` with kit extension K1).

The supervised `comment.create` sender (I9a, `bh-czes8`) is `application/sender.py`:
`OutboxService` runs the machine against `ReportStore.read_outbox` / `write_outbox` (one whole
`intake.outbox` rewrite per step, refused with `OUTBOX_MOVED` when the stored `seq` moved) under the
binding's `IntakeLock`, recovering a dead sender's `sending` to `unknown` (O13) on every lock
acquisition. `IntakePort` gains `draft_reply`, `outbox`, `render` (the dry run: no tracker request,
no store write), `approve` (O5, minting one single-use grant checked by the typed digest),
`send` (O9 persisted before exactly one `post_comment`), `reconcile` (one complete comment listing
per call; two without the marker after the attempt ended make it `failed(absent)`), `discard` and
`retry`. `IntakeService` delegates them to an optional `OutboxService` and refuses with
`OUTBOX_DISABLED` without one. `StoreOutboxView` is the store-backed `OutboxView` (with `issued()`)
thread recognition reads. The root (`intake_services`) adds `FlockIntakeLock` (`guard_primary`, an
`fcntl.flock` under `.bh/intake/locks`, `guard.live_epoch`), `RandomIdSource`, `leak_policy`,
`store_posture` and `outbox_service_for`, which binds no sender unless an enabled `OutboxPolicy` is
passed and takes the tracker adapter and its marker codec injected. The seven `bh work outbox`
verbs (wire 2.16.0, bh-73td2) call it through `intake_services.outbox_call`; they need
`intake.external.enabled` and `intake.external.outbox.enabled`. `outbox_wiring` (bh-uavc2) builds
the tracker, identity and marker codec from the hive's public-store binding when
`plugins.tracker-github.enabled` is on too: the tracker is the bound, narrowed port, the marker
codec is that port's optional `MarkerCodec` (so root never imports the plugin package), and the
identity is the bound `owner/repo` plus `install_id` (`.bh/intake/install-id`). Without a tracker,
`outbox show <id>` prints the stored record (`StoredOutboxRecord`) instead of the dry run
(bh-fehna). Tests: `tests/unit/modules/intake/test_intake_outbox_sender.py`,
`tests/test_intake_outbox_root.py` and `tests/test_intake_outbox_wiring.py`.

`ThreadService` is composed in production by `intake_services.thread_call` (bh-o9rep) when
`intake.external.comments.enabled`: over the operational store, the root lock, `StoreOutboxView`
and, for ingestion, the same binding requesting `comments.read`. The `bh work thread show`,
`ingest` and `considered --through <seq>` verbs (wire 2.21.0) reach `thread_of`, `ingest_thread`
plus `reconcile_thread`, and `mark_considered`; the outbox service carries the thread operations
too while comments are on. Tests: `tests/test_intake_thread_composition.py`.

Waiting for the reporter (bh-61xkc): `IntakeService.needs_info` drafts the question through the
sender as a `reply` record and writes `intake:needs-info`; `wake` / `wake_all` read the stored
thread and write `intake:untriaged` back exactly when it holds new external evidence (never via
`mark_considered`). The rules and records are `domain/waiting.py`. `intake_services.needs_info`
(outbox port) and `wake` (thread port) are gated by `intake.external.needs_info.enabled` (and
`.sweep` for `bh work wake --all`); `MilestoneService.on_linked` catches a report linked to already
merged work up on its milestones when `milestones.catch_up_on_link` is on. Tests:
`tests/unit/modules/intake/test_intake_needs_info.py`, `test_intake_link_catch_up.py` and
`tests/test_intake_needs_info_composition.py`.

The same sender also carries `labels.change` (I9b, `bh-88vji`) as its own gate: `OutboxPolicy`
gains `labels_enabled` (off by default, separate from `enabled`, which gates the outbox and the
comment), so a label operation is refused with `LABELS_DISABLED` at approve and at send before any
tracker request; it is approved separately, mints its own grant and is sent with one
`change_labels` whose `LabelOwnership` is the root's owned prefixes (`outbox_service_for(...,
owned_label_prefixes=)`), and a label outside them is refused at draft, in the leak check, at
approve and at send. `IntakePort.send` and `retry` take `kind` (default `comment.create`) and
`discard` an optional one; `reconcile` reads the issue's labels back (labels carry no marker). See
`external-intake-module-adr.md` section 11. Tests:
`tests/unit/modules/intake/test_intake_outbox_labels_sender.py`.

The sender also carries `issue.close` (`bh-pr5ll`) as a third gate: `OutboxPolicy.close_enabled`
is set by the composition root from the bound adapter's own `issue.close` enable
(`plugins.tracker-github.write.issue_close.enabled`; there is no separate intake key), and a close
is refused with `CLOSE_DISABLED` otherwise. A close rides on a reply record (`ReplyDraft.close`;
triage apply asks for one on a `fixed`, `reject` or `duplicate-of` reply, never on `needs-info`),
has its own digest, grant and receipt, and O9 refuses it with `CLOSE_NOT_READY` until the record's
comment is `sent`, no other comment of the report is unsent, and the report's latest labels record
is `sent`. `reconcile` reads the issue's state back. Apply drafts a reply before any other verb; a
`duplicate-of` gets one only while an outbox is bound (`ProposalService(replies=)`).
`IntakePort.backfill_closes` (`bh work outbox backfill-close`) adds the close to the latest reply of
every report closed in the store whose issue is still open, and drafts the reply a report never
got from its applied proposal (`ProposalService.backfill_reply`). Tests:
`tests/unit/modules/intake/test_intake_outbox_close_sender.py`.

A `duplicate-of` close names its canonical (`bh-iamru`). `ReplyDraft.duplicate_of` is the
canonical REPORT (apply passes `share_with`, the backfill does the same); the sender resolves it
to that report's bound issue (`CanonicalIssue`, only within the member's tracker project, else a
plain duplicate close) and stores it on the `issue.close` operation, whose rendered bytes add
`duplicate_of: <url>`, so the digest the operator approves covers the canonical. The port's
`IssueCloser.close_issue(..., duplicate_of=IssueKey)` (shape refused by `check_close` before any
request) reaches GitHub as `duplicate_issue_id`, the canonical's numeric issue id, on the same
`PATCH .../issues/{n}` (REST "Update an issue"; GraphQL `closeIssue(input: {duplicateIssueId})`
is the same link): the adapter reads the canonical once inside the write session BEFORE the grant
is spent, so a missing, moved or pull-request canonical is a `NotVisible` refusal that keeps the
grant. The `close.duplicate-of-canonical` conformance case holds every closer to it.

The pre-apply checks resolve every `#N` a `reporter_text` mentions (`bh-iamru`;
`domain/proposal_checks.mentions`, outside code, never `owner/repo#N`). The check runs in
`check_run` (`bh work triage check` and `apply --reviewed`), not in `propose`: propose is called
once per report and stays tracker-free (its lint is local), so checking there would cost one
listing per report and would still be stale by apply time, while the run-level check costs ONE
listing for the whole run and re-runs at apply. The issues are plain data (`MentionFact` in
`CheckContext.issues`) from the contracts' `ProjectIssues` port: `TrackerProjectIssues`
(`application/mentions.py`) lists the project once, through the id snapshot resolved without a
request when the adapter is an `IssueResolver` (closed issues included whatever
`read.include_closed` says) or the issue listing otherwise, and is read only when some proposal
of the run mentions a `#N`; the root binds it lazily (`intake_services.project_issues`,
`issues.read` only). A `#N` that is no issue of the project fails `mention_missing`; one that
cannot be verified (no tracker bound, a failed listing) fails `mention_unverified`. A `#N` that is
another bound report is returned as `ProposalCheck.relates`, and apply records a bh-side
`relates-to` (`ReportStore.relate` / `related_reports`, bd's built-in `relates-to` edge from the
mentioning report, read both ways, idempotent either way; `RELATES_EDGE`) before its verbs, so a
failure there writes nothing else. Tests: `test_intake_proposal_checks.py`,
`test_intake_apply_reviewed.py`, `test_intake_project_issues.py`, the
`store.relates-reports-either-way` contract case and `tests/test_intake_labels_triage_cli.py`.

GitHub's issue dependencies (`blocked by`) and sub-issues are NOT used (`bh-iamru`, decided): no
bead edge means the same thing. Their two ends are issues, and the only issue-backed beads are
reports; between two reports bh has `relates-to` (a mention: symmetric and non-blocking) and the
duplicate family (sent as the duplicate close above). A report's `tracks` and `validates` edges
end on WORK, which is never a GitHub issue, and epics and their children are work too. A "report
blocked by another report" would have to be derived through their tracked work (A tracks W1, W1
blocked by W2, B tracks W2): that is not an edge, it changes whenever planning does, W2 may be
tracked by several reports or none, and it would publish internal planning. So there is no switch,
no outbox operation kind and no grant for either; revisit only if bh gains a report-to-report
blocking or parent edge.

The label projection also carries the issue's **type** and a **field value** (`bh-poqy0`):
`domain/issue_values.py` holds the pure maps (`ValueSettings`, from
`intake.external.labels.issue_type` / `priority_field`) and the work-then-report fallback
(`desired_values`; an unmapped value is a note, never a guess). `LabelProjection(values=)` diffs
them against the issue's `TrackerIssue.issue_type` / `fields` from the same listing, reports a
difference like drift (`LabelSyncItem.values`, `LabelSyncOutcome.value_drift`) and drafts it as an
`issue.type` / `issue.fields` operation (a `ValueIntent`) on the SAME `labels` record, so the batch
review approves each with its own digest and grant. `OutboxPolicy.type_enabled` /
`fields_enabled` are the gates (set from the bound adapter's `write.issue_type.enabled` /
`write.issue_fields.enabled`; `TYPE_DISABLED` / `FIELDS_DISABLED`), and `reconcile` reads the
value back. The tracker port's optional `IssueTypeWriter` / `IssueFieldWriter` are the writers.
Tests: `tests/unit/modules/intake/test_intake_issue_values.py`.

Finding a record and sending in bulk (`bh-pc9rf`): the sender takes an optional `OutboxIndex`
port (outbox id to report; `beadhive.adapters.intake.FileOutboxIndex` over
`.bh/intake/outbox-index.json` in production, `MemoryOutboxIndex` in the kit), confirms every
answer against that report's document, and on a miss scans once with the new
`ReportStore.read_outboxes()` (one listing whose rows carry the documents on `ApiReportStore`) and
rebuilds the index. `IntakePort.send_approved(BatchSend)` (`bh work outbox send-approved`, wire
2.24.0) sends every approved operation in one process, per report in `SEND_ORDER` (comment,
labels, type, fields, close), each through the same O9 to O13 as `send` with its own grant and
receipt, and reports a refused one in `BatchSendOutcome.failed` while the rest continue (losing the
primary host stops it with `BatchSendOutcome.stopped`, keeping what was sent). It and
`approve_label_batch` run inside the lock's optional `BatchLock.batch()` scope, where
`FlockIntakeLock` reuses one host check and epoch read for at most
`intake.external.outbox.batch_recheck_seconds` (default `0`: every operation checks; reuse is
opt-in). Tests:
`tests/unit/modules/intake/test_intake_outbox_batch_send.py`,
`tests/test_intake_outbox_batch_root.py` and the perf check
`tests/test_intake_outbox_batch_perf.py` (`-m perf`).

The merge and release milestones (I10, `bh-56s4l`) are `application/milestones.py`:
`MilestoneService` behind the new `IntakePort.on_work_merged(MergeFact)` and
`IntakePort.on_release(ReleaseFact)` (free under `NullIntake`). A merge sets `delivery:merged` on
every report that tracks the work, open or closed, records `facts.merged` in `intake.outbox`,
drafts a `merged` record when `intake.external.milestones.draft_on_merge` and
`outbox.enabled` are on, and applies the completion policy; a release is verified by the new
`ReleaseVerifier` port (`beadhive.adapters.intake.GitReleaseVerifier`: `git rev-parse` and `git
merge-base --is-ancestor`, read-only; kit fake `FakeReleaseVerifier`) and only a `YES` moves a
report to `delivery:released`. Milestones only DRAFT: the service holds no tracker, grant or
writer. The work layer calls `work_intake.on_work_merged` from `work.merge` and `finish` after the
close, a no-op unless `intake.external.enabled` and never failing the merge. `bh work delivery
release <tag>` is registered in the operation catalog (wire 2.16.0, bh-73td2; `work_intake.delivery_release`,
`work.delivery_release_cmd`, mounted on the nested `work delivery` group). Tests:
`tests/unit/modules/intake/test_intake_milestones.py`,
`tests/test_intake_milestones_composition.py`, `tests/test_work_merge_milestones.py` and
`tests/test_intake_milestones_int.py` (`integration`).

The progress milestones (bh-dev42) are the same service: `IntakePort.on_work_started(StartFact)`
(free under `NullIntake`) drafts a `started` record for every report that tracks newly claimed
work, keyed on the work so only its first claim drafts, and `IntakePort.delivery_notice(NoticeFact)`
drafts a `blocked` / `deprioritised` record from an operator's public reason under fixed wording
(`domain/milestones.py`: `STARTED_MESSAGE`, `notice_draft`, `notice_reason_problem`). Both draft
only while delivery is `pending`, never move a dimension or close a report, and are gated by
`intake.external.milestones.draft_on_start` / `.draft_on_blocked` / `.draft_on_deprioritised`
beside `outbox.enabled`. The work layer calls `work_intake.on_work_claimed` after a fresh claim
(`work_lifecycle.claim_single_bead` on the `claimed` disposition, `provision_claim` for `bh work
next`, `work.start` and `work_group.claim_group`): a pure config read when off, never failing the
claim, output on stderr only. `bh work delivery blocked|deprioritised <work> --reason <text>` are
additive leaves of wire 2.21.0. Tests: `tests/unit/modules/intake/test_intake_progress_milestones.py`,
`tests/test_intake_progress_milestones_composition.py` and `tests/test_work_claim_milestones.py`.

Reproduction work (bh-u3a53) is a second report-to-work relation beside `tracks`:
`IntakePort.investigate(InvestigateRequest)`, `uninvestigate` and `relations` (`LinkService`, pure
rules `check_investigate` / `check_investigable` in `domain/link.py`). A bead that investigates a
report is linked by its own native edge (`ReportStore.investigate` / `uninvestigate` /
`investigations_of`; on bd 1.3.0, which refuses a custom type, the built-in `validates` edge from
the work to the report, `INVESTIGATES_EDGE`), so it never appears in `trackers_of`, which is all
the milestone triggers, the claim hook, the link catch-up, `reports_for_work` and `work_progress`
read: closing, merging, claiming or releasing it drafts nothing, writes no `delivery` value and
never closes the report. Investigating writes the edge and nothing else; several beads may
investigate one report, the work it tracks may not (`tracked_work`).
`intake_services.investigate_report` composes it with no milestone service bound. `bh work
investigate <report>` with `--work <id>`, `--new-work [--type --priority]` or `--work <id>
--remove` (with neither: the report's links) is an additive leaf of wire 2.21.0, off unless
`intake.external.enabled`; `bh work thread show` lists both kinds of link. Tests: `tests/unit/modules/intake/test_intake_investigate.py`,
`tests/test_intake_investigate_composition.py` and `tests/test_intake_investigate_int.py`
(`integration`), plus the `store.investigates-is-not-tracks` contract case.

The label projection and review-gated batch triage (epic `bh-hrx2r`, 0.35.0) reach the CLI
through the composition root only (`bh-v3fe7`, wire 2.22.0). `intake_services` translates
`intake.external.labels` into the module's `LabelSettings` (`labels_config`) and `dimensions.*`
into `RegistryDimension` records (`registry_dimensions`), builds `LabelProjection` over the bound
tracker (`labels_wiring`: `issues.read` and `labels.read`, plus `labels.define` for an approved
`define`; the root narrowing view carries the `LabelPruner` that `define --prune` deletes through)
with the outbox's policy and a root `AuditLog` (`FileAuditLog`, one JSON line per approved define
in the private `.bh/intake/audit.log`), and binds `TriageBinding` (`triage_binding`:
`adapters/intake.GitRepoFacts` on the hive checkout, the `work.integration_branch`, the outbox's
leak policy and `ScrubSanitizer`, a root `LandedCommits` over each bead's `git.commits` linkage,
`LinkageLandedCommits`, and the apply-time re-filer `reroute_refiler` for `--reroute-to
<report>=<hive>`) and `ReviewPolicy` (`review_policy`: `comments.enabled` and whether the binding
serves `comments.read`). `transition_refresh` binds the tracker-free projection with
`refresh_on_transition` into every transition composer (dispositions, link, needs-info, wake, the
milestone triggers) when `labels.on_transition` is on. `labels_sync` / `labels_define` /
`labels_review` / `labels_approve` and `triage_review` / `triage_propose` / `triage_check` /
`triage_apply` return an `IntakeCall`; `triage_apply` refuses an agent seat exactly as an outbox
approval does (`is_operator_seat`), and the module's own services refuse one for `define
--approve` and the batch approval. `work_intake` renders each result and owns the `--json`
serializers (proposals as their stored `intake.proposal` document) and the `triage review --out`
files (`batch-NNN.json` plus `index.json`); `bh label docs` appends `projected_label_lines`. Tests:
`tests/test_intake_labels_triage_cli.py` (every verb offline, including the dry-run walkthrough
that writes nothing), beside the module tests `test_intake_label_projection.py`,
`test_intake_label_refresh.py`, `test_intake_review.py` and `test_intake_apply_reviewed.py`.

The closed `delivery` (`pending|merged|released|reverted`) and `notified`
(`replied|merged|released|corrected|started|blocked|deprioritised`) dimensions are registered in
`INTAKE_DIMENSIONS`, so `bh label validate` accepts those values and refuses any other; nothing
writes them yet. An `intake-hold:` gate (`guard.INTAKE_HOLD_GATE_MARKER`) classifies as gate kind
`intake-hold`, and no seat resolves it through `bh work approve`. The operator views count only
`blocks`, `conditional-blocks` and `waits-for` edges as blockers, so a `tracks` or `validates`
(investigates) edge or any unknown edge type never blocks. `bd set-state` cannot clear a
dimension, so unlinking keeps the `delivery` value (`delivery_after_unlink`).

A bound report is held for life (`domain/hold.py`, `external-intake-module-adr.md` section 8): its
`ReportStore.create` opens an `intake-hold:` human gate, so it is never in `bd ready` or `bh work
ready`; `bh work claim`, `assign` and `claim --group` refuse it; a plain close is refused by bd; and
`ReportStore.close` is the held close (`bd close --force` through `issue_close_held`, then resolve
the hold, so there is no ready window). A born-native `bh report` item is never held. `bh doctor`
reports the `intake_holds` findings (a missing, unresolved or orphaned hold) for hives whose public
store binds a tracker adapter, and `IntakeService.sweep_holds(repair=True)` repairs them.

Consumers test against `beadhive.testing.intake.MemoryReportStore`, and every `ReportStore` must
pass `beadhive.testing.intake.REPORT_STORE_CASES`: `BdReportStore` passes them on an in-process
fake bd (`tests/test_intake_bead_store.py`) and on a real bd store
(`tests/test_intake_bead_store_int.py`, `integration`); `ApiReportStore` passes them over a fake
Beads service (`tests/test_intake_api_store.py`) and has a real-service parity proof
(`tests/test_intake_api_store_real_service.py`, opt-in `BEADS_INTAKE_SCRATCH=1`).
Module-internal tests are
`tests/unit/modules/intake`, `tests/unit/testing/test_intake_report_store_kit.py` and, for G5 and
G6, `tests/test_intake_import_boundaries.py`; the composition is pinned by
`tests/test_intake_services.py`.

### `modules/worktrees`

Owns workspace binding, branch/worktree identity, creation/removal policy, inventory, status,
cleanup, and merge-related worktree mechanics. Exactly one configured `worktrees.manager` (native
Git) implements the `worktree.manager` slot declared on `beadhive-plugins`; plugin-mediated
provisioning is retired (bh-055ot.1). Existing facade and test patch points remain supported.

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

The first extraction keeps filesystem, process, Dolt, and transport behavior in outer adapters.
Immutable validation facts, stream/cursor policy, activity/query models, storage/clock/notification
ports, and read-side application services live in `beadhive.modules.state`; compatibility facades
preserve the released imports. Executable import-boundary and closure checks enforce this split.

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

### `adapters/operator_api` and `adapters/frame_bridge`

The operator API projects appropriate operations and read models into authenticated HTTP/OpenAPI.
The Frame Bridge retains its explicit Gateway wire/version boundary. It is a per-frame adapter,
not the multi-frame Beadhive Gateway authored in the sibling `beadhive-gateway` repository.
Neither transport is required to expose every catalog operation. Streaming, session, and
authentication concerns remain transport or host-runtime responsibilities.

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
8. Gateway wire schema v1, consumed by the per-frame Beadhive Frame Bridge.
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

Selective validation is defined by the attest-key gate model; see
[the attest-keys single gate model ADR](design/attest-keys-single-gate-model-adr.md). The
module-local test-closure registry that preceded it is retired.

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

Selection is owned by the attest-key catalog (see
[the attest-keys single gate model ADR](design/attest-keys-single-gate-model-adr.md)); the
closure-certification registry and operational report are retired. Leaf merge, child-epic finish,
final workstream submit/review, scheduled validation, and release remain full-only on
`just check-all`. These retained gates supersede the aspirational
progression below wherever they are stricter.

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

Executable closure and import-boundary checks preserve the extraction boundary.

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
