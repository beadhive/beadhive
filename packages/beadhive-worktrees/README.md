# beadhive-worktrees

The provider- and transport-neutral managed-worktree capability described in
[the package-class ADR](../../docs/design/package-class-library-vs-plugin-adr.md) (section 5)
and the [worktree manager / Herdr binding ADR](../../docs/design/bh-mr9tk.2-worktree-manager-herdr-binding-adr.md).
This package depends only on `beadhive-plugins` — for the capability-slot machinery and the
`worktree.manager` / `workspace.binding` slot declarations (`beadhive_plugins.worktree_slots`) it
implements — never on the root `beadhive` distribution.

Importing it describes branch/leaf naming policy, the `WorktreeSpec` / `WorktreeHandle` contract,
the manager capability flags, ports, and the native Git manager; it never selects or binds a
provider itself. Selection (`worktrees.manager`, native only) and binding are root composition's
job exclusively, through `beadhive_plugins.binding.bind_application_port`.

## Modules

- `beadhive_worktrees.domain` — branch/leaf naming policy (`WT_PREFIX`, `bind_worktree`,
  `branch_suffix`, `leaf_for_branch`, `sanitize_leaf`); the manager contract types
  (`WorktreeSpec`, `WorktreeHandle` with its `bindings` map, `WorktreeRemoved`,
  `WorktreeManagerError`); the capability flags (`WorktreeManagerCapabilities` with `binds` and
  `remove_releases_bindings`, `NATIVE_CAPABILITIES`); and the inventory/status types.
- `beadhive_worktrees.contracts` — the slot ports specialised to those types
  (`WorktreeManagerPort`, `WorkspaceBindingPort`, `WORKTREE_MANAGER_KEY`), re-exports of the
  slot identities (`WORKTREE_MANAGER`, `WORKSPACE_BINDING`), the create-observer port
  (`WorktreeCreateObserver`), the attach pre-check port (`BranchInspector`), and
  `WorktreeInventory`.
- `beadhive_worktrees.application` — `WorktreeLifecycleService`, which sequences
  create/attach/remove through exactly one selected manager (no plugin-first fallback), runs
  create observers around it, and guards attach's start-point intent (an attach never moves a
  branch tip); and the typed inventory query boundary (`WorktreeInventoryService`).
- `beadhive_worktrees.adapters` — the native Git manager (`NativeGitWorktreeManager`, with
  separate `create` / `attach` / `remove`), `NativeGitBranchInspector`,
  `native_worktree_manager_provider_binding`, and the inventory callback adapter
  (`CallbackWorktreeInventory`).

## Root compatibility facade

Root keeps a forwarding facade at the old `beadhive.modules.worktrees` import path so existing
consumers keep working unchanged — every name previously importable from that module still
resolves to the identical object defined here (`docs/MODULES.md` principle 8: compatibility
facades are deliberate migration tools). Root composition (`beadhive.worktree`) binds the
native provider to the declared `worktree.manager` slot with `bind_application_port` and
injects the resulting port where the worktree lifecycle service is composed — this package
never constructs or selects a provider itself.

The exact retained root surface, consumers, removal gates, boundary score, and modularization
done-gate evidence are recorded in the
[worktree compatibility and removal ledger](../../docs/design/worktree-compatibility-removal-ledger.md).
Root's composed `BeadStateLookup` prefers the supervised Beads service and uses the shared
`work.beads.route` decision for its explicitly named argv compatibility fallback.
