# CLI compatibility and removal ledger

Status: active compatibility contract for `bh-3qkmk.2`

The supported transport boundary is `beadhive.adapters.cli`. The canonical operation catalog
owns operation identity, eligibility and request/result shape; the adapter owns Typer paths,
aliases, options, prompts, rendering, exits and help. `beadhive.cli` is the process composition
root. It binds application handlers, optional integration apps and the two transport mechanics,
then `project_cli_tree` replaces every provisional registration with the 208 catalog-derived
command/alias declarations and 36 catalog-derived parent declarations.

The generated tree deliberately retains callback objects and module-level Typer app identities.
That preserves direct Python calls and test/third-party monkeypatches while capability application
handlers finish moving out of the legacy flat modules. It is not permission for new behavior to
be implemented in `cli.py`.

## Explicit non-operation surface

| Path | Classification | Reason and removal rule |
| --- | --- | --- |
| `<root>` | transport mechanic | Owns global routing, setup gating, startup migrations/warnings, telemetry lifecycle, eager version and shell-completion options. It remains transport-owned. |
| `hive sync` | transport compatibility mechanic | A callback supplying the historical default for a Typer group. The typed catalog operations are `hive sync peers` and `hive sync remotes`. Remove only after the bare-group compatibility contract is retired in a separately reviewed CLI change. |

`PROJECTION_EXCLUSIONS` is explicitly empty. Every public leaf is catalog eligible; a future
exclusion must name its exact path and rationale in `adapters.cli.declarations` and update the
projection tests. Optional plugin apps remain lazily selected by plugin composition before tree
projection; generation does not import, enable or construct an unavailable integration.

## Retained compatibility handlers

All rows are `active`. The CLI adapter is steward. The introduction baseline and last pre-move
verification are `322c3e4573aee6dc2ee3870af462dc4ab935941f`; the implementation move is owned by
`bh-3qkmk.2`. Removal requires a separate reviewed change after the named target owns behavior,
the production/test patch inventory for every listed symbol is zero, and the CLI compatibility
closure remains byte-compatible.

