# Repository physical-layout baseline

> Status: **checked exact-tip baseline** for `bh-50gsn.1` (2026-09-21).

This record freezes the repository shape before the physical-cleanup workstream moves any
implementation. It replaces historical counts as the planning baseline; it does not rewrite or
invalidate commit-specific evidence in
[`structural-quality-baseline.md`](structural-quality-baseline.md) or
[`capability-module-dependency-map.json`](capability-module-dependency-map.json).

## Provenance and reproduction

| Item | Measured value |
| --- | --- |
| Source commit | `a8399581980d1287e05f33931525c25a5bb10bbc` (`v0.17.1`) |
| Commit time | `2026-09-21T23:05:04Z` |
| Checkout state before measurement | clean: `git status --porcelain=v1 --untracked-files=all` emitted no rows |
| Python | 3.11.15 |
| Git | 2.55.0 |
| uv | 0.12.1 |
| Ruff | 0.15.20 |
| pytest | 9.1.1 |
| just | 1.57.0 |
| `bh` | 0.17.1 |
| `bd` | 1.3.0 (`f45b249ce`) |
| Pants | 2.32.1, pinned by `pants.toml` |
| RepoWise | 0.45.0 binary available; no `.repowise/state.json` exists in the measured worktree |

Run these commands from the repository root at the named commit:

```sh
git rev-parse HEAD
git status --porcelain=v1 --untracked-files=all
uv run python scripts/check_import_boundaries.py
git ls-files 'src/beadhive/*.py' | wc -l
find src/beadhive -maxdepth 1 -type f -name '*.py' ! -name '__init__.py' -printf '%f\n' | sort
git ls-files 'tests/**/*.py' 'tests/*.py' | sort -u | wc -l
python -c 'import tomllib, pathlib; data = tomllib.loads(pathlib.Path("docs/design/import-boundary-exceptions.toml").read_text()); print({key: sum(row["status"] == "active" for row in data.get(key, [])) for key in ("boundary_exception", "cycle_exception", "facade")})'
python -c 'import tomllib, pathlib; rows = tomllib.loads(pathlib.Path("tests/closures.toml").read_text())["closures"]; print(len(rows), {(kind, status): sum(row["kind"] == kind and row["status"] == status for row in rows) for kind, status in sorted({(row["kind"], row["status"]) for row in rows})})'
find . -name BUILD -o -name BUILD.root | sort
```

`uv run` is the repository-supported interpreter entry point. The equivalent direct
`.venv/bin/python` commands produced the captured numbers in the restricted measurement process;
the checked dependency environment is the one created by `uv sync` during batch claim.

The generated or checked evidence paths used here are:

- `scripts/check_import_boundaries.py` and
  `docs/design/import-boundary-exceptions.toml`: native AST import graph, SCC snapshot, exact
  exceptions, and facades;
- `tests/closures.toml` and
  `docs/proof/bh-ck1t6.1-test-closure-certification.json`: test ownership and certification state;
- `docs/design/test-fixture-scope-inventory.md` and `tests/stateful_fixtures.py`: fixture ownership;
- `pants.toml`, the twelve checked `BUILD` files, and `scripts/check_pants_ownership.py`: build
  ownership;
- `justfile`: configured developer, submit, land, and release validation commands; and
- this file: the checked human-readable exact-tip snapshot.

RepoWise is not refreshed by this bead. The repository contains older RepoWise-derived records,
but their named commits are not this commit and this worktree has no RepoWise state. They are
historical and non-authoritative for current counts, callers, coverage, or conformance.

## Production ownership and flat implementation debt

The native checker finds **337 production Python files**. Physical ownership by first path below
`src/beadhive/` is:

| Root | Python files | Current physical owners |
| --- | ---: | --- |
| package root | 213 | `__init__.py` plus 212 flat implementation or compatibility modules |
| `modules/` | 71 | root marker 1; agents 9; config 15; hives 8; planning 7; state 10; work 10; worktrees 11 |
| `kernel/` | 24 | root marker 1; daemon 6; lifecycle 3; operations 3; plugins 6; telemetry 5 |
| `integrations/` | 13 | root marker 1; Herdr 12 |
| `adapters/` | 8 | root files 4; CLI 4 |
| `bootstrap/` | 6 | process composition roots |
| `testing/` | 2 | public conformance-test support |

