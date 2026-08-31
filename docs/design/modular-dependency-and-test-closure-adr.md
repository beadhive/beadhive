# ADR: Modular dependencies, compatibility facades, and test closure

> Status: **decided** (2026-08-31). Decision bead: `bh-inqwc.1`.

## Context

[`MODULES.md`](../MODULES.md) proposes a capability-oriented modular monolith and a staged
migration from the current flat `beadhive` package. This ADR turns that proposal into rules that
the modular-foundation molecule (`bh-inqwc`) can enforce. It does not claim that the target
packages already exist, move production behavior, or authorize a public contract change.

The package roles below are nested inside a named capability or named kernel. They are not an
invitation to create repository-wide `domain`, `application`, or `contracts` buckets. A package
must have one cohesive reason to change and one owner. Shared code moves inward only when it is a
stable contract or a genuinely cross-capability mechanism; unrelated helpers do not become a
kernel merely because several callers use them.

## Decision 1: capability packages depend inward

The target production roots are:

```text
src/beadhive/
  modules/<capability>/{domain,contracts,application}/
  kernel/<concern>/
  adapters/<adapter>/
  integrations/<integration>/
  bootstrap/<entrypoint>.py
```

A named kernel may expose a `contracts` subpackage. Files directly under `src/beadhive/` remain
legacy entry points or compatibility facades until an owned migration classifies them. Merely
moving a flat file under one of these roots does not satisfy the boundary.

### Responsibilities

| Role | Owns | Must not own |
| --- | --- | --- |
| Domain | Capability entities, value objects, invariants, and deterministic policy. | I/O, process or framework objects, plugin discovery, telemetry exporters, persistence DTOs, transport errors, or composition. |
| Contract | Stable inbound request/result types, outbound ports, public events, and adapter conformance semantics for one capability or kernel. | Concrete implementations, global registries, environment lookup, transport presentation, or hidden mutable state. |
| Application | One capability's use cases, transaction/orchestration policy, authorization and error mapping at the use-case boundary, expressed through declared ports. | Typer, FastMCP, HTTP servers, Herdr clients, OpenTelemetry SDKs, Dolt/Git/process details, or runtime discovery. |
| Kernel | A small, named cross-capability mechanism such as operations, plugins, lifecycle, telemetry semantics, or schema generation. | Capability business policy, a bag of shared helpers, arbitrary callback routing, or service lookup. |
| Adapter | One concrete transport or outbound-port implementation, including CLI, MCP, HTTP/gateway, persistence, process, Git, or telemetry SDK translation. | Domain policy, plugin selection, application orchestration, or process-wide assembly. |
| Integration | One optional external product's manifest, client, capability providers, lifecycle participants, and integration-specific presentation. | Core seat/work/plugin policy, unrelated integrations, or an alternate application layer. |
| Bootstrap | Process entry-point composition: load configuration, select implementations, bind handlers and ports, project transports, and manage process lifecycle. | Domain decisions, reusable business behavior, or implementation selected through ambient lookup. |

### Complete allowed-direction table

The architecture checker classifies an import by its most specific role. The table is exhaustive:
an omitted production-to-production direction is forbidden. “Own” means the same named capability,
kernel, adapter, or integration. Public means a documented non-private module; importing another
package's implementation or tests is never implied.

| Importer | Allowed Beadhive imports |
| --- | --- |
| `modules/<capability>/domain` | Its own domain package only. |
| `modules/<capability>/contracts` | Its own domain and contract packages; public contract packages of other capabilities or named kernels. |
| `modules/<capability>/application` | Its own domain, contracts, and application packages; public contracts of other capabilities; public kernel contracts. |
| `kernel/<concern>/contracts` | Its own contract package and other public kernel or capability contracts. It may not import a capability's domain or application implementation. |
| Other `kernel/<concern>` code | Its own kernel package and public contracts. It may not import capability application implementations. |
| `adapters/<adapter>` | Public domain, contract, and application packages needed by the implemented port; public kernel packages; its own adapter package. |
| `integrations/<integration>` | Public domain, contract, and application packages needed by its declared capabilities; public kernel packages; its own integration package. |
| `bootstrap/<entrypoint>` | Public domain, contract, application, kernel, adapter, and integration packages, plus its own bootstrap helpers. |
| A registered compatibility facade | Only the replacement packages and runtime patch collaborators named for that facade in the compatibility ledger. |

These additional constraints close ambiguities in the table:

- Domain packages do not import another capability. Cross-capability work is an application use
  case expressed through the provider's public contract or a consumer-owned outbound port.
- Adapters and integrations are peers. They do not import one another; reusable boundary
  semantics move to a named contract or kernel. Bootstrap may compose both.
- Application code may use a kernel's public contract, but it does not import a catalog,
  lifecycle, plugin, or telemetry implementation. The implementation is injected at bootstrap.