| Legacy surface and preserved symbols | Target owner | Characterization and reverse-dependent closure | Concrete expiry trigger |
| --- | --- | --- | --- |
| `beadhive.cli` root/seat/report: `_root`, `role_cmd`, `statusline_cmd`, `sync_cmd`, `hub_cmd`, `report_cmd`, `report_target_cmd`, `escalate_cmd` | `bootstrap/cli`, `modules/agents`, planning/work application contracts | `tests/test_cli.py`, `tests/test_dispatch_cli.py`, `tests/test_agent_run_summary_cli.py`, `tests/test_machine_json.py`, `tests/test_help_hygiene.py` | Bootstrap owns startup policy and each operation binds a typed application handler without a `beadhive.cli` patch consumer. |
| `beadhive.cli` HQ/contribution: `hq_init`, `hq_push`, `hq_prune_aggregate`, `hq_status`, `hq_clone`, `hq_restore_cmd`, `hq_intake_cmd`, `hq_bd_cmd`, `contrib_profile_build`, `contrib_profile_show`, `contrib_outbound`, `contrib_publish` | HQ/contribution application contracts | `tests/test_hq.py`, `tests/test_hq_intake.py`, `tests/test_contrib.py`, CLI JSON and naming tests | Typed HQ/contribution handlers own orchestration and no test patches a deep `cli.py` collaborator. |
| `beadhive.cli` passthrough: `bd_passthrough`, `git_passthrough` | CLI process adapter | `tests/test_cli.py`, `tests/test_bd_json_seam.py`, operation-catalog passthrough assertions | Retain as transport mechanics or replace with named process-adapter objects; never move opaque argv policy into a capability module. |
| `beadhive.cli` hive/sync/archive: `hive_init`, `hive_add`, `hive_rm`, `hive_retire`, `hive_reclaim`, `sync_default`, `sync_remotes_cmd`, `sync_peers_cmd`, `hive_sync_remote`, `hive_onboard`, `hive_list`, `hive_status`, `hive_migrate`, `hive_migrate_storage`, `hive_repair_cmd`, `hive_ready`, `hive_context`, `hive_check_push_fence`, `hive_hook_install`, `hive_hook_pre_push`, `hive_hook_push_main`, `hive_survey`, `hive_classify`, `hive_prefix`, `hive_enable`, `hive_disable`, `archive_list`, `archive_prune` | `modules/hives` application plus CLI projection adapters | `tests/contracts/test_hive_lifecycle_compatibility.py`, `tests/test_hive_*.py`, `tests/test_onboard*.py`, `tests/test_sync*.py`, `tests/test_archive.py` | The hives removal ledger reports zero facade/deep-patch consumers and every listed command binds a typed hives use case. |
| `beadhive.cli` worktrees: `wt_add`, `wt_list`, `wt_path`, `wt_init`, `wt_rm`, `wt_status`, `wt_prune`, `wt_mark_landed`, `wt_mark_abandoned` | `modules/worktrees` application plus CLI projection adapter | `tests/test_worktree.py`, `tests/test_worktree_inventory_json.py`, `tests/test_wt_status.py`, `tests/contracts/test_worktree_lifecycle_compatibility.py` | Worktree facade inventory is zero and the CLI binds only public worktree requests/results. |
| `beadhive.cli` registry/configuration: `labels_validate`, `labels_sync`, `labels_report`, `labels_allowed`, `labels_docs`, `config_path_cmd`, `config_show`, `config_init`, `config_split`, `config_schema_cmd`, `config_validate`, `config_get`, `config_set`, `config_unset` | registry contracts and `modules/config` application | `tests/test_labels.py`, `tests/test_config.py`, `tests/test_config_validate_cli.py`, `tests/contracts/test_config_resolution_compatibility.py` | Configuration/registry ledgers report zero CLI facade patches and handlers consume only their typed application contracts. |
| `beadhive.cli` local infrastructure: `dolt_up`, `dolt_provision`, `dolt_down`, `dolt_logs`, `dolt_ps`, `dolt_sql`, `otel_up`, `otel_down`, `otel_logs`, `otel_ps`, `otel_enable`, `otel_disable`, `otel_endpoint_cmd` | CLI process and telemetry adapters | `tests/test_dolt.py`, `tests/test_otel_cli.py`, `tests/test_otel_cli_instrument.py`, `tests/test_otel_cli_span.py` | Concrete process/telemetry adapters own execution and no domain/application package imports these CLI handlers. |
| `beadhive.cli` MCP/setup/harness diagnostics: `mcp_serve`, `mcp_install`, `setup_check`, `setup_show`, `setup_guide_cmd`, `setup_toolchain`, `harness_list`, `harness_auth`, `harness_install`, `doctor_cmd`, `alerts_show` | MCP composition (`bh-3qkmk.3`), setup/dependency adapters and state read contracts | `tests/test_mcp.py`, `tests/test_setup_guide_cli.py`, `tests/test_dep_cli.py`, `tests/test_doctor.py`, `tests/test_alerts.py` | Each command is a thin transport projection over its owning public contract and direct handler patches are zero. |
| `beadhive.cli` backup: `backup_export`, `backup_usage_cmd`, `backup_migrate_layout_cmd`, `backup_reclaim_cmd` | backup application and filesystem adapters | `tests/test_backup.py` and CLI help/naming snapshots | Typed backup use cases own policy; the CLI retains only Typer parameter declarations and result/error rendering. |

## Flat projection facade

`beadhive.cli_projection` remains a one-way facade to `beadhive.adapters.cli.projection` for
`work.py`, `plan.py` and existing external imports. It preserves the exact public names recorded
by `facade-cli-projection` in `import-boundary-exceptions.toml`, including the shared `otel`
module object used by the established trace-wrapper monkeypatch test. Its removal trigger is zero
production imports plus an empty `cli_projection.otel` patch inventory.

## Required proof

`tests/test_cli_projection.py` proves complete declarations, generated registration,
idempotence, ordering, parameter names, hidden state, passthrough settings, trace preservation and
the full help hashes. `tests/test_operation_catalog.py` proves every live leaf/signature is
declared exactly once and prompt seams are guarded. `tests/test_naming_conventions.py`,
`tests/test_cli.py`, `tests/test_help_hygiene.py`, host/dep/plugin/stream CLI tests and the module
compatibility closures protect aliases, lazy optional behavior, JSON envelopes, human output and
exit codes. These focused checks supplement but never replace `just check` and land-time
`just check-all`.
