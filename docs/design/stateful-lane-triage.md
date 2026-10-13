# Stateful lane triage: real integration, port fakes, or cached fixtures

- **Status:** Proposed (triage and plan only; nothing is moved, mocked or deleted by this record)
- **Date:** 2026-10-13
- **Bead:** `bh-e0lux`. **Profiles:** the operator-approved `fast` / `inner` subsets and the
  main-bound `all` profiles recorded on `bh-i3nc3`.
- **Evidence:** the full stateful JUnit report
  `.bh/validation/runs/run-fb7ed902461dbbf531b63de5b1a63fc9/reports/pytest-6-213nt47c.xml`
  (main `2e60e00f`, 10,603 tests, 551 s wall on 16 workers, 7,986 s summed) and the integration
  report `run-4fd4d1ed0ac641fd77b0c9adcee8fb59/reports/pytest-6-0t_d9o9q.xml` (262 tests, 447 s
  wall, 6,314 s summed). Every test was read before it was classified. All 139 node ids were
  confirmed with `pytest --collect-only` on `2e60e00f`.
- **Measurements taken here:** static reading, `--collect-only`, and single-test cProfile runs on
  an idle host (load average about 3 on 32 cores) for `test_doctor`, `test_hive_observaloop`,
  `test_mcp_resource`, `test_work` and `test_daemon_activity_api`. In-process timings of
  `build_release()`, `build_ledger()`, `operations()`, `mcp.build_server()` and the repo-scan
  scripts were taken with no pytest. No lane or suite was run.

## Summary

The 139 tests of 10 s or more are 51% of stateful time. They fall into three groups, plus one
set of tests that is already gone:

| Group | Meaning | Tests >= 10 s | Their seconds | Seconds out of stateful |
|---|---|---:|---:|---:|
| A | The real process boundary is the subject. Keep it real, mark `integration` | 55 | 1,966.8 | 1,966.8 (moves to integration) |
| B | The process is incidental. Use an in-process seam (never subprocess/argv patching) | 42 | 976.9 | 894.5 |
| C | A repeated expensive build. Build once per session and copy or share | 27 | 687.9 | 506.8 |
| R | Already removed by `bh-rqmmm` (lands with epic `bh-xg40p`) | 15 | 412.1 | 412.1 |
| **Total** | | **139** | **4,043.7** | **3,780.2** |

File-level dispositions add the smaller tests in the same files (table below). Across all
groups, the plan moves **2,251.5 s** to integration and removes **1,869.3 s** of B/C cost. The
retired closure files remove another **618.0 s**.

| Stage (cumulative) | Stateful summed s | Projected stateful wall s | Integration summed s | Projected integration wall s |
|---|---:|---:|---:|---:|
| Today (`2e60e00f`) | 7,985.9 | 551 (measured) | 6,313.5 | 447 (measured) |
| After R (`bh-rqmmm` lands) | 7,367.9 | 508 | 6,313.5 | 447 |
| After A (both integration groups) | 5,116.4 | 353 | 8,565.0 | 606 |
| After B | 4,196.8 | 290 | 8,565.0 | 606 |
| After C | **3,247.2** | **224** | 8,565.0 | **606** |

The wall projection scales summed time by today's packing overhead: 551 s of wall against
7,986/16 = 499 s of ideal time gives a factor of 1.104 for stateful, and 447 s against 395 s
gives 1.13 for integration. The longest remaining stateful item after the plan is a single
session build of about 35 s, which is well under the projected wall, so the tail does not bind.
The A and R figures are exact JUnit sums. The B figures come from profiles. The C figures
outside the per-test table are estimates (±50%), and each C child bead must measure before it
converts.

**The trade is visible at main-bound gates.** The integration lane grows by about 159 s of wall
(447 s to about 606 s; the 585 s worst case scales to about 790 s). Under the approved profiles
that cost is paid only by `merge-main`, `molecule`, `postland`, `release` and `push-main`.
`submit`, `merge` and `union` (`inner`) get the 551 s to about 224 s stateful win. Under today's
legacy model, every boundary runs every key, so the A moves would only shift time between lanes.
That is why both A child beads depend on `bh-i3nc3`.

## Classification rules

- **A.** The test proves something only a real process can prove: a pre-receive hook verifying
  SSH-signed commits, the git broker and frame sandbox, a real stdio MCP child against HTTP,
  descendant reaping, OS-process slot contention, cross-interpreter determinism, or a nested
  pytest with coverage. These stay byte-for-byte as they are and gain the `integration` marker.
  Where a file mixes real-process tests with in-process unit tests, the marker is applied per
  fixture (`backend`) or per test, never per file.
- **B.** The process is setup, not subject. The replacement must be a port or domain seam that
  already exists or is a small, named refactor: the `BuildVerifier` plugin port, `BdTransport`,
  the wire gate's `Reader` protocol, `triage_store.store`, an in-process collection lookup, or
  `pytester`. Patching `subprocess.run` / `Popen` / argv is ruled out.
- **C.** The same artifact (release candidate, evidence document, ledger, landed molecule, MCP
  registry) is rebuilt per test or per call. Build it once per worker and share it read-only, or
  restore a snapshot with `cp -a` onto a fixed root. `_PreparedHq` in
  `tests/test_hq_authority_backend.py` already uses this pattern.

## Seams and fixtures

