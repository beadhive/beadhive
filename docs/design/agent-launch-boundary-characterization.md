# Agent-launch boundary characterization

Status: frozen compatibility contract, deliberately migrated by `bh-5wuc0.5`

Evidence source revision: `5e4a76700e85b4bdda97640eeec5c2bbac632b46`

This record freezes the launch slice delivered by `bh-4bhs7` and records the deliberate ownership
move into `modules/agents` and `integrations/herdr`. The original measurements remain tied to their
named source revision. Current owners below are enforced by the executable compatibility contract;
the import-compatible facade must not silently change behavior.

## Authority matrix

| Concern | Current authoritative owner | Frozen evidence |
| --- | --- | --- |
| Seat contracts | `beadhive.seat_contracts` | `test_authoritative_owner_matrix_resolves_to_current_implementation_modules`; `test_six_row_authority_transport_is_exact_and_redacted` |
| Generic profiles | `beadhive.agent_launch_profile` | `test_legacy_public_imports_and_model_field_order_are_frozen`; `test_managed_seat_matrix_has_exact_provider_authority` |
| Herdr profiles | `beadhive.herdr_launch_profile` | `test_legacy_public_imports_and_model_field_order_are_frozen`; `test_operation_and_generation_are_exact_receipt_fences` |
| Prepared launch | `beadhive.integrations.herdr.cli_application._launch_cmd`, reached through the typed CLI application seam | `test_launch_preflight_finishes_before_native_claim`; `test_launch_exact_create_race_fails_at_last_safe_point_without_mutation` |
| Portable receipts | Generic core in `agent_launch_profile`; Herdr extension in `herdr_launch_profile` | `test_json_discriminators_redaction_and_checked_schema_boundary_are_frozen`; `test_extended_receipt_preserves_strict_base_and_exact_correlation` |
| Generations | Herdr launch profile plus `integrations.herdr.cli_application` generation fences | `test_reuse_refuses_stale_generation_and_conflicting_operation`; `test_stale_generation_cannot_authorize_successor_cleanup` |
| Adoption/recovery | `integrations.herdr.cli_application._recover_managed_generation`; durable core work state remains Beadhive-owned | `test_restart_reconciliation_adopts_only_exact_durable_generation`; `test_server_loss_recovery_advances_generation_without_completing_work` |
| Abort/compensation | `integrations.herdr.cli_application._launch_fail` and exact created-pane cleanup; native claim retention/release remains Beadhive work authority | `test_launch_warmup_failure_retains_claim_and_only_closes_created_pane`; `test_launch_startup_failure_closes_created_pane_and_keeps_native_resources` |
| Teardown | `integrations.herdr.cli_application._reap_cmd` plus exact receipt/generation/ownership proof | `test_generation_fenced_receipt_reap_is_idempotent_after_exact_pane_disappears`; `test_receipt_reap_preserves_unrelated_pane_at_expected_locator` |
| CLI output | Thin Typer projection in `integrations.herdr.cli`; path/signature inventory in `operation_catalog` | `test_typer_paths_and_catalog_signatures_are_frozen`; `test_herdr_presentation_is_a_thin_production_application_projection` |
| Error codes | Application-owned `_launch_fail` stage output for `launch`; `_lifecycle_failure` and the lifecycle schema for other JSON commands | `test_watch_timeout_and_reap_refusal_emit_stable_json_errors`; `test_launch_startup_failure_closes_created_pane_and_keeps_native_resources` |

The target direction remains inward: callers depend on provider-neutral agent policy and ports;
the Herdr adapter depends on those contracts; core never imports Herdr. The top-level
`herdr_plugin.py` is now only the composition/import-compatibility facade. The retained application
orchestration remains characterized debt, not permission for the provider adapter to own core
prepare/commit/abort policy.

## Defect-path characterization

The following are the minimum movement closure. Node IDs are intentionally exact so later file
moves must preserve or consciously replace each proof.

