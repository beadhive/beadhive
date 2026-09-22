# ADR: Repository physical organization and compatibility retirement

> Status: **decided** (2026-09-21). Decision bead: `bh-50gsn.2`.

## Context and relationship to existing decisions

The
[`modular-dependency-and-test-closure` ADR](modular-dependency-and-test-closure-adr.md)
already decides the capability-first architecture, package roles, complete allowed dependency
directions, and exception metadata. This ADR extends that decision only with physical and local
build/test-topology rules it deliberately left open. It does not replace or relax its direction
table. Certification timelines, CI activation or promotion, CI routing, and validation-policy
changes are outside this decision.

The exact starting state is
[`repository-physical-layout-baseline.md`](repository-physical-layout-baseline.md): 337 production
Python files, including 212 flat implementation or compatibility files; 8 owned legacy SCCs; and
52 active architecture-debt records. Existing module packages are real architecture, but file
placement, root compatibility surfaces, adapter placement, and public exposure are not yet
consistent enough for “clean repository” to be an observable claim.

The migration is behavior-preserving. Physical organization is not permission to alter command
behavior, wire schemas, state semantics, validation policy, public import behavior, or supported
monkeypatch seams.

## Decision 1: capability is the physical cohesion boundary

New implementation belongs to one of these roots:

```text
src/beadhive/
  modules/<capability>/
  kernel/<concern>/
  adapters/<boundary>/
  integrations/<external-product>/
  bootstrap/<entrypoint>.py
  testing/
```

The role inside `modules/<capability>/` is expressed with `domain/`, `contracts/`,
`application/`, and, when justified by Decision 3, `adapters/`. A capability owns a cohesive set
of product rules and use cases. Hexagonal roles describe dependency direction inside that owner;
they do not become repository-wide layers.

`kernel/<concern>/` is reserved for a small mechanism with multiple capability consumers and its
own invariant, owner, and closure. `adapters/` contains shared inbound or outbound technology
boundaries. `integrations/<external-product>/` owns an optional product's contracts, application
coordination, adapters, and presentation as one cohesive integration. `bootstrap/` alone selects
concrete implementations and assembles process entry points. `testing/` contains reusable public
conformance support, not product implementations or generic test helpers.

An extraction is complete only when the implementation and its owner move together. Creating a
new directory while leaving authority, mutable state, or orchestration in a root module is not a
completed extraction.

## Decision 2: package-root modules are closed by default

At `src/beadhive/`, the allowed file classes are:

1. `__init__.py`, limited to package metadata and intentional public re-exports;
2. a compatibility facade registered exactly in
   `docs/design/import-boundary-exceptions.toml`; and
3. existing migration debt named by the exact root-module inventory until its owning cleanup
   bead moves or classifies it.

The third class is a frozen migration allowance, not precedent for adding another root
implementation. A new root file must be either a ledgered compatibility facade introduced in the
same change as its replacement owner, or it is forbidden. Process roots go in `bootstrap/`,
transport implementations in `adapters/`, and product behavior in a capability, kernel, or
integration.

The enforcement bead must use an exact manifest or ledger-backed AST rule. Line count, filename
suffix, and directory depth are useful observations but are not ownership evidence.

## Decision 3: adapters stay local unless the boundary is shared

A concrete adapter stays at `modules/<capability>/adapters/` when all of these are true:

- it implements an outbound port owned by that capability;
- only that capability's use cases consume the port;
- it is not itself a public transport or reusable integration surface; and
- its real-adapter contract tests fit that capability's closure.

An implementation belongs under repository-level `adapters/<boundary>/` when it is an inbound
transport projecting operations from multiple capabilities, or when one external mechanism
implements ports for multiple owners and has an independent owner and test closure. CLI, MCP,
operator HTTP, Git/Pants impact resolution, and telemetry export are examples of boundaries that
may meet this rule; use by two call sites alone does not.

Code belongs under `integrations/<external-product>/` when the external product is the cohesion
boundary and the package must carry its own manifests, lifecycle, application coordination, and
transport translation. An integration is not a place for general adapters used without that
product.