- Production packages never import bootstrap. Importing a module must not start transports,
  discover plugins, read operator state, initialize telemetry, or spawn a process.
- Third-party imports follow the same ownership rule: framework and SDK packages stay in adapters,
  integrations, or bootstrap. A pure library in domain or contracts requires an explicit
  repository dependency decision; its presence is not a route around the inward boundary.
- Cross-role cycles are forbidden. A same-role cycle across named capabilities, kernels, adapters,
  or integrations is also forbidden. Cohesive files inside one named package may import each
  other, subject to the normal cycle check.

`bh-inqwc.2` owns the executable import checker and its initial path/symbol exception ledger.
Rules land deny-by-default for new target packages; current flat-package violations are not
silently treated as precedent.

## Decision 2: the catalog is declarative, not a service locator

The canonical operation catalog records operation identity, request/result contract references,
policy, and transport eligibility. Application handlers remain canonical for behavior. Bootstrap
constructs an explicit binding from catalog entries to known handlers and gives that binding to
the operation executor or projection adapter.

Consequently:

- domain and application code never query the catalog to obtain a service or handler;
- catalog entries do not hold constructed repositories, clients, adapters, or plugin instances;
- runtime discovery does not mutate the catalog into a dependency container;
- adapters project declared operations but do not invent a second operation namespace; and
- adding a catalog entry cannot move business behavior out of its owning application package.

This preserves the decision already implemented by `bh-j4gbx` and its
[canonical-catalog ADR](canonical-operation-catalog-adr.md). The catalog/kernel is a narrow
cross-capability mechanism, not a repository-wide application layer. For the same reason, this
ADR rejects a layer-only split into global `domain`, `application`, and `adapters` god packages:
layer names may describe the inside of a capability, but the capability remains the unit of
ownership, construction, and test closure.

## Decision 3: every compatibility facade and import exception has a removal ledger

