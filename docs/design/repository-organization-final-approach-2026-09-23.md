# Repository organization: final approach and plan-update instructions

Date: 2026-09-23 UTC.

This document reconciles the [first review](repository-organization-review-2026-09-23.md)
and [second review](repository-organization-second-review-2026-09-23.md). Use it as the
instruction set for updating the implementation plan, architecture decisions, dependencies,
and acceptance criteria. It specifies the intended changes; it does not report them as
implemented or grant implementation, review, or merge approval. No live tracker records were
consulted for this synthesis. Counts and branch observations in the reviews are historical
evidence and must be refreshed at execution time.

## 1. Adopt one architecture, delivered through bounded landings

Keep capability ownership, inward dependencies through ports, narrowly shared kernel
mechanisms, product-specific integrations, explicit composition, and executable compatibility.
Preserve externally observable behavior throughout the refactor.

Adopt the second review's iterative delivery model and the first review's architectural
finish conditions. **Each checkpoint must reach `main` independently. Repository-wide
completion remains the destination, but is not the finish claim of the current increment.**

The current increment covers config, work, worktrees, planning, their necessary effect and
cycle prerequisites, and the planned transports, daemon/bootstrap, Frame Bridge, and Herdr
consolidation. Freeze its precise path/family coverage before implementation. A cycle cut in
another family does not mean that family's physical migration is included or complete.

Residual release/contracts, fleet/HQ/backup administration, non-Herdr integration runtimes,
agent/state readers, telemetry, and other uncovered families remain visible for later
increments. Do not silently absorb them into this increment. Do not describe their continued
presence as repository cleanliness merely because they appear in a ledger.

Use two distinct completion claims:

| Claim | Required result |
| --- | --- |
| Checkpoint or increment complete | Declared scope implemented, independently reviewed, validated and landed on `main`; all obligations in that scope discharged; residual obligations explicitly accounted for; architectural budgets do not regress |
| Repository organization complete | No unallocated delivery scope, no legacy implementation or composition debt at the package root, no active boundary or cycle exceptions, no import cycles, and complete build/test/resource ownership across all retained owners; remaining facades are forwarding-only, explicitly owned, supported, and tested |

Repository-wide completion must include existing packages that need only build/test ownership
work. Closing the current increment does not close that wider objective.

## 2. Replace the long-lived accumulation branch with checkpoint integration

Represent checkpoints as independently landable delivery units linked by explicit
dependencies. Keep the umbrella workstream for tracking and reporting, without making its
eventual closure the only route to `main`. Use supported integration mechanics to achieve
this; verify the resolved integration destination rather than assuming a label or a
parent-child relationship changes it.

Each checkpoint follows this sequence:

1. Start from current `main` with prerequisite checkpoints already landed. Record the base
   commit/tree, behavior baseline, scope, compatibility obligations, and remaining debt.
2. Implement one coherent boundary improvement. Local commits provide recovery points;
   they do not count as delivered checkpoints.
3. Complete focused checks, required lifecycle gates, independent review, and an exact-tree
   evidence packet. An intermediate state must remain usable and behaviorally compatible.
4. Integrate through the normal workflow onto `main`, validate the actual integration
   result as required, and record its commit/tree. A container-only merge is still pending
   delivery.
5. Refresh the manifest, debt budgets, actual-versus-intended documentation, and the next
   checkpoint's inventory from that landed tree. Re-estimate and adjust the remaining plan.

Do not begin dependent movement against an unlanded predecessor. Independent preparation can
proceed concurrently; parallel implementation requires disjoint ownership and explicit
coordination of shared manifests, BUILD targets, compatibility modules, and tests. Serialize
same-file config, work, worktree, and Herdr chains.