Shared deterministic semantics move inward to a named provider contract or kernel. Sharing a
helper does not justify a central adapter, a `common` package, or a service locator. Bootstrap
constructs and injects concrete adapters; capability application code does not discover them.

## Decision 4: contracts are provider-owned packages, not a DTO dump

Each capability exposes stable cross-boundary types and ports from
`modules/<capability>/contracts/`. A named kernel may expose `kernel/<concern>/contracts/`.
Contract packages may contain:

- immutable request, result, event, and value types whose semantics belong to that provider;
- inbound use-case protocols and outbound ports;
- typed failure categories at the owning boundary; and
- conformance semantics needed by every implementation.

They do not contain concrete adapters, environment/configuration lookup, framework objects,
global registries, service construction, transport presentation, or unrelated types gathered
because several callers need them. There is no repository-wide `contracts`, `dto`, `models`, or
`services` package.

Wire schemas remain owned by their publishing transport/schema boundary and translate to or from
provider contracts. A type used by multiple capabilities is not automatically “shared”: it stays
with the provider of its invariant. Only a genuinely cross-capability mechanism with independent
semantics becomes a named kernel contract.

Existing single-file `contracts.py` surfaces may remain while they are compatible, but a growing
public contract family becomes a package before it accumulates implementation or unrelated
provider types. The public package `__init__.py` explicitly re-exports supported names.

## Decision 5: public APIs are explicit and compatibility is executable

New package APIs are public only when exported from the owning package's `__init__.py`, documented
as a supported boundary, and covered by contract tests. A non-exported deep module is internal
even if Python can import it. Leading underscores continue to mark private names; importing a
non-underscored implementation module does not make it public by accident.

Existing imports and monkeypatch seams are different: the active facade ledger is the authority
until migration proves they can retire. A facade must preserve importable names, call/return and
error behavior, and named collaborator lookup semantics. A static re-export is insufficient when
tests or consumers patch the old module at call time.

Internal callers migrate to the owning package API and may not add new dependencies on a legacy
facade. External consumers are not declared absent merely because repository search is empty.
Where they cannot be enumerated, removal also requires the responsible public-contract or release
owner to make and record the compatibility decision.

## Decision 6: every facade has a one-way lifecycle

A root compatibility facade is allowed only when all of the following are true:

- the replacement implementation and target owner already exist;
- the facade is registered with exact preserved symbols, consumer groups, executable tests,
  target owner, live successor, introduction and verification commits, and expiry trigger;
- it forwards inward and contains no independent business policy, persistence, discovery,
  process-wide assembly, mutable source of truth, or second implementation;
- new production code is prevented from adopting the facade; and
- every affected closure continues to run its facade and reverse-dependent tests.

The lifecycle remains `active` -> `removable` -> `removed`:

1. **Active:** at least one inventoried internal, test, documentation, or accepted external
   compatibility consumer remains.
2. **Removable:** exact repository search and executable contract evidence show zero legacy
   internal/patch consumers; replacement and reverse-dependent closures are green; and any
   unenumerable external surface has an approved release/deprecation decision.
3. **Removed:** facade deletion, consumer migration, ledger history, build ownership, and closure
   updates land in the same change.

Dates alone never expire a facade. Closing a successor does not silently renew it. If the named
successor is no longer live while the record remains active, ownership repair is required before
further migration. A facade may be retained intentionally, but that is a fresh decided record
with a durable owner and tests, not an indefinitely “temporary” exception.

## Decision 7: build and test topology follows capability-role ownership

Every migrated capability-role boundary is an independently addressable Pants build/test
artifact. “Role boundary” means a capability's domain, contracts, application, or local adapters;
a named kernel, shared adapter, integration, bootstrap root, or public testing kit is an equivalent
owner at its own level.

Source and test generators may still create per-file targets, but each generator or explicit
target set must be scoped to one capability-role owner. No source or test target spans unrelated
owners. A cross-capability integration or system scenario is itself a named test owner with
explicit inputs; it is not an excuse to place unrelated tests in one aggregate.