The 212 package-root implementation/facade files are the exact legacy physical-debt inventory.
Their presence is not evidence that each should become a package: later movement must assign a
capability, kernel, adapter, integration, bootstrap, or registered-facade owner first.

<!-- markdownlint-disable MD013 -->

- `activity_publisher.py`, `adopt.py`, `agent_launch_profile.py`, `agent_run_summary.py`, `agent_run_summary_reader.py`, `alerts.py`, `archive.py`, `backup.py`
- `bd.py`, `cache_store.py`, `channels.py`, `checkpoint.py`, `claim_authority.py`, `cli.py`, `cli_entrypoint.py`, `cli_projection.py`
- `complexity.py`, `complexity_backfill.py`, `compose.py`, `config.py`, `config_binding.py`, `config_consumer_ports.py`, `config_edit.py`, `config_partition.py`
- `config_paths.py`, `config_policy.py`, `config_release.py`, `config_schema.py`, `config_services.py`, `config_split_migration.py`, `config_store.py`, `config_validate.py`
- `config_work_settings.py`, `conflict_estimator.py`, `contract_release.py`, `contract_release_evidence.py`, `contributor.py`, `converge.py`, `coordination.py`, `credentials.py`
- `daemon_activity.py`, `daemon_activity_api.py`, `daemon_auth.py`, `daemon_config.py`, `daemon_contract.py`, `daemon_factory.py`, `daemon_hq_probe.py`, `daemon_local_state.py`
- `daemon_network.py`, `daemon_openapi.py`, `daemon_platform.py`, `daemon_state_broker.py`, `daemon_supervisor.py`, `daemon_telemetry.py`, `dep_cli.py`, `deps.py`
- `dispatch_hive_run.py`, `dispatch_log.py`, `dispatch_status.py`, `dispatch_supervisor.py`, `doctor.py`, `dolt.py`, `dolt_health.py`, `engine.py`
- `escalate.py`, `fleet.py`, `frame_bridge.py`, `frame_bridge_factory.py`, `frame_bridge_runtime.py`, `frame_bridge_upstream.py`, `gateway_contract.py`, `gateway_read.py`
- `gateway_wire_contracts.py`, `ghpr.py`, `git.py`, `git_identity.py`, `git_linkage.py`, `gitauth.py`, `gitref.py`, `gitworkspace.py`
- `gitworkspace_plugin.py`, `guard.py`, `harness.py`, `herdr_launch_profile.py`, `herdr_plugin.py`, `herdr_views.py`, `hitch_plugin.py`, `hive.py`
- `hive_identity.py`, `hive_migrate.py`, `hive_ready.py`, `hive_repair.py`, `hive_schema.py`, `hive_services.py`, `hive_sync.py`, `home_migration.py`
- `host.py`, `host_adopt.py`, `host_answers.py`, `host_cli.py`, `host_daemon.py`, `host_daemon_entrypoint.py`, `host_fence.py`, `host_lease.py`
- `host_provision.py`, `host_retire.py`, `hosts.py`, `hq.py`, `hq_restore.py`, `hub.py`, `hub_bulk.py`, `identity.py`
- `install_plane.py`, `jsonout.py`, `localloop.py`, `log.py`, `mcp.py`, `metadata.py`, `model_routing.py`, `molecule.py`
- `observaloop.py`, `observaloop_env.py`, `onboard.py`, `operation_catalog.py`, `operator_actions.py`, `operator_agents.py`, `operator_api.py`, `operator_contract.py`
- `operator_feed.py`, `operator_sources.py`, `operator_sse.py`, `operator_work_items.py`, `orca.py`, `otel.py`, `otel_lgtm.py`, `plan.py`
- `plan_repair.py`, `planning_services.py`, `plugin_runtime_catalog.py`, `plugins.py`, `precious.py`, `prepush.py`, `private_paths.py`, `process_watchdog.py`
- `public_readers.py`, `publish_export.py`, `registry.py`, `release.py`, `release_order.py`, `report.py`, `report_target.py`, `repowise_plugin.py`
- `retire.py`, `role.py`, `role_execution.py`, `role_process.py`, `route.py`, `run.py`, `run_journal.py`, `runtime.py`
- `safety.py`, `schedule.py`, `seat_contracts.py`, `seatrun.py`, `selective_validation.py`, `setup.py`, `setup_guide.py`, `source_descriptors.py`
- `state.py`, `state_services.py`, `state_stream.py`, `state_stream_epic_schedule.py`, `state_stream_gate_projection.py`, `state_stream_polling.py`, `state_stream_process.py`, `storage_migrate.py`
- `store_locator.py`, `stream_cli.py`, `survey.py`, `sync_remote.py`, `test_report.py`, `toolchain.py`, `transport_inventory.py`, `triage.py`
- `triage_store.py`, `validate.py`, `validate_probe.py`, `validation_admission.py`, `validation_ledger.py`, `validation_records.py`, `work.py`, `work_assignment.py`
- `work_dispatch.py`, `work_group.py`, `work_guards.py`, `work_intake.py`, `work_logic.py`, `work_merge.py`, `work_metrics.py`, `work_next.py`
- `work_reads.py`, `work_refine.py`, `work_services.py`, `work_show.py`, `work_submission.py`, `worktree.py`, `worktree_cleanup.py`, `worktree_git.py`
- `worktree_inventory.py`, `worktree_merge.py`, `worktree_verify.py`, `wt_status.py`