| ID | Group | Seam or fixture | Child |
|---|:---:|---|:---:|
| A1 | A | `backend` fixture (`_PreparedHq`): keep real. A collection hook adds `integration` to every item whose `fixturenames` include `backend`. `test_hq_seed_roundtrip_int.py` also signs with real `ssh-keygen` | G1 |
| A2 | A | `tests/spikes/test_bh_u562_3_unified_host_transport.py`: real stdio + streamable-HTTP MCP servers (spike evidence; overlaps A3) | G2 |
| A3 | A | `test_daemon_mcp_http` parity: a real `python -m beadhive.mcp` stdio child against the ASGI HTTP app | G2 |
| A4 | A | `test_daemon_live_contract` composition: a spawned live-contract scenario process | G2 |
| A5 | A | `test_mcp_call_every_tool`: a real child whose venv vanishes mid-session (bh-fwhlu) | G2 |
| A6 | A | `test_demo_live_ingress_matrix` parity: `git archive` plus an isolated interpreter install (the demos key runs the same path) | G2 |
| A7 | A | `test_pytest_with_report` coverage: a nested real pytest with coverage | G2 |
| A8 | A | `test_source_descriptors` `[3]`/`[5]`: real installed `bh` plus a forking fake `bd`. `[1]` stays in stateful as the smoke case | G2 |
| A9 | A | `test_state_stream_integration` feature demo: a real `bh stream` process group | G2 |
| A10 | A | `test_two_fresh_processes_generate_byte_identical_release`: cross-interpreter determinism | G2 |
| A11 | A | `test_concurrent_submit_processes_share_host_capacity_and_coalesce_retry`: spawned submits contending for host validation slots | G2 |
| A12 | A | `test_actual_gate_cli_allows_additive_catalog_operation`: the one real wire-gate CLI and git case kept after B6 | G2 |
| B1 | B | `doctor._data_build_verify` keys on `Path.cwd()` and runs the real Pants verifier from the repository root (13.4 s of a 17.1 s `doctor_payload` in a profile). Inject a fake `BuildVerifierProvider` through `collect_build_verify_diagnostics(providers=...)`, or `monkeypatch.chdir(tmp_path)` in the shared doctor fixture | G3 |
| B2 | B | `test_cli` root-callback survival: invoke a cheap verb, or `doctor` under the hermetic fixtures plus B1. Today it runs real `doctor` against the host config (48 s) | G3 |
| B3 | B | `hive.init` reaches the real `bd` binary through `hive._run_onboard` -> `onboard.execute_onboard` -> `bd.run` (9 calls, about 15 s profiled) even though `hive.run` is stubbed. Stub `_run_onboard` like the other prerequisites, or route it through `BdTransport` (`packages/beadhive-bd-cli`) | G4 |
| B4 | B | `triage_store.store(...)` called directly instead of one `worktree.clean_checkout` per run | G4 |
| B5 | B | Node-id resolution in-process (AST lookup of the function and its parametrize ids) instead of a nested `pytest --collect-only`; `pytester.runpytest_inprocess` for `test_collection_prefilter` | G4 |
| B6 | B | The `Reader` protocol already in `scripts/check_wire_schema_compat.py`: `main()` becomes a shim over `run(root, base_ref, *, baseline_reader)`, and tests pass a `FilesystemReader` over a baseline copy. The production `GitReader` batches through one `git cat-file --batch` (today it runs one `git show` per file over 271 files) | G5 |
| B7 | B | A read-only object seam in the capability scripts (`list_tree(rev, prefix)`, `read_blob(rev, path)`) with one `git cat-file --batch` process. The measured revision stays real | G6 |
| C1 | C | `validate_release(root)` calls `build_release()` every time (5.3 s idle, about 8 s in-lane). Validate against the per-worker `_cached_release()` copy through `_validate_release_candidate(root, _release_document())`, as `_write_release_document` already does for writes | G7 |
| C2 | C | The offline-generator test keeps its two real builds and validates both against the cached candidate | G7 |
| C3 | C | The checked-release test keeps one real `validate_release()`; the equality check uses the cached build | G7 |
| C4 | C | The evidence script runs in-process (`main([...])`) over one session build instead of three interpreters | G7 |
| C5 | C | One `build_release()` and one `build_compatibility_report()` per worker | G7 |
| C6 | C | The baseline `render_document()` is shared; the mutated generations stay real | G7 |
| C7 | C | `operations()` is hoisted out of the per-path loop and one parsed `src/` AST corpus is shared | G7 |
| C8 | C | One session render of the transport composition evidence, in-process, with a failing `git` first on `PATH` (proving the no-git-object property for both tests), compared to the checked file. This drops a `uv run` interpreter | G8 |
| C9 | C | One module-scoped `build_ledger()` (34 s idle, measured) for the three ledger tests | G8 |
| C10 | C | The session `build_proof()` is shared with one side of the reproducibility comparison | G6 |
| C11 | C | Repo-wide AST scans (`check_root_tests` 16.5 s, `check_test_wall_clock` 11.3 s idle) share one parsed-tests corpus. No saving is counted | G8 |
| C12 | C | Per-worker repository and landed-molecule templates (repo, worktrees, FakeBd state), restored with `cp -a` onto a fixed live root in the `_PreparedHq` style | G9 |

## Per-test table (every test of 10 s or more)

Seconds are the JUnit `time` from the 2026-10-13 stateful run. "Saved" is the seconds that leave
the stateful lane: the whole time for A (moved to integration) and R, and time minus the
estimated residual for B and C.

