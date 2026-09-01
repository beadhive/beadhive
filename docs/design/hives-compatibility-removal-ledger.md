# Hives compatibility and removal ledger

Status: active compatibility contract for `bh-bptze.2`

The supported application boundary is `beadhive.modules.hives.HiveLifecycleService`. It owns
canonical hive identity plus typed register, discover/list/status, onboard, readiness, retire,
and reclaim requests/results. The module depends inward on four narrow ports:
`HiveRegistry`, `WorkspaceRealizer`, `DependencyProbe`, and `LifecyclePublisher`.
`beadhive.hive_services` is the outer production composition and is not part of the module.

## Boundary and dependency rule

Dependencies point from CLI/MCP and concrete legacy adapters to `modules/hives`; the module does
not import Typer, FastMCP, config, Git workspace, Dolt/beads, plugin discovery, registry storage,
or legacy hive/retire implementations. `AcceptedWorkspaceRealizer` consumes the landed
`bh-cgcg` root choke point (`identity.workspace_root`, including the established
`hive.workspace_root` compatibility patch seam). It does not recreate root precedence or mutate
the externally owned `bh-cgcg` implementation. Worktree migration remains outside this bead.

`KernelHiveLifecycle` calls the established transport-neutral onboard and retirement execution
seams. The retained public facades wrap those same seams with their historical terminal rendering
and `typer.Exit` behavior, while CLI and MCP project the typed service results independently.
Neither the module DTOs nor the kernel adapter carry machine mode, JSON envelopes, rendered text,
streams, or exit codes. Registered-hive pagination cursors, collection-to-list conversion,
timestamps, and the `schema_version`/`command` wire envelope stay in the legacy identity, CLI,
and MCP outer adapters; the module exports only semantic identity/page/discovery values. The
execution paths already bind plugin manifest subscriptions with
`plugins.action_composition`,
`plugins.onboard_participants`, and `plugins.retire_observers`, then deliver with the kernel
`LifecycleDispatcher`. Manifest `DeliveryPolicy.criticality` remains authoritative: blocking
onboard subscriptions can fail the action, while retirement observers remain best effort and
warn without undoing completed core retirement. The adapter does not inspect nullable plugin
callbacks or create a second event mechanism.

## Retained compatibility surfaces

| Surface | Current consumers and patch contract | Removal gate |
| --- | --- | --- |
| `beadhive.hive` | `onboard` remains the rendering/exit compatibility facade; `execute_onboard` is the structured application seam. Installer/DAG patch points and direct library callers remain supported. | All production callers use typed requests and all semantic test patches target ports/adapters; the facade inventory is zero. |
| `beadhive.hive_identity` | Public identity imports plus the `registry.hives` monkeypatch seam. Semantic affiliation/record policy resolves to `modules.hives.domain.identity`; legacy cursor, timestamp, pagination, and versioned `hive list` wire projection remain here. | No external/public facade consumers and dedicated transport adapters own every wire projection. |
| `beadhive.hive_ready` | `Check`, `scan`, `ready_payload`, and `run_check`; CLI retains the `run_check` patch seam while the facade enters `HiveLifecycleService`. | Readiness probe adapter contracts replace all deep check patches and transport tests patch the service port. |
| `beadhive.onboard` | Step DAG, `Ctx`, payload/text compatibility, installer and raw-`bd` seams. `bh-8kn42` retains ownership of the raw-`bd` consolidation. | The typed lifecycle adapter owns the whole real implementation and `bh-8kn42` has landed/adopted; no deep production import remains. |
| `beadhive.registry` | Fleet/host config persistence, resolution, prefix/kind policy, and 60-importer compatibility surface measured by `bh-bptze.1`. | Registry storage adapter contract coverage exists, dynamic seam inventory is zero, and config persistence ownership is unchanged. |
| `beadhive.retire` | `retire_hive`/`reclaim_hive` retain exact rendering and exit behavior over structured `execute_retire_hive`/`execute_reclaim_hive` plans. Safety, backup, teardown, archive/purge, and plugin patches remain. | Worktree port migration has landed, safety/backup adapters are explicit, and no consumer imports retirement internals. |
| `beadhive.hive_services` | Production composition for current CLI/MCP and compatibility facades. | Bootstrap owns concrete adapter construction and no legacy adapter remains. |

No surface above is authorized for deletion by this bead. The removal gate requires a refreshed
semantic consumer inventory, equivalent contract/reverse-dependent tests, and a separate
reviewed change. Directory movement or a declining substring count is not sufficient evidence.

## Test closure

Pure tests under `tests/unit/modules/hives` construct fakes for all four ports and import the
module while filesystem, plugin discovery, network, process, thread, and outer-runtime imports
are refused. `tests/contracts/test_hive_lifecycle_compatibility.py` covers the real composition,
accepted workspace-root adapter, module-owned identity policy, shared CLI/MCP service entry, and
kernel lifecycle binding. Silent-fake transport tests prove CLI human/JSON and MCP projections
derive from typed results without relying on adapter output. Reverse dependents retain
identity/lifecycle JSON, plugin-kernel,
CLI/MCP resource, onboard, readiness, retirement, reclaim, registry, and workspace-root tests.
The registered closure supplements but never replaces `just check`.
