# beadhive-worktrees

The provider- and transport-neutral managed-worktree capability described in
[the package-class ADR](../../docs/design/package-class-library-vs-plugin-adr.md) (section 5)
and the [worktree manager / Herdr binding ADR](../../docs/design/bh-mr9tk.2-worktree-manager-herdr-binding-adr.md).
This package depends only on `beadhive-plugins` — for the `CapabilityRef` / `CapabilityKey` /
`bind_application_port` capability-slot machinery it uses to declare its own `worktree.manager`
slot — never on the root `beadhive` distribution.

Importing it describes branch/leaf naming policy, typed request/result contracts, ports, and
the native Git adapter; it never selects or binds a provider itself. Selection and binding are
root composition's job exclusively, through `beadhive_plugins.binding.bind_application_port`.

## Modules

- `beadhive_worktrees.domain` — branch/leaf naming policy (`WT_PREFIX`, `bind_worktree`,
  `branch_suffix`, `leaf_for_branch`, `sanitize_leaf`) and the typed request/result contracts
  (`CreateWorktreeRequest`, `RemoveWorktreeRequest`, `ProvisioningResult`, `ManagedWorktree`,
  and the inventory/status request and result types).
- `beadhive_worktrees.contracts` — the outbound ports (`WorktreeProvisioner`,
  `WorktreeInventory`) and the `worktree.manager` capability declaration (`WORKTREE_MANAGER`,
  `WORKTREE_MANAGER_KEY`) — exactly one configured manager per hive, with native Git as the
  built-in default provider.
- `beadhive_worktrees.application` — lifecycle sequencing over replaceable provisioner adapters
  (`WorktreeLifecycleService`) and the typed inventory query boundary
  (`WorktreeInventoryService`), preserving the plugin-first fallback seam unchanged.
- `beadhive_worktrees.adapters` — the native Git provisioner
  (`NativeGitWorktreeProvisioner`) plus `native_worktree_manager_provider_binding`, the plugin
  fallback adapter (`PluginWorktreeProvisioner`), and the inventory callback adapter
  (`CallbackWorktreeInventory`).

## Root compatibility facade

Root keeps a forwarding facade at the old `beadhive.modules.worktrees` import path so existing
consumers keep working unchanged — every name previously importable from that module still
resolves to the identical object defined here (`docs/MODULES.md` principle 8: compatibility
facades are deliberate migration tools). Root composition (`beadhive.worktree`) binds the
native provider to the declared `worktree.manager` slot with `bind_application_port` and
injects the resulting port where the worktree lifecycle service is composed — this package
never constructs or selects a provider itself.