| # | Test | s | Group | Seam / fixture | Child | Saved s | Risk (proof lost) |
|---:|---|---:|:---:|:---:|:---:|---:|---|
| 1 | `tests/test_test_closure_certification_process.py::test_real_gate_receipts_authorize_only_their_exact_candidate_process` | 172.3 | R | R | - | 172.3 | n/a (already deleted) |
| 2 | `tests/test_hq_authority_backend.py::test_real_foreign_holder_takeover_requires_authenticated_incumbent_evidence[backend0]` | 140.6 | A | A1 | G1 | 140.6 | HQ guard/broker proof runs at main-bound gates only |
| 3 | `tests/test_hq_authority_backend.py::test_protected_legacy_git_adoption_preserves_live_grant_then_binds_same_incarnation` | 135.2 | A | A1 | G1 | 135.2 | HQ guard/broker proof runs at main-bound gates only |
| 4 | `tests/test_hq_authority_backend.py::test_bound_hive_adopt_requires_fresh_bound_heartbeat_at_signed_broker` | 105.1 | A | A1 | G1 | 105.1 | HQ guard/broker proof runs at main-bound gates only |
| 5 | `tests/test_hq_authority_backend.py::test_real_frame_signed_hive_lease_adopt_renew_and_cordoned_release` | 89.3 | A | A1 | G1 | 89.3 | HQ guard/broker proof runs at main-bound gates only |
| 6 | `tests/test_hq_authority_backend.py::test_supersede_retires_previous_signer_atomically` | 83.9 | A | A1 | G1 | 83.9 | HQ guard/broker proof runs at main-bound gates only |
| 7 | `tests/test_transport_composition_roots.py::test_live_composition_evidence_is_current_and_closes_every_root` | 75.2 | C | C8 | G8 | 57.7 | Small; `uv run` resolution left to the gate |
| 8 | `tests/test_hq_authority_backend.py::test_lifecycle_cordon_drain_park_resume_quarantine_retire` | 70.2 | A | A1 | G1 | 70.2 | HQ guard/broker proof runs at main-bound gates only |
| 9 | `tests/test_hq_authority_backend.py::test_frame_hive_lease_guard_rejects_forgery_binding_and_cas_replays` | 67.0 | A | A1 | G1 | 67.0 | HQ guard/broker proof runs at main-bound gates only |
| 10 | `tests/test_hq_authority_backend.py::test_admitted_frame_candidates_bindings_cleanup_and_signer_reuse` | 66.0 | A | A1 | G1 | 66.0 | HQ guard/broker proof runs at main-bound gates only |
| 11 | `tests/test_contract_release.py::test_integrity_gate_rejects_path_escape_symlink_and_metadata_drift` | 65.7 | C | C1 | G7 | 64.7 | None; real rebuild still covered by the checked-release test |
| 12 | `tests/test_hq_authority_trusted_publish.py::test_git_keyless_operator_commands_trusted_accepts_signed_refuses` | 61.7 | A | A1 | G1 | 61.7 | HQ guard/broker proof runs at main-bound gates only |
| 13 | `tests/test_hq_authority_backend.py::test_real_cli_repeated_beats_observation_and_admission` | 57.4 | A | A1 | G1 | 57.4 | HQ guard/broker proof runs at main-bound gates only |
| 14 | `tests/test_wire_release_publish.py::test_adding_a_cli_verb_publishes_the_next_minor_release_and_gates_pass` | 53.9 | B | B6 | G5 | 45.9 | None; one real-CLI case kept (A12) |
| 15 | `tests/test_capability_module_map.py::test_capability_module_map_is_reproducible_from_the_measured_revision` | 53.8 | B | B7 | G6 | 48.8 | None; same revision, one git process |
| 16 | `tests/test_hq_authority_backend.py::test_config_head_advance_fences_old_hive_projection` | 52.9 | A | A1 | G1 | 52.9 | HQ guard/broker proof runs at main-bound gates only |
| 17 | `tests/test_daemon_live_contract.py::test_real_product_app_composes_concurrent_cli_stdio_http_operator_and_activity` | 52.1 | A | A4 | G2 | 52.1 | live composition proof at main-bound gates only |
| 18 | `tests/test_hq_authority_backend.py::test_admission_requires_three_beats_declared_policy_and_exact_expected_values` | 51.9 | A | A1 | G1 | 51.9 | HQ guard/broker proof runs at main-bound gates only |
| 19 | `tests/test_hq_authority_backend.py::test_eligibility_bracket_after_new_beat_admission_and_expiry_waiver` | 51.1 | A | A1 | G1 | 51.1 | HQ guard/broker proof runs at main-bound gates only |
| 20 | `tests/test_cli.py::test_root_survives_a_raising_otel_init` | 49.5 | B | B2 | G3 | 48.5 | None; asserts the root callback only |
| 21 | `tests/test_cli.py::test_root_survives_a_raising_home_migration` | 48.8 | B | B2 | G3 | 47.8 | None; asserts the root callback only |
| 22 | `tests/test_cli.py::test_no_routing_flags_is_a_plain_cwd_invocation` | 48.4 | B | B2 | G3 | 47.4 | None; asserts the root callback only |
| 23 | `tests/test_cli.py::test_root_survives_a_raising_hive_key_migration` | 48.3 | B | B2 | G3 | 47.3 | None; asserts the root callback only |
| 24 | `tests/test_transport_composition_roots.py::test_live_composition_evidence_does_not_require_an_unreachable_git_object` | 44.9 | C | C8 | G8 | 27.4 | Small; `uv run` resolution left to the gate |
| 25 | `tests/test_hq_authority_no_expiry.py::test_git_operator_mutations_without_duration_keep_no_expiry` | 44.5 | A | A1 | G1 | 44.5 | HQ guard/broker proof runs at main-bound gates only |
| 26 | `tests/test_hq_authority_trusted.py::test_git_rollback_and_cordon_are_honoured_in_both_modes[trusted]` | 41.9 | A | A1 | G1 | 41.9 | HQ guard/broker proof runs at main-bound gates only |
| 27 | `tests/test_pytest_with_report.py::test_coverage_switch_produces_xml_in_the_drop_zone` | 41.7 | A | A7 | G2 | 41.7 | coverage drop-zone proof at main-bound gates only |
| 28 | `tests/test_hq_authority_trusted.py::test_git_trusted_frame_reads_expired_authority_signed_fails_closed` | 41.2 | A | A1 | G1 | 41.2 | HQ guard/broker proof runs at main-bound gates only |
| 29 | `tests/test_hq_authority_trusted.py::test_git_rollback_and_cordon_are_honoured_in_both_modes[signed]` | 41.1 | A | A1 | G1 | 41.1 | HQ guard/broker proof runs at main-bound gates only |
| 30 | `tests/unit/modules/config/test_dependency_ledger.py::test_checked_config_dependency_ledger_is_current` | 34.9 | C | C9 | G8 | 23.5 | None; also a gate duplicate |
| 31 | `tests/unit/modules/config/test_dependency_ledger.py::test_dependency_ledger_records_honest_compatibility_and_no_ci_graduation` | 33.8 | C | C9 | G8 | 22.4 | None; also a gate duplicate |
| 32 | `tests/test_wire_release_publish.py::test_removing_or_changing_a_published_operation_still_fails[change-$.operations[name='hive.classify'].parameters[0].required: value changed]` | 32.6 | B | B6 | G5 | 24.6 | None; one real-CLI case kept (A12) |
| 33 | `tests/test_source_descriptors.py::test_current_installed_cli_resolves_registry_and_reaps_every_descriptor_stream[5]` | 32.4 | A | A8 | G2 | 32.4 | None; `[1]` stays in stateful as smoke |
| 34 | `tests/unit/modules/config/test_dependency_ledger.py::test_dependency_ledger_records_indirect_adapter_patch_points_exactly` | 32.0 | C | C9 | G8 | 20.6 | None; also a gate duplicate |
| 35 | `tests/test_contract_release_evidence.py::test_report_generator_check_is_read_only_and_deterministic` | 31.8 | C | C4 | G7 | 25.8 | Cross-process report determinism leans on A10 |
| 36 | `tests/test_contract_release.py::test_generator_is_offline_and_byte_identical_across_two_clean_runs` | 30.8 | C | C2 | G7 | 14.8 | None |
| 37 | `tests/test_wire_release_publish.py::test_removing_or_changing_a_published_operation_still_fails[remove-$.operations[name='hive.classify']: canonical operation was removed]` | 29.7 | B | B6 | G5 | 21.7 | None; one real-CLI case kept (A12) |
| 38 | `tests/test_hq_authority_backend.py::test_explicit_git_identity_policy_refresh_recovers_partial_rotation` | 28.2 | A | A1 | G1 | 28.2 | HQ guard/broker proof runs at main-bound gates only |
| 39 | `tests/test_hq_authority_backend.py::test_signed_own_manifest_publication_and_pinned_runtime_inventory` | 27.5 | A | A1 | G1 | 27.5 | HQ guard/broker proof runs at main-bound gates only |
| 40 | `tests/test_hq_authority_backend.py::test_restore_client_and_sqlite_preserves_receipt_floor` | 27.3 | A | A1 | G1 | 27.3 | HQ guard/broker proof runs at main-bound gates only |
| 41 | `tests/test_wire_release_publish.py::test_an_operation_added_after_the_baseline_is_immutable_once_published` | 25.8 | B | B6 | G5 | 17.8 | None; one real-CLI case kept (A12) |
| 42 | `tests/test_hq_seed_roundtrip_int.py::test_real_public_seed_latest_signed_git_mirror_and_selector` | 25.7 | A | A1 | G1 | 25.7 | HQ seed signing proof runs at main-bound gates only |
| 43 | `tests/test_hq_authority_fleet_mode.py::test_git_mode_command_raise_learn_join_cordon_and_keyless_raise` | 25.5 | A | A1 | G1 | 25.5 | HQ guard/broker proof runs at main-bound gates only |
| 44 | `tests/test_contract_release.py::test_validate_uses_candidate_exact_tree_and_write_removes_stale_target_entries` | 25.1 | C | C1 | G7 | 24.1 | None; real rebuild still covered by the checked-release test |
| 45 | `tests/test_gateway_contract.py::test_gateway_owned_wire_version_and_required_shape_drift_changes_artifact` | 24.5 | C | C6 | G7 | 5.0 | None |
| 46 | `tests/test_hq_authority_backend.py::test_signed_authority_receive_rejects_reused_signer_holder_and_ungranted_epoch[True]` | 24.1 | A | A1 | G1 | 24.1 | HQ guard/broker proof runs at main-bound gates only |
| 47 | `tests/test_contract_release.py::test_stale_file_directory_and_symlink_are_rejected_then_atomically_removed[directory-symlink]` | 23.8 | C | C1 | G7 | 22.8 | None; real rebuild still covered by the checked-release test |
| 48 | `tests/test_hq_authority_backend.py::test_git_adoption_recovers_lost_completion_after_later_config_edit` | 23.8 | A | A1 | G1 | 23.8 | HQ guard/broker proof runs at main-bound gates only |
| 49 | `tests/test_hive_observaloop.py::test_no_flag_does_not_install` | 23.5 | B | B3 | G4 | 23.4 | None; onboarding proven by onboard and integration tests |
| 50 | `tests/test_hive_observaloop.py::test_hive_init_succeeds_even_if_installer_explodes` | 23.3 | B | B3 | G4 | 23.2 | None; onboarding proven by onboard and integration tests |
| 51 | `tests/test_wire_release_publish.py::test_iterating_on_an_unpublished_release_refreshes_it_in_place` | 23.2 | B | B6 | G5 | 15.2 | None; one real-CLI case kept (A12) |
| 52 | `tests/test_hive_observaloop.py::test_flag_threaded_into_install` | 23.2 | B | B3 | G4 | 23.1 | None; onboarding proven by onboard and integration tests |
| 53 | `tests/test_contract_release.py::test_stale_file_directory_and_symlink_are_rejected_then_atomically_removed[empty-directory]` | 23.0 | C | C1 | G7 | 22.0 | None; real rebuild still covered by the checked-release test |
| 54 | `tests/test_hq_authority_backend.py::test_raw_frame_receive_requires_own_authorized_carrier` | 23.0 | A | A1 | G1 | 23.0 | HQ guard/broker proof runs at main-bound gates only |
| 55 | `tests/test_wire_release_publish.py::test_changing_an_existing_operation_can_publish_an_explicit_major_release` | 22.6 | B | B6 | G5 | 14.6 | None; one real-CLI case kept (A12) |
| 56 | `tests/test_capability_closeout.py::test_capability_closeout_is_reproducible` | 22.4 | B | B7 | G6 | 18.4 | None; same revision, one git process |
| 57 | `tests/test_hq_authority_backend.py::test_operator_renewal_after_expiry_preserves_authoritative_receipts` | 22.2 | A | A1 | G1 | 22.2 | HQ guard/broker proof runs at main-bound gates only |
| 58 | `tests/test_test_closure_operational_report.py::test_checked_report_rejects_even_an_optimistic_metric_mutation` | 22.1 | R | R | - | 22.1 | n/a (already deleted) |
| 59 | `tests/test_contract_release.py::test_stale_file_directory_and_symlink_are_rejected_then_atomically_removed[root-file]` | 22.0 | C | C1 | G7 | 21.0 | None; real rebuild still covered by the checked-release test |
| 60 | `tests/test_wire_schemas.py::test_actual_gate_cli_rejects_breaking_catalog_changes[result]` | 21.8 | B | B6 | G5 | 20.8 | None; one real-CLI case kept (A12) |
| 61 | `tests/test_wire_schemas.py::test_actual_gate_cli_rejects_breaking_catalog_changes[remove]` | 21.8 | B | B6 | G5 | 20.8 | None; one real-CLI case kept (A12) |
| 62 | `tests/test_wire_schemas.py::test_actual_gate_cli_rejects_same_major_not_mutations[allof-changed-def-$.allOf[0]->$ref]` | 21.8 | B | B6 | G5 | 20.8 | None; one real-CLI case kept (A12) |
| 63 | `tests/test_wire_schemas.py::test_actual_gate_cli_rejects_breaking_catalog_changes[policy]` | 21.7 | B | B6 | G5 | 20.7 | None; one real-CLI case kept (A12) |
| 64 | `tests/test_hq_authority_backend.py::test_raw_retired_key_cannot_publish_after_operator_retire` | 21.7 | A | A1 | G1 | 21.7 | HQ guard/broker proof runs at main-bound gates only |
| 65 | `tests/test_contract_release.py::test_stale_file_directory_and_symlink_are_rejected_then_atomically_removed[expected-directory-symlink]` | 21.7 | C | C1 | G7 | 20.7 | None; real rebuild still covered by the checked-release test |
| 66 | `tests/test_contract_release.py::test_writer_refuses_root_symlink_and_atomically_replaces_nested_symlink` | 21.5 | C | C1 | G7 | 20.5 | None; real rebuild still covered by the checked-release test |
| 67 | `tests/test_check_package_imports.py::test_real_repository_packages_classify_and_pass` | 21.5 | C | C11 | G8 | 0.0 | None (no change counted) |
| 68 | `tests/test_wire_schemas.py::test_actual_gate_cli_rejects_same_major_not_mutations[root-not-$]` | 21.5 | B | B6 | G5 | 20.5 | None; one real-CLI case kept (A12) |
| 69 | `tests/test_wire_schemas.py::test_actual_gate_cli_rejects_breaking_catalog_changes[signature]` | 21.4 | B | B6 | G5 | 20.4 | None; one real-CLI case kept (A12) |
| 70 | `tests/test_wire_schemas.py::test_actual_gate_cli_rejects_same_major_not_mutations[ref-sibling-not-$.coverage]` | 21.4 | B | B6 | G5 | 20.4 | None; one real-CLI case kept (A12) |
| 71 | `tests/test_wire_schemas.py::test_actual_gate_cli_allows_additive_catalog_operation` | 21.1 | A | A12 | G2 | 21.1 | None; this is the kept end-to-end case |
| 72 | `tests/test_wire_release_publish.py::test_editing_a_published_supported_release_in_place_fails` | 21.0 | B | B6 | G5 | 19.0 | None; one real-CLI case kept (A12) |
| 73 | `tests/test_wire_schemas.py::test_actual_gate_cli_rejects_same_major_not_mutations[dynamicref-changed-def-$.$defs.CompatGuard]` | 21.0 | B | B6 | G5 | 20.0 | None; one real-CLI case kept (A12) |
| 74 | `tests/test_hq_authority_backend.py::test_first_observer_rejects_expired_and_future_sender` | 20.6 | A | A1 | G1 | 20.6 | HQ guard/broker proof runs at main-bound gates only |
| 75 | `tests/test_wire_schemas.py::test_actual_gate_cli_rejects_breaking_catalog_changes[identity]` | 20.6 | B | B6 | G5 | 19.6 | None; one real-CLI case kept (A12) |
| 76 | `tests/test_work.py::test_concurrent_submit_processes_share_host_capacity_and_coalesce_retry[1]` | 20.5 | A | A11 | G2 | 20.5 | host-slot coalescing at main-bound gates only |
| 77 | `tests/test_test_closure_operational_report.py::test_checked_report_accounts_for_every_current_closure_without_inventing_savings` | 20.5 | R | R | - | 20.5 | n/a (already deleted) |
| 78 | `tests/test_wire_schemas.py::test_actual_gate_cli_rejects_breaking_catalog_changes[projection]` | 20.4 | B | B6 | G5 | 19.4 | None; one real-CLI case kept (A12) |
| 79 | `tests/test_work.py::test_concurrent_submit_processes_share_host_capacity_and_coalesce_retry[2]` | 20.1 | A | A11 | G2 | 20.1 | host-slot coalescing at main-bound gates only |
| 80 | `tests/spikes/test_bh_u562_3_unified_host_transport.py::test_every_current_tool_has_real_stdio_and_streamable_http_parity[stateless]` | 19.2 | A | A2 | G2 | 19.2 | None; spike evidence at main-bound gates only |
| 81 | `tests/test_test_closures.py::test_unsafe_or_non_test_pytest_selector_fails_closed[README.md]` | 19.1 | R | R | - | 19.1 | n/a (already deleted) |
| 82 | `tests/spikes/test_bh_u562_3_unified_host_transport.py::test_every_current_tool_has_real_stdio_and_streamable_http_parity[sessionful]` | 19.1 | A | A2 | G2 | 19.1 | None; spike evidence at main-bound gates only |
| 83 | `tests/test_test_closures.py::test_unsafe_or_non_test_pytest_selector_fails_closed[tests/closures.toml]` | 18.9 | R | R | - | 18.9 | n/a (already deleted) |
| 84 | `tests/test_capability_closeout.py::test_capability_closeout_proves_every_acceptance_boundary` | 18.8 | C | C10 | G6 | 18.3 | None |
| 85 | `tests/test_test_closures.py::test_unsafe_or_non_test_pytest_selector_fails_closed[tests/../README.md]` | 18.7 | R | R | - | 18.7 | n/a (already deleted) |
| 86 | `tests/test_test_closures.py::test_unsafe_or_non_test_pytest_selector_fails_closed[/tmp/test_plugins.py]` | 18.7 | R | R | - | 18.7 | n/a (already deleted) |
| 87 | `tests/test_test_closures.py::test_unsafe_or_non_test_pytest_selector_fails_closed[--version]` | 18.5 | R | R | - | 18.5 | n/a (already deleted) |
| 88 | `tests/test_test_closures.py::test_unsafe_or_non_test_pytest_selector_fails_closed[../tests/test_plugins.py]` | 18.5 | R | R | - | 18.5 | n/a (already deleted) |
| 89 | `tests/test_test_closures.py::test_unsafe_or_non_test_pytest_selector_fails_closed[tests\\test_plugins.py]` | 18.5 | R | R | - | 18.5 | n/a (already deleted) |
| 90 | `tests/test_hq_authority_backend.py::test_identity_policy_refresh_refuses_later_config_head` | 18.4 | A | A1 | G1 | 18.4 | HQ guard/broker proof runs at main-bound gates only |
| 91 | `tests/test_test_closures.py::test_unsafe_or_non_test_pytest_selector_fails_closed[tests/test_plugins.py::]` | 18.2 | R | R | - | 18.2 | n/a (already deleted) |
| 92 | `tests/test_source_descriptors.py::test_current_installed_cli_resolves_registry_and_reaps_every_descriptor_stream[3]` | 17.8 | A | A8 | G2 | 17.8 | None; `[1]` stays in stateful as smoke |
| 93 | `tests/test_hq_authority_backend.py::test_actual_console_reads_real_config_identity_and_protected_anchor` | 17.4 | A | A1 | G1 | 17.4 | HQ guard/broker proof runs at main-bound gates only |
| 94 | `tests/test_wire_release_publish.py::test_releases_below_the_supported_floor_must_be_marked_deprecated` | 17.3 | B | B6 | G5 | 15.3 | None; one real-CLI case kept (A12) |
| 95 | `tests/test_contract_release.py::test_two_fresh_processes_generate_byte_identical_release` | 17.1 | A | A10 | G2 | 17.1 | cross-process determinism at main-bound gates only |
| 96 | `tests/test_work.py::test_validation_mode_gates_molecule_clean_checkouts` | 16.8 | C | C12 | G9 | 12.8 | Low: incomplete restore could leak state |
| 97 | `tests/test_contract_release_evidence.py::test_compatibility_report_accounts_for_every_published_and_candidate_artifact` | 16.7 | C | C5 | G7 | 15.7 | None |
| 98 | `tests/test_work.py::test_epic_submit_and_finish_accept_reviewed_topology_over_leaf_limit` | 16.5 | C | C12 | G9 | 11.5 | Low: incomplete restore could leak state |
| 99 | `tests/test_test_closures.py::test_present_closure_cannot_validate_or_run_without_direct_tests` | 16.4 | R | R | - | 16.4 | n/a (already deleted) |
| 100 | `tests/test_hq_authority_backend.py::test_first_carrier_between_retire_prepare_and_receive_requires_retry` | 15.4 | A | A1 | G1 | 15.4 | HQ guard/broker proof runs at main-bound gates only |
| 101 | `tests/test_contract_release.py::test_checked_release_is_current_and_install_lookup_is_path_safe` | 15.1 | C | C3 | G7 | 7.1 | None; also a gate duplicate |
| 102 | `tests/test_hq_authority_backend.py::test_receipt_poll_is_idempotent_and_does_not_refresh` | 14.9 | A | A1 | G1 | 14.9 | HQ guard/broker proof runs at main-bound gates only |
| 103 | `tests/test_hq_authority_backend.py::test_git_adoption_keeps_pinned_config_parent_when_another_operator_advances` | 14.9 | A | A1 | G1 | 14.9 | HQ guard/broker proof runs at main-bound gates only |
| 104 | `tests/test_contract_release.py::test_stale_file_directory_and_symlink_are_rejected_then_atomically_removed[file-symlink]` | 14.7 | C | C1 | G7 | 13.7 | None; real rebuild still covered by the checked-release test |
| 105 | `tests/test_contract_release.py::test_stale_file_directory_and_symlink_are_rejected_then_atomically_removed[nested-file]` | 14.5 | C | C1 | G7 | 13.5 | None; real rebuild still covered by the checked-release test |
| 106 | `tests/test_hq_authority_backend.py::test_signed_old_authority_rollback_rejected_at_receive` | 14.5 | A | A1 | G1 | 14.5 | HQ guard/broker proof runs at main-bound gates only |
| 107 | `tests/test_hq_authority_backend.py::test_mutable_client_origin_and_config_cannot_replace_protected_authority` | 14.4 | A | A1 | G1 | 14.4 | HQ guard/broker proof runs at main-bound gates only |
| 108 | `tests/test_demo_live_ingress_matrix.py::test_parity_does_not_require_venv_or_ensurepip` | 14.4 | A | A6 | G2 | 14.4 | None; the demos key runs the same path |
| 109 | `tests/test_doctor_channels.py::test_doctor_still_exits_zero_with_a_channel_finding[stale]` | 14.1 | B | B1 | G3 | 13.6 | None; Pants verification has its own keys |
| 110 | `tests/test_doctor.py::test_doctor_payload_sections_are_structured` | 14.0 | B | B1 | G3 | 13.5 | None; Pants verification has its own keys |
| 111 | `tests/test_machine_json.py::test_doctor_json_and_text_render_the_same_object` | 13.8 | B | B1 | G3 | 13.3 | None; Pants verification has its own keys |
| 112 | `tests/test_daemon_mcp_http.py::test_authenticated_product_mcp_matches_exact_stdio_surface[stateless]` | 13.8 | A | A3 | G2 | 13.8 | stdio/HTTP parity checked at main-bound gates only |
| 113 | `tests/test_hq_authority_backend.py::test_bound_git_frame_grant_and_eligibility_reject_foreign_hq` | 13.7 | A | A1 | G1 | 13.7 | HQ guard/broker proof runs at main-bound gates only |
| 114 | `tests/test_hq_authority_backend.py::test_ungranted_predecessor_cannot_poison_epoch[epoch-999999]` | 13.7 | A | A1 | G1 | 13.7 | HQ guard/broker proof runs at main-bound gates only |
| 115 | `tests/test_doctor.py::test_doctor_payload_has_all_section_keys` | 13.4 | B | B1 | G3 | 12.9 | None; Pants verification has its own keys |
| 116 | `tests/test_hq_authority_backend.py::test_ungranted_predecessor_cannot_poison_epoch[key_id-SHA256:unknown]` | 13.3 | A | A1 | G1 | 13.3 | HQ guard/broker proof runs at main-bound gates only |
| 117 | `tests/test_daemon_mcp_http.py::test_authenticated_product_mcp_matches_exact_stdio_surface[sessionful]` | 13.3 | A | A3 | G2 | 13.3 | stdio/HTTP parity checked at main-bound gates only |
| 118 | `tests/test_machine_json.py::test_doctor_json_is_the_only_thing_on_stdout` | 13.3 | B | B1 | G3 | 12.8 | None; Pants verification has its own keys |
| 119 | `tests/test_doctor.py::test_doctor_render_includes_seats_section` | 13.3 | B | B1 | G3 | 12.8 | None; Pants verification has its own keys |
| 120 | `tests/test_triage_store.py::test_the_run_history_is_capped` | 13.2 | B | B4 | G4 | 12.9 | None; wiring covered by sibling tests |
| 121 | `tests/test_doctor.py::test_collect_group_auth_empty_when_no_repo_groups` | 13.2 | B | B1 | G3 | 12.7 | None; Pants verification has its own keys |
| 122 | `tests/test_doctor.py::test_collect_includes_node_id_and_beads_role` | 13.1 | B | B1 | G3 | 12.6 | None; Pants verification has its own keys |
| 123 | `tests/test_machine_json.py::test_doctor_payload_is_schema_versioned` | 13.1 | B | B1 | G3 | 12.6 | None; Pants verification has its own keys |
| 124 | `tests/test_doctor_channels.py::test_the_finding_reaches_the_rendered_warnings_section` | 13.1 | B | B1 | G3 | 12.6 | None; Pants verification has its own keys |
| 125 | `tests/test_doctor_channels.py::test_doctor_still_exits_zero_with_a_channel_finding[off_tag]` | 12.8 | B | B1 | G3 | 12.3 | None; Pants verification has its own keys |
| 126 | `tests/test_doctor.py::test_collect_includes_prefix_mismatches` | 12.8 | B | B1 | G3 | 12.3 | None; Pants verification has its own keys |
| 127 | `tests/test_mcp_doctor_resource.py::test_real_doctor_payload_carries_the_version_envelope` | 12.8 | B | B1 | G3 | 12.3 | None; Pants verification has its own keys |
| 128 | `tests/test_test_closure_certification_process.py::test_structural_mode_bootstraps_without_receipt_but_remains_fail_closed` | 11.4 | R | R | - | 11.4 | n/a (already deleted) |
| 129 | `tests/test_work.py::test_epic_submit_accepts_oldest_root_first_wrapper_followed_by_reviewed_children` | 11.3 | C | C12 | G9 | 7.3 | Low: incomplete restore could leak state |
| 130 | `tests/test_hq_authority_ceiling.py::test_git_renew_explicit_ceiling_and_expiry_fence` | 11.1 | A | A1 | G1 | 11.1 | HQ guard/broker proof runs at main-bound gates only |
| 131 | `tests/test_state_stream_integration.py::test_real_bh_stream_operator_feature_demo` | 10.9 | A | A9 | G2 | 10.9 | stream feature demo at main-bound gates only |
| 132 | `tests/test_operation_catalog.py::test_live_prompt_seams_are_guarded_and_mcp_projections_never_prompt` | 10.6 | C | C7 | G7 | 7.1 | None |
| 133 | `tests/test_work.py::test_buried_reversed_wrapper_rejects_malformed_parent_count` | 10.6 | C | C12 | G9 | 6.6 | Low: incomplete restore could leak state |
| 134 | `tests/test_hq_authority_backend.py::test_fleet_config_rollback_expiry_and_protected_publication` | 10.5 | A | A1 | G1 | 10.5 | HQ guard/broker proof runs at main-bound gates only |
| 135 | `tests/test_daemon_live_contract.py::test_every_live_contract_cell_links_to_an_exact_collected_test` | 10.3 | B | B5 | G4 | 10.0 | Small; lane collection still catches import errors |
| 136 | `tests/test_mcp_call_every_tool.py::test_serve_path_imports_are_resident_and_survive_a_vanished_env` | 10.3 | A | A5 | G2 | 10.3 | bh-fwhlu reproduction at main-bound gates only |
| 137 | `tests/test_test_closure_operational_report.py::test_registration_and_invalidation_contracts_fail_closed` | 10.3 | R | R | - | 10.3 | n/a (already deleted) |
| 138 | `tests/test_check_test_wall_clock.py::test_repository_tests_are_clean` | 10.1 | C | C11 | G8 | 0.0 | None (no change counted) |
| 139 | `tests/test_test_closure_promotion_policy.py::test_checked_policy_rejects_mutation` | 10.0 | R | R | - | 10.0 | n/a (already deleted) |