At minimum, every phase lands separately. Split large phases into coherent family checkpoints
when they can meet their own acceptance criteria. Do not replace one workstream-long branch
with another phase-long branch that accumulates unrelated completed families. Conversely,
combine adjacent tiny steps in one family when the pilot demonstrates that separate full
gates and reviews dominate their cost. Preserve mandatory gates when adjusting granularity.

### Checkpoint sequence

| Checkpoint | Deliverable and exit condition |
| --- | --- |
| Foundation landing | Reconcile and land the existing phase-one decisions, guard, ledger repairs, documentation, and historical-proof repairs onto current `main`. Recheck merge viability and configured gates; the second review's clean merge is not current acceptance evidence. Preserve already completed work and its history. |
| Enforcement and policy | Land the role/export rules, frozen root allowances, truthful facade classification and lifecycle checks, exception budgets, allocation reporting, shared acceptance contract, and patch-seam policy described below. Establish the build-graph checking mechanism before physical migration depends on it. |
| Necessary effect prerequisites | Land bounded cuts and port seams needed by the next family. Preserve external behavior prerequisites and existing ordering decisions. Identify high fan-in engine, logging, and telemetry collaborators before their consumers move. |
| Config pilot | Complete config end to end: implementation ownership, API, compatibility seams, role-scoped targets, graph checks, tests, resources, and installed-artifact proof. Land the result on `main` and measure implementation, validation, review, and integration cost. |
| Capability checkpoints | Use the pilot to size planning, work, and worktrees checkpoints. Land each coherent family or subfamily with its prerequisites, tests, and topology. Finish the current increment's capability scope before its dependent transport consolidation. |
| Transport and integration checkpoints | Land the planned CLI/MCP, daemon/operator, bootstrap/Frame Bridge, and Herdr changes in dependency order, splitting independent surfaces where useful. Include remaining assigned cycle cuts and installed-entrypoint/resource proof. |
| Audit and increment closeout | Audit the landed increment, make evidence-backed cleanup decisions, land any approved required follow-ons, then publish an exact-tree closeout with explicit residual repository-wide scope. |

The config pilot is the first complete physical family migration. Pull forward its genuinely
required prerequisite work, but do not make unrelated cycle elimination a prerequisite.
If existing external ordering blocks it, retain that block until its owner explicitly changes
the dependency after overlap analysis. The plan must expose this constraint rather than
silently bypassing it or claiming the pilot is immediately runnable.

## 3. Make scope exhaustive without planning every symbol up front

Extend the existing `root-module-ownership.toml`; do not create a competing root inventory.
Account for every root path in the reviewed baseline and reconcile changes on current `main`.
The reviews counted 212 non-marker paths; that is a reconciliation reference, not a permanent
hard-coded completeness count.

For each path record its truthful current classification, destination owner and role, delivery
allocation or explicit `unallocated` status, current-increment inclusion, and facade or
retirement disposition. Keep the package marker distinguished from implementation. Mixed
families may name a destination split, with symbol-level detail supplied by the family's
inventory immediately before movement.

Require complete allocation for each checkpoint before it starts and for every family included
in the current increment. Permit explicit `unallocated` rows outside that increment. Report
their count and scope at every landing; require zero only for repository-wide completion.
Name an accountable planning owner for deferred scope even when implementation is unallocated.
After the pilot, prioritize subsequent increments from this inventory without delaying the
already bounded increment to decompose all of them.

Account for retained packages, public conformance support, and resources through the existing
ownership and closure mechanisms. Give unchanged owners explicit topology-only work in the
appropriate increment. Avoid a second hand-maintained source/import inventory.

## 4. Land enforcement before relying on it

### Roles, exports, and concrete effects

Amend the modular dependency ADR and physical organization ADR together. Explicitly classify
capability-local adapters, capability/package API entry modules, and public testing/conformance
support. Teach the checker those roles with positive and negative fixtures. None of these
surfaces may evade checking by falling back to `legacy`.