<!-- markdownlint-enable MD013 -->

## Import graph and SCCs

`scripts/check_import_boundaries.py` parses the tree with the standard-library AST and imports no
product code. It reports:

| Measure | Exact-tip value |
| --- | ---: |
| Python modules | 337 |
| grouped import edges | 3,024 |
| cyclic SCCs | 8 |
| modules in cyclic SCCs | 59 |
| cyclic edges | 155 |
| symbols on cyclic edges | 172 |
| SCC sizes | 35, 8, 6, 2, 2, 2, 2, 2 |

The SCC memberships are:

1. **35 modules:** `bd`, `compose`, `config`, `converge`, `credentials`, `deps`, `dolt_health`,
   `engine`, `fleet`, `ghpr`, `gitref`, `gitworkspace`, `guard`, `host`, `host_lease`, `hosts`,
   `identity`, `log`, `metadata`, `observaloop`, `observaloop_env`, `otel`, `private_paths`,
   `registry`, `repowise_plugin`, `route`, `run`, `safety`, `setup`, `triage_store`, `validate`,
   `validation_ledger`, `validation_records`, `worktree`, and `worktree_merge`.
2. **8 modules:** `alerts`, `doctor`, `host_cli`, `host_daemon`, `host_provision`, `host_retire`,
   `mcp`, and `operator_sse`.
3. **6 modules:** `backup`, `hive`, `hq`, `hub`, `onboard`, and `storage_migrate`.
4. **2 modules:** `herdr_plugin` and `herdr_views`.
5. **2 modules:** `localloop` and `runtime`.
6. **2 modules:** `report` and `triage`.
7. **2 modules:** `state_stream_epic_schedule` and `state_stream_polling`.
8. **2 modules:** `work` and `work_show`.

Three nonliteral dynamic imports remain visible at `config_binding.py`, `mcp.py`, and `plugins.py`.
They are legacy-root callers; any move into a governed package must replace them with a literal,
registered discovery boundary, or an exact reviewed exception.

## All active architecture-debt records

The checked ledger contains **52 active records**: 6 boundary exceptions, 38 exact cycle edges,
and 8 compatibility facades. It also retains 13 removed historical records; those are not counted
as active debt. The checker is green with the frozen cycle digest
`f60b8e61436c0a28909f9d20ce9c3d1e500fe6961c76c54caae1c1e9969f7b09`.

### Boundary exceptions (6)