## Per-file table (every file of 30 s or more summed)

"To integration" counts every test in the file that moves, including tests under 10 s.
"B/C saved" includes the per-test rows plus the file-level fixture change. Group "none" means
the median test is under 0.5 s; only duration packing helps (see the durations plan).

| File | Tests | Sum s | Group | Child | To integration s | B/C saved s | Retired s | Note |
|---|---:|---:|:---:|:---:|---:|---:|---:|---|
| `tests/test_hq_authority_backend.py` | 72 | 1563.0 | A | G1 | 1563.0 | 0.0 | 0.0 | Whole file requests `backend`; all 72 items move |
| `tests/test_work.py` | 360 | 842.5 | C + A | G9/G2 | 40.6 | 123.2 | 0.0 | Session template hive (git init, remote, config) copied per test, ~0.25 s x ~350 tests; real git stays the subject of every lifecycle test |
| `tests/test_contract_release.py` | 315 | 523.1 | C + A | G7/G2 | 17.1 | 325.0 | 0.0 | 29 more tests in the 1-10 s band also pay a rebuild in `validate_release`/`write_release`; ~70% recoverable with the cached candidate |
| `tests/test_wire_schemas.py` | 103 | 279.4 | B + A | G5/G2 | 21.1 | 203.3 | 0.0 | The other 92 tests are in-process schema checks; unchanged |
| `tests/test_test_closures.py` | 38 | 257.9 | R | - | 0.0 | 0.0 | 257.9 | Removed by bh-rqmmm |
| `tests/test_wire_release_publish.py` | 9 | 233.5 | B | G5 | 0.0 | 174.2 | 0.0 | All 9 tests use the subprocess wire gate |
| `tests/test_cli.py` | 19 | 201.1 | B | G3 | 0.0 | 191.0 | 0.0 | Other 15 tests are cheap |
| `tests/test_test_closure_certification_process.py` | 6 | 187.5 | R | - | 0.0 | 0.0 | 187.5 | Removed by bh-rqmmm |
| `tests/test_hq_authority_trusted.py` | 49 | 132.9 | A | G1 | 129.6 | 0.0 | 0.0 | Only `test_git_*` items request `backend`; the in-process unit tests stay in stateful |
| `tests/test_transport_composition_roots.py` | 18 | 127.7 | C | G8 | 0.0 | 85.2 | 0.0 |  |
| `tests/test_contract_release_evidence.py` | 13 | 114.4 | C | G7 | 0.0 | 96.4 | 0.0 | Every test rebuilds the release or the report; one session build per worker |
| `tests/unit/modules/config/test_dependency_ledger.py` | 4 | 100.6 | C | G8 | 0.0 | 66.6 | 0.0 |  |
| `tests/test_doctor.py` | 178 | 97.4 | B | G3 | 0.0 | 81.8 | 0.0 | One more `doctor_payload` test at 5.4 s takes the same fake verifier |
| `tests/test_release_flow.py` | 146 | 81.6 | C | G9 | 0.0 | 30.0 | 0.0 | Template `hive` repo per session; median test 0.53 s |
| `tests/test_daemon_activity_api.py` | 35 | 80.9 | C | G10 | 0.0 | 40.0 | 0.0 | Profiled: 113 s of 117 s of app construction is `mcp.build_server` (FastMCP tool registry over `operations()`); build the registry once per process |
| `tests/test_hq_authority_trusted_publish.py` | 18 | 78.3 | A | G1 | 73.0 | 0.0 | 0.0 | Only `test_git_*` items request `backend`; the in-process unit tests stay in stateful |
| `tests/test_worktree_local_reclaim.py` | 62 | 78.3 | C | G9 | 0.0 | 15.0 | 0.0 | Template hive; two process-holder tests are real reclaim proofs and stay |
| `tests/test_work_top_level_composition.py` | 12 | 71.0 | C | G9 | 0.0 | 35.0 | 0.0 | Molecule template, as in `test_work`; 12 tests at ~6 s each |
| `tests/test_hive_observaloop.py` | 17 | 70.4 | B | G4 | 0.0 | 69.7 | 0.0 |  |
| `tests/test_daemon_mcp_http.py` | 19 | 66.1 | A + C | G2/G10 | 27.0 | 15.0 | 0.0 | 17 other tests pay `build_server` per app |
| `tests/test_capability_module_map.py` | 6 | 63.5 | B | G6 | 0.0 | 48.8 | 0.0 | The other tests are in-process classifiers |
| `tests/test_daemon_live_contract.py` | 6 | 62.5 | A + B | G2/G4 | 52.1 | 10.0 | 0.0 |  |
| `tests/test_test_closure_operational_report.py` | 5 | 62.4 | R | - | 0.0 | 0.0 | 62.4 | Removed by bh-rqmmm |
| `tests/test_hq_authority_no_expiry.py` | 15 | 60.6 | A | G1 | 60.1 | 0.0 | 0.0 | Only `test_git_*` items request `backend`; the in-process unit tests stay in stateful |
| `tests/test_test_closure_certification.py` | 52 | 57.6 | R | - | 0.0 | 0.0 | 57.6 | Removed by bh-rqmmm |
| `tests/test_source_descriptors.py` | 11 | 56.3 | A | G2 | 50.3 | 0.0 | 0.0 | `[1]` stays as the stateful smoke case |
| `tests/test_gateway_contract.py` | 12 | 52.9 | C | G7 | 0.0 | 15.0 | 0.0 | Session-shared baseline render for the other generation tests |
| `tests/test_mcp_resource.py` | 23 | 49.3 | C | G10 | 0.0 | 25.0 | 0.0 | Profiled: `operations()` / `_cli_projection` dominate; same once-per-process registry |
| `tests/test_herdr_plugin.py` | 178 | 49.3 | none | - | 0.0 | 0.0 | 0.0 | 178 CliRunner tests at 0.28 s median; packing only |
| `tests/spikes/test_bh_u562_3_unified_host_transport.py` | 3 | 46.4 | A | G2 | 46.4 | 0.0 | 0.0 | Whole spike file moves |
| `tests/test_pytest_with_report.py` | 8 | 44.3 | A | G2 | 41.7 | 0.0 | 0.0 |  |
| `tests/test_transport_inventory.py` | 10 | 44.2 | C | G10 | 0.0 | 15.0 | 0.0 | Projection building over `operations()`; memoize per process |
| `tests/test_machine_json.py` | 24 | 43.4 | B | G3 | 0.0 | 38.7 | 0.0 |  |
| `tests/test_doctor_channels.py` | 16 | 43.0 | B | G3 | 0.0 | 38.5 | 0.0 |  |
| `tests/test_capability_closeout.py` | 2 | 41.2 | B + C | G6 | 0.0 | 36.7 | 0.0 |  |
| `tests/test_mcp.py` | 22 | 38.5 | C | G10 | 0.0 | 18.0 | 0.0 | Same `build_server` cost per test |
| `tests/test_onboard_dag.py` | 36 | 37.7 | C | G9 | 0.0 | 9.0 | 0.0 | Template repo for the furnish tests |
| `tests/test_host_lease_cli.py` | 19 | 37.3 | C | G9 | 0.0 | 5.0 | 0.0 | Template bare repo for lease refs |
| `tests/test_host_heartbeat.py` | 62 | 36.2 | A (kept) | - | 0.0 | 0.0 | 0.0 | Real SSH signatures are the subject, but 62 tests at 0.48 s median: keep in stateful so signature regressions fail at submit |
| `tests/test_collection_prefilter.py` | 15 | 34.1 | B | G4 | 0.0 | 20.0 | 0.0 | `pytester.runpytest_inprocess` for the plugin cases; keep the xdist-worker case real |
| `tests/test_release_transaction.py` | 17 | 33.0 | A | G2 | 15.0 | 0.0 | 0.0 | The signed bump-commit and tag tests move; refusal tests stay |
| `tests/test_safety.py` | 132 | 32.9 | none | - | 0.0 | 0.0 | 0.0 | 132 tests at 0.23 s median; packing only |
| `tests/test_test_closure_shadow_policy.py` | 62 | 32.6 | R | - | 0.0 | 0.0 | 32.6 | Removed by bh-rqmmm |
| `tests/test_sync_remote.py` | 67 | 31.8 | none | - | 0.0 | 0.0 | 0.0 | 67 tests at 0.46 s median; packing only |
| `tests/test_hq_authority_fleet_mode.py` | 24 | 31.7 | A | G1 | 29.8 | 0.0 | 0.0 | Only `test_git_*` items request `backend`; the in-process unit tests stay in stateful |
| `tests/test_hive_hook_push_main.py` | 73 | 31.6 | none | - | 0.0 | 0.0 | 0.0 | 73 tests at 0.43 s median; packing only |
| `tests/test_localloop.py` | 117 | 31.2 | A | G2 | 12.3 | 0.0 | 0.0 | `test_cancel_reaps_the_whole_process_group` and `test_naive_terminate_leaks_a_grandchild` move; 115 cheap tests stay |
| `tests/test_operation_catalog.py` | 13 | 30.1 | C | G7 | 0.0 | 13.1 | 0.0 | Hoist `operations()` and share the AST corpus |