The checked mapping for each capability/closure names exact Pants addresses for:

1. owned production source targets, separated by role;
2. direct domain, application, adapter, and independence test targets;
3. shared-contract, compatibility-facade, and conformance test targets;
4. statically known reverse-dependent source and test targets;
5. fixture, conftest, harness, schema, generated, and other resource targets actually required;
6. boundary-crossing integration and system-scenario targets; and
7. target dependency edges plus owner, role, and attestation-category tags.

That mapping is repository-owned and drift-detectable. Its checker fails locally when a migrated
file has no owner or multiple incompatible owners, a target address is missing, a closure path and
target sources disagree, a required shared/facade/reverse-dependent test is absent, a fixture or
resource dependency is undeclared, or an import edge has no corresponding target dependency. From
one checkout, a contributor must be able to resolve a capability-role target and enumerate its
complete mapped test subset and transitive inputs without relying on path-name inference or an
external service. Uncertainty returns an explicit incomplete result; it is never treated as an
empty affected set.

Fixture and resource edges are narrow. A pure capability test depends only on the conftest,
fixtures, fakes, schemas, and resources it actually uses. Stateful fixture plugins, broad harness
targets, all-package resources, and unrelated generated artifacts cannot be inherited through a
convenience aggregate. Shared conformance kits remain independently addressable and are depended
on explicitly.

Broad umbrella targets may remain temporarily to preserve packaging or migration workflows, but
they are recorded as migration debt with their spanned owners and replacement targets. They do not
satisfy completion. In particular, the current `//src/beadhive:lib` generator cannot certify a
clean physical boundary merely because Pants generates file-level children beneath it.

### Future capability definition of done

A new or newly migrated capability is physically complete only when the same change provides:

- role-scoped BUILD generators or equivalently fine-grained source targets;
- direct and boundary test targets with narrow fixture/resource dependencies;
- explicit owner, role, and attestation-category tags;
- a closure-mapping row containing the source, direct-test, shared-contract/facade,
  reverse-dependent, integration/system, fixture, schema, and generated-resource addresses;
- checked dependency edges matching source imports and declared boundary relationships; and
- a local drift check proving the mapping is complete and every address resolves.

Incremental closeout records, per migrated owner, the files removed from broad aggregates, old and
new target addresses, direct and reverse-dependent tests, fixture/resource edges, mapping-check
result, and remaining aggregate debt. Repository closeout reaches zero cross-owner source/test
targets and zero unclassified aggregate debt.

## Decision 8: cleanliness is measured by ownership and edges

Repository cleanliness is achieved when one exact tree satisfies all of these conditions:

| Measure | Completion condition |
| --- | --- |
| Root implementation ownership | Zero unowned package-root implementation files. Every root file is `__init__.py`, an active registered facade, or absent. |
| New-root guard | The manifest/AST check rejects an unregistered root implementation and accepts a valid registered facade fixture. |
| Dependency direction | The native checker is green with zero active boundary exceptions. |
| Cyclic ownership | Zero active cycle exceptions and no SCC crossing capability, kernel, adapter, integration, or bootstrap ownership. |
| Facade debt | Every remaining active facade is forwarding-only, has a live owner and successor or retained-facade decision, exact consumers, and green executable tests. |
| Package API | Each migrated owner has explicit public exports; production consumers use those exports or declared provider contracts rather than implementation internals. |
| Build granularity | Every migrated capability-role, kernel, adapter, integration, bootstrap, and testing owner has independently addressable source and test targets; no target spans unrelated owners. |
| Closure-to-target mapping | Every owner has a checked mapping to source, direct-test, shared-contract/facade, reverse-dependent, fixture/resource, and required integration/system addresses. |
| Test ownership | Every owner has direct and boundary test targets whose complete mapped subset is locally computable and fails explicitly when incomplete. |
| Fixture ownership | Tests depend only on the conftests, fixtures, fakes, schemas, harness pieces, and resources they use; dynamic consumers remain explicitly unresolved until modeled. |
| Build dependency edges | Every moved source, test, schema, fixture, and generated artifact has one compatible Pants owner, owner/role tags, and checked dependency edges. |
| Aggregate debt | Broad source/test aggregates are classified migration debt with named replacements; clean closeout has zero cross-owner aggregates and zero unclassified aggregate debt. |
| Behavior | Existing characterization, contract, facade, reverse-dependent, and transport/schema compatibility proofs remain green for the moved surface. |
| Evidence | The baseline/closeout names commit, dirty state, commands, tool versions, ledger counts, SCCs, target addresses, mapping results, paths, and remaining aggregate debt. |