Capability APIs expose contracts, domain types, and use-case entry points. Cross-owner imports
must resolve through declared public exports, including supported existing API modules.
Re-exporting a symbol does not change its architectural role or legalize an otherwise forbidden
dependency. Permit cohesive internal imports within the approved role rules.

Concrete capability adapters belong under that capability's adapters and are wired only by
permitted composition code. The public capability API must not load or export them. Correct
the worktrees API's concrete-adapter exports as part of this enforcement change; inventory
consumers and preserve any established external compatibility through an explicit outward
transition surface if needed. Do not silently break supported imports to satisfy the checker.

Check contracts as well as domain/application code for forbidden framework and concrete-effect
imports. Supplement static checks with independence tests for process, state, network, and
discovery effects that imports alone cannot prove absent.

### Root freeze, facade truth, and debt budgets

Pin allowed legacy/composition paths to a reviewed baseline artifact and enforce it. Adding a
manifest row cannot authorize new root implementation. New root modules must satisfy the
approved facade rule, with an existing replacement; changing the legacy allowance requires an
explicit policy amendment. Preserve removed paths as history so deletion cannot silently
authorize their reintroduction.

Reclassify `work.py` and `worktree.py` as legacy implementation until their behavior has moved.
Preserve their compatibility obligations while correcting the label. Reconcile this
classification correction explicitly against the baseline; it is not new implementation debt.

Check facade bodies conservatively: imports, declarative constants, aliases, and forwarding
wrappers, with a narrowly specified allowance for call-time collaborator lookup. Reject
business policy, effect execution, and process-wide assembly disguised as facade code.
Reconcile public names against the preserved-symbol/export inventory. AST shape is necessary
evidence, not a substitute for execution and patch-contract tests.

Implement and test the full lifecycle:

| Facade state | Presence and evidence |
| --- | --- |
| Active | Exists, forwards to an existing replacement, has accurate compatibility and ownership records, and gains no new production consumers |
| Removable | May remain while approved removal is pending; satisfies forwarding rules and has consumer/release evidence permitting deletion |
| Removed | Is absent; historical record and removal evidence remain valid without causing a missing-file error |

Retain durable facades only by an explicit supported-API ownership decision. Unknown external
consumers justify continued compatibility, not an unsupported assertion that deletion is safe.

Freeze the active boundary-exception ID set and permitted edge scopes as well as the count.
Require non-increase at each landing, and prevent replacing a retired exception with another
under the same count or broadening an existing record. Apply equivalent no-regression checks
to cycles and root debt; record achieved reductions for subsequent checkpoints while retaining
the historical baseline. Policy amendments must be explicit and reviewed.

Every moved implementation must have zero imports of package-root modules, including indirect
escape routes through re-exports or unmodeled dynamic loading. Use narrow ports for remaining
root collaborators. Approved outward compatibility/composition wiring must stay explicit and
must not create new boundary exceptions to make a move pass. A surrounding legacy SCC may
remain temporarily only if the moved surface meets these rules and the remaining edges retain
accurate removal obligations.

Keep ordinary source checks independent of a live tracker. At lifecycle handoff, separately
verify active obligations against a revisioned snapshot or live audit. Missing or stale
lifecycle evidence is incomplete evidence, not a passing result.

## 5. Decide compatibility seams before movement

Amend the physical ADR's compatibility rule once, then classify each family's seams in its
pre-move inventory:

- **Supported or plausibly external seams:** preserve facade-side call-time lookup and pass
  narrow collaborators inward. This limited compatibility wiring is allowed; application code
  still cannot import the facade, and the facade must not become a process-wide service locator.
- **Internal test seams:** mechanically retarget tests to the new owner's ports or fakes in the
  same change as the implementation move. Preserve assertions, exercised behavior, error paths,
  and relevant failure sensitivity. This is consumer migration and does not wait for the audit.
- **Uncertain seams:** retain compatibility until evidence resolves their status. Lack of a
  known internal caller does not prove absence of external consumers.