## Operator question: do we need our own wrappers for the subprocess dependencies?

### Recommendation

**No new `packages/` wrapper library now.** Group B does not need one. Each measured B hot spot
already has a seam: the `BuildVerifier` plugin port (Pants/uv build verification), `BdTransport`
in `packages/beadhive-bd-cli` and the Beads client (bd), the wire gate's own `Reader` protocol
(git object reads), and plain domain APIs (`triage_store.store`, node-id lookup). The one new
seam this plan needs is a read-only git object reader. It is about 30 lines inside each of the
two script families that use it (G5, G6), and it stays in-script because
`check_wire_schema_compat.py` is copied alone into temporary repositories by its tests.

Rationale:

1. **The plan would gain no seconds from a library.** The B savings (919.6 s) come entirely
   from existing seams. The largest process costs (1,892 s of signed-HQ tests, the work
   lifecycle on real git, live MCP transports) are cases where the process is the subject.
   Faking them is the "proof lost" this triage exists to avoid.
2. **Faking git behavior is a trap.** The expensive git use is worktrees, merges with
   `--no-ff` topology, SSH-signed commits and a pre-receive hook. An in-memory fake would have
   to reproduce exactly the behavior those tests assert, and it would drift from git without
   failing.
3. **The cost is large.** 88 `src/beadhive` modules pass `"git"` argv directly, through about 15
   private `_git` / `_run_git` helpers. Every workspace library is vendored into the published
   wheel, and a port needs a contract suite run against both the fake and the adapter
   (`packages/beadhive-hq-storage/.../contract.py` is 1,649 lines).