| Record | Exact edge | Recorded successor |
| --- | --- | --- |
| `boundary-bootstrap-cli-compatibility` | `beadhive.bootstrap.cli` -> `beadhive.cli_entrypoint.main` | `bh-inqwc` |
| `boundary-bootstrap-mcp-compatibility` | `beadhive.bootstrap.mcp` -> `beadhive.mcp` | `bh-inqwc` |
| `boundary-bootstrap-host-compatibility` | `beadhive.bootstrap.host` -> `beadhive.host_daemon_entrypoint` | `bh-inqwc` |
| `boundary-bootstrap-frame-bridge-compatibility` | `beadhive.bootstrap.frame_bridge` -> `beadhive.frame_bridge_runtime` | `bh-inqwc` |
| `boundary-config-complexity-tier` | `beadhive.modules.config.contracts` -> `beadhive.complexity.ComplexityTier` | `bh-inqwc` |
| `boundary-config-complexity-names` | `beadhive.modules.config.contracts` -> `beadhive.complexity.tier_names` | `bh-inqwc` |

### Cycle exceptions (38)

| Record | Exact edge | Recorded successor |
| --- | --- | --- |
| `cycle-edge-001` | `beadhive.deps` -> `beadhive.config` | `bh-inqwc` |
| `cycle-edge-002` | `beadhive.engine` -> `beadhive.bd` | `bh-inqwc` |
| `cycle-edge-003` | `beadhive.guard` -> `beadhive.config` | `bh-inqwc` |
| `cycle-edge-004` | `beadhive.herdr_views` -> `beadhive.herdr_plugin` | `bh-inqwc` |
| `cycle-edge-005` | `beadhive.host` -> `beadhive.config` | `bh-inqwc` |
| `cycle-edge-006` | `beadhive.host_provision` -> `beadhive.host_cli` | `bh-inqwc` |
| `cycle-edge-007` | `beadhive.host_retire` -> `beadhive.host_cli` | `bh-inqwc` |
| `cycle-edge-008` | `beadhive.log` -> `beadhive.config` | `bh-inqwc` |
| `cycle-edge-009` | `beadhive.onboard` -> `beadhive.hive` | `bh-inqwc` |
| `cycle-edge-010` | `beadhive.onboard` -> `beadhive.hub` | `bh-inqwc` |
| `cycle-edge-011` | `beadhive.operator_sse` -> `beadhive.host_daemon` | `bh-inqwc` |
| `cycle-edge-017` | `beadhive.registry` -> `beadhive.bd` | `bh-inqwc` |
| `cycle-edge-018` | `beadhive.registry` -> `beadhive.config` | `bh-inqwc` |
| `cycle-edge-019` | `beadhive.registry` -> `beadhive.gitworkspace` | `bh-inqwc` |
| `cycle-edge-020` | `beadhive.registry` -> `beadhive.guard` | `bh-inqwc` |
| `cycle-edge-021` | `beadhive.registry` -> `beadhive.metadata` | `bh-inqwc` |
| `cycle-edge-023` | `beadhive.run` -> `beadhive.credentials` | `bh-inqwc` |
| `cycle-edge-024` | `beadhive.run` -> `beadhive.identity` | `bh-inqwc` |
| `cycle-edge-025` | `beadhive.run` -> `beadhive.otel` | `bh-inqwc` |
| `cycle-edge-026` | `beadhive.runtime` -> `beadhive.localloop` | `bh-inqwc` |
| `cycle-edge-027` | `beadhive.setup` -> `beadhive.compose` | `bh-inqwc` |
| `cycle-edge-028` | `beadhive.setup` -> `beadhive.config` | `bh-inqwc` |
| `cycle-edge-029` | `beadhive.setup` -> `beadhive.deps` | `bh-inqwc` |
| `cycle-edge-030` | `beadhive.setup` -> `beadhive.dolt_health` | `bh-inqwc` |
| `cycle-edge-031` | `beadhive.setup` -> `beadhive.repowise_plugin` | `bh-inqwc` |
| `cycle-edge-032` | `beadhive.setup` -> `beadhive.run` | `bh-inqwc` |
| `cycle-edge-033` | `beadhive.state_stream_polling` -> `beadhive.state_stream_epic_schedule` | `bh-inqwc` |
| `cycle-edge-034` | `beadhive.storage_migrate` -> `beadhive.backup` | `bh-inqwc` |
| `cycle-edge-035` | `beadhive.storage_migrate` -> `beadhive.hq` | `bh-inqwc` |
| `cycle-edge-036` | `beadhive.triage` -> `beadhive.report` | `bh-inqwc` |
| `cycle-edge-037` | `beadhive.work_show` -> `beadhive.work` | `bh-inqwc` |
| `cycle-edge-038` | `beadhive.worktree` -> `beadhive.converge` | `bh-inqwc` |
| `cycle-edge-039` | `beadhive.worktree` -> `beadhive.observaloop` | `bh-inqwc` |
| `cycle-edge-040` | `beadhive.worktree` -> `beadhive.observaloop_env` | `bh-inqwc` |
| `cycle-edge-041` | `beadhive.worktree` -> `beadhive.otel` | `bh-inqwc` |
| `cycle-edge-042` | `beadhive.worktree` -> `beadhive.triage_store` | `bh-inqwc` |
| `cycle-edge-043` | `beadhive.worktree` -> `beadhive.validation_ledger` | `bh-inqwc` |
| `cycle-edge-044` | `beadhive.worktree_merge` -> `beadhive.worktree` | `bh-inqwc` |

