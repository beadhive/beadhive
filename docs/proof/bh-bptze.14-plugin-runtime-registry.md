# `bh-bptze.14` built-in plugin runtime registry cut

## Boundary and baseline

The exact starting revision was `9dc7c10549712c42b5a38b020dcb75c26e7652c1` with tree
`6bddb1db53c64e34893d85de1e9e26e4dfe8f6ee`. It was clean and Good-signed. The strict
pre-edit `just check` passed 7,643 tests with 11 skips and the existing config-fragment
serializer warning. The import checker reported 281 files, 2,463 edges, five owned SCCs,
82 cyclic modules, a largest SCC of 64 modules, and 272 cyclic edges.

The narrow catalog/resolver boundary scores 13/14 (`2,2,2,2,1,2,2`) for cohesion, coupling,
state/effect ownership, contract narrowness, replaceability, dependency direction, and test
closure. `plugin_runtime_catalog.py` owns immutable ordered runtime facts; the compatibility
registry owns resolution through Python's standard import machinery. It owns no mutable cache,
configuration, lifecycle policy, installer behavior, or plugin implementation.

This is a support boundary for the existing compatibility facade, not a claim that the plugin
runtime is now a standalone capability module. Rejected alternatives were moving the five plugin
implementations, introducing a service locator, copying their `PLUGIN` objects into the catalog,
or changing configuration and lifecycle composition.

## Compatibility invariants

The catalog order is exactly `orca`, `observaloop`, `hitch`, `herdr`, `repowise`.
`beadhive.plugins.registry()` remains a public monkeypatchable function and returns the exact
`PLUGIN` object held by each resolved module. It invokes `importlib.import_module` on every call,
so Python retains ownership of module caching, a reloaded module is visible to the next call,
and import or missing-attribute failures cross the facade unchanged. The public frozen `Plugin`
constructor and manifest, enablement, CLI, onboarding, readiness, lifecycle, worktree, and Herdr
projection paths remain unchanged.

## Structural result

The five static edges from `beadhive.plugins` to the built-in implementations are absent. The
checked graph is 281 files and 2,460 edges with eight owned SCCs, 59 cyclic modules, a largest
SCC of 35, and 155 cyclic edges. The new cycle digest is
`f60b8e61436c0a28909f9d20ce9c3d1e500fe6961c76c54caae1c1e9969f7b09`.

The fail-closed checker proved `cycle-edge-013` through `cycle-edge-016` unused. Removing the
inverse registry imports also made the surviving `repowise_plugin -> plugins` dependency
acyclic, so `cycle-edge-022` is no longer an active exception. All five IDs remain in the ledger
as removed audit records; every still-active exception remains checked against its exact edge.

## Focused validation evidence

- catalog, identity, cache/reload visibility, ordered failure, and manifest contracts: 25 passed;
- registered `kernel.plugins` closure: 61 passed;
- registered Herdr closure: 316 passed, one skipped;
- registered Hitch closure: 146 passed;
- registered Observaloop closure: 92 passed, one skipped;
- registered Orca closure: 125 passed;
- registered RepoWise closure: 77 passed, one skipped; and
- CLI, onboarding, readiness, Herdr view, and worktree-hook compatibility: 85 passed with
  183 unrelated worktree cases deselected.

The authoritative post-change full gate is intentionally left to the dispatcher-controlled
readiness boundary; these focused checks do not replace it.
