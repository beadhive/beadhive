# Worktree compatibility and removal ledger

Status: active compatibility facade  
Owner: `beadhive-worktrees` capability and root worktree composition maintainers  
Decision: bh-e7s80, 2026-09-28

## Boundary decision

The selected capability is the provider and transport neutral managed worktree module. Its named
ports are `WorktreeManagerPort`, `WorkspaceBindingPort`, `WorktreeInventory`,
`BeadStateLookup`, `ClaimRecords`, and `MergeEvidence`. The native Git manager is the concrete
manager implementation. Root supplies the Beads or argv state adapter, claim record adapter,
GitHub merge evidence adapter, configuration, presentation, telemetry, and lifecycle composition.

The boundary scored 14/14 during the bh-e7s80 closeout:

| Dimension | Score | Evidence |
| --- | ---: | --- |
| Cohesion | 2 | Naming, lifecycle, inventory, classification, and removal policy serve one managed worktree capability. |
| Coupling | 2 | The package depends only on `beadhive-plugins`; root collaborators cross declared ports. |
| Data and side effect ownership | 2 | Manager and binding receipts own mechanics; root explicitly owns configuration and presentation. |
| Port narrowness | 2 | Consumers receive behavior specific protocols rather than root modules or storage types. |
| Replaceable implementations | 2 | Native Git has a conformance kit; state reads have Beads and argv implementations plus an in-memory fake. |
| Dependency direction | 2 | Package boundary tests reject imports of root `beadhive`, transports, subprocess, and test packages. |
| Test closure reduction | 2 | Package tests run from the package artifact; root tests substitute ports and retain only wiring checks. |

Rejected alternatives were moving root rendering/configuration into the package, which would
reverse the dependency, and directory-only reshuffling of the root files, which would add no
enforceable contract. The allowed direction is root and integrations → public
`beadhive_worktrees` contracts → package policy/domain. Package internals never import root.

Observable CLI, MCP, Herdr, Git, branch naming, removal safety, and monkeypatch behavior remain
unchanged. Clean-checkout validation, Git history/rewrite operations, root rendering, config
selection, telemetry, and presenter composition remain root responsibilities.

## Retained facade surface and consumers

`beadhive.modules.worktrees` is the old package import path. It contains imports, `__all__`, and
documentation only. Every retained name below is the identical object exported by
`beadhive_worktrees`.

| Retained names | Current consumers | Removal gate |
| --- | --- | --- |
| `WT_PREFIX`, `BATCH_BRANCH_PREFIX`, `BATCH_LEAF_PREFIX`, `apply_prefix`, `bind_worktree`, `branch_suffix`, `leaf_for_branch`, `sanitize_leaf` | Root branch/leaf composition and compatibility imports | Root and known external consumers import these names from `beadhive_worktrees`; compatibility contract test has zero consumers. |
| `WorktreeBinding`, `WorktreeBranchPolicy`, `WorktreeSpec`, `WorktreeHandle`, `WorktreeRemoved`, `ManagedWorktree`, `BoundWorktree`, `BindingGap`, `WorktreeInventoryRequest`, `WorktreeInventoryResult`, `WorktreeStatusRequest`, `WorktreeStatusResult` | Root composition, inventory, tests, and downstream typed imports | All consumers use the package path and the released package has completed its compatibility window. |
| `WorktreeManagerPort`, `WorkspaceBindingPort`, `WorktreeInventory`, `WorktreeManagerCapabilities`, `NATIVE_CAPABILITIES` | Root manager selection and Herdr binding | Manager/binding selection and Herdr import only package contracts. |
| `NativeGitWorktreeManager`, `NativeGitBranchInspector`, `CallbackWorktreeInventory`, `WorktreeLifecycleService`, `WorktreeInventoryService` | Root adapters and lifecycle/inventory composition | Root composition imports concrete adapters directly and old path has zero consumers. |
| `WorktreeManagerError`, `WorkspaceBindingError` | Root CLI error translation and compatibility imports | Root catches package exceptions directly and old path has zero consumers. |