### Compatibility facades (8)

| Record | Path and exact-tip static production consumers | Public/patch contract and executable consumers |
| --- | --- | --- |
| `facade-cli-projection` | `cli_projection.py`; `plan`, `work` | 12 ledgered symbols including `otel`; `test_cli_projection.py`, `test_naming_conventions.py` |
| `facade-config-schema` | `config_schema.py`; `daemon_platform`, `daemon_supervisor`, `host_daemon` | 46 ledgered schema/default symbols; `tests/contracts/test_config_contract_compatibility.py` and config reverse dependents |
| `facade-work` | `work.py`; `cli`, `mcp`, `work_show` | 15 public/lifecycle symbols; `test_structural_facade_contracts.py`, `test_work_reads.py`, `test_work.py` |
| `facade-config` | `config.py`; 76 static production importers | 11 public load/save/edit symbols; `test_structural_facade_contracts.py`, `test_config.py` |
| `facade-worktree` | `worktree.py`; 22 static production importers | 10 public/patch symbols; `test_structural_facade_contracts.py`, `test_worktree.py`, `test_wt_status.py` |
| `facade-config-partition` | `config_partition.py`; `config_split_migration`, `config_store` | 11 policy/allowlist symbols; `test_config_partition.py`, `test_config_fleet_merge.py`, `test_config_scope.py` |
| `facade-config-store` | `config_store.py`; `config` | 17 YAML/store/collaborator symbols; `test_config_boundaries.py`, `test_structural_facade_contracts.py`, `tests/contracts/test_config_resolution_compatibility.py` |
| `facade-config-policy` | `config_policy.py`; `config` | 6 migration/warning symbols; `test_config.py`, `test_config_validate_cli.py`, `tests/contracts/test_config_resolution_compatibility.py` |

The exact preserved-symbol arrays, patch reasons, test closures, external-consumer statements, and
expiry triggers remain authoritative in `import-boundary-exceptions.toml`; this summary does not
shorten those contracts. Static consumer counts come from the same AST graph. External consumers
are explicitly “not separately enumerable” in the ledger, so no consumer-zero claim is made.

## Tests, fixtures, closures, and build ownership

There are **466 checked Python files under `tests/`**: 394 at the test root, 43 under `unit/`, 14
under `harness/`, 11 under `contracts/`, 2 under `proof/`, 1 under `fixtures/`, and 1 under
`spikes/`.

Fixture scope is explicit:

- `tests/conftest.py` registers only the watchdog and stateful compatibility plugins;
- `tests/stateful_fixtures.py` owns the legacy compatibility scope and the named config, identity,
  Dolt, validation, telemetry, runtime, and plugin concern scopes;