4. **bd is already on its path.** The Beads SDK refactor (`bh-bwnys` / `bh-sy36q`) is
   producing the beads-client port and fake. `test_work.py`'s `FakeBd`, which patches
   `work.run` at the argv level, should migrate to that fake when it lands, not to a parallel
   wrapper.
5. **Process spawning and HQ already have seams.** `tests/harness/processes.py` owns the
   process-start policy. HQ consumers can use the `HqControlPlane` protocol, and the SQL side has
   `InMemoryHqStorage` in `packages/beadhive-hq-storage`. `GitControlPlane` itself stays real
   (group A).

Are the existing seams enough? Yes, for this plan. `GitControlPlane` stays real because it is
the subject. The `hq-storage` port and its fake cover the SQL control-plane consumers. The beads
client and `BdTransport` cover bd. `BuildVerifier` covers uv/Pants builds. `ssh-keygen` needs no
wrapper: the tests that call it are group A, and key parsing is already in-process through
`cryptography`.

**When to revisit.** Build a library if a later triage finds a large set of tests where git
*writes* are incidental: hundreds of tests that create commits only to reach unrelated logic. If
that happens, the shape would be:

- **Name:** `beadhive-vcs` (`packages/beadhive-vcs`, library class, stdlib only, like
  `beadhive-hq-storage`).