| Defect seam | Exact executable proof | Preserved result |
| --- | --- | --- |
| Warm launch | `tests/test_herdr_plugin.py::test_launch_warmup_failure_retains_claim_and_only_closes_created_pane` | Warm-up failure closes only the new pane and retains native work evidence. |
| Reuse mismatch | `tests/test_herdr_plugin.py::test_strict_reuse_requires_target_pane_workspace_and_exact_cwd`; `tests/test_herdr_plugin.py::test_launch_exact_reuse_post_observation_wrong_pane_fails_without_destructive_cleanup` | Reuse requires exact target, pane, Space, cwd, profile, and current observation; mismatch does not destructively clean up. |
| Stale generation | `tests/test_herdr_plugin.py::test_reuse_refuses_stale_generation_and_conflicting_operation`; `tests/test_herdr_managed_matrix.py::test_generation_fenced_reap_is_idempotent_and_cannot_stop_successor` | A stale or conflicting generation can neither authorize reuse nor stop its successor. |
| Partial allocation | `tests/test_herdr_plugin.py::test_launch_exact_create_post_observation_fails_closed_and_cleans_up`; `tests/test_herdr_plugin.py::test_launch_exact_create_race_fails_at_last_safe_point_without_mutation` | Failed final observation compensates only the newly created pane; a pre-mutation race mutates nothing. |
| Start failure | `tests/test_herdr_plugin.py::test_launch_startup_failure_closes_created_pane_and_keeps_native_resources`; `tests/test_herdr_plugin.py::test_duplicate_start_race_closes_loser_and_refuses_stale_winner_metadata` | The new pane is compensated, native lifecycle resources remain, and a duplicate winner must still pass generation proof. |
| Restart recovery | `tests/test_herdr_managed_matrix.py::test_restart_reconciliation_adopts_only_exact_durable_generation`; `tests/test_herdr_managed_matrix.py::test_server_loss_recovery_advances_generation_without_completing_work`; `tests/test_herdr_managed_matrix.py::test_server_loss_recovery_refuses_previous_live_generation` | Exact durable evidence may be reconciled; process loss advances a generation without completing work; a still-live prior generation refuses recovery. |
| Safe teardown | `tests/test_herdr_plugin.py::test_generation_fenced_receipt_reap_is_idempotent_after_exact_pane_disappears`; `tests/test_herdr_plugin.py::test_receipt_reap_preserves_unrelated_pane_at_expected_locator` | Exact reap is idempotent and never lets a stale receipt close unrelated content. |

## Compatibility inventory

The executable compatibility test freezes imports from:

- `beadhive.seat_contracts`: `SeatContract`, `seat_contract`;
- `beadhive.agent_launch_profile`: the generic profile, resolved profile, receipt, enums, policy,
  resolve, parse, and environment-reader functions; and
- `beadhive.herdr_launch_profile`: the exact target/profile/receipt types plus digest, build,
  parse, observation, consume, and resolve functions.

Field order, v1 discriminators, strict extra-field behavior, and receipt redaction are public
serialization constraints. The checked
`docs/schemas/herdr-lifecycle-receipt-v1.schema.json` covers `status`, `add`, `ps`, `spawn`,
`dispatch`, `watch`, `attach`, and `reap`; it does **not** cover `plugin herdr launch`. Generic
profile and Herdr launch-receipt JSON are currently Pydantic-model contracts without checked
schema artifacts. This gap is recorded rather than papered over; the later schema-publication
bead owns adding versioned artifacts.

The Typer paths `plugin herdr launch`, `spawn`, and `reap`, including catalog parameter names,
remain stable. Existing tests patch these supported movement seams directly:
`_session_snapshot`, `_snapshot_agent_records`, `_metadata_tokens`, `_launch_warm`, `_close_pane`,
`_strict_live_target`, `_validate_managed_generation`, `_recover_managed_generation`, and
`_generation_reap_matches`. `role.py` is the generic-profile caller, `herdr_plugin.py` composes
the typed provider and preserves the historical import identity,
`integrations/herdr/cli_application.py` owns the migrated application seams,
`integrations/herdr/cli.py` projects them through Typer, root `cli.py` projects the resolved profile
in role-explain JSON, and `operation_catalog.py` owns the declarative CLI inventory. These
dynamic/import seams are tested by
`test_supported_monkeypatch_points_and_dynamic_callers_remain_addressable` and
`test_herdr_presentation_is_a_thin_production_application_projection`.