- root, contract, proof, and spike tests retain `legacy_stateful_test_scope` unless explicitly
  narrowed;
- `tests/unit/**` is the pure-module location and requests only the concern scopes it needs; and
- dynamic fixture lookup and unknown infrastructure fall back to native/full validation.

`tests/closures.toml` has **24 present closures**: 7 module, 5 contract, 5 plugin, 4 kernel, 1
adapter, 1 integration, and 1 system closure. It names direct tests, shared contracts, and reverse
dependents. Every row remains uncertified for selective activation in the checked certification
artifact, so the authoritative commands are still:

| Boundary | Configured command |
| --- | --- |
| Developer/submit correctness | `just check` |
| Main integration and release | `just check-all` |
| This baseline and ADR | `just lint` |
| Architecture evidence | `just architecture-structural-check` inside `just check`; `just architecture-check` after a completed receipt |
| Advisory owned closure | `just test-closure <closure-id>` |

Pants ownership is repository-wide through **12 checked `BUILD` files**. Production Python is
currently one broad `//src/beadhive:lib` target rather than one target per capability. Tests are
split into root conftest, watchdog, stateful fixtures, harness, pure tests, legacy-stateful tests,
resources, package markers, and test configuration. Twenty-six pure test files carry
`pants:proven`; the rest retain their current native/stateful route. Documentation has separate
root, assets, design, examples, proof, releases, schemas, spikes, and upstream targets.

These facts distinguish semantic test ownership from current build granularity: a file appearing
under a capability package does not by itself prove an independently selectable or certified
closure.

## Overlap and ownership reconciliation

This baseline records overlap; it does not absorb or close it.

| Bead | Current state | Disposition here |
| --- | --- | --- |
| `bh-50gsn.3` — Reassign every active architecture exception and facade to a live successor | open | Owns successor repair for all 52 active records. This baseline preserves the current values and does not relabel them. |
| `bh-50gsn.4` — Enforce the ADR's no-new-unowned-root-implementation rule | open | Owns the executable manifest/AST guard. This bead records the 212-file starting set but adds no policy code. |
| `bh-50gsn.5` — Reconcile architecture documentation with the completed modularization and new cleanup policy | open | Owns current-state edits to `MODULES.md`, `DESIGN.md`, and fixture/closure guidance. Historical records stay immutable here. |
| `bh-62rm` — refactor: extract plan.py render/verify cluster (983 lines > 800 cap) | open | Adopt into planning cleanup; no competing size-based move is authorized here. |
| `bh-bo5` — Stabilize plan helpers used cross-module by beadhive.mcp | open | Preserve its plan/MCP public-contract decision; physical movement must adopt its result. |
| `bh-7ks1c` — Reduce validation test bottlenecks exposed by xdist scaling benchmarks | open | Excluded from physical ownership; its performance evidence remains separate from closure correctness. |
| `bh-nc0p` — perf(test): one test is 156s of the unit suite's 185s wall clock | open | Excluded; it owns one test's hermeticity and runtime, not repository organization. |
| `bh-gfvt` — skills(test-efficiency): a bh skill for keeping the gate fast by construction | open | Excluded; it owns reusable authoring guidance, not this repository's package layout. |
| `bh-xx292` — DECISION: integration tests run hermetically by default | open | Adopt the eventual test-policy decision; do not redefine the hermetic fence in this ADR. |
| `bh-gd443` — Where do ADRs live canonically, and how does the in-repo copy avoid going stale? | open | This checked in-repo ADR follows current practice but does not decide cross-system ADR authority. |
| `bh-vylp0` — Spike: validate a private-state inspection module boundary | open | Defer `private_paths` ownership to the spike verdict; no parallel extraction is implied. |

## Baseline conclusion

The repository has real capability packages and enforceable inward dependency rules, but it is
not physically clean: 212 flat implementation/facade files remain, 59 modules participate in
eight SCCs, and 52 active exceptions/facades require explicit successor ownership. The next
decision defines what “clean” means; later beads must reduce these exact inventories without
changing public behavior or test/validation policy accidentally.