- **Ports (`_ports.py`):** `ObjectReader` (`resolve(rev) -> Oid`, `read_blob(rev, path) -> bytes`,
  `list_tree(rev, prefix) -> tuple[str, ...]`), `RefStore` (`read_ref`, `compare_and_swap_ref`),
  and `CommitWriter` (`commit(tree: Mapping[str, bytes], parents, message, *, signer=None) ->
  Oid`). Worktrees, hooks and signing verification stay out of the port.
- **Fake (`_fake.py`):** `InMemoryRepo`, a dict of content-addressed blobs, trees and commits
  with CAS refs, verified by a `contract.py` suite that also runs against the real adapter (one
  `git cat-file --batch` / `git update-ref --stdin` process), exactly as
  `beadhive-hq-storage` pairs `_fake.py` with `contract.py`.

### Adoptable libraries surveyed

| Library | License | Fit | Verdict |
|---|---|---|---|
| dulwich | Apache-2.0 or GPL-2.0-or-later (dual) | Pure-Python git object store; `MemoryRepo` is a real in-memory repository; fast object reads with no process | The best candidate if B7's batched `cat-file` is not fast enough, or for the `CommitWriter` adapter above. It does not reproduce worktrees, hooks or signing verification. A new dependency: recommendation only |
| pygit2 | GPL-2.0 with linking exception (libgit2 the same) | Fast libgit2 bindings | Not permissive in the strict sense, and it adds a native library to the published wheel. Not recommended |
| GitPython | BSD-3-Clause | Wraps the `git` CLI | Still spawns git, so no speed gain and no fake. Not recommended |
| pytest-subprocess | MIT | Registers fake argv for `subprocess.Popen` | This is the argv patching ruled out for group B. Not recommended (at most for argv-contract tests such as replacing ad-hoc `FakeBd` argv parsing) |
| pytester (pytest built-in) | MIT | In-process pytest runs | Adopt for B5 (`test_collection_prefilter`); already installed |
| cryptography | Apache-2.0 or BSD-3-Clause | In-process SSH key parsing, already a dependency | Already in use; no wrapper needed for `ssh-keygen` |