Preserve signatures, type/model identity, validators, aliases, serialization, errors, mutable
state ownership, lazy binding, and cleanup. Test real execution through legacy paths for
supported seams. Keep test deletion, consolidation, reduced behavioral coverage, and changed
test policy behind the evidence-and-decision audit boundary.

## 6. Prove build ownership from the live graph

Retain the physical ADR's independently addressable capability-role targets and complete local
test-closure requirement. Amend its mechanism to use property checks over the resolved Pants
graph, rather than storing a duplicate list of every inferred dependency or generated address.

Establish the schema/checker contract in the enforcement checkpoint and prove it end to end
with config. Every subsequent movement must update its BUILD ownership, tags, closure selectors,
and non-import inputs in the same change, keeping these properties green:

- Every source and test file has exactly one effective source-owning target, accounting for
  generated targets without double-counting their generator. Owner/role tags match declared
  ownership. Existing test locations may use explicit ownership metadata.
- Completed source/test generators are confined to one owner and role. Packaging and
  composition aggregates remain distinct and may depend on several owners.
- Every closure selector resolves to existing addresses. The checker can enumerate direct,
  contract, facade, reverse-dependent, and integration coverage for the moved surface.
- Schemas, fixtures, fixture plugins, templates, generated assets, and runtime resource lookup
  inputs are explicitly declared where inference cannot discover them.
- Stateful harness and fixture dependencies are reachable only through appropriately tagged
  stateful test targets. Pure lanes remain independent of them.

Capture resolved target addresses and relevant dependencies as exact-tree validation evidence;
do not maintain them as a second authoritative import graph. Per-file generation inside an
owned role is sufficient; handwritten BUILD files per source file are unnecessary.

The current broad generators cannot become fully compliant merely by introducing a checker.
Record their remaining coverage as explicit transitional topology debt. Enforce full properties
for the pilot and each completed owner, and prevent completed paths returning to broad legacy
ownership. Global uniqueness and selector validity should hold throughout; global owner/role
separation is a repository-wide finish condition. No broad generator may quietly absorb newly
migrated code. Preserve validation and attestation semantics while target addresses change.

## 7. Repair ordering and unresolved decisions without widening scope

Make these amendments explicit in the plan:

- Make the prerequisite-cycle closeout depend on host-configuration injection as well as all
  other substantive work it certifies. Every later closeout must transitively cover its scope.
- Assign cycle records to their actual removal checkpoints, including host provision
  (`cycle-edge-006`), host retirement (`cycle-edge-007`), and runtime/local loop
  (`cycle-edge-026`). Include the necessary cuts in this increment without implying complete
  relocation of those families. Keep later work/worktree, operator/daemon, and Herdr cuts
  assigned to their real removal checkpoints.
- Refresh current destinations, expiry triggers, and accountable successors for both cycle
  and facade records. No checkpoint may close while active obligations still point to its
  completed scope. Preserve introduction dates, historical proofs, and historical baselines.
- Preserve native coordination and developer-feedback dependencies. Changing the latter's
  phase-wide ordering requires an explicit owner decision and overlap analysis; passing a
  feedback subset never replaces a full correctness gate.
- Resolve planning render/verify and MCP-helper overlaps by current symbols and authority
  before the planning checkpoint. Use the current decision ledger as the operational record,
  reconcile contradictory descriptions there, and retain old baseline dispositions as history.
  Do not adopt and exclude the same implementation in different plan sections.
- Give the judgment-envelope question a current disposition: retain capability-owned
  envelopes unless a common invariant justifies a shared contract. Record any shared-contract
  decision separately, keep impure fact collection outside pure evaluation, and defer new
  judgment-engine behavior or schema normalization to separately authorized work.

These decisions do not require every legacy cycle to disappear before the pilot. Recompute
the graph at each checkpoint; the reviews' cut-set simulation describes their inspected tree,
not a perpetual guarantee about new code.