Native Claude Task children and Codex collaboration children remain explicitly unmanaged. Their
native correlation variables are an authority boundary, while `BH_ROLE` is context only. Even
when a native child inherits a valid parent's `BH_AGENT_LAUNCH_RECEIPT`, the child receives no
managed launch, completion, compensation, or teardown authority. A harness without either native
child marker remains managed when it presents a valid receipt, and invalid receipt evidence still
fails closed. `test_native_children_ignore_valid_inherited_parent_receipts` freezes both the
Claude `CLAUDE_TASK_ID` and Codex `CODEX_THREAD_ID` cases;
`test_managed_external_harness_accepts_valid_receipt_without_native_child_marker` freezes the
surrounding positive case.

## Baseline and validation evidence

Validation cadence is **strict** because this slice freezes public imports, JSON shapes, CLI
paths, lifecycle fences, and cleanup authority.

At source revision `5e4a76700e85b4bdda97640eeec5c2bbac632b46`, the focused command was:

```text
PYTHONPATH=src <repo-venv>/bin/pytest -q -n 8 \
  tests/test_agent_launch_profile.py tests/test_herdr_launch_profile.py \
  tests/test_herdr_managed_matrix.py tests/test_herdr_plugin.py tests/test_role.py \
  --cov=beadhive.seat_contracts --cov=beadhive.agent_launch_profile \
  --cov=beadhive.herdr_launch_profile --cov=beadhive.herdr_plugin
```

Result: **322 passed in 14.28 s** (15.455 s measured shell wall time). Combined line coverage was
**82%**: `seat_contracts.py` 90% (20 statements), `agent_launch_profile.py` 96% (184),
`herdr_launch_profile.py` 87% (251), and `herdr_plugin.py` 80% (1,823). There were no expected
failures, flakes, quarantines, or skips in this focused closure.

RepoWise wrapper status at the same revision reported `ok`, `0 commits behind`, and a 112.2 MiB
index. Native status identified indexed ancestor
`739349806ead27c94282219b1befb796ba73b583`; it is an ancestor of the evidence revision. Native
`context`/`health` detail queries produced no payload before the bounded local command returned,
and no current dynamic coverage was ingested. RepoWise therefore corroborates index availability
only; it is not claimed as fresh per-test coverage or as authority for the owner matrix.

Final focused and repository-gate results are recorded in the bead check attestation. The advisory
current closure remains `plugin.herdr` in `tests/closures.toml`; the target `module.agents` row
remains correctly `absent` until the extraction creates that module.

The post-characterization closure added the executable compatibility file and completed with
**330 passed in 13.83 s**, with the same **82%** aggregate: `seat_contracts.py` 90%,
`agent_launch_profile.py` 96%, `herdr_launch_profile.py` 87%, and `herdr_plugin.py` 80%. This is
the expected eight-test increase with no failures.

Those measurements belong to submitted source commit
`5cc539b2c347d4d0345ea638341311f99e8ae0e6`, tree
`0814dda67ee8decac07fb2432cf0bc5adcd5c25d`. Its repository gate completed with **7,222 passed,
11 skipped in 138.92 s** after lint, format, Markdown, SBOM/license, import-boundary, and wire-schema
checks passed. This paragraph records the immutable measured source revision; it deliberately does
not claim that a document can contain the digest of the later commit that contains the document.

## Non-goals

The frozen baseline did not authorize behavior changes, native in-process child management, or
broader cleanup authority. Later extraction beads may deliberately move owners and introduce typed
ports only while comparing their results with this baseline and updating the executable owner
matrix, as `bh-5wuc0.4` and `.5` do.