## Optional cuts not in the projection

These tests assert that a checked-in artifact is current against the live repository, which
`architecture-structural-check` (in the `fast` profile, at every boundary) or
`generated-evidence-check` also checks. Removing the duplicate assertions would save about 70 s
more on top of the C residuals. Removing tests is an operator call, so this plan keeps every
assertion and counts no saving here:

- `test_check_package_imports.py::test_real_repository_packages_classify_and_pass` (21.5 s;
  `check_package_imports.py`)
- `test_check_test_wall_clock.py::test_repository_tests_are_clean` (10.1 s)
- `test_dependency_ledger.py::test_checked_config_dependency_ledger_is_current` (34.9 s;
  `config_dependency_ledger.py --check`)
- `test_contract_release.py::test_checked_release_is_current_and_install_lookup_is_path_safe`
  and the transport composition `--check` (the residual after C)

The integration growth at main-bound gates could also be offset by giving the signed-HQ family
its own attest key, so path selectors carry it forward when no HQ code changed. That is a key
catalog decision for the operator and is not part of this plan.

## Refreshing `tests/harness/test_durations.json` (G11)

Today `scripts/refresh_test_durations.py --min-seconds 0.5` keeps 2,009 entries across all
lanes. Stateful coverage is 1,624 of 10,603 items. Every other test, and every new test, gets
the global median of 0.048 s, so worksteal packing under-predicts by about 8%. The plan:

1. **Record every test.** Use `--min-seconds 0` and round to 0.01 s. Take the per-test median
   from the last N full-lane reports of each lane (stateful, root-composition, integration) and
   skip partial selective reports (filter on report test count), so a 30-test selective run does
   not set a medians row.
2. **Add a per-module fallback.** Emit a `modules` table (median per module). `duration_order.load`
   uses it for unknown tests before falling back to the global median, so a new parametrized
   case in `test_hq_authority_backend.py` is packed as a 20 s item, not a 0.05 s one.
3. **Prune and report.** Drop keys that `pytest --collect-only -q` no longer collects, and print
   coverage (known/collected) per lane on every refresh. The `bh-rqmmm` deletion already removed
   64 stale lines by hand.
4. **Re-run after every child group lands.** Add a `just refresh-test-durations` recipe. Groups A,
   B and C change the distribution heavily: A removes the 140 s items from stateful, and B and C
   turn 20 s items into 1 s items.
5. **Accept on prediction error.** Done when stateful coverage is 100% and the predicted lane sum
   is within 3% of the next full run's JUnit sum.

The output stays a single JSON file. At about 12,000 entries it is roughly 1.3 MB; G11 records
the real size and may switch to one line per entry for diff readability.

## Child beads (filed, not implemented)

Every child is `discovered-from: bh-e0lux`. The two A groups depend on `bh-i3nc3` (the keys
cutover). No child depends on a wrapper-library bead, because none is recommended.

| Key | Bead | Title | Group | Stateful seconds |
|---|---|---|:---:|---:|
| G1 | `bh-100df` | test(hq): mark the signed-HQ backend tests integration | A | 1,892.3 to integration |
| G2 | `bh-dcwkf` | test(stateful): mark live process-boundary tests integration | A | 359.2 to integration |
| G3 | `bh-um0rg` | test(doctor): make doctor tests hermetic with a fake build verifier | B | 362 saved |
| G4 | `bh-o6f8s` | test(stateful): replace incidental bd, worktree and nested-pytest processes with in-process seams | B | 113 saved |
| G5 | `bh-c2gnl` | refactor(wire): run the wire-schema gate in-process through its Reader seam | B | 377 saved |
| G6 | `bh-gf36w` | refactor(scripts): batch git object reads in the capability evidence scripts | B/C | 85 saved |
| G7 | `bh-fiy4a` | test(contract): reuse cached release candidates instead of rebuilding per validation | C | 450 saved |
| G8 | `bh-qxg5c` | test(evidence): build repository-wide evidence once per session | C | 152 saved |
| G9 | `bh-gb56v` | test(work): restore session repo and molecule templates instead of rebuilding per test | C | 217 saved (estimate) |
| G10 | `bh-svvnj` | refactor(mcp): build the operation catalog and MCP registry once per process | C | 113 saved (estimate) |
| G11 | `bh-0br25` | test(harness): refresh test_durations.json for full coverage | packing | n/a |

Suggested order, by seconds per unit of risk: G3, G5, G7 (all P1), then G4, G8, G6, G10, G9,
G11 after each group, and G1/G2 together with `bh-i3nc3`.

## Side findings (not filed)

- `bh doctor`, when run from the beadhive checkout, spends about 13 s in the real Pants build
  verifier (B1). This is a product cost as well as a test cost. Related: `bh-b5v4y` (the doctor
  read-path floor).
- `run.child_env` -> `_fill_git_workspace` costs about 0.16 s per subprocess cold (27 calls,
  4.4 s in the doctor profile). This is already filed as `bh-9mhkx`.
- `kernel.operations.operations()` is uncached (0.48 s per call; `_raw_cli_rows` ran about
  2,000 times in one profiled `test_mcp_resource` test). Related: `bh-0yll6`. G10 covers the
  test side.
- `src/beadhive/hq_sql_session.py:35` raises a `SyntaxWarning` (an invalid backslash-space
  escape) every time a repo-scan script parses it.
- Two existing tests already fake at the argv level: `FakeBd` patches `work.run`, and
  `test_release_flow.py` wraps `release.subprocess.Popen`. Both predate this rule and are out of
  scope here.