## 8. Give every movement one executable acceptance contract

Reference a shared checklist from every movement task and expose a small, documented set of
commands for architecture/exports, facade bodies, budgets, allocation/build-graph properties,
focused closures, and required full gates. Add negative fixtures for the rules the commands
claim to enforce. Prose-only declarations of cleanliness are insufficient.

Each checkpoint's evidence must establish:

| Obligation | Evidence |
| --- | --- |
| Behavior and public API | Before/after contract and consumer inventory; stable outputs, errors, signatures, identities, serialization, and supported exports |
| Authority and effects | Port conformance across success, refusal, and partial failure; preserved ordering, claim arbitration, transaction/compensation, idempotency, state, and resource lifecycle |
| Compatibility | Real legacy-import and supported-patch execution; mechanical internal-consumer migration; approved retirement or explicit retention |
| Architecture | Role/export, zero-root-import, facade-body, exception-budget, root-freeze, and allocation checks on the actual candidate |
| Build and packaging | Live target/closure resolution, declared fixture/resource inputs, reverse dependents, generated-contract drift checks, and installed wheel/PEX/console-entrypoint proof where affected |
| Validation | Baseline and focused closure results plus configured `just check` and `just check-all` at their required lifecycle boundaries; unresolved dynamic inputs receive the broader required lane |
| Provenance and delivery | Base/candidate/integration commit and tree, dirty state, commands and tool versions, results/skips, elapsed cost, residual obligations, and recovery anchor |

Record existing expected failures precisely. New or materially changed failures block landing;
unchanged expected failures follow repository policy and remain visible. Reuse validation only
under existing exact-tree/attestation rules. The pilot measures cost to improve task sizing,
not to justify weakened gates.

Distinguish local recovery commits from landed checkpoints. Once a checkpoint reaches shared
`main`, use the normal forward-fix or reviewed revert process; do not rewrite shared history.
If validation exposes an unplanned behavior decision, hold that affected checkpoint and replan
it while preserving already landed progress.

## 9. Reconcile documentation and close the increment honestly

Update the architecture documents with this precedence:

1. The amended modular dependency ADR governs allowed directions and symbol roles.
2. The amended physical organization ADR governs placement, public exposure, compatibility,
   retirement, build/test completion, and incremental versus global finish conditions.
3. `MODULES.md` explains the actual landed architecture and clearly separates intended future
   placement. Identify the governed region and remaining legacy scope without overstating
   repository-wide implementation.
4. Manifests and checks establish inventory and evidence; accepting a row does not override
   an architectural decision. Historical reviews, baselines, and proof packets stay historical.

Use Frame Bridge naming for the core-owned transport. Show local adapters and public testing
support. Distinguish shared schema-generation machinery from semantic publishers, packaged
resources, and their runtime lookup paths. Refresh this map at each landing.

Run the post-refactor test audit on the landed increment's exact tree. Preserve candidate-level
fault/overlap evidence and GO/PARTIAL-GO/NO-GO decisions. Compatibility tests can remain necessary
after their implementation moves. Approved follow-ons required by the increment's acceptance
become explicit blockers of its closeout; land them as further checkpoints. Recommendations
outside that scope receive explicit deferred dispositions rather than silently extending it.

The final packet must list what landed, what remains, unallocated scope, retained facades,
active exceptions, topology debt, and the next increment's planning ownership. Validate the
packet-inclusive candidate and actual integration result: an earlier embedded source SHA
cannot certify a later documentation-inclusive tree.

The plan update is complete when its dependencies, landing destinations, scope statements,
shared acceptance criteria, ADR amendments, deferred obligations, and closeout claims all
express this approach consistently. Return a concise change summary and identify any unresolved
symbol-level or release decision that blocks a particular checkpoint. Do not reopen the chosen
iterative delivery model or claim repository-wide completion from a bounded increment.