`beadhive.worktree` is the historical root composition and patch boundary. Extracted operations
are forwarding functions; the implementation owner is named in each forwarder's docstring. The
following surface remains because production consumers still use it:

| Retained names | Consumers |
| --- | --- |
| `locate`, `preview`, `ensure`, `add`, `path_of`, `init_existing`, `remove`, `prune`, `rebind`, `mark_landed`, `mark_abandoned` | CLI, work lifecycle, local loop, dispatcher, and Herdr application services |
| `managed`, `list_cmd`, `status_rows`, `status_cmd`, inventory/classification private forwards | CLI, MCP resources, Herdr inventory, and root compatibility tests |
| `history`, `signature_status`, `commit_messages`, `commit_shas`, `push_branch`, `is_clean`, `dirty_paths`, `current_branch`, `head_sha`, `head_full_sha`, `base_of`, `commit_rows`, safety-ref/rebase/landed/diff/log forwards | Work show, refine, submit, review, merge, dispatch, and batch workflows |
| `run_init`, init fingerprint forwards, process/marker forwards, `clean_checkout` | Worktree CLI, submit/review/merge validation, and validation admission |
| `merge_no_ff`, `merge_conflict_paths`, `merge_with_union`, `try_merge_rebase` and tier helper forwards | Single and group merge workflows |
| `WT_PREFIX`, `BATCH_BRANCH_PREFIX`, `BATCH_LEAF_PREFIX`, `_BEAD_PREFIX`, naming helpers, `wt_dir`, `clone_for_branch`, `in_bead_worktree`, `integration_base`, container helpers | Work lifecycle, work groups, submission, review, merge, scheduling, CLI, MCP, and telemetry |
| `_selected_worktree_manager`, `binding_store`, lifecycle observer forwards, compatibility collaborator attributes | Herdr integration and established root monkeypatch seams |

Removal requires a separately reviewed consumer-zero change. A name cannot be removed merely
because root has a newer internal path; CLI/MCP/Herdr consumers and compatibility tests must first
stop importing or patching it.

## Bead state adapter route

`worktree_state_adapters.BEAD_STATE_LOOKUP` composes `RoutedBeadStateLookup`. It opens the hive's
supervised session through `beads_routing.hive_session`, then uses the `beadhive-core` routing
table's `work.issue.get` and `work.issue.list` operations through
`BeadsSessionBeadStateLookup`. If session selection fails before any read,
`work.beads.route=api+cli-fallback` or `cli` may select `ArgvBeadStateLookup`. The default `api`
route fails closed with the shared actionable diagnostic. Once an API session opens, a failed
read is never replayed through argv.

## Modularization done gate

| Gate | Evidence |
| --- | --- |
| Named port | `beadhive_worktrees.contracts` exports the six ports listed above. |
| Concrete implementations | Native Git manager, callback inventory, rooted claim/merge adapters, and routed Beads/argv state adapters. |
| Enforced dependency direction | `test_worktrees_package_boundary.py`, `test_worktrees_independence.py`, and `scripts/check_package_imports.py`. |
| Isolated package tests and fixtures | `packages/beadhive-worktrees/tests`; package fakes live in `beadhive_worktrees.testing`. |
| Port substituting root tests | `test_worktree_capability_compatibility.py` rejects deep state patches; root classifier tests replace the composed ports. |
| Conformance evidence | Native manager and binding conformance kits in `packages/beadhive-worktrees/tests`. |
| Standalone artifact build/test | `packages/beadhive-worktrees/pyproject.toml`, `BUILD`, and package `justfile`; the package test command does not import or boot root. |

Physical extraction would require publishing `beadhive-worktrees` and `beadhive-plugins` from the
same release train. No additional source move, service boundary, or semantic versioning scheme is
required for the internal module boundary.