The number of directories, average file size, and the raw count of root files are supporting
signals only. A repository with many neat directories but unowned behavior or hidden reverse
dependencies is not clean. Conversely, an intentional, tested compatibility facade does not make
the repository unclean merely because the public import remains at the package root.

This definition concerns repository topology and local computability. It neither adopts nor
changes a certification timeline, CI activation or promotion rule, CI route, or validation policy.
Those concerns cannot make an incomplete ownership graph appear physically complete, and this ADR
does not make the completed graph authoritative for any CI decision.

## Behavior-preserving migration protocol

Each movement slice must:

1. identify the current authority, public imports, patch points, dynamic discovery, reverse
   dependents, fixture scopes, generated artifacts, and build owners;
2. add or confirm the target contract and characterization tests before moving behavior;
3. move one cohesive owner behind an explicit package API, leaving a ledgered facade where
   compatibility is still required;
4. add role-scoped source/test targets, narrow fixture/resource edges, owner/role tags, and the
   checked closure-to-target mapping in the same transition;
5. demonstrate that old and new paths have identical observable behavior for the supported
   surface; and
6. record the local target-resolution, mapping-drift, dependency-edge, and owned-test results.

A move may reduce debt; it may not hide it by broadening an exception, weakening a test, changing
validation selection, or binding a patchable collaborator too early. Public removal is a separate
compatibility decision after consumer-zero evidence, not a side effect of file movement.

## Rejected alternatives

- **Global layer dumps:** repository-wide `domain/`, `application/`, `services/`, `dto/`, or
  `contracts/` directories erase capability ownership and make unrelated changes share a closure.
- **Directory-only reshuffles:** relocating files without moving authority, dependency direction,
  tests, fixtures, target topology, and build ownership produces cosmetic architecture.
- **One aggregate build target per language tree:** per-file generation beneath a repository-wide
  owner still hides capability-role addressability, tags, test closure, and fixture/resource debt.
- **Generic shared packages:** `common`, `utils`, service locators, and mixed DTO buckets hide the
  provider and turn reuse into ambient coupling.
- **Central adapters by default:** an adapter used by one capability remains local; centralizing it
  early expands reverse dependencies without creating a real shared boundary.
- **Root implementation modules as convenience:** the root is not a staging area. Existing files
  are exact migration debt, and new ones fail the guard unless they are registered facades.
- **Importability as API:** Python's ability to deep-import a symbol is not a support commitment;
  explicit exports and contract tests are.
- **Deletion on repository grep alone:** monkeypatch seams, docs, generated consumers, and external
  compatibility require executable evidence and, where necessary, a release decision.
- **RepoWise as current authority without refresh:** its historical signals can guide inquiry, but
  exact-tip native evidence controls this migration.

## Ownership and consequences

- `bh-50gsn.3` owns repairing the 52 active ledger records so every active item names a live
  successor or explicit retained-facade owner. This ADR does not pre-judge removability.
- `bh-50gsn.4` owns the no-new-unowned-root implementation guard and its positive/negative
  fixtures. It must represent the baseline debt exactly rather than exempting the root wholesale.
- `bh-50gsn.5` owns current-state documentation reconciliation. Commit-specific historical
  evidence remains historical.
- Later physical-refactor molecules own the moves. They consume the Beads v1.3 contract and
  coordination semantics that land first; this ADR does not move or redefine those behaviors.

The result is a capability-oriented modular monolith whose physical layout exposes ownership,
whose dependency direction is enforced, and whose remaining compatibility is explicit and
executable rather than disguised as implementation structure.