A compatibility facade is a temporary, tested forwarding boundary for an existing import or
monkeypatch seam. It is not a license for new consumers to use the legacy path. The initial
inventory is inherited from the
[structural quality baseline](structural-quality-baseline.md#compatibility-matrix):

| Legacy facade | Current implementation family | Required executable proof | Removal owner |
| --- | --- | --- | --- |
| `beadhive.work` | `work_*`, migrating to `modules/work` | `tests/test_structural_facade_contracts.py`, `tests/test_work_reads.py`, and `tests/test_work.py` | The work-capability migration; until then, the work facade steward. |
| `beadhive.config` | `config_*`, migrating to `modules/config` | `tests/test_structural_facade_contracts.py` and `tests/test_config.py` | The config-capability migration; until then, the config facade steward. |
| `beadhive.worktree` | `worktree_*`, migrating to `modules/worktrees` | `tests/test_structural_facade_contracts.py`, `tests/test_worktree.py`, and `tests/test_wt_status.py` | The worktree-capability migration; until then, the worktree facade steward. |

The baseline's symbol and patch-point matrix is authoritative for these three entries; this ADR
does not abbreviate that compatibility surface to importability alone. A planned facade, such as
one for a later plugin migration, enters the inventory only in the change that characterizes its
real consumers and executable seams.

The checked ledger established by `bh-inqwc.2` must carry, for every facade or forbidden-import
exception:

1. a stable ledger ID and status (`active`, `removable`, or `removed`);
2. the exact importing path and symbol or facade path and preserved symbols;
3. the target owner package and named steward or successor bead;
4. the compatibility reason and consumer inventory, separating production, tests, docs, and
   known external consumers;
5. the executable characterization test and relevant test-closure entry;
6. the introduction and last-verification commits;
7. a concrete expiry trigger, such as completion of a named migration bead or removal of the last
   inventoried consumer; and
8. for a removed entry, the removal commit and replacement path.

Wildcards, directory-wide exemptions, “temporary” without an owner, and expiry stated only as a
date are invalid. An exception permits only the recorded edge; a second caller is a new violation.
Closing its owner/successor bead or satisfying its consumer-zero trigger makes an active entry
fail the architecture check. The same change must remove the edge or explicitly amend this ADR
and assign a new owner; silence cannot renew it.

A facade becomes `removable` only when all inventoried consumers use the replacement API, the
repository and documentation contain no legacy import or patch reference outside the ledger,
the replacement's contract and reverse-dependent closure is green, and the responsible public
contract/release owner approves removal. Deletion and the `removed` ledger record land together.
Until then, its facade tests remain mandatory in every affected closure.

## Decision 4: test closures graduate from evidence, not directory names

### Current closure

The current configured developer/submit correctness gate is `just check`: Ruff lint and format,
Markdown lint, license/SBOM policy, wire-schema compatibility, and the fenced parallel
non-integration pytest selection. The land/release closure is `just check-all`, which additionally
requires `bd`, runs the fenced land integration selection, and runs the local-loop and live-ingress
demos. `just cov` is periodic non-integration statement coverage with no global fail-under.

Those commands are authoritative while `bh-inqwc` is in progress. The structural baseline's
coverage and graph values are historical observations at the commits it names, not current
per-test coverage. Neither that document nor the current RepoWise data provides a fresh dynamic
per-test map, so this ADR makes no claim that a changed path selects all tests that execute it.

### Expected module closure during migration

`bh-inqwc.3` through `bh-inqwc.5` will make a candidate closure an explicit union, not a path
filter:

1. module-local domain/application tests using only module fixtures and injected fakes;
2. public contract and real-adapter conformance tests for every changed port or schema;
3. compatibility-facade tests for every affected ledger entry;
4. tests of statically known reverse dependents and, once available, dynamically observed
   dependents;
5. affected adapter and integration tests;
6. the registered boundary-crossing integration and system smoke tests; and
7. the import-boundary checker and closure-registry drift check.

Every module, kernel, adapter, integration/plugin, and shared contract family must have a stable
command or an explicit `absent` entry until it exists. Module-internal fixtures must not import,
boot, or construct higher layers. Higher-layer tests substitute the public port; real adapters
are exercised by contract or integration tests. During this stage candidate closures are useful
developer feedback only: `just check` remains the submit gate and `just check-all` remains the
land/release gate.

### Graduation criteria

A capability may replace the full merge closure with its selected closure only after an exact-tip
evidence ledger proves all of the following:

- the import checker reports zero unowned forbidden edges and no new cross-package cycle;
- its module command, contract command, facade closure, reverse-dependent closure, and required
  integration/system smoke tests are registered and green;
- an independence sentinel proves its unit closure performs no operator-config read, Dolt access,
  plugin discovery, telemetry startup, network/process spawn, or higher-layer construction;
- fresh dynamic per-test coverage or equivalent trace evidence exists at the measured commit and
  agrees with the registered closure for every production file in the capability; static impact
  evidence alone cannot satisfy this item;
- a shadow period covers at least **30 qualifying merged changes and 60 days**, whichever is
  later, running both the candidate closure and the existing full closure on the same tree, with
  **zero** cases where the candidate is green and the full closure finds a relevant failure;
- every deliberate boundary mutation in the module's conformance fixtures is rejected by the
  candidate closure; and
- collection counts, wall times, skipped/quarantined tests, tool versions, exact commit, and the
  resulting closure reduction are recorded rather than inferred.

Graduation changes only the ordinary merge selection for the certified capability. Shared
contract changes run all registered consumers. Release protection retains the full cross-module,
cross-plugin, integration, and system closure. A selected-green/full-red escape disables
graduation for the affected capability immediately, restores the full merge gate, records the
missed edge, and starts a new evidence window after the boundary or selector is repaired.

## Ownership reconciliation

| Existing record | Scope preserved by this ADR |
| --- | --- |
| `bh-1c04h` | Owns hermetic synthetic checkouts, phase fencing, and process/environment isolation. Module commands run inside that mechanism; this molecule does not build another sandbox or treat hermeticity as test selection. |
| `bh-1owpi` | Owns exact-tree land-time attestation and reuse. An attestation records the command actually run; a module-closure result must never be presented as a `just check-all` verdict. This ADR decides closure membership, not attestation storage or trust. |
| `bh-j4gbx` | Owns the canonical operation/schema artifacts and the CLI/MCP projections already landed from them. This ADR supplies dependency rules around that foundation and preserves application handlers as behavior owners; it does not reopen catalog compatibility or transport policy. |
| `docs/design/structural-quality-baseline.md` (`bh-1jhk4`) | Owns the historical `work`, `config`, and `worktree` facade/patch matrix and its exact-commit graph and coverage observations. This ADR seeds the removal ledger from that matrix without copying its measurements forward or declaring those facades removable. |

The modular-foundation molecule owns import direction, explicit fixture scope, reusable
conformance kits, module commands, impact mapping, and the new exact-tip evidence ledger. It must
consume the isolation and attestation mechanisms above and must not duplicate their runtime or
trust semantics.

## Consequences

- Package movement can be reviewed against one deny-by-default direction table rather than a
  suggestive directory tree.
- Capability ownership remains visible; neither the kernel nor a global layer becomes a place to
  hide unrelated behavior.
- Compatibility debt is preserved deliberately and can only shrink through executable,
  consumer-zero evidence.
- Selective testing has an explicit route to adoption, but no current speed claim is promoted to
  a correctness claim.
- Bootstrap remains the only place where concrete implementations and operation handlers are
  assembled.

## Non-goals

- Reorganizing production files in this decision bead.
- Splitting modules into distributions or services.
- Replacing the canonical catalog, schema compatibility policy, hermetic runner, or attestation
  store.
- Removing a current facade or weakening its monkeypatch seams.
- Enabling selective merge testing before the graduation evidence exists.
