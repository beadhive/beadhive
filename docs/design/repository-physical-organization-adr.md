# ADR: Repository physical organization and compatibility retirement

> Status: **decided** (2026-09-21). Decision bead: `bh-50gsn.2`.

## Context and relationship to existing decisions

The
[`modular-dependency-and-test-closure` ADR](modular-dependency-and-test-closure-adr.md)
already decides the capability-first architecture, package roles, complete allowed dependency
directions, exception metadata, and closure-graduation standard. This ADR extends that decision
with the physical rules it deliberately left open. It does not replace or relax its direction
table.

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

## Decision 7: cleanliness is measured by ownership and edges

Repository cleanliness is achieved when one exact tree satisfies all of these conditions:

| Measure | Completion condition |
| --- | --- |
| Root implementation ownership | Zero unowned package-root implementation files. Every root file is `__init__.py`, an active registered facade, or absent. |
| New-root guard | The manifest/AST check rejects an unregistered root implementation and accepts a valid registered facade fixture. |
| Dependency direction | The native checker is green with zero active boundary exceptions. |
| Cyclic ownership | Zero active cycle exceptions and no SCC crossing capability, kernel, adapter, integration, or bootstrap ownership. |
| Facade debt | Every remaining active facade is forwarding-only, has a live owner and successor or retained-facade decision, exact consumers, and green executable tests. |
| Package API | Each migrated owner has explicit public exports; production consumers use those exports or declared provider contracts rather than implementation internals. |
| Test ownership | Every owner has a present closure row naming direct, contract/facade, shared-contract, reverse-dependent, and required integration/system tests. |
| Fixture ownership | Module-local tests use explicit concern fixtures or injected fakes; ambiguous/dynamic consumers retain the native/full fallback. |
| Build ownership | Every moved source, test, schema, fixture, and generated artifact has one checked Pants owner and correct dependency edges. |
| Behavior | Characterization, contract, reverse-dependent, transport/schema compatibility, and configured full validation are green on the exact tree. |
| Evidence | The baseline/closeout names commit, dirty state, commands, tool versions, ledger counts, SCCs, paths, and validation receipts. |

The number of directories, average file size, and the raw count of root files are supporting
signals only. A repository with many neat directories but unowned behavior or hidden reverse
dependencies is not clean. Conversely, an intentional, tested compatibility facade does not make
the repository unclean merely because the public import remains at the package root.

Selective-test certification is not required to declare physical organization complete. Closure
registration and fail-closed ownership are required; activation still follows the independent
30-change/60-day evidence policy in the modular-dependency ADR.

## Behavior-preserving migration protocol

Each movement slice must:

1. identify the current authority, public imports, patch points, dynamic discovery, reverse
   dependents, fixture scopes, generated artifacts, and build owners;
2. add or confirm the target contract and characterization tests before moving behavior;
3. move one cohesive owner behind an explicit package API, leaving a ledgered facade where
   compatibility is still required;
4. update the import ledger, closure registry, fixtures, generated evidence, and BUILD ownership
   in the same transition;
5. demonstrate that old and new paths have identical observable behavior for the supported
   surface; and
6. pass the configured developer/submit and integration boundary appropriate to the bead.

A move may reduce debt; it may not hide it by broadening an exception, weakening a test, changing
validation selection, or binding a patchable collaborator too early. Public removal is a separate
compatibility decision after consumer-zero evidence, not a side effect of file movement.

## Rejected alternatives

- **Global layer dumps:** repository-wide `domain/`, `application/`, `services/`, `dto/`, or
  `contracts/` directories erase capability ownership and make unrelated changes share a closure.
- **Directory-only reshuffles:** relocating files without moving authority, dependency direction,
  tests, fixtures, and build ownership produces cosmetic architecture.
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
